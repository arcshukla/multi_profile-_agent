"""
admin_routes.py
---------------
Admin portal page routes and HTMX partial handlers.

Extracted from app/main.py to keep main.py focused on app wiring.
All routes under /admin/* are already guarded by AdminAuthMiddleware
(which redirects unauthenticated users to /login). Individual handlers
that perform mutations additionally use require_admin via Depends.
"""

from pathlib import Path
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.config import settings, TEMPLATES_DIR
from app.core.logging_config import get_logger
from app.auth.dependencies import get_current_user, require_admin
from app.models.billing_models import BillingTier
from app.models.profile_models import CreateProfileRequest
from app.models.api_models import SuccessResponse
from app.services.billing_service      import billing_service
from app.services.email_template_service   import email_template_service
from app.services.index_service        import index_service
from app.services.log_service          import log_service
from app.services.llm_prompts_service  import llm_prompts_service
from app.services.preferences_service  import preferences_service
from app.services.profile_service      import profile_service
from app.services.prompt_service       import prompt_service
from app.services.token_service        import token_service
from app.services.user_service         import user_service
from app.services.pushover_template_service import get_all_templates, save_template, restore_default
from app.services.document_service     import document_service
from app.storage.file_storage          import ProfileFileStorage
from app.utils.template_utils          import render, htmx_ok, htmx_err

logger    = get_logger(__name__)
router    = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["support_email"] = settings.SUPPORT_EMAIL


def _r(request: Request, template: str, ctx: dict = None):
    return render(templates, request, template, ctx)


# ── Admin page routes ─────────────────────────────────────────────────────────

@router.get("/admin")
@router.get("/admin/registry", response_class=HTMLResponse)
def admin_registry(request: Request):
    return _r(request, "admin/layout.html", {
        "active_tab":           "registry",
        "tab_content_template": "admin/tab_registry.html",
        "current_user":         get_current_user(request),
    })


@router.get("/admin/manage", response_class=HTMLResponse)
def admin_manage_list(request: Request):
    return RedirectResponse(url="/admin/registry")


@router.get("/admin/manage/{slug}", response_class=HTMLResponse)
def admin_manage_profile(request: Request, slug: str):
    profile = profile_service.get_profile(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    fs = ProfileFileStorage(slug)
    prompts, is_default = prompt_service.get_prompts(slug)
    owner_user = user_service.get_user_by_slug(slug)
    prefs = preferences_service.get(slug)

    return _r(request, "admin/layout.html", {
        "active_tab":           "manage",
        "tab_content_template": "admin/tab_manage.html",
        "current_user":         get_current_user(request),
        "profile":              profile,
        "slides_data":          fs.read_slides(),
        "profile_css":          fs.read_css(),
        "prompts":              prompts,
        "prompts_is_default":   is_default,
        "billing_entry":        billing_service.get_entry(slug),
        "billing_status":       billing_service.get_status(slug),
        "owner_user":           owner_user,
        "prefs":                prefs,
        "owner_prefs_saved":    False,
        "owner_prefs_error":    None,
    })


@router.post("/admin/manage/{slug}/preferences", response_class=HTMLResponse)
async def admin_update_owner_preferences(
    request: Request,
    slug: str,
    owner_email: str = Form(...),
    name: str = Form(""),
    notify_unanswered_email: str = Form(None),
    notify_lead_email:       str = Form(None),
    current_user: dict = Depends(require_admin),
):
    owner_user = user_service.get_user_by_slug(slug)
    prefs = preferences_service.get(slug)
    error = None
    saved = False

    if not owner_user:
        error = "Owner user not found for this profile."
    else:
        old_email = owner_user.email
        new_email = owner_email.strip().lower()
        if new_email and new_email != old_email:
            ok, err = user_service.update_email(old_email, new_email)
            if not ok:
                error = err
        if not error:
            ok, err = user_service.update_name(new_email, name.strip())
            if not ok:
                error = err
        if not error:
            prefs = {
                "notify_unanswered_email": notify_unanswered_email == "on",
                "notify_lead_email":       notify_lead_email == "on",
            }
            preferences_service.save(slug, prefs)
            saved = True
            owner_user = user_service.get_user_by_slug(slug)

    return _r(request, "admin/partials/owner_prefs_form.html", {
        "profile":          profile_service.get_profile(slug),
        "owner_user":       owner_user,
        "prefs":            prefs,
        "owner_prefs_saved": saved,
        "owner_prefs_error": error,
    })


@router.get("/admin/system", response_class=HTMLResponse)
def admin_system(request: Request):
    return _r(request, "admin/layout.html", {
        "active_tab":           "system",
        "tab_content_template": "admin/tab_system.html",
        "current_user":         get_current_user(request),
    })


_VALID_ANALYTICS_DAYS = {1, 7, 15, 30, 90}

@router.get("/admin/analytics", response_class=HTMLResponse)
def admin_analytics(request: Request, days: int = Query(default=30)):
    from app.services import analytics_service
    if days not in _VALID_ANALYTICS_DAYS:
        days = 30
    return _r(request, "admin/layout.html", {
        "active_tab":           "analytics",
        "tab_content_template": "admin/tab_analytics.html",
        "current_user":         get_current_user(request),
        "days":                 days,
        "kpis":                 analytics_service.get_platform_kpis(days=days),
        "profile_ranking":      analytics_service.get_profile_activity_ranking(days=days),
        "content_gaps":         analytics_service.get_all_content_gaps(limit=15),
        "platform_daily":       analytics_service.get_platform_daily(days=days),
        "platform_tokens":      analytics_service.get_platform_token_burn(days=days),
        "notif_stats":          analytics_service.get_notification_stats(),
    })


# ── HTMX: profiles table ─────────────────────────────────────────────────────

@router.get("/admin/registry/profiles", response_class=HTMLResponse)
def htmx_profiles_table(
    request: Request,
    name: Optional[str] = None,
    slug: Optional[str] = None,
    status: Optional[str] = None,
):
    profiles = profile_service.list_profiles(
        status_filter=status or None,
        name_filter=name or None,
        slug_filter=slug or None,
    )
    return _r(request, "admin/partials/profiles_table.html", {"profiles": profiles})


@router.post("/admin/registry/create", response_class=HTMLResponse)
async def htmx_create_profile(
    request: Request,
    name: str = Form(...),
    owner_email: str = Form(...),
    status: str = Form("enabled"),
):
    import json as _json
    error_msg = None
    try:
        profile_service.create_profile(
            CreateProfileRequest(name=name, owner_email=owner_email, status=status)
        )
    except ValueError as e:
        error_msg = str(e)

    profiles = profile_service.list_profiles()
    response = _r(request, "admin/partials/profiles_table.html", {"profiles": profiles})
    if error_msg:
        response.headers["HX-Trigger"] = _json.dumps({"showToast": {"message": error_msg, "type": "error"}})
    else:
        response.headers["HX-Trigger"] = _json.dumps({"showToast": {"message": "Profile created!", "type": "success"}})
    return response


# ── HTMX: manage tab helpers ──────────────────────────────────────────────────

@router.post("/admin/manage/{slug}/slides")
async def save_slides_htmx(request: Request, slug: str):
    if not profile_service.profile_exists(slug):
        raise HTTPException(404, "Profile not found")
    form = await request.form()
    slides = []
    i = 0
    while f"type_{i}" in form:
        slide_type = form.get(f"type_{i}", "standard")
        if slide_type == "quote":
            slides.append({
                "type":        "quote",
                "quote":       form.get(f"quote_{i}", "").strip(),
                "attribution": form.get(f"attribution_{i}", "").strip(),
            })
        else:
            slides.append({
                "type":     "standard",
                "title":    form.get(f"title_{i}", "").strip(),
                "subtitle": form.get(f"subtitle_{i}", "").strip(),
                "body":     form.get(f"body_{i}", "").strip(),
            })
        i += 1
    slides = [s for s in slides if any(v for k, v in s.items() if k != "type")][:5]
    ProfileFileStorage(slug).write_slides({"slides": slides})
    return {"success": True}


@router.post("/admin/manage/{slug}/slides/restore", response_class=HTMLResponse)
async def htmx_restore_slides(slug: str):
    ProfileFileStorage(slug).reset_slides()
    return htmx_ok("Slides restored to defaults.")


@router.get("/admin/manage/{slug}/docs", response_class=HTMLResponse)
def htmx_docs_list(request: Request, slug: str):
    result = document_service.list_documents(slug)
    return _r(request, "admin/partials/docs_list.html", {
        "slug":      slug,
        "documents": result.documents,
    })


@router.get("/admin/manage/{slug}/index-status", response_class=HTMLResponse)
def htmx_index_status(request: Request, slug: str):
    status = index_service.get_status(slug)
    return _r(request, "admin/partials/index_status.html", {
        "slug":   slug,
        "status": status,
    })


_CHUNKS_PAGE_SIZE = 20

@router.get("/admin/manage/{slug}/chunks", response_class=HTMLResponse)
def htmx_chunks(request: Request, slug: str, page: int = Query(1, ge=1)):
    # Guard: engine or collection may be mid-wipe during indexing
    if index_service.is_indexing(slug):
        return _r(request, "admin/partials/chunks.html", {
            "chunks": [], "slug": slug, "page": 1, "total_pages": 1,
            "total": 0, "page_size": _CHUNKS_PAGE_SIZE,
            "indexing": True,
        })

    engine    = index_service.get_engine(slug)
    total     = engine.chunk_count() if engine else 0
    page_size = _CHUNKS_PAGE_SIZE
    total_pages = max(1, (total + page_size - 1) // page_size)
    page        = min(page, total_pages)
    offset      = (page - 1) * page_size

    chunks = []
    if engine and total > 0:
        try:
            result = engine.collection.get(
                include=["documents", "metadatas"],
                limit=page_size,
                offset=offset,
            )
            for i, (doc, meta) in enumerate(
                zip(result.get("documents", []), result.get("metadatas", [])),
                start=offset + 1,
            ):
                chunks.append({
                    "i":         i,
                    "topic":     (meta or {}).get("topic", "—"),
                    "source":    (meta or {}).get("source", "—"),
                    "preview":   doc[:200],
                    "truncated": len(doc) > 200,
                })
        except Exception as e:
            logger.warning("htmx_chunks: collection read failed for '%s' (indexing may be in progress): %s", slug, e)
            total = 0

    return _r(request, "admin/partials/chunks.html", {
        "chunks":       chunks,
        "slug":         slug,
        "page":         page,
        "total_pages":  total_pages,
        "total":        total,
        "page_size":    page_size,
        "indexing":     False,
    })


# ── System sub-tab partials ───────────────────────────────────────────────────

@router.get("/admin/system/billing", response_class=HTMLResponse)
def htmx_system_billing(
    request: Request,
    name:           str = Query(""),
    slug:           str = Query(""),
    plan:           str = Query(""),
    payment_status: str = Query(""),
    don_name:       str = Query(""),
    don_slug:       str = Query(""),
    don_status:     str = Query(""),
):
    billing_path = Path("system/billing.json")
    billing_data = json.loads(billing_path.read_text(encoding="utf-8")) if billing_path.exists() else {}

    plans_set = {b.get("tier", "") for b in billing_data.values()}
    plans     = sorted(p for p in plans_set if p)

    slug_to_name = {o.slug: o.name for o in user_service.list_owners()}

    # Compute invoice totals across ALL profiles (unfiltered — stats always reflect full dataset)
    total_received = pending_users = pending_amount = 0
    for user_data in billing_data.values():
        for inv in user_data.get("invoices", []):
            if inv.get("status", "").lower() == "paid":
                total_received += inv.get("amount", 0)
            else:
                pending_users  += 1
                pending_amount += inv.get("amount", 0)

    # Build filtered invoice rows for the table
    billing_rows = []
    for user_slug, user_data in billing_data.items():
        user_name = slug_to_name.get(user_slug, user_slug)
        user_plan = user_data.get("tier", "")
        for inv in user_data.get("invoices", []):
            if name   and name.lower()           not in user_name.lower():   continue
            if slug   and slug.lower()           not in user_slug.lower():   continue
            if plan   and plan                   != user_plan:               continue
            if payment_status and payment_status.lower() != inv.get("status", "").lower(): continue
            billing_rows.append({
                "id":             inv.get("id", ""),
                "name":           user_name,
                "slug":           user_slug,
                "plan":           user_plan,
                "due_date":       inv.get("due_date", ""),
                "amount":         inv.get("amount", 0),
                "payment_status": inv.get("status", "").capitalize(),
            })

    # Aggregate donation totals across ALL profiles (unfiltered — stats always reflect full dataset)
    total_confirmed_donations = 0.0
    pending_donation_count    = 0
    for user_data in billing_data.values():
        for don in user_data.get("donations", []):
            s = don.get("status", "").lower()
            if s == "confirmed":
                total_confirmed_donations += don.get("amount", 0)
            elif s == "pending":
                pending_donation_count += 1

    # Build filtered donation rows for the table
    donation_rows = []
    for user_slug, user_data in billing_data.items():
        user_name = slug_to_name.get(user_slug, user_slug)
        for don in reversed(user_data.get("donations", [])):  # newest first
            don_status_val = don.get("status", "").lower()
            if don_name   and don_name.lower()   not in user_name.lower():   continue
            if don_slug   and don_slug.lower()   not in user_slug.lower():   continue
            if don_status and don_status.lower() != don_status_val:          continue
            donation_rows.append({
                "id":           don.get("id", ""),
                "name":         user_name,
                "slug":         user_slug,
                "amount":       don.get("amount", 0),
                "note":         don.get("note", ""),
                "status":       don_status_val.capitalize(),
                "confirmed_at": don.get("confirmed_at", ""),
                "confirmed_by": don.get("confirmed_by", ""),
            })

    return _r(request, "admin/partials/system_billing.html", {
        "billing_rows":              billing_rows,
        "total_received":            total_received,
        "pending_users":             pending_users,
        "pending_amount":            pending_amount,
        "plans":                     plans,
        "inv_filters":               {"name": name, "slug": slug, "plan": plan, "payment_status": payment_status},
        "donation_rows":             donation_rows,
        "don_filters":               {"name": don_name, "slug": don_slug, "status": don_status},
        "total_confirmed_donations": total_confirmed_donations,
        "pending_donation_count":    pending_donation_count,
    })


@router.post("/admin/system/billing/update_status", response_class=HTMLResponse)
async def system_billing_update_invoice_status(
    request:        Request,
    slug:           str = Form(...),
    due_date:       str = Form(...),
    payment_status: str = Form(...),
):
    """Update invoice payment status from the system billing table dropdown."""
    try:
        get_current_user(request)
        billing_service.set_invoice_status(slug, due_date, payment_status)
    except ValueError as exc:
        return htmx_err(str(exc))
    # Paid → lock permanently; return a badge, not a dropdown
    if payment_status.lower() == "paid":
        return HTMLResponse(
            '<span class="inline-flex items-center gap-1 bg-green-100 text-green-700'
            ' font-semibold px-2 py-0.5 rounded-full">'
            '<span class="w-1 h-1 rounded-full bg-green-500"></span>Paid</span>'
        )
    slug_esc = slug.replace('"', "&quot;")
    due_esc  = due_date.replace('"', "&quot;")
    opts = "".join(
        f'<option value="{v}"{"  selected" if v == payment_status else ""}>{v}</option>'
        for v in ("Pending", "Overdue", "Paid")
    )
    return HTMLResponse(
        f'<select name="payment_status"'
        f' class="border border-gray-200 rounded px-2 py-1 text-xs bg-white"'
        f' hx-post="/admin/system/billing/update_status"'
        f' hx-vals=\'{{"slug":"{slug_esc}","due_date":"{due_esc}"}}\''
        f' hx-trigger="change"'
        f' hx-target="this" hx-swap="outerHTML">{opts}</select>'
    )


@router.post("/admin/system/billing/confirm_donation", response_class=HTMLResponse)
async def system_billing_confirm_donation(
    request:     Request,
    slug:        str = Form(...),
    donation_id: str = Form(...),
):
    """Confirm a pending donation from the system billing table."""
    import threading  # noqa: PLC0415
    from app.services.notification_service import notification_service  # noqa: PLC0415

    try:
        admin_email = get_current_user(request)["email"]
        record      = billing_service.confirm_donation(slug, donation_id, admin_email)
    except ValueError as exc:
        return htmx_err(str(exc))

    threading.Thread(
        target=notification_service.notify_donation_confirmed,
        args=(slug, record.id, record.amount, record.confirmed_at),
        daemon=True,
    ).start()

    confirmed_date = record.confirmed_at[:10] if record.confirmed_at else "—"
    # OOB swap updates the Confirmed On cell; main swap replaces the Status cell
    oob = (
        f'<td id="don-confirmedat-{donation_id}" hx-swap-oob="true"'
        f' class="px-4 py-2.5 text-gray-500">{confirmed_date}</td>'
    )
    status_td = (
        f'<td id="don-status-{donation_id}" class="px-4 py-2.5">'
        f'<span class="inline-flex items-center gap-1 bg-green-100 text-green-700 font-semibold px-2 py-0.5 rounded-full">'
        f'<span class="w-1 h-1 rounded-full bg-green-500"></span>Confirmed'
        f'</span></td>'
    )
    response = HTMLResponse(status_td + oob)
    response.headers["HX-Trigger"] = '{"showToast":"Donation confirmed. Thank-you email sent to owner."}'
    return response


@router.get("/admin/system/history", response_class=HTMLResponse)
def htmx_system_history(request: Request, slug: Optional[str] = None):
    history = index_service.get_history(slug=slug or None, limit=100)
    active  = index_service.active_slugs()
    return _r(request, "admin/partials/system_history.html", {
        "history": history,
        "slug":    slug,
        "active":  active,   # slugs currently indexing or queued
    })


@router.get("/admin/system/deleted", response_class=HTMLResponse)
def htmx_system_deleted(request: Request):
    profiles = profile_service.list_profiles(status_filter="soft_deleted")
    return _r(request, "admin/partials/system_deleted.html", {"profiles": profiles})


@router.get("/admin/system/logs", response_class=HTMLResponse)
def htmx_system_logs(
    request:  Request,
    log_type: str           = Query("app"),
    slug:     Optional[str] = Query(None),
    tail:     int           = Query(200),
    search:   Optional[str] = Query(None),
):
    result       = log_service.read_log(log_type=log_type, slug=slug, tail=tail, search=search)
    profile_slugs = log_service.list_profile_logs()
    return _r(request, "admin/partials/system_logs.html", {
        "log_type":      log_type,
        "slug":          slug,
        "tail":          tail,
        "search":        search,
        "lines":         result["lines"],
        "total_lines":   result["total_lines"],
        "profile_slugs": profile_slugs,
    })


@router.get("/admin/system/llm", response_class=HTMLResponse)
def htmx_system_llm(request: Request):
    prompts  = llm_prompts_service.get_prompts()
    usage    = token_service.get_all()
    totals   = token_service.get_totals()
    profiles = [p for p in profile_service.list_profiles() if p.status != "deleted"]
    return _r(request, "admin/partials/system_llm.html", {
        "prompts":  prompts,
        "usage":    usage,
        "totals":   totals,
        "profiles": profiles,
    })


@router.post("/admin/system/llm/prompt/{key}", response_class=HTMLResponse)
async def htmx_save_llm_prompt(request: Request, key: str, content: str = Form(...)):
    ok = llm_prompts_service.update_prompt(key, content)
    if not ok:
        return htmx_err("Unknown prompt key.")
    return htmx_ok("Saved successfully.")


@router.post("/admin/system/llm/prompt/restore", response_class=HTMLResponse)
async def htmx_restore_llm_prompts(request: Request):
    llm_prompts_service.restore_defaults()
    prompts  = llm_prompts_service.get_prompts()
    usage    = token_service.get_all()
    totals   = token_service.get_totals()
    profiles = [p for p in profile_service.list_profiles() if p.status != "deleted"]
    return _r(request, "admin/partials/system_llm.html", {
        "prompts":  prompts,
        "usage":    usage,
        "totals":   totals,
        "profiles": profiles,
    })


@router.post("/admin/system/llm/tokens/reset/{slug}")
async def htmx_reset_token_usage(slug: str):
    token_service.reset_profile(slug)
    return htmx_ok("Reset.")


# ── Email Templates ───────────────────────────────────────────────────────────

@router.get("/admin/system/email", response_class=HTMLResponse)
def htmx_system_email(request: Request):
    return _r(request, "admin/partials/system_email_templates.html", {
        "templates": email_template_service.get_templates(),
    })


@router.post("/admin/system/email/save/{name}", response_class=HTMLResponse)
async def htmx_save_email_template(
    name:      str,
    subject:   str = Form(...),
    body_text: str = Form(...),
    body_html: str = Form(...),
):
    ok = email_template_service.update_template(name, subject, body_text, body_html)
    if not ok:
        return HTMLResponse('<span class="text-red-600">Unknown template name.</span>', status_code=400)
    return HTMLResponse('<span class="text-green-600 font-medium">Saved.</span>')


@router.post("/admin/system/email/preview", response_class=HTMLResponse)
async def htmx_preview_email_template(body_html: str = Form(...)):
    """Return the fragment wrapped in the shared email layout for iframe preview."""
    full_html = email_template_service.wrap_layout(body_html)
    return HTMLResponse(full_html)


@router.post("/admin/system/email/restore/{name}", response_class=HTMLResponse)
def htmx_restore_email_template(request: Request, name: str):
    email_template_service.restore_defaults(name)
    tmpls = email_template_service.get_templates()
    if name not in tmpls:
        return HTMLResponse("", status_code=404)
    return _r(request, "admin/partials/system_email_templates.html", {"templates": tmpls})


@router.post("/admin/system/email/restore_all", response_class=HTMLResponse)
def htmx_restore_all_email_templates(request: Request):
    email_template_service.restore_defaults()
    return _r(request, "admin/partials/system_email_templates.html", {
        "templates": email_template_service.get_templates(),
    })


# ── Pushover Templates ────────────────────────────────────────────────────────

@router.get("/admin/system/pushover", response_class=HTMLResponse)
def htmx_system_pushover(request: Request):
    return _r(request, "admin/partials/system_pushover_templates.html", {
        "pushover_templates": get_all_templates(),
    })


@router.post("/admin/system/pushover/save/{name}", response_class=HTMLResponse)
async def htmx_save_pushover_template(name: str, body_text: str = Form(...)):
    tmpls = get_all_templates()
    if name not in tmpls:
        return HTMLResponse('<span class="text-red-600">Unknown template name.</span>', status_code=400)
    data = tmpls[name]
    data["body_text"] = body_text
    save_template(name, data)
    return HTMLResponse('<span class="text-green-600 font-medium">Saved.</span>')


@router.post("/admin/system/pushover/restore/{name}", response_class=HTMLResponse)
def htmx_restore_pushover_template(request: Request, name: str):
    restore_default(name)
    tmpls = get_all_templates()
    if name not in tmpls:
        return HTMLResponse("", status_code=404)
    return _r(request, "admin/partials/system_pushover_templates.html", {
        "pushover_templates": tmpls,
    })


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/admin/system/users", response_class=HTMLResponse)
def htmx_system_users(request: Request):
    users    = user_service.list_users()
    profiles = [p for p in profile_service.list_profiles() if p.status != "deleted"]
    return _r(request, "admin/partials/system_users.html", {
        "users": users, "profiles": profiles,
    })


@router.post("/admin/system/users/add", response_class=HTMLResponse)
async def htmx_add_user(
    request: Request,
    email: str = Form(...),
    name:  str = Form(...),
    role:  str = Form(...),
    slug:  str = Form(""),
):
    ok, error = user_service.add_user(email, name, role, slug or None)
    users    = user_service.list_users()
    profiles = [p for p in profile_service.list_profiles() if p.status != "deleted"]
    return _r(request, "admin/partials/system_users.html", {
        "users": users, "profiles": profiles,
        "add_error": error if not ok else None,
    })


@router.post("/admin/system/users/remove/{email:path}", response_class=HTMLResponse)
async def htmx_remove_user(email: str):
    user_service.remove_user(email)
    return HTMLResponse("")


@router.get("/admin/system/users/edit/{email:path}", response_class=HTMLResponse)
def htmx_user_edit_row(request: Request, email: str):
    user = user_service.get_user(email)
    if not user:
        return HTMLResponse("", status_code=404)
    return _r(request, "admin/partials/user_edit_row.html", {"email": email})


@router.post("/admin/system/users/update", response_class=HTMLResponse)
async def htmx_update_user(
    request: Request,
    old_email: str = Form(...),
    new_email: str = Form(...),
):
    ok, error = user_service.update_email(old_email, new_email)
    users    = user_service.list_users()
    profiles = [p for p in profile_service.list_profiles() if p.status != "deleted"]
    return _r(request, "admin/partials/system_users.html", {
        "users":        users,
        "profiles":     profiles,
        "update_error": error if not ok else None,
    })


@router.get("/admin/system/config", response_class=HTMLResponse)
def htmx_system_config(request: Request):
    return _r(request, "admin/partials/system_config.html", {
        "rows":     settings.get_config_display(),
        "is_local": settings.IS_LOCAL,
    })


@router.get("/admin/system/templates", response_class=HTMLResponse)
def htmx_system_templates(request: Request):
    return _r(request, "admin/partials/system_templates.html", {
        "templates":          email_template_service.get_templates(),
        "prompts":            llm_prompts_service.get_prompts(),
        "pushover_templates": get_all_templates(),
    })


# ── Admin billing ─────────────────────────────────────────────────────────────

def _billing_partial(request: Request, slug: str):
    return _r(request, "admin/partials/billing_manage.html", {
        "slug":           slug,
        "billing_entry":  billing_service.get_entry(slug),
        "billing_status": billing_service.get_status(slug),
        "platform_fee":   settings.PLATFORM_FEE_INR,
        "donations":      billing_service.get_donations(slug),
        "donation_min":   settings.DONATION_MIN_INR,
        "donation_max":   settings.DONATION_MAX_INR,
    })


@router.get("/admin/billing/{slug}", response_class=HTMLResponse)
def admin_billing_panel(request: Request, slug: str):
    return _billing_partial(request, slug)


@router.post("/admin/billing/{slug}/tier", response_class=HTMLResponse)
async def admin_set_tier(request: Request, slug: str, tier: str = Form(...)):
    try:
        new_tier    = BillingTier(tier)
        admin_email = get_current_user(request)["email"]
        billing_service.set_tier(slug, new_tier, admin_email)
    except (ValueError, KeyError) as exc:
        return htmx_err(f"Error: {exc}")
    resp = _billing_partial(request, slug)
    resp.headers["HX-Trigger"] = '{"showToast":"Billing tier updated."}'
    return resp


@router.post("/admin/billing/{slug}/invoice", response_class=HTMLResponse)
async def admin_create_invoice(request: Request, slug: str):
    try:
        billing_service.create_invoice(slug)
    except ValueError as exc:
        return htmx_err(f"Error: {exc}")
    resp = _billing_partial(request, slug)
    resp.headers["HX-Trigger"] = '{"showToast":"Invoice created."}'
    return resp


@router.post("/admin/billing/{slug}/invoice/{invoice_id}/confirm", response_class=HTMLResponse)
async def admin_confirm_payment(request: Request, slug: str, invoice_id: str):
    import threading  # noqa: PLC0415
    from app.services.notification_service import notification_service  # noqa: PLC0415

    try:
        admin_email = get_current_user(request)["email"]
        inv = billing_service.confirm_payment(slug, invoice_id, admin_email)
    except ValueError as exc:
        return htmx_err(f"Error: {exc}")

    threading.Thread(
        target  = notification_service.notify_payment_confirmed,
        args    = (slug, inv.id, inv.amount, inv.period_start, inv.period_end, inv.paid_at),
        daemon  = True,
    ).start()

    resp = _billing_partial(request, slug)
    resp.headers["HX-Trigger"] = '{"showToast":"Payment confirmed. Receipt email sent to owner."}'
    return resp


@router.post("/admin/billing/{slug}/donation/{donation_id}/confirm", response_class=HTMLResponse)
async def admin_confirm_donation(request: Request, slug: str, donation_id: str):
    """
    Admin confirms receipt of a voluntary donation from a free-tier owner.
    Triggers a thank-you email to the owner in a background thread.
    """
    import threading  # noqa: PLC0415
    from app.services.notification_service import notification_service  # noqa: PLC0415

    try:
        admin_email = get_current_user(request)["email"]
        record      = billing_service.confirm_donation(slug, donation_id, admin_email)
    except ValueError as exc:
        return htmx_err(f"Error: {exc}")

    threading.Thread(
        target  = notification_service.notify_donation_confirmed,
        args    = (slug, record.id, record.amount, record.confirmed_at),
        daemon  = True,
    ).start()

    resp = _billing_partial(request, slug)
    resp.headers["HX-Trigger"] = '{"showToast":"Donation confirmed. Thank-you email sent to owner."}'
    return resp


# ── Tab switching (HTMX fragments) ────────────────────────────────────────────

@router.get("/admin/tab/registry", response_class=HTMLResponse)
def htmx_tab_registry(request: Request):
    return _r(request, "admin/tab_registry.html", {})


@router.get("/admin/manage-tab", response_class=HTMLResponse)
def htmx_tab_manage(request: Request):
    return _r(request, "admin/partials/manage_placeholder.html", {})


@router.get("/admin/tab/system", response_class=HTMLResponse)
def htmx_tab_system(request: Request):
    return _r(request, "admin/tab_system.html", {})


# ── Chat page ─────────────────────────────────────────────────────────────────

@router.get("/chat/{slug}", response_class=HTMLResponse)
def chat_page(request: Request, slug: str):
    from app.services.chat_service import chat_service
    from app.services.prompt_service import prompt_service as ps
    from app.core.config import settings as _cfg

    profile = profile_service.get_profile(slug)
    if not profile:
        resp = _r(request, "partials/profile_not_found.html", {
            "register_url": f"{_cfg.APP_URL.rstrip('/')}/register",
        })
        resp.status_code = 404
        return resp

    if profile.status != "enabled":
        resp = _r(request, "partials/profile_disabled.html", {"status": profile.status})
        resp.status_code = 410 if profile.status in ("soft_deleted", "deleted") else 403
        return resp

    fs = ProfileFileStorage(slug)
    return _r(request, "chat/chat.html", {
        "profile":         profile,
        "slides_data":     fs.read_slides(),
        "profile_css":     fs.read_css(),
        "welcome_message": chat_service.get_welcome_message(slug),
        "followups":       chat_service.get_initial_followups(slug),
        "placeholder":     ps.chat_placeholder(slug),
    })
