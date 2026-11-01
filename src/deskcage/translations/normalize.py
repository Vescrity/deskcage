#!/usr/bin/env python3
"""Re-indent a Qt ``.ts`` catalogue after lupdate.

lupdate 6 writes a flat file: every element of a ``<context>`` sits at the same
indentation and sibling contexts are glued together as ``</context><context>``
on one line.  That is valid XML but unreadable in a diff, so ``make`` runs this
after lupdate and the result is what gets committed.

Only the indentation of lines that start with a tag changes, so the literal
newlines inside multi-line translations keep their text exactly as translated.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TAG = re.compile(r"<(/?)([A-Za-z_][\w.:-]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*?)(/?)>")


def indent_for(line: str, depth: int) -> int:
    stripped = line.lstrip()
    if stripped.startswith("</"):
        return max(depth - 1, 0)
    return depth


def depth_delta(line: str) -> int:
    """Net element depth change of one line, ignoring text around the tags."""

    delta = 0
    for match in TAG.finditer(line):
        closing, _name, _attributes, self_closing = match.groups()
        if closing:
            delta -= 1
        elif not self_closing:
            delta += 1
    return delta


def normalize(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        # lupdate glues sibling contexts together on a single line.
        merged = raw.replace("</context><context>", "</context>\n<context>")
        lines.extend(merged.splitlines())

    out: list[str] = []
    depth = 0
    for raw in lines:
        token = raw.strip()
        if token.startswith("<"):
            out.append("    " * indent_for(raw, depth) + token)
        else:
            # Header, or the continuation of a multi-line translation.
            out.append(raw)
        depth = max(depth + depth_delta(raw), 0)
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    for name in argv:
        path = Path(name)
        text = path.read_text(encoding="utf-8")
        normalized = normalize(text)
        if normalized != text:
            path.write_text(normalized, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
