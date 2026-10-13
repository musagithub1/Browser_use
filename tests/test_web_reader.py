import gzip
import unittest
from unittest.mock import patch

import httpx

from jev_linux.web_reader import MAX_DECODED_BYTES, MAX_REDIRECTS, WebReader, WebReadError


class Chunks(httpx.SyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


class WebReaderTests(unittest.TestCase):
    def reader(self, handler, **client_options):
        client = httpx.Client(transport=httpx.MockTransport(handler), **client_options)
        self.addCleanup(client.close)
        reader = WebReader(client=client)
        self.addCleanup(reader.close)
        return reader

    def page(self, content, kind="text/html", **kwargs):
        return self.reader(lambda request: httpx.Response(200, headers={"Content-Type": kind}, content=content), **kwargs)

    def test_html_removes_scripts_styles_hidden_templates_and_head_metadata(self):
        reader = self.page("""<html><head><title>Unused title</title><style>secret css</style></head>
            <body><h1>Public article</h1><p>One &amp; two <strong>words</strong>.</p>
            <script>doSomething('secret js');</script><style>more secret css</style>
            <noscript>Enable JS fallback</noscript><svg><text>decorative text</text></svg>
            <template><p>hidden template</p></template><p>Last paragraph.</p></body></html>""")
        result = reader.read("https://example.com/article")
        self.assertEqual(result, {"url": "https://example.com/article",
                                  "content": "Public article\nOne & two words.\nLast paragraph.", "truncated": False})

    def test_xhtml_text_and_table_cell_separation(self):
        reader = self.page("<html><body><h1>Data</h1><table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>10</td></tr></table></body></html>",
                           kind="Application/XHTML+XML; charset=utf-8")
        self.assertEqual(reader.read("https://example.com")["content"], "Data\nName Value\nA 10")

    def test_plain_text_and_json_keep_meaningful_internal_spacing(self):
        for kind, text in (("text/plain; charset=utf-8", "  café 🍵\n\n  indented  line  "),
                           ("application/json", '{"message": "two  spaces", "value": 42}')):
            with self.subTest(kind=kind):
                reader = self.page(text, kind=kind)
                self.assertEqual(reader.read("https://example.com/data")["content"], text.strip())

    def test_character_truncation_is_unicode_safe(self):
        reader = self.page("café 🍵 is a drink", kind="text/plain")
        result = reader.read("https://example.com", max_chars=6)
        self.assertEqual(result["content"], "café 🍵")
        self.assertTrue(result["truncated"])
        exact = self.page("café 🍵", kind="text/plain").read("https://example.com", max_chars=6)
        self.assertFalse(exact["truncated"])

    def test_runtime_text_limit_rejects_invalid_or_unbounded_sizes_before_network(self):
        requests = []
        reader = self.reader(lambda request: requests.append(request) or httpx.Response(200, text="unused"))
        for limit in (0, -1, 20001, True, 1.5, "100", None):
            with self.subTest(limit=limit), self.assertRaisesRegex(WebReadError, "limit"):
                reader.read("https://example.com", max_chars=limit)
        self.assertEqual(requests, [])

    def test_invalid_and_credential_urls_are_rejected_without_secret_output(self):
        requests = []
        reader = self.reader(lambda request: requests.append(request) or httpx.Response(200, text="unused"))
        urls = ["file:///etc/passwd", "javascript:alert(1)", "https://user:secret-token@example.com",
                "https://@example.com", "https://example.com:badport", "https://example.com\nAuthorization: secret-token", [], None]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(WebReadError) as caught:
                reader.read(url)
            self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual(requests, [])

    def test_client_auth_headers_and_cookies_are_not_sent_even_after_redirect(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "/final", "Set-Cookie": "session=private; Path=/"})
            return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="Public content")
        reader = self.reader(handler, auth=("user", "secret"), cookies={"session": "old-private"},
                             headers={"Authorization": "Bearer secret-token", "Proxy-Authorization": "secret-proxy"},
                             follow_redirects=True)
        result = reader.read("https://example.com/start")
        self.assertEqual(result["url"], "https://example.com/final")
        self.assertEqual(result["content"], "Public content")
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("proxy-authorization", request.headers)
            self.assertNotIn("cookie", request.headers)

    def test_redirect_to_credential_url_or_non_web_scheme_stops_before_request(self):
        for destination in ("https://user:secret-token@other.example.com", "file:///etc/passwd"):
            with self.subTest(destination=destination):
                requests = []
                reader = self.reader(lambda request: requests.append(request) or httpx.Response(302, headers={"Location": destination}))
                with self.assertRaises(WebReadError) as caught:
                    reader.read("https://example.com")
                self.assertEqual(len(requests), 1)
                self.assertNotIn("secret-token", str(caught.exception))

    def test_redirect_loop_is_bounded_and_missing_destination_is_clear(self):
        requests = []
        reader = self.reader(lambda request: requests.append(request) or httpx.Response(302, headers={"Location": "/again"}))
        with self.assertRaisesRegex(WebReadError, "too many"):
            reader.read("https://example.com")
        self.assertEqual(len(requests), MAX_REDIRECTS + 1)
        missing = self.reader(lambda request: httpx.Response(302))
        with self.assertRaisesRegex(WebReadError, "without an address"):
            missing.read("https://example.com")

    def test_http_error_never_returns_response_body_or_headers(self):
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                reader = self.reader(lambda request: httpx.Response(status, text="secret-token and private response body"))
                with self.assertRaisesRegex(WebReadError, f"HTTP {status}") as caught:
                    reader.read("https://example.com")
                self.assertNotIn("secret-token", str(caught.exception))

    def test_network_and_timeout_errors_are_scrubbed(self):
        for error_type in (httpx.ConnectError, httpx.ReadTimeout, OSError):
            with self.subTest(error_type=error_type):
                def handler(request):
                    raise error_type("debug request includes Authorization: secret-token")
                reader = self.reader(handler)
                with self.assertRaisesRegex(WebReadError, "could not be reached") as caught:
                    reader.read("https://example.com")
                self.assertNotIn("secret-token", str(caught.exception))

    def test_unsupported_and_missing_content_types_are_rejected(self):
        for kind in ("application/pdf", "image/png", "application/octet-stream", ""):
            with self.subTest(kind=kind):
                reader = self.page("not a public text page", kind=kind)
                with self.assertRaisesRegex(WebReadError, "readable text page"):
                    reader.read("https://example.com")

    def test_empty_script_only_and_login_pages_report_unavailable_content(self):
        pages = ["<html><head><title>React App</title></head><body><div id='root'></div><script>renderApp()</script></body></html>",
                 "<html><body><form><input type='password'><script>login()</script></form></body></html>",
                 "<h1>Sign in</h1><form><label>Password</label><input type='password'></form>"]
        for page in pages:
            with self.subTest(page=page), self.assertRaisesRegex(WebReadError, "JavaScript|login"):
                self.page(page).read("https://example.com")
        with self.assertRaisesRegex(WebReadError, "no readable text"):
            self.page(" \n \t", kind="text/plain").read("https://example.com")

    def test_two_megabyte_limit_applies_to_decoded_compressed_response(self):
        compressed = gzip.compress(b"x" * (MAX_DECODED_BYTES + 1))
        self.assertLess(len(compressed), MAX_DECODED_BYTES)
        stream = Chunks(compressed)
        reader = self.reader(lambda request: httpx.Response(200, headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"}, stream=stream))
        with self.assertRaisesRegex(WebReadError, "too large"):
            reader.read("https://example.com")
        self.assertTrue(stream.closed)

    def test_exact_decoded_byte_limit_is_allowed_with_truncated_text(self):
        stream = Chunks(b"x" * MAX_DECODED_BYTES)
        reader = self.reader(lambda request: httpx.Response(200, headers={"Content-Type": "text/plain"}, stream=stream))
        result = reader.read("https://example.com", max_chars=3)
        self.assertEqual(result["content"], "xxx")
        self.assertTrue(result["truncated"])
        self.assertTrue(stream.closed)

    def test_total_deadline_stops_slow_stream_and_closes_response(self):
        stream = Chunks(b"some text")
        reader = self.reader(lambda request: httpx.Response(200, headers={"Content-Type": "text/plain"}, stream=stream))
        with patch("jev_linux.web_reader.time.monotonic", side_effect=[0, 1, 31]):
            with self.assertRaisesRegex(WebReadError, "timed out"):
                reader.read("https://example.com")
        self.assertTrue(stream.closed)

    def test_reader_close_preserves_injected_client_and_closes_owned_client(self):
        external = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        self.addCleanup(external.close)
        WebReader(client=external).close()
        self.assertFalse(external.is_closed)
        owned = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        self.addCleanup(owned.close)
        with patch("jev_linux.web_reader.httpx.Client", return_value=owned):
            reader = WebReader()
        reader.close()
        reader.close()
        self.assertTrue(owned.is_closed)
        with self.assertRaisesRegex(WebReadError, "closed"):
            reader.read("https://example.com")


if __name__ == "__main__":
    unittest.main()
