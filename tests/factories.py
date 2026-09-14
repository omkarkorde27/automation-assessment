"""Realistic artifact builders for tests.

Deliberately shaped like what the recorder will emit in M4 -- a real flow
against mockbank, not a minimal stub -- so the schema is exercised by something
that has to actually work.
"""

from __future__ import annotations

from cua.artifact.schema import (
    ApprovalState,
    Binding,
    CapabilityArtifact,
    CapabilityMeta,
    CapabilityRisk,
    Extraction,
    OutcomeSpec,
    OutputSpec,
    ParamSpec,
    ParamType,
    ParseSpec,
    ProductRef,
    RecordedAgainst,
    Step,
    StepAction,
    SuccessSpec,
    ValueSource,
)
from cua.conditions.model import ElementCondition, ElementMatch, UrlCondition, UrlMatch
from cua.locators.model import (
    Anchor,
    AnchorRelative,
    FrameRef,
    LocatorBundle,
    RoleNameExact,
    RoleNamePattern,
    SectionScope,
)

CONTENT = (FrameRef(name="content"),)


def bundle(target_id: str, *candidates, notes: str, frame_path=CONTENT, **kw) -> LocatorBundle:
    """`notes` is keyword-only and required, mirroring the schema: a bundle
    without stated reasoning is not reviewable, and these fixtures stand in for
    recorder output. `frame_path` defaults to the content frame, which is where
    almost everything in this app lives."""
    return LocatorBundle(
        target_id=target_id, frame_path=frame_path, candidates=candidates, notes=notes, **kw
    )


MEMBER_ID_FIELD = bundle(
    "member_id_input",
    RoleNameExact(role="textbox", name="Member ID"),
    AnchorRelative(
        anchor=Anchor(pattern=r"(?i)member\s*id|account holder\s*#"),
        relation="same_row",
        target_role="textbox",
    ),
    notes=(
        "This control has no accessible name and its id is regenerated per render, so "
        "neither a name nor an id locator survives. The anchor-relative candidate keys on "
        "the label cell in the same row, and its pattern spans both tenants' wording."
    ),
)

SEARCH_BUTTON = bundle(
    "search_button",
    RoleNameExact(role="button", name="Search"),
    notes="Submit input named by @value, which is not branded copy and is stable per tenant.",
)

RESULT_LINK = bundle(
    "result_row_link",
    RoleNamePattern(role="link", name_pattern=r"^\d{5}$"),
    notes="The member id is the link text; a pattern avoids baking one member's id in.",
)

SAVINGS_BALANCE = bundle(
    "savings_balance_cell",
    AnchorRelative(
        anchor=Anchor(text="Balance"),
        relation="same_column",
        target_role="cell",
        scope=SectionScope(anchor=Anchor(pattern="(?i)savings"), relation="within_row"),
    ),
    notes=(
        "A grid read is two-dimensional: the Balance column of the row containing Savings. "
        "A row ordinal would read the wrong account whenever account order differs."
    ),
)

NAV_SEARCH_LINK = bundle(
    "nav_member_search_link",
    RoleNamePattern(role="link", name_pattern=r"(?i)(member search|account holder lookup)"),
    frame_path=(FrameRef(name="nav"),),
    notes=(
        "Lives in the navigation frame and targets the content frame, so the flow "
        "changes screens without a page-level navigation. The pattern spans both "
        "tenants' wording for the same menu item."
    ),
)

NOT_FOUND_NOTICE = bundle(
    "not_found_notice",
    RoleNameExact(role="alert", name="No records found matching your search criteria."),
    RoleNamePattern(role="alert", name_pattern=r"(?i)no records found"),
    notes="Read the institution's own wording rather than substituting ours.",
)


OPEN_SUBACCOUNT_LINK = bundle(
    "open_subaccount_link",
    RoleNameExact(role="link", name="Open Sub-Account"),
    notes="Plain link text on the member detail screen; identical across both tenants.",
)

PRODUCT_CODE_SELECT = bundle(
    "product_code_select",
    RoleNameExact(role="combobox", name="Product Code"),
    notes=(
        "The one field on this form with a proper <label for>, so the accessible name "
        "is derived correctly and no anchor candidate is needed."
    ),
)

INITIAL_DEPOSIT_FIELD = bundle(
    "initial_deposit_input",
    AnchorRelative(
        anchor=Anchor(text="Initial Deposit"),
        relation="same_row",
        target_role="textbox",
    ),
    notes=(
        "Label sits in a sibling <td> with no `for`, so the control has no accessible "
        "name at all. The anchor-relative candidate is the only one that can reach it."
    ),
)

NICKNAME_FIELD = bundle(
    "nickname_input",
    RoleNameExact(role="textbox", name="Nickname"),
    notes="Named by aria-label. A third labelling style on the same form, deliberately.",
)

CONTINUE_BUTTON = bundle(
    "continue_button",
    RoleNameExact(role="button", name="Continue"),
    notes="Submit input named by @value. Reversible: it only reaches the review screen.",
)

CONFIRM_OPEN_BUTTON = bundle(
    "confirm_open_button",
    RoleNameExact(role="button", name="Confirm and Open Account"),
    notes=(
        "THE irreversible control. Named exactly, never by ordinal or position: if this "
        "bundle ever resolves ambiguously the run must escalate rather than guess, "
        "because the wrong guess posts a real account opening."
    ),
)

NEW_ACCOUNT_NUMBER = bundle(
    "new_account_number_cell",
    AnchorRelative(
        anchor=Anchor(text="New Account Number"),
        relation="same_row",
        target_role="cell",
    ),
    notes="Read the number the institution assigned, from the row its own label names.",
)

DUPLICATE_NOTICE = bundle(
    "duplicate_notice",
    RoleNamePattern(role="alert", name_pattern=r"(?i)already exists"),
    notes="The institution's own wording for an account it already holds.",
)


def open_subaccount_artifact(**overrides) -> CapabilityArtifact:
    """member.open_subaccount -- the IRREVERSIBLE flow.

    The second of the brief's two example goals, and the one where getting
    replay wrong costs something: step s11 posts an account opening that cannot
    be undone. Every step before it carries a checkpoint, which is what makes a
    resume point exist at all -- an unverified step can never be resumed onto.

    `SUBACCOUNT_ALREADY_EXISTS` is declared as an outcome rather than left to
    fail, because the institution already holding the account is an answer to
    the question that was asked, not a malfunction.
    """
    data = dict(
        capability=CapabilityMeta(
            id="member.open_subaccount",
            version="1.0.0",
            title="Open a sub-account for a member",
            description="Open a new sub-account and reach the confirmation screen.",
            risk_tier=CapabilityRisk.WRITES_IRREVERSIBLE,
            # Approved, because M6's replay-side gate needs a reviewed artifact
            # AND an explicit caller before an irreversible flow will run. This
            # one is hand-authored and read by every reviewer of this repo, so
            # `approved` is the honest state; `confirm_irreversible` is then the
            # half the tests vary, which is the half the caller controls.
            approval_state=ApprovalState.APPROVED,
        ),
        binding=Binding(
            product=ProductRef(vendor="meridian", product="core", version_range=">=4.2 <5"),
            app_profile_ref="meridian-core@4.2",
            recorded_against=RecordedAgainst(
                tenant="demo-cu",
                surface_fingerprint={"subaccount_new": "sha256:ccc",
                                     "subaccount_confirm": "sha256:ddd"},
            ),
        ),
        inputs={
            "member_id": ParamSpec(
                type=ParamType.STRING, description="The member's five-digit id.",
                pattern=r"^\d{5}$", example="12345",
            ),
            "product_code": ParamSpec(
                type=ParamType.STRING, description="Product code for the new sub-account.",
                pattern=r"^[A-Z0-9]{3,4}$", example="HSA",
            ),
            "initial_deposit": ParamSpec(
                type=ParamType.STRING, description="Opening deposit, minimum $25.00.",
                pattern=r"^\d+(\.\d{2})?$", example="50.00",
            ),
        },
        outputs={
            "account_number": OutputSpec(
                type=ParamType.STRING, description="The new sub-account's number."
            )
        },
        steps=(
            Step(id="s1", intent="Open the servicing console",
                 action=StepAction(type="navigate", url_template="{base_url}/"),
                 risk="navigate",
                 checkpoint=UrlCondition(url=UrlMatch(matches="/home$"), frame=CONTENT)),
            Step(id="s2", intent="Open the member search screen from the navigation menu",
                 action=StepAction(type="click", target=NAV_SEARCH_LINK),
                 risk="navigate",
                 checkpoint=UrlCondition(url=UrlMatch(matches="/members$"), frame=CONTENT)),
            Step(id="s3", intent="Enter the member id into the search field",
                 action=StepAction(type="fill", target=MEMBER_ID_FIELD,
                                   value_from=ValueSource(param="member_id")),
                 risk="input"),
            Step(id="s4", intent="Run the search",
                 action=StepAction(type="click", target=SEARCH_BUTTON),
                 risk="submit_reversible",
                 checkpoint=UrlCondition(url=UrlMatch(matches="/members/search$"),
                                         frame=CONTENT)),
            Step(id="s5", intent="Open the matching member's record",
                 action=StepAction(type="click", target=RESULT_LINK),
                 risk="navigate",
                 checkpoint=UrlCondition(url=UrlMatch(matches=r"/members/\d+$"), frame=CONTENT)),
            Step(id="s6", intent="Start a new sub-account for this member",
                 action=StepAction(type="click", target=OPEN_SUBACCOUNT_LINK),
                 risk="navigate",
                 checkpoint=UrlCondition(url=UrlMatch(matches="/subaccount/new$"),
                                         frame=CONTENT)),
            Step(id="s7", intent="Choose the product code",
                 action=StepAction(type="select_option", target=PRODUCT_CODE_SELECT,
                                   value_from=ValueSource(param="product_code")),
                 risk="input"),
            Step(id="s8", intent="Enter the opening deposit",
                 action=StepAction(type="fill", target=INITIAL_DEPOSIT_FIELD,
                                   value_from=ValueSource(param="initial_deposit")),
                 risk="input"),
            Step(id="s9", intent="Continue to the review screen",
                 action=StepAction(type="click", target=CONTINUE_BUTTON),
                 risk="submit_reversible",
                 # The resume point that matters. Everything up to here is
                 # reversible, so rebuilding it after a dropped session costs
                 # nothing but time; past here it would cost an account.
                 #
                 # Asserted on the irreversible control's presence, not on a
                 # heading: this app styles its screen titles as plain <div>s,
                 # which are not headings in the accessibility tree and so are
                 # not nodes a checkpoint can see. The confirm button exists on
                 # exactly one screen, which is the property we actually need.
                 checkpoint=ElementCondition(
                     element=ElementMatch(role="button", name="Confirm and Open Account"),
                     exists=True)),
            Step(id="s10", intent="Confirm and open the account",
                 action=StepAction(type="click", target=CONFIRM_OPEN_BUTTON),
                 risk="submit_irreversible",
                 # The confirm POST renders the done screen without changing the
                 # URL, so a URL checkpoint here would pass on the screen we just
                 # left. The app marks its success banner role="status", and that
                 # is what proves the opening actually happened.
                 checkpoint=ElementCondition(
                     element=ElementMatch(role="status",
                                          name_matches="(?i)opened successfully"),
                     exists=True)),
        ),
        extractions=(
            Extraction(output="account_number", target=NEW_ACCOUNT_NUMBER, source="name"),
        ),
        outcomes=(
            OutcomeSpec(
                code="SUBACCOUNT_ALREADY_EXISTS",
                description="The member already holds a sub-account with this product code.",
                after_step="s10",
                detect=ElementCondition(
                    element=ElementMatch(role="alert", name_matches="(?i)already exists"),
                    exists=True),
                message_from=DUPLICATE_NOTICE,
            ),
        ),
        success=SuccessSpec(
            checkpoint=ElementCondition(
                element=ElementMatch(role="status", name_matches="(?i)opened successfully"),
                exists=True),
            description="Reached the sub-account confirmation screen.",
        ),
    )
    data.update(overrides)
    return CapabilityArtifact(**data)


def lookup_balance_artifact(**overrides) -> CapabilityArtifact:
    """member.lookup_balance -- read-only, one input, one output, one outcome.

    Shaped to actually replay against mockbank's frameset: the shell is opened,
    navigation happens by clicking the nav frame's link (which targets the
    content frame), and every checkpoint asserts the CONTENT frame's URL,
    because the page URL is the shell and never changes.
    """
    data = dict(
        capability=CapabilityMeta(
            id="member.lookup_balance",
            version="1.0.0",
            title="Look up a member's savings balance",
            description="Search for a member by id and read their current savings balance.",
            risk_tier=CapabilityRisk.READ_ONLY,
        ),
        binding=Binding(
            product=ProductRef(vendor="meridian", product="core", version_range=">=4.2 <5"),
            app_profile_ref="meridian-core@4.2",
            recorded_against=RecordedAgainst(
                tenant="demo-cu",
                surface_fingerprint={"member_search": "sha256:aaa", "member_detail": "sha256:bbb"},
            ),
        ),
        inputs={
            "member_id": ParamSpec(
                type=ParamType.STRING,
                description="The member's five-digit id.",
                pattern=r"^\d{5}$",
                example="12345",
            )
        },
        outputs={
            "savings_balance": OutputSpec(
                type=ParamType.MONEY, description="Current savings balance."
            )
        },
        steps=(
            Step(
                id="s1",
                intent="Open the servicing console",
                action=StepAction(type="navigate", url_template="{base_url}/"),
                risk="navigate",
                checkpoint=UrlCondition(url=UrlMatch(matches="/home$"), frame=CONTENT),
            ),
            Step(
                id="s2",
                intent="Open the member search screen from the navigation menu",
                action=StepAction(type="click", target=NAV_SEARCH_LINK),
                risk="navigate",
                checkpoint=UrlCondition(url=UrlMatch(matches="/members$"), frame=CONTENT),
            ),
            Step(
                id="s3",
                intent="Enter the member id into the search field",
                action=StepAction(
                    type="fill",
                    target=MEMBER_ID_FIELD,
                    value_from=ValueSource(param="member_id"),
                ),
                risk="input",
            ),
            Step(
                id="s4",
                intent="Run the search",
                action=StepAction(type="click", target=SEARCH_BUTTON),
                risk="submit_reversible",
                # Asserting the results screen arrived, rather than assuming the
                # click worked. Without this the next step races the page load
                # and reports a locator failure for what is really "the search
                # has not come back yet". Both the found and not-found cases
                # land here, and the not-found outcome is detected by the ladder
                # before this checkpoint is ever evaluated.
                checkpoint=UrlCondition(url=UrlMatch(matches="/members/search$"), frame=CONTENT),
            ),
            Step(
                id="s5",
                intent="Open the matching member's record",
                action=StepAction(type="click", target=RESULT_LINK),
                risk="navigate",
                checkpoint=UrlCondition(url=UrlMatch(matches=r"/members/\d+$"), frame=CONTENT),
            ),
        ),
        extractions=(
            Extraction(
                output="savings_balance",
                target=SAVINGS_BALANCE,
                source="name",
                parse=ParseSpec(type="money", locale="en_US"),
            ),
        ),
        outcomes=(
            OutcomeSpec(
                code="MEMBER_NOT_FOUND",
                description="The institution has no member with that id.",
                after_step="s4",
                detect=ElementCondition(
                    element=ElementMatch(role="alert", name_matches="(?i)no records found"),
                    exists=True,
                ),
                message_from=NOT_FOUND_NOTICE,
            ),
        ),
        success=SuccessSpec(
            checkpoint=UrlCondition(url=UrlMatch(matches=r"/members/\d+$"), frame=CONTENT),
            description="Reached the member detail screen.",
        ),
    )
    data.update(overrides)
    return CapabilityArtifact(**data)
