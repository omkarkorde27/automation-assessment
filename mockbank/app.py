"""mockbank -- the target surface.

A stand-in for a legacy core-banking servicing console. This is a FIXTURE, not
part of the system under test: it exists so the agent has a real, hostile UI to
drive and so replay's error taxonomy can be exercised on demand.

Design notes that matter:
  * Content lives in a named child frame, so every locator needs a frame path.
  * Element ids churn per render (see render.IdChurn).
  * Accessible names are inconsistent by design -- some controls have none.
  * Faults are armed out-of-band and fire at specific points in the flow.

Nothing here is clever. It is deliberately the kind of app that punishes the
locator strategies people reach for first.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from . import data, faults
from .render import IdChurn
from .tenants import DEFAULT_TENANT, TENANTS, Tenant, get_tenant

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="mockbank", docs_url=None, redoc_url=None)

SESSION_COOKIE = "mb_session"
MIN_DEPOSIT_CENTS = 2500


# --------------------------------------------------------------------------
# request plumbing
# --------------------------------------------------------------------------

@app.middleware("http")
async def arm_injected_fault(request: Request, call_next):
    """`?inject=<fault>` arms a one-shot fault for the tenant in the path.

    Convenience for demos and tests; the durable path is POST /__control.
    """
    inject = request.query_params.get("inject")
    if inject:
        parts = request.url.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "t" and inject in faults.FAULT_POINTS:
            faults.arm(parts[1], inject, 1)
    return await call_next(request)


def _ctx(request: Request, tenant: Tenant, *, allow_interstitial: bool = True, **extra) -> dict:
    """Base template context. `cid` mints churned ids; `show_interstitial`
    is resolved once per render so the modal fires exactly one time.

    `allow_interstitial=False` for the shell and the nav frame: those render on
    every page load, and letting them consume the armed firing means the modal
    never reaches a content screen -- the one place it actually obstructs
    anything.
    """
    ctx = {
        "request": request,
        "tenant": tenant,
        "cid": IdChurn(),
        "show_interstitial": allow_interstitial and faults.consume(tenant.slug, "interstitial"),
        "page_title": extra.pop("page_title", "Meridian Core"),
    }
    ctx.update(extra)
    return ctx


def _render(request: Request, tenant: Tenant, template: str, status: int = 200,
            allow_interstitial: bool = True, **extra) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request=request, name=template,
        context=_ctx(request, tenant, allow_interstitial=allow_interstitial, **extra),
        status_code=status,
    )


def _tenant_or_404(slug: str) -> Tenant:
    t = get_tenant(slug)
    if t is None:
        raise _NotATenant(slug)
    return t


class _NotATenant(Exception):
    def __init__(self, slug: str) -> None:
        self.slug = slug


@app.exception_handler(_NotATenant)
async def _unknown_tenant(request: Request, exc: _NotATenant) -> Response:
    return JSONResponse(
        {"error": "unknown tenant", "tenant": exc.slug, "known": sorted(TENANTS)}, status_code=404
    )


def _session_user(request: Request, tenant: Tenant) -> str | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw or ":" not in raw:
        return None
    slug, _, user = raw.partition(":")
    return user if slug == tenant.slug else None


def _guard(request: Request, tenant: Tenant, path: str) -> Response | None:
    """Pre-render checks for every content route.

    Returns a Response to short-circuit with, or None to proceed. Order is
    deliberate: an expired session must win over everything, because that is
    what the real app does -- it bounces you before it renders anything.
    """
    if faults.consume(tenant.slug, "session_timeout"):
        resp = RedirectResponse(url=f"/t/{tenant.slug}/login?expired=1&next={quote(path)}", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    if _session_user(request, tenant) is None:
        return RedirectResponse(url=f"/t/{tenant.slug}/login?next={quote(path)}", status_code=303)

    if faults.consume(tenant.slug, "error_500"):
        return _render(
            request, tenant, "error.html", status=500,
            page_title="Error", heading="Unexpected Error",
            message="An unexpected error occurred while processing your request. (HTTP 500)",
            detail="javax.servlet.ServletException: core.svc.MemberFacade.lookup\n\tat core.svc.MemberFacade.lookup(MemberFacade.java:214)",
        )

    if faults.consume(tenant.slug, "slow_load"):
        return _render(
            request, tenant, "loading.html", page_title="Loading", target=path, delay=3
        )

    return None


# --------------------------------------------------------------------------
# shell + auth
# --------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url=f"/t/{DEFAULT_TENANT}/", status_code=307)


@app.get("/t/{slug}/", response_class=HTMLResponse)
@app.get("/t/{slug}", response_class=HTMLResponse)
async def shell(request: Request, slug: str) -> Response:
    tenant = _tenant_or_404(slug)
    return _render(
        request, tenant, "frameset.html", allow_interstitial=False,
        content_src=f"/t/{tenant.slug}/home",
    )


@app.get("/t/{slug}/nav", response_class=HTMLResponse)
async def nav(request: Request, slug: str) -> Response:
    tenant = _tenant_or_404(slug)
    return _render(request, tenant, "nav.html", allow_interstitial=False)


@app.get("/t/{slug}/login", response_class=HTMLResponse)
async def login_form(
    request: Request, slug: str, expired: int = 0, next: str = "", error: str = ""
) -> Response:
    tenant = _tenant_or_404(slug)
    churn = IdChurn()
    return _render(
        request, tenant, "login.html", page_title="Sign In", crumb="Sign In",
        expired=bool(expired), next_path=next or f"/t/{tenant.slug}/home",
        error=error, u_id=churn("txt"), p_id=churn("txt"),
    )


@app.post("/t/{slug}/login")
async def login_submit(
    request: Request,
    slug: str,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
) -> Response:
    tenant = _tenant_or_404(slug)
    # Fixture auth: any non-empty pair is accepted. There are no real
    # credentials anywhere in this project.
    if not username.strip() or not password.strip():
        return RedirectResponse(
            url=f"/t/{tenant.slug}/login?error={quote('User ID and password are required.')}",
            status_code=303,
        )
    target = next or f"/t/{tenant.slug}/home"
    resp = RedirectResponse(url=target, status_code=303)
    resp.set_cookie(SESSION_COOKIE, f"{tenant.slug}:{username.strip()}", httponly=False)
    return resp


@app.get("/t/{slug}/logout")
async def logout(request: Request, slug: str) -> Response:
    tenant = _tenant_or_404(slug)
    resp = RedirectResponse(url=f"/t/{tenant.slug}/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/t/{slug}/home", response_class=HTMLResponse)
async def home(request: Request, slug: str) -> Response:
    tenant = _tenant_or_404(slug)
    path = f"/t/{tenant.slug}/home"
    if (short := _guard(request, tenant, path)) is not None:
        return short
    return _render(
        request, tenant, "home.html", page_title="Dashboard", crumb="Dashboard",
        user=_session_user(request, tenant),
    )


# --------------------------------------------------------------------------
# member search -> detail
# --------------------------------------------------------------------------

@app.get("/t/{slug}/members", response_class=HTMLResponse)
async def members_search(request: Request, slug: str) -> Response:
    tenant = _tenant_or_404(slug)
    path = f"/t/{tenant.slug}/members"
    if (short := _guard(request, tenant, path)) is not None:
        return short
    churn = IdChurn()
    return _render(
        request, tenant, "members_search.html",
        page_title=tenant.member_search_heading, crumb=tenant.member_search_heading,
        q_id=churn("txt"), ln_id=churn("txt"),
    )


@app.post("/t/{slug}/members/search", response_class=HTMLResponse)
async def members_search_submit(request: Request, slug: str) -> Response:
    tenant = _tenant_or_404(slug)
    path = f"/t/{tenant.slug}/members"
    if (short := _guard(request, tenant, path)) is not None:
        return short

    # The member-id field's name is churned, so read whichever text field came
    # back rather than depending on a stable form key.
    form = await request.form()
    last_name = (form.get("last_name") or "").strip()
    member_q = next(
        (str(v).strip() for k, v in form.items() if k != "last_name" and str(v).strip()), ""
    )
    query = member_q or last_name

    results = [] if faults.consume(tenant.slug, "not_found") else data.find_members(query)
    return _render(
        request, tenant, "members_results.html",
        page_title="Search Results", crumb="Search Results", results=results, query=query,
    )


@app.get("/t/{slug}/members/{member_id}", response_class=HTMLResponse)
async def member_detail(request: Request, slug: str, member_id: str) -> Response:
    tenant = _tenant_or_404(slug)
    path = f"/t/{tenant.slug}/members/{member_id}"
    if (short := _guard(request, tenant, path)) is not None:
        return short

    member = data.MEMBERS.get(member_id)
    if member is None:
        return _render(
            request, tenant, "members_results.html",
            page_title="Search Results", crumb="Search Results", results=[], query=member_id,
        )

    if member.restricted or faults.consume(tenant.slug, "permission_denied"):
        return _render(
            request, tenant, "error.html", status=403,
            page_title="Not Authorized", heading="Not Authorized",
            message="You are not authorized to view this member record. Contact your administrator.",
        )

    return _render(
        request, tenant, "member_detail.html",
        page_title="Member Detail", crumb=f"Member Detail / {member.member_id}", m=member,
    )


# --------------------------------------------------------------------------
# open sub-account (the irreversible flow)
# --------------------------------------------------------------------------

def _subaccount_form_page(request: Request, tenant: Tenant, member, form: dict, errors: list[str]):
    churn = IdChurn()
    return _render(
        request, tenant, "subaccount_new.html",
        page_title="Open Sub-Account", crumb=f"Open Sub-Account / {member.member_id}",
        m=member, form=form, errors=errors, product_codes=data.PRODUCT_CODES,
        pc_id=churn("ddl"), dep_id=churn("txt"), nk_id=churn("txt"),
        status=400 if errors else 200,
    )


@app.get("/t/{slug}/members/{member_id}/subaccount/new", response_class=HTMLResponse)
async def subaccount_new(request: Request, slug: str, member_id: str) -> Response:
    tenant = _tenant_or_404(slug)
    path = f"/t/{tenant.slug}/members/{member_id}/subaccount/new"
    if (short := _guard(request, tenant, path)) is not None:
        return short
    member = data.MEMBERS.get(member_id)
    if member is None:
        return _render(
            request, tenant, "members_results.html",
            page_title="Search Results", crumb="Search Results", results=[], query=member_id,
        )
    return _subaccount_form_page(
        request, tenant, member, {"product_code": "SAV", "initial_deposit": "", "nickname": ""}, []
    )


@app.post("/t/{slug}/members/{member_id}/subaccount/new", response_class=HTMLResponse)
async def subaccount_validate(
    request: Request,
    slug: str,
    member_id: str,
    product_code: str = Form("SAV"),
    initial_deposit: str = Form(""),
    nickname: str = Form(""),
) -> Response:
    tenant = _tenant_or_404(slug)
    path = f"/t/{tenant.slug}/members/{member_id}/subaccount/new"
    if (short := _guard(request, tenant, path)) is not None:
        return short
    member = data.MEMBERS.get(member_id)
    if member is None:
        return _render(
            request, tenant, "members_results.html",
            page_title="Search Results", crumb="Search Results", results=[], query=member_id,
        )

    form = {"product_code": product_code, "initial_deposit": initial_deposit, "nickname": nickname}
    errors: list[str] = []

    if faults.consume(tenant.slug, "validation_error"):
        errors.append("Initial deposit does not meet the minimum for this product code.")
    else:
        cents = _parse_money(initial_deposit)
        if cents is None:
            errors.append("Initial deposit must be a dollar amount, for example 50.00.")
        elif cents < MIN_DEPOSIT_CENTS:
            errors.append("Initial deposit does not meet the minimum for this product code.")
        if product_code not in {c for c, _ in data.PRODUCT_CODES}:
            errors.append("Select a valid product code.")

    if errors:
        return _subaccount_form_page(request, tenant, member, form, errors)

    return _render(
        request, tenant, "subaccount_confirm.html",
        page_title="Confirm Sub-Account", crumb=f"Confirm / {member.member_id}",
        m=member, form=form, duplicate=False,
    )


@app.post("/t/{slug}/members/{member_id}/subaccount/confirm", response_class=HTMLResponse)
async def subaccount_confirm(
    request: Request,
    slug: str,
    member_id: str,
    product_code: str = Form("SAV"),
    initial_deposit: str = Form(""),
    nickname: str = Form(""),
) -> Response:
    tenant = _tenant_or_404(slug)
    path = f"/t/{tenant.slug}/members/{member_id}/subaccount/confirm"
    if (short := _guard(request, tenant, path)) is not None:
        return short
    member = data.MEMBERS.get(member_id)
    if member is None:
        return _render(
            request, tenant, "members_results.html",
            page_title="Search Results", crumb="Search Results", results=[], query=member_id,
        )

    form = {"product_code": product_code, "initial_deposit": initial_deposit, "nickname": nickname}

    already = any(
        o["member_id"] == member_id and o["product_code"] == product_code for o in data.OPENED
    )
    if faults.consume(tenant.slug, "duplicate") or already:
        return _render(
            request, tenant, "subaccount_confirm.html",
            page_title="Confirm Sub-Account", crumb=f"Confirm / {member.member_id}",
            m=member, form=form, duplicate=True,
        )

    cents = _parse_money(initial_deposit) or 0
    number = data.next_account_number()
    opened_at = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    data.OPENED.append(
        {
            "member_id": member_id,
            "product_code": product_code,
            "account_number": number,
            "opening_balance_cents": cents,
            "opened_at": opened_at,
        }
    )
    member.accounts.append(data.Account(number, "Savings", cents))

    return _render(
        request, tenant, "subaccount_done.html",
        page_title="Sub-Account Opened", crumb=f"Sub-Account Opened / {member.member_id}",
        m=member, form=form, account_number=number,
        opening_balance=f"${cents / 100:,.2f}", opened_at=opened_at,
    )


def _parse_money(raw: str) -> int | None:
    cleaned = (raw or "").strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return int(round(float(cleaned) * 100))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# fault control plane (test/demo only -- never part of a recorded flow)
# --------------------------------------------------------------------------

@app.get("/t/{slug}/__control")
async def control_state(request: Request, slug: str) -> JSONResponse:
    tenant = _tenant_or_404(slug)
    return JSONResponse({"tenant": tenant.slug, "armed": faults.state(tenant.slug), "available": faults.FAULT_POINTS})


@app.post("/t/{slug}/__control")
async def control_arm(
    request: Request, slug: str, fault: str = Query(...), count: int = Query(1)
) -> JSONResponse:
    tenant = _tenant_or_404(slug)
    if fault not in faults.FAULT_POINTS:
        return JSONResponse(
            {"error": "unknown fault", "fault": fault, "available": sorted(faults.FAULT_POINTS)},
            status_code=400,
        )
    faults.arm(tenant.slug, fault, count)
    return JSONResponse({"tenant": tenant.slug, "armed": faults.state(tenant.slug)})


@app.delete("/t/{slug}/__control")
async def control_clear(request: Request, slug: str) -> JSONResponse:
    tenant = _tenant_or_404(slug)
    faults.clear(tenant.slug)
    return JSONResponse({"tenant": tenant.slug, "armed": faults.state(tenant.slug)})
