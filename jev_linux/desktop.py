"""Native Ubuntu application discovery and conservative accessibility actions."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from urllib.parse import urlsplit

import gi

gi.require_version("Atspi", "2.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Atspi, Gdk, Gio

from .portal import RemoteDesktopPortal
from .environment import native_environment


class DesktopError(RuntimeError):
    """An operation whose target cannot currently be verified."""


class StaleControlError(DesktopError):
    """A selected snapshot control changed before validation could accept it."""


@dataclass
class _Element:
    accessible: object
    name: str
    role: str
    app: str
    window: object
    interfaces: tuple
    pid: int = 0
    window_id: str = ""


class DesktopBackend:
    """Keep native objects on one owner thread (GTK's thread in the GUI).

    IDs refer to the latest snapshot and are checked again before each action.
    AT-SPI does not expose every application. Missing controls are reported,
    rather than guessed by injecting keys into an unverified window.
    """

    def __init__(self, *, portal=None, own_pid: int | None = None):
        self._lock = threading.RLock()
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._refs: dict[str, _Element] = {}
        self._apps = {}
        self._generation = 0
        self._atspi_error = ""
        self._last_snapshot = {}
        self._native_launch_context = None
        self._native_launch_timestamp = 0
        self.capture_launch_context()
        self._portal = portal if portal is not None else RemoteDesktopPortal()
        try:
            result = Atspi.init()
            if result not in (0, 1):
                self._atspi_error = f"AT-SPI initialization returned {result}."
            # Bound failures from hung apps. A child count of -1 means an
            # inaccessible application; it does not terminate the scan.
            Atspi.set_timeout(150, 400)
        except Exception as exc:
            self._atspi_error = str(exc)

    def apps(self) -> list[dict]:
        with self._lock:
            records = []
            self._apps.clear()
            for app in Gio.AppInfo.get_all():
                try:
                    app_id = app.get_id()
                    if not app_id or not app_id.endswith(".desktop") or not app.should_show():
                        continue
                    # Gio discovers DesktopAppInfo subclasses and respects
                    # OnlyShowIn/NoDisplay/TryExec without parsing Exec as shell.
                    if not app.get_executable():
                        continue
                    name = app.get_display_name() or app.get_name()
                    if "jev linux" in name.casefold():
                        continue
                    aliases = [name.casefold(), app.get_name().casefold(),
                               app_id.removesuffix(".desktop").casefold()]
                    executable = os.path.basename(app.get_executable())
                    if executable:
                        aliases.append(executable.casefold())
                    searchable = " ".join(aliases)
                    if any(word in searchable for word in ("notes", "texteditor", "gedit", "text editor")):
                        aliases += ["notes", "notes app", "notepad", "text editor"]
                    browser = self._is_browser(app)
                    if browser:
                        aliases += ["browser", "web browser"]
                    try:
                        aliases.extend(word.casefold() for word in (app.get_keywords() or [])[:12])
                    except (AttributeError, TypeError):
                        pass
                    self._apps[app_id] = app
                    records.append({"id": app_id, "name": name,
                                    "aliases": list(dict.fromkeys(aliases)),
                                    "browser": browser})
                except Exception:
                    continue
            return sorted(records, key=lambda record: record["name"].casefold())

    @staticmethod
    def _is_browser(app) -> bool:
        """Recognize URI-capable browsers without treating Chrome PWAs as browsers."""
        try:
            if not app.supports_uris():
                return False
            try:
                categories = app.get_categories() or ""
            except (AttributeError, TypeError):
                categories = ""
            if "webbrowser" in {category.casefold() for category in categories.split(";")}:
                return True
            executable = os.path.basename(app.get_executable() or "").casefold()
            known_executables = {
                "google-chrome", "google-chrome-stable", "google-chrome-beta",
                "google-chrome-unstable", "chromium", "chromium-browser",
                "firefox", "firefox-esr", "brave", "brave-browser",
                "brave-browser-stable", "brave-browser-beta", "brave-browser-nightly",
                "microsoft-edge", "microsoft-edge-stable", "microsoft-edge-beta",
                "microsoft-edge-dev", "opera", "opera-stable", "vivaldi",
                "vivaldi-stable", "vivaldi-snapshot", "epiphany", "gnome-web",
            }
            known_names = {
                "chrome", "google chrome", "chromium", "chromium web browser",
                "firefox", "mozilla firefox", "firefox web browser", "brave",
                "brave web browser", "microsoft edge", "opera", "vivaldi",
                "epiphany", "gnome web",
            }
            return (executable in known_executables
                    or (app.get_display_name() or "").casefold() in known_names
                    or (app.get_name() or "").casefold() in known_names)
        except Exception:
            return False

    def launch_app(self, app_id: str) -> str:
        with self._lock:
            if app_id not in self._apps:
                self.apps()
            app = self._apps.get(app_id)
            if app is None:
                raise DesktopError("Choose an installed application ID from the apps list.")
            if not app.launch([], self._launch_context()):
                raise DesktopError(f"Ubuntu could not launch {app.get_display_name()}.")
            return f"Opened {app.get_display_name()}."

    def capture_launch_context(self, timestamp: int | None = None) -> None:
        """Capture desktop activation on the GTK thread when the user acts.

        GTK callbacks should pass their event timestamp before queuing a
        command. The worker can then reuse the native launch context without
        accessing the GDK display from another thread.
        """
        try:
            with self._lock:
                if timestamp == 0 and getattr(self, "_native_launch_timestamp", 0) > 0:
                    return
            display = Gdk.Display.get_default()
            context = display.get_app_launch_context() if display else None
            if context is not None and isinstance(timestamp, int) and timestamp >= 0:
                context.set_timestamp(timestamp)
            with self._lock:
                self._native_launch_context = context
                self._native_launch_timestamp = timestamp or 0
        except Exception:
            # Headless CLI planning still works without a GDK display.
            pass

    def _launch_context(self):
        context = getattr(self, "_native_launch_context", None) or Gio.AppLaunchContext()
        cleaned = native_environment(os.environ)
        for key in set(os.environ) - set(cleaned):
            context.unsetenv(key)
        for key in cleaned:
            if os.environ.get(key) != cleaned[key]:
                context.setenv(key, cleaned[key])
        return context

    def _excluded_app(self, app) -> bool:
        try:
            return (app.get_process_id() == self._own_pid
                    or "jev linux" in (app.get_name() or "").casefold())
        except Exception:
            return True

    @staticmethod
    def _states(accessible):
        states = accessible.get_state_set()
        if states is None or states.contains(Atspi.StateType.DEFUNCT):
            raise DesktopError("The accessibility control has disappeared.")
        return states

    @staticmethod
    def _is_password(accessible, role: str = "") -> bool:
        try:
            if accessible.get_role() == Atspi.Role.PASSWORD_TEXT or "password" in role.casefold():
                return True
            attributes = accessible.get_attributes() or {}
            return any(str(attributes.get(key, "")).casefold() == "password"
                       for key in ("type", "input-type", "text-input-type"))
        except Exception:
            return True

    @staticmethod
    def _action_names(accessible, interfaces: tuple) -> list[str]:
        if "Action" not in interfaces:
            return []
        action = accessible.get_action_iface()
        if action is None:
            return []
        return [action.get_action_name(index) or "activate"
                for index in range(min(max(0, action.get_n_actions()), 8))]

    @staticmethod
    def _focus_probe(window) -> list:
        """Ask the application to find focus before reading its larger tree."""
        collection = window.get_collection_iface()
        if not collection:
            return []
        rule = Atspi.MatchRule.new(
            Atspi.StateSet.new([Atspi.StateType.FOCUSED]),
            Atspi.CollectionMatchType.ALL, {}, Atspi.CollectionMatchType.ALL,
            [], Atspi.CollectionMatchType.ALL, [], Atspi.CollectionMatchType.ALL, False)
        return collection.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 8, True)

    @staticmethod
    def _window_identity(window) -> str:
        """Identify an AT-SPI window without its user-editable caption."""
        bus = getattr(getattr(window, "app", None), "bus_name", "")
        path = getattr(window, "path", "")
        if isinstance(bus, str) and bus.startswith(":") and isinstance(path, str) and path.startswith("/"):
            return bus + path
        return ""

    @staticmethod
    def _chromium_app(name: str) -> bool:
        return any(word in name.casefold() for word in
                   ("chrome", "chromium", "brave", "microsoft edge", "vivaldi", "opera"))

    @staticmethod
    def _enable_browser_accessibility(window) -> None:
        """Advertise a real accessibility client to an existing Chromium window.

        Chromium enables its AX mode when native clients read relations or
        attributes. Cached attributes can bypass that notification and leave
        only a frame exposed. These queries neither activate a screen reader
        nor restart the browser, navigate a tab, or change desktop settings.
        """
        cache_root = None
        try:
            # libatspi permits changing cache policy only on application roots.
            cache_root = window.get_application()
            if cache_root is None:
                return
            cache_root.set_cache_mask(Atspi.Cache.NONE)
            window.clear_cache()
            window.get_relation_set()
            window.get_attributes()
        except Exception:
            # An unresponsive window must not disable the rest of the desktop.
            pass
        finally:
            try:
                if cache_root is not None:
                    cache_root.set_cache_mask(Atspi.Cache.ALL)
                window.clear_cache()
            except Exception:
                pass

    @staticmethod
    def _browser_native_names(executable: str) -> set[str]:
        if executable in {"google-chrome", "google-chrome-stable", "google-chrome-beta", "google-chrome-unstable"}:
            return {"google chrome", "chrome"}
        if executable in {"chromium", "chromium-browser"}:
            return {"chromium", "chromium browser", "chromium web browser"}
        if executable in {"firefox", "firefox-esr"}:
            return {"firefox"}
        if executable in {"brave", "brave-browser", "brave-browser-stable", "brave-browser-beta", "brave-browser-nightly"}:
            return {"brave", "brave browser", "brave web browser"}
        if executable in {"microsoft-edge", "microsoft-edge-stable", "microsoft-edge-beta", "microsoft-edge-dev"}:
            return {"microsoft edge"}
        if executable in {"opera", "opera-stable"}:
            return {"opera"}
        if executable in {"vivaldi", "vivaldi-stable", "vivaldi-snapshot"}:
            return {"vivaldi"}
        return set()

    def _preferred_names(self, app: str | None) -> set[str]:
        if not isinstance(app, str) or not app:
            return set()
        names = {app.casefold()}
        if app.endswith(".desktop"):
            if app not in self._apps:
                self.apps()
            info = self._apps.get(app)
        else:
            info = next((info for info in self._apps.values()
                         if app.casefold() in {(info.get_display_name() or "").casefold(),
                                               (info.get_name() or "").casefold()}), None)
        if info is None:
            return names
        names.update(value.casefold() for value in (info.get_display_name(), info.get_name()) if value)
        try:
            app_id = info.get_id() or ""
            if app_id:
                names.add(app_id.removesuffix(".desktop").casefold())
            executable = os.path.basename(info.get_executable() or "").casefold()
            browser_names = self._browser_native_names(executable)
            browser = self._is_browser(info)
            # Browser PWAs share their browser's executable and process. That
            # fact alone cannot establish ownership of a particular PWA tab.
            generic_launchers = {"env", "sh", "bash", "python", "python3", "flatpak", "snap", "gtk-launch", "xdg-open"}
            if executable and executable not in generic_launchers and (browser or not browser_names):
                names.add(executable)
            if browser:
                names.update(browser_names)
                names.update(name.removesuffix(" web browser").removesuffix(" browser")
                             for name in tuple(names))
        except (AttributeError, TypeError):
            pass
        return names

    @staticmethod
    def default_browser_id() -> str | None:
        app = Gio.AppInfo.get_default_for_uri_scheme("https")
        return app.get_id() if app else None

    def snapshot_for_context(self, context: dict) -> dict:
        return self.snapshot(preferred_app=context.get("app_id") or context.get("app"),
                             preferred_window=context.get("window_id"),
                             preferred_url=context.get("url") if not context.get("window_id") else None)

    @staticmethod
    def _document_url(node) -> str:
        try:
            document = node.get_document_iface()
            if document is not None:
                for attribute in ("URI", "DocURL", "doc-url", "url"):
                    value = document.get_document_attribute_value(attribute)
                    if isinstance(value, str) and value:
                        return value
        except Exception:
            pass
        return ""

    @staticmethod
    def _url_matches(actual: str, expected: str) -> bool:
        """Allow normal www redirects without matching an unrelated origin."""
        try:
            actual_url, expected_url = urlsplit(actual), urlsplit(expected)
            if (actual_url.scheme.casefold() not in {"http", "https"}
                    or expected_url.scheme.casefold() not in {"http", "https"}
                    or not actual_url.hostname or not expected_url.hostname
                    or actual_url.username is not None or expected_url.username is not None):
                return False
            host = lambda url: url.hostname.casefold().rstrip(".").removeprefix("www.")
            port = lambda url: url.port or (443 if url.scheme.casefold() == "https" else 80)
            path = lambda url: (url.path or "/").rstrip("/") or "/"
            same_transport = (actual_url.scheme.casefold() == expected_url.scheme.casefold()
                              and port(actual_url) == port(expected_url))
            https_upgrade = (expected_url.scheme.casefold() == "http"
                             and actual_url.scheme.casefold() == "https"
                             and port(expected_url) == 80 and port(actual_url) == 443)
            return (host(actual_url) == host(expected_url)
                    and (same_transport or https_upgrade)
                    and path(actual_url) == path(expected_url)
                    and actual_url.query == expected_url.query)
        except (TypeError, ValueError):
            return False

    def _visible_documents(self, window) -> list:
        """Find exposed documents without reading page text or changing tabs."""
        try:
            collection = window.get_collection_iface()
            if collection is not None:
                rule = Atspi.MatchRule.new(
                    Atspi.StateSet.new([Atspi.StateType.SHOWING]),
                    Atspi.CollectionMatchType.ALL, {}, Atspi.CollectionMatchType.ALL,
                    [Atspi.Role.DOCUMENT_WEB, Atspi.Role.DOCUMENT_FRAME],
                    Atspi.CollectionMatchType.ANY, [], Atspi.CollectionMatchType.ALL, False)
                documents = collection.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 32, True)
                if documents:
                    return list(documents)
        except Exception:
            pass
        documents, stack, seen = [], [(window, 0)], set()
        deadline = time.monotonic() + 0.4
        while stack and len(seen) < 400 and time.monotonic() <= deadline:
            node, depth = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            try:
                states = self._states(node)
                if node.get_role_name() in ("document web", "document frame"):
                    if states.contains(Atspi.StateType.SHOWING):
                        documents.append(node)
                    # Skip page content; only browser-document ownership is
                    # needed here, not every child or embedded frame's text.
                    continue
                if depth < 32:
                    for index in range(min(max(0, node.get_child_count()), 250) - 1, -1, -1):
                        child = node.get_child_at_index(index)
                        if child is not None:
                            stack.append((child, depth + 1))
            except Exception:
                continue
        return documents

    def snapshot(self, preferred_app: str | None = None, preferred_window=None,
                 preferred_url: str | None = None) -> dict:
        with self._lock:
            preferred_names = self._preferred_names(preferred_app)
            if isinstance(preferred_window, dict):
                preferred_window = str(preferred_window.get("bus", "")) + str(preferred_window.get("window", ""))
            if not isinstance(preferred_window, str):
                preferred_window = None
            self._generation += 1
            self._refs.clear()
            result = {"windows": [], "elements": [], "focused_app": "",
                      "focused_element_id": None, "password_focused": False,
                      "self_focused": False, "truncated": False,
                      "focus_incomplete": False}
            if self._atspi_error:
                result["error"] = self._atspi_error
                return result
            try:
                desktop = Atspi.get_desktop(0)
                # Includes attributes: without this each input-type check
                # incurs a D-Bus round trip for every Chromium descendant.
                desktop.set_cache_mask(Atspi.Cache.ALL)
                app_count = max(0, desktop.get_child_count())
            except Exception as exc:
                result["error"] = f"Accessibility is unavailable: {exc}"
                return result
            trees = []
            for app_index in range(min(app_count, 80)):
                try:
                    app = desktop.get_child_at_index(app_index)
                    if app is None:
                        continue
                    app_name = app.get_name() or "Unnamed application"
                    pid = int(app.get_process_id())
                    excluded = self._excluded_app(app)
                    window_count = app.get_child_count()
                    for index in range(min(max(0, window_count), 20)):
                        window = app.get_child_at_index(index)
                        if window is None:
                            continue
                        if self._chromium_app(app_name):
                            self._enable_browser_accessibility(window)
                        # A single GTK dispatch may activate another app and
                        # resnapshot before queued AT-SPI cache events run.
                        # Every app's window state must therefore be fresh.
                        window.clear_cache()
                        states = self._states(window)
                        if states.contains(Atspi.StateType.ICONIFIED):
                            continue
                        showing = states.contains(Atspi.StateType.SHOWING)
                        active = states.contains(Atspi.StateType.ACTIVE)
                        window_id = self._window_identity(window)
                        if not showing and not active:
                            continue
                        preferred = ((not preferred_names or app_name.casefold() in preferred_names)
                                     and (not preferred_window or window_id == preferred_window))
                        matched_documents = []
                        if preferred_url and preferred and not excluded:
                            matched_documents = [document for document in self._visible_documents(window)
                                                 if self._url_matches(self._document_url(document), preferred_url)]
                            preferred = bool(matched_documents)
                        if not excluded:
                            result["windows"].append({"app": app_name,
                                "title": (window.get_name() or "")[:200],
                                "active": active, "showing": showing,
                                "window_id": window_id, "pid": pid,
                                "url_match": bool(matched_documents)})
                        focus_nodes = []
                        if active:
                            try:
                                focus_nodes = self._focus_probe(window)
                            except Exception:
                                result["focus_incomplete"] = True
                        trees.append((app_name, window, active, excluded, focus_nodes,
                                      pid, window_id, preferred, matched_documents))
                except Exception:
                    continue
            # Active flags are hints; Chromium may retain stale ACTIVE state.
            # Actual focused descendants determine the text/input target.
            scoped = bool(preferred_names or preferred_window or preferred_url)
            trees.sort(key=lambda tree: (scoped and not tree[7], not bool(tree[4]), not tree[2], tree[3]))
            if scoped:
                result["scope_missing"] = not any(tree[7] and not tree[3] for tree in trees)
            if preferred_url:
                result["requested_url_visible"] = not result["scope_missing"]
            deadline = time.monotonic() + 1.6
            visited = 0
            text_budget = 24000
            focused = []
            seen = set()
            recorded = set()
            records_by_node = {}

            def record_node(node, app_name, window, active, excluded, states, role, pid, window_id, document_id=""):
                nonlocal text_budget
                is_focused = active and states.contains(Atspi.StateType.FOCUSED)
                if excluded:
                    if is_focused:
                        result["self_focused"] = True
                    return
                if self._is_password(node, role):
                    if is_focused:
                        result["password_focused"] = True
                        result["focused_app"] = app_name
                    return False
                if node in recorded:
                    # A global focus probe has no document ancestry. Fill it
                    # when the scoped tree reaches the same control normally.
                    if document_id:
                        records_by_node[node]["document_id"] = document_id
                        records_by_node[node]["doc_id"] = document_id
                    return
                if not (states.contains(Atspi.StateType.SHOWING) or is_focused):
                    return
                name = node.get_name() or ""
                interfaces = tuple(node.get_interfaces() or [])
                editable = states.contains(Atspi.StateType.EDITABLE)
                actions = self._action_names(node, interfaces)
                meaningful_focus = is_focused and role not in ("window", "frame", "application")
                significant = bool(actions or editable or meaningful_focus
                                   or (name and role in ("label", "heading", "text", "document text")))
                if not significant:
                    return
                if len(result["elements"]) >= 220 and not meaningful_focus:
                    result["truncated"] = True
                    return
                element_id = f"e{self._generation}_{len(self._refs) + 1}"
                value = ""
                if "Text" in interfaces and text_budget > 0:
                    text = node.get_text_iface()
                    if text:
                        length = min(max(0, text.get_character_count()), 900, text_budget)
                        value = Atspi.Text.get_text(text, 0, length) or ""
                        text_budget -= len(value)
                record = {"id": element_id, "app": app_name,
                          "role": role, "name": name[:180], "value": value,
                          "actions": actions, "focused": meaningful_focus,
                          "editable": editable,
                          "sensitive": states.contains(Atspi.StateType.SENSITIVE),
                          "active_window": active, "window_id": window_id,
                          "pid": pid, "document_id": document_id, "doc_id": document_id}
                result["elements"].append(record)
                self._refs[element_id] = _Element(node, name, role, app_name, window,
                                                interfaces, pid, window_id)
                recorded.add(node)
                records_by_node[node] = record
                if meaningful_focus:
                    focused.append(record)

            # Preserve global focus evidence even when a requested browser's
            # tree fills the element/time budget before Jev's own window.
            for app_name, window, active, excluded, focus_nodes, pid, window_id, preferred, matched_documents in trees:
                for node in focus_nodes:
                    try:
                        node.clear_cache()
                        record_node(node, app_name, window, active, excluded,
                                    self._states(node), node.get_role_name() or "unknown", pid, window_id)
                    except Exception:
                        continue
            for app_name, window, active, excluded, focus_nodes, pid, window_id, preferred, matched_documents in trees:
                if scoped and not preferred:
                    continue
                # A page's controls must not lose their budget to dozens of
                # browser tabs, toolbars, or a different Chrome window.
                stack = [(window, 0, "")] + [(document, 0, "") for document in reversed(matched_documents)]
                while stack:
                    if visited >= 2200 or time.monotonic() > deadline:
                        result["truncated"] = True
                        break
                    node, depth, document_id = stack.pop()
                    if node in seen:
                        continue
                    seen.add(node)
                    visited += 1
                    try:
                        states = self._states(node)
                        if states.contains(Atspi.StateType.EDITABLE):
                            node.clear_cache()
                            states = self._states(node)
                        is_focused = active and states.contains(Atspi.StateType.FOCUSED)
                        if (depth > 0 and not is_focused
                                and not states.contains(Atspi.StateType.SHOWING)
                                and not states.contains(Atspi.StateType.VISIBLE)):
                            continue
                        role = node.get_role_name() or "unknown"
                        if role in ("document web", "document frame", "document text"):
                            document_id = self._window_identity(node)
                        if record_node(node, app_name, window, active, excluded,
                                       states, role, pid, window_id, document_id) is False:
                            continue
                        if depth < 32:
                            count = node.get_child_count()
                            if count > 250:
                                result["truncated"] = True
                            for index in range(min(max(0, count), 250) - 1, -1, -1):
                                child = node.get_child_at_index(index)
                                if child is not None:
                                    stack.append((child, depth + 1, document_id))
                    except Exception:
                        continue
                if result["truncated"] and (visited >= 2200 or time.monotonic() > deadline):
                    break
            focused_editable = [record for record in focused if record["editable"]]
            focused_apps = {record["app"] for record in focused}
            candidates = focused_editable if focused_editable else focused
            if len(focused_apps) == 1 and len(candidates) == 1:
                result["focused_app"] = candidates[0]["app"]
                result["focused_element_id"] = candidates[0]["id"]
            elif len(focused_apps) == 1 and candidates and not focused_editable:
                # Chromium marks an ancestor document/section as focused too.
                # It is sufficient to verify the owner for navigation keys.
                result["focused_app"] = candidates[0]["app"]
                result["focused_element_id"] = candidates[0]["id"]
            elif candidates:
                # Multiple toolkit focus records are not a safe input target.
                result["focus_ambiguous"] = True
            if preferred_url or preferred_window:
                matching_windows = {tree[6] for tree in trees if tree[7] and not tree[3]}
                result["elements"] = [element for element in result["elements"]
                                      if element["window_id"] in matching_windows]
            self._last_snapshot = result
            return result

    def _validate(self, element_id: str) -> tuple[_Element, object, tuple]:
        element = self._refs.get(element_id)
        if element is None:
            raise StaleControlError("This control ID has expired. Take a new desktop snapshot.")
        return self._validate_element(element)

    def _validate_element(self, element: _Element) -> tuple[_Element, object, tuple]:
        """Recheck the original target even after a focus snapshot changes IDs."""
        accessible = element.accessible
        try:
            accessible.clear_cache()
            states = self._states(accessible)
            role = accessible.get_role_name() or "unknown"
            if accessible.get_name() != element.name or role != element.role:
                raise StaleControlError("This control changed after the snapshot. Take a new snapshot.")
            if self._is_password(accessible, role):
                raise DesktopError("Password fields cannot be controlled.")
            app = accessible.get_application()
            if app is None or self._excluded_app(app) or app.get_name() != element.app:
                raise DesktopError("The target application has changed.")
            if element.pid and int(app.get_process_id()) != element.pid:
                raise DesktopError("The target application restarted after the snapshot.")
            if element.window_id and self._window_identity(element.window) != element.window_id:
                raise DesktopError("The target window changed after the snapshot.")
            if not states.contains(Atspi.StateType.SHOWING):
                raise DesktopError("The control is no longer visible. Take a new snapshot.")
            if not states.contains(Atspi.StateType.SENSITIVE):
                raise DesktopError("The control is currently disabled.")
            return element, states, tuple(accessible.get_interfaces() or [])
        except DesktopError:
            raise
        except Exception as exc:
            raise DesktopError(f"The control is no longer available: {exc}") from exc

    def click(self, element_id: str) -> str:
        with self._lock:
            element, states, interfaces = self._validate(element_id)
            action = element.accessible.get_action_iface() if "Action" in interfaces else None
            if action:
                names = self._action_names(element.accessible, interfaces)
                preferred = ("click", "press", "activate", "jump", "open", "toggle", "check")
                choice = next((index for wanted in preferred for index, name in enumerate(names)
                               if name.casefold() == wanted), None)
                if choice is None and len(names) == 1:
                    choice = 0
                if choice is not None and action.do_action(choice):
                    return f"Activated {element.name or element.role}."
            if states.contains(Atspi.StateType.EDITABLE) and "Component" in interfaces:
                if element.accessible.get_component_iface().grab_focus():
                    return f"Focused {element.name or element.role}."
            raise DesktopError("This control does not expose an accessibility action.")

    def _focused_target(self, snapshot: dict | None = None) -> str:
        snapshot = self.snapshot() if snapshot is None else snapshot
        if snapshot.get("password_focused"):
            raise DesktopError("Password fields cannot be controlled.")
        if snapshot.get("self_focused"):
            raise DesktopError("Jev has focus. Focus the application you want to control.")
        if snapshot.get("focus_incomplete"):
            raise DesktopError("A foreground application's focus could not be verified. "
                               "Close unresponsive windows or use a specific accessible control.")
        target = snapshot.get("focused_element_id")
        if not target or snapshot.get("focus_ambiguous"):
            raise DesktopError("No unambiguous focused application control is accessible. "
                               "Focus the target application or choose a control ID.")
        return target

    def _focus_identity(self, element_id: str) -> dict:
        element, states, _ = self._validate(element_id)
        if not states.contains(Atspi.StateType.FOCUSED):
            raise DesktopError("The control no longer has verified focus.")
        accessible = element.accessible
        try:
            app = accessible.get_application()
            # These are stable AT-SPI D-Bus identities, unlike snapshot eIDs,
            # app/window captions, and user-editable text. A unique bus name
            # also distinguishes application restarts that reuse object paths.
            bus_name = accessible.app.bus_name
            control_path = accessible.path
            window_path = element.window.path
            if (not isinstance(bus_name, str) or not bus_name.startswith(":")
                    or not isinstance(control_path, str) or not control_path.startswith("/")
                    or not isinstance(window_path, str) or not window_path.startswith("/")):
                raise ValueError("Missing stable accessibility identity.")
            return {"app": element.app, "pid": int(app.get_process_id()),
                    "bus": bus_name, "window": window_path, "control": control_path}
        except Exception as exc:
            raise DesktopError("The focused control has no stable identity; "
                               "cannot bind a keyboard action to it.") from exc

    def focus_token(self, snapshot: dict | None = None) -> dict:
        """Bind a shortcut to verified focus; keep this token only in memory.

        A supplied snapshot must be the latest one from this backend. Tokens
        survive rereading the same control and contain no document text.
        """
        with self._lock:
            target = self._focused_target(snapshot) if snapshot is not None else self._focused_target()
            return self._focus_identity(target)

    def needs_desktop_permission(self, operation: str, arguments: dict) -> str | None:
        """Check the precise portal device needed, without moving focus or editing."""
        with self._lock:
            if operation not in {"type_text", "write_text", "press_key", "scroll"}:
                return None
            if operation in {"type_text", "write_text"}:
                target = arguments.get("target")
                if not target:
                    raise DesktopError("Choose a current editable control before typing.")
                element, states, interfaces = self._validate(target)
                if not states.contains(Atspi.StateType.EDITABLE):
                    raise DesktopError("The selected control is not editable.")
                if "Text" in interfaces and "EditableText" in interfaces:
                    text = element.accessible.get_text_iface()
                    editable = element.accessible.get_editable_text_iface()
                    if text is not None and editable is not None and text.get_caret_offset() >= 0:
                        return None
            required = "pointer" if operation == "scroll" else "keyboard"
            return None if self._portal.status().get(required) else required

    def _activation_shortcut(self, element: _Element) -> str | None:
        from .gnome_activation import configured_favorite_shortcut
        self.apps()
        matches = [identifier for identifier in self._apps
                   if element.app.casefold() in self._preferred_names(identifier)]
        return configured_favorite_shortcut(matches[0]) if len(matches) == 1 else None

    @staticmethod
    def _foreground_window(element: _Element, snapshot: dict, refs: dict) -> bool:
        if any(snapshot.get(flag) for flag in ("self_focused", "password_focused", "focus_incomplete", "focus_ambiguous")):
            return False
        foreground = refs.get(snapshot.get("focused_element_id"))
        return bool(foreground and foreground.app == element.app
                    and foreground.pid == element.pid and foreground.window_id == element.window_id)

    def _ensure_portal_target_focus_steps(self, element: _Element) -> Generator[float, None, None]:
        """Activate a known background app, then focus its original field.

        A remembered Chromium field focus does not establish window activation.
        GNOME's configured favorite shortcut supplies that separate operation;
        it is used once and only while another known application has focus.
        """
        activation_deadline = None
        with self._lock:
            if element.pid and element.window_id and element.window is not None:
                snapshot = self.snapshot(preferred_app=element.app, preferred_window=element.window_id)
                if not self._foreground_window(element, snapshot, self._refs):
                    if snapshot.get("focused_app") == element.app:
                        raise DesktopError("A different window of the target app is in front. Bring the requested window forward, then continue.")
                    known_foreground = bool(snapshot.get("self_focused") or snapshot.get("focused_app"))
                    if not known_foreground or any(snapshot.get(flag) for flag in ("password_focused", "focus_incomplete", "focus_ambiguous")):
                        raise DesktopError("The foreground app cannot be verified. Bring the requested window forward, then continue.")
                    if not self._portal.status().get("keyboard"):
                        raise DesktopError("Ubuntu keyboard permission is required to activate the target app.")
                    shortcut = self._activation_shortcut(element)
                    if shortcut is None:
                        raise DesktopError("The target app has no verified activation shortcut. Bring its window forward, then continue.")
                    self._validate_element(element)
                    self._portal.press_key(shortcut)
                    activation_deadline = time.monotonic() + 3
        if activation_deadline is not None:
            while True:
                # Return to GTK so its own Wayland focus-out event can update
                # the states exposed by Jev's accessibility server. Clearing
                # client caches cannot process that server-side event.
                yield .05
                with self._lock:
                    snapshot = self.snapshot(preferred_app=element.app, preferred_window=element.window_id)
                    if self._foreground_window(element, snapshot, self._refs):
                        break
                    if snapshot.get("focused_app") == element.app or time.monotonic() >= activation_deadline:
                        raise DesktopError("The requested window did not become the verified foreground window. Bring it forward, then continue.")
        focus_deadline = None
        with self._lock:
            _, states, interfaces = self._validate_element(element)
            if not states.contains(Atspi.StateType.FOCUSED):
                component = element.accessible.get_component_iface() if "Component" in interfaces else None
                if not component or not component.grab_focus():
                    raise DesktopError("Could not focus the selected editable control.")
                focus_deadline = time.monotonic() + .5
        if focus_deadline is not None:
            while True:
                yield .025
                # Chrome's renderer can acknowledge the focus request before
                # its accessible state changes. Read the state afresh briefly.
                with self._lock:
                    _, states, _ = self._validate_element(element)
                    if states.contains(Atspi.StateType.FOCUSED):
                        break
                    if time.monotonic() >= focus_deadline:
                        raise DesktopError("The editable control did not receive focus. Bring its window forward, then continue.")

    def type_text(self, text: str, element_id: str | None = None) -> str:
        """Drive typing synchronously for callers without a GTK dispatcher."""
        steps = self.type_text_steps(text, element_id)
        try:
            while True:
                try:
                    delay = next(steps)
                except StopIteration as complete:
                    return complete.value
                time.sleep(delay)
        finally:
            steps.close()

    def verify_note_visible_steps(self, path, snapshot, notes_directory):
        """Check a saved note's full native body without changing editor state."""
        from .note_editor import verify_note_visible_steps
        steps = verify_note_visible_steps(path, snapshot, notes_directory)
        try:
            while True:
                with self._lock:
                    try:
                        delay = next(steps)
                    except StopIteration as complete:
                        return complete.value
                yield delay
        finally:
            steps.close()

    def verify_note_visible(self, path, snapshot, notes_directory):
        steps = self.verify_note_visible_steps(path, snapshot, notes_directory)
        try:
            while True:
                try:
                    delay = next(steps)
                except StopIteration as complete:
                    return complete.value
                time.sleep(delay)
        finally:
            steps.close()

    def refresh_note_steps(self, path, before, after, notes_directory):
        """Reload only an unchanged note buffer backed by a verified append."""
        from .note_editor import refresh_note_steps
        steps = refresh_note_steps(path, before, after, notes_directory)
        try:
            while True:
                with self._lock:
                    try:
                        delay = next(steps)
                    except StopIteration as complete:
                        return complete.value
                yield delay
        finally:
            steps.close()

    def refresh_note(self, path, before, after, notes_directory):
        steps = self.refresh_note_steps(path, before, after, notes_directory)
        try:
            while True:
                try:
                    delay = next(steps)
                except StopIteration as complete:
                    return complete.value
                time.sleep(delay)
        finally:
            steps.close()

    def type_text_steps(self, text: str, element_id: str | None = None) -> Generator[float, None, str]:
        """Type on the native owner, yielding delays for focus transitions.

        The GUI dispatcher resumes this generator on the same GTK thread.
        Its original accessible object survives intervening snapshots, while
        every resumed phase validates that object again. No lock crosses a
        yield, and the activation shortcut is sent only in its first phase.
        """
        if not isinstance(text, str) or not text or len(text) > 20000 or "\x00" in text:
            raise ValueError("Text must contain 1–20,000 characters without null bytes.")
        with self._lock:
            target = element_id if element_id else self._focused_target()
            element, states, interfaces = self._validate(target)
            if not states.contains(Atspi.StateType.EDITABLE):
                raise DesktopError("The selected control is not editable.")
            accessible = element.accessible
            text_iface = accessible.get_text_iface() if "Text" in interfaces else None
            editable = accessible.get_editable_text_iface() if "EditableText" in interfaces else None
            if text_iface is not None and editable is not None:
                position = text_iface.get_caret_offset()
                if position < 0:
                    raise DesktopError("The application does not expose an insertion cursor.")
                # insert_text preserves the rest of the document. Never use
                # set_text_contents: it can silently erase existing notes.
                if editable.insert_text(position, text, len(text.encode("utf-8"))):
                    try:
                        text_iface.set_caret_offset(position + len(text))
                    except Exception:
                        pass
                    return f"Inserted {len(text)} characters into {element.app}."
            if text_iface is None:
                raise DesktopError("The application does not expose text selection state; "
                                   "cannot safely insert text into this control.")
        yield from self._ensure_portal_target_focus_steps(element)
        with self._lock:
            self._validate_element(element)
            # Portal typing replaces selected text in many apps, so collapse
            # selections explicitly at the current caret before using it.
            position = text_iface.get_caret_offset()
            if position < 0:
                raise DesktopError("The application does not expose an insertion cursor.")
            selections = text_iface.get_n_selections()
            for index in range(max(0, selections) - 1, -1, -1):
                if not text_iface.remove_selection(index):
                    raise DesktopError("Could not safely clear the current text selection.")
            if not text_iface.set_caret_offset(position):
                raise DesktopError("Could not position the text insertion cursor.")
            # Recheck focus immediately before global keyboard injection.
            if not self._states(accessible).contains(Atspi.StateType.FOCUSED):
                raise DesktopError("Focus changed before typing. Take a new snapshot.")
            # Widget focus can persist in a background Chromium window. Match
            # a fresh global snapshot to the exact intended object before
            # using portal keys; an application-name match alone is too weak.
            focused_id = self._focused_target()
            focused_element, _, _ = self._validate(focused_id)
            if (focused_element.accessible != accessible or focused_element.app != element.app
                    or focused_element.pid != element.pid or focused_element.window_id != element.window_id):
                raise DesktopError("The intended text control is not the verified foreground target.")
            if element.pid and element.window_id and element.window is not None:
                try:
                    # The global scan reads window activation before walking
                    # its descendants. Chromium can retain widget FOCUSED
                    # after that window loses activation during the scan.
                    element.window.clear_cache()
                    window_states = self._states(element.window)
                    if (not window_states.contains(Atspi.StateType.ACTIVE)
                            or window_states.contains(Atspi.StateType.ICONIFIED)):
                        raise DesktopError("The target window lost foreground before typing. Bring it forward, then continue.")
                except DesktopError:
                    raise
                except Exception as exc:
                    raise DesktopError("The target window's foreground state could not be verified.") from exc
            _, final_states, _ = self._validate_element(element)
            if not final_states.contains(Atspi.StateType.FOCUSED):
                raise DesktopError("Focus changed before typing. Take a new snapshot.")
            self._portal.type_text(text)
            return f"Typed {len(text)} characters into {element.app}."

    def press_key(self, key: str, expected_focus: dict | None = None) -> str:
        with self._lock:
            if expected_focus is None:
                self._focused_target()
            else:
                changed = ("The focused target changed. Cancel and repeat the command "
                           "in the intended app.")
                try:
                    current = self.focus_token()
                except DesktopError as exc:
                    raise DesktopError(changed) from exc
                if not isinstance(expected_focus, dict) or current != expected_focus:
                    raise DesktopError(changed)
            self._portal.press_key(key)
            return f"Pressed {key}."

    def scroll(self, direction: str, amount: int = 3) -> str:
        with self._lock:
            self._focused_target()
            self._portal.scroll(direction, amount)
            return f"Scrolled {direction} {amount} steps."

    def open_url(self, url: str, browser: str | None = None) -> str:
        if not isinstance(url, str) or len(url) > 4096 or any(ord(char) < 32 for char in url):
            raise ValueError("Use a valid HTTP or HTTPS URL.")
        parsed = urlsplit(url)
        if (parsed.scheme.casefold() not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("Only HTTP and HTTPS URLs without embedded credentials are supported.")
        if browser is not None:
            with self._lock:
                if not isinstance(browser, str) or not browser.endswith(".desktop"):
                    raise DesktopError("Choose an installed browser ID from the apps list.")
                # Refresh on every explicit launch: a stale cached record is
                # not sufficient evidence that the requested browser exists.
                self.apps()
                app = self._apps.get(browser)
                if app is None:
                    raise DesktopError("The requested browser is not installed or available in the apps list.")
                if not self._is_browser(app):
                    raise DesktopError("The requested application is not a browser that accepts web URLs.")
                name = app.get_display_name() or app.get_name()
                try:
                    launched = app.launch_uris([url], self._launch_context())
                except Exception:
                    raise DesktopError(f"Ubuntu could not open the URL in {name}.") from None
                if not launched:
                    raise DesktopError(f"Ubuntu could not open the URL in {name}.")
                return f"Opened {url} in {name}."
        if not Gio.AppInfo.launch_default_for_uri(url, self._launch_context()):
            raise DesktopError("Ubuntu could not open the URL in your default browser.")
        return f"Opened {url}."

    def request_control(self) -> str:
        return self._portal.request_control()

    def status(self) -> dict:
        portal = self._portal.status()
        return {"session_type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
                "accessibility": not bool(self._atspi_error),
                "accessibility_error": self._atspi_error,
                "portal": portal,
                "capabilities": {"launch_apps": True, "open_urls": True,
                                 "accessibility_actions": not bool(self._atspi_error),
                                 "accessibility_text": not bool(self._atspi_error),
                                 "keyboard": bool(portal.get("keyboard")),
                                 "scroll": bool(portal.get("pointer"))}}

    def close(self) -> None:
        self._portal.close()
        self._refs.clear()
