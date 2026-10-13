import threading
import unittest
from unittest.mock import Mock

from jev_linux.portal import RemoteDesktopPortal, PortalError, OBJECT_PATH
from gi.repository import GLib


class FakeConnection:
    def __init__(self, response_code=0, old_handle=False, respond=True):
        self.callback = None
        self.events = []
        self.response_code = response_code
        self.old_handle = old_handle
        self.respond = respond

    def get_unique_name(self):
        return ":1.77"

    def signal_subscribe(self, *args):
        self.events.append("subscribe")
        self.callback = args[-1]
        return 1

    def signal_unsubscribe(self, identifier):
        self.events.append("unsubscribe")

    def call(self, bus, path, interface, method, parameters, reply_type, flags,
             timeout, cancellable, callback):
        self.events.append(method)
        if method == "Close":
            return
        options = parameters.unpack()[0]
        actual = (OBJECT_PATH + "/request/legacy/handle" if self.old_handle else
                  OBJECT_PATH + "/request/1_77/" + options["handle_token"])
        # A portal may emit Response before the method reply is dispatched.
        if self.respond:
            self.callback(self, "portal", actual, "request", "Response",
                          GLib.Variant("(ua{sv})", (self.response_code,
                                       {"session_handle": GLib.Variant("s", "/session/test")})))
        callback(self, GLib.Variant("(o)", (actual,)))

    def call_finish(self, result):
        return result


def make_portal(connection=None):
    portal = RemoteDesktopPortal.__new__(RemoteDesktopPortal)
    portal._closed = False
    portal._connection = connection or FakeConnection()
    portal._error = ""
    portal._pending = {}
    portal._lock = threading.RLock()
    portal._schedule = lambda function: function()
    return portal


class PortalTests(unittest.TestCase):
    def request(self, portal, timeout=0.01):
        return portal._request("CreateSession", lambda token: GLib.Variant(
            "(a{sv})", ({"handle_token": GLib.Variant("s", token)},)), timeout)

    def test_response_subscription_precedes_call_and_handles_early_response(self):
        portal = make_portal()
        self.assertEqual(self.request(portal), {"session_handle": "/session/test"})
        self.assertEqual(portal._connection.events, ["subscribe", "CreateSession", "unsubscribe"])
        self.assertFalse(portal._pending)

    def test_legacy_handle_early_response_is_not_lost(self):
        portal = make_portal(FakeConnection(old_handle=True))
        self.assertEqual(self.request(portal), {"session_handle": "/session/test"})
        self.assertFalse(portal._pending)

    def test_cancelled_consent_is_reported_as_cancelled(self):
        portal = make_portal(FakeConnection(response_code=1))
        with self.assertRaisesRegex(PortalError, "cancelled"):
            self.request(portal)
        self.assertFalse(portal._pending)

    def test_consent_timeout_closes_request_and_unsubscribes(self):
        portal = make_portal(FakeConnection(respond=False))
        with self.assertRaisesRegex(PortalError, "timed out"):
            self.request(portal, timeout=0.001)
        self.assertEqual(portal._connection.events,
                         ["subscribe", "CreateSession", "Close", "unsubscribe"])
        self.assertFalse(portal._pending)

    def test_keyboard_permission_must_come_from_start_response(self):
        portal = make_portal()
        portal._request_lock = threading.Lock()
        portal._session = None
        portal._devices = 0
        portal._available_devices = 7
        portal._version = 2
        portal.consent_timeout = 10
        portal._end_session = Mock()
        portal._call = Mock()
        portal._request = Mock(side_effect=[{"session_handle": "/session/test"}, {}, {"devices": 2}])
        with self.assertRaisesRegex(PortalError, "Keyboard control was not granted"):
            portal.request_control()
        self.assertEqual([call.args[0] for call in portal._request.call_args_list],
                         ["CreateSession", "SelectDevices", "Start"])
        self.assertEqual(portal._end_session.call_count, 2)


if __name__ == "__main__":
    unittest.main()
