"""Condition DSL -- schema only. Evaluation lands with the replay engine (M3).

Checkpoints, business-outcome detectors, recovery triggers, hard-failure
detectors and login-success assertions are all the same thing: a predicate over
an `Observation`, expressed as **data in the artifact** rather than code.

That is what makes replay deterministic. A checkpoint is not a callback the
engine has to trust; it is a declared assertion a human can read in review, a
tenant can override, and the engine can evaluate with no model in the loop.

Everything reduces to predicates over `UiNode`s plus the surface's URL, so a
desktop adapter evaluates the same conditions unchanged -- except URL ones,
which is why `SurfaceCapabilities.has_url` exists and the evaluator must refuse
rather than silently pass them.

    {"all": [{"element": {"role": "heading", "name_matches": "(?i)account opened"},
              "exists": true},
             {"url": {"matches": "/subaccount/confirm"}}]}
"""

from __future__ import annotations

from typing import Annotated, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..locators.model import FrameRef, SectionScope


class ElementMatch(BaseModel):
    """Which node(s) a condition is talking about.

    Same vocabulary as a locator candidate, deliberately: if you can target a
    control you can assert on it, without a second matching language.
    """

    model_config = ConfigDict(frozen=True)

    role: str | None = None
    name: str | None = None
    name_matches: str | None = None
    value: str | None = None
    value_matches: str | None = None
    scope: SectionScope | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> "ElementMatch":
        if not any(
            v is not None
            for v in (self.role, self.name, self.name_matches, self.value,
                      self.value_matches, self.scope, self.enabled)
        ):
            raise ValueError("element match needs at least one criterion")
        return self


class UrlMatch(BaseModel):
    model_config = ConfigDict(frozen=True)
    matches: str
    """Regex, searched against the URL."""


class ElementCondition(BaseModel):
    """Assert a node is present (or absent), optionally with matching text."""

    model_config = ConfigDict(frozen=True)

    element: ElementMatch
    exists: bool = True
    text_matches: str | None = None
    """When set, the matched node's name/value must also match this regex.
    Implies exists."""

    frame: tuple[FrameRef, ...] | None = None
    """Restrict to a frame. Unset means any frame -- deliberate, because a
    checkpoint usually cares that something is on screen, not where."""

    min_count: int = 1
    """Lets a checkpoint assert "at least N rows" without a second operator."""


class UrlCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: UrlMatch
    frame: tuple[FrameRef, ...] | None = None
    """Which document's URL. Unset means the top-level page. This matters in a
    frameset: the page URL is the shell, and the flow's real location is the
    content frame's URL."""


class TextCondition(BaseModel):
    """Regex over visible text. The blunt instrument -- used when an app reports
    something as prose with no useful role, which legacy apps do constantly."""

    model_config = ConfigDict(frozen=True)

    text_present: str
    frame: tuple[FrameRef, ...] | None = None


class AllCondition(BaseModel):
    model_config = ConfigDict(frozen=True)
    all: tuple["Condition", ...] = Field(min_length=1)


class AnyCondition(BaseModel):
    model_config = ConfigDict(frozen=True)
    any: tuple["Condition", ...] = Field(min_length=1)


class NotCondition(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    not_: "Condition" = Field(alias="not")


# Union order is left-to-right and each member has a distinct required key, so
# the shape in JSON/YAML is self-describing without a `type` discriminator --
# which keeps authored profiles readable.
Condition = Annotated[
    Union[ElementCondition, UrlCondition, TextCondition, AllCondition, AnyCondition, NotCondition],
    Field(union_mode="left_to_right"),
]

AllCondition.model_rebuild()
AnyCondition.model_rebuild()
NotCondition.model_rebuild()


def describe(cond: "Condition") -> str:
    """Human-readable form, for journals and failure messages.

    A checkpoint failure has to say what was expected in words a reviewer can
    act on -- "expected heading matching (?i)account opened" beats a JSON dump.
    """
    if isinstance(cond, ElementCondition):
        e = cond.element
        bits = []
        if e.role:
            bits.append(e.role)
        if e.name:
            bits.append(f'named "{e.name}"')
        if e.name_matches:
            bits.append(f"matching /{e.name_matches}/")
        if e.value_matches:
            bits.append(f"value matching /{e.value_matches}/")
        if e.scope:
            bits.append(f"{e.scope.relation} {e.scope.anchor.text or e.scope.anchor.pattern!r}")
        what = " ".join(bits) or "element"
        verb = "present" if cond.exists else "absent"
        extra = f" with text matching /{cond.text_matches}/" if cond.text_matches else ""
        count = f" (at least {cond.min_count})" if cond.min_count > 1 else ""
        return f"{what} is {verb}{extra}{count}"
    if isinstance(cond, UrlCondition):
        where = f" in frame {'/'.join(f.name or '?' for f in cond.frame)}" if cond.frame else ""
        return f"url matches /{cond.url.matches}/{where}"
    if isinstance(cond, TextCondition):
        return f"text matching /{cond.text_present}/ is present"
    if isinstance(cond, AllCondition):
        return "(" + " AND ".join(describe(c) for c in cond.all) + ")"
    if isinstance(cond, AnyCondition):
        return "(" + " OR ".join(describe(c) for c in cond.any) + ")"
    if isinstance(cond, NotCondition):
        return f"NOT {describe(cond.not_)}"
    return str(cond)
