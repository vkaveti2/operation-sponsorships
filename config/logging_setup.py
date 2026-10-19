"""Console + rotating file logging. Log files can contain lead emails, so logs/ is gitignored."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config.settings import LOGS_DIR


NOISY_LOGGERS = ("urllib3", "httpx", "httpx2", "httpcore", "googleapiclient", "google_auth_httplib2", "google_genai")


def quiet_noisy_loggers(verbose: bool) -> None:
    """Third-party libraries narrate every request at INFO (Gemini prints 'AFC is enabled ...'). Hide that
    unless --verbose, in the console and in logs/outreach.log."""
    level = logging.DEBUG if verbose else logging.WARNING
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(level)


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    if getattr(root, "_outreach_configured", False):
        return
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    try:
        LOGS_DIR.mkdir(exist_ok=True)
        file_handler = RotatingFileHandler(
            LOGS_DIR / "outreach.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(file_handler)
    except OSError as exc:  # read-only checkout, etc. Console logging still works.
        root.warning("Could not open logs/outreach.log (%s). Continuing without a log file.", exc)

    quiet_noisy_loggers(verbose)
    root._outreach_configured = True  # type: ignore[attr-defined]
