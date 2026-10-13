"""Check setup's fresh/update behavior without downloads or user-home writes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jev-setup-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project with spaces"
        (self.project / "scripts").mkdir(parents=True)
        (self.project / "jev_linux").mkdir()
        for name in ("__init__.py", "environment.py", "config.py"):
            shutil.copy2(PROJECT / "jev_linux" / name, self.project / "jev_linux" / name)
        shutil.copy2(PROJECT / "scripts/setup.sh", self.project / "scripts/setup.sh")
        (self.project / "requirements.txt").write_text("# Installer calls are intercepted.\n")

        self.bin = self.root / "bin"
        self.bin.mkdir()
        # A Snap/editor-selected Python on PATH must not handle native GI.
        self.executable(self.bin / "python3", "#!/bin/sh\nexit 99\n")
        self.executable(self.bin / "uv", '''#!/usr/bin/python3
import json, os, subprocess, sys
from pathlib import Path
log = Path(os.environ["XDG_CACHE_HOME"]) / "setup-events.jsonl"
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a") as stream:
    stream.write(json.dumps({"uv": sys.argv[1:]}) + "\\n")
if sys.argv[1] == "venv":
    subprocess.run(["/usr/bin/python3", "-m", "venv", "--system-site-packages",
                    "--without-pip", sys.argv[-1]], check=True)
elif sys.argv[1:3] != ["pip", "install"]:
    raise SystemExit("Unexpected installer invocation")
''')
        (self.project / "scripts/setup_speech.py").write_text('''import json, os, sys
from pathlib import Path
from jev_linux.config import CONFIG_PATH, save_config
log = Path(os.environ["XDG_CACHE_HOME"]) / "setup-events.jsonl"
with log.open("a") as stream:
    stream.write(json.dumps({"speech": sys.argv[1:]}) + "\\n")
if "--no-config" not in sys.argv:
    save_config({"speech_engine": "moonshine", "speech_model_arch": 4})
''')
        # Put the relocated source on sys.path just as the real script does.
        speech = self.project / "scripts/setup_speech.py"
        speech.write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n" + speech.read_text())
        (self.project / "scripts/install_launcher.py").write_text('''import json, os
from pathlib import Path
log = Path(os.environ["XDG_CACHE_HOME"]) / "setup-events.jsonl"
with log.open("a") as stream:
    stream.write(json.dumps({"launcher": True}) + "\\n")
''')
        self.env = dict(os.environ)
        for name in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "DISPLAY", "WAYLAND_DISPLAY",
                     "DBUS_SESSION_BUS_ADDRESS", "DBUS_STARTER_ADDRESS", "AT_SPI_BUS_ADDRESS"):
            self.env.pop(name, None)
        self.env.update(
            PATH=str(self.bin) + ":/usr/bin:/bin",
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_DATA_HOME=str(self.root / "data"),
            XDG_CACHE_HOME=str(self.root / "cache"),
            PYTHONDONTWRITEBYTECODE="1",
            DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent-jev-setup-test-bus",
            GTK_PATH="/snap/invalid-editor/gtk",
            GIO_MODULE_DIR="/snap/invalid-editor/gio",
            GDK_PIXBUF_MODULE_FILE="/snap/invalid-editor/pixbuf",
            GSETTINGS_SCHEMA_DIR="/snap/invalid-editor/schemas",
            XDG_DATA_DIRS="/snap/invalid-editor/share:/usr/local/share:/usr/share",
        )
        self.config = self.root / "config/jev-linux/config.json"

    @staticmethod
    def executable(path, text):
        path.write_text(text)
        path.chmod(0o755)

    def run_setup(self):
        return subprocess.run(["/bin/bash", "scripts/setup.sh"], cwd=self.project,
                              env=self.env, text=True, capture_output=True, timeout=30)

    def events(self):
        log = self.root / "cache/setup-events.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_fresh_setup_uses_system_python_and_activates_default_speech(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events()
        self.assertEqual(events[0]["uv"][0], "venv")
        self.assertIn({"speech": []}, events)
        self.assertIn({"launcher": True}, events)
        config = json.loads(self.config.read_text())
        self.assertEqual(config["speech_engine"], "moonshine")
        self.assertEqual(config["speech_model_arch"], 4)

    def test_update_keeps_existing_environment_and_selected_speech_settings(self):
        subprocess.run(["/usr/bin/python3", "-m", "venv", "--system-site-packages",
                        "--without-pip", str(self.project / ".venv")], check=True,
                       env=self.env, capture_output=True, timeout=30)
        marker = self.project / ".venv/keep-existing-package-marker"
        marker.write_text("preserve existing environment")
        self.config.parent.mkdir(parents=True)
        original = json.dumps({"speech_engine": "vosk", "vosk_model_path": "/chosen/model",
                               "speech_model_arch": 5, "speech_language": "en",
                               "notes_directory": "/chosen/notes", "memory_enabled": False})
        self.config.write_text(original)
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events()
        self.assertFalse(any(event.get("uv", [None])[0] == "venv" for event in events))
        self.assertTrue(any(event.get("uv", [])[:2] == ["pip", "install"] for event in events))
        self.assertIn({"speech": ["--no-config"]}, events)
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(marker.read_text(), "preserve existing environment")

    def test_conflicting_venv_path_is_preserved_and_refuses_installation(self):
        marker = self.project / ".venv"
        marker.write_text("existing unrelated file")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Preserve it elsewhere", result.stdout)
        self.assertEqual(marker.read_text(), "existing unrelated file")
        self.assertEqual(self.events(), [])

    def test_system_python_disguised_as_existing_venv_refuses_installation(self):
        environment = self.project / ".venv"
        (environment / "bin").mkdir(parents=True)
        (environment / "pyvenv.cfg").write_text("home = /usr/bin\n")
        self.executable(environment / "bin/python", '#!/bin/sh\nexec /usr/bin/python3 "$@"\n')
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
