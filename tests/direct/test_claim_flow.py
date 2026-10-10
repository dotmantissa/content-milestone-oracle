"""
Adjudication outcomes and the claim state machine.

One claim can end four ways: a clear pass settles and pays, a clear fail hands
the right to claim back to the creator, an unreadable page is recorded without
penalty, and a borderline reading defers. These tests drive each path through
the real non-deterministic block with mocked pages and reader responses.
"""

import json

from conftest import (
    GEN,
    GENERIC_URL,
    GITHUB_URL,
    YOUTUBE_URL,
    generic_page,
    github_page,
    set_scene,
    warp_by,
    youtube_page,
)


# ---------------------------------------------------------------------------
# Clear pass
# ---------------------------------------------------------------------------


def test_a_clear_pass_settles_and_pays_in_the_same_transaction(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431
    )

    assert out["zone"] == "pass"
    assert out["status"] == "achieved"
    assert out["observed_value"] == 12_431
    assert out["payout_atto"] == 5 * GEN
    assert out["fee_atto"] == 0

    record = oracle.get_milestone(mid)
    assert record["status"] == "achieved"
    assert record["payout_atto"] == 5 * GEN
    assert record["resolved_at"] == oracle.now_ts()
    assert record["attempts"] == 1


def test_a_reading_exactly_on_the_certain_pass_line_passes(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,100"), value=10_100
    )
    assert out["zone"] == "pass"
    assert out["status"] == "achieved"


def test_the_evidence_record_captures_how_the_reading_was_obtained(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(
        oracle,
        direct_bob,
        mid,
        url=GITHUB_URL,
        page=github_page("12,431"),
        value=12_431,
        display="12,431 stars",
        reason="the star counter in the repository header",
    )
    evidence = json.loads(oracle.get_evidence(mid))
    assert evidence["found"] is True
    assert evidence["value"] == 12_431
    assert evidence["zone"] == "pass"
    assert evidence["display"] == "12,431 stars"
    assert evidence["keyword_scoped"] is True
    assert evidence["candidates"] > 0
    assert evidence["page_chars"] > 0
    assert evidence["round"] == 1
    assert evidence["at"] == oracle.now_ts()


# ---------------------------------------------------------------------------
# Clear fail
# ---------------------------------------------------------------------------


def test_a_clear_fail_leaves_the_milestone_claimable(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("8,100"), value=8_100
    )

    assert out["zone"] == "fail"
    assert out["status"] == "open"
    record = oracle.get_milestone(mid)
    assert record["attempts"] == 1
    assert record["claimed_at"] == 0
    assert record["payout_atto"] == 0
    assert oracle.is_claimable(mid)["claimable"] is True


def test_a_creator_may_claim_again_after_a_fail(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("8,100"), value=8_100)
    out = claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431
    )
    assert out["status"] == "achieved"
    assert oracle.get_milestone(mid)["attempts"] == 2


def test_a_reading_just_under_the_certain_fail_line_fails(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("9,899"), value=9_899
    )
    assert out["zone"] == "fail"


def test_a_fail_after_the_deadline_expires_the_milestone(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    """
    Reachable through a deferred re-check, which is allowed to land after the
    deadline. A fail at that point ends the milestone.
    """
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=3_600
    )
    out = claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050
    )
    assert out["status"] == "deferred"

    warp_by(direct_vm, oracle, 7 * 3_600)
    set_scene(direct_vm, GITHUB_URL, github_page("9,000"), 9_000)
    direct_vm.sender = direct_bob
    out = oracle.resolve_deferred(mid)
    assert out["zone"] == "fail"
    assert out["status"] == "expired"
    assert oracle.is_refundable(mid)["refundable"] is True


# ---------------------------------------------------------------------------
# Unreadable
# ---------------------------------------------------------------------------


def test_an_unreadable_metric_is_recorded_without_penalty(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        oracle,
        direct_bob,
        mid,
        url=GITHUB_URL,
        page=github_page("12,431"),
        value=0,
        found=False,
        reason="the page shows forks and issues but no star count",
    )

    assert out["zone"] == "unreadable"
    assert out["status"] == "open"
    assert out["found"] is False
    assert oracle.get_milestone(mid)["observed_value"] == 0
    assert json.loads(oracle.get_evidence(mid))["found"] is False


# ---------------------------------------------------------------------------
# Claim preconditions
# ---------------------------------------------------------------------------


def test_only_the_named_creator_can_claim(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, direct_charlie
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    set_scene(direct_vm, GITHUB_URL, github_page("12,431"), 12_431)
    for impostor in (direct_alice, direct_charlie):
        direct_vm.sender = impostor
        with direct_vm.expect_revert("creator only"):
            oracle.submit_claim(mid)


def test_an_unknown_milestone_cannot_be_claimed(oracle, direct_vm, direct_bob):
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("unknown milestone `nope`"):
        oracle.submit_claim("nope")


def test_a_claim_after_the_deadline_is_refused(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=3_600
    )
    warp_by(direct_vm, oracle, 3_601)
    set_scene(direct_vm, GITHUB_URL, github_page("12,431"), 12_431)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("the deadline passed at"):
        oracle.submit_claim(mid)
    assert oracle.is_claimable(mid)["reason"] == "deadline passed"


def test_an_achieved_milestone_cannot_be_claimed_twice(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431)
    set_scene(direct_vm, GITHUB_URL, github_page("12,431"), 12_431)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("is achieved, not open"):
        oracle.submit_claim(mid)


def test_claiming_is_blocked_while_paused(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_owner
    oracle.set_paused(True)
    set_scene(direct_vm, GITHUB_URL, github_page("12,431"), 12_431)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("contract is paused"):
        oracle.submit_claim(mid)
    assert oracle.is_claimable(mid)["reason"] == "contract is paused"


def test_a_claim_on_a_platform_disabled_after_creation_is_refused(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_owner
    oracle.set_platform_enabled("github", False)
    set_scene(direct_vm, GITHUB_URL, github_page("12,431"), 12_431)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("platform `github` is disabled"):
        oracle.submit_claim(mid)
    # The escrow stays reachable: disabling a reader must not trap funds.
    assert oracle.get_milestone(mid)["status"] == "open"


# ---------------------------------------------------------------------------
# Reading other platforms and metrics
# ---------------------------------------------------------------------------


def test_a_youtube_view_milestone_reads_the_view_count(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(
        oracle,
        direct_alice,
        direct_bob,
        url=YOUTUBE_URL,
        platform="youtube",
        metric="views",
        threshold=1_000_000_000,
    )
    out = claim(
        oracle,
        direct_bob,
        mid,
        url=YOUTUBE_URL,
        page=youtube_page("1.4B"),
        value=1_400_000_000,
    )
    assert out["zone"] == "pass"
    assert out["observed_value"] == 1_400_000_000


def test_a_generic_web_milestone_falls_back_to_the_page_head(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(
        oracle,
        direct_alice,
        direct_bob,
        url=GENERIC_URL,
        platform="generic_web",
        metric="views",
        threshold=8_000,
    )
    out = claim(
        oracle, direct_bob, mid, url=GENERIC_URL, page=generic_page("8,204"), value=8_204
    )
    assert out["zone"] == "pass"


def test_the_keywords_used_for_a_pairing_are_inspectable(oracle):
    github_stars = oracle.resolve_keywords("github", "stars")
    assert "stars" in github_stars
    assert "stargazers" in github_stars
    # Platform-specific hints are appended to the metric's own keywords.
    assert "sponsor" in github_stars

    youtube_views = oracle.resolve_keywords("youtube", "views")
    assert "views" in youtube_views
    assert "streamed" in youtube_views


def test_an_unknown_metric_still_gets_its_own_name_as_a_keyword(oracle):
    """
    A platform may be registered with a metric whose keyword list was later
    removed. The metric name itself is always usable as a search term.
    """
    assert "stars" in oracle.resolve_keywords("github", "stars")
    assert oracle.resolve_keywords("github", "unknownmetric")[0] == "unknownmetric"
