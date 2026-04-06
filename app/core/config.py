"""
config.py
---------
Central configuration loaded from environment variables + .env file.

All settings live here. No os.getenv() calls should appear outside this file.
Import `settings` wherever configuration is needed.

Each setting is declared as a CfgField that carries its own metadata
(env var name, default, secret flag, UI visibility, label, section) so nothing can
fall out of sync between the value definition and the admin display.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Base paths  (all relative so the app works anywhere — local or HF Spaces)
# ---------------------------------------------------------------------------
BASE_DIR       = Path(__file__).resolve().parent.parent.parent  # project root
PROFILES_DIR   = BASE_DIR / "profiles"
SYSTEM_DIR     = BASE_DIR / "system"
LOGS_DIR       = BASE_DIR / "logs"
STATIC_DIR     = BASE_DIR / "static"
TEMPLATES_DIR  = BASE_DIR / "templates"
DEFAULTS_DIR   = BASE_DIR / "app" / "defaults"

# Ensure critical dirs exist at import time
for _d in [PROFILES_DIR, SYSTEM_DIR, LOGS_DIR, STATIC_DIR]:
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# CfgField — self-describing configuration descriptor
# ---------------------------------------------------------------------------
_bool   = lambda v: v.strip().lower() in ("true", "1", "yes")
_upper  = lambda v: v.strip().upper()
_emails = lambda v: [e.strip() for e in v.split(",") if e.strip()]


class CfgField:
    """
    Descriptor for a single configuration field.

    Bundles the env var name, default value, and display metadata in one place
    so each setting is fully self-describing.  The admin config page is built
    by iterating Settings._cfg_fields — no separate mapping required.

    Params
    ------
    env     : str | None  – environment variable name; None for computed values
    default : Any         – value used when the env var is absent
    secret  : bool        – mask the value as "***" in the admin UI
    show    : bool        – include in the admin config display (default True)
    cast    : callable    – converts the raw env var string (e.g. int, float)
    label   : str | None  – human-readable display label (defaults to attr name)
    section : str         – grouping header shown in the admin config table
    """

    def __init__(self, env, default, *, secret=False, show=True, cast=None, label=None, section="General"):
        self.env     = env
        self.default = default
        self.secret  = secret
        self.show    = show
        self.cast    = cast
        self.label   = label
        self.section = section
        self._attr   = None   # set by __set_name__

    def __set_name__(self, owner, name):
        self._attr = f"_cfgval_{name}"
        if self.label is None:
            self.label = name
        if not hasattr(owner, "_cfg_fields"):
            owner._cfg_fields = []
        owner._cfg_fields.append((name, self))

    def _resolve(self):
        raw = os.environ.get(self.env) if self.env else None
        if raw is not None:
            return self.cast(raw) if self.cast else raw
        return self.default

    def __get__(self, obj, _=None):
        if obj is None:
            return self                        # class-level access returns descriptor
        if self._attr not in obj.__dict__:
            obj.__dict__[self._attr] = self._resolve()
        return obj.__dict__[self._attr]

    def __set__(self, obj, value):
        obj.__dict__[self._attr] = value


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Settings:
    """
    Application-wide settings.

    Every field is a CfgField so its env var name, default, and display metadata
    live alongside the declaration.  Use settings.get_config_display() to get
    the full table for the admin UI — automatically stays in sync.
    """

    # ── LLM ──────────────────────────────────────────────────────────────────
    OPENROUTER_BASE_URL = CfgField("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1",
                                   label="OpenRouter Base URL", section="LLM")
    OPENROUTER_API_KEY  = CfgField("OPENROUTER_API_KEY",  "",
                                   label="OpenRouter API Key",  secret=True, section="LLM")
    AI_MODEL            = CfgField("AI_MODEL",            "openai/gpt-4o-mini",
                                   label="AI Model", section="LLM")

    # ── Server ────────────────────────────────────────────────────────────────
    APP_VERSION = CfgField("APP_VERSION", "1.0.0",                label="App Version",    section="Server")
    IS_LOCAL    = CfgField("IS_LOCAL",    False,                   label="Is Local",       cast=_bool, section="Server")
    APP_URL     = CfgField("APP_URL",     "http://localhost:7860", label="Public App URL", section="Server")

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL = CfgField("LOG_LEVEL", "INFO", label="Log Level", cast=_upper, section="Logging")

    # ── Profile system (internal paths — not shown in UI) ─────────────────────
    INDEX_HISTORY_FILE = CfgField(None, SYSTEM_DIR / "index_history.log", show=False)

    # ── Billing / audit (internal paths) ──────────────────────────────────────
    TOKEN_LEDGER_FILE   = CfgField(None, SYSTEM_DIR / "token_ledger.jsonl", show=False)
    BILLING_ARCHIVE_DIR = CfgField(None, SYSTEM_DIR / "billing_archive",    show=False)
    BILLING_FILE        = CfgField(None, SYSTEM_DIR / "billing.json",       show=False)

    # ── RAG / indexing ────────────────────────────────────────────────────────
    PROFILE_CACHE_MINUTES = CfgField("PROFILE_CACHE_MINUTES",  20,    label="Profile Cache (min)", cast=int,   section="RAG / Indexing")
    FORCE_REINGEST        = CfgField("FORCE_PROFILE_REINGEST", False,  label="Force Re-ingest",    cast=_bool, section="RAG / Indexing")
    CHUNK_SIZE            = CfgField("CHUNK_SIZE",             1024,   label="Chunk Size",         cast=int,   section="RAG / Indexing")
    CHUNK_OVERLAP         = CfgField("CHUNK_OVERLAP",          128,    label="Chunk Overlap",      cast=int,   section="RAG / Indexing")
    RAG_TOP_K             = CfgField("RAG_TOP_K",              4,      label="RAG Top K",          cast=int,   section="RAG / Indexing")

    # ── Authentication ────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID     = CfgField("GOOGLE_CLIENT_ID",     "", label="Google Client ID",     secret=True, section="Authentication")
    GOOGLE_CLIENT_SECRET = CfgField("GOOGLE_CLIENT_SECRET", "", label="Google Client Secret", secret=True, section="Authentication")
    SESSION_SECRET_KEY   = CfgField("SESSION_SECRET_KEY",   "change-me-in-production",
                                    label="Session Secret Key", secret=True, section="Authentication")
    ADMIN_EMAILS         = CfgField("ADMIN_EMAILS",         [], label="Admin Emails",
                                    cast=_emails, section="Authentication")

    # ── Support ───────────────────────────────────────────────────────────────
    SUPPORT_EMAIL = CfgField("SUPPORT_EMAIL", "support@aiprofile.app", label="Support Email", section="Support")

    # ── Rate limiting ─────────────────────────────────────────────────────────
    CHAT_RATE_LIMIT = CfgField("CHAT_RATE_LIMIT", "20/minute", label="Chat Rate Limit", section="Security")

    # ── Billing / UPI ─────────────────────────────────────────────────────────
    UPI_VPA               = CfgField("UPI_VPA",               "",                    label="UPI VPA",               section="Billing")
    UPI_PAYEE_NAME        = CfgField("UPI_PAYEE_NAME",        "AI Profile Platform", label="UPI Payee Name",        section="Billing")
    PLATFORM_FEE_INR      = CfgField("PLATFORM_FEE_INR",      10.0,                  label="Platform Fee (INR)",    cast=float, section="Billing")
    BILLING_INTERVAL_DAYS = CfgField("BILLING_INTERVAL_DAYS", 30,                    label="Billing Interval (days)", cast=int, section="Billing")
    DONATION_MIN_INR      = CfgField("DONATION_MIN_INR",      10.0,                  label="Donation Min (INR)",      cast=float, section="Billing")
    DONATION_MAX_INR      = CfgField("DONATION_MAX_INR",      500.0,                 label="Donation Max (INR)",      cast=float, section="Billing")
    DONATION_UPI_VPA      = CfgField("DONATION_UPI_VPA",      "",                    label="Donation UPI VPA (optional, falls back to UPI VPA)", section="Billing")
    DONATION_UPI_NAME     = CfgField("DONATION_UPI_NAME",     "",                    label="Donation UPI Payee Name (optional)", section="Billing")

    # ── Notifications (optional) ──────────────────────────────────────────────
    PUSHOVER_USER_KEY  = CfgField("PUSHOVER_USER_KEY",  "", label="Pushover User Key",  secret=True, section="Notifications")
    PUSHOVER_API_TOKEN = CfgField("PUSHOVER_API_TOKEN", "", label="Pushover API Token", secret=True, section="Notifications")
    PUSHOVER_URL       = CfgField("PUSHOVER_URL",       "https://api.pushover.net/1/messages.json",
                                  label="Pushover URL", section="Notifications")
    SENDGRID_API_KEY    = CfgField("SENDGRID_API_KEY",    "", label="SendGrid API Key",    secret=True, section="Notifications")
    SENDGRID_FROM_EMAIL = CfgField("SENDGRID_FROM_EMAIL", "", label="SendGrid From Email",              section="Notifications")
    SENDGRID_URL        = CfgField("SENDGRID_URL",        "https://api.sendgrid.com/v3/mail/send",
                                   label="SendGrid URL", section="Notifications")

    # ── HF Dataset sync ───────────────────────────────────────────────────────
    HF_STORAGE_REPO              = CfgField("HF_STORAGE_REPO",              "",
                                            label="HF Storage Repo",              section="HF Sync")
    HF_TOKEN                     = CfgField("HF_TOKEN",                     "",
                                            label="HF Token",         secret=True, section="HF Sync")
    HF_LOG_SYNC_INTERVAL_MINUTES = CfgField("HF_LOG_SYNC_INTERVAL_MINUTES", 5,
                                            label="HF Log Sync Interval (min)", cast=int, section="HF Sync")

    # ── Paths (computed from module-level constants — shown in UI, no env var) ─
    BASE_DIR_STR      = CfgField(None, str(BASE_DIR),     label="Base Dir",      section="Paths")
    PROFILES_DIR_STR  = CfgField(None, str(PROFILES_DIR), label="Profiles Dir",  section="Paths")
    SYSTEM_DIR_STR    = CfgField(None, str(SYSTEM_DIR),   label="System Dir",    section="Paths")
    LOGS_DIR_STR      = CfgField(None, str(LOGS_DIR),     label="Logs Dir",      section="Paths")

    # ── Features (on/off switches) ────────────────────────────────────────────
    CAROUSEL_AI_THEME_ENABLED = CfgField("CAROUSEL_AI_THEME_ENABLED", True,
                                         label="Carousel AI Theme (owner portal)", cast=_bool, section="Features")

    # ── Admin display ─────────────────────────────────────────────────────────

    def get_config_display(self) -> list[dict]:
        """
        Return all show=True fields as a list of dicts with keys:
          label, value, source, section

        source values
        -------------
        "HF Env"  – running on HF Spaces and the env var is explicitly set
        ".env"    – running locally and the env var is explicitly set
        "Default" – env var not set; using the built-in default
        None      – computed path / constant with no corresponding env var
        """
        rows = []
        for _name, field in self._cfg_fields:
            if not field.show:
                continue

            val = getattr(self, _name)

            # Display value
            if field.secret:
                display = "***" if val else "NOT SET"
            elif isinstance(val, list):
                display = ", ".join(val) if val else "NOT SET"
            else:
                display = str(val) if val not in ("", None) else "NOT SET"

            # Source
            if field.env is None:
                source = None                                    # computed constant
            elif os.environ.get(field.env) is not None:
                source = ".env" if self.IS_LOCAL else "HF Env"
            else:
                source = "Default"

            rows.append({
                "label":   field.label,
                "value":   display,
                "source":  source,
                "section": field.section,
            })
        return rows


settings = Settings()
