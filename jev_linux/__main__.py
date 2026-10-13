import argparse
import json
from pathlib import Path
import shutil
import sys

from .config import DATA_DIR, load_config
from .environment import clean_editor_environment


def main():
    clean_editor_environment()
    parser = argparse.ArgumentParser(description="Jev voice control for Ubuntu")
    parser.add_argument("--doctor", action="store_true", help="Check local dependencies without activating the microphone")
    parser.add_argument("--check-api", action="store_true", help="Make one small, paid Jev connectivity check")
    parser.add_argument("--plan", metavar="COMMAND", help="Plan an instruction without executing it")
    parser.add_argument("--smoke-ui", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = load_config()
    if args.check_api:
        from .provider import JevClient
        with JevClient(config) as client:
            result = client.decide({"command": "open the notes app"}, {
                "intent": {"type": "choice", "instructions": "What does the command request?", "criteria": {"open": "Open an app", "type": "Type text", "none": "Nothing"}}
            })
            print(json.dumps(result, indent=2))
        return
    from .desktop import DesktopBackend
    backend = DesktopBackend()
    if args.doctor:
        speech_path = config["vosk_model_path"] if config.get("speech_engine") == "vosk" else config["speech_model_path"]
        print(json.dumps({"desktop": backend.status(),
                          "speech_model_installed": bool(speech_path) and Path(speech_path).is_dir(),
                          "speech_engine": config.get("speech_engine"),
                          "speech_language": config.get("speech_language"),
                          "speech_model_arch": config.get("speech_model_arch"),
                          "planning_enabled": bool(config.get("planning_enabled", True)),
                          "planner_model": config.get("planner_model"),
                          "speech_silence_seconds": config.get("speech_silence_seconds"),
                          "memory_enabled": bool(config.get("memory_enabled", True)),
                          "memory_database_installed": (DATA_DIR / "memory.sqlite3").is_file(),
                          "microphone_recorder": bool(shutil.which("arecord")),
                          "provider": config["provider"],
                          "key_configured": bool(config.get(config["provider"] + "_api_key")),
                          "installed_apps": [a["name"] for a in backend.apps()]}, indent=2))
        backend.close()
        return
    if args.plan:
        from .controller import OPERATIONS, build_request
        from .memory import MemoryStore
        from .provider import JevClient
        memory = None
        if config.get("memory_enabled", True) and (DATA_DIR / "memory.sqlite3").is_file():
            store = MemoryStore(secrets=[config.get(name, "") for name in ("openrouter_api_key", "typesafe_api_key")])
            try:
                memory = store.context()
            finally:
                store.close()
        try:
            if config.get("planning_enabled", True):
                from .planning import Planner
                planner = Planner(config)
                try:
                    result = planner.plan(args.plan, context={"available_actions": {
                        key: value for key, value in OPERATIONS.items() if key not in {"done", "wait", "unsupported"}},
                        "installed_apps": backend.apps(), "desktop": backend.snapshot(), "memory": memory or {}})
                    print(json.dumps(result.as_dict(), indent=2))
                finally:
                    planner.close()
            else:
                state, questions, _ = build_request(args.plan, True, backend.snapshot(), backend.apps(), [], memory)
                with JevClient(config) as client:
                    result = client.decide(state, questions)
                    print(json.dumps(result, indent=2))
        finally:
            backend.close()
        return
    from .controller import Controller
    from .ui import launch_ui
    from .main_thread_desktop import MainThreadDesktopBackend
    from gi.repository import GLib
    GLib.set_application_name("Jev Linux")
    GLib.set_prgname("jev-linux")
    backend = MainThreadDesktopBackend(backend)
    controller = Controller(backend, config)
    if args.smoke_ui:
        from gi.repository import GLib, Gtk
        GLib.timeout_add(2500, lambda: (Gtk.main_quit(), False)[1])
    launch_ui(controller, backend, config)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Jev Linux: {error}", file=sys.stderr)
        sys.exit(1)
