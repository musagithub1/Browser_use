from pathlib import Path
import os
import shlex

root = Path(__file__).resolve().parents[1]
run = root / "run.sh"
run.chmod(0o755)
applications = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "applications"
applications.mkdir(parents=True, exist_ok=True)
desktop = applications / "jev-linux.desktop"
exec_path = str(run).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
desktop.write_text(f'''[Desktop Entry]
Type=Application
Name=Jev Linux
Comment=Voice control for your Ubuntu desktop
Exec="{exec_path}"
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Accessibility;
StartupNotify=true
''')
bin_dir = Path.home() / ".local/bin"
bin_dir.mkdir(parents=True, exist_ok=True)
launcher = bin_dir / "jev-linux"
launcher.write_text("#!/bin/sh\nexec " + shlex.quote(str(run)) + ' "$@"\n')
launcher.chmod(0o755)
print("Installed Jev Linux app and jev-linux command.")
