"""
index_service.py
----------------
Manages document ingestion and ChromaDB indexing per profile.

Responsibilities:
  - Build a SemanticRAGEngine for any profile on demand
  - Run ingestion (force or incremental)
  - Track indexing events in system/index_history.log (append-only)
  - Provide index status for Admin UI

Design:
  - One RAG engine instance cached per profile for the process lifetime
  - Thread-safe — indexing is locked per profile slug
  - index_history.log is append-only: never rewritten, only extended
"""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.constants import INDEX_STATUS_SUCCESS, INDEX_STATUS_FAILED, INDEX_STATUS_RUNNING
from app.core.logging_config import get_logger, get_indexing_logger, get_profile_logger
from app.storage.file_storage import ProfileFileStorage
from app.storage.chroma_manager import drop_client_cache
from app.rag.profile_rag import build_profile_rag
from app.rag.semantic_rag_engine import SemanticRAGEngine
from app.services.token_service import token_service

logger = get_logger(__name__)
indexing_log = get_indexing_logger()

# ── Per-profile state ─────────────────────────────────────────────────────────
_engines: dict[str, SemanticRAGEngine] = {}     # slug → engine
_indexing_locks: dict[str, threading.Lock] = {}  # slug → lock
_state_lock = threading.Lock()                   # protects _engines / _indexing_locks
_currently_indexing: set[str] = set()            # slugs actively being indexed right now
_startup_pending: set[str] = set()              # slugs queued for on-demand index (triggered, not yet running)

# Cache for _last_indexed_entry() — reading the full index_history.log on every
# get_status() call (called once per profile on every admin list page) is the
# single biggest performance bottleneck.  Cached per slug; invalidated when
# _record_event() records a new success entry for that slug.
_last_entry_cache: dict[str, Optional[dict]] = {}   # slug → {timestamp, duration_seconds} or None


def _get_lock(slug: str) -> threading.Lock:
    with _state_lock:
        if slug not in _indexing_locks:
            _indexing_locks[slug] = threading.Lock()
        return _indexing_locks[slug]


def is_warming_up(slug: str) -> bool:
    """True if this profile has an on-demand index in flight (queued or actively running)."""
    return slug in _startup_pending or slug in _currently_indexing


def trigger_on_demand(slug: str) -> None:
    """
    Trigger background indexing when a visitor arrives at an un-indexed profile.

    Idempotent and safe to call from any code path (welcome, chat, etc.):
      - Already in flight → returns immediately.
      - No documents uploaded → skips silently (nothing to index).
      - Otherwise marks as pending, spawns a daemon thread, and returns.

    The pending flag is cleared automatically by index_profile's finally block
    once indexing finishes (success or failure).
    """
    if slug in _startup_pending or slug in _currently_indexing:
        logger.debug("On-demand index: '%s' already in flight — skipping", slug)
        return
    fs = ProfileFileStorage(slug)
    if fs.document_count() == 0:
        logger.debug("On-demand index: '%s' has no documents — skipping", slug)
        return
    _startup_pending.add(slug)
    logger.info("On-demand index: triggered for '%s' (first visitor arrival)", slug)
    threading.Thread(
        target=index_service.index_profile,
        args=(slug, False),
        daemon=True,
        name=f"on-demand-index-{slug}",
    ).start()


class IndexService:
    """
    Document indexing service.

    Entry points:
      index_profile(slug, force)   — run ingestion (blocking)
      get_engine(slug)             — get cached RAG engine for chat
      get_status(slug)             — current index status
      force_reindex(slug)          — wipe index + re-run ingestion
    """

    # ── Engine factory / cache ────────────────────────────────────────────────

    def get_engine(self, slug: str) -> Optional[SemanticRAGEngine]:
        """
        Return the RAG engine for a profile, initialising it if needed.
        Returns None if the profile folder doesn't exist.
        """
        with _state_lock:
            if slug in _engines:
                return _engines[slug]

        fs = ProfileFileStorage(slug)
        if not fs.exists():
            logger.warning("get_engine: profile folder not found for '%s'", slug)
            return None

        try:
            engine = build_profile_rag(
                db_path=fs.chroma_path(),
                slug=slug,
                on_tokens=lambda op, p, c, t: token_service.record(slug, op, p, c, t),
            )
        except Exception as e:
            logger.error("get_engine: failed to build RAG engine for '%s': %s", slug, e)
            return None
        with _state_lock:
            _engines[slug] = engine
        return engine

    def evict_engine(self, slug: str) -> None:
        """Remove a cached engine and close its ChromaDB connection."""
        with _state_lock:
            engine = _engines.pop(slug, None)
        if engine is not None:
            engine.close()

    # ── Indexing ──────────────────────────────────────────────────────────────

    def index_profile(self, slug: str, force: bool = False) -> dict:
        """
        Ingest all documents for a profile.

        Args:
            slug:  Profile slug
            force: If True, wipe existing index first

        Returns:
            {"status": "success"|"failed", "document_count": n, "duration_seconds": n}
        """
        lock = _get_lock(slug)
        if not lock.acquire(blocking=False):
            logger.info("Indexing already in progress for '%s' — returning running status", slug)
            return {"status": INDEX_STATUS_RUNNING, "document_count": 0, "duration_seconds": 0}

        _currently_indexing.add(slug)
        start = time.time()
        try:
            return self._run_indexing(slug, force)
        finally:
            lock.release()
            _currently_indexing.discard(slug)
            _startup_pending.discard(slug)

    def _run_indexing(self, slug: str, force: bool) -> dict:
        fs = ProfileFileStorage(slug)
        plog = get_profile_logger(slug)
        plog.info("Indexing started | slug=%s | force=%s", slug, force)
        indexing_log.info("Indexing started | slug=%s | force=%s", slug, force)

        if not fs.exists():
            return self._record_event(slug, INDEX_STATUS_FAILED, 0, 0, error="Profile folder not found")

        if force:
            plog.info("Force reindex: clearing ChromaDB for '%s'", slug)
            self.evict_engine(slug)
            drop_client_cache(fs.chroma_path())
            fs.delete_chroma()

        doc_count = fs.document_count()
        if doc_count == 0:
            plog.info("No documents to index for '%s'", slug)
            return self._record_event(slug, INDEX_STATUS_SUCCESS, 0, 0)

        start = time.time()
        try:
            engine = build_profile_rag(
                db_path=fs.chroma_path(),
                slug=slug,
                on_tokens=lambda op, p, c, t: token_service.record(slug, op, p, c, t),
            )
            # Replace cached engine
            with _state_lock:
                _engines[slug] = engine

            new_chunks = engine.ingest_all(fs.docs_dir)
            duration = round(time.time() - start, 2)

            plog.info(
                "Indexing complete | slug=%s | docs=%d | new_chunks=%d | %.1fs",
                slug, doc_count, new_chunks, duration,
            )
            return self._record_event(slug, INDEX_STATUS_SUCCESS, doc_count, duration)

        except Exception as e:
            duration = round(time.time() - start, 2)
            plog.error("Indexing failed | slug=%s | error=%s", slug, e, exc_info=True)
            return self._record_event(slug, INDEX_STATUS_FAILED, doc_count, duration, error=str(e))

    def force_reindex(self, slug: str) -> dict:
        """Wipe index and re-run ingestion from scratch."""
        return self.index_profile(slug, force=True)

    def is_indexing(self, slug: str) -> bool:
        return slug in _currently_indexing

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self, slug: str) -> dict:
        """
        Return current index status for a profile.

        Returns dict with: status, chunk_count, document_count, last_indexed
        """
        if self.is_indexing(slug):
            fs = ProfileFileStorage(slug)
            return {"status": INDEX_STATUS_RUNNING, "chunk_count": 0, "document_count": fs.document_count(), "last_indexed": None}

        fs = ProfileFileStorage(slug)
        engine = self.get_engine(slug)
        try:
            chunk_count = engine.chunk_count() if engine else 0
        except Exception as e:
            logger.warning(
                "ChromaDB error reading chunk_count for %s: %s — treating as not_indexed",
                slug, e,
            )
            chunk_count = 0
        doc_count = fs.document_count()
        last_entry = self._last_indexed_entry(slug)
        last_indexed = last_entry["timestamp"] if last_entry else None
        last_duration = last_entry["duration_seconds"] if last_entry else None

        if chunk_count > 0:
            status = INDEX_STATUS_SUCCESS
        elif last_indexed:
            # Indexing ran but produced 0 chunks (LLM split may have failed)
            status = "empty"
        else:
            status = "not_indexed"
        return {
            "status":           status,
            "chunk_count":      chunk_count,
            "document_count":   doc_count,
            "last_indexed":     last_indexed,
            "duration_seconds": last_duration,
        }

    # ── History ───────────────────────────────────────────────────────────────

    def _record_event(
        self, slug: str, status: str, doc_count: int, duration: float, error: Optional[str] = None
    ) -> dict:
        """Append an indexing event to system/index_history.log (never rewrite)."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "profile_slug": slug,
            "status": status,
            "document_count": doc_count,
            "duration_seconds": duration,
        }
        if error:
            entry["error"] = error

        try:
            history_file = settings.INDEX_HISTORY_FILE
            history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            indexing_log.info("History recorded | %s", json.dumps(entry))
        except Exception as e:
            logger.error("Failed to write index history: %s", e)

        # Update the per-slug cache immediately so get_status() doesn't need to
        # re-scan the full log file on the very next request.
        if status == INDEX_STATUS_SUCCESS:
            _last_entry_cache[slug] = {
                "timestamp":        entry["timestamp"],
                "duration_seconds": duration,
            }
        elif status == "purged":
            _last_entry_cache[slug] = None

        return entry

    def clear_slug_history(self, slug: str) -> None:
        """Write a purge sentinel into index_history.log so a same-slug recreate starts clean."""
        history_file = settings.INDEX_HISTORY_FILE
        try:
            history_file.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "timestamp":    datetime.now(timezone.utc).isoformat(),
                "profile_slug": slug,
                "status":       "purged",
            }
            with open(history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            _last_entry_cache[slug] = None   # invalidate so next get_status() reflects the purge
            logger.info("Index history purge marker written for '%s'", slug)
        except Exception as e:
            logger.warning("Failed to write purge marker for '%s': %s", slug, e)

    def _last_indexed_entry(self, slug: str) -> Optional[dict]:
        """
        Return the most recent successful index entry for this slug.
        Result is served from _last_entry_cache when available — avoids scanning
        the full index_history.log on every get_status() call (the main perf bottleneck
        on admin list pages that call this once per profile).
        Cache is populated by _record_event() on indexing completion and on first miss.
        Resets to None when a 'purged' sentinel is encountered.
        """
        if slug in _last_entry_cache:
            return _last_entry_cache[slug]

        # Cache miss — scan the log file once and populate the cache entry.
        history_file = settings.INDEX_HISTORY_FILE
        if not history_file.exists():
            _last_entry_cache[slug] = None
            return None
        last: Optional[dict] = None
        try:
            for line in history_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("profile_slug") != slug:
                        continue
                    status = entry.get("status")
                    if status == INDEX_STATUS_SUCCESS:
                        last = {
                            "timestamp":        entry.get("timestamp"),
                            "duration_seconds": entry.get("duration_seconds"),
                        }
                    elif status == "purged":
                        last = None  # reset — prior incarnation of this slug
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.warning("Could not read index history for '%s': %s", slug, e)
        _last_entry_cache[slug] = last
        return last

    def get_history(self, slug: Optional[str] = None, limit: int = 100) -> list[dict]:
        """
        Return indexing history entries, newest first.
        Filter by slug if provided.
        """
        history_file = settings.INDEX_HISTORY_FILE
        if not history_file.exists():
            return []
        entries = []
        try:
            for line in history_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if slug is None or entry.get("profile_slug") == slug:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.error("Failed to read index history: %s", e)
        # Newest first
        return list(reversed(entries))[:limit]


# Singleton
index_service = IndexService()
