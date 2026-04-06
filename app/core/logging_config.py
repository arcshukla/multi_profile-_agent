"""
logging_config.py
-----------------
Structured logging setup for the multi-profile platform.

Log files:
  logs/app.log        — general application events
  logs/indexing.log   — document ingestion / indexing events
  logs/chat.log       — per-turn chat events (question + answer)
  logs/profile_<slug>.log  — per-profile events (created dynamically)

All loggers share the same JSON-ish structured format:
  2026-03-22 10:00:00.123  INFO  [session123]  module.name  Message here

Usage:
  from app.core.logging_config import get_logger, get_profile_logger
  logger = get_logger(__name__)
  plog   = get_profile_logger("archana-shukla")
"""

import contextvars
import logging
import uuid
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from app.core.config import settings, LOGS_DIR

# ---------------------------------------------------------------------------
# Context variable — propagates session_id across async call chains
# ---------------------------------------------------------------------------
_current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_session_id", default="system"
)


def new_session_id() -> str:
    return uuid.uuid4().hex


def set_current_session_id(sid: str) -> None:
    _current_session_id.set(sid)


def get_current_session_id() -> str:
    return _current_session_id.get()


# ---------------------------------------------------------------------------
# Custom filter — stamps every log record with the current session_id
# ---------------------------------------------------------------------------
class _SessionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = get_current_session_id()
        return True


# ---------------------------------------------------------------------------
# Shared formatter
# ---------------------------------------------------------------------------
_FMT = "%(asctime)s.%(msecs)03d  %(levelname)-8s  [%(session_id)s]  %(name)s  %(filename)s:%(lineno)d  %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)


def _file_handler(log_path: Path, backup_days: int = 30) -> TimedRotatingFileHandler:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    h = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=backup_days,
        encoding="utf-8",
        utc=False,
    )
    h.setFormatter(_formatter)
    h.addFilter(_SessionFilter())
    return h


def _stream_handler() -> logging.StreamHandler:
    h = logging.StreamHandler()
    h.setFormatter(_formatter)
    h.addFilter(_SessionFilter())
    return h


# ---------------------------------------------------------------------------
# Shared log files — created once at module load
# ---------------------------------------------------------------------------
_APP_LOG_FILE      = LOGS_DIR / "app.log"
_INDEXING_LOG_FILE = LOGS_DIR / "indexing.log"
_CHAT_LOG_FILE     = LOGS_DIR / "chat.log"

_root_configured = False


def _configure_root() -> None:
    """Configure root logger once, adding console + app.log handlers."""
    global _root_configured
    if _root_configured:
        return
    _root_configured = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
    root.addHandler(_stream_handler())
    root.addHandler(_file_handler(_APP_LOG_FILE, backup_days=30))


_configure_root()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_logger(name: str) -> logging.Logger:
    """
    Standard module logger. Output goes to console + logs/app.log.

    Call once at the top of each file:
        logger = get_logger(__name__)
    """
    return logging.getLogger(name)


def get_indexing_logger() -> logging.Logger:
    """Logger for indexing/ingestion events → logs/indexing.log (30-day rotation)."""
    log = logging.getLogger("indexing")
    if not any(isinstance(h, TimedRotatingFileHandler) and "indexing" in getattr(h, "baseFilename", "")
               for h in log.handlers):
        log.addHandler(_file_handler(_INDEXING_LOG_FILE, backup_days=30))
    return log


def get_chat_logger() -> logging.Logger:
    """Logger for chat events → logs/chat.log (90-day rotation for audit trail)."""
    log = logging.getLogger("chat")
    if not any(isinstance(h, TimedRotatingFileHandler) and "chat" in getattr(h, "baseFilename", "")
               for h in log.handlers):
        log.addHandler(_file_handler(_CHAT_LOG_FILE, backup_days=90))
    return log


def get_profile_logger(slug: str) -> logging.Logger:
    """Per-profile logger → logs/profile_<slug>.log (30-day rotation)."""
    name = f"profile.{slug}"
    log = logging.getLogger(name)
    profile_log = LOGS_DIR / f"profile_{slug}.log"
    if not any(isinstance(h, TimedRotatingFileHandler) and slug in getattr(h, "baseFilename", "")
               for h in log.handlers):
        log.addHandler(_file_handler(profile_log, backup_days=30))
    return log


def get_session_logger(base_logger: logging.Logger, session_id: str) -> logging.Logger:
    """
    Returns a LoggerAdapter that prefixes every message with [sid[:8]].

    Use inside chat() to tag all lines with the session for multi-user correlation.
    """
    class _SidAdapter(logging.LoggerAdapter):
        def process(self, msg, kwargs):
            return msg, kwargs

    return _SidAdapter(base_logger, {"sid": session_id})
