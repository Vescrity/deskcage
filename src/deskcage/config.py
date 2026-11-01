"""Filesystem locations, profile storage and host helpers."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

APP = "dcage"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConfigError(RuntimeError):
    pass


def _xdg(name: str, fallback: str) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else Path(os.path.expanduser(fallback))


def config_root() -> Path:
    return _xdg("XDG_CONFIG_HOME", "~/.config") / APP


def profiles_dir() -> Path:
    return config_root() / "profiles"


def runtime_root() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / APP
    return Path(tempfile.gettempdir()) / f"{APP}-{os.getuid()}"


def logs_root() -> Path:
    """Per-instance log directories, kept after an instance is gone."""

    return runtime_root() / "logs"


def ensure_runtime_dir() -> Path:
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def daemon_log_path() -> Path:
    """Where a background daemon writes its stdout/stderr.

    The default is inside the runtime directory (tmpfs, gone at logout).  Set
    ``DESKCAGE_DAEMON_LOG`` to a path to keep it on disk, or to ``-`` / ``off`` to
    discard it.
    """

    value = os.environ.get("DESKCAGE_DAEMON_LOG")
    if value:
        if value.strip().lower() in ("-", "off", "none", "discard"):
            return Path(os.devnull)
        return Path(value).expanduser()
    return runtime_root() / "dcaged.log"


def daemon_pid_path() -> Path:
    return runtime_root() / "dcaged.pid"


def _global_config() -> dict:
    path = config_root() / "config.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def find_bus_bridge() -> str | None:
    """Locate the ``bus-bridge`` command used for ``bus = "bridge"`` profiles."""
    override = os.environ.get("DESKCAGE_BUS_BRIDGE")
    if override:
        return override
    configured = _global_config().get("bus", {}).get("bridge")
    if configured:
        return os.path.expanduser(str(configured))
    return shutil.which("bus-bridge")


def host_bus_address() -> str:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if address:
        return address
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidate = Path(runtime) / "bus"
        if candidate.exists():
            return f"unix:path={candidate}"
    raise ConfigError(
        "no session bus: set DBUS_SESSION_BUS_ADDRESS or XDG_RUNTIME_DIR"
    )


class Store:
    """Profiles are ``<root>/<id>.toml``."""

    def __init__(self, root: Path | None = None):
        self.root = root or profiles_dir()

    @staticmethod
    def validate_id(profile_id: str) -> str:
        if not _SAFE_ID.match(profile_id or ""):
            raise ConfigError(f"invalid profile id: {profile_id!r}")
        return profile_id

    def path(self, profile_id: str) -> Path:
        return self.root / f"{self.validate_id(profile_id)}.toml"

    def exists(self, profile_id: str) -> bool:
        return self.path(profile_id).is_file()

    def list_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            entry.stem
            for entry in self.root.iterdir()
            if entry.is_file() and entry.suffix == ".toml"
        )

    def load(self, profile_id: str):
        from .profile import Profile, ProfileError

        path = self.path(profile_id)
        if not path.is_file():
            raise ProfileError(f"unknown profile: {profile_id}")
        with path.open("r", encoding="utf-8") as handle:
            return Profile.from_toml(profile_id, handle.read())

    def save(self, profile) -> None:
        profile.validate()
        path = self.path(profile.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = profile.to_toml()
        # Parse it back before touching the real file, so a writer bug can
        # never corrupt an existing profile.
        from .profile import Profile

        Profile.from_toml(profile.id, text)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def delete(self, profile_id: str) -> None:
        path = self.path(profile_id)
        for candidate in (path, path.with_suffix(".toml.tmp")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass

    def rename(self, profile_id: str, new_id: str) -> str:
        """Move a profile to another id and return the new one.

        The new file is written (and parsed back) before the old one goes
        away, so an interruption leaves both files rather than neither.  The
        id lives in the file name only, but the profile is re-saved instead of
        renamed on disk so validation and the strict permissions still apply.
        """

        from .profile import ProfileError

        if not self.path(profile_id).is_file():
            raise ProfileError(f"unknown profile: {profile_id}")
        target = self.path(new_id)
        if target == self.path(profile_id):
            return new_id
        if target.exists():
            raise ProfileError(f"profile already exists: {new_id}")
        profile = self.load(profile_id)
        profile.id = new_id
        self.save(profile)
        self.delete(profile_id)
        return new_id

    def new_id(self, base: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", base.strip().lower()).strip("-._")
        slug = slug or "profile"
        candidate = slug
        index = 2
        while self.exists(candidate):
            candidate = f"{slug}-{index}"
            index += 1
        return candidate
