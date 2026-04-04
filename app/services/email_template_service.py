"""
email_template_service.py
--------------------------
Manages named email templates editable by admins.

Storage:
  Defaults : app/defaults/email_templates.json  (ships with the repo, never written)
  Overrides: system/email_templates.json         (written only when admin saves changes,
                                                  synced to HF Dataset alongside system/)

On startup the service reads from app/defaults/email_templates.json.
When an admin saves changes those are written to system/email_templates.json
which takes priority on subsequent reads.  Deleting system/email_templates.json
restores all templates to their built-in defaults.

Currently defined templates
---------------------------
  unanswered_question — sent to a profile owner when a visitor asks an
                        unanswered question and the owner has opted in.

Available placeholders (all templates)
--------------------------------------
  {owner_name}  — owner's display name
  {question}    — the question the visitor asked
  {session_id}  — chat session identifier
  {slug}        — profile slug
  {chat_url}    — public URL for the owner's chat page
  {owner_url}   — URL for the owner's portal (preferences / docs)
"""

import json

from app.core.config import SYSTEM_DIR, DEFAULTS_DIR
from app.core.logging_config import get_logger

logger = get_logger(__name__)

_STORE        = SYSTEM_DIR   / "email_templates.json"   # admin overrides (HF-synced)
_DEFAULTS_FILE = DEFAULTS_DIR / "email_templates.json"  # shipped defaults (repo)
_LAYOUT_FILE   = DEFAULTS_DIR / "email_layout.html"     # shared logo/wrapper (never overridden)


def _load_defaults() -> dict[str, dict]:
    """Load the built-in default templates from app/defaults/email_templates.json."""
    try:
        return json.loads(_DEFAULTS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed to load default email templates from %s: %s", _DEFAULTS_FILE, e)
        return {}


class EmailTemplateService:
    """
    Load and save admin-editable email templates.

    Admins may change subject, body_text, and body_html for each named template.
    Placeholder variables (e.g. {question}) must be preserved for the sending
    code to substitute them correctly.
    """

    def get_templates(self) -> dict[str, dict]:
        """
        Return all templates.
        Reads defaults from app/defaults/email_templates.json.
        If system/email_templates.json exists (admin has saved changes), those
        fields override the defaults.
        """
        defaults = _load_defaults()
        if not _STORE.exists():
            return defaults

        try:
            raw = json.loads(_STORE.read_text(encoding="utf-8"))
            merged = _copy(defaults)
            for key, val in raw.items():
                if key in merged and isinstance(val, dict):
                    for field in ("subject", "body_text", "body_html"):
                        if field in val:
                            merged[key][field] = val[field]
            return merged
        except Exception as e:
            logger.warning("Failed to read email_templates.json — using defaults: %s", e)
            return defaults

    def get(self, name: str) -> dict | None:
        """Return a single template by name, or None if not found."""
        return self.get_templates().get(name)

    def update_template(self, name: str, subject: str, body_text: str, body_html: str) -> bool:
        """
        Update one template's subject, body_text, and body_html and persist.
        Returns True on success, False if name is unknown.
        """
        if name not in _load_defaults():
            logger.warning("Unknown email template name: '%s'", name)
            return False

        templates = self.get_templates()
        templates[name]["subject"]   = subject
        templates[name]["body_text"] = body_text
        templates[name]["body_html"] = body_html
        return self._save(templates)

    def restore_defaults(self, name: str | None = None) -> bool:
        """
        Restore templates to defaults.
        If name is given, restores only that template; otherwise restores all.
        """
        if name:
            defaults = _load_defaults()
            if name not in defaults:
                logger.warning("Unknown email template name for restore: '%s'", name)
                return False
            templates = self.get_templates()
            templates[name]["subject"]   = defaults[name]["subject"]
            templates[name]["body_text"] = defaults[name]["body_text"]
            templates[name]["body_html"] = defaults[name]["body_html"]
            result = self._save(templates)
            if result:
                logger.info("Email template '%s' restored to default", name)
            return result
        else:
            try:
                if _STORE.exists():
                    _STORE.unlink()
                logger.info("All email templates restored to defaults")
                return True
            except Exception as e:
                logger.error("Failed to restore email template defaults: %s", e)
                return False

    def _save(self, templates: dict) -> bool:
        try:
            payload = {
                k: {
                    "subject":   v["subject"],
                    "body_text": v["body_text"],
                    "body_html": v["body_html"],
                }
                for k, v in templates.items()
            }
            _STORE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info("Email templates saved to %s", _STORE)
            return True
        except Exception as e:
            logger.error("Failed to save email templates: %s", e)
            return False

    def wrap_layout(self, body_html_fragment: str) -> str:
        """
        Wrap an inner body_html fragment with the shared email layout
        (ProfileSpark logo header, outer table, background).

        The layout file uses a single ``{body_content}`` placeholder.
        All other ``{placeholder}`` tokens in the fragment are preserved
        so callers can still call ``.format(**vars_)`` afterwards.
        """
        try:
            layout = _LAYOUT_FILE.read_text(encoding="utf-8")
            return layout.replace("{body_content}", body_html_fragment)
        except Exception as e:
            logger.warning("email_layout.html unreadable, sending fragment as-is: %s", e)
            return body_html_fragment

    def render(self, name: str, vars_: dict) -> dict | None:
        """
        Fetch template *name*, substitute *vars_* into all text fields, and
        wrap body_html with the shared email layout.

        Returns a dict with keys ``subject``, ``body_text``, ``body_html``
        ready to hand directly to ``sendgrid_service.send()``.
        Returns None if the template name is unknown.

        All rendering is done here so callers (notification_service) only need
        to assemble a vars dict and dispatch — they never touch format() or the
        layout wrapper.
        """
        tmpl = self.get(name)
        if tmpl is None:
            logger.warning("render: unknown email template '%s'", name)
            return None
        try:
            fragment   = tmpl["body_html"].format(**vars_)
            return {
                "subject":   tmpl["subject"].format(**vars_),
                "body_text": tmpl["body_text"].format(**vars_),
                "body_html": self.wrap_layout(fragment),
            }
        except KeyError as e:
            logger.error("render: missing placeholder %s in template '%s'", e, name)
            return None


def _copy(d: dict) -> dict:
    return {k: dict(v) for k, v in d.items()}


# Singleton
email_template_service = EmailTemplateService()
