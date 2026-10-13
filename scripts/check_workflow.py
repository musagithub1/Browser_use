"""Explicit live diagnostic: uses paid APIs, private scratch notes, and app launches."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jev_linux.environment import clean_editor_environment
clean_editor_environment()

from jev_linux.config import load_config
from jev_linux.controller import Controller
from jev_linux.desktop import DesktopBackend
from jev_linux.planning import Planner
from jev_linux.provider import JevClient


class LoggedJev(JevClient):
    def decide(self, state, questions):
        result = super().decide(state, questions)
        answers = result["answers"]
        print(json.dumps({"decision": answers["action"]["choice"],
                          "confidence": answers["action"]["confidence"],
                          "step_complete": answers.get("step_complete", {}).get("noul")}), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run the diagnostic task using paid APIs and open desktop apps")
    parser.add_argument("--web", action="store_true", help="Also read example.com and generate a summary")
    args = parser.parse_args()
    if not args.execute:
        parser.print_help()
        return
    checks = Path.home() / ".cache/jev-linux/checks"
    checks.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = Path(tempfile.mkdtemp(prefix="workflow-", dir=checks))
    config = load_config()
    config.update(notes_directory=str(folder / "notes"), memory_enabled=False)
    controller = Controller(DesktopBackend(), config, client=LoggedJev(config), planner=Planner(config), debounce=0)

    def event(value):
        if value["type"] in {"status", "action", "error", "confirmation", "task_progress"}:
            print(json.dumps({key: value[key] for key in ("type", "message", "status", "current_step", "total_steps") if key in value}), flush=True)

    controller.set_event_handler(event)
    command = ('Read https://example.com, create a note summarizing the page in two bullet points, then append "Reviewed today" to that note and open Calculator.' if args.web
               else 'Create a note containing "alpha", open Calculator, then append "beta" to that note.')
    controller.submit(command)
    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline:
            active = controller._active
            if active and active.status in {"completed", "failed", "blocked", "needs_details", "awaiting_confirmation", "paused"}:
                break
            time.sleep(.1)
        active = controller._active
        notes = {path.name: path.read_text() for path in (folder / "notes").glob("*.md")}
        report = {"command": command, "status": active.status if active else "no task",
                  "steps_verified": active.step_index if active else 0,
                  "actions": [item["action"] for item in active.completed] if active else [], "notes": notes}
        (folder / "result.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
        if report["status"] != "completed":
            raise RuntimeError(f"Diagnostic ended with {report['status']}; report: {folder / 'result.json'}")
        if args.web:
            assert len(notes) == 1 and "Reviewed today" in next(iter(notes.values()))
            assert "read_url" in report["actions"]
        else:
            assert list(notes.values()) == ["alpha\nbeta\n"], notes
    finally:
        controller.close()
        controller._worker.join(timeout=15)


if __name__ == "__main__":
    main()
