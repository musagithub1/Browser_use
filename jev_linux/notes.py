"""Create, read, and append local Markdown notes without replacing content."""

from contextlib import contextmanager
import codecs
from datetime import datetime
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat


class NoteError(RuntimeError):
    """A note is inaccessible or unsuitable for the requested operation."""


class NoteChangedError(NoteError):
    """The note changed while reading or since a supplied revision."""


def _resolve_note(path, notes_directory):
    root = Path(notes_directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NoteError("The configured notes directory is not a directory.")
    requested = Path(path).expanduser()
    if requested.suffix.casefold() != ".md":
        raise NoteError("Choose an existing Markdown (.md) note.")
    requested = requested if requested.is_absolute() else root / requested
    target = requested.resolve(strict=True)
    if not target.is_relative_to(root) or target == root:
        raise NoteError("The note must be inside the configured notes directory.")
    if target.suffix.casefold() != ".md":
        raise NoteError("Choose an existing Markdown (.md) note.")
    return root, target


@contextmanager
def _open_note(path, notes_directory, *, append=False):
    root, target = _resolve_note(path, notes_directory)
    relative = target.relative_to(root)
    directories = []
    fd = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        directories.append(directory)
        # Walk the already resolved path under pinned directory descriptors.
        # A later symlink replacement must fail instead of escaping the root.
        for component in relative.parts[:-1]:
            directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory)
            directories.append(directory)
        flags = (os.O_RDWR | os.O_APPEND) if append else os.O_RDONLY
        fd = os.open(relative.name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NoteError("The selected note must be a regular file.")
        fcntl.flock(fd, fcntl.LOCK_EX if append else fcntl.LOCK_SH)
        _assert_current_file(fd, directory, relative.name)
        yield target, fd, directory, relative.name
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


def _assert_current_file(fd, directory, name):
    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    opened = os.fstat(fd)
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise NoteChangedError("The note changed since it was read. Read it again before appending.")


def _file_version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_open_note(fd, max_chars):
    """Hash/validate the full file, retaining only a bounded UTF-8 prefix."""
    before = os.fstat(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    chunks = []
    retained = 0
    characters = 0
    last_byte = b""
    try:
        while True:
            raw = os.read(fd, 65536)
            if not raw:
                break
            digest.update(raw)
            last_byte = raw[-1:]
            decoded = decoder.decode(raw, final=False)
            characters += len(decoded)
            if retained < max_chars:
                prefix = decoded[:max_chars - retained]
                chunks.append(prefix)
                retained += len(prefix)
        decoded = decoder.decode(b"", final=True)
        characters += len(decoded)
        if retained < max_chars:
            chunks.append(decoded[:max_chars - retained])
    except UnicodeDecodeError as exc:
        raise NoteError("The note is not valid UTF-8 text.") from exc
    if _file_version(before) != _file_version(os.fstat(fd)):
        raise NoteChangedError("The note changed while it was being read. Read it again.")
    return {"content": "".join(chunks), "truncated": characters > max_chars,
            "revision": digest.hexdigest()}, last_byte


def read_note(path, notes_directory, max_chars=16000):
    """Read a verified local .md note with a full-file SHA-256 revision.

    Content is limited by Unicode character count. The whole file is streamed
    for its revision and UTF-8 validity; extra content is not retained.
    """
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 0 <= max_chars <= 1000000:
        raise ValueError("max_chars must be an integer between 0 and 1,000,000.")
    with _open_note(path, notes_directory) as (target, fd, directory, name):
        result, _ = _read_open_note(fd, max_chars)
        _assert_current_file(fd, directory, name)
        return {"path": str(target), **result}


def append_note(path, content, notes_directory, expected_revision=None):
    """Append text to an existing verified note, preserving every original byte.

    A missing final newline gets a separating newline before new content. The
    appended content ends with a newline. Existing file permissions are kept.
    Cooperative writers are locked; a supplied revision also detects edits
    made since read_note(). External editors may not honor advisory locks.
    """
    if not isinstance(content, str) or not content or "\x00" in content:
        raise ValueError("Provide nonempty text without null bytes to append.")
    if expected_revision is not None and (not isinstance(expected_revision, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_revision) is None):
        raise ValueError("expected_revision must be a SHA-256 revision from read_note().")
    # Encode before opening the file so malformed Unicode cannot cause writes.
    encoded = content.encode("utf-8")
    with _open_note(path, notes_directory, append=True) as (target, fd, directory, name):
        current, last_byte = _read_open_note(fd, 0)
        if expected_revision is not None and current["revision"] != expected_revision:
            raise NoteChangedError("The note changed since it was read. Read it again before appending.")
        _assert_current_file(fd, directory, name)
        prefix = b"\n" if last_byte and last_byte != b"\n" else b""
        suffix = b"" if encoded.endswith(b"\n") else b"\n"
        payload = memoryview(prefix + encoded + suffix)
        while payload:
            written = os.write(fd, payload)
            if written <= 0:
                raise NoteError("Could not finish appending the note. Original content was preserved.")
            payload = payload[written:]
        os.fsync(fd)
        _assert_current_file(fd, directory, name)
        return target


def create_note(directory, content, title=None):
    folder = Path(directory).expanduser()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    title = title or next((line.strip().lstrip("# ") for line in content.splitlines() if line.strip()), "Untitled note")
    slug = re.sub(r"[^\w .-]", "", title, flags=re.UNICODE).strip(" .")[:70] or "Untitled note"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for index in range(1000):
        suffix = "" if index == 0 else f"-{index}"
        path = folder / f"{slug}-{stamp}{suffix}.md"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                if not content.endswith("\n"):
                    stream.write("\n")
            return path
        except FileExistsError:
            continue
    raise RuntimeError("Could not find an unused note filename.")
