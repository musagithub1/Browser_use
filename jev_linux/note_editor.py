"""Guarded native refresh of a note changed by the current Jev task."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import time

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

from .desktop import DesktopError
from .notes import read_note


_BANNER = "File Has Changed on Disk"
_RELOAD = "Discard Changes and Reload"


class NoteEditorTreePending(DesktopError):
    """The native editor tree is temporarily incomplete while rendering."""


@dataclass
class _Document:
    window: object
    body: object
    button: object | None
    banner: bool
    alerts: tuple = ()


def _showing(node):
    node.clear_cache()
    states = node.get_state_set()
    if states is None or states.contains(Atspi.StateType.DEFUNCT):
        raise NoteEditorTreePending("Text Editor's document controls are being refreshed.")
    return states.contains(Atspi.StateType.SHOWING)


def _is_editor(app):
    """Identify the installed GNOME editor, never a generic similarly named app."""
    attributes = app.get_attributes() or {}
    if any(attributes.get(key) in {"org.gnome.TextEditor", "org.gnome.TextEditor.desktop"}
           for key in ("application-id", "desktop-id", "gtk-application-id")):
        return True
    try:
        executable = Path(f"/proc/{int(app.get_process_id())}/exe").resolve(strict=True)
        return executable.name == "gnome-text-editor"
    except (OSError, TypeError, ValueError):
        return False


def _nodes(window):
    stack, seen = [window], set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if len(seen) > 500:
            raise DesktopError("Text Editor's document could not be checked completely.")
        if not _showing(node):
            continue
        yield node
        # GNOME restores many background tabs. Their labels/dirty states do
        # not identify the visible document and must not be inspected here.
        if node.get_role_name() == "page tab list":
            continue
        count = node.get_child_count()
        if count < 0 or count > 250:
            raise NoteEditorTreePending("Text Editor's document controls could not be checked completely.")
        for index in range(count - 1, -1, -1):
            child = node.get_child_at_index(index)
            if child is not None:
                stack.append(child)


def _document(desktop, path):
    """Require one visible filename and one body in its editor window."""
    matches = []
    desktop.clear_cache()
    app_count = desktop.get_child_count()
    if not 0 <= app_count <= 100:
        raise NoteEditorTreePending("The editor application could not be identified safely.")
    for index in range(app_count):
        app = desktop.get_child_at_index(index)
        if app is None:
            continue
        app.clear_cache()
        if not _is_editor(app):
            continue
        count = app.get_child_count()
        if not 0 <= count <= 30:
            raise NoteEditorTreePending("Text Editor's windows could not be identified safely.")
        for window_index in range(count):
            window = app.get_child_at_index(window_index)
            if window is None or not _showing(window):
                continue
            nodes = list(_nodes(window))
            names = [node.get_name() or "" for node in nodes]
            # A tab/window title may expose the basename. It must be exact;
            # substring matching could select an unrelated or unsaved note.
            if path.name not in names:
                continue
            folders = {str(path.parent)}
            home = Path.home()
            if path.parent.is_relative_to(home):
                relative = path.parent.relative_to(home)
                folders.add("~" if str(relative) == "." else "~/" + str(relative))
            exposed_folders = [name for node, name in zip(nodes, names)
                               if node.get_role_name() == "label"
                               and (name.startswith("/") or name.startswith("~/"))]
            if any(folder not in folders for folder in exposed_folders):
                raise DesktopError("Text Editor's folder association does not match the saved note.")
            bodies, buttons, alerts = [], [], []
            for node, name in zip(nodes, names):
                states = node.get_state_set()
                attributes = node.get_attributes() or {}
                if (name in {"Unsaved Changes", "Modified"}
                        or name.strip("*• ") == path.name and name != path.name
                        or name.startswith(("*" + path.name, "•" + path.name,
                                            "* " + path.name, "• " + path.name))
                        or str(attributes.get("modified", "")).casefold() == "true"):
                    raise DesktopError("The note has unsaved editor changes; reload it manually.")
                # Some editor versions expose the full file URI/path. When
                # available, require that association to match as well.
                for key in ("file", "filename", "file-uri", "uri"):
                    value = attributes.get(key)
                    if value and (str(value).startswith("/") or str(value).startswith("file://")):
                        if str(value) not in {str(path), path.as_uri()}:
                            raise DesktopError("Text Editor's file association does not match the saved note.")
                if (states.contains(Atspi.StateType.EDITABLE)
                        and "Text" in node.get_interfaces()
                        and node.get_role_name() in {"text", "entry", "document text"}):
                    bodies.append(node)
                if name == _RELOAD and node.get_role_name() in {"push button", "button"}:
                    buttons.append(node)
                if node.get_role_name() == "alert":
                    alerts.append(node)
            if not bodies:
                raise NoteEditorTreePending("The note's visible editor body is being refreshed.")
            if len(bodies) > 1 or len(buttons) > 1:
                raise DesktopError("The note's visible body or reload control is ambiguous.")
            matches.append(_Document(window, bodies[0], buttons[0] if buttons else None,
                                     _BANNER in names, tuple(alerts)))
    if len(matches) > 1:
        raise DesktopError("More than one visible Text Editor document matches this note.")
    return matches[0] if matches else None


def _body_text(document):
    if not _showing(document.body):
        raise DesktopError("The note's editor body is no longer visible.")
    interface = document.body.get_text_iface()
    native = isinstance(interface, Atspi.Accessible)
    count = (Atspi.Text.get_character_count(interface) if native
             else interface.get_character_count())
    if count < 0:
        raise NoteEditorTreePending("Text Editor's note buffer is being refreshed.")
    if count > 1000001:
        raise DesktopError("The note's full editor content could not be checked.")
    # Accessible.get_text aliases get_text_iface in PyGI; use the Text
    # interface's static method to avoid that name collision on real nodes.
    value = (Atspi.Text.get_text(interface, 0, count) if native
             else interface.get_text(0, count))
    if not isinstance(value, str) or len(value) != count:
        raise NoteEditorTreePending("Text Editor did not expose the complete note buffer.")
    return value


def _same_buffer(actual, expected):
    # GtkTextView may add or omit one final paragraph separator. Permit that
    # convention, but never discard arbitrary whitespace or other edits.
    return (actual == expected or actual == expected + "\n"
            or actual + "\n" == expected)


def _disk_matches(path, expected, notes_directory):
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise DesktopError("The saved note's path changed before editor refresh.")
    current = read_note(path, notes_directory, max_chars=1000000)
    if (current["path"] != str(path) or current["truncated"]
            or current["revision"] != expected["revision"]
            or current["content"] != expected["content"]):
        raise DesktopError("The saved note changed again; read it before refreshing the editor.")


def _confirmation(document, path):
    if not document.alerts:
        return None
    if (len(document.alerts) != 1 or not _showing(document.alerts[0])
            or document.alerts[0].get_name() != f"Discard Changes to “{path.name}”?"):
        raise DesktopError("An unfamiliar Text Editor dialog is open; resolve it manually.")
    alert = document.alerts[0]
    buttons = [node for node in _nodes(alert)
               if node.get_role_name() in {"push button", "button"}]
    if len(buttons) != 2 or {node.get_name() for node in buttons} != {"Cancel", "Discard"}:
        raise DesktopError("The note's reload confirmation controls are ambiguous.")
    return alert, next(node for node in buttons if node.get_name() == "Discard")


def _activate_button(button, label):
    if not _showing(button) or button.get_name() != label:
        raise DesktopError("The note's native reload control changed before refresh.")
    # GTK4 exposes SENSITIVE for usable buttons but omits ENABLED even on
    # these live controls. Match the native click backend's semantics.
    if not button.get_state_set().contains(Atspi.StateType.SENSITIVE):
        raise DesktopError("The note's native reload button is not available.")
    action = button.get_action_iface()
    if action is None or action.get_n_actions() != 1:
        raise DesktopError("The note's native reload action is ambiguous.")
    if not action.do_action(0):
        raise DesktopError("Text Editor rejected the native reload action.")


def verify_note_visible_steps(path, snapshot, notes_directory, *, desktop=None,
                              clock=time.monotonic):
    """Read-only proof that this exact saved note is visible in Text Editor.

    Opening a file can return before GTK exposes its document. Yield while
    waiting so the dispatcher can process native events and revoke its lease.
    No focus, reload, or confirmation action is performed by this check.
    """
    path = Path(path)
    if (snapshot.get("truncated") is not False or snapshot.get("path") != str(path)
            or not isinstance(snapshot.get("content"), str)
            or snapshot.get("revision") != hashlib.sha256(snapshot["content"].encode("utf-8")).hexdigest()):
        raise DesktopError("Full saved-note evidence is required to verify its visible editor content.")
    _disk_matches(path, snapshot, notes_directory)
    desktop = desktop if desktop is not None else Atspi.get_desktop(0)
    deadline = clock() + 3
    while True:
        _disk_matches(path, snapshot, notes_directory)
        try:
            document = _document(desktop, path)
            if document is not None:
                if document.alerts:
                    raise DesktopError("A Text Editor dialog is open; resolve it manually before verifying the note.")
                if _same_buffer(_body_text(document), snapshot["content"]):
                    _disk_matches(path, snapshot, notes_directory)
                    return True
        except (NoteEditorTreePending, GLib.Error):
            # Native children may be replaced after opening. Only incomplete
            # reads retry; ambiguity, dirty buffers and mismatched files fail.
            pass
        if clock() >= deadline:
            raise DesktopError("The saved note's exact content is not visible in Text Editor yet; open it, then continue.")
        yield .05


def refresh_note_steps(path, before, after, notes_directory, *, desktop=None,
                       clock=time.monotonic):
    """Invoke the native reload once, only when discarding loses no user edits.

    Each yield lets GTK process launch and editor events. The desktop adapter
    rechecks its current-task lease before every resumed phase.
    """
    path = Path(path)
    for snapshot in (before, after):
        if (snapshot.get("truncated") is not False or snapshot.get("path") != str(path)
                or not isinstance(snapshot.get("content"), str)
                or snapshot.get("revision") != hashlib.sha256(snapshot["content"].encode("utf-8")).hexdigest()):
            raise DesktopError("Full before-and-after note evidence is required for a safe editor refresh.")
    appended = after.get("appended_text")
    if not isinstance(appended, str) or not appended or after.get("previous_revision") != before["revision"]:
        raise DesktopError("The editor refresh must be linked to the current task's saved append.")
    original = before["content"]
    expected = original + ("\n" if original and not original.endswith("\n") else "") + appended
    expected += "" if appended.endswith("\n") else "\n"
    if after["content"] != expected:
        raise DesktopError("The saved note does not match the verified append.")
    _disk_matches(path, after, notes_directory)
    desktop = desktop if desktop is not None else Atspi.get_desktop(0)
    deadline = clock() + 3
    while True:
        document = _document(desktop, path)
        if document is not None:
            value = _body_text(document)
            confirmation = _confirmation(document, path)
            if _same_buffer(value, after["content"]) and confirmation is None:
                _disk_matches(path, after, notes_directory)
                return True
            if not _same_buffer(value, before["content"]):
                raise DesktopError("The note has unsaved or unfamiliar editor contents; reload it manually.")
            if confirmation is not None or document.banner and document.button is not None:
                break
        if clock() >= deadline:
            raise DesktopError("The updated note is not visible yet; open or reload it in Text Editor, then continue.")
        yield .05
    # Return to GTK before the effect so Stop/voice updates can revoke the
    # dispatcher's current-task lease after the initial read-only scan.
    yield 0
    # Re-discover immediately before the effect; do not act on stale controls.
    checked = _document(desktop, path)
    if (checked is None or checked.window != document.window or checked.body != document.body
            or not _same_buffer(_body_text(checked), before["content"])):
        raise DesktopError("The note's editor changed before refresh; no reload was performed.")
    _disk_matches(path, after, notes_directory)
    if not _same_buffer(_body_text(checked), before["content"]):
        raise DesktopError("The note's editor changed before refresh; no reload was performed.")
    pending = _confirmation(checked, path)
    confirmed = pending is not None
    if pending is not None:
        # A previous attempt may have opened this exact app-level dialog.
        # The original buffer/disk proof above still applies; do not send a
        # second reload request while its confirmation is already pending.
        _activate_button(pending[1], "Discard")
    else:
        if checked.button != document.button or not checked.banner or checked.button is None:
            raise DesktopError("The note's editor changed before refresh; no reload was performed.")
        _activate_button(checked.button, _RELOAD)
    deadline = clock() + 3
    while True:
        yield .05
        _disk_matches(path, after, notes_directory)
        # Reload removes its banner and replaces editor children. Incomplete
        # native tree responses during that transition are read-only retries;
        # the action above is never repeated. Genuine ambiguity/dirty state
        # still raises immediately instead of entering this recovery path.
        try:
            updated = _document(desktop, path)
            pending = _confirmation(updated, path) if updated is not None else None
            verified = (updated is not None and updated.window == document.window
                        and pending is None and _same_buffer(_body_text(updated), after["content"]))
        except (NoteEditorTreePending, GLib.Error):
            pending = None
            verified = False
        if verified:
            return True
        if pending is not None:
            if confirmed:
                raise DesktopError("Text Editor's reload confirmation remained open; resolve it manually.")
            yield 0
            _disk_matches(path, after, notes_directory)
            current = _document(desktop, path)
            if (current is None or current.window != document.window
                    or current.body != document.body
                    or not _same_buffer(_body_text(current), before["content"])):
                raise DesktopError("The note changed before reload confirmation; no editor content was discarded.")
            current_confirmation = _confirmation(current, path)
            if current_confirmation != pending:
                raise DesktopError("The note's reload confirmation changed; resolve it manually.")
            _disk_matches(path, after, notes_directory)
            if not _same_buffer(_body_text(current), before["content"]):
                raise DesktopError("The note changed before reload confirmation; no editor content was discarded.")
            if _confirmation(current, path) != current_confirmation:
                raise DesktopError("The note's reload confirmation changed; resolve it manually.")
            _activate_button(current_confirmation[1], "Discard")
            confirmed = True
            deadline = clock() + 3
        if clock() >= deadline:
            raise DesktopError("The saved append is preserved, but Text Editor's updated text could not be verified.")
