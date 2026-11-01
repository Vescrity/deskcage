"""Instance logs: one directory per instance under ``$XDG_RUNTIME_DIR/dcage/logs``.

An instance keeps its logs when it exits, so ``dcage logs`` works after the
sandbox is gone.
"""

from __future__ import annotations

from pathlib import Path

from . import config

# Files that live next to the process logs but are not process output.
DIAGNOSTIC = ("bus.log", "sandbox.log")


def directory(instance: str) -> Path:
    return config.logs_root() / instance


def instances() -> list[str]:
    """Instance names with logs on disk, including released ones."""

    root = config.logs_root()
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def _order(entry: Path) -> tuple:
    numbers: list[int] = []
    for part in entry.stem.split("-"):
        try:
            numbers.append(int(part))
        except ValueError:
            numbers.append(0)
    return (numbers, entry.name)


def files(instance: str) -> list[tuple[str, Path]]:
    """``(name, path)`` for every process log of one instance, oldest first.

    ``bus.log`` and ``sandbox.log`` stay in the directory as diagnostics but
    are not process output, so ``dcage logs`` does not dump them.
    """

    root = directory(instance)
    if not root.is_dir():
        return []
    entries = [
        entry
        for entry in root.iterdir()
        if entry.is_file() and entry.suffix == ".log" and entry.name not in DIAGNOSTIC
    ]
    return [(entry.name, entry) for entry in sorted(entries, key=_order)]


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def text(instance: str) -> str:
    """All logs of one instance, each file behind a header line."""

    chunks: list[str] = []
    for name, path in files(instance):
        body = read(path)
        if not body:
            continue
        chunks.append(f"=== {name} ===")
        chunks.append(body.rstrip("\n"))
        chunks.append("")
    return "\n".join(chunks)
