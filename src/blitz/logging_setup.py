"""Configuration centralisée du logging stdlib."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LogConfig

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure(level: str = "INFO", log_cfg: LogConfig | None = None, console: bool = True) -> None:
    """Configure le root logger une seule fois (idempotent)."""
    root = logging.getLogger()
    if getattr(root, "_blitz_configured", False):
        return

    root.setLevel(level.upper())
    formatter = logging.Formatter(_FORMAT)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        root.addHandler(ch)

    if log_cfg is not None:
        fh = RotatingFileHandler(
            Path(log_cfg.file),
            maxBytes=log_cfg.max_bytes,
            backupCount=log_cfg.backups,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)

    # Réduire le bruit des bibliothèques tierces.
    logging.getLogger("paho").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    root._blitz_configured = True  # type: ignore[attr-defined]
