"""Console entry point for the GUI.

Importing :mod:`deskcage.gui` requires PyQt6.  Keep that import here so a missing
optional dependency produces one clear message instead of a traceback.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    try:
        from .gui import main as gui_main
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] == "PyQt6":
            print(
                "dcage-gui: PyQt6 is required for the graphical interface.\n"
                "Install it with: pip install 'deskcage[gui]'  (or pip install PyQt6)",
                file=sys.stderr,
            )
            return 1
        raise
    return gui_main(argv)


if __name__ == "__main__":
    sys.exit(main())
