import hashlib
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from jev_linux.notes import NoteChangedError, NoteError, append_note, create_note, read_note


class NoteActionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "notes"
        self.root.mkdir()
        self.path = self.root / "existing.md"

    def test_append_preserves_original_bytes_and_adds_missing_separator(self):
        original = "# Original\r\nExisting café".encode("utf-8")
        self.path.write_bytes(original)
        snapshot = read_note(self.path, self.root)
        result = append_note(self.path, "追加の内容", self.root, expected_revision=snapshot["revision"])
        self.assertEqual(result, self.path.resolve())
        self.assertEqual(self.path.read_bytes(), original + "\n追加の内容\n".encode("utf-8"))

    def test_append_preserves_existing_final_newline_without_extra_blank_line(self):
        self.path.write_bytes(b"original\n")
        append_note(self.path, "new content\n", self.root)
        self.assertEqual(self.path.read_bytes(), b"original\nnew content\n")

    def test_append_empty_note_does_not_add_leading_blank_line(self):
        self.path.touch()
        append_note(self.path, "first line", self.root)
        self.assertEqual(self.path.read_bytes(), b"first line\n")

    def test_read_returns_utf8_character_bound_and_full_file_revision(self):
        raw = "café 🍵\nsecond line".encode("utf-8")
        self.path.write_bytes(raw)
        result = read_note(self.path, self.root, max_chars=6)
        self.assertEqual(result, {"path": str(self.path.resolve()), "content": "café 🍵",
                                  "truncated": True, "revision": hashlib.sha256(raw).hexdigest()})
        complete = read_note(self.path, self.root, max_chars=100)
        self.assertEqual(complete["content"], raw.decode("utf-8"))
        self.assertFalse(complete["truncated"])
        self.assertEqual(complete["revision"], result["revision"])

    def test_read_handles_utf8_character_split_across_stream_chunks(self):
        raw = b"x" * 65535 + "🍵".encode("utf-8") + b"tail"
        self.path.write_bytes(raw)
        result = read_note(self.path, self.root, max_chars=65536)
        self.assertEqual(result["content"], "x" * 65535 + "🍵")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["revision"], hashlib.sha256(raw).hexdigest())

    def test_read_validates_utf8_beyond_truncated_prefix(self):
        original = b"valid prefix" + b"\xff"
        self.path.write_bytes(original)
        with self.assertRaisesRegex(NoteError, "UTF-8"):
            read_note(self.path, self.root, max_chars=3)
        with self.assertRaisesRegex(NoteError, "UTF-8"):
            append_note(self.path, "next", self.root)
        self.assertEqual(self.path.read_bytes(), original)

    def test_changed_revision_rejects_append_and_preserves_newer_content(self):
        self.path.write_text("before", encoding="utf-8")
        revision = read_note(self.path, self.root)["revision"]
        self.path.write_text("edited externally", encoding="utf-8")
        with self.assertRaisesRegex(NoteChangedError, "changed since it was read"):
            append_note(self.path, "append", self.root, expected_revision=revision)
        self.assertEqual(self.path.read_text(), "edited externally")

    def test_relative_path_and_internal_symlink_resolve_inside_notes(self):
        self.path.write_text("base\n", encoding="utf-8")
        alias = self.root / "alias.md"
        alias.symlink_to(self.path)
        self.assertEqual(read_note("alias.md", self.root)["path"], str(self.path.resolve()))
        append_note(alias, "next", self.root)
        self.assertEqual(self.path.read_text(), "base\nnext\n")

    def test_outside_path_and_external_symlinks_are_rejected(self):
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("outside original", encoding="utf-8")
        file_link = self.root / "link.md"
        file_link.symlink_to(outside)
        folder = Path(self.temp.name) / "outside-folder"
        folder.mkdir()
        nested = folder / "nested.md"
        nested.write_text("nested original", encoding="utf-8")
        dir_link = self.root / "link-dir"
        dir_link.symlink_to(folder, target_is_directory=True)
        for path in (outside, "../outside.md", file_link, dir_link / "nested.md"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(NoteError, "inside"):
                    read_note(path, self.root)
                with self.assertRaisesRegex(NoteError, "inside"):
                    append_note(path, "bad append", self.root)
        self.assertEqual(outside.read_text(), "outside original")
        self.assertEqual(nested.read_text(), "nested original")

    def test_missing_and_non_markdown_notes_are_not_created_or_modified(self):
        with self.assertRaises(FileNotFoundError):
            append_note(self.path, "content", self.root)
        self.assertFalse(self.path.exists())
        text = self.root / "existing.txt"
        text.write_text("text original", encoding="utf-8")
        for action in (lambda: read_note(text, self.root), lambda: append_note(text, "append", self.root)):
            with self.assertRaisesRegex(NoteError, "Markdown"):
                action()
        self.assertEqual(text.read_text(), "text original")

    def test_fifo_and_directory_are_rejected_without_blocking(self):
        os.mkfifo(self.path)
        with self.assertRaisesRegex(NoteError, "regular file"):
            read_note(self.path, self.root)
        with self.assertRaisesRegex(NoteError, "regular file"):
            append_note(self.path, "append", self.root)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises((NoteError, IsADirectoryError)):
            append_note(self.path, "append", self.root)

    def test_append_keeps_existing_permissions_and_create_is_private(self):
        self.path.write_text("base", encoding="utf-8")
        self.path.chmod(0o640)
        append_note(self.path, "next", self.root)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        first = create_note(self.root, "Original", "new")
        second = create_note(self.root, "Other", "new")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(), "Original\n")
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)

    def test_short_writes_complete_append_without_replacing_original(self):
        self.path.write_text("old", encoding="utf-8")
        real_write = os.write
        def write_small(fd, payload):
            return real_write(fd, payload[:2])
        with patch("jev_linux.notes.os.write", side_effect=write_small):
            append_note(self.path, "new content", self.root)
        self.assertEqual(self.path.read_text(), "old\nnew content\n")

    def test_cooperative_concurrent_revision_appends_only_one_change(self):
        self.path.write_text("base\n", encoding="utf-8")
        revision = read_note(self.path, self.root)["revision"]
        barrier = threading.Barrier(2)
        results = []
        def append(value):
            barrier.wait(timeout=2)
            try:
                append_note(self.path, value, self.root, expected_revision=revision)
                results.append("saved")
            except NoteChangedError:
                results.append("changed")
        threads = [threading.Thread(target=append, args=(value,)) for value in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ["saved", "changed"])
        self.assertIn(self.path.read_text(), ("base\nfirst\n", "base\nsecond\n"))

    def test_atomic_editor_replacement_during_read_is_rejected(self):
        self.path.write_text("original", encoding="utf-8")
        real_read = os.read
        replaced = False
        def replace_then_read(fd, count):
            nonlocal replaced
            if not replaced:
                replacement = self.root / "replacement.md"
                replacement.write_text("newer version", encoding="utf-8")
                replacement.replace(self.path)
                replaced = True
            return real_read(fd, count)
        with patch("jev_linux.notes.os.read", side_effect=replace_then_read):
            with self.assertRaises(NoteChangedError):
                append_note(self.path, "should not append", self.root)
        self.assertEqual(self.path.read_text(), "newer version")

    def test_invalid_append_input_makes_no_changes(self):
        self.path.write_text("original", encoding="utf-8")
        for content in ("", None, "text\x00content", "\ud800"):
            with self.subTest(content=repr(content)), self.assertRaises((ValueError, UnicodeEncodeError)):
                append_note(self.path, content, self.root)
        with self.assertRaises(ValueError):
            append_note(self.path, "append", self.root, expected_revision="invalid")
        self.assertEqual(self.path.read_text(), "original")


if __name__ == "__main__":
    unittest.main()
