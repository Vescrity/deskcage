"""Shell completion for the ``dcage`` command line.

The shell scripts are tiny wrappers that call the hidden ``dcage _complete``
command, so bash and zsh share one implementation.  The command line itself
(commands, options, ``choices``) is read back from :func:`deskcage.cli.build_parser`,
which keeps completion in sync with the real parser; only the arguments whose
values live outside argparse are listed here.
"""

from __future__ import annotations

import argparse
import os

from . import config, logs

SHELLS = ("bash", "zsh")

# Argument destinations whose values come from somewhere else than argparse.
PROFILE_DESTS = {"profile", "from_profile"}
INSTANCE_DESTS = {"instance"}

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


# ------------------------------------------------------------------- files


def script(shell: str) -> str:
    """The completion script shipped with the package."""

    if shell not in SHELLS:
        raise ValueError(f"unsupported shell: {shell}")
    name = {"bash": "dcage", "zsh": "_dcage"}[shell]
    from importlib import resources

    return (
        resources.files("deskcage")
        .joinpath("data", name)
        .read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------- candidates


def candidates(words: list[str], cword: int) -> list[str]:
    """Completion candidates for ``words[cword]`` (``words`` excludes ``dcage``)."""

    words = list(words)
    if cword < 0:
        cword = 0
    while len(words) <= cword:
        words.append("")
    head = words[:cword]
    prefix = words[cword]
    if "--" in head:
        # Everything after ``--`` is a command line for the sandbox.
        return []

    from .cli import build_parser

    root = build_parser()
    node, chain, consumed = _locate(root, head)

    # ``--option value``: the value of the option right before the cursor.
    if head:
        action = _option_action(node, root, head[-1])
        if action is not None and _takes_value(action) and "=" not in head[-1]:
            return _filter(_values(action, chain, head, prefix), prefix)

    # ``--option=value``: complete the value and keep the option attached.
    option, equals, value_prefix = prefix.partition("=")
    if equals and option.startswith("-"):
        action = _option_action(node, root, option)
        if action is not None and _takes_value(action):
            values = _values(action, chain, head, value_prefix)
            return [f"{option}={value}" for value in _filter(values, value_prefix)]

    if prefix.startswith("-"):
        return _filter(_option_strings(node, root), prefix)

    subcommands = _subparsers(node)
    if subcommands is not None and not consumed:
        names = [name for name in subcommands.choices if name != "_complete"]
        return _filter(names, prefix)

    positionals = _positionals(node)
    if not positionals:
        return []
    index = len(consumed)
    if index >= len(positionals):
        return []
    action = positionals[index]
    return _filter(_values(action, chain, head, prefix), prefix)


def _locate(root: argparse.ArgumentParser, head: list[str]):
    """Follow ``head`` to the active parser, its command path and its positionals."""

    node = root
    chain: list[str] = []
    consumed: list[str] = []
    index = 0
    while index < len(head):
        token = head[index]
        if token == "--":
            break
        subcommands = _subparsers(node)
        if (
            subcommands is not None
            and not token.startswith("-")
            and token in subcommands.choices
        ):
            node = subcommands.choices[token]
            chain.append(token)
            consumed = []
            index += 1
            continue
        if token.startswith("-"):
            action = _option_action(node, root, token.split("=", 1)[0])
            if action is not None and _takes_value(action) and "=" not in token:
                index += 2
            else:
                index += 1
            continue
        consumed.append(token)
        index += 1
    return node, chain, consumed


# ------------------------------------------------------------------ sources


def _values(
    action: argparse.Action, chain: list[str], head: list[str], prefix: str
) -> list[str]:
    if action.choices:
        return [str(choice) for choice in action.choices]
    dest = action.dest
    if dest in PROFILE_DESTS:
        return _profiles()
    if dest == "id" and chain[:1] == ["profile"] and chain[1:2] != ["create"]:
        return _profiles()
    if dest == "env":
        # The value is ``KEY=VALUE``: complete the key and leave the word open
        # for the value, which the shells do when a candidate ends in ``=``.
        return [f"{name}=" for name in sorted(os.environ)]
    if dest == "cwd":
        return _directories(prefix)
    if dest == "log_level":
        return list(LOG_LEVELS)
    if dest in INSTANCE_DESTS:
        if not action.option_strings:
            return _instances(include_logs=chain[:1] == ["logs"])
        # ``start --instance`` only makes sense together with ``--reuse``.
        return _instances() if "--reuse" in head else []
    return []


def _profiles() -> list[str]:
    directory = config.profiles_dir()
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return sorted(entry.stem for entry in entries if entry.suffix == ".toml")


def _instances(include_logs: bool = False) -> list[str]:
    """Live instance names; never starts a daemon just to complete a word."""

    names: list[str] = []
    try:
        from .client import ManagerClient

        client = ManagerClient.connect(autostart=False)
        names = [row[0] for row in client.list_instances(timeout=1000)]
    except Exception:  # noqa: BLE001 - completion must never fail loudly
        names = []
    if include_logs:
        names = sorted(set(names) | set(logs.instances()))
    return sorted(set(names))


def _directories(prefix: str) -> list[str]:
    expanded = os.path.expanduser(prefix)
    directory, base = os.path.split(expanded)
    try:
        entries = sorted(os.listdir(directory or "."))
    except OSError:
        return []
    results: list[str] = []
    keep = prefix[: len(prefix) - len(base)]
    for name in entries:
        if not name.startswith(base):
            continue
        if not os.path.isdir(os.path.join(directory or ".", name)):
            continue
        results.append(keep + name + os.sep)
    return results


# ------------------------------------------------------------------ helpers


def _subparsers(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _positionals(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [
        action
        for action in parser._actions
        if not action.option_strings
        and not isinstance(action, argparse._SubParsersAction)
    ]


def _option_strings(
    node: argparse.ArgumentParser, root: argparse.ArgumentParser
) -> list[str]:
    options: list[str] = []
    for parser in (node, root):
        for action in parser._actions:
            options.extend(action.option_strings)
    return sorted(set(options))


def _option_action(
    node: argparse.ArgumentParser, root: argparse.ArgumentParser, token: str
):
    for parser in (node, root):
        for action in parser._actions:
            if token in action.option_strings:
                return action
    return None


def _takes_value(action: argparse.Action) -> bool:
    return action.nargs != 0 and action.option_strings != []


def _filter(values: list[str], prefix: str) -> list[str]:
    return [value for value in values if value.startswith(prefix)]
