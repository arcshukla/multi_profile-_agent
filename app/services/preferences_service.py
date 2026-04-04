"""
preferences_service.py
----------------------
Load and save per-profile owner preferences.

Storage: profiles/{slug}/config/preferences.json

Schema:
  {
    "notify_unanswered_email": false,
    "chat_history_limit":      10
  }

New keys are always merged in on load so older preference files are upgraded
transparently — callers always receive a fully-populated dict.
"""

import json

from app.core.config import PROFILES_DIR
from app.core.constants import CHAT_HISTORY_LIMIT_DEFAULT
from app.core.logging_config import get_logger

logger = get_logger(__name__)

_DEFAULTS: dict = {
    "notify_unanswered_email": False,
    "chat_history_limit":      CHAT_HISTORY_LIMIT_DEFAULT,
}


class PreferencesService:

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}   # slug → merged prefs

    def get(self, slug: str) -> dict:
        """
        Load preferences, merging with defaults so all keys are always present.
        Falls back to defaults if the file is absent or unreadable.
        Result is cached per-slug and served from memory on repeat calls.
        """
        if slug in self._cache:
            return self._cache[slug]

        path = PROFILES_DIR / slug / "config" / "preferences.json"
        stored: dict = {}
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error("Failed to load preferences for %s: %s", slug, e)
        # Merge: defaults first, then overwrite with stored values
        merged = {**_DEFAULTS, **stored}
        self._cache[slug] = merged
        return merged

    def save(self, slug: str, prefs: dict) -> None:
        path = PROFILES_DIR / slug / "config" / "preferences.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
        self._cache.pop(slug, None)   # invalidate so next get() reloads from disk
        from app.storage.hf_sync import hf_sync
        hf_sync.push_file(path)
        logger.info("Preferences saved for slug=%s", slug)


preferences_service = PreferencesService()
