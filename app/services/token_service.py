"""
token_service.py
----------------
Tracks cumulative LLM token consumption per profile, persisted across restarts.

Storage:
  system/token_usage.json    — lifetime aggregates per profile (fast dashboard reads)
  system/token_ledger.jsonl  — append-only time-series log (billing / audit)

token_usage.json schema:
  {
    "<slug>": {
      "indexing_prompt":     int,
      "indexing_completion": int,
      "indexing_total":      int,
      "indexing_calls":      int,
      "intent_prompt":       int,
      "intent_completion":   int,
      "intent_total":        int,
      "intent_calls":        int,
      "query_prompt":        int,
      "query_completion":    int,
      "query_total":         int,
      "query_calls":         int
    }
  }

token_ledger.jsonl schema (one JSON object per line):
  {"ts":"<ISO-UTC>","slug":"<slug>","op":"<indexing|intent|query>",
   "prompt":<int>,"completion":<int>,"total":<int>}

Thread-safety: a single module-level lock guards all reads and writes.
"""

import json
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config import SYSTEM_DIR, settings
from app.core.logging_config import get_logger
from app.storage.hf_sync import hf_sync

logger = get_logger(__name__)

_STORE    = SYSTEM_DIR / "token_usage.json"
_LEDGER   = settings.TOKEN_LEDGER_FILE
_LOCK     = threading.Lock()

_ZERO_PROFILE: dict = {
    "indexing_prompt":     0,
    "indexing_completion": 0,
    "indexing_total":      0,
    "indexing_calls":      0,
    "intent_prompt":       0,
    "intent_completion":   0,
    "intent_total":        0,
    "intent_calls":        0,
    "query_prompt":        0,
    "query_completion":    0,
    "query_total":         0,
    "query_calls":         0,
}


class TokenService:
    """
    Append token usage for a profile and read aggregate stats.

    All methods are thread-safe.
    """

    # ── Write ─────────────────────────────────────────────────────────────────

    def record(
        self,
        slug:       str,
        operation:  str,   # "indexing" | "intent" | "query"
        prompt:     int,
        completion: int,
        total:      int,
    ) -> None:
        """
        Add token counts to the running totals for the given profile + operation.

        Silently ignores unknown operation names.
        """
        if operation not in ("indexing", "intent", "query"):
            return
        with _LOCK:
            data = self._load()
            profile = data.setdefault(slug, dict(_ZERO_PROFILE))
            profile[f"{operation}_prompt"]     += prompt
            profile[f"{operation}_completion"] += completion
            profile[f"{operation}_total"]      += total
            profile[f"{operation}_calls"]      += 1
            self._save(data)
            self._append_ledger(slug, operation, prompt, completion, total)

    def reset_profile(self, slug: str) -> None:
        """Zero out token counts for one profile."""
        with _LOCK:
            data = self._load()
            data[slug] = dict(_ZERO_PROFILE)
            self._save(data)

    def delete_profile(self, slug: str) -> None:
        """Remove a profile's entry from token_usage.json entirely (called on hard delete)."""
        with _LOCK:
            data = self._load()
            if slug in data:
                data.pop(slug)
                self._save(data)
                logger.info("Token usage cleared for deleted profile '%s'", slug)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_all(self) -> dict[str, dict]:
        """Return the full usage dict (all profiles)."""
        with _LOCK:
            return self._load()

    def get_profile(self, slug: str) -> dict:
        """Return usage for one profile, or zeroed entry if unknown."""
        with _LOCK:
            return dict(self._load().get(slug, _ZERO_PROFILE))

    def get_totals(self) -> dict:
        """Return sums across all profiles for each operation type."""
        totals = {
            "indexing_total": 0, "indexing_calls": 0,
            "intent_total":   0, "intent_calls":   0,
            "query_total":    0, "query_calls":    0,
            "grand_total":    0,
        }
        with _LOCK:
            for profile in self._load().values():
                for op in ("indexing", "intent", "query"):
                    totals[f"{op}_total"] += profile.get(f"{op}_total", 0)
                    totals[f"{op}_calls"] += profile.get(f"{op}_calls", 0)
        totals["grand_total"] = (
            totals["indexing_total"] + totals["intent_total"] + totals["query_total"]
        )
        return totals

    def get_ledger(
        self,
        slug: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> list[dict]:
        """
        Return raw ledger entries, optionally filtered.

        Args:
            slug:  Filter to a specific profile slug (None = all profiles)
            since: ISO date string lower bound, inclusive  e.g. "2026-03-01"
            until: ISO date string upper bound, inclusive  e.g. "2026-03-31"

        Returns:
            List of ledger dicts, chronological order.
        """
        if not _LEDGER.exists():
            return []
        entries = []
        try:
            for line in _LEDGER.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if slug and entry.get("slug") != slug:
                    continue
                ts = entry.get("ts", "")
                if since and ts < since:
                    continue
                if until and ts > until + "T23:59:59":
                    continue
                entries.append(entry)
        except Exception as e:
            logger.error("Failed to read token ledger: %s", e)
        return entries

    def get_monthly_summary(
        self,
        slug: Optional[str] = None,
        months: int = 6,
    ) -> list[dict]:
        """
        Return per-month token totals, newest month first.

        Args:
            slug:   Filter to one profile (None = all profiles combined)
            months: How many calendar months to return (default 6)

        Returns:
            List of dicts: {period, slug, query_tokens, indexing_tokens,
                            intent_tokens, total_tokens, query_calls, indexing_calls}
        """
        entries = self.get_ledger(slug=slug)
        buckets: dict[str, dict] = defaultdict(lambda: {
            "query_tokens": 0, "indexing_tokens": 0, "intent_tokens": 0,
            "total_tokens": 0, "query_calls": 0, "indexing_calls": 0,
        })
        for e in entries:
            period = e.get("ts", "")[:7]   # "YYYY-MM"
            op     = e.get("op", "")
            total  = e.get("total", 0)
            b = buckets[period]
            b["total_tokens"] += total
            if op == "query":
                b["query_tokens"] += total
                b["query_calls"]  += 1
            elif op == "indexing":
                b["indexing_tokens"] += total
                b["indexing_calls"]  += 1
            elif op == "intent":
                b["intent_tokens"] += total

        result = [
            {"period": period, "slug": slug or "all", **data}
            for period, data in sorted(buckets.items(), reverse=True)
        ]
        return result[:months]

    # ── Private ───────────────────────────────────────────────────────────────

    # In-memory cache for token_usage.json — invalidated on every write.
    # Eliminates the disk read on every dashboard / LLM stats page load.
    _cache: Optional[dict] = None

    def _append_ledger(
        self, slug: str, op: str, prompt: int, completion: int, total: int
    ) -> None:
        """Append one time-stamped entry to the immutable billing ledger."""
        entry = {
            "ts":         datetime.now(timezone.utc).isoformat(),
            "slug":       slug,
            "op":         op,
            "prompt":     prompt,
            "completion": completion,
            "total":      total,
        }
        try:
            _LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with open(_LEDGER, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            hf_sync.push_file(_LEDGER)
        except Exception as e:
            logger.error("Failed to append token ledger: %s", e)

    def _load(self) -> dict:
        if TokenService._cache is not None:
            return TokenService._cache
        if not _STORE.exists():
            TokenService._cache = {}
            return TokenService._cache
        try:
            TokenService._cache = json.loads(_STORE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("token_usage.json unreadable — starting fresh: %s", e)
            TokenService._cache = {}
        return TokenService._cache

    def _save(self, data: dict) -> None:
        TokenService._cache = None   # invalidate before write
        try:
            _STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            hf_sync.push_file(_STORE)
        except Exception as e:
            logger.error("Failed to save token_usage.json: %s", e)


# Singleton
token_service = TokenService()
