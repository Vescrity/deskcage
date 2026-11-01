"""Sandbox profile model and TOML (de)serialization."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

from . import tomlwrite

HOME_MODES = ("tmp", "bind", "ro", "tmpbind")
BUS_MODES = ("none", "isolated", "bridge")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Groups offered by the profile editor.  ``[env-preset]`` switches whole groups
# of host variables on; every ``[env]`` entry is either a literal string or
# ``true`` (inherit that variable).
ENV_GROUPS: dict[str, tuple[str, ...]] = {
    "xdg": (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_DIRS",
        "XDG_CONFIG_DIRS",
    ),
    "session": (
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_NUMERIC",
        "LC_TIME",
        "LC_COLLATE",
        "LC_MONETARY",
        "LC_MESSAGES",
        "LC_PAPER",
        "LC_NAME",
        "LC_ADDRESS",
        "LC_TELEPHONE",
        "LC_MEASUREMENT",
        "LC_IDENTIFICATION",
        "TZ",
        "XDG_SESSION_TYPE",
        "XDG_SESSION_ID",
        "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_DESKTOP",
        "SHELL",
        "TERM",
        "COLORTERM",
    ),
    "theme": (
        "QT_QPA_PLATFORMTHEME",
        "QT_STYLE_OVERRIDE",
        "QT_QPA_PLATFORM",
        "QT_SCALE_FACTOR",
        "QT_SCREEN_SCALE_FACTORS",
        "QT_AUTO_SCREEN_SCALE_FACTOR",
        "QT_FONT_DPI",
        "GDK_BACKEND",
        "GDK_SCALE",
        "GDK_DPI_SCALE",
        "GTK_THEME",
        "XCURSOR_THEME",
        "XCURSOR_SIZE",
    ),
    "proxy": (
        "http_proxy",
        "https_proxy",
        "ftp_proxy",
        "all_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "FTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ),
}
DEFAULT_ENV_GROUPS = ("xdg", "session")


def default_presets() -> list[str]:
    return list(DEFAULT_ENV_GROUPS)



class ProfileError(ValueError):
    pass


def _as_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{what} must be a string")
    return value


def _as_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ProfileError(f"{what} must be a boolean")
    return value


def _as_str_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProfileError(f"{what} must be an array of strings")
    return list(value)


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ProfileError(f"{key} must be a table")
    return value


@dataclass
class Profile:
    id: str
    description: str = ""
    command: list[str] = field(default_factory=list)
    keep_alive: bool = True
    network: bool = True
    home: str = "tmp"
    hostname: str = ""
    extra_args: list[str] = field(default_factory=list)

    bus: str = "bridge"
    bus_pid_hint: bool = False

    readonly: list[str] = field(default_factory=list)
    readwrite: list[str] = field(default_factory=list)
    tmpfs: list[str] = field(default_factory=list)
    tmpbind: list[str] = field(default_factory=list)
    tmpbind_strict: bool = False

    dri: bool = True
    snd: bool = True
    video: bool = False
    nvidia: bool = False

    wayland: bool = True
    x11: bool = True
    pipewire: bool = True
    pulse: bool = True
    a11y: bool = False

    env_presets: list[str] = field(default_factory=default_presets)
    env: dict[str, str | bool] = field(default_factory=dict)

    # ------------------------------------------------------------------ load

    @classmethod
    def from_dict(cls, profile_id: str, data: dict) -> "Profile":
        if not isinstance(data, dict):
            raise ProfileError("profile must be a TOML table")
        bus = _table(data, "bus")
        files = _table(data, "files")
        devices = _table(data, "devices")
        desktop = _table(data, "desktop")
        env = _table(data, "env")
        preset_table = _table(data, "env-preset")
        if not all(isinstance(v, bool) for v in preset_table.values()):
            raise ProfileError("env-preset values must be booleans")
        for name in preset_table:
            if name not in ENV_GROUPS:
                raise ProfileError(f"unknown env preset: {name!r}")
        if not all(
            isinstance(k, str) and isinstance(v, (str, bool)) for k, v in env.items()
        ):
            raise ProfileError("env values must be strings or true")

        return cls(
            id=profile_id,
            description=_as_str(data.get("description", ""), "description"),
            command=_as_str_list(data.get("command", []), "command"),
            keep_alive=_as_bool(data.get("keep_alive", True), "keep_alive"),
            network=_as_bool(data.get("network", True), "network"),
            home=_as_str(data.get("home", "tmp"), "home"),
            hostname=_as_str(data.get("hostname", ""), "hostname"),
            extra_args=_as_str_list(data.get("extra_args", []), "extra_args"),
            bus=_as_str(bus.get("mode", "bridge"), "bus.mode"),
            bus_pid_hint=_as_bool(bus.get("pid_hint", False), "bus.pid_hint"),
            readonly=_as_str_list(files.get("readonly", []), "files.readonly"),
            readwrite=_as_str_list(files.get("readwrite", []), "files.readwrite"),
            tmpfs=_as_str_list(files.get("tmpfs", []), "files.tmpfs"),
            tmpbind=_as_str_list(files.get("tmpbind", []), "files.tmpbind"),
            tmpbind_strict=_as_bool(
                files.get("tmpbind_strict", False), "files.tmpbind_strict"
            ),
            dri=_as_bool(devices.get("dri", True), "devices.dri"),
            snd=_as_bool(devices.get("snd", True), "devices.snd"),
            video=_as_bool(devices.get("video", False), "devices.video"),
            nvidia=_as_bool(devices.get("nvidia", False), "devices.nvidia"),
            wayland=_as_bool(desktop.get("wayland", True), "desktop.wayland"),
            x11=_as_bool(desktop.get("x11", True), "desktop.x11"),
            pipewire=_as_bool(desktop.get("pipewire", True), "desktop.pipewire"),
            pulse=_as_bool(desktop.get("pulse", True), "desktop.pulse"),
            a11y=_as_bool(desktop.get("a11y", False), "desktop.a11y"),
            env_presets=[g for g in ENV_GROUPS if preset_table.get(g)],
            env=dict(env),
        )

    @classmethod
    def from_toml(cls, profile_id: str, text: str) -> "Profile":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ProfileError(f"invalid TOML: {exc}") from exc
        profile = cls.from_dict(profile_id, data)
        profile.validate()
        return profile

    # ------------------------------------------------------------------ dump

    def to_dict(self) -> dict:
        data = {
            "description": self.description,
            "command": list(self.command),
            "keep_alive": self.keep_alive,
            "network": self.network,
            "home": self.home,
            "hostname": self.hostname,
            "extra_args": list(self.extra_args),
            "bus": {"mode": self.bus, "pid_hint": self.bus_pid_hint},
            "files": {
                "readonly": list(self.readonly),
                "readwrite": list(self.readwrite),
                "tmpfs": list(self.tmpfs),
                "tmpbind": list(self.tmpbind),
                "tmpbind_strict": self.tmpbind_strict,
            },
            "devices": {
                "dri": self.dri,
                "snd": self.snd,
                "video": self.video,
                "nvidia": self.nvidia,
            },
            "desktop": {
                "wayland": self.wayland,
                "x11": self.x11,
                "pipewire": self.pipewire,
                "pulse": self.pulse,
                "a11y": self.a11y,
            },
        }
        presets = {group: True for group in ENV_GROUPS if group in self.env_presets}
        if presets:
            data["env-preset"] = presets
        if self.env:
            data["env"] = dict(self.env)
        return data

    def to_toml(self) -> str:
        return tomlwrite.dumps(self.to_dict())

    # ------------------------------------------------------------- validation

    def validate(self) -> None:
        if not _ID.match(self.id or ""):
            raise ProfileError(f"invalid profile id: {self.id!r}")
        if self.home not in HOME_MODES:
            raise ProfileError(f"home must be one of {', '.join(HOME_MODES)}")
        if self.bus not in BUS_MODES:
            raise ProfileError(f"bus.mode must be one of {', '.join(BUS_MODES)}")
        if not all(isinstance(item, str) for item in self.command):
            raise ProfileError("command must be an array of strings")
        if "--" in self.extra_args:
            raise ProfileError(
                "extra_args must not contain '--': it would end bwrap's options"
            )
        for group in self.env_presets:
            if group not in ENV_GROUPS:
                raise ProfileError(f"unknown env preset: {group!r}")
        for key, value in self.env.items():
            if not _ENV_KEY.match(key):
                raise ProfileError(f"invalid environment variable name: {key!r}")
            if value is False:
                raise ProfileError(f"env {key} must be a string or true")
            if not isinstance(value, (str, bool)):
                raise ProfileError(f"env {key} must be a string or true")

    def description_line(self) -> str:
        return self.description or self.id
