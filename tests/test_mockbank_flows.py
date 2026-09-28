"""M0 smoke tests for the mockbank fixture.

These prove the surface is real before anything is built on top of it: both
flows reach their end state, both tenants serve the same product with different
labels, every fault fires where it is supposed to, and the hostile markup is
actually hostile (ids churn, the search input has no accessible name).

They run with no API key and no browser.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from mockbank import data, faults
from mockbank.app import app
from mockbank.tenants import TENANTS

TENANT = "demo-cu"
BASE = f"/t/{TENANT}"


@pytest.fixture
def client():
    faults.clear()
    data.reset()
    with TestClient(app) as c:
        yield c
    faults.clear()


@pytest.fixture
def auth(client):
    """A signed-in client. mockbank accepts any non-empty credential pair."""
    client.post(
        f"{BASE}/login",
        data={"username": "operator", "password": "demo-pass-not-real", "next": f"{BASE}/home"},
        follow_redirects=True,
    )
    return client


# --------------------------------------------------------------------------
# shell, frames, auth
# --------------------------------------------------------------------------

def test_shell_serves_two_named_frames(client):
    html = client.get(f"{BASE}/").text
    assert 'name="nav"' in html
    assert 'name="content"' in html


def test_content_requires_a_session(client):
    resp = client.get(f"{BASE}/members", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_login_grants_access(auth):
    assert "Dashboard" in auth.get(f"{BASE}/home").text


def test_unknown_tenant_is_rejected(client):
    resp = client.get("/t/nope/home", follow_redirects=False)
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# the surface is hostile in the ways we claim
# --------------------------------------------------------------------------

def test_element_ids_churn_between_renders(auth):
    """Any locator keyed on id or name resolves once, then rots."""
    ids_a = set(re.findall(r'id="(ctl00_[^"]+)"', auth.get(f"{BASE}/members").text))
    ids_b = set(re.findall(r'id="(ctl00_[^"]+)"', auth.get(f"{BASE}/members").text))
    assert ids_a and ids_b
    assert ids_a != ids_b, "ids must differ between renders or the fixture is not hostile"


def test_search_input_has_no_accessible_name(auth):
    """The member-id field is reachable only by anchor-relative locating."""
    html = auth.get(f"{BASE}/members").text
    field = re.search(r'<input type="text" id="ctl00_[^"]+" name="ctl00_[^"]+"[^>]*>', html)
    assert field, "expected the churned member-id input"
    markup = field.group(0)
    for naming_attr in ("aria-label", "title", "placeholder"):
        assert naming_attr not in markup
    # ...and no <label for> points at it either.
    assert "<label" not in html.split("<table class=\"form\">")[1].split("</table>")[0]


def test_no_test_ids_anywhere(auth):
    for path in (f"{BASE}/members", f"{BASE}/members/12345"):
        assert "data-testid" not in auth.get(path).text


# --------------------------------------------------------------------------
# flow 1: look up a member and read the savings balance
# --------------------------------------------------------------------------

def test_lookup_flow_reaches_the_savings_balance(auth):
    page = auth.get(f"{BASE}/members").text
    field = re.search(r'name="(ctl00_[^"]+)"', page).group(1)

    results = auth.post(f"{BASE}/members/search", data={field: "12345", "last_name": ""})
    assert "Whitfield" in results.text

    detail = auth.get(f"{BASE}/members/12345").text
    assert "Member Detail" in detail
    assert "$4,210.75" in detail          # the savings balance we will extract
    assert "521-84-9077" in detail        # regulated data present, for redaction to find


def test_search_by_last_name_lists_matches(auth):
    page = auth.get(f"{BASE}/members").text
    field = re.search(r'name="(ctl00_[^"]+)"', page).group(1)
    results = auth.post(f"{BASE}/members/search", data={field: "", "last_name": "Ellery"})
    assert "23456" in results.text


def test_missing_member_is_a_business_outcome_not_an_error(auth):
    page = auth.get(f"{BASE}/members").text
    field = re.search(r'name="(ctl00_[^"]+)"', page).group(1)
    resp = auth.post(f"{BASE}/members/search", data={field: "99999", "last_name": ""})
    assert resp.status_code == 200, "not-found must not be an HTTP error"
    assert "No records found" in resp.text


# --------------------------------------------------------------------------
# flow 2: open a sub-account and reach the confirmation screen
# --------------------------------------------------------------------------

def test_subaccount_flow_reaches_confirmation(auth):
    form = auth.get(f"{BASE}/members/12345/subaccount/new")
    assert "Open Sub-Account" in form.text

    confirm = auth.post(
        f"{BASE}/members/12345/subaccount/new",
        data={"product_code": "SAV", "initial_deposit": "50.00", "nickname": "Vacation"},
    )
    assert "Confirm Sub-Account" in confirm.text
    assert "cannot be undone" in confirm.text

    done = auth.post(
        f"{BASE}/members/12345/subaccount/confirm",
        data={"product_code": "SAV", "initial_deposit": "50.00", "nickname": "Vacation"},
    )
    assert "Sub-Account Opened" in done.text
    assert data.OPENED and data.OPENED[0]["member_id"] == "12345"


@pytest.mark.parametrize("deposit", ["", "abc", "1.00"])
def test_deposit_below_minimum_or_malformed_is_rejected(auth, deposit):
    resp = auth.post(
        f"{BASE}/members/12345/subaccount/new",
        data={"product_code": "SAV", "initial_deposit": deposit, "nickname": ""},
    )
    assert "Confirm Sub-Account" not in resp.text
    assert "alert" in resp.text


def test_repeat_open_is_reported_as_duplicate(auth):
    payload = {"product_code": "HSA", "initial_deposit": "75.00", "nickname": ""}
    auth.post(f"{BASE}/members/12345/subaccount/confirm", data=payload)
    again = auth.post(f"{BASE}/members/12345/subaccount/confirm", data=payload)
    assert "already exists" in again.text


# --------------------------------------------------------------------------
# fault injection -- each fires where FAULT_POINTS says it does
# --------------------------------------------------------------------------

def test_not_found_fault_overrides_a_valid_query(auth):
    faults.arm(TENANT, "not_found")
    page = auth.get(f"{BASE}/members").text
    field = re.search(r'name="(ctl00_[^"]+)"', page).group(1)
    resp = auth.post(f"{BASE}/members/search", data={field: "12345", "last_name": ""})
    assert "No records found" in resp.text


def test_validation_error_fault_rejects_a_valid_submission(auth):
    faults.arm(TENANT, "validation_error")
    resp = auth.post(
        f"{BASE}/members/12345/subaccount/new",
        data={"product_code": "SAV", "initial_deposit": "500.00", "nickname": ""},
    )
    assert "minimum" in resp.text
    assert "Confirm Sub-Account" not in resp.text


def test_permission_denied_fault(auth):
    faults.arm(TENANT, "permission_denied")
    resp = auth.get(f"{BASE}/members/12345")
    assert resp.status_code == 403
    assert "Not Authorized" in resp.text


def test_restricted_member_is_denied_without_any_fault(auth):
    resp = auth.get(f"{BASE}/members/45678")
    assert resp.status_code == 403


def test_interstitial_fault_obstructs_the_next_page(auth):
    faults.arm(TENANT, "interstitial")
    html = auth.get(f"{BASE}/members").text
    assert 'role="dialog"' in html
    assert "modalWrap" in html                      # a real overlay, not decoration
    assert 'role="dialog"' not in auth.get(f"{BASE}/members").text   # fires once


def test_session_timeout_fault_bounces_to_login(auth):
    faults.arm(TENANT, "session_timeout")
    resp = auth.get(f"{BASE}/members", follow_redirects=False)
    assert resp.status_code == 303
    assert "expired=1" in resp.headers["location"]


def test_slow_load_fault_serves_a_progressbar_then_the_page(auth):
    faults.arm(TENANT, "slow_load")
    stalled = auth.get(f"{BASE}/members").text
    assert 'role="progressbar"' in stalled
    assert "http-equiv=\"refresh\"" in stalled
    assert "Member Search" in auth.get(f"{BASE}/members").text   # recovers on retry


def test_error_500_fault_is_a_hard_failure(auth):
    faults.arm(TENANT, "error_500")
    resp = auth.get(f"{BASE}/members")
    assert resp.status_code == 500
    assert "ServletException" in resp.text      # distinctive string for the profile detector


def test_duplicate_fault(auth):
    faults.arm(TENANT, "duplicate")
    resp = auth.post(
        f"{BASE}/members/12345/subaccount/confirm",
        data={"product_code": "VAC", "initial_deposit": "40.00", "nickname": ""},
    )
    assert "already exists" in resp.text


def test_inject_query_param_arms_a_one_shot_fault(auth):
    assert "No records found" not in auth.get(f"{BASE}/members?inject=not_found").text
    page = auth.get(f"{BASE}/members").text
    field = re.search(r'name="(ctl00_[^"]+)"', page).group(1)
    assert "No records found" in auth.post(
        f"{BASE}/members/search", data={field: "12345", "last_name": ""}
    ).text


def test_control_plane_arms_and_clears(auth):
    auth.post(f"{BASE}/__control", params={"fault": "not_found", "count": 2})
    assert auth.get(f"{BASE}/__control").json()["armed"]["not_found"] == 2
    auth.delete(f"{BASE}/__control")
    assert auth.get(f"{BASE}/__control").json()["armed"] == {}


def test_control_plane_rejects_unknown_faults(auth):
    assert auth.post(f"{BASE}/__control", params={"fault": "nonsense"}).status_code == 400


def _open_subaccount(auth, product_code="HSA"):
    """Drive the two-step sub-account form the way the recorded flow does."""
    form = {"product_code": product_code, "initial_deposit": "50.00", "nickname": ""}
    auth.post(f"{BASE}/members/12345/subaccount/new", data=form)
    return auth.post(f"{BASE}/members/12345/subaccount/confirm", data=form).text


def test_reset_reseeds_a_member_a_write_has_mutated(auth):
    """The README's demo sequence depends on this.

    Opening a sub-account appends a *Savings* row whatever the product code, so
    a member who has been written to has two of them -- and `lookup_balance`
    then correctly refuses to resolve an ambiguous locator. The write demo is
    documented with a reset immediately after it so re-running the read demo
    works; if this endpoint stops reseeding, that sequence breaks silently.
    """
    assert auth.get(f"{BASE}/members/12345").text.count("<td>Savings</td>") == 1

    _open_subaccount(auth)
    assert auth.get(f"{BASE}/members/12345").text.count("<td>Savings</td>") == 2, (
        "the write should have mutated the member -- otherwise this proves nothing"
    )

    body = auth.post(f"{BASE}/__control/reset").json()
    assert body["faults_cleared_for"] == TENANT
    assert auth.get(f"{BASE}/members/12345").text.count("<td>Savings</td>") == 1


def test_reset_restores_the_account_number_sequence(auth):
    """A reseed that left the counter running would make the demo's output drift
    on every repeat -- the kind of thing nobody notices until a recorded
    expectation stops matching."""
    auth.post(f"{BASE}/__control/reset")
    first = re.search(r"2000\d{5}", _open_subaccount(auth)).group(0)

    auth.post(f"{BASE}/__control/reset")
    assert re.search(r"2000\d{5}", _open_subaccount(auth)).group(0) == first


def test_reset_clears_armed_faults_for_the_caller(auth):
    auth.post(f"{BASE}/__control", params={"fault": "not_found", "count": 2})
    auth.post(f"{BASE}/__control/reset")
    assert auth.get(f"{BASE}/__control").json()["armed"] == {}


def test_reset_is_scoped_to_a_real_tenant(auth):
    assert auth.post("/t/nope/__control/reset").status_code == 404


# --------------------------------------------------------------------------
# two tenants, same vendor product
# --------------------------------------------------------------------------

def test_tenants_share_the_product_but_differ_in_labels(client):
    seen = {}
    for slug in TENANTS:
        client.post(
            f"/t/{slug}/login",
            data={"username": "operator", "password": "x", "next": f"/t/{slug}/members"},
            follow_redirects=True,
        )
        html = client.get(f"/t/{slug}/members").text
        assert "Meridian Core 4.2" in html          # same underlying product
        seen[slug] = TENANTS[slug].member_id_label
        assert seen[slug] in html

    assert seen["demo-cu"] == "Member ID"
    assert seen["valley-cu"] == "Account Holder #"
    assert seen["demo-cu"] != seen["valley-cu"], (
        "an exact-name locator recorded on one tenant must not trivially match the other"
    )


def test_both_tenants_run_the_same_flow(client):
    for slug in TENANTS:
        client.post(
            f"/t/{slug}/login",
            data={"username": "operator", "password": "x", "next": f"/t/{slug}/home"},
            follow_redirects=True,
        )
        assert "$4,210.75" in client.get(f"/t/{slug}/members/12345").text
