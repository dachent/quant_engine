from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from storage import ensure_runtime_dirs, get_app_paths
from runtime_boundary import require_runtime_boundary


def _build_logger(name: str, filename: str, *, activate: bool) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    if not activate or any(
        isinstance(handler, RotatingFileHandler) for handler in logger.handlers
    ):
        return logger

    paths = ensure_runtime_dirs(get_app_paths())
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    logger.handlers = [
        handler
        for handler in logger.handlers
        if not isinstance(handler, logging.NullHandler)
    ]
    file_handler = RotatingFileHandler(
        paths.logs_dir / filename,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def setup_ui_logging(*, activate: bool = False) -> logging.Logger:
    if activate:
        require_runtime_boundary("ui_logging")
    return _build_logger("ui", "ui.log", activate=activate)


def setup_runner_logging(*, activate: bool = False) -> logging.Logger:
    if activate:
        require_runtime_boundary("runner_logging")
    return _build_logger("runner", "runner.log", activate=activate)
