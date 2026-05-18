"""Logging adapter for AstrBot-hosted and standalone daemon execution."""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from typing import Any

try:
    from astrbot.api import logger as _astrbot_logger

    USING_ASTRBOT_LOGGER = True
except ImportError:
    _astrbot_logger = None
    USING_ASTRBOT_LOGGER = False


_LEVEL_VALUES = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


class _StandaloneLogger:
    def __init__(self, name: str):
        self.name = name

    @staticmethod
    def _threshold() -> int:
        level_name = os.getenv("QZONE_DAEMON_LOG_LEVEL", "INFO").upper()
        return _LEVEL_VALUES.get(level_name, _LEVEL_VALUES["INFO"])

    def _enabled(self, level: str) -> bool:
        return _LEVEL_VALUES[level] >= self._threshold()

    @staticmethod
    def _format(message: object, args: tuple[Any, ...]) -> str:
        text = str(message)
        if not args:
            return text
        try:
            return text % args
        except Exception:
            return " ".join([text, *(str(item) for item in args)])

    def _write(self, level: str, message: object, *args: Any, exc_info: Any = None, **_: Any) -> None:
        if not self._enabled(level):
            return
        timestamp = datetime.now().isoformat(timespec="seconds")
        print(
            f"{timestamp} {level} {self.name}: {self._format(message, args)}",
            file=sys.stderr,
        )
        if exc_info:
            if exc_info is True:
                traceback.print_exc(file=sys.stderr)
            elif isinstance(exc_info, tuple):
                traceback.print_exception(*exc_info, file=sys.stderr)

    def debug(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("DEBUG", message, *args, **kwargs)

    def info(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("INFO", message, *args, **kwargs)

    def warning(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("WARNING", message, *args, **kwargs)

    warn = warning

    def error(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("ERROR", message, *args, **kwargs)

    def critical(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._write("CRITICAL", message, *args, **kwargs)

    fatal = critical

    def exception(self, message: object, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("exc_info", True)
        self._write("ERROR", message, *args, **kwargs)

    def log(self, level: int, message: object, *args: Any, **kwargs: Any) -> None:
        for level_name, level_value in sorted(_LEVEL_VALUES.items(), key=lambda item: item[1]):
            if level <= level_value:
                self._write(level_name, message, *args, **kwargs)
                return
        self._write("CRITICAL", message, *args, **kwargs)

    def isEnabledFor(self, level: int) -> bool:
        return int(level) >= self._threshold()


def get_logger(name: str = "qzone_bridge"):
    if USING_ASTRBOT_LOGGER and _astrbot_logger is not None:
        return _astrbot_logger
    return _StandaloneLogger(name)


logger = get_logger("qzone_bridge")


def configure_standalone_logging(default_level: str = "INFO") -> None:
    os.environ.setdefault("QZONE_DAEMON_LOG_LEVEL", default_level)
