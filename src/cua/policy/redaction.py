"""Redaction at every egress.

The discovery agent is the egress that matters most: a member detail screen
carries an SSN and a date of birth, and the whole point of discovery is to look
at that screen. Without this module the first real LLM run would send regulated
data to a model, and the invariant would be a sentence in a README rather than a
property of the system.

Two mechanisms, in order of trustworthiness:

1. **Node rules from the app profile.** The institution's own screens declare
   which fields hold what ("the cell in the row labelled SSN"). This does not
   depend on a value happening to look recognizable, which is why it is the real
   mechanism.
2. **Regex detectors.** A net for values that appear somewhere nobody declared --
   free-text notes, an error message quoting an account number. Best-effort by
   construction, and described that way in the write-up rather than oversold.

Masking is applied to the *node*, not to a copy of the text, so everything
downstream that renders a node -- prompts, journals, the annotated screenshot --
inherits it from one decision.
"""

from __future__ import annotations

import re

from ..conditions.dsl import node_matches
from ..perception.model import Observation, UiNode, normalize_name
from ..profiles.schema import AppProfile

# Named detectors, enabled per profile. Deliberately conservative: a pattern
# that fires on ordinary text trains people to ignore redaction.
DETECTORS: dict[str, re.Pattern] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "card_luhn": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "email": re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "phone": re.compile(r"\b(?:\+1[ -]?)?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}\b"),
    "account_number": re.compile(r"\b\d{9,12}\b"),
}

REDACTED = "<redacted>"


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 13:
        return False
    checksum = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        checksum += n
    return checksum % 10 == 0


def scrub_text(text: str, detectors: tuple[str, ...]) -> str:
    """Replace detector hits in free text. Used for journal messages and any
    string that reaches a prompt without being a node value."""
    out = text
    for name in detectors:
        pattern = DETECTORS.get(name)
        if pattern is None:
            continue
        if name == "card_luhn":
            # Without the checksum this fires on every long digit run -- order
            # numbers, timestamps, account ids -- and redaction that cries wolf
            # gets switched off.
            out = pattern.sub(lambda m: REDACTED if _luhn_ok(m.group(0)) else m.group(0), out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


def classify(node: UiNode, profile: AppProfile) -> tuple[str, tuple[str, ...]] | None:
    """The profile's classification for this node, if any: (label, mask targets).

    A rule scoped `within_row` matches BOTH cells of a label/value row -- the
    value because its row label is the anchor, and the label because the row
    text contains it. Masking both takes the field's name away with its
    contents, so a member record rendered `<redacted> <redacted>` three times
    over and an operator could no longer tell which row was the SSN. That
    directly contradicts what this module promises: the node keeps its role and
    its own label context, because an agent still has to see that an SSN field
    exists in order to leave it alone.

    So a node whose own name IS the anchor is the label, not the data.
    """
    for rule in profile.sensitivity.node_rules:
        if not node_matches(node, rule.match):
            continue
        scope = rule.match.scope
        if scope is not None and scope.anchor.text:
            if normalize_name(node.name) == normalize_name(scope.anchor.text):
                continue
        return rule.classification, rule.mask
    return None


def apply_sensitivity(observation: Observation, profile: AppProfile,
                      known_secrets: set[str] | None = None) -> Observation:
    """Mark and mask every node the profile classifies as regulated.

    Two passes, and the second one is the one that is easy to leave out.

    On a legacy screen the data IS the accessible name -- a table cell's name is
    "521-84-9077", not "SSN" -- so masking a node means replacing its *name*.
    Marking it `sensitive` and scrubbing only detector-matched text is not
    enough: a date of birth matches no detector, and the rule that classified it
    would have masked nothing at all.

    And a regulated value does not only appear on its own node. `row_label` is
    "the other cell in this row", so the label cell sitting beside an SSN value
    carries that SSN as its structural context. Masking the value node alone
    leaves the number in its neighbour's anchors, which are rendered straight
    into the prompt. The second pass scrubs the collected values out of every
    node's anchors.

    The node keeps its role and its own label context -- an agent still has to
    see that an SSN field exists in order to leave it alone.

    `known_secrets` makes the second pass outlive the screen it learned from,
    and it is not an optimisation. A value is regulated because of what it is,
    not because of which screen it is on -- and this application proves the
    point: the sub-account form's pane header reads "Open Sub-Account -- Dana
    Whitfield (12345)", which exists ONLY as `anchors.section_label` on every
    node in that pane. No node carries it, so no node rule can reach it, and a
    per-observation secret set is empty there because nothing on that screen was
    masked. Carrying forward what was masked on the member record two steps
    earlier is what closes it.

    Pass the same set for the length of one run. Callers that pass nothing get
    exactly the previous behaviour.
    """
    detectors = profile.sensitivity.text_detectors

    # Pass 1: decide what is regulated, and collect the literal values so they
    # can be chased out of everywhere else they appear.
    masked: list[bool] = []
    secrets: set[str] = known_secrets if known_secrets is not None else set()

    for node in observation.nodes:
        hit = classify(node, profile)
        by_rule = bool(hit and "text" in hit[1])
        by_detector = bool(detectors) and (
            scrub_text(node.value or "", detectors) != (node.value or "")
            or scrub_text(node.name, detectors) != node.name
        )
        is_masked = by_rule or by_detector
        masked.append(is_masked)
        if is_masked:
            for raw in (node.name, node.value):
                if raw and len(raw.strip()) > 3:
                    secrets.add(raw.strip())

    # Pass 2: rewrite.
    changed: list[UiNode] = []
    for node, is_masked in zip(observation.nodes, masked):
        anchors = node.anchors
        scrubbed = {
            field: _scrub_anchor(value, secrets, detectors)
            for field, value in anchors.model_dump().items()
        }
        update: dict = {}
        if scrubbed != anchors.model_dump():
            update["anchors"] = anchors.model_copy(update=scrubbed)
        if is_masked:
            update["sensitive"] = True
            update["name"] = REDACTED if node.name.strip() else node.name
            update["value"] = REDACTED if node.value else node.value
        changed.append(node.model_copy(update=update) if update else node)

    return observation.model_copy(update={"nodes": tuple(changed)})


def _scrub_anchor(value: str, secrets: set[str], detectors: tuple[str, ...]) -> str:
    if not value:
        return value
    out = value
    for secret in secrets:
        if secret in out:
            out = out.replace(secret, REDACTED)
    return scrub_text(out, detectors) if detectors else out


def screenshot_masks(observation: Observation, profile: AppProfile, *,
                     persisted: bool = False) -> list[tuple[float, float, float, float]]:
    """Page-absolute boxes to paint over before a screenshot leaves this process.

    Separate from `apply_sensitivity` because the two masks differ: an account
    number is masked in text but left visible on screen, since an operator
    taking over needs to read it off the record in front of them.

    `persisted` is the second half of that sentence, and it is the half that was
    missing. A live view is transient, served to one operator who already holds
    the lease on the session. A file written into an evidence pack outlives the
    incident, gets attached to tickets and copied between environments -- so
    when the bytes are going to disk, EVERY node the profile classifies is
    painted over, whatever its `mask` list says. The per-rule list decides what
    an operator may see; it does not decide what may be archived.

    This keeps both design intents instead of trading one against the other,
    and it needs no new mask vocabulary: "screenshot" still means "hide this
    even from the operator", and persistence is simply stricter than that.
    """
    boxes = []
    for node in observation.nodes:
        hit = classify(node, profile)
        if hit and (persisted or "screenshot" in hit[1]):
            boxes.append((node.bbox.x, node.bbox.y, node.bbox.w, node.bbox.h))
    return boxes


# --------------------------------------------------------------------------
# outputs -- the egress the first pass of this module did not cover
# --------------------------------------------------------------------------

def redaction_for(output: str, spec, profile: AppProfile) -> str:
    """How `output` should appear in logs and evidence.

    Two sources, artifact first: `OutputSpec.redact` is a decision somebody made
    about this capability, and the profile's `output_redaction_defaults` is the
    institution's blanket rule for a name like "ssn" or "account_number". The
    artifact wins because it is the more specific statement, and the profile
    fills in for every capability whose author did not think about it -- which
    is the case the default exists for.

    The profile's keys match on NAME TOKENS, not on the whole string. Exact
    matching looked right and was brittle in the way that matters: a rule keyed
    `account_number` did nothing for an output called `new_account_number`, and
    the number went to disk in full. A blanket rule that only covers the one
    spelling somebody thought of is not a blanket rule -- and the failure is
    silent, because an output nobody wrote a key for looks exactly like an
    output nobody needed one for.
    """
    declared = getattr(spec, "redact", None)
    mode = getattr(declared, "value", declared) or "none"
    if mode != "none":
        return mode
    return _default_mode(output, profile.sensitivity.output_redaction_defaults)


def _default_mode(output: str, defaults: dict[str, str]) -> str:
    """The most specific profile default whose tokens appear in `output`.

    `account_number` covers `new_account_number` and `account_number_masked`;
    it does not cover `account` or `number` alone, and it does not fire on
    `accountnumber`, because tokens are matched as tokens rather than as
    substrings. Longest key wins, so a `primary_account_number` rule beats the
    generic one where both are declared.
    """
    if not defaults:
        return "none"
    if output in defaults:
        return defaults[output]

    tokens = output.split("_")
    best: tuple[int, str] = (0, "none")
    for key, mode in defaults.items():
        want = key.split("_")
        n = len(want)
        if n and any(tokens[i:i + n] == want for i in range(len(tokens) - n + 1)):
            if n > best[0]:
                best = (n, mode)
    return best[1]


def redact_output(value, mode: str):
    """Render one extracted value for a journal or an evidence file.

    NOT for the value returned to the caller. An agent that asked for an account
    number needs the account number; the boundary redaction defends is
    persistence, which is what `OutputSpec.redact` says in as many words. Two
    different renderings of one value is the whole point, and collapsing them
    would either break callers or leak into evidence.
    """
    if mode in ("none", "", None):
        return value
    if mode == "drop":
        return REDACTED
    text = str(value)
    if mode == "last4":
        tail = text[-4:] if len(text) > 4 else text
        return f"{'*' * max(0, len(text) - 4)}{tail}"
    if mode == "mask":
        return REDACTED
    # An unknown mode is a typo in a committed file. Failing closed costs a
    # legible log line; failing open costs the thing this module exists for.
    return REDACTED


def screenshot_allowed(url: str, profile: AppProfile) -> bool:
    """False when the profile forbids capturing this screen at all.

    `sensitivity.never_screenshot` holds URL-path regexes, because "do not
    photograph the SSN maintenance screen" is a statement about a screen, and a
    screen is addressed by where it lives. Masking boxes are the finer
    instrument; this is the one for a page where the answer to "which parts are
    regulated" is "all of it".
    """
    if not profile.sensitivity.never_screenshot:
        return True
    path = url or ""
    return not any(re.search(pattern, path) for pattern in profile.sensitivity.never_screenshot)
