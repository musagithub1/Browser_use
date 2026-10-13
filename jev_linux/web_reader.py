"""Read public web text for a requested task without browser cookies or scripts."""

from html.parser import HTMLParser
import time
from urllib.parse import urljoin, urlsplit
import httpx


MAX_DECODED_BYTES = 2_000_000
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = 15
READ_DEADLINE = 30


def _validated_url(url):
    invalid = "Reading a web page requires an HTTP or HTTPS address without embedded credentials."
    if (not isinstance(url, str) or not url or len(url) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in url)):
        raise WebReadError(invalid)
    try:
        parsed = urlsplit(url)
        if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("Invalid address.")
        parsed.port  # Reject malformed ports before handing the URL to httpx.
        result = httpx.URL(url)
        if not result.host:
            raise ValueError("Invalid host.")
        return result
    except (TypeError, ValueError, httpx.InvalidURL):
        raise WebReadError(invalid) from None


class WebReadError(RuntimeError):
    pass


class _TextParser(HTMLParser):
    IGNORED = {"head", "title", "script", "style", "noscript", "svg", "template"}
    BLOCKS = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6",
              "tr", "section", "article", "blockquote", "pre", "header", "footer"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
        self.password_input = False

    def handle_starttag(self, tag, attrs):
        tag = tag.split(":")[-1]
        if tag in self.IGNORED:
            self.hidden += 1
        elif not self.hidden and tag in self.BLOCKS:
            self.parts.append("\n")
        elif not self.hidden and tag in {"td", "th"}:
            self.parts.append(" ")
        elif not self.hidden and tag == "input":
            self.password_input |= any(name.casefold() == "type" and (value or "").casefold() == "password"
                                       for name, value in attrs)

    def handle_endtag(self, tag):
        tag = tag.split(":")[-1]
        if tag in self.IGNORED:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class WebReader:
    def __init__(self, client=None):
        self._owns_client = client is None
        self._closed = False
        self.client = client if client is not None else httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False)

    def read(self, url, max_chars=16000):
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 20000:
            raise WebReadError("The web text limit must be an integer between 1 and 20,000 characters.")
        if self._closed:
            raise WebReadError("The web reader is closed.")
        current_url = _validated_url(url)
        deadline = time.monotonic() + READ_DEADLINE
        try:
            for redirect in range(MAX_REDIRECTS + 1):
                if time.monotonic() > deadline:
                    raise WebReadError("Reading the web page timed out. Try again.")
                # A raw request avoids merging default client cookies, auth,
                # or provider headers. auth=None also disables client auth.
                request = httpx.Request("GET", current_url, headers={
                    "User-Agent": "Jev-Linux/1.0",
                    "Accept": "text/html,application/xhtml+xml,text/plain,application/json",
                    "Accept-Encoding": "gzip, deflate",
                }, extensions={"timeout": httpx.Timeout(REQUEST_TIMEOUT).as_dict()})
                response = self.client.send(request, auth=None, follow_redirects=False, stream=True)
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise WebReadError("The web page returned a redirect without an address.")
                        if redirect == MAX_REDIRECTS:
                            raise WebReadError("The web page redirected too many times.")
                        current_url = _validated_url(urljoin(str(response.url), location))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise WebReadError(f"The web page could not be read (HTTP {response.status_code}).")
                    kind = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                    if kind not in {"text/html", "text/plain", "application/xhtml+xml", "application/json"}:
                        raise WebReadError("That address does not return a readable text page.")
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if time.monotonic() > deadline:
                            raise WebReadError("Reading the web page timed out. Try again.")
                        if len(data) + len(chunk) > MAX_DECODED_BYTES:
                            raise WebReadError("The web page is too large to read in one task step.")
                        data.extend(chunk)
                    text = bytes(data).decode(response.encoding or "utf-8", errors="replace")
                    if kind in {"text/html", "application/xhtml+xml"}:
                        parser = _TextParser()
                        parser.feed(text)
                        parser.close()
                        text = "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())
                        if parser.password_input and len(text) < 1000:
                            raise WebReadError("The page requires login. Open it in your browser to access its content.")
                    else:
                        text = text.strip()
                    if not text:
                        raise WebReadError("The page has no readable text. Open it in your browser for a page that requires login or JavaScript.")
                    return {"url": str(response.url), "content": text[:max_chars], "truncated": len(text) > max_chars}
                finally:
                    response.close()
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, LookupError):
            raise WebReadError("The web page could not be reached. Check the address or internet connection.") from None

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            self.client.close()
