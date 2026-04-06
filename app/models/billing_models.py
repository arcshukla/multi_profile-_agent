"""
billing_models.py
-----------------
Pydantic models for the billing subsystem.

Phase 1: flat UPI/QR platform fee.
Phase 2 (future): configurable billing engine with multiple dimensions
  (token overage, chat message count, session count).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel


# ── Enums ──────────────────────────────────────────────────────────────────

class BillingTier(str, Enum):
    FREE             = "free"
    PAID_INDIVIDUAL  = "paid_individual"


class InvoiceStatus(str, Enum):
    PENDING = "pending"
    PAID    = "paid"
    OVERDUE = "overdue"


class DonationStatus(str, Enum):
    PENDING   = "pending"    # QR generated, awaiting payment + admin confirmation
    CONFIRMED = "confirmed"  # Admin confirmed receipt of payment


# ── Donation ───────────────────────────────────────────────────────────────

class DonationRecord(BaseModel):
    id:            str                                         # "don_{uuid8}"
    amount:        float                                       # INR, owner-chosen
    currency:      str = "INR"
    payment_mode:  str = "upi_qr"                             # str not enum — extensible
    note:          str = ""                                    # optional owner note
    upi_uri:       Optional[str] = None                       # platform UPI deep-link
    qr_path:       Optional[str] = None                       # relative to STATIC_DIR
    provider_meta: dict = {}                                   # future: razorpay order_id, etc.
    status:        DonationStatus = DonationStatus.PENDING
    created_at:    str                                         # ISO UTC
    confirmed_at:  Optional[str] = None
    confirmed_by:  Optional[str] = None                        # admin email
    email_sent:    bool = False                                # idempotency: thank-you email sent?


# ── Invoice ────────────────────────────────────────────────────────────────

class Invoice(BaseModel):
    id:           str            # "inv_{uuid8}"
    amount:       float          # INR
    currency:     str = "INR"
    period_start: str            # "YYYY-MM-DD"
    period_end:   str            # "YYYY-MM-DD"
    due_date:     str            # "YYYY-MM-DD" (= period_end for Phase 1)
    status:       InvoiceStatus = InvoiceStatus.PENDING
    upi_uri:      str            # full upi:// deep-link
    qr_path:      str            # relative to SYSTEM_DIR, e.g. "qr/inv_xxx.png"
    created_at:   str            # ISO UTC
    paid_at:      Optional[str] = None
    confirmed_by: Optional[str] = None   # admin email


# ── BillingEntry (per profile) ─────────────────────────────────────────────

class BillingEntry(BaseModel):
    slug:            str
    tier:            BillingTier = BillingTier.FREE
    tier_changed_at: Optional[str] = None
    tier_changed_by: Optional[str] = None
    invoices:        list[Invoice] = []
    donations:       list[DonationRecord] = []


# ── API request / response shapes ─────────────────────────────────────────

class ChangeTierRequest(BaseModel):
    tier: BillingTier


class BillingStatusResponse(BaseModel):
    slug:           str
    tier:           BillingTier
    billing_due:    bool            # any PENDING invoice with due_date <= today
    latest_invoice: Optional[Invoice] = None
