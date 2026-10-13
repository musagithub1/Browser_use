"""Private memory persists useful references without retaining credentials."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from jev_linux.memory import MemoryStore


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "private" / "memory.sqlite3"
        self.memory = MemoryStore(self.path, secrets=("my-private-token",))

    def tearDown(self):
        self.memory.close()
        self.directory.cleanup()

    def test_permissions_and_restart_persistence(self):
        task_id = self.memory.record_task("Create a shopping note", "done", [{
            "action": "create_note", "note_path": "/home/user/Documents/shopping.txt",
            "summary": "Saved shopping note",
        }])
        preference_id = self.memory.remember("Use Firefox for browsing.")
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.memory.close()
        with MemoryStore(self.path) as reopened:
            self.assertEqual(reopened.recent_tasks()[0]["id"], task_id)
            self.assertEqual(reopened.recent_tasks()[0]["actions"][0]["note_path"],
                             "/home/user/Documents/shopping.txt")
            self.assertEqual(reopened.preferences()[0]["id"], preference_id)
            self.assertTrue(reopened.preferences()[0]["created_at"].endswith("Z"))
            json.dumps(reopened.context(), allow_nan=False)

    def test_secrets_are_redacted_before_storage(self):
        secrets = ["my-private-token", "sk-or-v1-012345abcdef", "sk-proj-secret123",
                   "jv_live_private123", "ts_live_secret456"]
        command = "Remember " + " ".join(secrets)
        self.memory.record_task(command, "done", [{
            "action": "write_text", "text": command, "summary": command,
        }])
        self.memory.remember(command)
        serialized = json.dumps(self.memory.context())
        on_disk = self.path.read_bytes()
        for secret in secrets:
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret.encode(), on_disk)
        self.assertIn("[redacted]", serialized)

    def test_bounded_fields_and_only_safe_action_references(self):
        actions = [{
            "action": "create_note", "summary": "s" * 3000, "text": "t" * 3000,
            "app": "Text Editor", "url": "https://example.com", "note_path": "/tmp/note.txt",
            "key": "Ctrl+S", "direction": "down", "target": "raw-target-id",
            "error": "private error", "ui_labels": ["private label"],
            "arguments": {"arbitrary": "private"}, "raw_audio": b"private",
        } for _ in range(75)]
        self.memory.record_task("c" * 5000, "s" * 100, actions)
        task = self.memory.recent_tasks()[0]
        self.assertEqual(len(task["command"]), 2000)
        self.assertEqual(len(task["status"]), 64)
        self.assertEqual(len(task["actions"]), 60)
        self.assertEqual(len(task["actions"][0]["text"]), 1000)
        self.assertEqual(len(task["actions"][0]["summary"]), 1000)
        self.assertEqual(set(task["actions"][0]), {
            "action", "summary", "text", "app", "url", "note_path", "key", "direction",
        })

    def test_retention_and_context_limits(self):
        for index in range(45):
            self.memory.record_task(f"Task {index}", "done", [])
        for index in range(25):
            self.memory.remember(f"Preference {index}")
        tasks = self.memory.recent_tasks(100)
        self.assertEqual(len(tasks), 30)
        self.assertEqual(tasks[0]["command"], "Task 44")
        self.assertEqual(tasks[-1]["command"], "Task 15")
        self.assertEqual(len(self.memory.preferences()), 20)
        self.assertEqual(self.memory.preferences()[-1]["text"], "Preference 5")
        self.assertEqual(len(self.memory.context()["recent_tasks"]), 8)
        self.assertEqual(self.memory.recent_tasks(0), [])

    def test_explicit_preferences_are_deduplicated_and_removable(self):
        preference_id = self.memory.remember("Use Firefox.")
        self.assertEqual(self.memory.remember("  use   firefox.  "), preference_id)
        self.assertEqual(len(self.memory.preferences()), 1)
        self.assertEqual(self.memory.preferences()[0]["text"], "Use Firefox.")
        self.assertTrue(self.memory.forget_preference(preference_id))
        self.assertFalse(self.memory.forget_preference(preference_id))
        self.assertEqual(self.memory.preferences(), [])
        with self.assertRaises(ValueError):
            self.memory.remember("   ")

    def test_updates_preserve_work_and_cancelled_status(self):
        task_id = self.memory.record_task("Open notes and", "running", [{"action": "launch_app", "app": "Text Editor"}])
        self.assertTrue(self.memory.update_task(task_id, command="Open notes and create a list",
                                               actions=[{"action": "launch_app", "app": "Text Editor"},
                                                        {"action": "create_note", "note_path": "/tmp/list.txt"}]))
        self.assertTrue(self.memory.update_task(task_id, status="cancelled"))
        self.memory.close()
        with MemoryStore(self.path) as reopened:
            task = reopened.recent_tasks()[0]
            self.assertEqual(task["status"], "cancelled")
            self.assertEqual(task["command"], "Open notes and create a list")
            self.assertEqual(len(task["actions"]), 2)
            self.assertEqual(task["actions"][1]["note_path"], "/tmp/list.txt")
            self.assertFalse(reopened.update_task(99999, status="done"))
            self.assertEqual(len(reopened.recent_tasks()), 1)

    def test_clear_retains_monotonic_ids(self):
        task_id = self.memory.record_task("Old task", "done", [])
        preference_id = self.memory.remember("Old preference")
        self.memory.clear()
        self.assertEqual(self.memory.context(), {"recent_tasks": [], "preferences": []})
        self.assertFalse(self.memory.update_task(task_id, status="done"))
        self.assertGreater(self.memory.record_task("New task", "running", []), task_id)
        self.assertGreater(self.memory.remember("New preference"), preference_id)

    def test_concurrent_reads_and_writes(self):
        def work(index):
            task_id = self.memory.record_task(f"Task {index}", "running", [])
            self.memory.update_task(task_id, status="done", actions=[{"action": "scroll", "direction": "down"}])
            self.memory.remember(f"Preference {index % 10}")
            json.dumps(self.memory.context(), allow_nan=False)
            return task_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(work, range(80)))
        self.assertEqual(len(set(ids)), 80)
        self.assertEqual(len(self.memory.recent_tasks(30)), 30)
        self.assertEqual(len(self.memory.preferences()), 10)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_corrupt_database_is_preserved(self):
        self.memory.close()
        corrupt = b"This is important history that must not be silently erased."
        self.path.write_bytes(corrupt)
        with self.assertRaisesRegex(RuntimeError, "history was preserved"):
            MemoryStore(self.path)
        self.assertEqual(self.path.read_bytes(), corrupt)

    def test_closed_memory_rejects_operations(self):
        self.memory.close()
        self.memory.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.memory.context()


if __name__ == "__main__":
    unittest.main()
