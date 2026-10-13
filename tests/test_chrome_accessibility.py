import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jev_linux.desktop import DesktopBackend, DesktopError
from gi.repository import Atspi
from tests.test_desktop import FakeText, backend_with_control
from tests.test_browser import FakeDesktopApp


class States:
    def __init__(self, *states):
        self.states = set(states)

    def contains(self, state):
        return state in self.states


class Node:
    def __init__(self, name="", *, role="panel", children=(), editable=False, focused=False,
                 active=False, path="/accessible/node", bus=":1.65"):
        self.name = name
        self.role = role
        self.children = list(children)
        self.path = path
        self.app = SimpleNamespace(bus_name=bus)
        states = [Atspi.StateType.SHOWING, Atspi.StateType.VISIBLE, Atspi.StateType.SENSITIVE]
        if editable:
            states.append(Atspi.StateType.EDITABLE)
        if focused:
            states.append(Atspi.StateType.FOCUSED)
        if active:
            states.append(Atspi.StateType.ACTIVE)
        self.states = States(*states)
        self.cache_masks = []
        self.probes = []
        self.pending_children = None

    def get_name(self):
        return self.name

    def get_role_name(self):
        return self.role

    def get_role(self):
        return Atspi.Role.ENTRY if self.role == "entry" else Atspi.Role.PANEL

    def get_state_set(self):
        return self.states

    def get_interfaces(self):
        return []

    def get_attributes(self):
        self.probes.append("attributes")
        return {}

    def get_relation_set(self):
        self.probes.append("relations")
        if self.pending_children is not None:
            self.children = self.pending_children
            self.pending_children = None
        return []

    def get_application(self):
        return getattr(self, "owner", self)

    def get_document_iface(self):
        uri = getattr(self, "uri", "")
        return SimpleNamespace(get_document_attribute_value=lambda attribute: uri if attribute == "URI" else "")

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def set_cache_mask(self, mask):
        self.cache_masks.append(mask)

    def clear_cache(self):
        pass


class CachedStateNode(Node):
    """Expose a delayed AT-SPI cache refresh without processing GTK events."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_states = None
        self.pending_name = None
        self.pending_role = None
        self.pending_attributes = None
        self.attributes = {}
        self.clear_calls = 0

    def set_live_state(self, state, present):
        updated = set((self.pending_states or self.states).states)
        if present:
            updated.add(state)
        else:
            updated.discard(state)
        self.pending_states = States(*updated)

    def set_live_metadata(self, *, name=None, role=None, attributes=None):
        self.pending_name = name
        self.pending_role = role
        self.pending_attributes = attributes

    def get_attributes(self):
        self.probes.append("attributes")
        return self.attributes

    def clear_cache(self):
        self.clear_calls += 1
        if self.pending_states is not None:
            self.states = self.pending_states
            self.pending_states = None
        if self.pending_name is not None:
            self.name = self.pending_name
            self.pending_name = None
        if self.pending_role is not None:
            self.role = self.pending_role
            self.pending_role = None
        if self.pending_attributes is not None:
            self.attributes = self.pending_attributes
            self.pending_attributes = None


class App(Node):
    def __init__(self, name, pid, windows):
        super().__init__(name, role="application", children=windows)
        self.pid = pid
        self.assign_app(self)

    def assign_app(self, node):
        node.owner = self
        for child in node.children:
            if child is not None:
                self.assign_app(child)

    def get_process_id(self):
        return self.pid


def make_backend():
    backend = DesktopBackend.__new__(DesktopBackend)
    backend._lock = threading.RLock()
    backend._own_pid = 900
    backend._refs = {}
    backend._apps = {}
    backend._generation = 0
    backend._atspi_error = ""
    backend._last_snapshot = {}
    return backend


class ChromeAccessibilityTests(unittest.TestCase):
    def setUp(self):
        self.backend = make_backend()

    def snapshot(self, apps, **kwargs):
        desktop = Node(children=apps)
        with patch("jev_linux.desktop.Atspi.get_desktop", return_value=desktop), \
             patch.object(self.backend, "_focus_probe", side_effect=lambda window: getattr(window, "focus_nodes", [])):
            return self.backend.snapshot(**kwargs)

    def test_reading_uncached_relations_enables_existing_browser_controls(self):
        search = Node("Search", role="combo box", editable=True, path="/accessible/search")
        window = Node(role="frame", path="/accessible/window")
        window.pending_children = [search]
        chrome = App("Google Chrome", 300, [window])
        snapshot = self.snapshot([chrome], preferred_app="Google Chrome")
        self.assertEqual([element["name"] for element in snapshot["elements"]], ["Search"])
        self.assertEqual(chrome.pid, 300)
        self.assertEqual(chrome.cache_masks, [Atspi.Cache.NONE, Atspi.Cache.ALL])
        self.assertEqual(window.probes[:2], ["relations", "attributes"])

    def test_cache_is_restored_when_a_browser_query_fails(self):
        window = Node(role="frame")
        window.get_relation_set = lambda: (_ for _ in ()).throw(RuntimeError("App unresponsive"))
        DesktopBackend._enable_browser_accessibility(window)
        self.assertEqual(window.cache_masks, [Atspi.Cache.NONE, Atspi.Cache.ALL])

    def test_requested_browser_controls_survive_another_apps_large_tree(self):
        large = Node(role="frame", active=True, children=[
            Node(f"Field {index}", role="entry", editable=True) for index in range(240)])
        other = App("Other editor", 100, [large])
        search = Node("Search", role="combo box", editable=True, path="/accessible/search")
        chrome = App("Google Chrome", 300, [Node(role="frame", children=[search], path="/accessible/browser")])
        snapshot = self.snapshot([other, chrome], preferred_app="Google Chrome")
        self.assertEqual([element["app"] for element in snapshot["elements"]], ["Google Chrome"])
        self.assertEqual(snapshot["elements"][0]["window_id"], ":1.65/accessible/browser")
        self.assertEqual(snapshot["elements"][0]["pid"], 300)
        self.assertFalse(snapshot["scope_missing"])

    def test_scoping_does_not_hide_jev_own_foreground_focus(self):
        jev_control = Node("Start listening", role="button", focused=True)
        jev_window = Node(role="frame", active=True, children=[jev_control])
        jev_window.focus_nodes = [jev_control]
        jev = App("Jev Linux", 900, [jev_window])
        chrome = App("Google Chrome", 300, [Node(role="frame", children=[
            Node(f"Search {index}", role="entry", editable=True) for index in range(230)])])
        snapshot = self.snapshot([chrome, jev], preferred_app="Google Chrome")
        self.assertTrue(snapshot["self_focused"])
        self.assertEqual(len(snapshot["elements"]), 220)

    def test_current_foreground_other_app_remains_verified_when_scoped(self):
        foreground = Node("Document", role="entry", editable=True, focused=True)
        window = Node(role="frame", active=True, children=[foreground])
        window.focus_nodes = [foreground]
        editor = App("Text Editor", 100, [window])
        chrome = App("Google Chrome", 300, [Node(role="frame", children=[
            Node("Search", role="entry", editable=True)])])
        snapshot = self.snapshot([chrome, editor], preferred_app="Google Chrome")
        self.assertEqual(snapshot["focused_app"], "Text Editor")
        focused = next(element for element in snapshot["elements"] if element["id"] == snapshot["focused_element_id"])
        self.assertEqual(focused["app"], "Text Editor")

    def test_background_non_browser_window_refreshes_stale_active_state(self):
        code_field = Node("Editor", role="entry", editable=True, focused=True)
        code_window = CachedStateNode(role="frame", active=True, children=[code_field],
                                      path="/accessible/code-window", bus=":1.25")
        code_window.focus_nodes = [code_field]
        code_window.set_live_state(Atspi.StateType.ACTIVE, False)
        search = Node("Search", role="entry", editable=True, focused=True,
                      path="/accessible/search")
        chrome_window = Node(role="frame", active=True, children=[search],
                             path="/accessible/browser")
        chrome_window.focus_nodes = [search]

        snapshot = self.snapshot([App("Code", 100, [code_window]),
                                  App("Google Chrome", 300, [chrome_window])])

        self.assertFalse(snapshot.get("focus_ambiguous", False))
        self.assertEqual(snapshot["focused_app"], "Google Chrome")
        self.assertIs(self.backend._refs[snapshot["focused_element_id"]].accessible, search)
        code_record = next(record for record in snapshot["windows"] if record["app"] == "Code")
        self.assertFalse(code_record["active"])
        self.assertGreaterEqual(code_window.clear_calls, 1)

    def test_non_browser_foreground_window_refreshes_new_active_state(self):
        field = Node("Document", role="entry", editable=True, focused=True)
        editor_window = CachedStateNode(role="frame", children=[field],
                                        path="/accessible/editor")
        editor_window.focus_nodes = [field]
        editor_window.set_live_state(Atspi.StateType.ACTIVE, True)

        snapshot = self.snapshot([App("Text Editor", 100, [editor_window])])

        self.assertEqual(snapshot["focused_app"], "Text Editor")
        self.assertIs(self.backend._refs[snapshot["focused_element_id"]].accessible, field)
        self.assertTrue(snapshot["windows"][0]["active"])
        self.assertGreaterEqual(editor_window.clear_calls, 1)

    def test_global_focus_probe_refreshes_new_focused_control_state(self):
        search = CachedStateNode("Search", role="entry", editable=True,
                                 path="/accessible/search")
        search.set_live_state(Atspi.StateType.FOCUSED, True)
        chrome_window = Node(role="frame", active=True, children=[search],
                             path="/accessible/browser")
        chrome_window.focus_nodes = [search]

        snapshot = self.snapshot([App("Google Chrome", 300, [chrome_window])])

        self.assertEqual(snapshot["focused_app"], "Google Chrome")
        self.assertIs(self.backend._refs[snapshot["focused_element_id"]].accessible, search)
        self.assertGreaterEqual(search.clear_calls, 1)

    def test_unfocused_editable_control_refreshes_name_and_role_before_snapshot(self):
        search = CachedStateNode("Loading field", role="text", editable=True,
                                 path="/accessible/search")
        search.set_live_metadata(name="Search", role="combo box",
                                 attributes={"input-type": "search"})
        chrome_window = Node(role="frame", children=[search], path="/accessible/browser")

        snapshot = self.snapshot([App("Google Chrome", 300, [chrome_window])],
                                 preferred_app="Google Chrome")

        self.assertEqual([(record["name"], record["role"]) for record in snapshot["elements"]],
                         [("Search", "combo box")])
        self.assertIsNone(snapshot["focused_element_id"])
        self.assertGreaterEqual(search.clear_calls, 1)

    def test_unfocused_editable_control_refreshes_password_attributes_before_snapshot(self):
        field = CachedStateNode("Input", role="entry", editable=True,
                                path="/accessible/input")
        field.set_live_metadata(attributes={"input-type": "password"})
        chrome_window = Node(role="frame", children=[field], path="/accessible/browser")

        snapshot = self.snapshot([App("Google Chrome", 300, [chrome_window])],
                                 preferred_app="Google Chrome")

        self.assertEqual(snapshot["elements"], [])
        self.assertFalse(snapshot["password_focused"])
        self.assertGreaterEqual(field.clear_calls, 1)

    def test_browser_window_and_page_identity_disambiguate_search_from_address(self):
        address = Node("Address and search bar", role="entry", editable=True)
        search = Node("Search", role="combo box", editable=True)
        document = Node(role="document web", children=[search], path="/accessible/document")
        window = Node(role="frame", children=[address, document], path="/accessible/window")
        other_window = Node(role="frame", children=[Node("Search", role="entry", editable=True)], path="/accessible/other")
        chrome = App("Google Chrome", 300, [other_window, window])
        snapshot = self.snapshot([chrome], preferred_app="Google Chrome", preferred_window=":1.65/accessible/window")
        records = {element["name"]: element for element in snapshot["elements"]}
        self.assertEqual(len(records), 2)
        self.assertEqual(records["Search"]["document_id"], ":1.65/accessible/document")
        self.assertEqual(records["Address and search bar"]["document_id"], "")
        self.assertTrue(all(element["window_id"] == ":1.65/accessible/window" for element in records.values()))

    def test_focused_browser_search_keeps_its_document_identity(self):
        search = Node("Search", role="combo box", editable=True, focused=True)
        document = Node(role="document web", children=[search], path="/accessible/document")
        window = Node(role="frame", active=True, children=[document], path="/accessible/window")
        window.focus_nodes = [search]
        snapshot = self.snapshot([App("Google Chrome", 300, [window])], preferred_app="Google Chrome")
        self.assertEqual(len(snapshot["elements"]), 1)
        self.assertEqual(snapshot["elements"][0]["doc_id"], ":1.65/accessible/document")

    def test_installed_desktop_id_scopes_to_native_application_name(self):
        self.backend._apps["google-chrome.desktop"] = SimpleNamespace(
            get_display_name=lambda: "Google Chrome", get_name=lambda: "Google Chrome")
        chrome = App("Google Chrome", 300, [Node(role="frame", children=[Node("Search", editable=True)])])
        snapshot = self.snapshot([chrome], preferred_app="google-chrome.desktop")
        self.assertFalse(snapshot["scope_missing"])
        self.assertEqual(snapshot["elements"][0]["app"], "Google Chrome")

    def test_native_application_id_and_executable_match_installed_gnome_apps(self):
        cases = [("org.gnome.TextEditor.desktop", "Text Editor", "/usr/bin/gnome-text-editor", "org.gnome.TextEditor"),
                 ("org.gnome.Calculator.desktop", "Calculator", "/usr/bin/gnome-calculator", "gnome-calculator")]
        for app_id, display, executable, native in cases:
            with self.subTest(native=native):
                self.backend._apps = {app_id: FakeDesktopApp(app_id, display, executable, "Utility;", supports_uris=False)}
                window = Node(role="frame", children=[Node("Field", editable=True)])
                snapshot = self.snapshot([App(native, 300, [window])], preferred_app=app_id)
                self.assertFalse(snapshot["scope_missing"])
                self.assertEqual(snapshot["elements"][0]["app"], native)

    def test_firefox_desktop_display_name_matches_native_browser(self):
        info = FakeDesktopApp("firefox.desktop", "Firefox Web Browser", "/usr/bin/firefox", "Network;WebBrowser;")
        self.backend._apps = {info.app_id: info}
        firefox = App("Firefox", 300, [Node(role="frame", children=[Node("Search", editable=True)])])
        for requested in ("firefox.desktop", "Firefox Web Browser"):
            with self.subTest(requested=requested):
                snapshot = self.snapshot([firefox], preferred_app=requested)
                self.assertFalse(snapshot["scope_missing"])

    def test_native_app_names_must_match_whole_alias(self):
        info = FakeDesktopApp("firefox.desktop", "Firefox Web Browser", "/usr/bin/firefox", "Network;WebBrowser;")
        self.backend._apps = {info.app_id: info}
        other = App("Firefox password manager", 301, [Node(role="frame", children=[Node("Field", editable=True)])])
        snapshot = self.snapshot([other], preferred_app=info.app_id)
        self.assertTrue(snapshot["scope_missing"])
        self.assertEqual(snapshot["elements"], [])

    def test_chrome_pwa_executable_does_not_claim_the_whole_browser(self):
        pwa = FakeDesktopApp("chrome-pwa.desktop", "A Web App", "/opt/google/chrome/google-chrome", None, supports_uris=False)
        self.backend._apps = {pwa.app_id: pwa}
        chrome = App("Google Chrome", 300, [Node(role="frame", children=[Node("Search", editable=True)])])
        snapshot = self.snapshot([chrome], preferred_app=pwa.app_id)
        self.assertTrue(snapshot["scope_missing"])
        self.assertNotIn("google-chrome", self.backend._preferred_names(pwa.app_id))
        self.assertEqual(snapshot["elements"], [])

    def test_generic_launchers_cannot_claim_unrelated_app_instances(self):
        info = FakeDesktopApp("my-tool.desktop", "My tool", "/usr/bin/python3", "Utility;", supports_uris=False)
        self.backend._apps = {info.app_id: info}
        self.assertNotIn("python3", self.backend._preferred_names(info.app_id))

    def test_context_wrapper_preserves_selected_window(self):
        with patch.object(self.backend, "snapshot", return_value={}) as snapshot:
            self.backend.snapshot_for_context({"app_id": "google-chrome.desktop", "window_id": ":1.65/accessible/window"})
        snapshot.assert_called_once_with(preferred_app="google-chrome.desktop", preferred_window=":1.65/accessible/window", preferred_url=None)

    def test_context_selects_url_until_an_owned_window_is_bound(self):
        with patch.object(self.backend, "snapshot", return_value={}) as snapshot:
            self.backend.snapshot_for_context({"app_id": "google-chrome.desktop", "url": "https://youtube.com"})
            self.backend.snapshot_for_context({"app_id": "google-chrome.desktop", "url": "https://youtube.com", "window_id": ":1.65/accessible/window"})
        self.assertEqual(snapshot.call_args_list[0].kwargs["preferred_url"], "https://youtube.com")
        self.assertIsNone(snapshot.call_args_list[1].kwargs["preferred_url"])

    def test_uri_ownership_prefers_youtube_over_active_browser_signin_popup(self):
        signin = Node(role="document web", children=[Node("Email", editable=True, focused=True)], path="/accessible/signin-doc")
        signin.uri = "https://accounts.google.com/signin"
        popup = Node(role="frame", active=True, children=[signin], path="/accessible/popup")
        popup.focus_nodes = [signin.children[0]]
        search = Node("Search", role="combo box", editable=True)
        youtube = Node(role="document web", children=[search], path="/accessible/youtube-doc")
        youtube.uri = "https://www.youtube.com/"
        main = Node(role="frame", children=[youtube], path="/accessible/main")
        snapshot = self.snapshot([App("Google Chrome", 300, [popup, main])], preferred_app="Google Chrome", preferred_url="https://youtube.com")
        self.assertTrue(snapshot["requested_url_visible"])
        self.assertEqual([element["name"] for element in snapshot["elements"]], ["Search"])
        self.assertEqual(snapshot["elements"][0]["window_id"], ":1.65/accessible/main")
        self.assertEqual(snapshot["elements"][0]["doc_id"], ":1.65/accessible/youtube-doc")
        # Global focus still refers to the popup; it must never be assumed to
        # match the chosen page merely because both belong to Chrome.
        self.assertEqual(snapshot["focused_app"], "Google Chrome")
        self.assertNotIn(snapshot["focused_element_id"], [element["id"] for element in snapshot["elements"]])

    def test_missing_requested_url_offers_no_other_browser_inputs(self):
        doc = Node(role="document web", children=[Node("Email", editable=True, focused=True)])
        doc.uri = "https://accounts.google.com/signin"
        window = Node(role="frame", active=True, children=[doc])
        window.focus_nodes = [doc.children[0]]
        snapshot = self.snapshot([App("Google Chrome", 300, [window])], preferred_app="Google Chrome", preferred_url="https://youtube.com")
        self.assertTrue(snapshot["scope_missing"])
        self.assertFalse(snapshot["requested_url_visible"])
        self.assertEqual(snapshot["elements"], [])

    def test_matched_page_controls_receive_budget_before_large_browser_toolbar(self):
        doc = Node(role="document web", children=[Node("Search", editable=True)], path="/accessible/doc")
        doc.uri = "https://youtube.com/"
        window = Node(role="frame", children=[Node(f"Tab {index}", editable=True) for index in range(240)] + [doc])
        snapshot = self.snapshot([App("Google Chrome", 300, [window])], preferred_app="Google Chrome", preferred_url="https://youtube.com")
        self.assertTrue(snapshot["requested_url_visible"])
        self.assertEqual(snapshot["elements"][0]["name"], "Search")
        self.assertTrue(snapshot["truncated"])

    def test_url_ownership_handles_www_and_https_upgrade_but_rejects_other_origins(self):
        for actual, expected in [("https://www.youtube.com/", "https://youtube.com"),
                                 ("https://youtube.com/", "http://youtube.com")]:
            self.assertTrue(self.backend._url_matches(actual, expected))
        for actual in ("https://youtube.com.evil.example/", "https://accounts.google.com/", "https://youtube.com:8443/", "http://youtube.com/", "https://youtube.com/results?search_query=Hello"):
            self.assertFalse(self.backend._url_matches(actual, "https://youtube.com/"))

    def test_native_launch_context_retains_the_triggering_user_event(self):
        context = Mock()
        display = Mock()
        display.get_app_launch_context.return_value = context
        with patch("jev_linux.desktop.Gdk.Display.get_default", return_value=display):
            self.backend.capture_launch_context(123456)
        context.set_timestamp.assert_called_once_with(123456)
        self.assertIs(self.backend._launch_context(), context)

    def test_programmatic_callback_does_not_replace_a_real_user_event_context(self):
        context = Mock()
        display = Mock()
        display.get_app_launch_context.return_value = context
        with patch("jev_linux.desktop.Gdk.Display.get_default", return_value=display):
            self.backend.capture_launch_context(123456)
            self.backend.capture_launch_context(0)
        self.assertIs(self.backend._launch_context(), context)
        display.get_app_launch_context.assert_called_once()
        self.assertEqual(self.backend._native_launch_timestamp, 123456)

    def test_headless_launch_uses_gio_context(self):
        with patch("jev_linux.desktop.Gdk.Display.get_default", return_value=None):
            self.backend.capture_launch_context(123456)
        context = Mock()
        with patch("jev_linux.desktop.Gio.AppLaunchContext", return_value=context):
            self.assertIs(self.backend._launch_context(), context)

    def test_missing_requested_browser_does_not_offer_other_app_fields(self):
        editor = App("Text Editor", 100, [Node(role="frame", children=[Node("Document", editable=True)])])
        snapshot = self.snapshot([editor], preferred_app="Google Chrome")
        self.assertTrue(snapshot["scope_missing"])
        self.assertEqual(snapshot["elements"], [])

    def permission_backend(self, *, native=False, keyboard=False, pointer=False):
        control = FakeText(editable=native)
        backend = backend_with_control(control)
        backend._portal.status = lambda: {"granted": keyboard or pointer, "keyboard": keyboard, "pointer": pointer}
        return backend, control

    def test_native_text_insertion_needs_no_keyboard_permission_or_focus_change(self):
        backend, control = self.permission_backend(native=True)
        self.assertIsNone(backend.needs_desktop_permission("type_text", {"target": "e1", "text": "Hello"}))
        self.assertEqual(control.insertions, [])
        self.assertFalse(control.selection_removed)
        self.assertEqual(backend._portal.typed, [])

    def test_chromium_text_without_editable_interface_requires_keyboard_not_pointer(self):
        backend, control = self.permission_backend(pointer=True)
        self.assertEqual(backend.needs_desktop_permission("type_text", {"target": "e1"}), "keyboard")
        self.assertEqual(control.insertions, [])
        self.assertFalse(control.selection_removed)

    def test_granted_keyboard_does_not_require_another_typing_permission(self):
        backend, _ = self.permission_backend(keyboard=True)
        self.assertIsNone(backend.needs_desktop_permission("write_text", {"target": "e1"}))

    def test_stale_or_password_target_is_rejected_before_permission_request(self):
        backend, control = self.permission_backend()
        with self.assertRaisesRegex(DesktopError, "expired"):
            backend.needs_desktop_permission("type_text", {"target": "old"})
        control.get_attributes = lambda: {"input-type": "password"}
        with self.assertRaisesRegex(DesktopError, "Password"):
            backend.needs_desktop_permission("type_text", {"target": "e1"})
        self.assertEqual(backend._portal.typed, [])

    def test_scroll_and_shortcut_check_the_device_actually_required(self):
        backend, _ = self.permission_backend(keyboard=True)
        self.assertEqual(backend.needs_desktop_permission("scroll", {}), "pointer")
        self.assertIsNone(backend.needs_desktop_permission("press_key", {}))
        backend._portal.status = lambda: {"keyboard": False, "pointer": True}
        self.assertEqual(backend.needs_desktop_permission("press_key", {}), "keyboard")
        self.assertIsNone(backend.needs_desktop_permission("scroll", {}))
        self.assertIsNone(backend.needs_desktop_permission("open_url", {}))


if __name__ == "__main__":
    unittest.main()
