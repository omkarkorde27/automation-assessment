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
from ..perception.model import Observation, UiNode
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
    """The profile's classification for this node, if any: (label, mask targets)."""
    for rule in profile.sensitivity.node_rules:
        if node_matches(node, rule.match):
            return rule.classification, rule.mask
    return None


def apply_sensitivity(observation: Observation, profile: AppProfile) -> Observation:
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
    """
    detectors = profile.sensitivity.text_detectors

    # Pass 1: decide what is regulated, and collect the literal values so they
    # can be chased out of everywhere else they appear.
    masked: list[bool] = []
    secrets: set[str] = set()

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


def screenshot_masks(observation: Observation, profile: AppProfile) -> list[tuple[float, float, float, float]]:
    """Page-absolute boxes to paint over before a screenshot is shown to anyone.

    Separate from `apply_sensitivity` because the two masks differ: an account
    number is masked in text but left visible on screen, since an operator
    taking over needs to read it.
    """
    boxes = []
    for node in observation.nodes:
        hit = classify(node, profile)
        if hit and "screenshot" in hit[1]:
            boxes.append((node.bbox.x, node.bbox.y, node.bbox.w, node.bbox.h))
    return boxes
