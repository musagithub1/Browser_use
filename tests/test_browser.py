import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jev_linux.desktop import DesktopBackend, DesktopError


class FakeDesktopApp:
    def __init__(self, app_id="google-chrome.desktop", name="Google Chrome",
                 executable="/usr/bin/google-chrome-stable", categories="Network;WebBrowser;",
                 supports_uris=True, visible=True):
        self.app_id = app_id
        self.name = name
        self.executable = executable
        self.categories = categories
        self.uri_support = supports_uris
        self.visible = visible
        self.launch_uris = Mock(return_value=True)
        self.launch = Mock(return_value=True)

    def get_id(self):
        return self.app_id

    def get_display_name(self):
        return self.name

    def get_name(self):
        return self.name

    def get_executable(self):
        return self.executable

    def should_show(self):
        return self.visible

    def get_categories(self):
        return self.categories

    def get_keywords(self):
        return []

    def supports_uris(self):
        return self.uri_support


class BrowserLaunchTests(unittest.TestCase):
    def setUp(self):
        self.chrome = FakeDesktopApp()
        self.default_launch = Mock(return_value=True)
        self.installed = Mock(return_value=[self.chrome])
        self.context = Mock()
        self.gio = SimpleNamespace(
            AppInfo=SimpleNamespace(get_all=self.installed,
                                    launch_default_for_uri=self.default_launch),
            AppLaunchContext=Mock(return_value=self.context),
        )
        patcher = patch("jev_linux.desktop.Gio", self.gio)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = DesktopBackend.__new__(DesktopBackend)
        self.backend._lock = threading.RLock()
        self.backend._apps = {}

    def test_named_chrome_receives_uri_without_default_fallback(self):
        result = self.backend.open_url("https://www.youtube.com/", "google-chrome.desktop")
        self.assertEqual(result, "Opened https://www.youtube.com/ in Google Chrome.")
        self.chrome.launch_uris.assert_called_once_with(["https://www.youtube.com/"], self.context)
        self.chrome.launch.assert_not_called()
        self.default_launch.assert_not_called()

    def test_unspecified_browser_preserves_default_uri_launch(self):
        result = self.backend.open_url("https://www.youtube.com/")
        self.assertEqual(result, "Opened https://www.youtube.com/.")
        self.default_launch.assert_called_once_with("https://www.youtube.com/", self.context)
        self.installed.assert_not_called()
        self.chrome.launch_uris.assert_not_called()

    def test_native_browser_launch_removes_snap_editor_schema_paths(self):
        environment = {"GSETTINGS_SCHEMA_DIR": "/home/testuser/snap/code/267/.local/share/glib-2.0/schemas",
                       "XDG_DATA_DIRS": "/snap/code/267/usr/share:/usr/share:/var/lib/snapd/desktop"}
        with patch.dict("os.environ", environment, clear=True):
            self.backend.open_url("https://youtube.com", "google-chrome.desktop")
        self.context.unsetenv.assert_called_once_with("GSETTINGS_SCHEMA_DIR")
        self.context.setenv.assert_called_once_with("XDG_DATA_DIRS", "/usr/share:/var/lib/snapd/desktop")

    def test_native_launch_keeps_user_runtime_paths_outside_snap(self):
        with patch.dict("os.environ", {"GSETTINGS_SCHEMA_DIR": "/opt/user/schemas", "XDG_DATA_DIRS": "/opt/user/share:/usr/share"}, clear=True):
            self.backend.open_url("https://youtube.com", "google-chrome.desktop")
        self.context.unsetenv.assert_not_called()
        self.context.setenv.assert_not_called()

    def test_missing_requested_browser_never_uses_default(self):
        with self.assertRaisesRegex(DesktopError, "not installed"):
            self.backend.open_url("https://www.youtube.com/", "firefox.desktop")
        self.default_launch.assert_not_called()
        self.chrome.launch_uris.assert_not_called()

    def test_refresh_does_not_launch_uninstalled_cached_browser(self):
        self.backend._apps[self.chrome.app_id] = self.chrome
        self.installed.return_value = []
        with self.assertRaisesRegex(DesktopError, "not installed"):
            self.backend.open_url("https://www.youtube.com/", self.chrome.app_id)
        self.installed.assert_called_once()
        self.chrome.launch_uris.assert_not_called()
        self.default_launch.assert_not_called()

    def test_named_browser_requires_exact_installed_desktop_id(self):
        for browser in ("Chrome", "", 42, "/usr/bin/google-chrome"):
            with self.subTest(browser=browser), self.assertRaisesRegex(DesktopError, "browser ID"):
                self.backend.open_url("https://www.youtube.com/", browser)
        self.default_launch.assert_not_called()
        self.chrome.launch_uris.assert_not_called()

    def test_uri_capable_nonbrowser_is_rejected(self):
        editor = FakeDesktopApp("editor.desktop", "Editor", "/usr/bin/editor", "Utility;TextEditor;")
        self.installed.return_value = [editor]
        with self.assertRaisesRegex(DesktopError, "not a browser"):
            self.backend.open_url("https://www.youtube.com/", editor.app_id)
        editor.launch_uris.assert_not_called()
        self.default_launch.assert_not_called()

    def test_browser_without_uri_support_and_chrome_pwa_are_not_browsers(self):
        category_only = FakeDesktopApp(supports_uris=False)
        pwa = FakeDesktopApp("chrome-pwa.desktop", "A Web App", "/opt/google/chrome/google-chrome",
                             None, supports_uris=False)
        self.installed.return_value = [category_only, pwa]
        records = self.backend.apps()
        self.assertEqual([record["browser"] for record in records], [False, False])
        for app in (category_only, pwa):
            with self.subTest(app=app.app_id), self.assertRaisesRegex(DesktopError, "not a browser"):
                self.backend.open_url("https://www.youtube.com/", app.app_id)
            app.launch_uris.assert_not_called()
        self.default_launch.assert_not_called()

    def test_category_and_known_executable_or_exact_name_share_browser_metadata(self):
        category = FakeDesktopApp("new.desktop", "New Browser", "/opt/new-browser", "Network;WebBrowser;")
        executable = FakeDesktopApp("firefox.desktop", "Web Surf", "/snap/bin/firefox", None)
        name = FakeDesktopApp("wrapped.desktop", "Google Chrome", "/usr/bin/browser-wrapper", None)
        unrelated = FakeDesktopApp("manager.desktop", "Firefox Password Manager", "/usr/bin/manager", None)
        self.installed.return_value = [category, executable, name, unrelated]
        records = {record["id"]: record for record in self.backend.apps()}
        for app in (category, executable, name):
            with self.subTest(app=app.app_id):
                self.assertTrue(records[app.app_id]["browser"])
                self.assertIn("web browser", records[app.app_id]["aliases"])
                self.backend.open_url("https://example.com/", app.app_id)
                app.launch_uris.assert_called_once()
        self.assertFalse(records[unrelated.app_id]["browser"])
        self.assertNotIn("web browser", records[unrelated.app_id]["aliases"])
        self.default_launch.assert_not_called()

    def test_browser_listing_excludes_hidden_and_unlaunchable_entries(self):
        hidden = FakeDesktopApp("hidden.desktop", visible=False)
        wrong_type = FakeDesktopApp("some-service", name="Service")
        no_executable = FakeDesktopApp("broken.desktop", executable="")
        self.installed.return_value = [hidden, wrong_type, no_executable, self.chrome]
        self.assertEqual([record["id"] for record in self.backend.apps()], [self.chrome.app_id])
        for app in (hidden, wrong_type, no_executable):
            with self.subTest(app=app.app_id), self.assertRaises(DesktopError):
                self.backend.open_url("https://example.com/", app.app_id)
            app.launch_uris.assert_not_called()
        self.default_launch.assert_not_called()

    def test_explicit_launch_false_or_exception_reports_failure_without_fallback(self):
        for failure in (False, RuntimeError("private launch detail")):
            with self.subTest(failure=type(failure).__name__):
                self.chrome.launch_uris.reset_mock(side_effect=True)
                self.chrome.launch_uris.return_value = failure
                if isinstance(failure, Exception):
                    self.chrome.launch_uris.side_effect = failure
                with self.assertRaisesRegex(DesktopError, "could not open the URL in Google Chrome") as caught:
                    self.backend.open_url("https://www.youtube.com/", self.chrome.app_id)
                self.assertNotIn("private launch detail", str(caught.exception))
                self.chrome.launch_uris.assert_called_once()
        self.default_launch.assert_not_called()

    def test_default_launch_false_reports_failure(self):
        self.default_launch.return_value = False
        with self.assertRaisesRegex(DesktopError, "default browser"):
            self.backend.open_url("https://example.com/")

    def test_invalid_urls_are_rejected_before_any_browser_launch(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.com/",
                    "https://@example.com/", "https://example.com/\n", "https://", None):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.backend.open_url(url, self.chrome.app_id)
        self.installed.assert_not_called()
        self.chrome.launch_uris.assert_not_called()
        self.default_launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
