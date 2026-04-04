"""
billing.py
----------
Owner-facing billing routes.

GET  /owner/billing                          — billing page (tier badge, QR, history)
GET  /owner/billing/qr/{inv_id}             — serve invoice QR PNG (validates ownership)
POST /owner/billing/donation                 — create voluntary donation QR (free tier only)
GET  /owner/billing/donation/qr/{don_id}    — serve donation QR PNG
"""

import inspect
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import require_owner
from app.core.config import STATIC_DIR, TEMPLATES_DIR, settings
from app.models.billing_models import BillingTier
from app.services.billing_service import billing_service

router    = APIRouter(prefix="/owner", include_in_schema=False)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["support_email"] = settings.SUPPORT_EMAIL


def _r(request: Request, template: str, ctx: dict = {}):
    sig   = inspect.signature(templates.TemplateResponse)
    first = list(sig.parameters.keys())[0]
    if first == "request":
        return templates.TemplateResponse(request, template, ctx)
    return templates.TemplateResponse(template, {"request": request, **ctx})


# ── Billing page ──────────────────────────────────────────────────────────

@router.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request, user: dict = Depends(require_owner)):
    slug           = user.get("slug") or ""
    billing_entry  = billing_service.get_entry(slug)
    billing_status = billing_service.get_status(slug)

    ctx = {
        "user":           user,
        "billing_entry":  billing_entry,
        "billing_status": billing_status,
        "upi_vpa":        settings.UPI_VPA,
    }

    # Donation context — only relevant for free-tier owners
    if billing_entry.tier == BillingTier.FREE:
        ctx["donations"]        = billing_service.get_donations(slug)
        ctx["donation_min"]     = settings.DONATION_MIN_INR
        ctx["donation_max"]     = settings.DONATION_MAX_INR
        ctx["donation_upi_vpa"] = settings.DONATION_UPI_VPA or settings.UPI_VPA

    return _r(request, "owner/billing.html", ctx)


# ── Serve QR image ────────────────────────────────────────────────────────

@router.post("/billing/qr/regenerate/{invoice_id}")
def regenerate_qr_endpoint(request: Request, invoice_id: str, user: dict = Depends(require_owner)):
    """
    Owner-triggered QR regeneration.
    Useful when the QR was missing at invoice creation (library not installed)
    or was wiped by an HF Spaces restart.
    """
    safe_id = Path(invoice_id).name
    if not safe_id or safe_id != invoice_id:
        return HTMLResponse("Invalid invoice ID", status_code=400)

    slug  = user.get("slug") or ""
    entry = billing_service.get_entry(slug)
    inv   = next((i for i in entry.invoices if i.id == safe_id), None)
    if inv is None:
        return HTMLResponse("Invoice not found", status_code=404)

    billing_service.regenerate_qr(slug, safe_id)
    return RedirectResponse(url="/owner/billing?qr_regenerated=1", status_code=303)


@router.get("/billing/qr/{invoice_id}")
def serve_qr(request: Request, invoice_id: str, user: dict = Depends(require_owner)):
    """
    Serve the QR PNG for an invoice.
    Validates that the invoice belongs to the requesting owner's profile.
    Regenerates the QR from the stored UPI URI if the file is missing
    (e.g. after an HF Spaces restart that wiped static/).
    """
    # Sanitise: only allow bare IDs (no path separators)
    safe_id = Path(invoice_id).name
    if not safe_id or safe_id != invoice_id:
        return HTMLResponse("Invalid invoice ID", status_code=400)

    slug  = user.get("slug") or ""
    entry = billing_service.get_entry(slug)

    # Confirm invoice belongs to this owner
    inv = next((i for i in entry.invoices if i.id == safe_id), None)
    if inv is None:
        return HTMLResponse("Invoice not found", status_code=404)

    # QR expires the day after the due date (force regenerate for overdue invoices)
    try:
        if date.today() > date.fromisoformat(inv.due_date):
            return HTMLResponse("QR expired — due date has passed", status_code=410)
    except ValueError:
        pass  # malformed due_date: serve anyway

    qr_file = STATIC_DIR / inv.qr_path
    if not qr_file.exists():
        # Lazy regeneration (HF Spaces restart / first-time QR failure)
        billing_service.regenerate_qr(slug, safe_id)

    if not qr_file.exists():
        return HTMLResponse("QR image not available", status_code=404)

    return FileResponse(str(qr_file), media_type="image/png")


# ── Donation routes (free-tier only) ─────────────────────────────────────────

@router.post("/billing/donation", response_class=HTMLResponse)
async def create_donation(
    request: Request,
    amount:  float = Form(...),
    note:    str   = Form(""),
    user:    dict  = Depends(require_owner),
):
    """
    Owner generates a voluntary donation QR to contribute to the platform.
    Validates free tier + amount bounds. Returns an HTMX partial.
    """
    slug  = user.get("slug") or ""
    entry = billing_service.get_entry(slug)

    if entry.tier != BillingTier.FREE:
        return _r(request, "owner/partials/donation_result.html", {
            "error": "Donations are only available on the free tier.",
        })

    try:
        record = billing_service.create_donation(slug, amount, note)
    except ValueError as exc:
        return _r(request, "owner/partials/donation_result.html", {"error": str(exc)})

    return _r(request, "owner/partials/donation_result.html", {
        "record":           record,
        "donation_upi_vpa": settings.DONATION_UPI_VPA or settings.UPI_VPA,
    })


@router.get("/billing/donation/qr/{donation_id}")
def serve_donation_qr(
    request:     Request,
    donation_id: str,
    user:        dict = Depends(require_owner),
):
    """Serve the QR PNG for a donation record (validates ownership)."""
    safe_id = Path(donation_id).name
    if not safe_id or safe_id != donation_id:
        return HTMLResponse("Invalid donation ID", status_code=400)

    slug  = user.get("slug") or ""
    entry = billing_service.get_entry(slug)

    rec = next((d for d in entry.donations if d.id == safe_id), None)
    if rec is None:
        return HTMLResponse("Donation record not found", status_code=404)

    # Donation QR expires 10 minutes after creation
    try:
        created = datetime.fromisoformat(rec.created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(minutes=10):
            return HTMLResponse("QR expired", status_code=410)
    except (ValueError, TypeError):
        pass  # malformed created_at: serve anyway

    if not rec.qr_path:
        return HTMLResponse("No QR for this donation", status_code=404)

    qr_file = STATIC_DIR / rec.qr_path
    if not qr_file.exists():
        return HTMLResponse("QR image not available", status_code=404)

    return FileResponse(str(qr_file), media_type="image/png")
