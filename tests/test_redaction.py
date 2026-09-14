"""Redaction at every egress -- the replay half.

Part 11 names this suite: "no sensitive value reaches artifact, journal, or
prompt". The prompt egress was closed in M4 and is tested where the prompt is
built (`test_discovery_loop.py::test_regulated_data_never_reaches_the_model`);
duplicating it here would test the same code twice and the same risk once.

What M6 closes is the egress nobody had looked at: **replay**. Discovery
redacted and the console redacted; the replay engine read the screen raw, and
replay is the path that runs unattended in production. Its journal, its failure
messages and the `observed` field of every intervention a person reads were all
built from an unredacted observation of a member record carrying an SSN and a
date of birth.

The other two are fields that were declared, documented, defaulted in a
committed profile, and read by nothing: `OutputSpec.redact` and
`sensitivity.never_screenshot`. A schema is a claim about behaviour, and only a
read site makes the claim true.
"""

from __future__ import annotations

import json

import pytest

from cua.artifact.schema import OutputSpec, ParamType, Redaction
from cua.escalation import InterventionBroker, InterventionStore, LiveSession
from cua.observability.journal import MemoryJournal
from cua.policy.redaction import (
    REDACTED, apply_sensitivity, classify, redact_output, redaction_for, screenshot_allowed,
    scrub_text,
)
from cua.profiles import ProfileRepository
from cua.replay import ReplayEngine, Success
from cua.replay.engine import CredentialResolver
from cua.session.control import ControlLease
from cua.surfaces.web_playwright import WebSurface
from factories import lookup_balance_artifact

CREDS = CredentialResolver({"MOCKBANK_USER": "operator", "MOCKBANK_PASS": "demo-pass-not-real"})
PARAMS = {"member_id": "12345"}

#: Fabricated, and the only reason the fixture has an SSN field at all. If this
#: string appears in a journal, an artifact or an evidence file, something that
#: was supposed to mask it did not run.
SSN = "521-84-9077"
DOB = "1979-03-11"


@pytest.fixture
def profile(live_server):
    resolved = ProfileRepository("profiles").resolve("demo-cu")
    surface = resolved.profile.surface.model_copy(
        update={"base_url": f"{live_server}/t/demo-cu"})
    return resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage, hash=resolved.hash)


@pytest.fixture
async def signed_in(page, live_server):
    await page.goto(f"{live_server}/t/demo-cu/login", wait_until="networkidle")
    await page.fill("input[type=text]", "operator")
    await page.fill("input[type=password]", "demo-pass-not-real")
    await page.click("input[type=submit]")
    await page.wait_for_timeout(300)
    return page


def dump(journal: MemoryJournal) -> str:
    return json.dumps([e.to_dict() for e in journal.events], default=str)


# ==========================================================================
# the replay journal
# ==========================================================================

async def test_a_replay_journal_never_carries_the_regulated_values_on_the_screen(
        signed_in, profile, live_server):
    """The whole run, end to end, over a screen that really does hold an SSN.

    Not a unit test of the masker: the question is whether every path the engine
    takes to a journal goes through it, and the only honest way to ask that is
    to run the flow and grep the result.
    """
    journal = MemoryJournal()
    engine = ReplayEngine(WebSurface(signed_in, journal=journal), lookup_balance_artifact(),
                          profile, journal=journal, credentials=CREDS, tenant="demo-cu")
    result = await engine.run(PARAMS)
    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()

    text = dump(journal)
    assert SSN not in text, "the member's SSN reached the journal"
    assert DOB not in text, "the member's date of birth reached the journal"


async def test_the_screen_summary_a_person_reads_is_redacted(signed_in, profile, live_server):
    """`_observed_summary` is the busiest egress in the engine: it lands in the
    journal, in `Failure.observed`, and in the `observed` field of every
    intervention. It is built from the member detail screen."""
    journal = MemoryJournal()
    surface = WebSurface(signed_in, journal=journal)
    engine = ReplayEngine(surface, lookup_balance_artifact(), profile,
                          journal=journal, credentials=CREDS, tenant="demo-cu")

    await signed_in.goto(f"{live_server}/t/demo-cu/members/12345", wait_until="networkidle")
    raw = await surface.observe()
    summary = await engine._observed_summary()

    assert any(SSN in (n.name or "") or SSN in (n.value or "") for n in raw.nodes), (
        "the fixture stopped rendering an SSN, so this test proves nothing -- fix "
        "the fixture rather than the assertion"
    )
    assert SSN not in summary
    assert DOB not in summary
    assert REDACTED in summary, "and something was actually masked, not merely absent"


async def test_a_masked_value_is_gone_from_its_neighbours_context_too(
        signed_in, profile, live_server):
    """A regulated value does not only live on its own node.

    `row_label` is "the other cell in this row", so the label cell beside an SSN
    carries that SSN as its structural context -- and anchors are rendered
    straight into prompts and evidence.
    """
    await signed_in.goto(f"{live_server}/t/demo-cu/members/12345", wait_until="networkidle")
    surface = WebSurface(signed_in)
    masked = apply_sensitivity(await surface.observe(), profile.profile)

    blob = masked.model_dump_json()
    assert SSN not in blob
    assert DOB not in blob


# ==========================================================================
# OutputSpec.redact -- declared in M2, read for the first time in M6
# ==========================================================================

def test_the_redaction_mode_comes_from_the_artifact_first_and_the_profile_second():
    """The artifact is the more specific statement; the profile is the
    institution's blanket rule for every author who did not think about it."""
    prof = ProfileRepository("profiles").resolve("demo-cu").profile

    assert redaction_for("account_number", OutputSpec(), prof) == "last4", "profile default"
    assert redaction_for("account_number", OutputSpec(redact=Redaction.DROP), prof) == "drop", \
        "the artifact overrides the profile"
    assert redaction_for("savings_balance", OutputSpec(), prof) == "none", \
        "nothing declared for it, and a balance is not regulated"


def test_a_profile_default_covers_the_spellings_nobody_thought_of():
    """Exact-name keying looked right and leaked.

    The recorded write flow calls its output `new_account_number`, the profile
    rule is keyed `account_number`, and the number went to `result.json` in
    full. A blanket rule that only covers one spelling is not a blanket rule,
    and the miss is silent: an output nobody wrote a key for is indistinguishable
    from an output nobody needed one for.
    """
    prof = ProfileRepository("profiles").resolve("demo-cu").profile

    assert redaction_for("new_account_number", OutputSpec(), prof) == "last4"
    assert redaction_for("account_number", OutputSpec(), prof) == "last4"
    # Tokens, not substrings -- or the rule starts firing on things it should not.
    assert redaction_for("accountnumber", OutputSpec(), prof) == "none"
    assert redaction_for("number", OutputSpec(), prof) == "none"
    assert redaction_for("opening_balance", OutputSpec(), prof) == "none"


def test_the_most_specific_default_wins():
    """Two rules can both apply; the one that named more of the output governs."""
    from cua.policy.redaction import _default_mode

    defaults = {"account_number": "last4", "primary_account_number": "drop"}
    assert _default_mode("primary_account_number", defaults) == "drop"
    assert _default_mode("new_account_number", defaults) == "last4"


def test_each_redaction_mode_renders_what_its_name_says():
    assert redact_output("100045218", "last4") == "*****5218"
    assert redact_output("100045218", "drop") == REDACTED
    assert redact_output("100045218", "mask") == REDACTED
    assert redact_output("100045218", "none") == "100045218"
    assert redact_output("100045218", "typo-in-a-committed-file") == REDACTED, (
        "an unknown mode fails closed: a legible log line is cheaper than a leak"
    )


async def test_a_redacted_output_is_masked_in_evidence_and_whole_for_the_caller(
        signed_in, profile):
    """The distinction `OutputSpec.redact` exists to make, asserted both ways.

    An agent that asked for an account number needs the account number. The
    boundary redaction defends is persistence, and collapsing the two renderings
    would either break the caller or leak into evidence.
    """
    artifact = lookup_balance_artifact()
    artifact = artifact.model_copy(update={"outputs": {
        "savings_balance": OutputSpec(type=ParamType.MONEY, redact=Redaction.LAST4)}})

    journal = MemoryJournal()
    engine = ReplayEngine(WebSurface(signed_in, journal=journal), artifact, profile,
                          journal=journal, credentials=CREDS, tenant="demo-cu")
    result = await engine.run(PARAMS)

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert result.outputs["savings_balance"] == "4210.75", "the caller gets the value"

    logged = journal.of("extraction.ok")[0].data
    assert logged["redaction"] == "last4"
    assert logged["value"] == "***0.75"
    assert "4210.75" not in dump(journal), "and no other event leaked it"


# ==========================================================================
# never_screenshot -- the same story, one screen wider
# ==========================================================================

def test_a_profile_can_forbid_capturing_a_screen_at_all():
    prof = ProfileRepository("profiles").resolve("demo-cu").profile
    forbidding = prof.model_copy(update={"sensitivity": prof.sensitivity.model_copy(
        update={"never_screenshot": (r"/members/\d+$",)})})

    assert screenshot_allowed("http://host/t/demo-cu/members", forbidding) is True
    assert screenshot_allowed("http://host/t/demo-cu/members/12345", forbidding) is False
    assert screenshot_allowed("http://host/t/demo-cu/members/12345", prof) is True, \
        "and an empty list forbids nothing"


async def test_a_forbidden_screen_produces_no_image_and_says_so(
        signed_in, profile, live_server, tmp_path):
    """Masking boxes are the finer instrument. This is the one for a page whose
    answer to "which parts are regulated" is "all of it"."""
    prof = profile.profile
    forbidding = profile.__class__(
        profile=prof.model_copy(update={"sensitivity": prof.sensitivity.model_copy(
            update={"never_screenshot": (r"/members/\d+$",)})}),
        lineage=profile.lineage, hash=profile.hash)

    await signed_in.goto(f"{live_server}/t/demo-cu/members/12345", wait_until="networkidle")
    journal = MemoryJournal()
    session = LiveSession(session_id="sess_x", surface=WebSurface(signed_in, journal=journal),
                          lease=ControlLease("sess_x", journal=journal),
                          profile=forbidding, journal=journal)

    observation, png = await session.snapshot()
    assert png is None
    assert journal.of("screenshot.suppressed"), "silently returning no image is not enough"
    assert observation.nodes, "the node list is still served -- it is what the operator acts on"


async def test_an_ordinary_screen_still_produces_one(signed_in, profile, live_server):
    """The guard must not be a tax on every other screen."""
    await signed_in.goto(f"{live_server}/t/demo-cu/members/12345", wait_until="networkidle")
    session = LiveSession(session_id="sess_x", surface=WebSurface(signed_in),
                          lease=ControlLease("sess_x"), profile=profile)
    _, png = await session.snapshot()
    assert png and png[:4] == b"\x89PNG"


# ==========================================================================
# the free-text net
# ==========================================================================

def test_the_card_detector_does_not_fire_on_every_long_number():
    """A detector that cries wolf gets switched off, which is worse than not
    having it. Luhn is what separates a card from an order number."""
    detectors = ("card_luhn",)
    assert scrub_text("card 4111111111111111", detectors) == f"card {REDACTED}"
    assert scrub_text("order 1234567890123456", detectors) == "order 1234567890123456"


# ==========================================================================
# Action.sensitive -- set since M1, read for the first time in M6
# ==========================================================================

async def test_a_sensitive_value_never_survives_into_a_surface_error(signed_in, live_server):
    """A driver error quotes the text it was typing, and the one `fill` in these
    flows that carries a credential is the one most likely to fail.

    Provoked with a locator that cannot resolve, which is the cheapest way to
    make the surface produce an error message about an action that has a value.
    """
    from cua.locators.model import LocatorBundle, RoleNameExact
    from cua.surfaces.base import Action, ActionType

    journal = MemoryJournal()
    surface = WebSurface(signed_in, journal=journal)
    secret = "demo-pass-not-real"

    result = await surface.act(Action(
        type=ActionType.FILL, value=secret, sensitive=True,
        target=LocatorBundle(
            target_id="password", frame_path=(),
            candidates=(RoleNameExact(role="textbox", name="No Such Field"),),
            notes="deliberately unresolvable"),
        reason="type the password"))

    assert result.ok is False
    assert secret not in (result.error or "")
    assert secret not in dump(journal)


async def test_an_ordinary_value_is_still_quoted_where_it_helps(signed_in):
    """The rule is about declared-sensitive values, not about hiding every
    value -- an error that will not say what it was doing is not debuggable."""
    from cua.locators.model import LocatorBundle, RoleNameExact
    from cua.surfaces.base import Action, ActionType

    surface = WebSurface(signed_in, journal=MemoryJournal())
    result = await surface.act(Action(
        type=ActionType.FILL, value="12345", sensitive=False,
        target=LocatorBundle(
            target_id="member_id", frame_path=(),
            candidates=(RoleNameExact(role="textbox", name="No Such Field"),),
            notes="deliberately unresolvable"),
        reason="type the member id"))

    assert result.ok is False
    assert "member_id" in (result.error or ""), "the diagnosis survives"
    assert result.error_detail["candidates_tried"] == ["role_name_exact(0 match)"]


# ==========================================================================
# account numbers -- three defects found by auditing one value end to end
#
# The audit asked a single question: where does a real account number end up?
# The answers were that the committed node rule matched nothing, the detector
# that would have caught it anyway was off, and `result.json` wrote the value
# verbatim into the evidence pack. Each looked fine in isolation; the only way
# to see them was to follow one value to every surface it reaches.
# ==========================================================================

SUBACCOUNT_PARAMS = {"member_id": "12345", "product_code": "HSA", "initial_deposit": "50.00"}


async def open_a_subaccount(page, profile, journal=None):
    """Run the write flow and return (result, the number the app really wrote)."""
    from cua.replay.engine import ReplayEngine
    from factories import open_subaccount_artifact
    from mockbank import data

    journal = journal or MemoryJournal()
    engine = ReplayEngine(WebSurface(page, journal=journal), open_subaccount_artifact(),
                          profile, journal=journal, credentials=CREDS, tenant="demo-cu",
                          confirm_irreversible=True)
    result = await engine.run(SUBACCOUNT_PARAMS)
    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    return result, data.OPENED[0]["account_number"], journal


def test_every_declared_node_rule_is_reachable_by_its_own_matcher():
    """The meta-test, and the one that would have caught this.

    A sensitivity rule that matches nothing looks identical to one that works:
    the profile parses, the tests pass, and the masking simply never happens.
    The `account_number` rule shipped that way -- it asked for a table whose
    anchor was "Account Number", and `within_table` matches a grid by its panel
    header ("Accounts"). `classify()` returned None on the exact cell it was
    aimed at.
    """
    prof = ProfileRepository("profiles").resolve("demo-cu").profile
    # What each relation is matched against, from locators.resolve._in_scope.
    matched_against = {"within_section", "within_table", "within_row", "within_dialog"}
    for rule in prof.sensitivity.node_rules:
        scope = rule.match.scope
        if scope is None:
            continue
        assert scope.relation in matched_against, scope.relation
        assert scope.anchor.text != "Account Number", (
            "'Account Number' is a COLUMN header, and no relation matches on one. "
            "This is the exact mistake the original rule made; scope to the grid "
            "('Accounts') and match the cell by shape instead."
        )


async def test_an_account_number_is_masked_wherever_this_product_renders_it(
        signed_in, profile):
    """Two screens, two layouts, one rule each.

    The grid on the member record is a column of bare numbers; the confirmation
    screen is a label/value row. A rule written for one does not cover the
    other, which is why there are two -- and why this visits both rather than
    trusting that "the account number is masked" is a single fact.
    """
    result, real, _ = await open_a_subaccount(signed_in, profile)
    assert result.outputs["account_number"] == real, "the caller still gets it whole"

    surface = WebSurface(signed_in)
    on_confirmation = apply_sensitivity(await surface.observe(), profile.profile)
    assert real not in on_confirmation.model_dump_json(), "the confirmation screen"

    await signed_in.goto(f"{profile.base_url}/members/12345", wait_until="networkidle")
    on_record = apply_sensitivity(await surface.observe(), profile.profile)
    assert real not in on_record.model_dump_json(), "the member record's grid"
    assert "100045218" not in on_record.model_dump_json(), "and the seeded accounts too"


async def test_a_persisted_screenshot_masks_more_than_the_live_one(signed_in, profile):
    """The two audiences are not the same, and until now they got the same file.

    An operator holding the lease needs to read the account number off the
    screen in front of them. A PNG in an evidence pack outlives the incident and
    travels, so it gets everything the profile classifies.
    """
    from cua.policy.redaction import screenshot_masks

    await open_a_subaccount(signed_in, profile)
    await signed_in.goto(f"{profile.base_url}/members/12345", wait_until="networkidle")
    raw = await WebSurface(signed_in).observe()

    live = screenshot_masks(raw, profile.profile)
    persisted = screenshot_masks(raw, profile.profile, persisted=True)

    assert len(persisted) > len(live), (
        "the persisted copy must cover the account-number cells the live view "
        "deliberately leaves readable"
    )
    assert live, "and the live view still hides what an operator may never see"


async def test_the_evidence_copy_of_a_result_never_carries_the_value_itself(
        signed_in, profile):
    """`result.json` is an egress, and it was the one that was missed.

    Every other path that writes an extracted value goes through the redactor.
    This one wrote `result.outputs` verbatim -- so an evidence pack recorded the
    number the journal had just masked to its last four digits, in a file that
    ships with the deliverable.
    """
    from cua.replay.result import to_dict

    result, real, journal = await open_a_subaccount(signed_in, profile)

    written = to_dict(result)
    assert written["outputs"] == {"account_number": f"{'*' * (len(real) - 4)}{real[-4:]}"}
    assert real not in json.dumps(written, default=str)
    assert real not in dump(journal), "and the journal agrees with it"
    assert result.outputs["account_number"] == real, (
        "while the caller's copy is untouched -- the boundary is persistence, "
        "not the return value"
    )


def test_a_result_nobody_declared_a_redaction_for_fails_closed():
    """Silence is not permission.

    A `Success` built without `evidence_outputs` is one nobody has said how to
    persist. The names survive, so a reader can still see WHAT was read; the
    values do not.
    """
    from cua.replay.result import Success, to_dict

    hand_built = Success(outputs={"account_number": "200050001"})
    assert to_dict(hand_built)["outputs"] == {"account_number": REDACTED}



# ==========================================================================
# the two defects the evidence sweep turned up
# ==========================================================================

async def test_a_regulated_value_stays_masked_on_screens_that_do_not_declare_it(
        signed_in, profile, live_server):
    """A value is regulated for what it is, not for which screen it is on.

    This application embeds the member's name in the sub-account form's pane
    header -- "Open Sub-Account - Dana Whitfield (12345)" -- which exists ONLY as
    `anchors.section_label` on every node in that pane. No node carries it, so no
    node rule can reach it, and a per-observation secret set is empty there
    because nothing on that screen matched a rule.

    Carrying forward what was masked on the member record two steps earlier is
    what closes it, and it is the mechanism that makes "redaction at every
    egress" true of a flow rather than of a screenshot.
    """
    surface = WebSurface(signed_in)
    secrets: set[str] = set()

    # The screen that declares it.
    await signed_in.goto(f"{profile.base_url}/members/12345", wait_until="networkidle")
    apply_sensitivity(await surface.observe(), profile.profile, secrets)
    assert any("Whitfield" in s for s in secrets), "the name should have been learned here"

    # The screen that only mentions it in structural context.
    await signed_in.goto(f"{profile.base_url}/members/12345/subaccount/new",
                         wait_until="networkidle")
    raw = await surface.observe()
    assert "Whitfield" in raw.model_dump_json(), (
        "the fixture stopped embedding the name in the pane header, so this test "
        "proves nothing -- fix the fixture rather than the assertion"
    )
    carried = apply_sensitivity(raw, profile.profile, secrets)
    assert "Whitfield" not in carried.model_dump_json()

    # And without the carried set, it leaks -- which is what was happening.
    alone = apply_sensitivity(raw, profile.profile)
    assert "Whitfield" in alone.model_dump_json(), (
        "if this no longer leaks, the carry-forward is no longer what fixes it"
    )


async def test_masking_a_field_does_not_take_its_label_away(signed_in, profile):
    """An agent still has to see that an SSN field exists in order to leave it alone.

    A rule scoped `within_row` matches both cells of a label/value row: the value
    because its row label is the anchor, the label because the row text contains
    it. Masking both rendered a member record as `<redacted> <redacted>` three
    times over, and an operator could not tell which row was which.
    """
    await signed_in.goto(f"{profile.base_url}/members/12345", wait_until="networkidle")
    raw = await WebSurface(signed_in).observe()
    masked = apply_sensitivity(raw, profile.profile)

    by_name = {r.name: m.name for r, m in zip(raw.nodes, masked.nodes) if r.role == "cell"}
    for label in ("SSN", "Date of Birth", "Name"):
        assert by_name.get(label) == label, f"the {label!r} label must survive"

    values = {m.name for r, m in zip(raw.nodes, masked.nodes)
              if r.role == "cell" and r.anchors.row_label in ("SSN", "Date of Birth", "Name")}
    assert values == {REDACTED}, f"but the values must not: {values}"


# ==========================================================================
# labels are not data (the label/value category error)
# ==========================================================================

#: Text that NAMES a field or a column. Metadata about where regulated data
#: lives, never the data. Masking it takes the field's name away with its
#: contents: an operator reading `<redacted> <redacted>` down a member record
#: can no longer tell which row was the SSN, and an agent cannot see that a
#: surname field exists in order to leave it alone.
LABELS = {
    "Member ID", "Name", "Last Name", "Nickname", "SSN", "Date of Birth",
    "Branch", "Status", "Account Number", "Type", "Balance",
    "Product Code", "Initial Deposit", "Opening Balance", "New Account Number",
}

#: The data those labels describe. If any of these survives, the rules are
#: pointing at the wrong node.
VALUES = {"Dana Whitfield", SSN, DOB}


async def test_sensitivity_rules_never_mask_a_column_header_or_field_label(
        signed_in, profile, live_server):
    """A field's LABEL is metadata; the field's VALUE is the regulated thing.

    Both halves are asserted, because each alone passes for the wrong reason: a
    rule that masks nothing satisfies "no label is masked", and a rule that
    masks the whole screen satisfies "every value is masked".

    This is a regression test for a real category error. `_in_scope`'s
    `within_row` falls back to CONTAINMENT on the row text, so the anchor
    "Name" selected every row whose text contains "name" -- and this product
    has two that are not names: the search form's `Last Name` label and the
    confirmation screen's `Nickname`. The guard in `classify()` compared the
    node's own name to the anchor for EXACT equality, so "last name" != "name"
    and the label was masked while the surname typed into the box beside it was
    left in the clear. Exactly backwards, on all three surfaces at once.
    """
    base = f"{live_server}/t/demo-cu"
    surface = WebSurface(signed_in)

    async def nodes_on(url: str, fill: tuple[str, str] | None = None):
        await signed_in.goto(url, wait_until="networkidle")
        if fill is not None:
            await signed_in.fill(*fill)
            await signed_in.wait_for_timeout(100)
        return await surface.observe()

    screens = {
        "member record": await nodes_on(f"{base}/members/12345"),
        "search form": await nodes_on(f"{base}/members"),
        "search form, surname typed": await nodes_on(
            f"{base}/members", ("input[name='last_name']", "Whitfield")),
        "sub-account form": await nodes_on(f"{base}/members/12345/subaccount/new"),
    }

    for where, raw in screens.items():
        for node in raw.nodes:
            hit = classify(node, profile.profile)
            if hit is None:
                continue
            assert node.name.strip() not in LABELS, (
                f"on the {where}, the rule {hit[0]!r} masked {node.name!r}, which "
                f"is the LABEL naming the field. A label describes where "
                f"regulated data lives; it is not the data."
            )
            # A header is a label that happens to sit at the top of a column.
            assert node.role != "columnheader", (
                f"on the {where}, {hit[0]!r} masked the column header "
                f"{node.name!r}. The cells UNDER it hold the data, not it."
            )


async def test_the_values_those_labels_describe_are_still_masked(
        signed_in, profile, live_server):
    """The other half. Above says we stopped masking labels; this says we did
    not achieve that by masking nothing."""
    base = f"{live_server}/t/demo-cu"
    surface = WebSurface(signed_in)

    await signed_in.goto(f"{base}/members/12345", wait_until="networkidle")
    record = apply_sensitivity(await surface.observe(), profile.profile)
    rendered = json.dumps(record.model_dump(), default=str)
    for value in VALUES:
        assert value not in rendered, f"{value!r} survived redaction on the member record"
    # ...and the labels are all still legible in the same document.
    for label in ("Name", "SSN", "Date of Birth"):
        assert label in rendered, (
            f"{label!r} is gone from the record. The point of not masking labels "
            f"is that the reader can still tell which row was which."
        )


async def test_a_surname_typed_into_the_search_box_is_masked_even_though_its_label_is_not(
        signed_in, profile, live_server):
    """The inverse of the bug, and the reason it was worth fixing rather than
    just loosening: the label was masked and the value beside it was not."""
    base = f"{live_server}/t/demo-cu"
    surface = WebSurface(signed_in)
    await signed_in.goto(f"{base}/members", wait_until="networkidle")
    await signed_in.fill("input[name='last_name']", "Whitfield")
    await signed_in.wait_for_timeout(100)

    redacted = apply_sensitivity(await surface.observe(), profile.profile)
    box = next(n for n in redacted.nodes
               if n.role == "textbox" and n.anchors.row_label == "Last Name")
    assert box.sensitive and "Whitfield" not in (box.value or ""), (
        "the surname an operator typed is a surname"
    )
    assert any(n.name == "Last Name" for n in redacted.nodes), (
        "the label beside it is metadata and stays readable"
    )


def test_screenshot_masks_and_the_node_list_agree_about_what_is_regulated():
    """The three surfaces -- console node table, observation JSON, screenshot
    boxes -- must not disagree, and the reason they cannot is that all three go
    through the single `classify()`. This asserts the wiring, so that a fourth
    surface added later has to join it rather than grow its own copy."""
    import inspect

    from cua.policy import redaction

    source = inspect.getsource(redaction.screenshot_masks)
    assert "classify(" in source, (
        "screenshot masking must derive its boxes from the same classification "
        "the node list uses, or a label can be legible in one and painted over "
        "in the other"
    )


async def test_an_empty_field_is_never_reported_as_redacted(
        signed_in, profile, live_server):
    """Nothing typed means nothing to hide.

    A node rule selects a control by role and position -- "the textbox in the
    Last Name row" -- and the position is true of the control whether or not
    anyone has typed in it. So an untouched search box came back `sensitive`
    and the console, which renders `<redacted>` for any sensitive node, drew it
    over a field that was simply blank.

    That is the label/header error wearing a different hat: the rule describes
    where regulated data WOULD live and the UI reports it as though the data
    were there. An operator cannot tell a masked value from an empty box, which
    is the same confusion as not being able to tell which row was the SSN.
    """
    await signed_in.goto(f"{live_server}/t/demo-cu/members", wait_until="networkidle")
    blank = apply_sensitivity(await WebSurface(signed_in).observe(), profile.profile)

    for node in blank.nodes:
        if node.role != "textbox":
            continue
        assert not node.sensitive, (
            f"the empty {node.anchors.row_label!r} box is marked sensitive, so the "
            f"console will render <redacted> over a field nobody has typed into"
        )
        assert node.value in ("", None), "and there is nothing in it to have masked"


async def test_a_cell_with_no_content_under_a_regulated_column_is_not_masked(
        signed_in, profile, live_server):
    """The same rule, the other shape. `has_content` is about the node, not the
    kind of node, so it has to hold for cells too."""
    from cua.perception.model import UiNode

    await signed_in.goto(f"{live_server}/t/demo-cu/members/12345",
                         wait_until="networkidle")
    record = await WebSurface(signed_in).observe()
    real = next(n for n in record.nodes if n.name == "Dana Whitfield")

    # The same node, emptied. Position and role unchanged -- only the content.
    empty = real.model_copy(update={"name": "   ", "value": None})
    hollow = record.model_copy(update={"nodes": (empty,)})
    assert not apply_sensitivity(hollow, profile.profile).nodes[0].sensitive

    # ...and the untouched one is still masked, so this did not pass by turning
    # the rule off.
    assert apply_sensitivity(
        record.model_copy(update={"nodes": (real,)}), profile.profile
    ).nodes[0].sensitive
