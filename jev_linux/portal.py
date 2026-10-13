"""Wayland input through the user's RemoteDesktop portal permission.

The portal owns the permission dialog. This module never clicks or bypasses it.
Gio callbacks run on a private GLib context so callers can wait on a worker while
the application's GTK main loop remains available.
"""

from __future__ import annotations

import threading
import uuid
from typing import Callable

import gi

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib


BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
REMOTE = "org.freedesktop.portal.RemoteDesktop"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"


class PortalError(RuntimeError):
    """An unavailable, denied, expired, or unsupported portal operation."""


def parse_key(key: str) -> list[int]:
    """Resolve a deliberately bounded key chord to XKB keysyms."""
    if not isinstance(key, str) or len(key) > 60:
        raise ValueError("Use a key such as Enter, Tab, or Ctrl+N.")
    parts = [part.strip() for part in key.split("+")]
    if not parts or not all(parts) or len(parts) > 4:
        raise ValueError("Use a key such as Enter, Tab, or Ctrl+N.")
    modifiers = {"ctrl": "Control_L", "control": "Control_L", "alt": "Alt_L",
                 "shift": "Shift_L", "super": "Super_L", "meta": "Super_L"}
    names = {"enter": "Return", "return": "Return", "esc": "Escape",
             "escape": "Escape", "tab": "Tab", "space": "space",
             "backspace": "BackSpace", "delete": "Delete", "del": "Delete",
             "left": "Left", "right": "Right", "up": "Up", "down": "Down",
             "home": "Home", "end": "End", "pageup": "Page_Up",
             "page_up": "Page_Up", "pagedown": "Page_Down",
             "page_down": "Page_Down", "insert": "Insert"}
    resolved = []
    for part in parts[:-1]:
        name = modifiers.get(part.lower())
        if not name or name in resolved:
            raise ValueError("Only Ctrl, Alt, Shift, and Super are supported modifiers.")
        resolved.append(name)
    last = parts[-1].lower()
    if len(last) == 1 and last.isascii() and last.isalnum():
        name = last
    elif last.startswith("f") and last[1:].isdigit() and 1 <= int(last[1:]) <= 12:
        name = last.upper()
    else:
        name = names.get(last)
    if not name:
        raise ValueError("Unsupported key. Use letters, digits, F1–F12, or a navigation key.")
    if name == "Delete" and {"Control_L", "Alt_L"}.issubset(resolved):
        raise ValueError("Ctrl+Alt+Delete is not supported.")
    resolved.append(name)
    return [Gdk.keyval_from_name(name) for name in resolved]


class RemoteDesktopPortal:
    def __init__(self, *, consent_timeout: float = 120.0):
        self.consent_timeout = consent_timeout
        self._context = GLib.MainContext.new()
        self._loop = GLib.MainLoop.new(self._context, False)
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._closed = False
        self._connection = None
        self._session = None
        self._session_subscription = 0
        self._devices = 0
        self._available_devices = 0
        self._version = 0
        self._error = ""
        self._pending = {}
        self._thread = threading.Thread(target=self._run, name="jev-portal", daemon=True)
        self._thread.start()
        self._ready.wait(8)

    def _run(self):
        self._context.push_thread_default()
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = self._connection.call_sync(
                BUS_NAME, OBJECT_PATH, "org.freedesktop.DBus.Properties", "GetAll",
                GLib.Variant("(s)", (REMOTE,)), GLib.VariantType.new("(a{sv})"),
                Gio.DBusCallFlags.NONE, 5000, None)
            properties = reply.unpack()[0]
            self._available_devices = int(properties.get("AvailableDeviceTypes", 0))
            self._version = int(properties.get("version", 0))
        except Exception as exc:
            self._error = f"RemoteDesktop portal unavailable: {exc}"
        finally:
            self._ready.set()
        try:
            self._loop.run()
        finally:
            self._context.pop_thread_default()

    def _schedule(self, callback: Callable):
        source = GLib.idle_source_new()
        def run(*_):
            callback()
            return GLib.SOURCE_REMOVE
        source.set_callback(run)
        source.attach(self._context)

    def _call(self, function: Callable, timeout: float = 8):
        if self._closed:
            raise PortalError("Desktop control is closed.")
        finished = threading.Event()
        result = {}
        def run():
            try:
                result["value"] = function()
            except Exception as exc:
                result["error"] = exc
            finally:
                finished.set()
        self._schedule(run)
        if not finished.wait(timeout):
            raise PortalError("The desktop portal did not respond in time.")
        if "error" in result:
            raise PortalError(str(result["error"])) from result["error"]
        return result.get("value")

    def status(self) -> dict:
        with self._lock:
            return {"available": bool(self._version), "version": self._version,
                    "available_devices": self._available_devices,
                    "granted": bool(self._session and self._devices),
                    "keyboard": bool(self._session and self._devices & 1),
                    "pointer": bool(self._session and self._devices & 2),
                    "pending": bool(self._pending), "error": self._error,
                    "closed": self._closed}

    def _request(self, method: str, make_parameters: Callable, timeout: float) -> dict:
        if self._closed or not self._connection:
            raise PortalError(self._error or "Desktop portal is unavailable.")
        token = "jev_" + uuid.uuid4().hex
        sender = self._connection.get_unique_name().lstrip(":").replace(".", "_")
        expected = f"{OBJECT_PATH}/request/{sender}/{token}"
        done = threading.Event()
        state = {"handle": expected, "actual": None, "subscription": 0,
                 "early": {}, "done": False, "event": done}
        with self._lock:
            self._pending[token] = state

        def finish(response=None, error=None):
            if state["done"]:
                return
            state["done"] = True
            if error is not None:
                state["error"] = error
            else:
                state["response"] = response
            if state["subscription"]:
                self._connection.signal_unsubscribe(state["subscription"])
                state["subscription"] = 0
            with self._lock:
                self._pending.pop(token, None)
            done.set()

        def response(_conn, _sender, path, _interface, _signal, parameters, *_):
            unpacked = parameters.unpack()
            if path in (expected, state["actual"]):
                finish(response=unpacked)
            elif state["actual"] is None:
                state["early"][path] = unpacked

        def called(connection, result, *_):
            try:
                actual = connection.call_finish(result).unpack()[0]
                state["actual"] = state["handle"] = actual
                if actual in state["early"]:
                    finish(response=state["early"][actual])
            except Exception as exc:
                finish(error=exc)

        def begin():
            if self._closed:
                finish(error=PortalError("Desktop control was closed."))
                return
            # Subscribe before invoking the method: immediate Response signals
            # are otherwise easy to miss. A wildcard path also supports older
            # portal versions that return a handle different from the prediction.
            state["subscription"] = self._connection.signal_subscribe(
                BUS_NAME, REQUEST, "Response", None, None,
                Gio.DBusSignalFlags.NONE, response)
            try:
                self._connection.call(
                    BUS_NAME, OBJECT_PATH, REMOTE, method, make_parameters(token),
                    GLib.VariantType.new("(o)"), Gio.DBusCallFlags.NONE, 10000,
                    None, called)
            except Exception as exc:
                finish(error=exc)

        self._schedule(begin)
        if not done.wait(timeout):
            def cancel():
                self._close_object(state["handle"], REQUEST)
                finish(error=PortalError(
                    "Desktop control consent timed out. Click Allow in Ubuntu's "
                    "sharing dialog, then request control again."))
            self._schedule(cancel)
            done.wait(2)
            raise PortalError("Desktop control consent timed out; request control again.")
        if "error" in state:
            raise PortalError(str(state["error"])) from state["error"]
        code, results = state["response"]
        if code != 0:
            detail = "cancelled" if code == 1 else "denied or unavailable"
            raise PortalError(f"Desktop control permission was {detail}. "
                              "Request control again and click Allow in Ubuntu's dialog.")
        return results

    def request_control(self, parent_window: str = "") -> str:
        """Request consent; invoke on a worker, never from GTK's main thread."""
        with self._request_lock:
            if self._closed:
                raise PortalError("Desktop control is closed.")
            if self._session and self._devices & 1 and self._devices & 2:
                return "Keyboard and pointer control is already enabled."
            if not self._version:
                raise PortalError(self._error or "Ubuntu's RemoteDesktop portal is unavailable.")
            if not self._available_devices & 1:
                raise PortalError("This desktop portal does not provide keyboard control.")
            self._end_session()
            try:
                result = self._request("CreateSession", lambda token: GLib.Variant(
                    "(a{sv})", ({"handle_token": GLib.Variant("s", token),
                                  "session_handle_token": GLib.Variant(
                                      "s", "jev_" + uuid.uuid4().hex)},)), 15)
                self._session = result["session_handle"]
                self._call(self._subscribe_session)
                devices = self._available_devices & 3
                self._request("SelectDevices", lambda token: GLib.Variant(
                    "(oa{sv})", (self._session,
                                   {"handle_token": GLib.Variant("s", token),
                                    "types": GLib.Variant("u", devices)})), 15)
                result = self._request("Start", lambda token: GLib.Variant(
                    "(osa{sv})", (self._session, parent_window,
                                    {"handle_token": GLib.Variant("s", token)})),
                    self.consent_timeout)
                self._devices = int(result.get("devices", 0))
                if not self._session or not self._devices & 1:
                    raise PortalError("Keyboard control was not granted in Ubuntu's dialog.")
                self._error = ""
                return "Desktop control enabled with Ubuntu's permission."
            except Exception as exc:
                self._error = str(exc)
                self._end_session()
                raise

    def _subscribe_session(self):
        def closed(*_):
            with self._lock:
                self._session = None
                self._devices = 0
                self._error = "Ubuntu ended desktop control. Request control again."
            if self._session_subscription:
                self._connection.signal_unsubscribe(self._session_subscription)
                self._session_subscription = 0
        self._session_subscription = self._connection.signal_subscribe(
            BUS_NAME, SESSION, "Closed", self._session, None,
            Gio.DBusSignalFlags.NONE, closed)

    def _close_object(self, path: str, interface: str):
        if path and self._connection:
            self._connection.call(BUS_NAME, path, interface, "Close", None,
                                  None, Gio.DBusCallFlags.NONE, 2000, None, None)

    def _end_session(self):
        if self._closed:
            return
        def end():
            if self._session_subscription:
                self._connection.signal_unsubscribe(self._session_subscription)
                self._session_subscription = 0
            self._close_object(self._session, SESSION)
            self._session = None
            self._devices = 0
        self._call(end)

    def _notify(self, method: str, suffix: str, arguments: tuple, device: int):
        def notify():
            if not self._session or not self._devices & device:
                name = "keyboard" if device == 1 else "pointer"
                raise PortalError(f"No {name} control permission. Use Enable desktop control "
                                  "and click Allow in Ubuntu's sharing dialog.")
            self._connection.call_sync(
                BUS_NAME, OBJECT_PATH, REMOTE, method,
                GLib.Variant("(oa{sv}" + suffix + ")", (self._session, {}, *arguments)),
                None, Gio.DBusCallFlags.NONE, 3000, None)
        self._call(notify)

    def press_key(self, key: str):
        keysyms = parse_key(key)
        pressed = []
        try:
            for keysym in keysyms:
                self._notify("NotifyKeyboardKeysym", "iu", (keysym, 1), 1)
                pressed.append(keysym)
        finally:
            for keysym in reversed(pressed):
                self._notify("NotifyKeyboardKeysym", "iu", (keysym, 0), 1)

    def type_text(self, text: str):
        for character in text:
            if character == "\n":
                keysym = Gdk.KEY_Return
            elif character == "\t":
                keysym = Gdk.KEY_Tab
            elif character == "\r":
                continue
            else:
                keysym = Gdk.unicode_to_keyval(ord(character))
            self._notify("NotifyKeyboardKeysym", "iu", (keysym, 1), 1)
            self._notify("NotifyKeyboardKeysym", "iu", (keysym, 0), 1)

    def scroll(self, direction: str, amount: int = 3):
        axes = {"up": (0, -1), "down": (0, 1), "left": (1, -1), "right": (1, 1)}
        if direction.lower() not in axes or isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 20:
            raise ValueError("Scroll direction must be up/down/left/right and amount 1–20.")
        axis, sign = axes[direction.lower()]
        self._notify("NotifyPointerAxisDiscrete", "ui", (axis, sign * amount), 2)

    def close(self):
        if self._closed:
            return
        def close_all():
            for state in list(self._pending.values()):
                self._close_object(state["handle"], REQUEST)
                if state["subscription"]:
                    self._connection.signal_unsubscribe(state["subscription"])
                state["done"] = True
                state["error"] = PortalError("Desktop control was closed.")
                state["event"].set()
            self._pending.clear()
            if self._session_subscription:
                self._connection.signal_unsubscribe(self._session_subscription)
                self._session_subscription = 0
            self._close_object(self._session, SESSION)
            self._session = None
            self._devices = 0
            self._closed = True
            self._loop.quit()
        try:
            self._call(close_all)
        finally:
            self._closed = True
            self._thread.join(timeout=2)
