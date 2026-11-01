"""A tiny TOML writer for the deskcage profile schema.

The profile schema only needs strings, booleans, integers, arrays of those,
and one level of nested tables.  That is small enough to serialize by hand;
reading uses the standard library's :mod:`tomllib`.
"""

from __future__ import annotations

from typing import Any

_BARE_KEY = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class TomlError(ValueError):
    pass


def _quote(value: str) -> str:
    out = ['"']
    for ch in value:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _key(value: str) -> str:
    if value and all(ch in _BARE_KEY for ch in value):
        return value
    return _quote(value)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(item) for item in value) + "]"
    raise TomlError(f"cannot serialize {type(value).__name__}")


def _value(value: Any, indent: int = 0) -> str:
    """Render one value, one array element per line.

    Lists are the only long values in a profile: a handful of paths on a
    single line is hard to edit by hand, while TOML itself happily allows a
    multi-line array with a trailing comma after every element.  Empty arrays
    stay inline so an unused section does not take three lines.
    """

    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        pad = " " * (indent + 4)
        items = "".join(f"{pad}{_scalar(item)},\n" for item in value)
        return "[\n" + items + " " * indent + "]"
    return _scalar(value)


def dumps(data: dict[str, Any]) -> str:
    scalars = [(k, v) for k, v in data.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in data.items() if isinstance(v, dict)]

    lines: list[str] = []
    for key, value in scalars:
        lines.append(f"{_key(key)} = {_value(value)}")

    for key, table in tables:
        if lines:
            lines.append("")
        lines.append(f"[{_key(key)}]")
        for subkey, value in table.items():
            if isinstance(value, dict):
                raise TomlError(f"nested table too deep: {key}.{subkey}")
            lines.append(f"{_key(subkey)} = {_value(value)}")

    return "\n".join(lines) + "\n"
