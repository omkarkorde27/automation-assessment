"""Per-render element id churn.

Legacy enterprise apps (classic ASP.NET WebForms being the canonical offender)
emit generated control ids that change between renders and between versions.
This reproduces that on purpose: any locator strategy keyed on `id`, `name`, or
a CSS path built from them will resolve on the first load and fail on the next.

That is not a gimmick -- it is the single most common reason naive UI automation
rots in these environments, and the surface should punish it rather than let us
claim robustness we never demonstrated.
"""

import random


class IdChurn:
    """Callable that mints a fresh generated-looking id on every call."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._block = self._rng.randint(10, 99)
        self._n = 0

    def __call__(self, prefix: str = "ctl") -> str:
        self._n += 1
        return f"ctl00_ctl{self._block}_{prefix}{self._n:02d}"


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"
