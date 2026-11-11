"""Filesystem layout, root-owned file checks and atomic JSON I/O."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import ConfigError

PACKAGE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PACKAGE_DIR / "data"


@dataclass(frozen=True, slots=True)
class Paths:
    root: Path = Path("/")

    @classmethod
    def default(cls) -> "Paths":
        """Return system paths, or an explicit test root for root-owned tests.

        ``CS2BLOCKER_TEST_ROOT`` is honored only for uid 0.  pkexec strips the
        environment, and an unprivileged user cannot use this to redirect a
        privileged helper anywhere.
        """
        override = os.environ.get("CS2BLOCKER_TEST_ROOT")
        if override and os.geteuid() == 0:
            root = Path(override).expanduser().resolve()
            if not root.is_dir():
                raise ConfigError(f"CS2BLOCKER_TEST_ROOT is not a directory: {root}")
            return cls(root)
        return cls(Path("/"))

    @classmethod
    def for_root(cls, root: Path | str) -> "Paths":
        return cls(Path(root).resolve())

    @property
    def config_dir(self) -> Path:
        return self.root / "etc/cs2blocker"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def state_dir(self) -> Path:
        return self.root / "var/lib/cs2blocker"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    def cache_file(self, appid: int = 730) -> Path:
        return self.state_dir / f"sdr-{int(appid)}.json"

    @property
    def run_dir(self) -> Path:
        return self.root / "run/cs2blocker"

    @property
    def status_file(self) -> Path:
        return self.run_dir / "status.json"

    @property
    def lock_file(self) -> Path:
        return self.run_dir / "lock"

    @property
    def config_dir_mode(self) -> int:
        return 0o755

    def ensure_dirs(self) -> None:
        for directory, mode in (
            (self.config_dir, 0o755),
            (self.state_dir, 0o755),
            (self.run_dir, 0o755),
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                # A read-only bind mount (systemd ProtectSystem) is fine if
                # the directory already exists; only fail if it is missing.
                if not directory.is_dir():
                    raise
            try:
                if stat.S_IMODE(directory.stat().st_mode) != mode:
                    os.chmod(directory, mode)
            except OSError:
                # EROFS on /etc or /var is expected for the monitor unit,
                # which only needs /run; an existing directory is enough.
                if not directory.is_dir():
                    raise
            if os.geteuid() == 0:
                st = directory.stat()
                if st.st_uid != 0:
                    raise ConfigError(f"refusing state directory not owned by root: {directory}")
                if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise ConfigError(f"refusing group/world-writable state directory: {directory}")

    @staticmethod
    def bundled_snapshot(appid: int = 730) -> Path:
        return DATA_DIR / f"sdr-{int(appid)}.json"

    @staticmethod
    def bundled_presets() -> Path:
        return DATA_DIR / "presets.toml"


def _check_secure_stat(path: Path, st: os.stat_result, *, require_root: bool) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"refusing non-regular file: {path}")
    if require_root and st.st_uid != 0:
        raise ConfigError(f"refusing file not owned by root: {path}")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(f"refusing group/world-writable file: {path}")


def read_json(
    path: Path,
    *,
    require_root: bool = True,
    missing_ok: bool = False,
) -> Any | None:
    """Read JSON through an O_NOFOLLOW descriptor and validate ownership first."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ConfigError(f"missing file: {path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot open {path}: {exc}") from exc
    try:
        st = os.fstat(fd)
        _check_secure_stat(path, st, require_root=require_root)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def write_json(
    path: Path,
    data: Any,
    *,
    mode: int = 0o644,
    fsync_dir: bool = True,
) -> None:
    """Write JSON atomically: temp file in the same directory, fsync, replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if fsync_dir:
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def lock(paths: Paths, *, timeout: float | None = None) -> Iterator[None]:
    """Exclusive lock shared by apply/refresh/clear (and monitor startup)."""
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if timeout is not None:
            import time

            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ConfigError("another cs2blocker operation is running")
                    time.sleep(0.05)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_optional_json(path: Path, *, require_root: bool = False) -> Any | None:
    return read_json(path, require_root=require_root, missing_ok=True)
