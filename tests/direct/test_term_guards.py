"""Regression tests for fixed escrow terms and truthful action predicates."""

import pytest

from conftest import DAY, GITHUB_URL, SIX_HOURS, github_page, warp_by, warp_to


def test_admin_updates_preserve_existing_timing_terms(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob, claim
):
    old = make_milestone(oracle, direct_alice, direct_bob, deadline_in=DAY)
    original = oracle.get_milestone(old)
    direct_vm.sender = direct_owner
    oracle.set_consensus_params(500, 200, 50, 60, 1, 0)
    oracle.set_economics(0, 365 * DAY, 0)
    new = make_milestone(oracle, direct_alice, direct_bob, threshold=20_000)

    existing = oracle.get_milestone(old)
    assert existing["resolution_deadline"] == original["resolution_deadline"]
    assert existing["cancel_window_ends"] == original["cancel_window_ends"]
    assert existing["defer_seconds"] == SIX_HOURS
    assert existing["max_defer_rounds"] == 4
    fresh = oracle.get_milestone(new)
    assert fresh["resolution_deadline"] == fresh["deadline_ts"]
    assert fresh["cancel_window_ends"] == fresh["created_at"]
    assert fresh["defer_seconds"] == 60
    assert fresh["max_defer_rounds"] == 1

    result = claim(
        oracle, direct_bob, old, url=GITHUB_URL, page=github_page("10,050"), value=10_050
    )
    assert result["status"] == "deferred"
    assert result["recheck_after"] == oracle.now_ts() + SIX_HOURS
    warp_by(direct_vm, oracle, SIX_HOURS)
    result = oracle.resolve_deferred(old)
    assert result["status"] == "deferred"
    assert result["defer_rounds"] == 2
    assert result["recheck_after"] == oracle.now_ts() + SIX_HOURS


@pytest.mark.parametrize("new_grace", [0, 4 * DAY])
def test_owner_cannot_move_an_existing_refund_deadline(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob,
    claim, new_grace
):
    mid = make_milestone(oracle, direct_alice, direct_bob, deadline_in=DAY)
    limit = oracle.get_milestone(mid)["resolution_deadline"]
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    direct_vm.sender = direct_owner
    oracle.set_consensus_params(100, 50, 5, SIX_HOURS, 4, new_grace)
    warp_to(direct_vm, limit)
    assert oracle.is_refundable(mid)["refundable"] is False
    assert oracle.is_resolvable(mid)["resolvable"] is True
    with direct_vm.expect_revert("still resolvable"):
        oracle.refund(mid)
    warp_by(direct_vm, oracle, 1)
    assert oracle.is_refundable(mid)["refundable"] is True
    assert oracle.refund(mid)["status"] == "refunded"


@pytest.mark.parametrize("new_window,elapsed,allowed", [(0, 1, True), (DAY, 3_601, False)])
def test_owner_cannot_change_an_existing_cancellation_window(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob,
    new_window, elapsed, allowed
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_owner
    oracle.set_economics(0, 365 * DAY, new_window)
    warp_by(direct_vm, oracle, elapsed)
    direct_vm.sender = direct_alice
    if allowed:
        oracle.cancel_milestone(mid)
        assert oracle.get_milestone(mid)["status"] == "cancelled"
    else:
        with direct_vm.expect_revert("cancellation window closed"):
            oracle.cancel_milestone(mid)


@pytest.mark.parametrize("action", ["claim", "resolve"])
@pytest.mark.parametrize("guard", ["pause", "platform"])
def test_predicates_match_adjudication_guards(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob,
    claim, action, guard
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    if action == "resolve":
        claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
        warp_by(direct_vm, oracle, SIX_HOURS)
    direct_vm.sender = direct_owner
    if guard == "pause":
        oracle.set_paused(True)
        reason = "contract is paused"
    else:
        oracle.set_platform_enabled("github", False)
        reason = "platform `github` is disabled"

    direct_vm.sender = direct_bob
    if action == "claim":
        status = oracle.is_claimable(mid)
        assert status["claimable"] is False
        assert status["reason"] == reason
        with direct_vm.expect_revert(reason):
            oracle.submit_claim(mid)
    else:
        status = oracle.is_resolvable(mid)
        assert status["resolvable"] is False
        assert status["reason"] == reason
        with direct_vm.expect_revert(reason):
            oracle.resolve_deferred(mid)


def test_closed_resolution_window_directs_bots_to_refund_before_recheck(
    deploy_oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    oracle = deploy_oracle(resolution_grace_seconds=0)
    mid = make_milestone(oracle, direct_alice, direct_bob, deadline_in=60)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    warp_by(direct_vm, oracle, 61)
    status = oracle.is_resolvable(mid)
    assert status["reason"] == "resolution window closed, call refund"
    with direct_vm.expect_revert("resolution window closed"):
        oracle.resolve_deferred(mid)
    assert oracle.refund(mid)["status"] == "refunded"


def test_url_preflight_rejects_urls_too_long_to_create(
    oracle, direct_vm, direct_alice, direct_bob
):
    url = GITHUB_URL + "?q=" + "a" * 512
    assert oracle.check_url("github", url)["ok"] is False
    direct_vm.sender = direct_alice
    direct_vm.value = 1
    with direct_vm.expect_revert("content_url must be"):
        oracle.create_milestone(direct_bob, url, "github", "stars", 10_000, oracle.now_ts() + DAY)


def test_disabled_platform_url_preflight_explains_rejection(oracle, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    oracle.set_platform_enabled("github", False)
    status = oracle.check_url("github", GITHUB_URL)
    assert status["ok"] is False
    assert status["reason"] == "platform `github` is disabled"
