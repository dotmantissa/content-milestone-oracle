"""
Borderline readings, deferral, re-checks and the contested state.

This is the mechanism the primitive exists for. A reading inside the tolerance
band is a statement that the contract cannot tell the two outcomes apart, so it
refuses to settle and schedules a re-read instead. Engagement metrics only
climb, so waiting is a fair tie-break: a genuine near miss resolves itself and a
stalled one runs out the sponsor's deadline.
"""

import json

from conftest import (
    DAY,
    GEN,
    GITHUB_URL,
    SIX_HOURS,
    github_page,
    set_scene,
    warp_by,
    warp_to,
)


def resolve(oracle, direct_vm, caller, mid, value, *, page_count=None, **kwargs):
    """Advance to the re-check window and resolve with a given reading."""
    set_scene(
        direct_vm,
        GITHUB_URL,
        github_page(page_count if page_count is not None else f"{value:,}"),
        value,
        **kwargs,
    )
    direct_vm.sender = caller
    return oracle.resolve_deferred(mid)


# ---------------------------------------------------------------------------
# Entering deferral
# ---------------------------------------------------------------------------


def test_a_borderline_reading_defers_instead_of_settling(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050
    )

    assert out["zone"] == "borderline"
    assert out["status"] == "deferred"
    assert out["defer_rounds"] == 1
    assert out["recheck_after"] == oracle.now_ts() + SIX_HOURS

    record = oracle.get_milestone(mid)
    assert record["status"] == "deferred"
    assert record["observed_value"] == 10_050
    assert record["payout_atto"] == 0
    assert record["claimed_at"] == oracle.now_ts()
    assert oracle.get_treasury()["escrowed_atto"] == 5 * GEN


def test_a_reading_exactly_on_the_threshold_is_borderline(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    """
    The band has to straddle the threshold. A band only on one side moves the
    knife edge rather than removing it, so hitting the threshold exactly is
    deliberately not a settlement.
    """
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,000"), value=10_000
    )
    assert out["zone"] == "borderline"
    assert out["decision_lines"]["certain_pass_at"] == 10_100


def test_the_defer_window_is_configurable(
    deploy_oracle, make_milestone, direct_alice, direct_bob, claim
):
    contract = deploy_oracle(defer_seconds=3_600)
    mid = make_milestone(contract, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        contract, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050
    )
    assert out["recheck_after"] == contract.now_ts() + 3_600


# ---------------------------------------------------------------------------
# The re-check window
# ---------------------------------------------------------------------------


def test_a_recheck_before_the_window_opens_is_refused(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)

    status = oracle.is_resolvable(mid)
    assert status["resolvable"] is False
    assert status["reason"] == "re-check window has not opened"

    set_scene(direct_vm, GITHUB_URL, github_page("10,200"), 10_200)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("re-check unlocks at"):
        oracle.resolve_deferred(mid)


def test_a_recheck_is_permissionless(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, direct_charlie, claim
):
    """
    Anyone may poke a stalled milestone. The outcome routes to the recorded
    parties, so a third party calling it can only help.
    """
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    warp_by(direct_vm, oracle, SIX_HOURS)
    assert oracle.is_resolvable(mid)["resolvable"] is True

    out = resolve(oracle, direct_vm, direct_charlie, mid, 10_240)
    assert out["status"] == "achieved"
    assert oracle.get_milestone(mid)["payout_atto"] == 5 * GEN


def test_a_recheck_that_clears_the_band_pays_the_creator(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    warp_by(direct_vm, oracle, SIX_HOURS)

    out = resolve(oracle, direct_vm, direct_bob, mid, 10_240)
    assert out["zone"] == "pass"
    assert out["status"] == "achieved"
    assert out["attempt"] == 2
    assert oracle.get_treasury()["escrowed_atto"] == 0
    assert oracle.get_treasury()["total_paid_atto"] == 5 * GEN


def test_a_recheck_that_drops_below_the_band_reopens_the_milestone(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    """
    Counts can fall when a platform reverses spam. The claim is released rather
    than failed outright, so the creator can try again before the deadline.
    """
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    warp_by(direct_vm, oracle, SIX_HOURS)

    out = resolve(oracle, direct_vm, direct_bob, mid, 9_400)
    assert out["zone"] == "fail"
    assert out["status"] == "open"
    # The borderline ladder belongs to the claim, so a released claim resets it
    # and the creator gets a full set of re-checks on the next attempt.
    assert oracle.get_milestone(mid)["defer_rounds"] == 0
    assert oracle.is_claimable(mid)["claimable"] is True


def test_a_still_borderline_recheck_schedules_the_next_one(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    warp_by(direct_vm, oracle, SIX_HOURS)

    out = resolve(oracle, direct_vm, direct_bob, mid, 10_060)
    assert out["status"] == "deferred"
    assert out["defer_rounds"] == 2
    assert out["recheck_after"] == oracle.now_ts() + SIX_HOURS


def test_resolving_a_milestone_that_is_not_deferred_is_refused(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    set_scene(direct_vm, GITHUB_URL, github_page("10,200"), 10_200)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("is open, nothing is deferred"):
        oracle.resolve_deferred(mid)
    assert oracle.is_resolvable(mid)["reason"] == "milestone is open"


def test_rechecks_are_blocked_while_paused(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    warp_by(direct_vm, oracle, SIX_HOURS)
    direct_vm.sender = direct_owner
    oracle.set_paused(True)
    set_scene(direct_vm, GITHUB_URL, github_page("10,240"), 10_240)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("contract is paused"):
        oracle.resolve_deferred(mid)


# ---------------------------------------------------------------------------
# Post-deadline resolution
# ---------------------------------------------------------------------------


def test_a_claim_filed_before_the_deadline_resolves_after_it(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    """
    The documented asymmetry. A borderline reading is a positive statement that
    the count was already within tolerance of the threshold when the claim was
    filed, so the re-check that settles it is allowed to land after the
    deadline. The resolution grace window bounds how long that holds.
    """
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=3_600
    )
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    deadline = oracle.get_milestone(mid)["deadline_ts"]

    warp_by(direct_vm, oracle, SIX_HOURS)
    assert oracle.now_ts() > deadline
    assert oracle.is_resolvable(mid)["resolvable"] is True

    out = resolve(oracle, direct_vm, direct_bob, mid, 10_240)
    assert out["status"] == "achieved"


def test_the_resolution_grace_window_closes_the_claim(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=3_600
    )
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    limit = oracle.get_milestone(mid)["resolution_deadline"]

    warp_to(direct_vm, limit + 1)
    status = oracle.is_resolvable(mid)
    assert status["resolvable"] is False
    assert status["reason"] == "resolution window closed, call refund"

    set_scene(direct_vm, GITHUB_URL, github_page("10,240"), 10_240)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("the resolution window closed at"):
        oracle.resolve_deferred(mid)

    assert oracle.is_refundable(mid)["refundable"] is True


def test_an_unresolved_borderline_claim_refunds_the_sponsor(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    """
    The burden of proof sits with the claim, so a reading that never becomes
    distinguishable resolves in the sponsor's favour.
    """
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=3_600
    )
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    warp_to(direct_vm, oracle.get_milestone(mid)["resolution_deadline"] + 1)

    direct_vm.sender = direct_alice
    out = oracle.refund(mid)
    assert out["status"] == "refunded"
    assert out["refunded_atto"] == 5 * GEN
    assert "outlived the resolution window" in out["reason"]
    assert oracle.get_treasury()["escrowed_atto"] == 0


# ---------------------------------------------------------------------------
# The contested state
# ---------------------------------------------------------------------------


def ladder_to_contested(oracle, direct_vm, creator, mid, claim_fn, rounds=4):
    """Walk a milestone through enough borderline rounds to contest it."""
    claim_fn(
        oracle, creator, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050
    )
    for step in range(1, rounds):
        warp_by(direct_vm, oracle, SIX_HOURS)
        resolve(oracle, direct_vm, creator, mid, 10_050 + step)
    return oracle.get_milestone(mid)


def test_repeated_borderline_rounds_mark_the_milestone_contested(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=30 * DAY
    )
    record = ladder_to_contested(oracle, direct_vm, direct_bob, mid, claim)

    assert record["status"] == "contested"
    assert record["defer_rounds"] == 4
    assert record["attempts"] == 4
    assert oracle.get_status_counts()["contested"] == 1


def test_the_contest_threshold_is_configurable(
    deploy_oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    contract = deploy_oracle(max_defer_rounds=1)
    mid = make_milestone(contract, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        contract, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050
    )
    assert out["status"] == "contested"


def test_a_contested_milestone_keeps_rechecking_without_laddering(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    """
    Contested is a soft state, not a dead end. Auto-settling after N borderline
    rounds would reintroduce the knife edge the band exists to remove, so the
    contract keeps re-reading and lets the grace window end it.
    """
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=30 * DAY
    )
    ladder_to_contested(oracle, direct_vm, direct_bob, mid, claim)

    warp_by(direct_vm, oracle, SIX_HOURS)
    out = resolve(oracle, direct_vm, direct_bob, mid, 10_070)
    assert out["status"] == "contested"
    assert out["zone"] == "borderline"
    assert out["attempt"] == 5
    assert out["recheck_after"] == oracle.now_ts() + SIX_HOURS
    assert out["resolution_deadline"] == oracle.get_milestone(mid)["resolution_deadline"]

    record = oracle.get_milestone(mid)
    assert record["defer_rounds"] == 4  # does not ladder past the cap
    assert record["observed_value"] == 10_070
    assert json.loads(record["evidence"])["round"] == 5


def test_a_contested_milestone_settles_once_the_count_clears_the_band(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=30 * DAY
    )
    ladder_to_contested(oracle, direct_vm, direct_bob, mid, claim)

    warp_by(direct_vm, oracle, SIX_HOURS)
    out = resolve(oracle, direct_vm, direct_bob, mid, 11_500)
    assert out["zone"] == "pass"
    assert out["status"] == "achieved"
    assert oracle.get_status_counts()["contested"] == 0
    assert oracle.get_status_counts()["achieved"] == 1


def test_a_contested_milestone_can_also_resolve_downward(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=30 * DAY
    )
    ladder_to_contested(oracle, direct_vm, direct_bob, mid, claim)

    warp_by(direct_vm, oracle, SIX_HOURS)
    out = resolve(oracle, direct_vm, direct_bob, mid, 8_000)
    assert out["zone"] == "fail"
    assert out["status"] == "open"


# ---------------------------------------------------------------------------
# Manual exits from a pending claim
# ---------------------------------------------------------------------------


def test_a_sponsor_can_concede_a_borderline_claim(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)

    direct_vm.sender = direct_alice
    out = oracle.concede(mid)
    assert out["status"] == "achieved"
    assert out["conceded"] is True
    assert out["payout_atto"] == 5 * GEN
    assert oracle.get_treasury()["escrowed_atto"] == 0


def test_a_sponsor_can_concede_a_contested_claim(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=30 * DAY
    )
    ladder_to_contested(oracle, direct_vm, direct_bob, mid, claim)
    direct_vm.sender = direct_alice
    assert oracle.concede(mid)["status"] == "achieved"


def test_only_the_sponsor_can_concede(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, direct_charlie, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    for impostor in (direct_bob, direct_charlie):
        direct_vm.sender = impostor
        with direct_vm.expect_revert("sponsor only"):
            oracle.concede(mid)


def test_conceding_cannot_bypass_adjudication(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    """Concession is only available once a claim has actually been adjudicated."""
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("only a deferred or contested claim can be conceded"):
        oracle.concede(mid)


def test_a_creator_can_withdraw_a_pending_claim(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)

    direct_vm.sender = direct_bob
    oracle.withdraw_claim(mid)
    record = oracle.get_milestone(mid)
    assert record["status"] == "open"
    assert record["defer_rounds"] == 0
    assert record["deferred_until"] == 0
    assert record["claimed_at"] == 0
    assert oracle.is_claimable(mid)["claimable"] is True


def test_withdrawing_after_the_deadline_expires_the_milestone(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=3_600
    )
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    warp_by(direct_vm, oracle, 2 * 3_600)

    direct_vm.sender = direct_bob
    oracle.withdraw_claim(mid)
    assert oracle.get_milestone(mid)["status"] == "expired"
    assert oracle.is_refundable(mid)["refundable"] is True


def test_only_the_creator_can_withdraw_a_claim(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, direct_charlie, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    for impostor in (direct_alice, direct_charlie):
        direct_vm.sender = impostor
        with direct_vm.expect_revert("creator only"):
            oracle.withdraw_claim(mid)


def test_withdrawing_with_no_pending_claim_is_refused(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("no pending claim"):
        oracle.withdraw_claim(mid)


def test_withdrawing_a_claim_works_while_paused(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob, claim
):
    """A pause must never trap a creator in a pending claim."""
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    direct_vm.sender = direct_owner
    oracle.set_paused(True)
    direct_vm.sender = direct_bob
    oracle.withdraw_claim(mid)
    assert oracle.get_milestone(mid)["status"] == "open"
