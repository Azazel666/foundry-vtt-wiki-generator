"""JSON state on NFS: atomic writes and the single-run lock."""

import json
import logging
import os
import re
import socket
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STALE_LOCK_SECONDS = 12 * 3600  # longer than any run: the window and the LLM deadline fit in a night


class Clock:
    """Wall clock and sleeping, replaced by a fake in tests."""

    def __init__(self, tz) -> None:
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def safe_name(value: str) -> str:
    """One path segment from a package id or version string."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", value).strip(".")
    return name or "unknown"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("state_file_unreadable path=%s error=%s", path, exc)
        return default


class LockHeld(Exception):
    pass


class RunLock:
    """Exclusive create of run.lock; a lock older than STALE_LOCK_SECONDS belonged to a dead run."""

    def __init__(self, path: Path, stale_after: float = STALE_LOCK_SECONDS) -> None:
        self.path = path
        self.stale_after = stale_after

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._take():
            self._drop_if_stale()
            if not self._take():
                raise LockHeld(str(self.path))
        return self

    def __exit__(self, *_exc) -> None:
        self.path.unlink(missing_ok=True)

    def _take(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"host": socket.gethostname(), "pid": os.getpid(), "started": time.time()}, handle)
        return True

    def _drop_if_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if age > self.stale_after:
            log.warning("stale_lock_removed path=%s age_s=%d", self.path, age)
            self.path.unlink(missing_ok=True)
