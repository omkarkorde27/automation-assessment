"""Draw the observation onto the screenshot, and mask what must not be seen.

Three callers, one renderer: the discovery prompt, the evidence pack, and the
operator console. They want the same picture for the same reason -- whoever is
looking should see what a person would see, plus the numbering used to point at
things -- and it lives under `observability` rather than under `discovery`
because the console must be able to render a screen without importing anything
that can reach a model.

The model is shown the same screen a person would see, plus the numbering it
uses to point at things. Two boxes on one image keep the semantic layer and the
visual layer in agreement -- if node `content:n4` is drawn around the wrong
control, that is visible immediately rather than three steps later.

Masking happens **here**, before the image is attached to a message, because
this is the last point at which the pixels are still ours. A redaction applied
only to the text listing would be undone by the screenshot sitting next to it.
"""

from __future__ import annotations

import io

from ..perception.model import Observation

_INTERACTIVE = {"textbox", "button", "link", "combobox", "checkbox", "radio"}


def annotate(
    png: bytes,
    observation: Observation,
    *,
    masks: list[tuple[float, float, float, float]] | None = None,
    max_labels: int = 60,
) -> bytes:
    """Return a PNG with sensitive regions painted out and nodes labelled.

    Falls back to masking-only if anything goes wrong with the drawing: an
    unlabelled screenshot is a cosmetic loss, but an unmasked one is a data
    incident, so the mask is applied first and independently.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return png

    image = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(image)

    # 1. Mask first. If labelling fails after this, the image is still safe.
    for x, y, w, h in masks or []:
        if w <= 0 or h <= 0:
            continue
        draw.rectangle([x, y, x + w, y + h], fill=(20, 20, 20))
        draw.text((x + 3, y + 2), "REDACTED", fill=(255, 255, 255))

    safe = io.BytesIO()
    image.save(safe, format="PNG")

    try:
        labelled = 0
        for node in observation.nodes:
            if labelled >= max_labels or not node.visible:
                continue
            if node.role not in _INTERACTIVE:
                continue
            b = node.bbox
            if b.w <= 0 or b.h <= 0:
                continue
            draw.rectangle([b.x, b.y, b.x + b.w, b.y + b.h], outline=(220, 30, 30), width=2)
            tag = node.node_id.split(":")[-1]
            tw = 7 * len(tag) + 6
            draw.rectangle([b.x, max(0, b.y - 14), b.x + tw, b.y], fill=(220, 30, 30))
            draw.text((b.x + 3, max(0, b.y - 13)), tag, fill=(255, 255, 255))
            labelled += 1
    except Exception:
        return safe.getvalue()

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
