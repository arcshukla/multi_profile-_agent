"""
profile_service.py
------------------
Business logic layer for profile management.

Sits between the API routes and the storage layer.
Coordinates: UserService (owner registry) + ProfileFileStorage + IndexService + PromptService.

Rules:
  - API routes call service methods, not storage directly
  - Service methods validate, orchestrate, and log
  - Owner registry reads/writes go exclusively through UserService
  - Filesystem operations go through ProfileFileStorage
"""

from pathlib import Path
from typing import Optional

from app.core.constants import STATUS_ENABLED, STATUS_DISABLED, STATUS_DELETED
from app.core.logging_config import get_logger, get_profile_logger
from app.models.profile_models import CreateProfileRequest, ProfileResponse
from app.models.user_models import UserEntity
from app.services.user_service import user_service
from app.storage.file_storage import ProfileFileStorage
from app.utils.slug_utils import slugify, unique_slug
from app.services.prompt_service import prompt_service

logger = get_logger(__name__)


class ProfileService:
    """
    Profile lifecycle management.

    Operations:
      create, get, list, update status, delete, soft-delete
    """

    # ── Read ──────────────────────────────────────────────────────────────────

    def list_profiles(
        self,
        status_filter: Optional[str] = None,
        name_filter: Optional[str] = None,
        slug_filter: Optional[str] = None,
    ) -> list[ProfileResponse]:
        """Return all profiles, optionally filtered. Enriches with runtime data."""
        owners = user_service.list_owners()

        if status_filter:
            owners = [o for o in owners if o.status == status_filter]
        if name_filter:
            owners = [o for o in owners if name_filter.lower() in o.name.lower()]
        if slug_filter:
            owners = [o for o in owners if slug_filter.lower() in o.slug.lower()]

        return [self._enrich(o) for o in owners]

    def get_profile(self, slug: str) -> Optional[ProfileResponse]:
        owner = user_service.get_user_by_slug(slug)
        return self._enrich(owner) if owner else None

    def get_entry(self, slug: str) -> Optional[UserEntity]:
        return user_service.get_user_by_slug(slug)

    def get_display_name(self, slug: str) -> str:
        """Return the profile owner's display name, falling back to slug if not found."""
        owner = user_service.get_user_by_slug(slug)
        return owner.name if owner else slug

    def profile_exists(self, slug: str) -> bool:
        return user_service.get_user_by_slug(slug) is not None

    # ── Write ─────────────────────────────────────────────────────────────────

    def create_profile(self, req: CreateProfileRequest) -> ProfileResponse:
        """
        Create a new profile:
          1. Derive slug from name
          2. Validate uniqueness
          3. Create filesystem structure
          4. Write default prompts
          5. Register owner in users.json
        """
        existing_slugs = [o.slug for o in user_service.list_owners()]
        slug = unique_slug(req.name, existing_slugs)
        if not slug:
            raise ValueError(f"Cannot derive a valid slug from name: '{req.name}'")

        # Create filesystem directories
        fs = ProfileFileStorage(slug)
        fs.create_directories()

        # Write default prompts file
        prompt_service.ensure_prompts_file(slug)

        # Register in users.json
        ok, err = user_service.add_user(
            email=req.owner_email,
            name=req.name,
            slug=slug,
            status=req.status,
        )
        if not ok:
            raise ValueError(err)

        plog = get_profile_logger(slug)
        plog.info("Profile created | name='%s' | slug='%s'", req.name, slug)
        logger.info("Profile created: '%s' (%s) owner=%s", req.name, slug, req.owner_email)

        return self._enrich(user_service.get_user_by_slug(slug))

    def update_status(self, slug: str, status: str) -> Optional[ProfileResponse]:
        """Enable or disable a profile."""
        if status not in (STATUS_ENABLED, STATUS_DISABLED):
            raise ValueError(f"Invalid status: {status}")
        ok, err = user_service.update_status(slug, status)
        if not ok:
            return None
        get_profile_logger(slug).info("Status updated: %s", status)
        return self.get_profile(slug)

    def soft_delete(self, slug: str) -> bool:
        """Mark profile as deleted (keeps files, removes from active list)."""
        ok, _ = user_service.update_status(slug, STATUS_DELETED)
        if ok:
            get_profile_logger(slug).info("Profile soft-deleted")
        return ok

    def hard_delete(self, slug: str) -> bool:
        """
        Permanently delete profile:
          1. Archive billing data to system/billing_archive/{slug}/
          2. Remove from users.json
          3. Delete filesystem (docs, chroma, config)
        """
        # Archive billing data before any deletion
        self._archive_billing_data(slug)

        # Remove from registry
        removed = user_service.remove_user_by_slug(slug)
        if not removed:
            return False

        # Evict engine cache
        try:
            from app.services.index_service import index_service
            index_service.evict_engine(slug)
        except Exception as e:
            logger.warning("Failed to evict engine cache for '%s': %s", slug, e)

        # Clear system-level records so a same-slug recreate starts clean
        try:
            from app.services.token_service import token_service
            token_service.delete_profile(slug)
        except Exception as e:
            logger.warning("Failed to clear token data for '%s': %s", slug, e)

        try:
            from app.services.billing_service import billing_service
            billing_service.delete_profile(slug)
        except Exception as e:
            logger.warning("Failed to clear billing data for '%s': %s", slug, e)

        try:
            from app.services.index_service import index_service
            index_service.clear_slug_history(slug)
        except Exception as e:
            logger.warning("Failed to write index history purge marker for '%s': %s", slug, e)

        # Delete filesystem
        ProfileFileStorage(slug).delete_all()

        get_profile_logger(slug).info("Profile permanently deleted")
        logger.info("Profile hard-deleted: '%s'", slug)
        return True

    def _archive_billing_data(self, slug: str) -> None:
        """
        Copy billing-critical records to system/billing_archive/{slug}/ before deletion.
        Failures are logged but never block the deletion.
        """
        import json
        import shutil
        from datetime import datetime, timezone
        from app.core.config import settings
        from app.services.token_service import token_service

        archive_dir = settings.BILLING_ARCHIVE_DIR / slug
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)

            usage = token_service.get_profile(slug)
            snapshot = {
                "slug":           slug,
                "archived_at":    datetime.now(timezone.utc).isoformat(),
                "token_usage":    usage,
                "ledger_entries": token_service.get_ledger(slug=slug),
            }
            (archive_dir / "billing_snapshot.json").write_text(
                json.dumps(snapshot, indent=2), encoding="utf-8"
            )

            chat_src = ProfileFileStorage(slug).chat_events_path
            if chat_src.exists():
                shutil.copy2(chat_src, archive_dir / "chat_events.jsonl")

            logger.info("Billing archive created for '%s' at %s", slug, archive_dir)
        except Exception as e:
            logger.error("Failed to archive billing data for '%s': %s", slug, e)

    def restore_deleted(self, slug: str) -> Optional[ProfileResponse]:
        """Re-enable a soft-deleted profile."""
        entry = user_service.get_user_by_slug(slug)
        if not entry or entry.status != STATUS_DELETED:
            return None
        user_service.update_status(slug, STATUS_ENABLED)
        get_profile_logger(slug).info("Profile restored from deleted state")
        return self.get_profile(slug)

    # ── Enrichment ────────────────────────────────────────────────────────────

    def _enrich(self, owner: UserEntity) -> ProfileResponse:
        """
        Augment an owner record with runtime data:
          - has_photo (filesystem check)
          - document_count
          - chunk_count (ChromaDB)
          - last_indexed (from index_history.log)
        """
        from app.services.index_service import index_service

        fs = ProfileFileStorage(owner.slug)
        status_info = index_service.get_status(owner.slug)

        has_photo = fs.has_photo()
        photo_ts  = int(fs.photo_path.stat().st_mtime) if has_photo else 0
        return ProfileResponse(
            name=owner.name,
            slug=owner.slug,
            status=owner.status,
            base_folder=f"profiles/{owner.slug}",   # computed — not stored
            has_photo=has_photo,
            photo_ts=photo_ts,
            document_count=status_info.get("document_count", 0),
            chunk_count=status_info.get("chunk_count", 0),
            last_indexed=status_info.get("last_indexed"),
        )


# Singleton
profile_service = ProfileService()
