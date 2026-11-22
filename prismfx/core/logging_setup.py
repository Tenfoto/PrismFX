"""Rotating file logger for PrismFX Studio (PFX-P04).

Writes UTF-8 rotating logs to `<profile_dir>/prismfx.log` (or `%LOCALAPPDATA%\\PrismFX\\prismfx.log`
by default) while keeping 165 Hz hot paths free of I/O when no errors occur.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import pathlib
import threading
from typing import Optional

_LOGGER_ROOT_NAME = "prismfx"
_CONFIG_LOCK = threading.Lock()
_ACTIVE_HANDLER: Optional[RotatingFileHandler] = None
_ACTIVE_LOG_PATH: Optional[pathlib.Path] = None


def default_log_dir() -> pathlib.Path:
    env_dir = os.environ.get("PRISMFX_DATA_DIR")
    if env_dir:
        return pathlib.Path(env_dir)
    base = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home())
    return base / "PrismFX"


def configure_logging(
    log_dir: Optional[pathlib.Path] = None,
    filename: str = "prismfx.log",
    max_bytes: int = 1_048_576,
    backup_count: int = 3,
    level: int = logging.INFO,
) -> Optional[pathlib.Path]:
    """Configure or reconfigure the rotating file logger under `log_dir / filename`."""
    global _ACTIVE_HANDLER, _ACTIVE_LOG_PATH

    target_dir = pathlib.Path(log_dir).resolve() if log_dir is not None else default_log_dir().resolve()
    target_path = target_dir / filename

    with _CONFIG_LOCK:
        root_logger = logging.getLogger(_LOGGER_ROOT_NAME)
        root_logger.setLevel(level)
        root_logger.propagate = False

        if _ACTIVE_HANDLER is not None:
            try:
                root_logger.removeHandler(_ACTIVE_HANDLER)
                _ACTIVE_HANDLER.flush()
                _ACTIVE_HANDLER.close()
            except Exception:
                pass
            _ACTIVE_HANDLER = None
            _ACTIVE_LOG_PATH = None

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                str(target_path),
                maxBytes=max(65_536, int(max_bytes)),
                backupCount=max(1, int(backup_count)),
                encoding="utf-8",
                delay=False,
            )
            handler.setLevel(level)
            handler.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)s [%(levelname)s] %(name)s (%(threadName)s): %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            root_logger.addHandler(handler)
            _ACTIVE_HANDLER = handler
            _ACTIVE_LOG_PATH = target_path
            return target_path
        except OSError:
            return None


def get_logger(name: str = _LOGGER_ROOT_NAME) -> logging.Logger:
    """Return a child logger under the `prismfx` namespace."""
    if not name or name == _LOGGER_ROOT_NAME:
        return logging.getLogger(_LOGGER_ROOT_NAME)
    if name.startswith(f"{_LOGGER_ROOT_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_LOGGER_ROOT_NAME}.{name}")


def get_log_path() -> Optional[pathlib.Path]:
    with _CONFIG_LOCK:
        return _ACTIVE_LOG_PATH


def flush_logging() -> None:
    with _CONFIG_LOCK:
        if _ACTIVE_HANDLER is not None:
            try:
                _ACTIVE_HANDLER.flush()
            except Exception:
                pass


def close_logging() -> None:
    global _ACTIVE_HANDLER, _ACTIVE_LOG_PATH
    with _CONFIG_LOCK:
        root_logger = logging.getLogger(_LOGGER_ROOT_NAME)
        if _ACTIVE_HANDLER is not None:
            try:
                root_logger.removeHandler(_ACTIVE_HANDLER)
                _ACTIVE_HANDLER.flush()
                _ACTIVE_HANDLER.close()
            except Exception:
                pass
            _ACTIVE_HANDLER = None
            _ACTIVE_LOG_PATH = None
