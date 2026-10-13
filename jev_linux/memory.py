"""Private local task history and explicitly remembered user preferences."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

from .config import DATA_DIR


MAX_TASKS = 30
MAX_PREFERENCES = 20
MAX_ACTIONS = 60
ACTION_FIELDS = {
    "action": 128,
    "summary": 1000,
    "app": 256,
    "url": 2000,
    "note_path": 2000,
    "text": 1000,
    "key": 128,
    "direction": 64,
    "browser": 256,
}
_TOKEN_PATTERN = re.compile(
    r"(?<![\w])(?:sk-[A-Za-z0-9_-]+|jv_live_[A-Za-z0-9_-]+|"
    r"ts_(?:live|test)_[A-Za-z0-9_-]+)"
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class MemoryStore:
    """Thread-safe SQLite memory; no audio, screenshots, or inferred preferences.

    Records have integer ``id`` values and UTC ``created_at`` timestamps. Tasks
    contain ``command``, ``status`` and sanitized, flat ``actions`` dictionaries.
    Preferences contain explicitly supplied ``text``. Lists are newest first.
    """

    def __init__(self, path: str | Path | None = None, secrets=()):
        self.path = Path(path) if path is not None else DATA_DIR / "memory.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        if isinstance(secrets, str):
            secrets = (secrets,)
        self._secrets = tuple(sorted(
            {secret for secret in secrets if isinstance(secret, str) and secret},
            key=len, reverse=True,
        ))
        self._db: sqlite3.Connection | None = None
        try:
            parent = self.path.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if parent.stat().st_uid != os.getuid():
                raise RuntimeError("Local memory requires a private directory owned by your user.")
            os.chmod(parent, 0o700)
            if self.path.is_symlink():
                raise RuntimeError("The local memory database must not be a symbolic link.")
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if not self.path.is_file() or self.path.stat().st_uid != os.getuid():
                    raise RuntimeError("The local memory database must be a private file owned by your user.")
            else:
                os.close(descriptor)
            os.chmod(self.path, 0o600)
            self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Local memory is damaged; the existing history was preserved.")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Local memory uses an unsupported format; the existing history was preserved.")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA secure_delete=ON")
            with self._db:
                self._db.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        command TEXT NOT NULL CHECK(length(command) <= 2000),
                        status TEXT NOT NULL CHECK(length(status) <= 64),
                        actions TEXT NOT NULL
                    )
                """)
                self._db.execute("""
                    CREATE TABLE IF NOT EXISTS preferences (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        text TEXT NOT NULL CHECK(length(text) <= 2000),
                        dedup_key TEXT NOT NULL UNIQUE
                    )
                """)
                # Verify an existing schema before changing its version marker.
                self._db.execute("SELECT id, created_at, command, status, actions FROM tasks LIMIT 0")
                self._db.execute("SELECT id, created_at, text, dedup_key FROM preferences LIMIT 0")
                self._db.execute("PRAGMA user_version=1")
        except (OSError, sqlite3.Error):
            if self._db is not None:
                self._db.close()
                self._db = None
            self._closed = True
            raise RuntimeError(
                "Local memory could not be opened safely; the existing history was preserved."
            ) from None
        except Exception:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._closed = True
            raise

    def _text(self, value: Any, limit: int) -> str:
        if not isinstance(value, str):
            raise ValueError("Memory text must be a string.")
        for secret in self._secrets:
            value = value.replace(secret, "[redacted]")
        value = _TOKEN_PATTERN.sub("[redacted]", value)
        return value.strip()[:limit]

    def _actions(self, actions: Any) -> list[dict[str, str]]:
        if not isinstance(actions, (list, tuple)):
            raise ValueError("Task actions must be a list of safe action summaries.")
        result = []
        for action in actions[-MAX_ACTIONS:]:
            if isinstance(action, str):
                action = {"summary": action}
            if not isinstance(action, dict):
                continue
            clean = {
                name: self._text(action[name], limit)
                for name, limit in ACTION_FIELDS.items()
                if isinstance(action.get(name), str)
            }
            if clean:
                result.append(clean)
        return result

    def _connection(self) -> sqlite3.Connection:
        if self._closed or self._db is None:
            raise RuntimeError("Local memory is closed.")
        return self._db

    @staticmethod
    def _database_error() -> RuntimeError:
        return RuntimeError("Local memory could not be read or updated; the existing history was preserved.")

    def record_task(self, command: str, status: str, actions) -> int:
        command = self._text(command, 2000)
        status = self._text(status, 64)
        actions_json = json.dumps(self._actions(actions), ensure_ascii=False, allow_nan=False)
        with self._lock:
            db = self._connection()
            try:
                with db:
                    cursor = db.execute(
                        "INSERT INTO tasks(created_at, command, status, actions) VALUES (?, ?, ?, ?)",
                        (_timestamp(), command, status, actions_json),
                    )
                    task_id = int(cursor.lastrowid)
                    db.execute("DELETE FROM tasks WHERE id NOT IN (SELECT id FROM tasks ORDER BY id DESC LIMIT ?)",
                               (MAX_TASKS,))
                return task_id
            except sqlite3.Error:
                raise self._database_error() from None

    def recent_tasks(self, limit: int = 8) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("The task history limit must be a nonnegative integer.")
        with self._lock:
            db = self._connection()
            try:
                rows = db.execute(
                    "SELECT id, created_at, command, status, actions FROM tasks ORDER BY id DESC LIMIT ?",
                    (min(limit, MAX_TASKS),),
                ).fetchall()
                tasks = []
                for row in rows:
                    actions = json.loads(row["actions"])
                    if not isinstance(actions, list):
                        raise ValueError("Invalid stored actions")
                    tasks.append({
                        "id": row["id"], "created_at": row["created_at"],
                        "command": self._text(row["command"], 2000),
                        "status": self._text(row["status"], 64), "actions": self._actions(actions),
                    })
                return tasks
            except (sqlite3.Error, ValueError, TypeError):
                raise self._database_error() from None

    def update_task(self, task_id: int, command: str | None = None,
                    status: str | None = None, actions=None) -> bool:
        """Update a recorded task; a forgotten ID never creates a replacement."""
        if isinstance(task_id, bool) or not isinstance(task_id, int):
            raise ValueError("A task ID must be an integer.")
        updates = {}
        if command is not None:
            updates["command"] = self._text(command, 2000)
        if status is not None:
            updates["status"] = self._text(status, 64)
        if actions is not None:
            updates["actions"] = json.dumps(self._actions(actions), ensure_ascii=False, allow_nan=False)
        with self._lock:
            db = self._connection()
            try:
                with db:
                    if not updates:
                        return db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is not None
                    # Column names come only from the fixed dictionary above.
                    columns = ", ".join(f"{name}=?" for name in updates)
                    cursor = db.execute(f"UPDATE tasks SET {columns} WHERE id=?",
                                        (*updates.values(), task_id))
                return cursor.rowcount > 0
            except sqlite3.Error:
                raise self._database_error() from None

    def remember(self, text: str) -> int:
        text = self._text(text, 2000)
        if not text:
            raise ValueError("A remembered preference cannot be empty.")
        dedup_key = " ".join(text.split()).casefold()
        with self._lock:
            db = self._connection()
            try:
                with db:
                    db.execute(
                        "INSERT OR IGNORE INTO preferences(created_at, text, dedup_key) VALUES (?, ?, ?)",
                        (_timestamp(), text, dedup_key),
                    )
                    preference_id = int(db.execute(
                        "SELECT id FROM preferences WHERE dedup_key=?", (dedup_key,),
                    ).fetchone()[0])
                    db.execute("DELETE FROM preferences WHERE id NOT IN (SELECT id FROM preferences ORDER BY id DESC LIMIT ?)",
                               (MAX_PREFERENCES,))
                return preference_id
            except sqlite3.Error:
                raise self._database_error() from None

    def preferences(self) -> list[dict]:
        with self._lock:
            db = self._connection()
            try:
                rows = db.execute("SELECT id, created_at, text FROM preferences ORDER BY id DESC LIMIT ?",
                                  (MAX_PREFERENCES,)).fetchall()
                return [{"id": row["id"], "created_at": row["created_at"],
                         "text": self._text(row["text"], 2000)} for row in rows]
            except (sqlite3.Error, ValueError):
                raise self._database_error() from None

    def forget_preference(self, preference_id: int) -> bool:
        if isinstance(preference_id, bool) or not isinstance(preference_id, int):
            raise ValueError("A preference ID must be an integer.")
        with self._lock:
            db = self._connection()
            try:
                with db:
                    cursor = db.execute("DELETE FROM preferences WHERE id=?", (preference_id,))
                return cursor.rowcount > 0
            except sqlite3.Error:
                raise self._database_error() from None

    def clear(self) -> None:
        with self._lock:
            db = self._connection()
            try:
                with db:
                    db.execute("DELETE FROM tasks")
                    db.execute("DELETE FROM preferences")
            except sqlite3.Error:
                raise self._database_error() from None

    def context(self) -> dict:
        with self._lock:
            return {"recent_tasks": self.recent_tasks(), "preferences": self.preferences()}

    def close(self) -> None:
        with self._lock:
            if self._db is not None and not self._closed:
                self._db.close()
                self._db = None
            self._closed = True

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *args) -> None:
        self.close()
