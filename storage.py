"""
Shared persistence helpers: atomic JSON writes, loud-failure JSON reads,
and a cross-process single-instance lock.

Every state/queue/history file in the project goes through these so that:
- a crash mid-write can never corrupt a file (write to a temp file in the
  same directory, then os.replace() — atomic on POSIX)
- a corrupt file is backed up and reported LOUDLY instead of silently
  resetting to defaults (which previously could reset the daily upload
  cap and let the agent over-post)
- the agent loop and the dashboard can never run a pipeline cycle at the
  same time (double uploads, clobbered work files)
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON atomically: temp file in the same dir + os.replace().

    Raises OSError on failure (callers decide whether that's fatal).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any) -> Any:
    """Read a JSON state file. Missing file -> default (normal first run).

    A CORRUPT file is backed up alongside the original and reported as an
    error — never silently swallowed, because 'history reset to empty'
    also resets the daily upload cap.
    """
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            os.replace(path, backup)
            logger.error(
                "STATE FILE CORRUPT: %s (%s). Backed it up to %s and starting "
                "with defaults — daily counts/history may need restoring from "
                "the backup before publishing resumes.", path, e, backup)
        except OSError:
            logger.error(
                "STATE FILE CORRUPT: %s (%s). Could not back it up; "
                "continuing with defaults.", path, e)
        return default
    except OSError as e:
        logger.error("Could not read state file %s: %s — using defaults.", path, e)
        return default


class InstanceLock:
    """Advisory cross-process lock so only one pipeline can run at a time.

    Used by both the CLI agent (run/once modes) and the dashboard's
    run-a-cycle endpoint. flock() releases automatically if the process
    dies, so a crash never leaves a stale lock.
    """

    def __init__(self, lock_dir: Path, name: str = "agent"):
        self.path = Path(lock_dir) / f"{name}.lock"
        self._fh = None

    def acquire(self) -> bool:
        """Try to take the lock. Returns False if another process holds it."""
        try:
            import fcntl
        except ImportError:
            # Non-POSIX platform: degrade gracefully rather than block runs.
            logger.warning("fcntl unavailable — running without a cross-process lock")
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        except OSError as e:
            # e.g. a filesystem without flock support: warn, don't hard-block.
            logger.warning("Lock file %s unusable (%s) — proceeding without lock",
                           self.path, e)
            fh.close()
            return True
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self._fh.close()
            self._fh = None
