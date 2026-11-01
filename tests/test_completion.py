"""Shell completion: the candidate engine, the shipped scripts and ``_complete``."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import harness

from deskcage import complete, config


def engine(env: harness.TestEnv) -> None:
    env.cli("profile", "create", "work", "--bus", "isolated", "--command", "sleep",
            "--command", "60")
    env.cli("profile", "create", "play", "--bus", "none", "--command", "sleep",
            "--command", "60")

    # Commands and profile subcommands come from the real parser.
    commands = complete.candidates([""], 0)
    assert "start" in commands and "completion" in commands, commands
    assert "_complete" not in commands, commands
    assert complete.candidates(["sta"], 0) == ["start"]
    assert complete.candidates(["profile", ""], 1) == [
        "list", "show", "path", "command", "create", "duplicate", "rename",
        "edit", "delete",
    ]
    assert complete.candidates(["profile", "sh"], 1) == ["show"]

    # Options, including the inline ``--opt=value`` form.
    flags = complete.candidates(["start", "--"], 1)
    assert {"--instance", "--reuse", "--keep", "--env", "--cwd"} <= set(flags), flags
    assert complete.candidates(["profile", "create", "x", "--bus", ""], 4) == [
        "none", "isolated", "bridge",
    ]
    assert complete.candidates(["profile", "create", "x", "--bus=iso"], 3) == [
        "--bus=isolated",
    ]
    assert complete.candidates(["keep", "work", ""], 2)[:2] == ["on", "off"]
    assert complete.candidates(
        ["profile", "create", "x", "--home", "tm"], 4
    ) == ["tmp", "tmpbind"]
    assert complete.candidates(
        ["profile", "create", "x", "--from", ""], 4
    ) == ["play", "work"]
    assert complete.candidates(["daemon", "--log-level", "WA"], 2) == ["WARNING"]
    # Flags that take no value never offer one.
    assert complete.candidates(["logs", "-f", ""], 2) == []

    # Profiles, but only where a profile name is expected.
    assert complete.candidates(["start", "wo"], 1) == ["work"]
    assert complete.candidates(["profile", "show", ""], 2) == ["play", "work"]
    assert complete.candidates(["profile", "delete", ""], 2) == ["play", "work"]
    assert complete.candidates(["profile", "create", ""], 2) == []
    assert complete.candidates(["profile", "duplicate", "work", ""], 3) == []
    assert complete.candidates(["profile", "rename", ""], 2) == ["play", "work"]
    assert complete.candidates(["profile", "rename", "work", ""], 3) == []

    # Values that live on the host: environment names and directories.
    os.environ["DCAGE_COMPLETE_TEST"] = "1"
    try:
        names = complete.candidates(["exec", "work", "--env", "DCAGE_COMPLETE"], 3)
        assert names == ["DCAGE_COMPLETE_TEST="], names
        assert complete.candidates(["exec", "work", "--env=DCAGE_COMPLETE"], 2) == [
            "--env=DCAGE_COMPLETE_TEST=",
        ]
        # The key is completed, not the value: ``KEY=VALUE`` gets no proposals.
        typed = ["exec", "work", "--env", "DCAGE_COMPLETE_TEST=1"]
        assert complete.candidates(typed, 3) == []
    finally:
        os.environ.pop("DCAGE_COMPLETE_TEST", None)
    assert complete.candidates(["exec", "work", "--cwd", "/tm"], 3) == ["/tmp/"]

    # A ``--`` belongs to the sandbox command line: no candidates at all.
    assert complete.candidates(["start", "work", "--", ""], 3) == []
    assert complete.candidates(["exec", "work", "--", "fi"], 3) == []

    # Live instances; ``logs`` also knows names whose sandbox is gone.
    instance = env.cli("start", "work", "--instance", "web").stdout.strip()
    assert instance == "web"
    assert complete.candidates(["stop", "w"], 1) == ["web"]
    assert complete.candidates(["exec", ""], 1) == ["web"]
    assert complete.candidates(["logs", ""], 1) == ["web"]
    env.cli("stop", "web")
    assert complete.candidates(["stop", ""], 1) == []
    assert complete.candidates(["logs", ""], 1) == ["web"]

    # ``--instance`` only suggests existing names together with ``--reuse``.
    assert complete.candidates(["start", "work", "--instance", ""], 3) == []
    instance = env.cli("start", "work", "--instance", "again").stdout.strip()
    assert complete.candidates(
        ["start", "work", "--reuse", "--instance", ""], 4
    ) == ["again"]
    env.cli("stop", "again")

    # The hidden helper is what the shell calls; it must stay quiet on junk.
    assert env.cli("_complete", "0").stdout.split()[:1] == ["daemon"]
    flags = env.cli("_complete", "1", "start", "--").stdout.split()
    assert "--reuse" in flags, flags
    # A literal ``--`` already on the line is not swallowed by argparse.
    assert env.cli("_complete", "3", "start", "work", "--", "").stdout == ""
    assert env.cli("_complete", "not-a-number", check=False).returncode != 0
    assert "_complete" not in env.cli("--help").stdout


def no_autostart() -> None:
    """Completing a word never starts the daemon behind the user's back."""

    with harness.TestEnv() as env:
        assert not config.daemon_pid_path().exists()
        assert complete.candidates(["stop", ""], 1) == []
        # Without a daemon the log directory is still a useful source.
        directory = config.logs_root() / "released"
        directory.mkdir(parents=True)
        (directory / "1-1.log").write_text("old\n", encoding="utf-8")
        assert complete.candidates(["logs", ""], 1) == ["released"]
        assert complete.candidates(["stop", ""], 1) == []
        assert not config.daemon_pid_path().exists()


def scripts(env: harness.TestEnv) -> None:
    bash = env.cli("completion", "bash").stdout
    assert "complete -o default -F _dcage dcage" in bash, bash
    assert "COMP_CWORD - 1" in bash, bash

    zsh = env.cli("completion", "zsh").stdout
    assert zsh.startswith("#compdef dcage"), zsh
    assert "compadd -a candidates" in zsh, zsh
    assert "compdef _dcage dcage" in zsh, zsh

    assert env.cli("completion", "fish", check=False).returncode != 0

    if shutil.which("zsh"):
        path = env.dir / "_dcage"
        path.write_text(zsh, encoding="utf-8")
        checked = subprocess.run(
            ["zsh", "-n", str(path)], text=True, capture_output=True, timeout=30
        )
        assert checked.returncode == 0, checked.stderr
        zsh_end_to_end(env, path)


def zsh_end_to_end(env: harness.TestEnv, script: Path) -> None:
    """Run the zsh function with stubbed widgets, so no terminal is needed."""

    driver = env.dir / "driver.zsh"
    driver.write_text(
        "compdef() { : }\n"
        "compadd() {\n"
        "    [[ $1 == '-S' && $2 == '' ]] && print -r -- nospace\n"
        "    print -r -- \"compadd:${candidates[*]}\"\n"
        "}\n"
        "_files() { print -r -- files }\n"
        "dcage() {\n"
        "    if [[ $MODE == flags ]]; then\n"
        "        [[ \"$*\" == '_complete 1 start --' ]] && {\n"
        "            print -r -- --reuse\n"
        "            print -r -- --keep\n"
        "        }\n"
        "    else\n"
        "        print -r -- 'PATH='\n"
        "    fi\n"
        "    return 0\n"
        "}\n"
        f"source {shlex.quote(str(script))}\n"
        "MODE=flags; words=(dcage start --); CURRENT=3; _dcage\n"
        "MODE=flags; words=(dcage start demo --instance ''); CURRENT=5; _dcage\n"
        "MODE=env; words=(dcage exec demo --env ''); CURRENT=5; _dcage\n",
        encoding="utf-8",
    )
    checked = subprocess.run(
        ["zsh", "-f", str(driver)], text=True, capture_output=True, timeout=60
    )
    assert checked.returncode == 0, checked.stderr
    lines = checked.stdout.splitlines()
    # ``CURRENT - 2`` must line up with the bash mapping, an empty answer must
    # fall back to file completion instead of one empty candidate, and a
    # ``KEY=`` candidate must keep the word open for the value.
    assert lines == [
        "compadd:--reuse --keep",
        "files",
        "nospace",
        "compadd:PATH=",
    ], lines


def bash_end_to_end(env: harness.TestEnv) -> None:
    """Source the real script and run the completion function like bash would."""

    bash = shutil.which("bash")
    if not bash:
        return
    if "work" not in env.cli("profile", "list").stdout:
        env.cli("profile", "create", "work", "--bus", "none", "--command", "sleep",
                "--command", "60")
    script = env.cli("completion", "bash").stdout
    script_path = env.dir / "dcage.bash"
    script_path.write_text(script, encoding="utf-8")

    bin_dir = env.dir / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "dcage"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} -m deskcage \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = env.env()
    environment["PATH"] = f"{bin_dir}:{environment.get('PATH', '')}"

    marker = env.dir / "nospace.marker"

    def shell(words: list[str], cword: int) -> tuple[list[str], bool]:
        holder = " ".join(shlex.quote(word) for word in words)
        marker.write_text("", encoding="utf-8")
        environment["DCAGE_TEST_MARKER"] = str(marker)
        code = (
            f"source {shlex.quote(str(script_path))}\n"
            # Shadow the builtin so the nospace decision can be observed; the
            # script silences compopt's stderr, hence the file.
            "compopt() { [[ $1 == -o && $2 == nospace ]] &&"
            ' echo NOSPACE >> "$DCAGE_TEST_MARKER"; }\n'
            f"COMP_WORDS=({holder})\n"
            f"COMP_CWORD={cword}\n"
            "_dcage\n"
            "printf '%s\\n' \"${COMPREPLY[@]}\"\n"
        )
        result = subprocess.run(
            [bash, "-c", code], env=environment, text=True, capture_output=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.split(), "NOSPACE" in marker.read_text(encoding="utf-8")

    def run(words: list[str], cword: int) -> list[str]:
        return shell(words, cword)[0]

    def nospace(words: list[str], cword: int) -> bool:
        return shell(words, cword)[1]

    assert run(["dcage", "sta"], 1) == ["start"]
    assert run(["dcage", "profile", "sh"], 2) == ["show"]
    assert "work" in run(["dcage", "start", ""], 2)
    assert "--reuse" in run(["dcage", "start", "--"], 2)

    # ``KEY=`` and ``dir/`` keep the cursor in the word; finished words get a
    # space like any other completion.
    assert "PATH=" in run(["dcage", "exec", "work", "--env", ""], 4)
    assert nospace(["dcage", "exec", "work", "--env", ""], 4)
    assert run(["dcage", "exec", "work", "--cwd", "/tm"], 4) == ["/tmp/"]
    assert nospace(["dcage", "exec", "work", "--cwd", "/tm"], 4)
    assert not nospace(["dcage", "start", ""], 1)

    instance = env.cli("start", "work", "--instance", "web").stdout.strip()
    assert run(["dcage", "stop", "w"], 2) == ["web"]
    assert "web" in run(["dcage", "logs", ""], 2)
    env.cli("stop", instance)
    assert run(["dcage", "stop", "w"], 2) == []
    assert "web" in run(["dcage", "logs", ""], 2)


def main() -> int:
    no_autostart()
    with harness.TestEnv() as env:
        engine(env)
        scripts(env)
        bash_end_to_end(env)
    print("completion: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
