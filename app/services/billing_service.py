"""
billing_service.py
------------------
Manages billing tiers, invoices, and UPI/QR code generation.

Storage:
  system/billing.json  — billing entries keyed by slug
  static/qr/           — QR PNG images, one per invoice

FREE profiles are never written to billing.json.
_get_entry() returns a default free BillingEntry for any missing slug.

Thread-safety: a single module-level lock guards all reads and writes.
"""

import json
import shutil
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from uuid import uuid4

from app.core.config import STATIC_DIR, settings
from app.core.logging_config import get_logger
from app.models.billing_models import (
    BillingEntry, BillingStatusResponse, BillingTier, DonationRecord,
    DonationStatus, Invoice, InvoiceStatus,
)
from app.storage.hf_sync import hf_sync

logger = get_logger(__name__)

_STORE   = settings.BILLING_FILE
_QR_DIR  = STATIC_DIR / "qr"
_LOCK    = threading.Lock()

# Graceful degrade if Pillow / qrcode not installed
try:
    import qrcode as _qrcode
    _QR_AVAILABLE = True
except ImportError:
    _QR_AVAILABLE = False
    logger.warning("qrcode[pil] not installed — QR images will not be generated")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_qr(record_id: str, upi_uri: str) -> str:
    """
    Save a QR PNG to static/qr/{record_id}.png.
    Returns the relative path string (e.g. "qr/don_abc.png").
    Exported at module level so payment_providers.py can import it.
    """
    qr_path = f"qr/{record_id}.png"
    if not _QR_AVAILABLE:
        return qr_path
    try:
        _QR_DIR.mkdir(parents=True, exist_ok=True)
        img = _qrcode.make(upi_uri)
        qr_file = _QR_DIR / f"{record_id}.png"
        img.save(str(qr_file))
        hf_sync.push_file(qr_file)
    except Exception as exc:
        logger.error("QR generation failed for %s: %s", record_id, exc, exc_info=True)
    return qr_path


def _build_donation_upi_uri(vpa: str, name: str, amount: float, record_id: str, note: str) -> str:
    """
    Build a UPI deep-link for a voluntary donation.
    Exported at module level so payment_providers.py can import it.
    """
    tn = quote(note or "Voluntary Donation", safe="")
    return (
        f"upi://pay?pa={vpa}&pn={quote(name, safe='')}"
        f"&am={amount:.2f}&cu=INR"
        f"&tn={tn}&tr={record_id}"
    )


class BillingService:
    """
    CRUD on billing.json + UPI/QR helpers.
    All public methods are thread-safe.
    """

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load(self) -> dict:
        """Load billing.json, falling back to numbered backups on parse failure."""
        max_bak = getattr(settings, "DATA_BACKUP_COUNT", 3)
        candidates = [_STORE] + [
            _STORE.parent / f"billing.bak{i}.json" for i in range(1, max_bak + 1)
        ]
        for path in candidates:
            try:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if path != _STORE:
                        logger.warning(
                            "billing.json unreadable — loaded from backup: %s", path.name
                        )
                    return data
            except json.JSONDecodeError as e:
                logger.error("Failed to parse %s: %s", path.name, e)
        return {}

    def _save(self, data: dict) -> None:
        """Rotate backups then write. Keeps up to DATA_BACKUP_COUNT backups."""
        max_bak = getattr(settings, "DATA_BACKUP_COUNT", 3)
        # Shift existing backups: bak2→bak3, bak1→bak2
        for i in range(max_bak - 1, 0, -1):
            src = _STORE.parent / f"billing.bak{i}.json"
            dst = _STORE.parent / f"billing.bak{i + 1}.json"
            if src.exists():
                shutil.copy2(src, dst)
        # Copy current file to bak1 before overwriting
        if _STORE.exists():
            shutil.copy2(_STORE, _STORE.parent / "billing.bak1.json")
        _STORE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        hf_sync.push_file(_STORE)

    def delete_profile(self, slug: str) -> None:
        """Remove a profile's billing entry entirely (called on hard delete)."""
        with _LOCK:
            data = self._load()
            if slug in data:
                data.pop(slug)
                self._save(data)
                logger.info("Billing entry removed for deleted profile '%s'", slug)

    def _get_entry(self, slug: str, data: dict) -> BillingEntry:
        raw = data.get(slug)
        if raw is None:
            return BillingEntry(slug=slug)
        return BillingEntry(**raw)

    def _set_entry(self, entry: BillingEntry, data: dict) -> None:
        data[entry.slug] = entry.model_dump()

    # ── UPI / QR ──────────────────────────────────────────────────────────────

    def _build_upi_uri(self, amount: float, invoice_id: str) -> str:
        vpa  = settings.UPI_VPA
        name = quote(settings.UPI_PAYEE_NAME, safe="")
        note = quote("Platform Fee", safe="")
        return (
            f"upi://pay?pa={vpa}&pn={name}"
            f"&am={amount:.2f}&cu=INR"
            f"&tn={note}&tr={invoice_id}"
        )

    def _generate_qr(self, invoice_id: str, upi_uri: str) -> str:
        """Delegate to module-level _generate_qr (shared with donation QR)."""
        return _generate_qr(invoice_id, upi_uri)

    # ── Public read ───────────────────────────────────────────────────────────

    def get_entry(self, slug: str) -> BillingEntry:
        with _LOCK:
            return self._get_entry(slug, self._load())

    def get_status(self, slug: str) -> BillingStatusResponse:
        entry   = self.get_entry(slug)
        today   = date.today().isoformat()
        pending = [
            inv for inv in entry.invoices
            if inv.status == InvoiceStatus.PENDING
        ]
        billing_due = any(inv.due_date <= today for inv in pending)
        # latest invoice = most recently created
        latest = entry.invoices[-1] if entry.invoices else None
        return BillingStatusResponse(
            slug=slug,
            tier=entry.tier,
            billing_due=billing_due,
            latest_invoice=latest,
        )

    def is_billing_due(self, slug: str) -> bool:
        return self.get_status(slug).billing_due

    # ── Public write ──────────────────────────────────────────────────────────

    def set_tier(
        self,
        slug:        str,
        new_tier:    BillingTier,
        admin_email: str,
    ) -> BillingEntry:
        """
        Change billing tier for a profile.
        Upgrading from FREE automatically creates the first invoice.
        Downgrading to FREE does NOT cancel open invoices (audit trail preserved).
        """
        with _LOCK:
            data  = self._load()
            entry = self._get_entry(slug, data)

            if entry.tier == new_tier:
                return entry  # no-op

            was_free   = entry.tier == BillingTier.FREE
            entry.tier            = new_tier
            entry.tier_changed_at = _utcnow()
            entry.tier_changed_by = admin_email

            self._set_entry(entry, data)
            self._save(data)

        logger.info("Billing tier for %s changed to %s by %s", slug, new_tier, admin_email)

        # Auto-create first invoice when upgrading from free (outside the lock
        # because create_invoice() acquires its own lock)
        if was_free and new_tier != BillingTier.FREE:
            self.create_invoice(slug)

        return self.get_entry(slug)

    def create_invoice(
        self,
        slug:         str,
        amount:       Optional[float] = None,
        period_start: Optional[str]   = None,
        period_end:   Optional[str]   = None,
    ) -> Invoice:
        """
        Create a new invoice for the profile (rolling period).
        Raises ValueError if a PENDING invoice already exists.
        Raises ValueError if UPI_VPA is not configured.
        """
        if not settings.UPI_VPA:
            raise ValueError(
                "UPI_VPA is not configured. Set the UPI_VPA environment variable."
            )

        amount       = amount or settings.PLATFORM_FEE_INR
        today        = date.today()
        period_start = period_start or today.isoformat()
        period_end   = period_end   or (today + timedelta(days=settings.BILLING_INTERVAL_DAYS)).isoformat()
        due_date     = period_end

        with _LOCK:
            data  = self._load()
            entry = self._get_entry(slug, data)

            # Guard: no duplicate open invoices
            open_inv = [i for i in entry.invoices if i.status == InvoiceStatus.PENDING]
            if open_inv:
                raise ValueError("An open invoice already exists for this profile.")

            invoice_id = "inv_" + uuid4().hex[:8]
            upi_uri    = self._build_upi_uri(amount, invoice_id)
            qr_path    = self._generate_qr(invoice_id, upi_uri)

            invoice = Invoice(
                id           = invoice_id,
                amount       = amount,
                period_start = period_start,
                period_end   = period_end,
                due_date     = due_date,
                upi_uri      = upi_uri,
                qr_path      = qr_path,
                created_at   = _utcnow(),
            )
            entry.invoices.append(invoice)
            self._set_entry(entry, data)
            self._save(data)

        logger.info("Invoice %s created for %s (₹%.2f)", invoice_id, slug, amount)
        return invoice

    def confirm_payment(
        self,
        slug:        str,
        invoice_id:  str,
        admin_email: str,
    ) -> Invoice:
        """
        Mark an invoice as paid.
        Raises ValueError if the invoice is not found or already confirmed.
        """
        with _LOCK:
            data  = self._load()
            entry = self._get_entry(slug, data)

            inv = next((i for i in entry.invoices if i.id == invoice_id), None)
            if inv is None:
                raise ValueError(f"Invoice {invoice_id!r} not found for profile {slug!r}.")
            if inv.status == InvoiceStatus.PAID:
                raise ValueError(f"Invoice {invoice_id!r} is already confirmed as paid.")

            inv.status       = InvoiceStatus.PAID
            inv.paid_at      = _utcnow()
            inv.confirmed_by = admin_email

            self._set_entry(entry, data)
            self._save(data)

        logger.info("Invoice %s confirmed paid by %s", invoice_id, admin_email)
        return inv

    def set_invoice_status(
        self,
        slug:       str,
        due_date:   str,
        new_status: str,
    ) -> Invoice:
        """
        Set invoice status to any value (Paid/Pending/Overdue).
        Used by the system billing table dropdown (admin bulk view).
        """
        try:
            status = InvoiceStatus(new_status.lower())
        except ValueError:
            raise ValueError(f"Invalid invoice status: {new_status!r}")
        with _LOCK:
            data  = self._load()
            entry = self._get_entry(slug, data)
            inv = next((i for i in entry.invoices if i.due_date == due_date), None)
            if inv is None:
                raise ValueError(f"No invoice with due_date {due_date!r} for profile {slug!r}.")
            inv.status = status
            if status == InvoiceStatus.PAID and not inv.paid_at:
                inv.paid_at = _utcnow()
            self._set_entry(entry, data)
            self._save(data)
        logger.info("Invoice due_date=%s for %s set to %s", due_date, slug, status.value)
        return inv

    def regenerate_qr(self, slug: str, invoice_id: str) -> Optional[str]:
        """
        Regenerate a missing QR PNG from the stored UPI URI.
        Returns the qr_path if successful, None otherwise.
        Used when QR files are lost (e.g. HF Spaces restart).
        """
        entry = self.get_entry(slug)
        inv   = next((i for i in entry.invoices if i.id == invoice_id), None)
        if inv is None:
            return None
        self._generate_qr(invoice_id, inv.upi_uri)
        return inv.qr_path

    # ── Donations (free-tier voluntary contributions to the platform) ──────────

    def create_donation(
        self,
        slug:   str,
        amount: float,
        note:   str = "",
        mode:   str = "upi_qr",
    ) -> DonationRecord:
        """
        Owner-initiated: generate a payment artefact (QR for upi_qr) for a
        voluntary donation to the platform. Multiple PENDING records are allowed.

        Raises ValueError if:
          - profile is not on the free tier
          - amount is outside DONATION_MIN_INR / DONATION_MAX_INR bounds
          - payment mode is unknown
        """
        min_amt = settings.DONATION_MIN_INR
        max_amt = settings.DONATION_MAX_INR
        if not (min_amt <= amount <= max_amt):
            raise ValueError(
                f"Donation amount must be between ₹{min_amt:.0f} and ₹{max_amt:.0f}."
            )

        from app.services.payment_providers import get_provider  # avoid circular at module level
        provider = get_provider(mode)

        with _LOCK:
            data  = self._load()
            entry = self._get_entry(slug, data)

            if entry.tier != BillingTier.FREE:
                raise ValueError("Donations are only available for free-tier profiles.")

            record_id = "don_" + uuid4().hex[:8]
            result    = provider.create_payment(record_id, amount, note)

            record = DonationRecord(
                id           = record_id,
                amount       = amount,
                payment_mode = result["payment_mode"],
                note         = note,
                upi_uri      = result.get("upi_uri"),
                qr_path      = result.get("qr_path"),
                provider_meta= result.get("provider_meta", {}),
                created_at   = _utcnow(),
            )
            entry.donations.append(record)
            self._set_entry(entry, data)
            self._save(data)

        logger.info("Donation %s created for %s (₹%.2f, mode=%s)", record_id, slug, amount, mode)
        return record

    def confirm_donation(
        self,
        slug:        str,
        donation_id: str,
        admin_email: str,
    ) -> DonationRecord:
        """
        Admin-initiated: mark a PENDING donation as CONFIRMED.
        Raises ValueError if the record is not found or already confirmed.
        Does NOT send the thank-you email — caller handles that.
        """
        with _LOCK:
            data  = self._load()
            entry = self._get_entry(slug, data)

            rec = next((d for d in entry.donations if d.id == donation_id), None)
            if rec is None:
                raise ValueError(f"Donation {donation_id!r} not found for profile {slug!r}.")
            if rec.status == DonationStatus.CONFIRMED:
                raise ValueError(f"Donation {donation_id!r} is already confirmed.")

            rec.status       = DonationStatus.CONFIRMED
            rec.confirmed_at = _utcnow()
            rec.confirmed_by = admin_email

            self._set_entry(entry, data)
            self._save(data)

        logger.info("Donation %s confirmed by %s for %s", donation_id, admin_email, slug)
        return rec

    def get_donations(self, slug: str) -> list[DonationRecord]:
        """Return all donation records for a profile, newest first."""
        entry = self.get_entry(slug)
        return list(reversed(entry.donations))

    def mark_donation_email_sent(self, slug: str, donation_id: str) -> None:
        """
        Set email_sent=True on a donation record after successful thank-you
        email dispatch. Idempotency guard — prevents re-sending on retry.
        """
        with _LOCK:
            data  = self._load()
            entry = self._get_entry(slug, data)
            rec   = next((d for d in entry.donations if d.id == donation_id), None)
            if rec is None:
                logger.warning("mark_donation_email_sent: record %s not found for %s", donation_id, slug)
                return
            rec.email_sent = True
            self._set_entry(entry, data)
            self._save(data)
        logger.info("Donation email flag set for %s/%s", slug, donation_id)


billing_service = BillingService()
