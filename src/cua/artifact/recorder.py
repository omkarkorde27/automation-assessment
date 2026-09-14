"""Turn a discovery run into a capability artifact.

The recorder is where the model's exploration becomes a contract. It never reads
the model's messages -- only the transcript of what was *done* -- so the artifact
describes observed behaviour rather than a description the model wrote of its
own behaviour.

Four jobs, in order:

1. **Prune** to the successful path. Failed attempts, re-observations and
   backtracking are how a person finds the way; they are not the way.
2. **Derive a `LocatorBundle`** per step from the node the model chose. This is
   the point of the whole design: the model picked node `content:n4`, and the
   recorder -- which can see the accessible name, the row label, the section, and
   how many other nodes would match each candidate -- writes the durable locator.
   A model that writes selectors produces a capability only as robust as its
   selector-writing; a recorder that derives them produces one as robust as the
   ladder.
3. **Canonicalize** values into parameters (R-M4-1), so the capability is
   reusable and so no customer data is baked into it.
4. **Synthesize checkpoints** from what actually changed on screen, so every step
   asserts its own result instead of assuming the click worked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..conditions.model import (
    AnyCondition, Condition, ElementCondition, ElementMatch, UrlCondition, UrlMatch,
)
from ..discovery.transcript import DiscoveryRun, DiscoveryStep, value_shape
from ..locators.model import (
    Anchor, AnchorRelative, FrameRef, LabelFor, LocatorBundle, RecordedNode,
    RoleNameExact, RoleNamePattern, RoleOrdinal, SectionScope,
)
from ..perception.model import Observation, UiNode, normalize_name
from ..surfaces.base import ActionType, RiskTier
from .schema import (
    Binding, CapabilityArtifact, CapabilityMeta, CapabilityRisk, Extraction, OutcomeSpec,
    OutputSpec, ParamSpec, ParamType, ParseSpec, ProductRef, Provenance, RecordedAgainst,
    Step, StepAction, SuccessSpec, ValueSource,
)


class RecorderRefusal(Exception):
    """The recording cannot be made honestly, so it is not made at all.

    Raised rather than returning a degraded artifact: an artifact that silently
    contains a member's name is worse than no artifact, because it looks fine.
    """


# --------------------------------------------------------------------------
# R-M4-1 -- parameterization is enforced here, and nowhere else can enforce it
# --------------------------------------------------------------------------

def _as_value_source(
    step: DiscoveryStep, run: DiscoveryRun, params: dict[str, str]
) -> ValueSource:
    """Decide whether a typed value is a parameter, a constant, or a refusal.

    The schema permits `{literal: "..."}` because genuine constants exist -- a
    product code chosen from a fixed dropdown. Nothing in the schema can tell a
    constant from a leaked parameter or from a customer's name read off one
    screen and typed into another, so the guarantee has to live at record time.
    """
    value = step.value or ""

    if step.is_param_candidate and step.param_name:
        return ValueSource(param=step.param_name)

    normalized = normalize_name(value)

    # A value the model did not flag, but which matches something it DID flag,
    # is a parameter the model forgot to mark. Recording it as a literal would
    # hard-code this run's member id into every future run.
    for name, declared in params.items():
        if normalized and normalized == normalize_name(declared):
            return ValueSource(param=name)

    # The same field, filled again with a different value. A model exploring a
    # flow will try a second member id to see what "not found" looks like, and
    # the application echoes that id straight back onto the screen -- which
    # would otherwise trip the screen-data check below and refuse the recording
    # with a diagnosis ("read off a customer record") that is simply wrong. The
    # field is already bound to a parameter; a second value going into it is
    # that same parameter, not a constant and not stolen data.
    if step.node is not None:
        bound = _parameter_bound_to(step, run)
        if bound:
            return ValueSource(param=bound)

    # Part 5.3's third canonicalization: timestamps are flagged volatile.
    #
    # A date is correct exactly once. Frozen as a literal it back-dates every
    # future run to the day the flow was recorded -- which most applications
    # accept without complaint, which is what makes it dangerous: nothing
    # downstream would ever surface it. So it becomes a declared input, typed
    # DATE, marked volatile, with the recorded value kept as the default so
    # unattended replay still runs.
    #
    # Checked BEFORE the screen-data refusal below on purpose. A date the
    # application also displays would otherwise be refused as customer data read
    # off a record, with a diagnosis that is simply wrong: a date is not PII, and
    # the right answer for it is parameterization rather than refusal.
    if _looks_volatile(value):
        return ValueSource(param=_volatile_param_name(step, params))

    # A value that appeared on screen and was typed back is recorded customer
    # data. There is no version of baking that into a reusable capability that
    # is acceptable, so the recording fails rather than degrades.
    if normalized and len(normalized) > 2:
        for shown in run.screen_texts():
            if normalize_name(shown) == normalized:
                raise RecorderRefusal(
                    f"step {step.index} ({step.tool}) would record the literal {value!r}, "
                    f"which the application displayed on screen during this run. That is "
                    f"data read off a customer record, not a constant. Re-run discovery and "
                    f"mark it as a parameter, or remove the step."
                )

    return ValueSource(literal=value)


# Conservative on purpose: a detector that fires on ordinary text turns every
# recorded constant into a parameter the caller now has to supply. Dates in the
# three shapes a back-office form actually accepts, plus a clock time.
_VOLATILE = (
    re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?Z?$"),   # 2026-09-14
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$"),                            # 09/14/2026
    re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{2,4}$"),                         # 14-SEP-2026
    re.compile(r"^\d{1,2}:\d{2}(:\d{2})?( ?[AaPp][Mm])?$"),              # 14:05
)


def _looks_volatile(value: str) -> bool:
    """Is this a timestamp -- a value that was true on one day and no other?"""
    text = (value or "").strip()
    return bool(text) and any(p.match(text) for p in _VOLATILE)


def _volatile_param_name(step: DiscoveryStep, params: dict[str, str]) -> str:
    """Name the input a promoted timestamp becomes.

    From the field's own label where there is one, because `effective_date` is
    reviewable and `date_1` is not. Falls back to a positional name rather than
    raising: refusing a whole recording over a field with no label would trade a
    real capability for a cosmetic one.
    """
    node = step.node
    candidates = []
    if node is not None:
        candidates = [node.anchors.label, node.anchors.row_label, node.name]
    for raw in candidates:
        slug = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")
        if slug and not slug[0].isdigit() and slug not in params:
            return slug[:40]
    return f"recorded_date_{step.index}"


def _parameter_bound_to(step: DiscoveryStep, run: DiscoveryRun) -> str:
    """The parameter another step already bound to this same control, if any.

    Identity is the control's label and role rather than its node id, because
    this application regenerates ids on every render -- two fills of the same
    field across two page loads carry different ids by design.
    """
    node = step.node
    if node is None:
        return ""
    signature = (node.role, normalize_name(node.name),
                 normalize_name(node.anchors.row_label), normalize_name(node.anchors.label))
    for other in run.steps:
        if other is step or other.node is None or not other.param_name:
            continue
        if not other.is_param_candidate:
            continue
        if (other.node.role, normalize_name(other.node.name),
                normalize_name(other.node.anchors.row_label),
                normalize_name(other.node.anchors.label)) == signature:
            return other.param_name
    return ""


# --------------------------------------------------------------------------
# locator derivation -- the model chose a node; we write the durable address
# --------------------------------------------------------------------------

@dataclass
class _Derived:
    bundle: LocatorBundle
    reasons: list[str] = field(default_factory=list)


def _uniqueness(candidate, node: UiNode, observation: Observation) -> int:
    """How many nodes in the same frame this candidate would match.

    Counted at record time so the bundle can be *scored* rather than hoped over:
    a candidate that already matches three nodes on the screen it was recorded
    from will never resolve uniquely later.
    """
    from ..locators.resolve import _matches

    pool = [n for n in observation.nodes if n.frame_path == node.frame_path and n.visible]
    try:
        return len(_matches(candidate, pool))
    except Exception:
        return 0


def _run_varying(text: str, params: dict[str, str]) -> bool:
    """Is this text one of the values that differ from run to run?"""
    norm = normalize_name(text)
    return bool(norm) and any(normalize_name(v) == norm for v in params.values())


def _is_data_row(node: UiNode, params: dict[str, str]) -> bool:
    """Is this node sitting in a row of results rather than a labelled form row?

    A search-result row contains the value that was searched for, so anything
    else in that row -- the member's name, their branch, their status -- is this
    run's data too. `row_label` is "the other cell in this row", which on a
    results row means a customer's name.
    """
    row = node.anchors.row_text or ""
    return any(v and v in row for v in params.values())


def derive_bundle(
    node: UiNode,
    observation: Observation,
    *,
    target_id: str,
    label_variants: dict[str, list[str]] | None = None,
    params: dict[str, str] | None = None,
    data_texts: set[str] | None = None,
    for_extraction: bool = False,
) -> LocatorBundle:
    """Build the candidate ladder for one node, strongest first.

    Only candidates that actually resolve uniquely against the screen they were
    recorded from are kept. A ladder padded with candidates that were already
    ambiguous at record time is worse than a short one: it turns a clean
    LOCATOR_UNRESOLVED into an ambiguity nobody can act on.

    R-M4-1 applies to locators, not only to values. On a results screen the link
    that opens a member's record is *named* after that member -- its accessible
    name is "12345" and the cell beside it holds "Dana Whitfield". Recording
    either verbatim produces a capability that works for exactly one customer and
    carries their name around in its locator. So a candidate whose matching text
    is this run's data is generalized to the value's shape, and an anchor keyed
    on a data row is dropped: a shape is reusable, a customer's name is not.
    """
    if observation.by_id(node.node_id) is None:
        # Every candidate is scored by how many nodes it matches on the screen
        # the node was chosen from. If the node is not on that screen, every
        # count is zero and the ladder comes out empty with a misleading reason.
        raise RecorderRefusal(
            f"{node.describe()} is not present in the observation it is being derived "
            f"against, so no candidate can be scored. This is a wiring error: a bundle "
            f"must be derived from the screen the node was selected on."
        )

    variants = label_variants or {}
    values = params or {}
    candidates: list = []
    reasons: list[str] = []

    name = node.name.strip()
    norm = normalize_name(name)
    # An extraction target's text is the thing being READ. It cannot also be
    # what identifies the node: a locator matching "4,210.75" finds the balance
    # only while the balance happens to be 4,210.75. Such a node must be reached
    # structurally -- the Balance cell of the Savings row -- or not at all.
    name_is_data = _run_varying(name, values) or for_extraction

    if for_extraction:
        # The node's own text is the value being read, so it can never identify
        # the node. A grid cell is reached the way a person reads one: the
        # Balance COLUMN of the Savings ROW. Both anchors are labels, so neither
        # changes when the number does.
        grid = _grid_candidate(node, data_texts or set())
        if grid is not None and _uniqueness(grid, node, observation) == 1:
            candidates.append(grid)
            reasons.append(
                f"read two-dimensionally -- the {node.anchors.col_header!r} column of the "
                f"row identified by its label. Recording this cell's own text would "
                f"produce a locator that finds the value only while it stays that value"
            )
    elif name and name_is_data:
        # The control is named after the thing it operates on. Recording the
        # name would pin the capability to one customer; the shape of the value
        # is what is actually stable about this control.
        shaped = RoleNamePattern(role=node.role, name_pattern=value_shape(name))
        if _uniqueness(shaped, node, observation) == 1:
            candidates.append(shaped)
            reasons.append(
                f"this control is named after the parameter value itself, so the recorded "
                f"candidate matches the value's SHAPE ({value_shape(name)}) rather than "
                f"this run's value -- otherwise the capability would only ever work for "
                f"one customer"
            )
    elif name:
        exact = RoleNameExact(role=node.role, name=name)
        if _uniqueness(exact, node, observation) == 1:
            candidates.append(exact)
            reasons.append(f"accessible name {name!r} is unique among {node.role}s in this frame")

    # A pattern spanning every tenant's spelling of this label, generated from
    # the overlays on disk rather than guessed. This is what makes one recording
    # run on both institutions.
    spellings = variants.get(norm)
    if name and not name_is_data and spellings and len(spellings) > 1:
        alternation = "|".join(re.escape(s) for s in spellings)
        pattern = RoleNamePattern(role=node.role, name_pattern=f"(?i)^({alternation})$")
        if _uniqueness(pattern, node, observation) == 1:
            candidates.append(pattern)
            reasons.append(
                f"tenants spell this label {spellings!r}, so a pattern candidate spans them"
            )
    elif name and not name_is_data and len(name.split()) <= 6:
        # Whitespace/case/punctuation tolerance for a single tenant.
        pattern = RoleNamePattern(role=node.role, name_pattern=f"(?i)^{re.escape(name)}$")
        if _uniqueness(pattern, node, observation) == 1:
            candidates.append(pattern)
            reasons.append("a case-insensitive pattern absorbs whitespace and casing drift")

    if node.anchors.label:
        label_for = LabelFor(control_role=node.role, label_text=node.anchors.label)
        if _uniqueness(label_for, node, observation) == 1:
            candidates.append(label_for)
            reasons.append(f"associated label {node.anchors.label!r} reaches the control")

    if node.anchors.row_label and _is_data_row(node, values):
        reasons.append(
            f"no anchor candidate recorded: this row is search-result data, so its "
            f"neighbouring cells hold one customer's details rather than a stable label"
        )
    elif node.anchors.row_label:
        anchored = AnchorRelative(
            anchor=Anchor(text=node.anchors.row_label), relation="same_row",
            target_role=node.role)
        if _uniqueness(anchored, node, observation) == 1:
            candidates.append(anchored)
            reasons.append(
                f"in a table row labelled {node.anchors.row_label!r} -- this is the candidate "
                f"that survives the per-render id churn, since it keys on text a person reads"
            )

    if not candidates:
        section = (node.anchors.section_label or node.anchors.nearest_heading
                   or node.anchors.dialog_label)
        if not section:
            raise RecorderRefusal(
                f"cannot address {node.describe()} durably: it has no accessible name, no "
                f"label, no row context and no enclosing section. A bare document-wide "
                f"ordinal is never recorded -- it would break on the first layout change."
            )
        pool = [n for n in observation.nodes
                if n.role == node.role and n.frame_path == node.frame_path and n.visible]
        candidates.append(RoleOrdinal(
            role=node.role, ordinal=pool.index(node) if node in pool else 0,
            scope=SectionScope(anchor=Anchor(text=section), relation="within_section")))
        reasons.append(
            f"last structural resort: no name, no label, no row -- an ordinal scoped to "
            f"section {section!r}, which is why the scope is mandatory"
        )

    # Strategy weight x how decisively the best candidate won.
    weights = {"role_name_exact": 1.0, "role_name_pattern": 0.95, "label_for": 0.9,
               "anchor_relative": 0.8, "role_ordinal": 0.5, "bbox_normalized": 0.2}
    best = weights.get(candidates[0].strategy, 0.5)
    score = round(min(1.0, best * (1.0 if len(candidates) > 1 else 0.9)), 2)

    return LocatorBundle(
        target_id=target_id,
        frame_path=tuple(FrameRef(name=part) for part in node.frame_path),
        candidates=tuple(candidates),
        recorded=RecordedNode(
            role=node.role,
            # A data-named control's snapshot records the shape, for the same
            # reason the candidate does: this is persisted in the artifact.
            name=value_shape(name) if name_is_data else name,
            value_shape=value_shape(node.value) if node.value and not node.sensitive else None,
            states=dict(node.states),
            bbox=(node.bbox.nx, node.bbox.ny, node.bbox.nw, node.bbox.nh),
            # Scrubbed for the same reason the candidates are. A snapshot of a
            # results row would carry a customer's name and branch into the
            # artifact, and would report drift on every run besides.
            anchors=_snapshot_anchors(node, values, data_row=for_extraction,
                                      data=data_texts or set()),
        ),
        stability_score=score,
        notes=" ".join(f"{r}." for r in reasons),
    )


_LABEL_WORD = re.compile(r"^[A-Za-z][A-Za-z /&-]{2,}$")
_HAS_DIGIT = re.compile(r"\d")


def _looks_like_a_label(text: str) -> bool:
    """Column headers name a column; they do not contain this row's values."""
    stripped = text.strip()
    return bool(stripped) and not _HAS_DIGIT.search(stripped) and any(
        c.isalpha() for c in stripped)


def _row_label_word(row_text: str) -> str | None:
    """The most label-like word in a grid row.

    In a row reading "100045218  Savings  $4,210.75  Open", the account number
    and the balance are this member's data; "Savings" is the account type, which
    is what a person would say to identify the row. Picking the alphabetic token
    is a heuristic, and it is the same one a human uses.
    """
    for token in row_text.split("  "):
        token = token.strip()
        if _LABEL_WORD.fullmatch(token) and token.lower() not in {"open", "active", "closed"}:
            return token
    for token in row_text.split():
        if _LABEL_WORD.fullmatch(token) and token.lower() not in {"open", "active", "closed"}:
            return token
    return None


def _grid_candidate(node: UiNode, data: set[str]) -> AnchorRelative | None:
    """A two-dimensional read: this column, that row.

    Only valid when the column header is a real header. On a two-column form
    laid out as a table -- which legacy apps do constantly -- the "header" above
    a value cell is whatever sat in the row before it, which on a confirmation
    screen is the account number just issued. Anchoring on that produces a
    locator that resolves only while the account number stays what it was on
    the day of recording, and it puts a customer's account number in the
    artifact. Both are reasons to refuse it.
    """
    header = node.anchors.col_header
    label = _row_label_word(node.anchors.row_text or "")
    if not header or not label:
        return None
    if header.strip() in data or not _looks_like_a_label(header):
        return None
    return AnchorRelative(
        anchor=Anchor(text=header), relation="same_column", target_role=node.role,
        scope=SectionScope(anchor=Anchor(pattern=f"(?i){re.escape(label)}"),
                           relation="within_row"),
    )


def _scrub(value: str, params: dict[str, str], data: set[str]) -> str:
    """Remove this run's data from an anchor string.

    By substring, not by equality: a panel header reads "Open Sub-Account --
    Dana Whitfield (12345)", which is not equal to any single value but contains
    two of them.
    """
    out = value
    for name, raw in params.items():
        if raw and len(raw) > 1:
            out = out.replace(raw, "{" + name + "}")
    for secret in sorted(data, key=len, reverse=True):
        if len(secret) > 3:
            out = out.replace(secret, "<redacted>")
    return out


def _snapshot_anchors(node: UiNode, params: dict[str, str], *,
                      data_row: bool = False,
                      data: set[str] | None = None) -> dict[str, str]:
    # An extraction target sits in a row of values by definition -- that is what
    # makes it worth extracting -- so its row context is this customer's data
    # whether or not a parameter value happens to appear in it.
    data_row = data_row or _is_data_row(node, params)
    out: dict[str, str] = {}
    for key, value in node.anchors.model_dump().items():
        if not value:
            continue
        if data_row and key in ("row_label", "row_text"):
            continue
        if _run_varying(value, params):
            continue
        out[key] = _scrub(value, params, data or set())
    return out


def _unique_id(candidate: str, used: set[str]) -> str:
    if candidate not in used:
        used.add(candidate)
        return candidate
    n = 2
    while f"{candidate}_{n}" in used:
        n += 1
    used.add(f"{candidate}_{n}")
    return f"{candidate}_{n}"


def _target_id(step: DiscoveryStep, used: set[str], params: dict[str, str] | None = None,
               *, for_extraction: bool = False) -> str:
    """A stable, readable address -- this is what a tenant override keys on.

    Derived from the node's name, unless the name is this run's data: a target
    id of `12345_link` would make a tenant override key on one customer.
    """
    node = step.node
    values = params or {}
    name = "" if (for_extraction or _run_varying(node.name, values)) else node.name
    row = "" if _is_data_row(node, values) else node.anchors.row_label
    base = (normalize_name(name) or normalize_name(row)
            or normalize_name(node.anchors.section_label) or node.role)
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_") or node.role
    suffix = {"textbox": "input", "combobox": "select", "button": "button",
              "link": "link", "cell": "cell"}.get(node.role, node.role)
    candidate = f"{base}_{suffix}" if not base.endswith(suffix) else base
    if candidate not in used:
        used.add(candidate)
        return candidate
    n = 2
    while f"{candidate}_{n}" in used:
        n += 1
    used.add(f"{candidate}_{n}")
    return f"{candidate}_{n}"


# --------------------------------------------------------------------------
# checkpoints -- synthesized from what actually changed
# --------------------------------------------------------------------------

def synthesize_checkpoint(step: DiscoveryStep, *, content_frame: tuple[str, ...],
                          base_url: str) -> Condition | None:
    """What proves this step worked, derived from the pre/post observation delta.

    A checkpoint asserts something that became true *because* of the action, so
    it is built from the difference rather than from the end state: anything
    already on screen beforehand proves nothing about the click.

    `base_url` is required rather than optional because leaving it out is the
    bug it exists to prevent. A URL checkpoint built from the observed address
    keeps whatever prefix that address had -- here `/t/demo-cu` -- and an
    artifact carrying its recording tenant inside a checkpoint is pinned to that
    tenant no matter what `binding` claims. It replays on the institution it was
    learned from and fails on every sibling, which is Invariant 9 broken from
    the inside. A default of `""` would silently reintroduce exactly that, so
    there is none.
    """
    if step.pre is None or step.post is None or not step.ok:
        return None
    if step.action_type in (ActionType.FILL, ActionType.SELECT_OPTION):
        # Typing into a field changes the field, not the screen. Asserting the
        # value we just typed would assert our own action back to ourselves.
        return None

    frame_key = "/".join(content_frame)
    before_url = step.pre.frame_urls.get(frame_key, step.pre.url)
    after_url = step.post.frame_urls.get(frame_key, step.post.url)

    conditions: list[Condition] = []

    if after_url and after_url != before_url:
        path = _route_of(after_url, base_url)
        # Numeric path segments are this run's data, not the shape of the route.
        pattern = re.sub(r"/\d+", r"/\\d+", re.escape(path).replace("\\/", "/"))
        conditions.append(UrlCondition(
            url=UrlMatch(matches=f"{pattern}$"),
            frame=tuple(FrameRef(name=p) for p in content_frame)))

    before = {(n.role, n.name) for n in step.pre.nodes if n.visible}
    new_nodes = [n for n in step.post.nodes
                 if n.visible and (n.role, n.name) not in before and n.name]
    # A heading, status or alert that appeared is the app telling us where we
    # are. Prefer those over an arbitrary new cell of data.
    for role in ("heading", "status", "alert", "button"):
        landmark = next((n for n in new_nodes if n.role == role and len(n.name) < 60), None)
        if landmark is not None:
            conditions.append(ElementCondition(
                element=ElementMatch(role=landmark.role, name=landmark.name), exists=True))
            break

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    # Either signal is sufficient: a URL that did not change on a framed app is
    # normal, and so is a heading the tenant renamed.
    return AnyCondition(any=tuple(conditions))


# --------------------------------------------------------------------------
# the recorder
# --------------------------------------------------------------------------

_RECORDABLE = {"click", "fill", "select_option", "navigate", "press_key"}


def _drop_superseded(steps: list[DiscoveryStep]) -> list[DiscoveryStep]:
    """Drop the explicit backtracks -- Part 5.1.

    A model exploring a lookup flow will often search for a value it expects to
    fail, to see how the application says "no such member", and then search
    again for the real one. Both searches are how it *found* the way; only the
    second is the way. Recorded verbatim, the capability replays the failed
    search first and returns MEMBER_NOT_FOUND before it ever reaches the answer.

    The signal is a parameter being filled more than once: everything from its
    first fill up to its last is a superseded attempt. Steps that carry another
    parameter's only value are kept regardless -- a multi-field form fills three
    different fields once each, and none of that is a backtrack.
    """
    fills: dict[str, list[int]] = {}
    for i, step in enumerate(steps):
        source = step.param_name if step.is_param_candidate else ""
        if source:
            fills.setdefault(source, []).append(i)

    last_fill_of = {name: idx[-1] for name, idx in fills.items()}
    protected = set(last_fill_of.values())

    superseded: set[int] = set()
    for name, indices in fills.items():
        if len(indices) < 2:
            continue
        for i in range(indices[0], indices[-1]):
            if i not in protected:
                superseded.add(i)

    for i in sorted(superseded):
        steps[i].pruned = True
        steps[i].pruned_because = "superseded: this parameter was filled again later"

    return [s for i, s in enumerate(steps) if i not in superseded]


def record(
    run: DiscoveryRun,
    *,
    capability_id: str,
    product: ProductRef,
    app_profile_ref: str,
    version: str = "1.0.0",
    title: str = "",
    content_frame: tuple[str, ...] = ("content",),
    label_variants: dict[str, list[str]] | None = None,
    fingerprints: dict[str, str] | None = None,
) -> CapabilityArtifact:
    """Transcript in, sealed draft artifact out."""
    if not run.succeeded:
        raise RecorderRefusal(
            f"refusing to record a run that ended '{run.status}': {run.stop_reason}. "
            f"An artifact recorded from a flow that never reached its goal would replay "
            f"the same failure deterministically."
        )

    params = run.params_used()
    data = _data_texts(run, params)
    kept = _drop_superseded([s for s in run.kept_steps if s.tool in _RECORDABLE])
    if not kept:
        raise RecorderRefusal("the run reached its goal without performing any action")

    used_ids: set[str] = set()
    steps: list[Step] = []
    inputs: dict[str, ParamSpec] = {}
    bundles: dict[int, LocatorBundle] = {}

    for position, step in enumerate(kept, start=1):
        bundle = None
        if step.node is not None and step.pre is not None:
            bundle = derive_bundle(step.node, step.pre,
                                   target_id=_target_id(step, used_ids, params),
                                   label_variants=label_variants, params=params,
                                   data_texts=data)
            bundles[step.index] = bundle

        value_source = None
        if step.value is not None and step.action_type in (ActionType.FILL,
                                                           ActionType.SELECT_OPTION):
            value_source = _as_value_source(step, run, params)
            if value_source.param:
                volatile = _looks_volatile(step.value or "")
                inputs.setdefault(value_source.param, ParamSpec(
                    type=ParamType.DATE if volatile else ParamType.STRING,
                    description=(f"Recorded as {step.value!r} on the day of discovery. "
                                 f"Supplied to: {step.reason}") if volatile
                                else f"Supplied to: {step.reason}",
                    pattern=value_shape(step.value) or None,
                    example=None,
                    volatile=volatile,
                    # The recorded value survives as a DEFAULT, not as a literal.
                    # A caller that knows better overrides it; one that does not
                    # gets a flow that runs, and a schema that says the value is
                    # stale by construction.
                    default=step.value if volatile else None,
                ))

        url_template = None
        if step.action_type is ActionType.NAVIGATE and step.url:
            url_template = _templatize(step.url, run.base_url, params)

        steps.append(Step(
            id=f"s{position}",
            intent=step.reason or f"{step.tool} {step.node.describe() if step.node else ''}".strip(),
            action=StepAction(
                type=step.action_type,
                target=bundle,
                value_from=value_source,
                url_template=url_template,
                key=_key_for(step),
            ),
            checkpoint=synthesize_checkpoint(step, content_frame=content_frame,
                                             base_url=run.base_url),
            risk=step.risk,
            requires_confirmation=step.risk is RiskTier.SUBMIT_IRREVERSIBLE,
        ))

    last_step_id = steps[-1].id

    outputs: dict[str, OutputSpec] = {}
    extractions: list[Extraction] = []
    for declared in run.extractions:
        observation = _observation_at(run, declared.at_step)
        bundle = derive_bundle(declared.node, observation,
                               target_id=_unique_id(
                                   f"{declared.output_name}_{declared.node.role}", used_ids),
                               label_variants=label_variants, params=params,
                               data_texts=data, for_extraction=True)
        parse = _parse_for(declared.node)
        outputs[declared.output_name] = OutputSpec(
            type=ParamType.MONEY if parse and parse.type == "money" else ParamType.STRING,
            description=declared.reason or f"Read from {declared.node.describe()}",
        )
        extractions.append(Extraction(
            output=declared.output_name, target=bundle, source="name", parse=parse))

    outcomes = []
    for declared in run.outcomes:
        detect: Condition
        if declared.observed_node is not None and declared.observed_node.name:
            detect = ElementCondition(
                element=ElementMatch(
                    role=declared.observed_node.role,
                    name_matches=f"(?i){re.escape(declared.observed_node.name[:60])}"),
                exists=True)
        else:
            detect = ElementCondition(
                element=ElementMatch(role="alert", name_matches=f"(?i){re.escape(declared.hint[:40])}"),
                exists=True)
        outcomes.append(OutcomeSpec(
            code=declared.code, description=declared.hint, detect=detect, after_step=None))

    success = _success_spec(run, steps, content_frame, params, data)

    # A reversible submit does not make a capability a writer: running a search
    # is a submit, and classifying every lookup as "writes" would make the tier
    # useless for the decision it exists to inform. Only an irreversible step
    # raises the capability's tier.
    irreversible = any(s.risk is RiskTier.SUBMIT_IRREVERSIBLE for s in steps)
    risk_tier = (CapabilityRisk.WRITES_IRREVERSIBLE if irreversible
                 else CapabilityRisk.READ_ONLY)

    goal = _generalize(run.goal, params, data)
    artifact = CapabilityArtifact(
        capability=CapabilityMeta(
            id=capability_id, version=version,
            title=title or _title_from(goal),
            description=goal,
            risk_tier=risk_tier,
        ),
        binding=Binding(
            product=product, app_profile_ref=app_profile_ref,
            recorded_against=RecordedAgainst(
                tenant=run.tenant, surface_fingerprint=fingerprints or {}),
        ),
        inputs=inputs,
        outputs=outputs,
        steps=tuple(steps),
        extractions=tuple(extractions),
        outcomes=tuple(outcomes),
        success=success,
        provenance=Provenance(
            recorded_from_run=run.run_id,
            model=run.model,
            discovery_goal=goal,
            steps_observed=len(run.steps),
            steps_pruned=len(run.steps) - len(kept),
        ),
    )
    return artifact.seal()


def _key_for(step: DiscoveryStep) -> str | None:
    """`press_key` carries its key in the transcript's value slot."""
    return step.value if step.action_type is ActionType.PRESS_KEY else None


def _data_texts(run: DiscoveryRun, params: dict[str, str]) -> set[str]:
    """Values the application displayed that belong to one customer.

    A node in a row containing a parameter value is a result row, so its text is
    that customer's data; so is anything the profile classified as sensitive.
    """
    found: set[str] = set()

    # Every row an extraction reads from is a row of values -- that is what made
    # it worth extracting. The balance's own row holds the account number too,
    # and neither is a parameter, so nothing else would mark them as data.
    extracted_rows = {e.node.anchors.row_text for e in run.extractions if e.node.anchors.row_text}
    for declared in run.extractions:
        for raw in (declared.node.name, declared.node.value):
            if raw and len(raw.strip()) > 3:
                found.add(raw.strip())

    for step in run.steps:
        for observation in (step.pre, step.post):
            if observation is None:
                continue
            for node in observation.nodes:
                in_extracted_row = bool(node.anchors.row_text) and \
                    node.anchors.row_text in extracted_rows
                if not (node.sensitive or in_extracted_row or _is_data_row(node, params)):
                    continue
                for raw in (node.name, node.value):
                    if raw and len(raw.strip()) > 3:
                        found.add(raw.strip())
    return found


def _generalize(text: str, params: dict[str, str], data: set[str] | None = None) -> str:
    """Make free text safe and reusable.

    Two things happen to prose on its way into an artifact. Parameter values
    become their parameter names -- otherwise a capability described as "look up
    member 12345" documents itself as being about one customer forever, and the
    value R-M4-1 worked to keep out of the steps walks back in through the
    description. And data the application displayed is removed outright: the
    model's own `finish` message is written while it is looking at a member's
    record, so it says things like "member 12345 (Dana Whitfield) shows the
    Savings account 100045218". None of that belongs in a reusable capability.
    """
    out = text
    for name, value in params.items():
        if value and len(value) > 1:
            out = out.replace(value, "{" + name + "}")
    # Longest first, so a value contained inside another is not half-replaced.
    for value in sorted(data or (), key=len, reverse=True):
        out = out.replace(value, "<redacted>")
    return out


def _title_from(goal: str) -> str:
    """A short label for the capability, from the goal it was recorded for.

    The first SENTENCE, not the first eighty characters. Slicing prose mid-word
    produced titles like "...so you can see and declare how th", and the title
    is not decoration -- it is the first thing a reviewer reads in the catalog
    and part of what a calling agent is shown.

    *Known limit, stated in REPORT §7:* the description is still the discovery
    goal verbatim, so a goal that contains exploratory instructions ("search for
    a member that does not exist, so you can see how it reports that") describes
    the RECORDING rather than the capability. Naming a capability well from its
    own transcript is a job for the authoring review pass, not for a slice.
    """
    text = " ".join((goal or "").split())
    if not text:
        return ""
    first = re.split(r"(?<=[.!?])\s+", text)[0]
    if len(first) <= 100:
        return first
    # A single very long sentence: cut on a word boundary and say that it was.
    return first[:97].rsplit(" ", 1)[0] + "..."


def _route_of(url: str, base_url: str) -> str:
    """The part of a URL that is the ROUTE, with the deployment stripped off.

    The action side of the recorder has always done this -- `_templatize` turns
    a recorded address into `{base_url}/members/{member_id}`. The condition side
    did not, so checkpoints kept `/t/demo-cu` and pinned the flow to one
    institution. Same canonicalization, same reason, one function apart.

    Falls back to stripping scheme and host when the observed URL does not sit
    under the recorded base -- a redirect to a login host, say. Better a route
    that is merely over-specific than one built from a string this function does
    not understand.
    """
    clean = url.split("?")[0].split("#")[0]
    base = (base_url or "").rstrip("/")
    if base and clean.startswith(base):
        return clean[len(base):] or "/"
    return re.sub(r"^https?://[^/]+", "", clean)


def _templatize(url: str, base_url: str, params: dict[str, str]) -> str:
    """Replace the host with `{base_url}` and any parameter value with its name.

    A recorded URL containing this run's member id is a capability that can only
    ever look up that member.
    """
    out = url.replace(base_url.rstrip("/"), "{base_url}")
    for name, value in params.items():
        if value:
            out = out.replace(value, "{" + name + "}")
    return out


def _observation_at(run: DiscoveryRun, step_index: int) -> Observation:
    for step in run.steps:
        if step.index == step_index and step.post is not None:
            return step.post
    for step in reversed(run.steps):
        if step.post is not None:
            return step.post
    raise RecorderRefusal("the run captured no observations")


def _parse_for(node: UiNode) -> ParseSpec | None:
    text = node.name or node.value or ""
    if re.search(r"[$€£]\s?[\d,]+\.\d{2}", text) or re.fullmatch(r"[\d,]+\.\d{2}", text.strip()):
        return ParseSpec(type="money", locale="en_US")
    if re.fullmatch(r"\d+", text.strip()):
        return ParseSpec(type="string")
    return None


def _success_spec(run: DiscoveryRun, steps: list[Step], content_frame,
                  params: dict[str, str], data: set[str]) -> SuccessSpec:
    """What proves the goal was reached.

    The last step's checkpoint, reused: the model called `finish` while looking
    at a screen, and the condition that proved the step arrived there is the same
    condition that proves the capability succeeded. If the last step asserted
    nothing, the recording is not verifiable and is refused -- an artifact whose
    success condition is "we ran out of steps" is not a capability.
    """
    for step in reversed(steps):
        if step.checkpoint is not None:
            return SuccessSpec(
                checkpoint=step.checkpoint,
                description=_generalize(
                    run.finish_reason or "Reached the final screen of the flow.",
                    params, data),
            )
    raise RecorderRefusal(
        "no step produced an observable change, so there is nothing to assert as success. "
        "The flow cannot be verified and will not be recorded."
    )
