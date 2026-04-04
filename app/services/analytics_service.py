"""
analytics_service.py
--------------------
Pure data aggregation for owner and admin analytics dashboards.

No rendering, no side effects. Reads only from existing sources:
  - profiles/{slug}/analytics/chat_events.jsonl  (via ProfileFileStorage)
  - system/token_ledger.jsonl                    (via TokenService)
  - system/token_usage.json                      (via TokenService)
  - logs/chat.log.*                              (for LEAD lines)
  - users.json                                   (via ProfileService for slug list)

All methods are synchronous and crash-safe (empty results on any I/O error).
"""

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.core.config import LOGS_DIR
from app.core.logging_config import get_logger
from app.services.token_service import token_service
from app.storage.file_storage import ProfileFileStorage

logger = get_logger(__name__)

# Approximate cost per 1k tokens (GPT-4o-mini default) — used for admin cost estimate
_COST_PER_1K_TOKENS = 0.00015   # USD


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_all_events(slug: str) -> list[dict]:
    """Return all chat events for a profile, oldest first."""
    try:
        path = ProfileFileStorage(slug).chat_events_path
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return events
    except Exception as e:
        logger.warning("analytics: could not load events for %s: %s", slug, e)
        return []


def _parse_lead_lines(slug: Optional[str] = None) -> list[dict]:
    """
    Scan all chat.log.* files for LEAD entries.
    Returns list of {ts, slug, email}.
    """
    pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+\s+\w+\s+\S+\s+chat\s+LEAD\s+\|\s+slug=(\S+)\s+\|\s+email=(\S+)"
    )
    results = []
    try:
        for log_file in sorted(LOGS_DIR.glob("chat.log*")):
            try:
                for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    m = pattern.match(line)
                    if m:
                        ts, s, email = m.group(1), m.group(2), m.group(3)
                        if slug is None or s == slug:
                            results.append({"ts": ts, "slug": s, "email": email})
            except Exception:
                pass
    except Exception as e:
        logger.warning("analytics: could not parse lead lines: %s", e)
    return results


def _date_n_days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _events_since(events: list[dict], days: int) -> list[dict]:
    cutoff = _date_n_days_ago(days)
    return [e for e in events if (e.get("ts") or "")[:10] >= cutoff]


def _group_by_date(events: list[dict], key_fn) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for e in events:
        k = key_fn(e)
        if k:
            counts[k] += 1
    return counts


def _fill_date_range(days: int) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


# ── Owner analytics ───────────────────────────────────────────────────────────

def get_owner_kpis(slug: str) -> dict:
    """
    KPI summary for the owner dashboard analytics page.
    Returns a flat dict safe to pass directly to Jinja2.
    """
    events  = _load_all_events(slug)
    recent  = _events_since(events, 30)
    leads   = _parse_lead_lines(slug)

    total_q  = len(events)
    unanswered = sum(1 for e in events if not e.get("was_answered", True))
    answer_rate = round((total_q - unanswered) / total_q * 100) if total_q else 0

    sessions = {e.get("session_id") for e in events if e.get("session_id")}
    by_session: dict[str, int] = defaultdict(int)
    for e in events:
        sid = e.get("session_id")
        if sid:
            by_session[sid] += 1
    avg_depth = round(sum(by_session.values()) / len(by_session), 1) if by_session else 0

    query_events = [e for e in events if e.get("tokens")]
    avg_tokens   = round(sum(e["tokens"] for e in query_events) / len(query_events)) if query_events else 0

    latencies    = [e["latency_ms"] for e in events if e.get("latency_ms")]
    avg_latency  = round(sum(latencies) / len(latencies)) if latencies else 0

    recent_q      = len(recent)
    recent_leads  = len([l for l in leads if l["ts"][:10] >= _date_n_days_ago(30)])

    return {
        "total_questions":  total_q,
        "unanswered_count": unanswered,
        "answer_rate":      answer_rate,
        "unique_sessions":  len(sessions),
        "avg_session_depth": avg_depth,
        "total_leads":      len(leads),
        "recent_questions": recent_q,
        "recent_leads":     recent_leads,
        "avg_tokens":       avg_tokens,
        "avg_latency_ms":   avg_latency,
    }


def get_daily_questions(slug: str, days: int = 30) -> dict:
    """
    Daily question volume for the past N days, split into answered/unanswered.
    Returns {labels: [...], answered: [...], unanswered: [...]}
    """
    events  = _events_since(_load_all_events(slug), days)
    dates   = _fill_date_range(days)

    answered_by_day:   dict[str, int] = defaultdict(int)
    unanswered_by_day: dict[str, int] = defaultdict(int)
    for e in events:
        d = (e.get("ts") or "")[:10]
        if not d:
            continue
        if e.get("was_answered", True):
            answered_by_day[d] += 1
        else:
            unanswered_by_day[d] += 1

    return {
        "labels":     dates,
        "answered":   [answered_by_day.get(d, 0) for d in dates],
        "unanswered": [unanswered_by_day.get(d, 0) for d in dates],
    }


def get_top_content_gaps(slug: str, limit: int = 10) -> list[dict]:
    """
    Most common unanswered questions for this profile.
    Returns [{question, count}, ...] sorted by count desc.
    """
    events = _load_all_events(slug)
    counts: dict[str, int] = defaultdict(int)
    for e in events:
        if not e.get("was_answered", True):
            q = (e.get("question") or "").strip().lower()
            if q:
                counts[q] += 1
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [{"question": q, "count": c} for q, c in ranked]


def get_token_daily(slug: str, days: int = 30) -> dict:
    """
    Daily token consumption by operation type for this profile.
    Returns {labels: [...], query: [...], intent: [...], indexing: [...]}
    """
    since   = _date_n_days_ago(days)
    ledger  = token_service.get_ledger(slug=slug, since=since)
    dates   = _fill_date_range(days)

    query_by_day:    dict[str, int] = defaultdict(int)
    intent_by_day:   dict[str, int] = defaultdict(int)
    indexing_by_day: dict[str, int] = defaultdict(int)

    for entry in ledger:
        d  = (entry.get("ts") or "")[:10]
        op = entry.get("op", "")
        t  = entry.get("total", 0)
        if op == "query":
            query_by_day[d] += t
        elif op == "intent":
            intent_by_day[d] += t
        elif op == "indexing":
            indexing_by_day[d] += t

    return {
        "labels":   dates,
        "query":    [query_by_day.get(d, 0) for d in dates],
        "intent":   [intent_by_day.get(d, 0) for d in dates],
        "indexing": [indexing_by_day.get(d, 0) for d in dates],
    }


def get_lead_timeline(slug: str, days: int = 30) -> dict:
    """
    Daily lead capture count for this profile over the past N days.
    Returns {labels: [...], counts: [...]}
    """
    cutoff = _date_n_days_ago(days)
    leads  = [l for l in _parse_lead_lines(slug) if l["ts"][:10] >= cutoff]
    dates  = _fill_date_range(days)
    by_day: dict[str, int] = defaultdict(int)
    for l in leads:
        by_day[l["ts"][:10]] += 1
    return {
        "labels": dates,
        "counts": [by_day.get(d, 0) for d in dates],
    }


# ── Admin analytics ───────────────────────────────────────────────────────────

def _all_active_slugs() -> list[str]:
    """Return slugs for all non-deleted profiles that have a chat_events file."""
    from app.services.profile_service import profile_service
    try:
        profiles = profile_service.list_profiles()
        return [p.slug for p in profiles if p.status != "deleted"]
    except Exception as e:
        logger.warning("analytics: could not list profiles: %s", e)
        return []


def get_platform_kpis(days: int = 30) -> dict:
    """
    Platform-wide KPI summary for admin dashboard.
    """
    slugs = _all_active_slugs()
    cutoff = _date_n_days_ago(days)

    total_q = 0
    unanswered = 0
    active_slugs = set()

    for slug in slugs:
        events = _events_since(_load_all_events(slug), days)
        if events:
            active_slugs.add(slug)
        total_q    += len(events)
        unanswered += sum(1 for e in events if not e.get("was_answered", True))

    answer_rate = round((total_q - unanswered) / total_q * 100) if total_q else 0

    all_leads = [l for l in _parse_lead_lines() if l["ts"][:10] >= cutoff]
    totals    = token_service.get_totals()

    return {
        "total_questions":    total_q,
        "unanswered_count":   unanswered,
        "answer_rate":        answer_rate,
        "active_profiles":    len(active_slugs),
        "total_profiles":     len(slugs),
        "total_leads":        len(all_leads),
        "platform_tokens":    totals.get("grand_total", 0),
        "platform_q_calls":   totals.get("query_calls", 0),
    }


def get_platform_daily(days: int = 30) -> dict:
    """
    Daily questions and leads platform-wide, for a trend line.
    Returns {labels: [...], questions: [...], leads: [...]}
    """
    slugs   = _all_active_slugs()
    cutoff  = _date_n_days_ago(days)
    dates   = _fill_date_range(days)
    by_day: dict[str, int] = defaultdict(int)

    for slug in slugs:
        for e in _events_since(_load_all_events(slug), days):
            d = (e.get("ts") or "")[:10]
            if d:
                by_day[d] += 1

    lead_by_day: dict[str, int] = defaultdict(int)
    for l in _parse_lead_lines():
        d = l["ts"][:10]
        if d >= cutoff:
            lead_by_day[d] += 1

    return {
        "labels":    dates,
        "questions": [by_day.get(d, 0) for d in dates],
        "leads":     [lead_by_day.get(d, 0) for d in dates],
    }


def get_profile_activity_ranking(days: int = 30) -> list[dict]:
    """
    Per-profile activity summary sorted by question count desc.
    Returns list of dicts with keys: slug, questions, leads, gaps, answer_rate, last_active
    """
    slugs   = _all_active_slugs()
    cutoff  = _date_n_days_ago(days)

    all_leads = _parse_lead_lines()
    lead_by_slug: dict[str, int] = defaultdict(int)
    for l in all_leads:
        if l["ts"][:10] >= cutoff:
            lead_by_slug[l["slug"]] += 1

    rows = []
    for slug in slugs:
        events   = _events_since(_load_all_events(slug), days)
        total    = len(events)
        gaps     = sum(1 for e in events if not e.get("was_answered", True))
        rate     = round((total - gaps) / total * 100) if total else 0
        ts_list  = [e.get("ts", "") for e in events if e.get("ts")]
        last_on  = max(ts_list)[:10] if ts_list else None
        rows.append({
            "slug":        slug,
            "questions":   total,
            "leads":       lead_by_slug.get(slug, 0),
            "gaps":        gaps,
            "answer_rate": rate,
            "last_active": last_on,
        })
    return sorted(rows, key=lambda r: r["questions"], reverse=True)


def get_platform_token_burn(days: int = 30) -> dict:
    """
    Daily token burn and estimated USD cost platform-wide.
    Returns {labels: [...], tokens: [...], cost_usd: [...]}
    """
    since  = _date_n_days_ago(days)
    ledger = token_service.get_ledger(since=since)
    dates  = _fill_date_range(days)
    by_day: dict[str, int] = defaultdict(int)

    for entry in ledger:
        d = (entry.get("ts") or "")[:10]
        if d:
            by_day[d] += entry.get("total", 0)

    tokens_per_day = [by_day.get(d, 0) for d in dates]
    cost_per_day   = [round(t / 1000 * _COST_PER_1K_TOKENS, 4) for t in tokens_per_day]

    return {
        "labels":    dates,
        "tokens":    tokens_per_day,
        "cost_usd":  cost_per_day,
    }


def get_all_content_gaps(limit: int = 20) -> list[dict]:
    """
    Most frequent unanswered questions across all profiles.
    Returns [{question, count, slugs}, ...] sorted by count desc.
    """
    slugs = _all_active_slugs()
    counts: dict[str, int] = defaultdict(int)
    slug_map: dict[str, set] = defaultdict(set)

    for slug in slugs:
        for e in _load_all_events(slug):
            if not e.get("was_answered", True):
                q = (e.get("question") or "").strip().lower()
                if q:
                    counts[q] += 1
                    slug_map[q].add(slug)

    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [
        {"question": q, "count": c, "profiles": sorted(slug_map[q])}
        for q, c in ranked
    ]
