"""Read GNOME's configured favorite shortcut without activating anything."""

from __future__ import annotations

import os


_SHELL = "org.gnome.shell"
_KEYBINDINGS = "org.gnome.shell.keybindings"
_DOCK = "org.gnome.shell.extensions.dash-to-dock"
_BACKGROUND_FOCUS_ACTIONS = {
    "skip", "cycle-windows", "focus-or-previews", "focus-or-appspread",
    "focus-minimize-or-previews", "focus-minimize-or-appspread",
}


def _load_gio():
    # Planning and tests can import this module without a native GI runtime.
    from gi.repository import Gio
    return Gio


def _settings(gio, source, schema_id):
    schema = source.lookup(schema_id, True)
    if schema is None:
        return None
    return gio.Settings.new_full(schema, None, None)


def _uses_chord(chords, index):
    # Treat common Super aliases as conflicts, even though the selected
    # binding itself must use GNOME's exact supported default spelling.
    return any(chord.casefold() in {f"<super>{index}", f"<mod4>{index}",
                                   f"<meta>{index}"} for chord in chords)


def configured_favorite_shortcut(app_id: str) -> str | None:
    """Return a current, unambiguous ``Super+1`` through ``Super+9`` chord.

    This only reads configuration. The caller must already know that the
    target application is running and another application is foreground,
    send the chord at most once through an approved input session, and verify
    the exact target window afterward. A shortcut does not identify a window.

    GNOME's stock handler calls the favorite app's activate(), raising its
    recent window rather than cycling windows:
    https://github.com/GNOME/gnome-shell/blob/49.0/js/ui/windowManager.js#L1547
    Ubuntu Dock instead activates the corresponding displayed icon; its
    focus-or-appspread action can open an overview if already foreground.
    Only layouts retaining the favorite order and background focus actions
    are accepted here. Unsupported or changed configuration returns None.
    """
    if (not isinstance(app_id, str) or not app_id.endswith(".desktop")
            or "/" in app_id or "\\" in app_id):
        return None
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").casefold().split(":")
    if "gnome" not in desktops:
        return None
    try:
        gio = _load_gio()
        source = gio.SettingsSchemaSource.get_default()
        if source is None:
            return None
        shell = _settings(gio, source, _SHELL)
        bindings = _settings(gio, source, _KEYBINDINGS)
        if shell is None or bindings is None:
            return None
        favorites = list(shell.get_strv("favorite-apps"))
        if favorites.count(app_id) != 1:
            return None
        index = favorites.index(app_id) + 1
        if not 1 <= index <= 9:
            return None
        preceding = favorites[:index]
        if len(set(preceding)) != len(preceding):
            return None
        # GNOME drops unavailable/hidden favorite apps, shifting later slots.
        visible_ids = {app.get_id() for app in gio.AppInfo.get_all()
                       if app.should_show()}
        if not all(favorite in visible_ids for favorite in preceding):
            return None

        binding_name = f"switch-to-application-{index}"
        expected = [f"<Super>{index}"]
        if list(bindings.get_strv(binding_name)) != expected:
            return None
        for key in bindings.props.settings_schema.list_keys():
            value = bindings.get_value(key)
            if (key != binding_name and value.get_type_string() == "as"
                    and _uses_chord(value.unpack(), index)):
                return None

        # Check installed Dock settings conservatively even if it is unloaded:
        # enabled-extensions alone does not include every session extension.
        dock = _settings(gio, source, _DOCK)
        if dock is not None and dock.get_boolean("hot-keys"):
            if (not dock.get_boolean("show-favorites")
                    or dock.get_boolean("isolate-workspaces")
                    or dock.get_boolean("isolate-monitors")
                    or dock.get_string("click-action") not in _BACKGROUND_FOCUS_ACTIONS):
                return None
            dock_binding = f"app-hotkey-{index}"
            if list(dock.get_strv(dock_binding)) != expected:
                return None
            for key in dock.props.settings_schema.list_keys():
                value = dock.get_value(key)
                if (key != dock_binding and value.get_type_string() == "as"
                        and _uses_chord(value.unpack(), index)):
                    return None
        return f"Super+{index}"
    except Exception:
        # Missing native schemas/runtime and unreadable settings are unsupported;
        # no guessed shortcut, settings write, launch, or desktop action follows.
        return None
