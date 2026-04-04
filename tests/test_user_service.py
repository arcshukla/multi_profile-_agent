"""
test_user_service.py
--------------------
Unit tests for UserService — the owner registry stored in system/users.json.

Tests cover: add, get, list, update status, remove, resolve_session, slug uniqueness.
All file I/O is redirected to tmp_path via the conftest isolate_data_dirs fixture.
"""

import pytest

from app.core.constants import STATUS_ENABLED, STATUS_DISABLED, STATUS_DELETED, STATUS_SUSPENDED, STATUS_SOFT_DELETED
from app.services.user_service import UserService


@pytest.fixture
def svc():
    """Fresh UserService instance for each test (no singleton state leakage)."""
    return UserService()


# ── Add & get ─────────────────────────────────────────────────────────────────

def test_add_and_get_by_email(svc, isolate_data_dirs):
    ok, err = svc.add_user(email="alice@example.com", name="Alice", slug="alice", status=STATUS_ENABLED)
    assert ok is True
    assert not err   # empty string on success

    user = svc.get_user("alice@example.com")
    assert user is not None
    assert user.name == "Alice"
    assert user.slug == "alice"
    assert user.status == STATUS_ENABLED


def test_get_user_by_slug(svc, isolate_data_dirs):
    email = "bobbyslug@example.com"
    svc.add_user(email=email, name="Bob", slug="bob-unique", status=STATUS_ENABLED)
    user = svc.get_user_by_slug("bob-unique")
    assert user is not None
    assert user.email == email


def test_get_missing_user_returns_none(svc, isolate_data_dirs):
    assert svc.get_user("nobody@example.com") is None
    assert svc.get_user_by_slug("nobody") is None


# ── Duplicate guards ──────────────────────────────────────────────────────────

def test_duplicate_email_rejected(svc, isolate_data_dirs):
    svc.add_user(email="dup@example.com", name="First", slug="first", status=STATUS_ENABLED)
    ok, err = svc.add_user(email="dup@example.com", name="Second", slug="second", status=STATUS_ENABLED)
    assert ok is False
    assert err  # non-empty error message


def test_duplicate_slug_rejected(svc, isolate_data_dirs):
    svc.add_user(email="a@example.com", name="A", slug="same-slug", status=STATUS_ENABLED)
    ok, err = svc.add_user(email="b@example.com", name="B", slug="same-slug", status=STATUS_ENABLED)
    assert ok is False
    assert "slug" in (err or "").lower()


# ── Status updates ────────────────────────────────────────────────────────────

def test_update_status_disabled(svc, isolate_data_dirs):
    svc.add_user(email="c@example.com", name="C", slug="c-slug", status=STATUS_ENABLED)
    ok, err = svc.update_status("c-slug", STATUS_DISABLED)
    assert ok is True
    user = svc.get_user_by_slug("c-slug")
    assert user.status == STATUS_DISABLED


def test_update_status_deleted_migrates_to_soft_deleted(svc, isolate_data_dirs):
    """
    Writing the legacy STATUS_DELETED ('deleted') is allowed by update_status,
    but it is immediately migrated to 'soft_deleted' by _load() on next read.
    """
    svc.add_user(email="d@example.com", name="D", slug="d-slug", status=STATUS_ENABLED)
    ok, _ = svc.update_status("d-slug", STATUS_DELETED)
    assert ok is True
    user = svc.get_user_by_slug("d-slug")
    # Migration converts 'deleted' → 'soft_deleted' on read-back
    assert user.status == STATUS_SOFT_DELETED


def test_update_status_missing_slug_fails(svc, isolate_data_dirs):
    ok, _ = svc.update_status("ghost-slug", STATUS_DISABLED)
    assert ok is False


# ── Remove ────────────────────────────────────────────────────────────────────

def test_remove_user_by_slug(svc, isolate_data_dirs):
    svc.add_user(email="rem@example.com", name="Rem", slug="rem", status=STATUS_ENABLED)
    removed = svc.remove_user_by_slug("rem")
    assert removed is True
    assert svc.get_user_by_slug("rem") is None


def test_remove_nonexistent_returns_false(svc, isolate_data_dirs):
    assert svc.remove_user_by_slug("ghost") is False


# ── List ──────────────────────────────────────────────────────────────────────

def test_list_owners(svc, isolate_data_dirs):
    svc.add_user(email="e1@example.com", name="E1", slug="e1", status=STATUS_ENABLED)
    svc.add_user(email="e2@example.com", name="E2", slug="e2", status=STATUS_DISABLED)
    owners = svc.list_owners()
    slugs = [o.slug for o in owners]
    assert "e1" in slugs
    assert "e2" in slugs


# ── Session resolution ────────────────────────────────────────────────────────

def test_resolve_session_known_user(svc, isolate_data_dirs, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "ADMIN_EMAILS", [])
    svc.add_user(email="sess@example.com", name="Sess", slug="sess", status=STATUS_ENABLED)
    result = svc.resolve_session("sess@example.com", "Sess")
    assert result is not None
    assert result["role"] == "owner"
    assert result["slug"] == "sess"


def test_resolve_session_admin_email(svc, isolate_data_dirs, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "ADMIN_EMAILS", ["admin@example.com"])
    result = svc.resolve_session("admin@example.com", "Admin User")
    assert result is not None
    assert result["role"] == "admin"


def test_resolve_session_unknown_returns_none(svc, isolate_data_dirs, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "ADMIN_EMAILS", [])
    result = svc.resolve_session("nobody@example.com", "Nobody")
    assert result is None


# ── New status values ─────────────────────────────────────────────────────────

def test_update_status_suspended(svc, isolate_data_dirs):
    svc.add_user(email="sus@example.com", name="Sus", slug="sus", status=STATUS_ENABLED)
    ok, _ = svc.update_status("sus", STATUS_SUSPENDED)
    assert ok is True
    assert svc.get_user_by_slug("sus").status == STATUS_SUSPENDED


def test_update_status_soft_deleted(svc, isolate_data_dirs):
    svc.add_user(email="sd@example.com", name="Sd", slug="sd", status=STATUS_ENABLED)
    ok, _ = svc.update_status("sd", STATUS_SOFT_DELETED)
    assert ok is True
    assert svc.get_user_by_slug("sd").status == STATUS_SOFT_DELETED


def test_legacy_deleted_migration(svc, isolate_data_dirs):
    """
    When users.json contains a legacy 'deleted' status, _load() must migrate
    it to 'soft_deleted' automatically.
    """
    import json
    import app.services.user_service as us_mod

    # Write a users.json file with the legacy 'deleted' status value
    users_file = us_mod._USERS_FILE
    users_file.write_text(json.dumps({
        "legacy@example.com": {
            "slug": "legacy-slug",
            "name": "Legacy",
            "status": "deleted",  # old value
            "created_at": "2025-01-01T00:00:00Z",
        }
    }), encoding="utf-8")

    # Create fresh service so cache is empty
    fresh = UserService()
    user = fresh.get_user_by_slug("legacy-slug")
    assert user is not None
    assert user.status == STATUS_SOFT_DELETED, "legacy 'deleted' must be normalised to 'soft_deleted'"

    # Confirm the file on disk was also updated
    on_disk = json.loads(users_file.read_text(encoding="utf-8"))
    assert on_disk["legacy@example.com"]["status"] == STATUS_SOFT_DELETED


def test_re_registration_blocked_for_suspended(svc, isolate_data_dirs, monkeypatch):
    """
    An email already in users.json cannot create a new profile regardless of status.
    resolve_session returns their existing record even when suspended.
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "ADMIN_EMAILS", [])
    svc.add_user(email="blocked@example.com", name="B", slug="blocked", status=STATUS_SUSPENDED)
    result = svc.resolve_session("blocked@example.com", "B")
    # Must return their existing record — not None — so they cannot register fresh
    assert result is not None
    assert result["slug"] == "blocked"


# ── Backup rotation ───────────────────────────────────────────────────────────

def test_backup_rotation_creates_bak1(svc, isolate_data_dirs):
    """After two saves, users.bak1.json must exist."""
    svc.add_user(email="bak@example.com", name="Bak", slug="bak", status=STATUS_ENABLED)
    svc.add_user(email="bak2@example.com", name="Bak2", slug="bak2", status=STATUS_ENABLED)
    from app.core.config import SYSTEM_DIR
    bak1 = SYSTEM_DIR / "users.bak1.json"
    assert bak1.exists(), "users.bak1.json should have been created by rotation"
