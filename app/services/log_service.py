"""
log_service.py
--------------
Read log files for display in the Admin UI System tab.

Log files:
  logs/app.log
  logs/indexing.log
  logs/chat.log
  logs/profile_<slug>.log
"""

import os
from pathlib import Path
from typing import Optional

from app.core.config import LOGS_DIR
from app.core.logging_config import get_logger

logger = get_logger(__name__)

# Read buffer size for binary tail (8 KB per chunk)
_TAIL_CHUNK = 8192


def _tail_lines(path: Path, n: int) -> list[str]:
    """
    Return the last n lines of a file without reading the entire file.
    Uses binary seek from EOF — O(result_size) rather than O(file_size).
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        file_size = f.tell()
        if file_size == 0:
            return []

        buf = b""
        pos = file_size
        lines_found = 0

        while pos > 0 and lines_found <= n:
            read_size = min(_TAIL_CHUNK, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            buf = chunk + buf
            lines_found = buf.count(b"\n")

        decoded = buf.decode("utf-8", errors="replace")
        all_lines = decoded.splitlines()
        # Drop leading empty line that can appear when the file starts mid-chunk
        return all_lines[-n:] if n > 0 else all_lines


def _search_lines(path: Path, search: str, tail: int) -> tuple[list[str], int]:
    """
    Stream the file line-by-line to find matching lines, keeping only the last
    `tail` matches.  Never loads the whole file into memory at once.
    Returns (matching_lines[-tail:], total_matching_count).
    """
    needle = search.lower()
    matches: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if needle in line.lower():
                    matches.append(line.rstrip("\n"))
    except Exception as e:
        logger.error("Failed to stream log %s: %s", path, e)
        return [], 0
    total = len(matches)
    return matches[-tail:] if tail > 0 else matches, total


class LogService:

    def read_log(
        self,
        log_type: str,
        slug: Optional[str] = None,
        tail: int = 200,
        search: Optional[str] = None,
    ) -> dict:
        """
        Read a log file and return recent lines.

        Args:
            log_type: "app" | "indexing" | "chat" | "profile"
            slug:     Required when log_type == "profile"
            tail:     Number of lines from the end
            search:   Filter lines containing this string

        Returns:
            {"log_type": ..., "lines": [...], "total_lines": n}

        Performance:
            Without search: binary seek from EOF — reads only the bytes needed,
            not the full file.  O(tail_bytes) instead of O(file_size).
            With search: streams line-by-line — never loads full file into memory.
        """
        path = self._resolve_path(log_type, slug)
        if path is None or not path.exists():
            return {"log_type": log_type, "slug": slug, "lines": [], "total_lines": 0}

        try:
            if search:
                lines, total = _search_lines(path, search, tail)
            else:
                lines = _tail_lines(path, tail)
                total = len(lines)
        except Exception as e:
            logger.error("Failed to read log %s: %s", path, e)
            return {"log_type": log_type, "slug": slug, "lines": [], "total_lines": 0}

        return {
            "log_type": log_type,
            "slug": slug,
            "lines": lines,
            "total_lines": total,
        }

    def _resolve_path(self, log_type: str, slug: Optional[str]) -> Optional[Path]:
        mapping = {
            "app":      LOGS_DIR / "app.log",
            "indexing": LOGS_DIR / "indexing.log",
            "chat":     LOGS_DIR / "chat.log",
        }
        if log_type == "profile":
            if not slug:
                logger.warning("log_type='profile' requires a slug")
                return None
            return LOGS_DIR / f"profile_{slug}.log"
        return mapping.get(log_type)

    def list_profile_logs(self) -> list[str]:
        """Return slugs for which a profile log file exists."""
        slugs = []
        for f in LOGS_DIR.glob("profile_*.log"):
            slug = f.stem.replace("profile_", "", 1)
            if slug:
                slugs.append(slug)
        return sorted(slugs)


# Singleton
log_service = LogService()
