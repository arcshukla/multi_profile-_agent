"""
test_profile_service.py
-----------------------
Unit tests for ProfileService.

Covers: create, get, list, status updates, soft-delete, restore, hard-delete,
get_display_name, all 5 status states.

External dependencies (ChromaDB, HFSync, token_service) are monkeypatched.
"""

import pytest

from app.core.constants import STATUS_ENABLED, STATUS_DISABLED, STATUS_DELETED, STATUS_SUSPENDED, STATUS_SOFT_DELETED
from app.models.profile_models import CreateProfileRequest
from app.services.profile_service import ProfileService


@pytest.fixture
def svc(monkeypatch, isolate_data_dirs):
    """ProfileService with all external I/O neutralised."""

    # index_service.get_status → return minimal dict (avoids ChromaDB init)
    import app.services.index_service as idx
    monkeypatch.setattr(idx.index_service, "get_status", lambda slug: {
        "document_count": 0,
        "chunk_count": 0,
        "last_indexed": None,
    })
    monkeypatch.setattr(idx.index_service, "evict_engine", lambda slug: None)

    # prompt_service.ensure_prompts_file → no-op (avoids file creation side-effects)
    import app.services.prompt_service as ps
    monkeypatch.setattr(ps.prompt_service, "ensure_prompts_file", lambda slug: None)

    return ProfileService()


def _make_request(name: str, email: str = None, status: str = STATUS_ENABLED) -> CreateProfileRequest:
    email = email or f"{name.lower().replace(' ', '.')}@example.com"
    return CreateProfileRequest(name=name, owner_email=email, status=status)


# ── Create ────────────────────────────────────────────────────────────────────

def test_create_profile(svc, isolate_data_dirs):
    profile = svc.create_profile(_make_request("Alice Smith"))
    assert profile.slug == "alice-smith"
    assert profile.name == "Alice Smith"
    assert profile.status == STATUS_ENABLED


def test_create_profile_slug_uniqueness(svc, isolate_data_dirs):
    svc.create_profile(_make_request("Bob", "bob1@example.com"))
    profile2 = svc.create_profile(_make_request("Bob", "bob2@example.com"))
    assert profile2.slug != "bob"  # must get a suffixed slug


def test_create_profile_duplicate_email_raises(svc, isolate_data_dirs):
    svc.create_profile(_make_request("Carol", "carol@example.com"))
    with pytest.raises(ValueError):
        svc.create_profile(_make_request("Carol Again", "carol@example.com"))


# ── Get ───────────────────────────────────────────────────────────────────────

def test_get_profile(svc, isolate_data_dirs):
    svc.create_profile(_make_request("Dana"))
    profile = svc.get_profile("dana")
    assert profile is not None
    assert profile.name == "Dana"


def test_get_missing_profile_returns_none(svc, isolate_data_dirs):
    assert svc.get_profile("no-such-slug") is None


def test_profile_exists(svc, isolate_data_dirs):
    svc.create_profile(_make_request("Eve"))
    assert svc.profile_exists("eve") is True
    assert svc.profile_exists("ghost") is False


# ── get_display_name ──────────────────────────────────────────────────────────

def test_get_display_name_found(svc, isolate_data_dirs):
    svc.create_profile(_make_request("Frank Ocean", "frank@example.com"))
    assert svc.get_display_name("frank-ocean") == "Frank Ocean"


def test_get_display_name_fallback(svc, isolate_data_dirs):
    assert svc.get_display_name("nobody-here") == "nobody-here"


# ── List & filter ─────────────────────────────────────────────────────────────

def test_list_all_profiles(svc, isolate_data_dirs):
    svc.create_profile(_make_request("G1", "g1@example.com"))
    svc.create_profile(_make_request("G2", "g2@example.com"))
    profiles = svc.list_profiles()
    slugs = [p.slug for p in profiles]
    assert "g1" in slugs
    assert "g2" in slugs


def test_list_profiles_status_filter(svc, isolate_data_dirs):
    svc.create_profile(_make_request("H1", "h1@example.com", STATUS_ENABLED))
    svc.create_profile(_make_request("H2", "h2@example.com", STATUS_DISABLED))
    enabled = svc.list_profiles(status_filter=STATUS_ENABLED)
    assert all(p.status == STATUS_ENABLED for p in enabled)


# ── Status update ─────────────────────────────────────────────────────────────

def test_update_status(svc, isolate_data_dirs):
    svc.create_profile(_make_request("Ivy"))
    updated = svc.update_status("ivy", STATUS_DISABLED)
    assert updated is not None
    assert updated.status == STATUS_DISABLED


def test_update_status_invalid_raises(svc, isolate_data_dirs):
    svc.create_profile(_make_request("Jay"))
    with pytest.raises(ValueError):
        svc.update_status("jay", "invalid-status")


def test_update_status_suspended(svc, isolate_data_dirs):
    svc.create_profile(_make_request("Kelly"))
    updated = svc.update_status("kelly", STATUS_SUSPENDED)
    assert updated is not None
    assert updated.status == STATUS_SUSPENDED


def test_update_status_soft_deleted(svc, isolate_data_dirs):
    svc.create_profile(_make_request("Liam"))
    updated = svc.update_status("liam", STATUS_SOFT_DELETED)
    assert updated is not None
    assert updated.status == STATUS_SOFT_DELETED


# ── Soft-delete & restore ─────────────────────────────────────────────────────

def test_soft_delete(svc, isolate_data_dirs):
    """soft_delete() sets status to soft_deleted (not the legacy 'deleted')."""
    svc.create_profile(_make_request("Kim"))
    ok = svc.soft_delete("kim")
    assert ok is True
    entry = svc.get_entry("kim")
    assert entry.status == STATUS_SOFT_DELETED


def test_restore_soft_deleted(svc, isolate_data_dirs):
    """restore_deleted() accepts soft_deleted status and re-enables profile."""
    svc.create_profile(_make_request("Lee"))
    svc.soft_delete("lee")
    restored = svc.restore_deleted("lee")
    assert restored is not None
    assert restored.status == STATUS_ENABLED


def test_restore_legacy_deleted(svc, isolate_data_dirs):
    """
    restore_deleted() accepts the legacy 'deleted' value.
    Note: the migration auto-converts 'deleted' → 'soft_deleted' on every load,
    so get_entry() will return 'soft_deleted' even after writing 'deleted'.
    restore_deleted() handles both soft_deleted and deleted.
    """
    svc.create_profile(_make_request("Leo"))
    from app.services.user_service import user_service
    user_service.update_status("leo", STATUS_DELETED)
    # Migration converts 'deleted' → 'soft_deleted' on load
    entry = svc.get_entry("leo")
    assert entry.status == STATUS_SOFT_DELETED
    restored = svc.restore_deleted("leo")
    assert restored is not None
    assert restored.status == STATUS_ENABLED


def test_restore_non_deleted_returns_none(svc, isolate_data_dirs):
    svc.create_profile(_make_request("Mia"))
    result = svc.restore_deleted("mia")  # not deleted — should fail
    assert result is None


def test_restore_suspended_returns_none(svc, isolate_data_dirs):
    """restore_deleted() must NOT restore suspended profiles — that's admin-only."""
    svc.create_profile(_make_request("Max"))
    svc.update_status("max", STATUS_SUSPENDED)
    result = svc.restore_deleted("max")
    assert result is None


# ── Hard delete ───────────────────────────────────────────────────────────────

def test_hard_delete(svc, isolate_data_dirs, monkeypatch):
    import app.services.token_service as ts
    monkeypatch.setattr(ts.token_service, "get_profile", lambda slug: {})
    monkeypatch.setattr(ts.token_service, "get_ledger", lambda **kw: [])

    svc.create_profile(_make_request("Nina"))
    ok = svc.hard_delete("nina")
    assert ok is True
    assert svc.get_profile("nina") is None


def test_hard_delete_missing_returns_false(svc, isolate_data_dirs, monkeypatch):
    import app.services.token_service as ts
    monkeypatch.setattr(ts.token_service, "get_profile", lambda slug: {})
    monkeypatch.setattr(ts.token_service, "get_ledger", lambda **kw: [])
    assert svc.hard_delete("ghost-slug") is False
