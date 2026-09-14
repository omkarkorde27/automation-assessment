"""Cost accounting and the zero-API preflight.

Both exist because of the same lesson. The dead-end detector shipped with a bug
that fired on a working run, and it was found by a real Opus run against the
sub-account flow -- tens of thousands of input tokens to learn something a
scripted sequence proves in under a second. The control logic is decidable
without a model; only the model's judgement needs the model.
"""

from __future__ import annotations

import pytest

from cua.discovery.costs import PRICES, RunCost, Spend, price_for
from cua.discovery.dryrun import DryRunReport, check_tool_schemas, run_control_checks
from cua.profiles import ProfileRepository


# --------------------------------------------------------------------------
# cost accounting
# --------------------------------------------------------------------------

def test_the_two_calls_are_reported_separately():
    """A single total hides that the loop is the expensive part and the review
    is not -- which is the whole basis for using a smaller model on one of
    them."""
    cost = RunCost(agent=Spend("claude-opus-5", 100_000, 1_500),
                   reviewer=Spend("claude-haiku-4-5-20251001", 1_500, 300))
    report = cost.report()

    assert "discovery loop" in report and "authoring review" in report
    assert "claude-opus-5" in report and "haiku" in report
    assert cost.agent.usd > cost.reviewer.usd * 100


def test_a_smaller_reviewer_is_cheaper_by_the_ratio_of_its_prices():
    """Asserted against the price table rather than a hardcoded number, so the
    test does not quietly become a lie when a price changes."""
    tokens = (1_500, 300)
    opus = Spend("claude-opus-5", *tokens).usd
    haiku = Spend("claude-haiku-4-5-20251001", *tokens).usd

    oi, oo = PRICES["claude-opus-5"]
    hi, ho = PRICES["claude-haiku-4-5-20251001"]
    expected = ((tokens[0] / 1e6) * oi + (tokens[1] / 1e6) * oo) / \
               ((tokens[0] / 1e6) * hi + (tokens[1] / 1e6) * ho)

    assert opus / haiku == pytest.approx(expected)
    assert opus > haiku


def test_the_counterfactual_prices_the_same_tokens_on_another_model():
    """Comparing two real runs conflates price with how verbose each model was.
    The counterfactual holds the token count fixed so only price varies."""
    cost = RunCost(agent=Spend("claude-opus-5", 10, 10),
                   reviewer=Spend("claude-haiku-4-5-20251001", 1_343, 253))
    line = cost.counterfactual(reviewer_model="claude-opus-5")

    assert "claude-opus-5" in line and "15.0x" in line


def test_an_unknown_model_reports_no_price_rather_than_a_wrong_one():
    spend = Spend("some-model-we-do-not-price", 1_000, 100)
    assert spend.usd is None
    assert "no price on file" in spend.line("x")


def test_a_dated_model_id_still_finds_its_price():
    assert price_for("claude-haiku-4-5-20251001") == PRICES["claude-haiku-4-5-20251001"]


def test_every_report_states_that_prices_are_assumptions():
    """Token counts are measured; prices are not. A dollar figure that does not
    say which rates produced it is a number somebody will quote later."""
    report = RunCost(agent=Spend("claude-opus-5", 1, 1)).report()
    assert "measured" in report and "assumptions" in report


# --------------------------------------------------------------------------
# the preflight
# --------------------------------------------------------------------------

def test_the_schema_checks_need_no_browser_and_no_key():
    checks = check_tool_schemas()
    assert len(checks) == 3
    assert all(c.ok for c in checks), [c.name for c in checks if not c.ok]


def test_a_failing_check_makes_the_whole_report_fail():
    from cua.discovery.dryrun import Check

    report = DryRunReport(checks=[Check("a", True), Check("b", False, "why")])
    assert not report.ok
    assert "FAIL  b  -- why" in report.render()
    assert "1/2 control checks passed" in report.render()
    assert "$0.00" in report.render()


async def test_the_preflight_passes_against_the_live_fixture(live_server):
    """The same checks `cua discover` runs before spending anything."""
    resolved = ProfileRepository("profiles").resolve("demo-cu")
    surface = resolved.profile.surface.model_copy(
        update={"base_url": f"{live_server}/t/demo-cu"})
    profile = resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage, hash=resolved.hash,
    )

    report = await run_control_checks(profile)

    assert report.ok, report.render()
    names = [c.name for c in report.checks]
    assert "filling a form is not mistaken for a dead end" in names
    assert "a genuinely stuck agent is detected" in names
    assert "the irreversible guard is installed on the surface" in names
