"""
file_storage.py
---------------
Manages the filesystem layout for each profile.

Each profile lives under:
  profiles/<slug>/
    photo.jpg
    docs/           ← uploaded documents
    chromadb/       ← ChromaDB vector index
    config/
      header.html
      profile.css
      profile.js
      prompts.py

This module creates, reads, and cleans up those folders.
No business logic here — pure I/O.
"""

import copy
import gc
import json
import shutil
import time
from pathlib import Path
from typing import Optional

from app.core.config import PROFILES_DIR, DEFAULTS_DIR
from app.storage.hf_sync import hf_sync
from app.core.constants import (
    PROFILE_DOCS_DIR, PROFILE_CHROMADB_DIR, PROFILE_CONFIG_DIR,
    PROFILE_PHOTO_FILE, PROFILE_PROMPTS_FILE, PROFILE_HEADER_FILE,
    PROFILE_SLIDES_FILE, PROFILE_CSS_FILE, PROFILE_JS_FILE, ALLOWED_DOC_EXTENSIONS,
)
from app.core.logging_config import get_logger

logger = get_logger(__name__)


class ProfileFileStorage:
    """
    Filesystem operations for a single profile.

    All paths are derived from the profile slug.
    No knowledge of the registry or service layer.
    """

    def __init__(self, slug: str) -> None:
        self.slug        = slug
        self.base        = PROFILES_DIR / slug
        self.docs_dir    = self.base / PROFILE_DOCS_DIR
        self.chroma_dir  = self.base / PROFILE_CHROMADB_DIR
        self.config_dir  = self.base / PROFILE_CONFIG_DIR
        self.photo_path  = self.base / PROFILE_PHOTO_FILE
        self.prompts_path = self.base / PROFILE_PROMPTS_FILE
        self.header_path  = self.base / PROFILE_HEADER_FILE
        self.slides_path  = self.base / PROFILE_SLIDES_FILE
        self.css_path     = self.base / PROFILE_CSS_FILE
        self.js_path      = self.base / PROFILE_JS_FILE

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def create_directories(self) -> None:
        """Create all required subdirectories for a new profile.
        Removes any stale directory first so old files cannot survive a hard-delete + recreate."""
        if self.base.exists():
            shutil.rmtree(self.base)
            logger.warning("Removed stale directory for '%s' before creation", self.slug)
        for d in [self.docs_dir, self.chroma_dir, self.config_dir]:
            d.mkdir(parents=True, exist_ok=True)
        logger.info("Created directory structure for profile '%s' at %s", self.slug, self.base)

    def delete_all(self) -> bool:
        """Delete the entire profile folder. Returns True if deleted. Raises OSError on failure."""
        if self.base.exists():
            try:
                shutil.rmtree(self.base)
                logger.info("Deleted profile folder: %s", self.base)
                hf_sync.delete_dir(self.slug, wait=True)  # blocking — must finish before a same-slug recreate
                return True
            except OSError as e:
                logger.error("Failed to delete profile folder %s: %s", self.base, e, exc_info=True)
                raise
        logger.warning("Profile folder not found for deletion: %s", self.base)
        return False

    def exists(self) -> bool:
        return self.base.exists()

    # ── Photo ─────────────────────────────────────────────────────────────────

    def save_photo(self, data: bytes) -> Path:
        self.base.mkdir(parents=True, exist_ok=True)
        self.photo_path.write_bytes(data)
        logger.info("Saved photo for profile '%s' (%d bytes)", self.slug, len(data))
        hf_sync.push_file(self.photo_path, wait=True)  # blocking — must persist before response
        return self.photo_path

    def has_photo(self) -> bool:
        return self.photo_path.exists()

    # ── Documents ─────────────────────────────────────────────────────────────

    def save_document(self, filename: str, data: bytes) -> Path:
        """Save an uploaded document. Raises ValueError for unsupported types."""
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_DOC_EXTENSIONS:
            raise ValueError(f"Unsupported file type: {ext}. Allowed: {ALLOWED_DOC_EXTENSIONS}")
        dest = self.docs_dir / filename
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        logger.info("Saved document '%s' for profile '%s' (%d bytes)", filename, self.slug, len(data))
        hf_sync.push_file(dest)
        return dest

    def delete_document(self, filename: str) -> bool:
        path = self.docs_dir / filename
        if path.exists():
            path.unlink()
            logger.info("Deleted document '%s' from profile '%s'", filename, self.slug)
            hf_sync.delete_file(path)
            return True
        return False

    def list_documents(self) -> list[Path]:
        if not self.docs_dir.exists():
            return []
        return sorted(
            f for f in self.docs_dir.iterdir()
            if f.is_file() and f.suffix.lower() in ALLOWED_DOC_EXTENSIONS
        )

    def document_count(self) -> int:
        return len(self.list_documents())

    # ── Config files ──────────────────────────────────────────────────────────

    def read_text(self, path: Path, default: str = "") -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default
        except Exception as e:
            logger.warning("Could not read %s: %s", path, e)
            return default

    def write_text(self, path: Path, content: str) -> None:
        """Write text to a file. Raises OSError on permission or disk errors."""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(content, encoding="utf-8")
            hf_sync.push_file(path)
        except OSError as e:
            logger.error("Failed to write %s: %s", path, e, exc_info=True)
            raise

    def read_header(self) -> str:
        """Return profile header HTML, falling back to the default template."""
        content = self.read_text(self.header_path, default=None)
        if content is not None:
            return content
        return self.read_text(DEFAULTS_DIR / "header.html", default="")

    def write_header(self, html: str) -> None:
        self.write_text(self.header_path, html)

    # Default slides shown for new profiles or any profile without slides.json
    _DEFAULT_SLIDES: dict = {
        "slides": [
            {"type": "standard", "title": "AI-powered professional avatar", "subtitle": "",
             "body": "Ask me anything about career journey, experience, leadership, and expertise."},
            {"type": "standard", "title": "Explore professional story", "subtitle": "",
             "body": "Platforms built · Teams led · Problems solved · Impact delivered."},
            {"type": "standard", "title": "Want to connect?", "subtitle": "",
             "body": "Unanswered questions reach directly on my email. Leave your email below and I will follow up personally."},
        ]
    }

    def read_slides(self) -> dict:
        """Return slides dict from slides.json; returns default if not set."""
        raw = self.read_text(self.slides_path, default=None)
        if raw is None:
            return copy.deepcopy(self._DEFAULT_SLIDES)
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "slides" in data:
                return data
        except Exception:
            pass
        return copy.deepcopy(self._DEFAULT_SLIDES)

    def write_slides(self, data: dict) -> None:
        self.write_text(self.slides_path, json.dumps(data, ensure_ascii=False, indent=2))

    def reset_slides(self) -> None:
        """Delete slides.json so the next read returns the built-in defaults."""
        if self.slides_path.exists():
            self.slides_path.unlink()

    def read_css(self) -> str:
        """Return profile CSS, falling back to the default stylesheet."""
        content = self.read_text(self.css_path, default=None)
        if content is not None:
            return content
        return self.read_text(DEFAULTS_DIR / "profile.css", default="")

    def write_css(self, css: str) -> None:
        self.write_text(self.css_path, css)

    def read_js(self) -> str:
        return self.read_text(self.js_path, default="")

    def write_js(self, js: str) -> None:
        self.write_text(self.js_path, js)

    def read_prompts_raw(self) -> str:
        return self.read_text(self.prompts_path, default="")

    def write_prompts_raw(self, content: str) -> None:
        self.write_text(self.prompts_path, content)

    # ── Analytics (structured chat events) ───────────────────────────────────

    @property
    def analytics_dir(self) -> Path:
        return self.base / "analytics"

    @property
    def chat_events_path(self) -> Path:
        return self.analytics_dir / "chat_events.jsonl"

    def append_chat_event(self, event: dict) -> None:
        """Append one structured chat event as a JSON line."""
        self.analytics_dir.mkdir(parents=True, exist_ok=True)
        with self.chat_events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        hf_sync.push_file(self.chat_events_path)

    def read_chat_events(self, limit: int = 200) -> list[dict]:
        """Return the last `limit` chat events, newest first."""
        if not self.chat_events_path.exists():
            return []
        try:
            lines = self.chat_events_path.read_text(encoding="utf-8").splitlines()
            events = []
            for line in reversed(lines[-limit:]):
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return events
        except Exception as e:
            logger.warning("Could not read chat events for '%s': %s", self.slug, e)
            return []

    # ── ChromaDB path ─────────────────────────────────────────────────────────

    def chroma_path(self) -> str:
        """Return the ChromaDB persistence path for this profile (canonical)."""
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        return str(self.chroma_dir)

    @property
    def chroma_dir_new(self) -> Path:
        return self.base / f"{PROFILE_CHROMADB_DIR}_new"

    def chroma_path_new(self) -> str:
        """Return a fresh temp ChromaDB path for atomic-swap indexing."""
        if self.chroma_dir_new.exists():
            shutil.rmtree(self.chroma_dir_new)
        self.chroma_dir_new.mkdir(parents=True, exist_ok=True)
        return str(self.chroma_dir_new)

    def delete_chroma_new(self) -> None:
        """Remove the temp index directory (cleanup after a failed indexing attempt)."""
        if self.chroma_dir_new.exists():
            try:
                shutil.rmtree(self.chroma_dir_new)
                logger.info("Removed temp ChromaDB index for profile '%s'", self.slug)
            except OSError as e:
                logger.warning("Could not remove temp ChromaDB dir for '%s': %s", self.slug, e)

    def swap_chroma(self, retries: int = 5, delay: float = 1.0) -> None:
        """
        Atomically replace the canonical index with the newly-built temp index.
        1. Delete the old chromadb/ directory.
        2. Rename chromadb_new/ → chromadb/.
        Caller must have closed all engines pointing at both paths first.

        On Windows, SQLite file handles may linger briefly after the client
        is stopped. Retry with back-off to accommodate this.
        """
        if self.chroma_dir.exists():
            gc.collect()  # nudge CPython to release lingering file handles
            for attempt in range(1, retries + 1):
                try:
                    shutil.rmtree(self.chroma_dir)
                    break
                except PermissionError:
                    if attempt == retries:
                        raise
                    logger.debug(
                        "swap_chroma: rmtree attempt %d/%d failed for '%s', retrying in %.1fs",
                        attempt, retries, self.slug, delay,
                    )
                    time.sleep(delay)
        self.chroma_dir_new.rename(self.chroma_dir)
        logger.info("Swapped ChromaDB index for profile '%s'", self.slug)

    def delete_chroma(self) -> None:
        """Wipe the canonical ChromaDB index directory (and leave it deleted — caller recreates)."""
        if self.chroma_dir.exists():
            shutil.rmtree(self.chroma_dir)
            logger.info("Deleted ChromaDB index for profile '%s'", self.slug)
