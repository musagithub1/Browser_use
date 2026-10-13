"""A small native GTK desktop surface for Jev Linux."""

from __future__ import annotations

import threading
import re
import signal
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from .voice import create_voice_recognizer, speech_model_name
from .config import DATA_DIR


_CSS = b"""
window { background: #101722; color: #edf2f8; }
label { color: #edf2f8; }
.title { font-size: 27px; font-weight: bold; }
.subtitle { color: #96a6bb; font-size: 12px; }
.section { font-size: 12px; font-weight: bold; color: #96a6bb; }
.card { background: #182231; border: 1px solid #2a384d; border-radius: 12px; padding: 16px; }
.badge { color: #75d8c0; background: #1c3440; border-radius: 8px; padding: 6px 10px; }
button { background: #27364b; color: #edf2f8; border: 1px solid #354963; border-radius: 8px; padding: 10px 15px; }
button:hover { background: #344965; }
button.primary { background: #35bda0; border-color: #35bda0; color: #0e2020; font-weight: bold; }
button.primary:hover { background: #60d3ba; }
button.danger { background: #3c2630; border-color: #79414e; color: #ffb1b1; font-weight: bold; }
button.danger:hover { background: #59333e; }
button:disabled { opacity: 0.45; }
entry, textview, textview text { background: #111b29; color: #edf2f8; }
entry { border: 1px solid #354963; border-radius: 8px; padding: 10px; }
textview { padding: 10px; }
scrolledwindow { border: 1px solid #2a384d; border-radius: 8px; }
.warning { color: #ffca80; }
.status { color: #98dacb; }
dialog { background: #101722; }
combobox button { padding: 8px; }
"""

_COMMAND_PLACEHOLDER = 'Or type: “Open Notes and create a shopping list”'
_COMMAND_HELP = "To send keyboard shortcuts or scroll, use the microphone and switch to the target app before speaking."
_ANSWER_HELP = "Answer the task question, or cancel and give a new command"


def _label(text: str = "", style: str | None = None, wrap: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    label.set_line_wrap(wrap)
    if style:
        label.get_style_context().add_class(style)
    return label


class JevWindow(Gtk.Window):
    def __init__(self, controller: Any, backend: Any, config: dict[str, Any]) -> None:
        super().__init__(title="Jev Linux")
        self.controller = controller
        self.backend = backend
        self.config = config
        self._closing = False
        self._listening = False
        self._voice_session = 0
        self._confirmation_token = None
        self._log_lines: list[str] = []
        self._memory_enabled = bool(config.get("memory_enabled", True))
        self._memory_error = ""
        self._memory_dialog = None
        self._settings_dialog = None
        self._quit_source = None
        self._memory_generation = 0
        self._memory_status_generation = 0
        self._memory_busy = False
        self._memory_refresh_again = False
        self._control_request_busy = False
        self._permission_device = ""
        self._permission_task_id = None
        self._task_goal = ""
        self._task_steps = []
        self._task_unsupported = []
        self._task_current = 0
        self._task_total = 0
        self._task_status = "idle"
        self._task_reason = ""
        self.set_default_size(720, 740)
        self.set_size_request(570, 610)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("delete-event", self._close)
        self.connect("key-press-event", self._key_press)
        css = Gtk.CssProvider()
        css.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_border_width(24)
        self.add(root)

        header = Gtk.Box(spacing=12)
        title_block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        title_block.pack_start(_label("Jev Linux", "title"), False, False, 0)
        title_block.pack_start(_label("Speak a task. Let your desktop get to work.", "subtitle"), False, False, 0)
        header.pack_start(title_block, True, True, 0)
        settings = Gtk.Button(label="Settings")
        settings.connect("clicked", self._settings)
        settings.set_valign(Gtk.Align.CENTER)
        header.pack_end(settings, False, False, 0)
        self.memory_button = Gtk.Button(label="Memory on" if self._memory_enabled else "Memory off")
        self.memory_button.set_tooltip_text("Task history and preferences are saved locally. Click to review or clear them.")
        self.memory_button.set_valign(Gtk.Align.CENTER)
        self.memory_button.connect("clicked", self._schedule_memory)
        header.pack_end(self.memory_button, False, False, 0)
        root.pack_start(header, False, False, 0)

        self.provider_label = _label(self._provider_text(), "badge", wrap=True)
        root.pack_start(self.provider_label, False, False, 0)

        voice_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        voice_card.get_style_context().add_class("card")
        voice_card.pack_start(_label("MICROPHONE", "section"), False, False, 0)
        self.speech_label = _label(speech_model_name(self.config), "subtitle", wrap=True)
        voice_card.pack_start(self.speech_label, False, False, 0)
        self.status_label = _label("Microphone off · ready when you are", "status", wrap=True)
        voice_card.pack_start(self.status_label, False, False, 0)
        controls = Gtk.Box(spacing=12)
        self.mic_button = Gtk.ToggleButton(label="Start listening")
        self.mic_button.set_tooltip_text("For keyboard shortcuts or scrolling, start listening, then click the app you want to control before speaking.")
        self.mic_button.get_style_context().add_class("primary")
        self.mic_button.connect("toggled", self._toggle_microphone)
        controls.pack_start(self.mic_button, True, True, 0)
        stop = Gtk.Button(label="■  Stop / cancel")
        stop.get_style_context().add_class("danger")
        stop.connect("clicked", self._cancel)
        controls.pack_start(stop, True, True, 0)
        voice_card.pack_start(controls, False, False, 0)
        voice_card.pack_start(
            _label("Speech is recognized locally. Commands and visible control labels go to your selected AI provider.", "subtitle", wrap=True),
            False, False, 0,
        )
        root.pack_start(voice_card, False, False, 0)

        command_row = Gtk.Box(spacing=10)
        self.command_entry = Gtk.Entry()
        self.command_entry.set_placeholder_text(_COMMAND_PLACEHOLDER)
        self.command_entry.set_tooltip_text(_COMMAND_HELP)
        self.command_entry.connect("activate", self._send_command)
        command_row.pack_start(self.command_entry, True, True, 0)
        send = Gtk.Button(label="Run")
        send.connect("clicked", self._send_command)
        command_row.pack_start(send, False, False, 0)
        root.pack_start(command_row, False, False, 0)

        transcript_header = Gtk.Box(spacing=10)
        transcript_header.pack_start(_label("LIVE TRANSCRIPT", "section"), True, True, 0)
        self.confidence_label = _label("", "subtitle")
        transcript_header.pack_end(self.confidence_label, False, False, 0)
        root.pack_start(transcript_header, False, False, 0)
        self.transcript_label = _label("Your words will appear here as you speak.", wrap=True)
        self.transcript_label.set_selectable(True)
        self.transcript_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.transcript_label.set_lines(3)
        self.transcript_label.set_size_request(-1, 48)
        root.pack_start(self.transcript_label, False, False, 0)

        self.task_expander = Gtk.Expander(label="Current task")
        task_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        task_box.set_margin_top(8)
        self.task_goal_label = _label("No task is running.", wrap=True)
        self.task_state_label = _label("", "subtitle", wrap=True)
        task_box.pack_start(self.task_goal_label, False, False, 0)
        task_box.pack_start(self.task_state_label, False, False, 0)
        self.task_steps_view = Gtk.TextView()
        self.task_steps_view.set_editable(False)
        self.task_steps_view.set_cursor_visible(False)
        self.task_steps_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.task_steps_view.set_can_focus(False)
        task_scroll = Gtk.ScrolledWindow()
        task_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        task_scroll.set_min_content_height(72)
        task_scroll.set_max_content_height(128)
        task_scroll.set_propagate_natural_height(True)
        task_scroll.add(self.task_steps_view)
        task_box.pack_start(task_scroll, False, False, 0)
        self.task_reason_label = _label("", "warning", wrap=True)
        task_box.pack_start(self.task_reason_label, False, False, 0)
        task_buttons = Gtk.Box(spacing=10)
        self.resume_task_button = Gtk.Button(label="Continue task")
        self.resume_task_button.set_sensitive(False)
        self.resume_task_button.set_tooltip_text("Continue from the next unfinished step. Pending sensitive actions still require approval.")
        self.resume_task_button.connect("clicked", self._resume_task)
        task_buttons.pack_start(self.resume_task_button, False, False, 0)
        task_box.pack_start(task_buttons, False, False, 0)
        self.task_expander.add(task_box)
        root.pack_start(self.task_expander, False, False, 0)

        self.confirmation = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.confirmation.get_style_context().add_class("card")
        self.confirmation.set_no_show_all(True)
        self.confirmation_label = _label("", "warning", wrap=True)
        self.confirmation.pack_start(self.confirmation_label, False, False, 0)
        confirmation_buttons = Gtk.Box(spacing=10)
        self.approve_button = Gtk.Button(label="Approve action")
        self.approve_button.get_style_context().add_class("primary")
        self.approve_button.connect("clicked", self._confirm)
        confirmation_buttons.pack_start(self.approve_button, False, False, 0)
        reject = Gtk.Button(label="Cancel action")
        reject.connect("clicked", self._cancel)
        confirmation_buttons.pack_start(reject, False, False, 0)
        self.confirmation.pack_start(confirmation_buttons, False, False, 0)
        root.pack_start(self.confirmation, False, False, 0)

        activity_header = Gtk.Box(spacing=10)
        activity_header.pack_start(_label("ACTIVITY", "section"), True, True, 0)
        self.latency_label = _label("", "subtitle")
        activity_header.pack_end(self.latency_label, False, False, 0)
        root.pack_start(activity_header, False, False, 0)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_can_focus(False)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(105)
        scroll.add(self.log_view)
        root.pack_start(scroll, True, True, 0)

        footer = Gtk.Box(spacing=10)
        self.control_button = Gtk.Button(label="Allow desktop control")
        self.control_button.connect("clicked", self._request_control)
        footer.pack_start(self.control_button, False, False, 0)
        footer.pack_end(_label("In this window: Esc cancels · Ctrl+L toggles mic", "subtitle"), False, False, 0)
        root.pack_end(footer, False, False, 0)
        self.voice = self._create_voice()
        self.controller.set_event_handler(self._receive_event)
        self._refresh_memory_status()
        self._append_log("Ready. Microphone stays off until you start listening.")

    def _provider_text(self) -> str:
        if self.config.get("provider", "openrouter") == "openrouter":
            return f"Jev via OpenRouter  ·  {self.config.get('openrouter_model', 'Select model in Settings')}"
        return "Jev  ·  TypeSafe API"

    def _redact(self, message: Any) -> str:
        text = str(message)
        for name in ("openrouter_api_key", "typesafe_api_key"):
            secret = self.config.get(name)
            if secret:
                text = text.replace(str(secret), "[hidden key]")
        return text

    def _create_voice(self):
        return create_voice_recognizer(
            self.config,
            self._receive_transcript,
            self._receive_voice_status,
        )

    def _receive_transcript(self, text: str, final: bool) -> None:
        session = self._voice_session
        GLib.idle_add(self._handle_transcript, text, final, session)

    def _handle_transcript(self, text: str, final: bool, session: int) -> bool:
        if not self._closing and self._listening and session == self._voice_session:
            self.transcript_label.set_text(self._redact(text))
            control_phrase = re.sub(r"[.!?]+$", "", text.strip()).strip().casefold()
            if control_phrase == "stop listening":
                self._cancel()
                return False
            self._submit(text, final)
            if final and control_phrase in {"confirm", "confirm action"}:
                self.confirmation.hide()
        return False

    def _receive_voice_status(self, message: str) -> None:
        session = self._voice_session
        GLib.idle_add(self._handle_voice_status, message, session)

    def _handle_voice_status(self, message: str, session: int) -> bool:
        if self._closing or session != self._voice_session:
            return False
        self.status_label.set_text(self._redact(message))
        if message.startswith("Error:"):
            self._append_log(message)
            self._listening = False
            self.mic_button.handler_block_by_func(self._toggle_microphone)
            self.mic_button.set_active(False)
            self.mic_button.set_label("Start listening")
            self.mic_button.handler_unblock_by_func(self._toggle_microphone)
            self._cancel_controller()
        return False

    def _toggle_microphone(self, button: Gtk.ToggleButton) -> None:
        active = button.get_active()
        if active:
            self._capture_launch_context()
            self._voice_session += 1
            self._listening = True
            button.set_label("Listening · click to turn off")
            if not self.voice.start():
                self._listening = False
                button.handler_block_by_func(self._toggle_microphone)
                button.set_active(False)
                button.set_label("Start listening")
                button.handler_unblock_by_func(self._toggle_microphone)
        else:
            self._listening = False
            self._voice_session += 1
            self.voice.stop()
            button.set_label("Start listening")
            self.confirmation.hide()
            self._cancel_controller()

    def _submit(self, text: str, final: bool) -> None:
        try:
            self.controller.submit(text, final=final)
        except Exception as exc:
            self._append_log(f"Could not start command: {exc}")

    def _capture_launch_context(self) -> None:
        try:
            if hasattr(self.backend, "capture_launch_context"):
                self.backend.capture_launch_context(Gtk.get_current_event_time())
        except Exception:
            # The command still works when desktop activation is unavailable.
            pass

    def _send_command(self, _widget: Any) -> None:
        command = self.command_entry.get_text().strip()
        if not command:
            return
        self._capture_launch_context()
        self.command_entry.set_text("")
        self.transcript_label.set_text(self._redact(command))
        self._submit(command, True)
        if command.lower() in {"confirm", "confirm action"}:
            self.confirmation.hide()

    def _cancel(self, _widget: Any = None) -> None:
        self._listening = False
        self._voice_session += 1
        self.voice.stop()
        self.mic_button.handler_block_by_func(self._toggle_microphone)
        self.mic_button.set_active(False)
        self.mic_button.set_label("Start listening")
        self.mic_button.handler_unblock_by_func(self._toggle_microphone)
        self.confirmation.hide()
        self._confirmation_token = None
        self._cancel_controller()

    def _cancel_controller(self) -> None:
        try:
            self.controller.cancel()
        except Exception as exc:
            self._append_log(f"Cancel failed: {exc}")

    def _confirm(self, _widget: Any) -> None:
        if not self.approve_button.get_sensitive():
            return
        self.confirmation.hide()
        try:
            self.controller.confirm()
        except Exception as exc:
            self._append_log(f"Could not approve action: {exc}")

    def _receive_event(self, event: dict[str, Any]) -> None:
        GLib.idle_add(self._handle_event, dict(event))

    def _handle_event(self, event: dict[str, Any]) -> bool:
        if self._closing:
            return False
        event_type = event.get("type", "status")
        message = self._redact(event.get("message", event.get("text", "")))
        latency = event.get("latency_ms")
        if latency is not None:
            try:
                self.latency_label.set_text(f"AI response {float(latency):,.0f} ms")
            except (ValueError, TypeError):
                pass
        confidence = event.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
                self.confidence_label.set_text(f"Confidence {confidence:.0%}" if confidence <= 1 else f"Confidence {confidence:.0f}%")
            except (ValueError, TypeError):
                pass
        if event_type == "transcript":
            self.transcript_label.set_text(message)
        elif event_type == "confirmation_cancelled":
            self.confirmation.hide()
            self._confirmation_token = None
        elif event_type == "confirmation":
            self._confirmation_token = event.get("id")
            requires_target_focus = bool(event.get("requires_target_focus"))
            self.approve_button.set_sensitive(not requires_target_focus)
            confirmation_message = message or "This action needs your approval."
            if requires_target_focus:
                confirmation_message += '\n\nKeep the target app focused and say “confirm”. Start the microphone first if it is off.'
            self.confirmation_label.set_text(confirmation_message)
            self.confirmation.set_no_show_all(False)
            self.confirmation.show_all()
            self.confirmation.set_no_show_all(True)
            self._append_log(message)
            self._render_task_progress()
        elif event_type in {"task_plan", "task_progress"}:
            self._update_task_progress(event)
            if message:
                self._append_log(message)
        elif event_type == "desktop_permission":
            self._permission_device = str(event.get("device", ""))
            self._permission_task_id = event.get("task_id")
            self._task_status = "awaiting_permission"
            self._task_reason = message or "This step needs permission to control the desktop."
            self.status_label.set_text(self._task_reason)
            self._render_task_progress()
            if message:
                self._append_log(message)
            self._request_control(None, expected_task_id=self._permission_task_id)
        elif event_type == "memory":
            self._update_memory_status(
                bool(event.get("enabled", self.config.get("memory_enabled", True))),
                str(event.get("error", "")),
            )
            if self._memory_dialog is not None:
                self._refresh_memory_dialog()
            if message:
                self._append_log(message)
        else:
            if event_type == "status" and message == "Stopped. No queued actions.":
                self.confirmation.hide()
                self._permission_task_id = None
                self._permission_device = ""
                if self._task_status not in {"idle", "completed", "cancelled"}:
                    self._task_status = "cancelled"
                    self._task_reason = "Task cancelled."
                    self._render_task_progress()
            if event_type in {"status", "error"} and message:
                self.status_label.set_text(message)
                if self._task_status != "awaiting_permission":
                    self._permission_task_id = None
                    self._permission_device = ""
            if message:
                self._append_log(message)
        return False

    def _update_task_progress(self, event: dict[str, Any]) -> None:
        status = str(event.get("status", "in_progress")).strip().lower().replace(" ", "_")
        if event.get("type") == "task_plan" or status != "awaiting_permission":
            self._permission_task_id = None
            self._permission_device = ""
        if event.get("type") == "task_plan":
            plan = event.get("plan", {})
            if not isinstance(plan, dict):
                return
            self._task_goal = self._redact(plan.get("goal", event.get("goal", "")))
            steps = plan.get("steps", [])
            self._task_steps = [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []
            unsupported = plan.get("unsupported", [])
            self._task_unsupported = unsupported if isinstance(unsupported, list) else []
            self._task_total = len(self._task_steps)
            self._task_current = 0
        else:
            if status == "planning":
                self._task_steps = []
                self._task_unsupported = []
                self._task_total = 0
                self._task_current = 0
            if "goal" in event:
                self._task_goal = self._redact(event["goal"])
        try:
            self._task_total = max(0, int(event.get("total_steps", self._task_total)))
            self._task_current = min(self._task_total, max(0, int(event.get("current_step", self._task_current))))
        except (ValueError, TypeError):
            pass
        if event.get("step_instruction") and self._task_current < len(self._task_steps):
            self._task_steps[self._task_current] = {
                **self._task_steps[self._task_current],
                "instruction": event["step_instruction"],
            }
        if event.get("step_kind") and self._task_current < len(self._task_steps):
            self._task_steps[self._task_current] = {
                **self._task_steps[self._task_current],
                "kind": event["step_kind"],
            }
        self._task_status = status
        self._task_reason = self._redact(event.get("message", ""))
        self._render_task_progress()

    def _needs_task_answer(self) -> bool:
        return bool(
            self._task_status == "needs_details"
            and self._task_current < len(self._task_steps)
            and self._task_steps[self._task_current].get("kind") == "clarify"
        )

    def _render_task_progress(self) -> None:
        state_names = {
            "idle": "Ready", "planning": "Planning", "in_progress": "Working",
            "paused": "Paused", "blocked": "Blocked", "needs_details": "More detail needed",
            "failed": "Step failed", "completed": "Completed", "cancelled": "Cancelled",
            "awaiting_confirmation": "Awaiting approval",
            "awaiting_permission": "Awaiting desktop permission",
        }
        state = state_names.get(self._task_status, self._task_status.replace("_", " ").capitalize())
        count = f"Completed {self._task_current} of {self._task_total}"
        if self._task_status == "completed" and self._task_unsupported:
            state = "More help needed"
        summary = f"{count} · {state}" if self._task_total else state
        self.task_expander.set_label("Current task" if self._task_status == "idle" else f"Current task · {summary}")
        self.task_expander.set_tooltip_text(self._task_reason or self._task_goal or "Review the steps for the current task.")
        self.task_goal_label.set_text(self._task_goal[:300] or "No task is running.")
        self.task_state_label.set_text(summary if self._task_status != "idle" else "")
        lines = []
        for index, step in enumerate(self._task_steps[:60]):
            if index < self._task_current:
                marker = "✓"
            elif index == self._task_current and self._task_status not in {"completed", "cancelled"}:
                marker = "!" if self._task_status in {"paused", "blocked", "failed", "needs_details", "awaiting_permission"} else "→"
            else:
                marker = "○"
            instruction = self._redact(step.get("instruction", ""))[:2000]
            if step.get("kind") == "clarify":
                instruction = f"Question: {instruction}"
            lines.append(f"{marker} {index + 1}. {instruction}")
            expected = self._redact(step.get("expected_outcome", ""))[:1000]
            if expected:
                lines.append(f"   Expected: {expected}")
        if len(self._task_steps) > 60:
            lines.append(f"… {len(self._task_steps) - 60} more planned steps")
        if self._task_unsupported:
            lines.extend(("", "Still needed:"))
            for item in self._task_unsupported[:20]:
                text = item.get("instruction", item.get("reason", "")) if isinstance(item, dict) else item
                lines.append(f"○ {self._redact(text)[:1000]}")
        placeholder = "Preparing the task steps…" if self._task_status == "planning" else "Task steps will appear here."
        self.task_steps_view.get_buffer().set_text("\n".join(lines).strip() or placeholder)
        needs_answer = self._needs_task_answer()
        self.command_entry.set_placeholder_text(_ANSWER_HELP if needs_answer else _COMMAND_PLACEHOLDER)
        self.command_entry.set_tooltip_text(_ANSWER_HELP if needs_answer else _COMMAND_HELP)
        reason = self._task_reason[:600]
        if needs_answer:
            reason = f"{reason}\n{_ANSWER_HELP}" if reason else _ANSWER_HELP
        self.task_reason_label.set_text(reason)
        self.resume_task_button.set_tooltip_text(
            "Answer the task question to continue." if needs_answer else
            "Allow desktop control to continue this step." if self._task_status == "awaiting_permission" else
            "Continue from the next unfinished step. Pending sensitive actions still require approval."
        )
        self.resume_task_button.set_sensitive(
            self._task_status in {"paused", "blocked", "needs_details", "failed"}
            and not needs_answer
            and not self.confirmation.get_visible()
        )

    def _resume_task(self, _widget: Any) -> None:
        if self._closing or not self.resume_task_button.get_sensitive():
            return
        self._capture_launch_context()
        previous_status = self._task_status
        previous_reason = self._task_reason
        self._task_status = "in_progress"
        self._task_reason = "Continuing from the next unfinished step…"
        self._render_task_progress()
        try:
            if self.controller.resume_task() is False:
                self._task_status = previous_status
                self._task_reason = previous_reason or "Task is still paused. Add the missing details before continuing."
                self._render_task_progress()
        except Exception as exc:
            self._task_status = previous_status
            self._task_reason = self._redact(f"Could not continue task: {exc}")
            self._render_task_progress()

    def _append_log(self, message: str) -> None:
        self._log_lines.append(self._redact(message))
        self._log_lines = self._log_lines[-100:]
        buffer = self.log_view.get_buffer()
        buffer.set_text("\n\n".join(self._log_lines))
        end = buffer.get_end_iter()
        self.log_view.scroll_to_iter(end, 0, False, 0, 1)

    def _request_control(self, _widget: Any, expected_task_id: Any = None) -> None:
        if self._closing or self._control_request_busy:
            return
        permission_task_id = expected_task_id
        if permission_task_id is None and self._task_status == "awaiting_permission":
            permission_task_id = self._permission_task_id
        self._control_request_busy = True
        self.control_button.set_sensitive(False)
        self._append_log("Requesting permission to control the desktop…")

        def worker() -> None:
            granted = False
            try:
                result = self.backend.request_control()
                granted = result is not False
                message = "Desktop control is ready." if granted else "Desktop control was not granted. Use Allow desktop control to try again."
            except Exception as exc:
                message = f"Could not enable desktop control: {exc}"
            GLib.idle_add(done, message, granted)

        def done(message: str, granted: bool) -> bool:
            self._control_request_busy = False
            if not self._closing:
                self.control_button.set_sensitive(True)
                if granted:
                    self._capture_launch_context()
                if granted and permission_task_id is not None:
                    try:
                        resumed = self.controller.resume_after_permission(permission_task_id)
                    except Exception as exc:
                        resumed = False
                        message = f"Could not continue after desktop permission: {exc}"
                    else:
                        if resumed:
                            message = "Desktop control granted. Continuing the task…"
                        elif self._task_status == "awaiting_permission":
                            device = self._permission_device if self._permission_device in {"keyboard", "pointer"} else "desktop"
                            message = f"This step is still waiting for {device} permission. Use Allow desktop control to try again."
                if self._task_status == "awaiting_permission":
                    self._task_reason = self._redact(message)
                    self.status_label.set_text(self._task_reason)
                    self._render_task_progress()
                self._append_log(message)
            return False

        threading.Thread(target=worker, name="jev-desktop-permission", daemon=True).start()

    def _update_memory_status(self, enabled: bool, error: str = "") -> None:
        self._memory_status_generation += 1
        self._memory_enabled = enabled
        self._memory_error = self._redact(error)
        self.memory_button.set_label("Memory unavailable" if error else "Memory on" if enabled else "Memory off")
        self.memory_button.set_tooltip_text(
            self._memory_error or (
                "Task history and preferences are saved locally. Click to review or clear them."
                if enabled else "Task memory is off. Saved history can be reviewed or cleared here."
            )
        )

    def _refresh_memory_status(self) -> None:
        self._memory_status_generation += 1
        generation = self._memory_status_generation

        def worker() -> None:
            try:
                snapshot = self.controller.memory_snapshot()
                if not isinstance(snapshot, dict):
                    raise RuntimeError("Local memory could not be read.")
                enabled = bool(snapshot.get("enabled", self.config.get("memory_enabled", True)))
                error = str(snapshot.get("error", "") or "")
            except Exception:
                enabled = False
                error = "Local memory is unavailable. Open Memory to review the error."
            GLib.idle_add(done, enabled, error)

        def done(enabled: bool, error: str) -> bool:
            if not self._closing and generation == self._memory_status_generation:
                self._update_memory_status(enabled, error)
            return False

        threading.Thread(target=worker, name="jev-memory-status", daemon=True).start()

    def _schedule_memory(self, _widget: Any) -> None:
        # Return from the AT-SPI/GTK action handler before entering a modal loop.
        GLib.idle_add(self._memory_idle)

    def _memory_idle(self) -> bool:
        if not self._closing:
            self._memory(None)
        return False

    def _memory(self, _widget: Any) -> None:
        if self._memory_dialog is not None:
            self._memory_dialog.present()
            return
        dialog = Gtk.Dialog(title="Jev task memory", transient_for=self, modal=True)
        dialog.set_default_size(660, 540)
        dialog.add_buttons("Refresh", 1, "Clear memory", Gtk.ResponseType.APPLY, "Close", Gtk.ResponseType.CLOSE)
        self._memory_dialog = dialog
        self._memory_generation += 1
        self._memory_busy = False
        self._memory_refresh_again = False
        self._memory_refresh_button = dialog.get_widget_for_response(1)
        self._memory_clear_button = dialog.get_widget_for_response(Gtk.ResponseType.APPLY)
        self._memory_clear_button.get_style_context().add_class("danger")
        content = dialog.get_content_area()
        content.set_border_width(20)
        content.set_spacing(12)
        content.pack_start(_label("Task history and preferences", "title"), False, False, 0)
        content.pack_start(_label(
            "When task memory is on, commands and action history are saved on this computer. Recent context and saved preferences are sent to your AI provider for follow-up tasks. Microphone audio is not saved.",
            "subtitle", wrap=True,
        ), False, False, 0)
        self._memory_state_label = _label("Loading saved memory…", "status", wrap=True)
        self._memory_error_label = _label("", "warning", wrap=True)
        content.pack_start(self._memory_state_label, False, False, 0)
        content.pack_start(self._memory_error_label, False, False, 0)
        notebook = Gtk.Notebook()
        for attribute, name in (("_memory_tasks_view", "Recent tasks"), ("_memory_preferences_view", "Preferences")):
            view = Gtk.TextView()
            view.set_editable(False)
            view.set_cursor_visible(False)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_min_content_height(210)
            scroll.add(view)
            notebook.append_page(scroll, Gtk.Label(label=name))
            setattr(self, attribute, view)
        content.pack_start(notebook, True, True, 0)
        content.pack_start(_label("Change task memory in Settings. Turning it off keeps saved history until you clear it.", "subtitle", wrap=True), False, False, 0)
        dialog.show_all()
        self._refresh_memory_dialog()
        try:
            while True:
                response = dialog.run()
                if response == Gtk.ResponseType.APPLY:
                    self._refresh_memory_dialog(clear=True)
                elif response == 1:
                    self._refresh_memory_dialog()
                else:
                    break
        finally:
            self._memory_generation += 1
            self._memory_dialog = None
            self._memory_busy = False
            self._memory_refresh_again = False
            dialog.destroy()

    def _refresh_memory_dialog(self, *, clear: bool = False) -> None:
        if self._memory_dialog is None or self._closing:
            return
        if self._memory_busy:
            self._memory_refresh_again = True
            return
        generation = self._memory_generation
        self._memory_busy = True
        self._memory_refresh_button.set_sensitive(False)
        self._memory_clear_button.set_sensitive(False)
        self._memory_state_label.set_text("Clearing saved memory…" if clear else "Loading saved memory…")
        self._memory_error_label.set_text("")

        def worker() -> None:
            try:
                if clear:
                    self.controller.clear_memory()
                snapshot = self.controller.memory_snapshot()
                if not isinstance(snapshot, dict):
                    raise RuntimeError("The saved memory could not be read.")
                error = ""
            except Exception as exc:
                snapshot = None
                error = f"Could not {'clear' if clear else 'read'} memory: {exc}"
            GLib.idle_add(self._show_memory_snapshot, generation, snapshot, error)

        threading.Thread(target=worker, name="jev-memory-review", daemon=True).start()

    def _show_memory_snapshot(self, generation: int, snapshot: dict | None, error: str = "") -> bool:
        if self._closing or self._memory_dialog is None or generation != self._memory_generation:
            return False
        self._memory_busy = False
        self._memory_refresh_button.set_sensitive(True)
        self._memory_clear_button.set_sensitive(True)
        if snapshot is None:
            self._memory_error_label.set_text(self._redact(error))
            self._memory_state_label.set_text("Saved memory is unavailable.")
            self._update_memory_status(self._memory_enabled, error)
        else:
            enabled = bool(snapshot.get("enabled", self.config.get("memory_enabled", True)))
            error = str(snapshot.get("error", "") or "")
            self._update_memory_status(enabled, error)
            self._memory_error_label.set_text(self._redact(error))
            tasks = snapshot.get("recent_tasks", [])
            preferences = snapshot.get("preferences", [])
            tasks = tasks if isinstance(tasks, (list, tuple)) else []
            preferences = preferences if isinstance(preferences, (list, tuple)) else []
            self._memory_state_label.set_text(
                f"Memory {'on' if enabled else 'off'} · {len(tasks)} recent task(s) · {len(preferences)} preference(s)"
            )
            self._memory_tasks_view.get_buffer().set_text(self._memory_tasks_text(tasks))
            self._memory_preferences_view.get_buffer().set_text(self._memory_preferences_text(preferences))
        if self._memory_refresh_again:
            self._memory_refresh_again = False
            self._refresh_memory_dialog()
        return False

    def _memory_tasks_text(self, tasks: list) -> str:
        lines = []
        for index, task in enumerate(tasks[:30], 1):
            if not isinstance(task, dict):
                continue
            command = self._redact(task.get("command", "Untitled task"))[:2000]
            status = self._redact(task.get("status", ""))[:100].replace("_", " ")
            lines.append(f"{index}. {command}" + (f"\n   {status}" if status else ""))
            actions = task.get("actions", [])
            if isinstance(actions, (list, tuple)):
                for action in actions[:12]:
                    if isinstance(action, dict):
                        name = self._redact(action.get("action", action.get("operation", ""))).replace("_", " ")[:100]
                        result = self._redact(action.get("summary", action.get("result", "")))[:1000]
                        detail = ": ".join(part for part in (name, result) if part)
                        fields = []
                        for field, label in (("app", "App"), ("url", "URL"), ("note_path", "Note"),
                                             ("text", "Text"), ("key", "Key"), ("direction", "Scroll")):
                            value = action.get(field)
                            if isinstance(value, str) and value:
                                fields.append(f"      {label}: {self._redact(value)[:2000]}")
                        if fields:
                            detail += "\n" + "\n".join(fields)
                    else:
                        detail = self._redact(action)[:500]
                    if detail:
                        lines.append(f"   • {detail}")
                if len(actions) > 12:
                    lines.append(f"   … {len(actions) - 12} more action(s)")
            lines.append("")
        return "\n".join(lines).strip() or "No saved tasks yet."

    def _memory_preferences_text(self, preferences: list) -> str:
        lines = []
        for preference in preferences[:20]:
            text = preference.get("text", "") if isinstance(preference, dict) else preference
            text = self._redact(text).strip()[:2000]
            if text:
                lines.append(f"• {text}")
        return "\n\n".join(lines) or "No saved preferences yet. Say “remember that…” to save a preference."

    def _settings(self, _widget: Any) -> None:
        dialog = Gtk.Dialog(title="Jev settings", transient_for=self, modal=True)
        self._settings_dialog = dialog
        dialog.set_default_size(560, 430)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save settings", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_border_width(20)
        content.set_spacing(10)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        content.pack_start(grid, True, True, 0)
        provider = Gtk.ComboBoxText()
        provider.append("openrouter", "Jev via OpenRouter")
        provider.append("typesafe", "Jev via TypeSafe API")
        provider.set_active_id(self.config.get("provider", "openrouter"))
        model = Gtk.Entry()
        model.set_text(self.config.get("openrouter_model", ""))
        model.set_placeholder_text("Exact OpenRouter model ID")
        router_key = Gtk.Entry()
        router_key.set_visibility(False)
        router_key.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        router_key.set_placeholder_text("Key stored · leave blank to keep" if self.config.get("openrouter_api_key") else "OpenRouter API key")
        direct_key = Gtk.Entry()
        direct_key.set_visibility(False)
        direct_key.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        direct_key.set_placeholder_text("Key stored · leave blank to keep" if self.config.get("typesafe_api_key") else "TypeSafe API key")
        speech_path = Gtk.Entry()
        speech_path.set_text(self.config.get("speech_model_path", ""))
        speech_engine = Gtk.ComboBoxText()
        speech_engine.append("moonshine", "Moonshine · accurate local English")
        speech_engine.append("vosk", "Vosk · original lightweight model")
        speech_engine.set_active_id(self.config.get("speech_engine", "moonshine"))
        speech_quality = Gtk.ComboBoxText()
        speech_quality.append("4", "Small Streaming · less memory")
        speech_quality.append("5", "Medium Streaming · higher accuracy, more memory")
        speech_quality.set_active_id(str(self.config.get("speech_model_arch", 4)))
        memory_enabled = Gtk.CheckButton(label="Save task history and preferences locally")
        memory_enabled.set_active(bool(self.config.get("memory_enabled", True)))
        planning_enabled = Gtk.CheckButton(label="Break longer tasks into steps")
        planning_enabled.set_active(bool(self.config.get("planning_enabled", True)))
        planner_model = Gtk.Entry()
        planner_model.set_text(self.config.get("planner_model", ""))
        planner_model.set_placeholder_text("Model for task planning")
        for row, (name, widget) in enumerate([
            ("AI provider", provider), ("OpenRouter model", model),
            ("OpenRouter key", router_key), ("TypeSafe key", direct_key),
            ("Speech recognition", speech_engine), ("English speech model", speech_quality),
            ("Moonshine model folder", speech_path),
            ("Task memory", memory_enabled),
            ("Task planning", planning_enabled),
            ("Planning model", planner_model),
        ]):
            grid.attach(_label(name), 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
        content.pack_start(_label("Keys are stored only in your private local configuration. Blank key fields keep the saved key.", "subtitle", wrap=True), False, False, 0)
        content.pack_start(_label("When task memory is on, recent context and saved preferences are sent to your AI provider for follow-ups. Audio is not saved.", "subtitle", wrap=True), False, False, 0)
        error_label = _label("", "warning", wrap=True)
        content.pack_start(error_label, False, False, 0)
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            updated = dict(self.config)
            selected_arch = int(speech_quality.get_active_id() or "4")
            model_folder = speech_path.get_text().strip()
            if selected_arch != int(self.config.get("speech_model_arch", 4)):
                size = "medium" if selected_arch == 5 else "small"
                model_folder = str(DATA_DIR / f"moonshine/download.moonshine.ai/model/{size}-streaming-en/quantized_26_08_21")
            updated.update({
                "provider": provider.get_active_id() or "openrouter",
                "openrouter_model": model.get_text().strip(),
                "speech_model_path": model_folder,
                "speech_engine": speech_engine.get_active_id() or "moonshine",
                "speech_model_arch": selected_arch,
                "memory_enabled": memory_enabled.get_active(),
                "planning_enabled": planning_enabled.get_active(),
                "planner_model": planner_model.get_text().strip(),
            })
            if router_key.get_text().strip():
                updated["openrouter_api_key"] = router_key.get_text().strip()
            if direct_key.get_text().strip():
                updated["typesafe_api_key"] = direct_key.get_text().strip()
            try:
                from .config import save_config
                save_config(updated)
                self._cancel()
                self.config.clear()
                self.config.update(updated)
                self.voice = self._create_voice()
                self.speech_label.set_text(speech_model_name(self.config))
                self.provider_label.set_text(self._provider_text())
                self._update_memory_status(bool(self.config.get("memory_enabled", True)))
                if hasattr(self.controller, "reconfigure"):
                    self.controller.reconfigure(self.config)
                self._refresh_memory_status()
                self._append_log("Settings saved. Microphone is off.")
                break
            except Exception as exc:
                error_label.set_text(self._redact(f"Could not save settings: {exc}"))
        router_key.set_text("")
        direct_key.set_text("")
        self._settings_dialog = None
        dialog.destroy()

    def _key_press(self, _widget: Any, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._cancel()
            return True
        if event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self.mic_button.set_active(not self.mic_button.get_active())
            return True
        return False

    def _close(self, *_args: Any) -> bool:
        if not self._closing:
            self._closing = True
            self._listening = False
            self._voice_session += 1
            self.voice.stop()
            if self._memory_dialog is not None:
                self._memory_dialog.response(Gtk.ResponseType.CLOSE)
            if self._settings_dialog is not None:
                self._settings_dialog.response(Gtk.ResponseType.CLOSE)
            try:
                self.controller.close()
            finally:
                if Gtk.main_level() > 0:
                    Gtk.main_quit()
                    self._quit_source = GLib.timeout_add(10, self._quit_main_loops)
        return False

    def _quit_main_loops(self) -> bool:
        if Gtk.main_level() > 0:
            Gtk.main_quit()
            return True
        self._quit_source = None
        return False


def launch_ui(controller: Any, backend: Any, config: dict[str, Any]) -> None:
    """Run the native desktop UI; no microphone capture begins automatically."""
    initialized, _ = Gtk.init_check(None)
    if not initialized:
        raise RuntimeError("Cannot open Jev Linux: no graphical Ubuntu session is available.")
    window = JevWindow(controller, backend, config)
    signal_sources = [GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, lambda: (window._close(), False)[1]) for signum in (signal.SIGTERM, signal.SIGINT)]
    window.show_all()
    try:
        Gtk.main()
    finally:
        window._close()
        window.destroy()
        if window._quit_source is not None and GLib.MainContext.default().find_source_by_id(window._quit_source):
            GLib.source_remove(window._quit_source)
        for source in signal_sources:
            if GLib.MainContext.default().find_source_by_id(source):
                GLib.source_remove(source)
