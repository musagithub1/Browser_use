"""Keep GTK, GDK, and AT-SPI desktop objects on their native UI thread."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import threading
import time

from gi.repository import GLib


class DesktopDispatchError(RuntimeError):
    pass


@dataclass(eq=False)
class _Call:
    method: str
    args: tuple
    kwargs: dict
    guard: object = None
    done: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    running: bool = False
    cancelled: bool = False
    steps: object = None
    result: object = None
    error: BaseException | None = None


class MainThreadDesktopBackend:
    """Marshal native desktop calls to the thread running GTK's main loop.

    Waiting workers never own accessibility proxies or launch contexts. A
    queued action loses its lease on cancellation, close, or queue timeout.
    Once an effect begins, its caller waits for the actual outcome so it cannot
    mistake a timeout for permission to retry an action that already happened.
    """

    _EFFECTS = frozenset({"launch_app", "open_url", "click", "type_text", "press_key", "scroll", "refresh_note"})
    _GUARDED = _EFFECTS | {"verify_note_visible"}
    _RESUMABLE = frozenset({"type_text", "refresh_note", "verify_note_visible"})

    def __init__(self, backend, *, timeout_seconds: float = 30, schedule=None,
                 schedule_after=None):
        self._backend = backend
        self._owner = threading.get_ident()
        self._lock = threading.RLock()
        self._closed = False
        self._pending = set()
        self._timeout = max(0.01, float(timeout_seconds))
        self._schedule = schedule or GLib.idle_add
        if schedule_after is not None:
            self._schedule_after = schedule_after
        elif schedule is not None:
            # Custom event queues retain their callback-only interface. Tests
            # that model elapsed time can supply a delayed scheduler as well.
            self._schedule_after = lambda delay, callback: self._schedule(callback)
        else:
            self._schedule_after = lambda delay, callback: GLib.timeout_add(
                max(1, math.ceil(delay * 1000)), callback,
                priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _close_steps(self, call):
        """Release suspended native proxies only on the GTK owner thread."""
        steps, call.steps = call.steps, None
        if steps is not None:
            steps.close()

    def _invoke(self, method: str, args=(), kwargs=None, guard=None):
        kwargs = dict(kwargs or {})
        if threading.get_ident() == self._owner:
            with self._lock:
                if self._closed:
                    raise DesktopDispatchError("Desktop control is closed.")
            if guard is not None and not guard():
                raise DesktopDispatchError("Task changed before desktop action.")
            if method in self._RESUMABLE and callable(getattr(self._backend, method + "_steps", None)):
                # Waiting here would prevent the very focus-out event needed
                # by the continuation. Reject before creating it or acting.
                raise DesktopDispatchError("Resumable desktop actions must be dispatched from the command worker.")
            return getattr(self._backend, method)(*args, **kwargs)

        call = _Call(method, tuple(args), kwargs, guard)
        with self._lock:
            if self._closed:
                raise DesktopDispatchError("Desktop control is closed.")
            self._pending.add(call)

        def finish(error=None):
            if error is not None and call.error is None:
                call.error = error
            try:
                self._close_steps(call)
            except BaseException as cleanup_error:
                if call.error is None:
                    call.error = cleanup_error
            with self._lock:
                call.running = False
                self._pending.discard(call)
                call.done.set()

        def dispatch():
            with self._lock:
                cancelled = call.done.is_set() or call.cancelled or self._closed
                if not cancelled:
                    call.running = True
                    call.started = True
            if cancelled:
                finish(DesktopDispatchError("Desktop call was cancelled before its next phase."))
                return False
            try:
                if call.guard is not None and not call.guard():
                    raise DesktopDispatchError("Task changed before desktop action.")
                if call.steps is None:
                    resumable = (getattr(self._backend, call.method + "_steps", None)
                                 if call.method in self._RESUMABLE else None)
                    if callable(resumable):
                        call.steps = resumable(*call.args, **call.kwargs)
                    else:
                        call.result = getattr(self._backend, call.method)(*call.args, **call.kwargs)
                        finish()
                        return False
                try:
                    delay = next(call.steps)
                except StopIteration as complete:
                    call.result = complete.value
                    finish()
                    return False
                if (not isinstance(delay, (float, int)) or not math.isfinite(delay)
                        or delay < 0):
                    raise DesktopDispatchError("The native action yielded an invalid continuation delay.")
            except BaseException as error:
                finish(error)
                return False
            finally:
                with self._lock:
                    call.running = False
            try:
                # Keep the same call and lease pending while GTK processes
                # ordinary events. No recursive main-loop iteration is used.
                self._schedule_after(delay, dispatch)
            except BaseException as error:
                finish(error)
            return False

        try:
            self._schedule(dispatch)
        except BaseException:
            with self._lock:
                call.cancelled = True
                self._pending.discard(call)
            raise
        deadline = time.monotonic() + self._timeout
        while not call.done.wait(0.05):
            if time.monotonic() < deadline:
                continue
            with self._lock:
                # The native operation already began. Await its real result;
                # abandoning it would hide effects and invite duplicate work.
                if call.started and method in self._EFFECTS:
                    continue
                call.cancelled = True
                self._pending.discard(call)
            raise DesktopDispatchError("The desktop UI did not respond in time; the queued call was cancelled.")
        if call.error is not None:
            raise call.error
        return call.result

    def run_guarded(self, method: str, args=(), kwargs=None, guard=None):
        if method not in self._GUARDED:
            raise ValueError("Only supported desktop actions can use a guarded dispatch.")
        return self._invoke(method, args, kwargs, guard)

    def apps(self):
        return self._invoke("apps")

    def snapshot(self, *args, **kwargs):
        return self._invoke("snapshot", args, kwargs)

    def snapshot_for_context(self, context):
        return self._invoke("snapshot_for_context", (dict(context),))

    def needs_desktop_permission(self, operation, arguments):
        return self._invoke("needs_desktop_permission", (operation, dict(arguments)))

    def launch_app(self, app_id):
        return self._invoke("launch_app", (app_id,))

    def open_url(self, url, browser=None):
        return self._invoke("open_url", (url,), {"browser": browser})

    def click(self, element_id):
        return self._invoke("click", (element_id,))

    def type_text(self, text, element_id=None):
        return self._invoke("type_text", (text, element_id))

    def refresh_note(self, path, before, after, notes_directory):
        return self._invoke("refresh_note", (path, before, after, notes_directory))

    def verify_note_visible(self, path, snapshot, notes_directory):
        return self._invoke("verify_note_visible", (path, snapshot, notes_directory))

    def press_key(self, key, expected_focus=None):
        return self._invoke("press_key", (key,), {"expected_focus": expected_focus})

    def scroll(self, direction, amount=3):
        return self._invoke("scroll", (direction, amount))

    def focus_token(self, snapshot=None):
        return self._invoke("focus_token", (snapshot,))

    def default_browser_id(self):
        return self._invoke("default_browser_id")

    def capture_launch_context(self, timestamp=None):
        return self._invoke("capture_launch_context", (timestamp,))

    def status(self):
        return self._backend.status()

    def request_control(self):
        with self._lock:
            if self._closed:
                raise DesktopDispatchError("Desktop control is closed.")
        return self._backend.request_control()

    def close(self):
        suspended = []
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for call in tuple(self._pending):
                if not call.running:
                    call.cancelled = True
                    call.error = DesktopDispatchError("Desktop control closed before the action completed.")
                    call.done.set()
                    self._pending.discard(call)
                    suspended.append(call)

        def cleanup():
            for call in suspended:
                self._close_steps(call)
            self._backend.close()
            return False

        if threading.get_ident() == self._owner:
            cleanup()
        else:
            # Native proxy references must also be released on their owner.
            self._schedule(cleanup)
