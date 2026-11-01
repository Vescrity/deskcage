"""Profile model: TOML round-trip, defaults and validation."""

from __future__ import annotations

import sys

import harness  # noqa: F401 - sets sys.path

from deskcage.profile import Profile, ProfileError


def roundtrip() -> None:
    profile = Profile(
        id="work",
        description="browser in a box",
        command=["firefox", "--new-instance"],
        keep_alive=True,
        network=False,
        home="bind",
        hostname="work-box",
        bus="isolated",
        bus_pid_hint=True,
        readonly=["~/.config/fontconfig"],
        readwrite=["~/Downloads", "/tmp/shared"],
        tmpfs=["/tmp/scratch"],
        tmpbind=["~/.config/QQ", "/tmp/scratch"],
        tmpbind_strict=True,
        dri=False,
        snd=False,
        video=True,
        nvidia=True,
        wayland=True,
        x11=False,
        pipewire=False,
        pulse=True,
        a11y=True,
        env={"FOO": "bar", "QUOTED": 'a "b" \\ c\nd', "PASSED": True},
        extra_args=["--unshare-net"],
    )
    text = profile.to_toml()
    again = Profile.from_toml("work", text)
    assert again.to_dict() == profile.to_dict(), (again.to_dict(), profile.to_dict())
    assert again.description == "browser in a box"
    assert again.bus == "isolated" and again.bus_pid_hint is True
    assert again.env["QUOTED"] == 'a "b" \\ c\nd'
    assert again.env["PASSED"] is True
    assert again.keep_alive is True
    assert again.tmpbind == ["~/.config/QQ", "/tmp/scratch"]
    assert again.tmpbind_strict is True


def array_layout() -> None:
    """Non-empty arrays are written one element per line, empty ones inline."""

    profile = Profile(
        id="layout",
        command=["sh", "-c", "true"],
        readonly=["/a", "/b"],
        tmpfs=[],
        env={"FOO": "bar"},
    )
    text = profile.to_toml()
    assert 'command = [\n    "sh",\n    "-c",\n    "true",\n]' in text, text
    assert 'readonly = [\n    "/a",\n    "/b",\n]' in text, text
    assert "tmpfs = []" in text, text
    assert Profile.from_toml("layout", text).to_dict() == profile.to_dict()


def defaults() -> None:
    profile = Profile.from_toml("plain", "")
    assert profile.description == ""
    assert profile.description_line() == "plain"
    assert profile.bus == "bridge"
    assert profile.home == "tmp"
    assert profile.network is True
    assert profile.keep_alive is True
    assert profile.env == {}
    assert profile.tmpbind == [] and profile.tmpbind_strict is False
    assert profile.to_dict()["bus"] == {"mode": "bridge", "pid_hint": False}


def new_profile_env() -> None:
    profile = Profile(id="fresh")
    assert profile.env_presets == ["xdg", "session"]
    assert profile.env == {}
    text = profile.to_toml()
    assert "[env-preset]\nxdg = true\nsession = true\n" in text, text
    assert "LANG" not in text, text


def env_groups_complete() -> None:
    from deskcage.profile import DEFAULT_ENV_GROUPS, ENV_GROUPS, default_presets

    assert set(default_presets()) == set(DEFAULT_ENV_GROUPS)
    assert not set(ENV_GROUPS["xdg"]) & set(ENV_GROUPS["proxy"])
    assert "XDG_RUNTIME_DIR" not in ENV_GROUPS["xdg"]


def env_presets_roundtrip() -> None:
    text = (
        "[env-preset]\n"
        "proxy = true\n"
        "theme = true\n"
        "\n"
        "[env]\n"
        'LANG = "C"\n'
        "TERM = true\n"
    )
    profile = Profile.from_toml("p", text)
    assert profile.env_presets == ["theme", "proxy"]
    assert profile.env == {"LANG": "C", "TERM": True}
    dumped = Profile.from_toml("p", profile.to_toml())
    assert dumped.env_presets == ["theme", "proxy"]
    assert dumped.env == {"LANG": "C", "TERM": True}


def invalid() -> None:
    cases = [
        '[bus]\nmode = "carrier-pigeon"\n',
        'home = "elsewhere"\n',
        'command = "not-a-list"\n',
        '[devices]\ndri = "yes"\n',
        '[devices]\nnvidia = "yes"\n',
        '[desktop]\na11y = "yes"\n',
        '[env]\nFOO = 1\n',
        '[env]\nFOO = false\n',
        '[env]\n"bad key" = "x"\n',
        '[env-preset]\nxdg = "yes"\n',
        '[env-preset]\nnope = true\n',
        'extra_args = ["--"]\n',
        '[files]\ntmpbind_strict = "yes"\n',
        "not toml at all =",
    ]
    for text in cases:
        try:
            Profile.from_toml("bad", text)
        except ProfileError:
            continue
        raise AssertionError(f"accepted invalid profile: {text!r}")

    try:
        Profile(id="bad id").validate()
    except ProfileError:
        pass
    else:
        raise AssertionError("accepted an invalid profile id")


def main() -> int:
    roundtrip()
    array_layout()
    defaults()
    new_profile_env()
    env_groups_complete()
    env_presets_roundtrip()
    invalid()
    print("toml: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
