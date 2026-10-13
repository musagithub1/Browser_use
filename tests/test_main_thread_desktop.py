import queue
import threading
import time
import unittest

from jev_linux.main_thread_desktop import MainThreadDesktopBackend, DesktopDispatchError


class NativeBackend:
    def __init__(self):
        self.calls = []

    def note(self, name):
        self.calls.append((name, threading.get_ident()))

    def apps(self):
        self.note("apps")
        return [{"id": "chrome.desktop"}]

    def snapshot(self, **kwargs):
        self.note("snapshot")
        return kwargs

    def open_url(self, url, browser=None):
        self.note("open_url")
        return "Opened the requested URL"

    def status(self):
        self.note("status")
        return {"portal": {"keyboard": True}}

    def request_control(self):
        self.note("request_control")
        return "Desktop control is ready"

    def close(self):
        self.note("close")


class ResumableNativeBackend(NativeBackend):
    def __init__(self):
        super().__init__()
        self.foreground_ready = False
        self.activations = 0
        self.typed = []

    def type_text(self, *args):
        raise AssertionError("The GTK adapter must drive the resumable typing API.")

    def type_text_steps(self, text, element_id=None):
        self.note("activate")
        self.activations += 1
        try:
            yield 0.05
            while not self.foreground_ready:
                self.note("wait_foreground")
                yield 0.05
            self.note("type_text")
            self.typed.append((text, element_id))
            return "Typed the requested text"
        finally:
            self.note("typing_finished")


class ResumableNoteBackend(NativeBackend):
    def __init__(self):
        super().__init__()
        self.reloaded = False

    def refresh_note(self, *args):
        raise AssertionError("The GTK adapter must drive the resumable note refresh.")

    def refresh_note_steps(self, *args):
        self.note("check_note")
        try:
            yield .05
            self.note("reload_note")
            self.reloaded = True
            yield .05
            self.note("verify_note")
            return True
        finally:
            self.note("refresh_finished")


class MainThreadDesktopTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeBackend()
        self.scheduled = queue.Queue()
        self.proxy = MainThreadDesktopBackend(self.native, schedule=self.scheduled.put)

    def worker(self, callback):
        outcome = {}
        def run():
            try:
                outcome["result"] = callback()
            except Exception as error:
                outcome["error"] = error
        thread = threading.Thread(target=run)
        thread.start()
        return thread, outcome

    def dispatch(self):
        callback = self.scheduled.get(timeout=1)
        self.assertIs(callback(), False)

    def use_resumable_backend(self):
        self.native = ResumableNativeBackend()
        self.resume_delays = []

        def schedule_after(delay, callback):
            self.resume_delays.append(delay)
            self.scheduled.put(callback)

        self.proxy = MainThreadDesktopBackend(self.native, schedule=self.scheduled.put,
                                              schedule_after=schedule_after)
        self.addCleanup(self.proxy.close)

    def test_native_objects_are_only_used_by_the_owner_thread(self):
        owner = threading.get_ident()
        thread, outcome = self.worker(lambda: (self.proxy.apps(), self.proxy.snapshot(preferred_app="Chrome")))
        self.dispatch()
        self.dispatch()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["result"], ([{"id": "chrome.desktop"}], {"preferred_app": "Chrome"}))
        self.assertEqual(self.native.calls, [("apps", owner), ("snapshot", owner)])

    def test_owner_call_does_not_wait_for_its_own_main_loop(self):
        self.proxy.open_url("https://youtube.com")
        self.assertTrue(self.scheduled.empty())
        self.assertEqual(self.native.calls, [("open_url", threading.get_ident())])

    def test_native_error_is_propagated_to_waiting_worker(self):
        error = ValueError("The control disappeared")
        self.native.snapshot = lambda **kwargs: (_ for _ in ()).throw(error)
        thread, outcome = self.worker(self.proxy.snapshot)
        self.dispatch()
        thread.join(1)
        self.assertIs(outcome["error"], error)

    def test_changed_task_cannot_act_after_waiting_in_the_ui_queue(self):
        current = {"valid": True}
        thread, outcome = self.worker(lambda: self.proxy.run_guarded(
            "open_url", ("https://youtube.com",), {}, lambda: current["valid"]))
        callback = self.scheduled.get(timeout=1)
        current["valid"] = False
        callback()
        thread.join(1)
        self.assertRegex(str(outcome["error"]), "Task changed")
        self.assertEqual(self.native.calls, [])

    def test_close_wakes_worker_and_prevents_later_queued_action(self):
        thread, outcome = self.worker(lambda: self.proxy.open_url("https://youtube.com"))
        callback = self.scheduled.get(timeout=1)
        self.proxy.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome["error"], DesktopDispatchError)
        callback()
        self.assertEqual(self.native.calls, [("close", threading.get_ident())])

    def test_queue_timeout_cannot_launch_an_action_when_ui_later_recovers(self):
        self.proxy._timeout = 0.01
        thread, outcome = self.worker(lambda: self.proxy.open_url("https://youtube.com"))
        callback = self.scheduled.get(timeout=1)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome["error"], DesktopDispatchError)
        callback()
        self.assertEqual(self.native.calls, [])

    def test_portal_status_and_consent_do_not_block_on_the_gtk_queue(self):
        thread, outcome = self.worker(lambda: (self.proxy.status(), self.proxy.request_control()))
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.scheduled.empty())
        self.assertTrue(all(tid == thread.ident for _, tid in self.native.calls))
        self.assertEqual(outcome["result"][0], {"portal": {"keyboard": True}})

    def test_off_owner_close_releases_native_references_on_owner(self):
        thread, _ = self.worker(self.proxy.close)
        thread.join(1)
        self.assertEqual(self.native.calls, [])
        self.dispatch()
        self.assertEqual(self.native.calls, [("close", threading.get_ident())])

    def test_running_action_waits_for_actual_result_after_queue_deadline(self):
        scheduled = queue.Queue()
        ready = threading.Event()
        started = threading.Event()
        release = threading.Event()
        holder = {}
        native = NativeBackend()

        def open_url(url, browser=None):
            native.note("open_url")
            started.set()
            release.wait(2)
            return "Real action result"
        native.open_url = open_url

        def owner_loop():
            holder["proxy"] = MainThreadDesktopBackend(native, timeout_seconds=0.01, schedule=scheduled.put)
            ready.set()
            while True:
                callback = scheduled.get()
                if callback is None:
                    return
                callback()
        owner = threading.Thread(target=owner_loop)
        owner.start()
        self.addCleanup(lambda: (release.set(), scheduled.put(None), owner.join(2)))
        self.assertTrue(ready.wait(1))
        thread, outcome = self.worker(lambda: holder["proxy"].open_url("https://youtube.com"))
        self.assertTrue(started.wait(1))
        time.sleep(0.1)
        self.assertTrue(thread.is_alive())
        release.set()
        thread.join(1)
        self.assertEqual(outcome["result"], "Real action result")
        self.assertEqual(native.calls, [("open_url", owner.ident)])

    def test_resumable_typing_returns_to_owner_loop_between_activation_and_text(self):
        self.use_resumable_backend()
        owner = threading.get_ident()
        thread, outcome = self.worker(lambda: self.proxy.type_text("Hello", "chosen"))
        self.dispatch()

        self.assertTrue(thread.is_alive())
        self.assertEqual(self.native.activations, 1)
        self.assertEqual(self.native.typed, [])
        self.assertEqual(self.resume_delays, [0.05])

        # The owner main loop can process native focus notifications here.
        self.native.foreground_ready = True
        self.dispatch()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["result"], "Typed the requested text")
        self.assertEqual(self.native.typed, [("Hello", "chosen")])
        self.assertEqual(self.native.activations, 1)
        self.assertEqual(self.native.calls, [("activate", owner), ("type_text", owner),
                                           ("typing_finished", owner)])

    def test_cancelled_suspended_typing_settles_worker_without_text_or_reactivation(self):
        self.use_resumable_backend()
        current = {"valid": True}
        thread, outcome = self.worker(lambda: self.proxy.run_guarded(
            "type_text", ("Hello", "chosen"), {}, lambda: current["valid"]))
        self.dispatch()
        current["valid"] = False
        self.native.foreground_ready = True
        self.dispatch()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome["error"], DesktopDispatchError)
        self.assertEqual(self.native.activations, 1)
        self.assertEqual(self.native.typed, [])
        self.assertIn(("typing_finished", threading.get_ident()), self.native.calls)
        self.assertTrue(self.scheduled.empty())

    def test_close_settles_started_suspended_typing_and_later_resume_has_no_effect(self):
        self.use_resumable_backend()
        thread, outcome = self.worker(lambda: self.proxy.type_text("Hello", "chosen"))
        self.dispatch()
        resume = self.scheduled.get(timeout=1)
        self.proxy.close()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome["error"], DesktopDispatchError)
        self.native.foreground_ready = True
        self.assertIs(resume(), False)
        self.assertEqual(self.native.activations, 1)
        self.assertEqual(self.native.typed, [])
        self.assertIn(("typing_finished", threading.get_ident()), self.native.calls)
        self.assertIn(("close", threading.get_ident()), self.native.calls)

    def test_off_owner_close_settles_suspended_worker_and_releases_steps_on_owner(self):
        self.use_resumable_backend()
        owner = threading.get_ident()
        thread, outcome = self.worker(lambda: self.proxy.type_text("Hello", "chosen"))
        self.dispatch()
        resume = self.scheduled.get(timeout=1)
        closer, close_outcome = self.worker(self.proxy.close)
        closer.join(1)
        thread.join(1)

        self.assertFalse(closer.is_alive())
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", close_outcome)
        self.assertIsInstance(outcome["error"], DesktopDispatchError)
        self.assertEqual(self.native.calls, [("activate", owner)])

        self.dispatch()
        self.native.foreground_ready = True
        self.assertIs(resume(), False)
        self.assertEqual(self.native.calls, [("activate", owner), ("typing_finished", owner),
                                           ("close", owner)])
        self.assertEqual(self.native.activations, 1)
        self.assertEqual(self.native.typed, [])

    def test_started_suspended_typing_waits_past_queue_deadline_for_actual_result(self):
        self.use_resumable_backend()
        self.proxy._timeout = 0.01
        thread, outcome = self.worker(lambda: self.proxy.type_text("Hello", "chosen"))
        self.dispatch()
        time.sleep(0.1)

        self.assertTrue(thread.is_alive())
        self.assertEqual(self.native.activations, 1)
        self.assertEqual(self.native.typed, [])
        self.native.foreground_ready = True
        self.dispatch()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["result"], "Typed the requested text")
        self.assertEqual(self.native.activations, 1)
        self.assertEqual(self.native.typed, [("Hello", "chosen")])

    def test_owner_cannot_start_blocking_resumable_typing(self):
        self.use_resumable_backend()
        with self.assertRaises(DesktopDispatchError):
            self.proxy.type_text("Hello", "chosen")

        self.assertEqual(self.native.calls, [])
        self.assertTrue(self.scheduled.empty())

    def test_note_refresh_yields_and_verifies_on_owner_thread(self):
        native = ResumableNoteBackend()
        self.proxy = MainThreadDesktopBackend(native, schedule=self.scheduled.put)
        thread, outcome = self.worker(lambda: self.proxy.run_guarded(
            "refresh_note", ("note.md", {}, {}, "notes"), {}, lambda: True))
        self.dispatch()
        self.assertFalse(native.reloaded)
        self.dispatch()
        self.assertTrue(native.reloaded)
        self.assertTrue(thread.is_alive())
        self.dispatch()
        thread.join(1)
        self.assertIs(outcome["result"], True)
        self.assertEqual(native.calls, [(name, threading.get_ident()) for name in
                                      ("check_note", "reload_note", "verify_note", "refresh_finished")])

    def test_cancelled_note_refresh_cannot_reload_after_its_wait(self):
        native = ResumableNoteBackend()
        self.proxy = MainThreadDesktopBackend(native, schedule=self.scheduled.put)
        current = {"valid": True}
        thread, outcome = self.worker(lambda: self.proxy.run_guarded(
            "refresh_note", ("note.md", {}, {}, "notes"), {}, lambda: current["valid"]))
        self.dispatch()
        current["valid"] = False
        self.dispatch()
        thread.join(1)
        self.assertIsInstance(outcome["error"], DesktopDispatchError)
        self.assertFalse(native.reloaded)
        self.assertEqual(native.calls, [("check_note", threading.get_ident()),
                                       ("refresh_finished", threading.get_ident())])

    def test_owner_cannot_start_blocking_note_refresh(self):
        native = ResumableNoteBackend()
        self.proxy = MainThreadDesktopBackend(native, schedule=self.scheduled.put)
        with self.assertRaises(DesktopDispatchError):
            self.proxy.refresh_note("note.md", {}, {}, "notes")
        self.assertEqual(native.calls, [])


if __name__ == "__main__":
    unittest.main()
