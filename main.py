#!/usr/bin/env python3
"""Entry point: python main.py <command>. Run `python main.py --help` for the list."""
import sys

PROJECT_PACKAGES = ("src", "config", "tests")

if sys.version_info < (3, 10):
    sys.exit(
        "This project needs Python 3.10 or newer, but this is Python %d.%d.\n"
        "Use the project's virtual environment instead:\n"
        "    source .venv/bin/activate\n"
        "    python main.py --help\n"
        "or run it directly:  .venv/bin/python main.py --help\n"
        "(No .venv yet? See 'Setup' in README.md.)" % sys.version_info[:2]
    )

try:
    from src.cli import main
except ModuleNotFoundError as exc:
    if (exc.name or "").split(".")[0] in PROJECT_PACKAGES:
        raise
    sys.exit(
        "The package '%s' is not installed for this Python (%s).\n"
        "Use the project's virtual environment instead:\n"
        "    source .venv/bin/activate\n"
        "    python main.py --help\n"
        "or install the requirements into it:  .venv/bin/python -m pip install -r requirements.txt"
        % (exc.name, sys.executable)
    )

if __name__ == "__main__":
    sys.exit(main())
