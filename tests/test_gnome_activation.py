import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jev_linux.gnome_activation import configured_favorite_shortcut


CHROME = "google-chrome.desktop"
FAVORITES = ["firefox_firefox.desktop", "org.gnome.Terminal.desktop",
             "snap-store_snap-store.desktop", CHROME]


class FakeValue:
    def __init__(self, value):
        self.value = value

    def get_type_string(self):
        return "as" if isinstance(self.value, list) else "b"

    def unpack(self):
        return self.value


class FakeSettings:
    def __init__(self, values):
        self.values = values
        self.props = SimpleNamespace(settings_schema=SimpleNamespace(
            list_keys=lambda: list(values)))

    def get_strv(self, key):
        return self.values[key]

    def get_boolean(self, key):
        return self.values[key]

    def get_string(self, key):
        return self.values[key]

    def get_value(self, key):
        return FakeValue(self.values[key])


def fake_gio(favorites=None, *, dock=True):
    favorites = list(FAVORITES if favorites is None else favorites)
    values = {
        "org.gnome.shell": {"favorite-apps": favorites},
        "org.gnome.shell.keybindings": {
            f"switch-to-application-{index}": [f"<Super>{index}"]
            for index in range(1, 10)},
    }
    if dock:
        values["org.gnome.shell.extensions.dash-to-dock"] = {
            "hot-keys": True, "show-favorites": True,
            "isolate-workspaces": False, "isolate-monitors": False,
            "click-action": "focus-or-appspread",
            **{f"app-hotkey-{index}": [f"<Super>{index}"]
               for index in range(1, 11)},
        }
    source = SimpleNamespace(lookup=lambda schema_id, recursive:
                             schema_id if schema_id in values else None)
    apps = [SimpleNamespace(get_id=lambda name=name: name,
                            should_show=lambda: True) for name in favorites]
    gio = SimpleNamespace(
        SettingsSchemaSource=SimpleNamespace(get_default=lambda: source),
        Settings=SimpleNamespace(new_full=lambda schema, backend, path:
                                 FakeSettings(values[schema])),
        AppInfo=SimpleNamespace(get_all=lambda: apps),
    )
    return gio, values, apps


class GnomeActivationTests(unittest.TestCase):
    def resolve(self, gio, app_id=CHROME, desktop="ubuntu:GNOME"):
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": desktop}), \
             patch("jev_linux.gnome_activation._load_gio", return_value=gio):
            return configured_favorite_shortcut(app_id)

    def test_current_ubuntu_dock_layout_resolves_chrome_without_actions(self):
        gio, _, _ = fake_gio()
        self.assertEqual(self.resolve(gio), "Super+4")

    def test_stock_gnome_and_disabled_dock_use_stock_shortcut(self):
        gio, _, _ = fake_gio(dock=False)
        self.assertEqual(self.resolve(gio), "Super+4")
        gio, values, _ = fake_gio()
        values["org.gnome.shell.extensions.dash-to-dock"].update(
            {"hot-keys": False, "show-favorites": False})
        self.assertEqual(self.resolve(gio), "Super+4")

    def test_non_gnome_and_missing_runtime_or_schema_are_unsupported(self):
        gio, values, _ = fake_gio()
        self.assertIsNone(self.resolve(gio, desktop="KDE"))
        values.pop("org.gnome.shell.keybindings")
        self.assertIsNone(self.resolve(gio))
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}), \
             patch("jev_linux.gnome_activation._load_gio", side_effect=ImportError):
            self.assertIsNone(configured_favorite_shortcut(CHROME))

    def test_absent_duplicated_and_out_of_range_favorites_are_unsupported(self):
        for favorites in (FAVORITES[:-1], FAVORITES + [CHROME],
                          ["org.gnome.Terminal.desktop", *FAVORITES],
                          [f"app-{index}.desktop" for index in range(9)] + [CHROME]):
            with self.subTest(favorites=favorites):
                gio, _, _ = fake_gio(favorites)
                self.assertIsNone(self.resolve(gio))

    def test_unavailable_or_hidden_preceding_favorite_does_not_guess_slot(self):
        gio, _, apps = fake_gio()
        apps.pop(0)
        self.assertIsNone(self.resolve(gio))
        gio, _, apps = fake_gio()
        apps[0].should_show = lambda: False
        self.assertIsNone(self.resolve(gio))

    def test_disabled_remapped_and_multiple_stock_bindings_are_unsupported(self):
        for binding in ([], ["<Alt>4"], ["<Super>4", "<Alt>4"], ["<Super>5"]):
            with self.subTest(binding=binding):
                gio, values, _ = fake_gio()
                values["org.gnome.shell.keybindings"]["switch-to-application-4"] = binding
                self.assertIsNone(self.resolve(gio))

    def test_conflicting_shell_or_dock_bindings_are_unsupported(self):
        for schema, key, binding in (
                ("org.gnome.shell.keybindings", "toggle-overview", "<Super>4"),
                ("org.gnome.shell.keybindings", "switch-to-application-2", "<Mod4>4"),
                ("org.gnome.shell.extensions.dash-to-dock", "app-hotkey-2", "<Super>4"),
                ("org.gnome.shell.extensions.dash-to-dock", "shortcut", "<Super>4")):
            with self.subTest(schema=schema, key=key):
                gio, values, _ = fake_gio()
                values[schema][key] = [binding]
                self.assertIsNone(self.resolve(gio))

    def test_dock_reordering_remapping_isolation_and_non_focus_actions_are_unsupported(self):
        for changed in ({"show-favorites": False}, {"isolate-workspaces": True},
                        {"isolate-monitors": True}, {"app-hotkey-4": []},
                        {"app-hotkey-4": ["<Alt>4"]}, {"click-action": "launch"},
                        {"click-action": "quit"}, {"click-action": "previews"}):
            with self.subTest(changed=changed):
                gio, values, _ = fake_gio()
                values["org.gnome.shell.extensions.dash-to-dock"].update(changed)
                self.assertIsNone(self.resolve(gio))


if __name__ == "__main__":
    unittest.main()
