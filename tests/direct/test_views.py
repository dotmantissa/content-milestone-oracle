"""
Read surface: indexes, pagination, status tallies and the predicate views.

Every view an integrator builds a UI or a bot against is pinned here, including
the ``is_claimable`` / ``is_resolvable`` / ``is_refundable`` predicates that let
a caller avoid a doomed transaction and read back the reason.
"""

import pytest

from conftest import DAY, GITHUB_URL, SIX_HOURS, github_page, warp_by


# ---------------------------------------------------------------------------
# Counting and pagination
# ---------------------------------------------------------------------------


def test_an_empty_oracle_reads_cleanly(oracle, direct_alice):
    assert oracle.milestone_count() == 0
    assert oracle.list_milestones(0, 10) == []
    assert oracle.list_by_sponsor(direct_alice, 0, 10) == []
    assert oracle.list_by_creator(direct_alice, 0, 10) == []
    assert oracle.count_by_sponsor(direct_alice) == 0
    assert oracle.get_status_counts() == {
        "open": 0,
        "deferred": 0,
        "contested": 0,
        "achieved": 0,
        "expired": 0,
        "refunded": 0,
        "cancelled": 0,
    }


@pytest.fixture
def populated(oracle, make_milestone, direct_alice, direct_bob, direct_charlie):
    """Twelve milestones across two sponsors and two creators."""
    ids = []
    for index in range(8):
        ids.append(
            make_milestone(
                oracle, direct_alice, direct_bob, threshold=1_000 + index
            )
        )
    for index in range(4):
        ids.append(
            make_milestone(
                oracle, direct_charlie, direct_alice, threshold=2_000 + index
            )
        )
    return ids


def test_milestones_are_listed_in_creation_order(oracle, populated):
    listed = [m["milestone_id"] for m in oracle.list_milestones(0, 50)]
    assert listed == populated
    assert oracle.milestone_count() == 12


def test_pagination_walks_the_whole_set(oracle, populated):
    seen = []
    offset = 0
    while True:
        page = oracle.list_milestones(offset, 5)
        if not page:
            break
        seen.extend(m["milestone_id"] for m in page)
        offset += 5
    assert seen == populated


def test_an_offset_past_the_end_returns_nothing(oracle, populated):
    assert oracle.list_milestones(999, 10) == []


def test_a_negative_offset_is_treated_as_the_start(oracle, populated):
    assert oracle.list_milestones(-5, 3)[0]["milestone_id"] == populated[0]


@pytest.mark.parametrize("limit", [0, -1, 51, 1_000])
def test_an_out_of_range_limit_falls_back_to_the_cap(oracle, populated, limit):
    """Unbounded reads would blow the compute budget, so the cap is enforced."""
    assert len(oracle.list_milestones(0, limit)) == 12


def test_the_page_cap_is_fifty(oracle, make_milestone, direct_alice, direct_bob):
    for index in range(55):
        make_milestone(oracle, direct_alice, direct_bob, threshold=1_000 + index)
    assert oracle.milestone_count() == 55
    assert len(oracle.list_milestones(0, 50)) == 50
    assert len(oracle.list_milestones(50, 50)) == 5


# ---------------------------------------------------------------------------
# Party indexes
# ---------------------------------------------------------------------------


def test_milestones_are_indexed_by_sponsor_and_creator(
    oracle, populated, direct_alice, direct_bob, direct_charlie
):
    assert oracle.count_by_sponsor(direct_alice) == 8
    assert oracle.count_by_sponsor(direct_charlie) == 4
    assert oracle.count_by_creator(direct_bob) == 8
    assert oracle.count_by_creator(direct_alice) == 4

    sponsored = [m["milestone_id"] for m in oracle.list_by_sponsor(direct_alice, 0, 50)]
    assert sponsored == populated[:8]
    created = [m["milestone_id"] for m in oracle.list_by_creator(direct_alice, 0, 50)]
    assert created == populated[8:]


def test_party_indexes_paginate_too(oracle, populated, direct_alice):
    first = oracle.list_by_sponsor(direct_alice, 0, 3)
    second = oracle.list_by_sponsor(direct_alice, 3, 3)
    assert len(first) == 3
    assert len(second) == 3
    assert first[0]["milestone_id"] != second[0]["milestone_id"]


def test_an_address_with_no_milestones_reads_empty(oracle, populated, direct_bob):
    assert oracle.list_by_sponsor(direct_bob, 0, 10) == []
    assert oracle.count_by_sponsor(direct_bob) == 0


def test_one_address_can_be_both_sponsor_and_creator(
    oracle, make_milestone, direct_alice
):
    mid = make_milestone(oracle, direct_alice, direct_alice)
    assert oracle.count_by_sponsor(direct_alice) == 1
    assert oracle.count_by_creator(direct_alice) == 1
    assert oracle.list_by_sponsor(direct_alice, 0, 10)[0]["milestone_id"] == mid


# ---------------------------------------------------------------------------
# Status tallies
# ---------------------------------------------------------------------------


def test_status_counts_follow_every_transition(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    achieved = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    deferred = make_milestone(oracle, direct_alice, direct_bob, threshold=20_000)
    cancelled = make_milestone(oracle, direct_alice, direct_bob, threshold=30_000)
    refunded = make_milestone(
        oracle, direct_alice, direct_bob, threshold=40_000, deadline_in=3_600
    )
    assert oracle.get_status_counts()["open"] == 4

    claim(
        oracle, direct_bob, achieved, url=GITHUB_URL, page=github_page("12,431"), value=12_431
    )
    claim(
        oracle, direct_bob, deferred, url=GITHUB_URL, page=github_page("20,050"), value=20_050
    )
    direct_vm.sender = direct_alice
    oracle.cancel_milestone(cancelled)

    counts = oracle.get_status_counts()
    assert counts == {
        "open": 1,
        "deferred": 1,
        "contested": 0,
        "achieved": 1,
        "expired": 0,
        "refunded": 0,
        "cancelled": 1,
    }

    warp_by(direct_vm, oracle, 2 * 3_600)
    direct_vm.sender = direct_alice
    oracle.refund(refunded)

    counts = oracle.get_status_counts()
    assert counts["open"] == 0
    assert counts["refunded"] == 1
    assert sum(counts.values()) == 4


def test_status_counts_never_double_count_a_repeated_transition(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    """Two consecutive fails leave the milestone open exactly once."""
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("8,100"), value=8_100)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("8,200"), value=8_200)
    assert oracle.get_status_counts()["open"] == 1


# ---------------------------------------------------------------------------
# The milestone record
# ---------------------------------------------------------------------------


def test_the_milestone_record_carries_its_own_timing_windows(
    oracle, make_milestone, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob, deadline_in=10 * DAY)
    record = oracle.get_milestone(mid)
    now = oracle.now_ts()
    assert record["deadline_ts"] == now + 10 * DAY
    assert record["cancel_window_ends"] == now + 3_600
    assert record["resolution_deadline"] == record["deadline_ts"] + 2 * DAY


def test_an_unknown_milestone_is_reported_clearly(oracle, direct_vm):
    for method in ("get_milestone", "get_evidence", "is_claimable", "is_resolvable", "is_refundable"):
        with direct_vm.expect_revert("unknown milestone `ghost`"):
            getattr(oracle, method)("ghost")


def test_evidence_is_empty_until_the_first_adjudication(
    oracle, make_milestone, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    assert oracle.get_evidence(mid) == ""


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def test_is_claimable_reports_the_terms_a_creator_needs(
    oracle, make_milestone, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    status = oracle.is_claimable(mid)
    assert status["claimable"] is True
    assert status["reason"] == ""
    assert status["creator"] == direct_bob.as_hex
    assert status["decision_lines"]["certain_pass_at"] == 10_100


def test_the_predicates_agree_with_the_write_methods(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    """
    Every predicate is checked against the call it predicts, so a caller that
    trusts the view is never surprised by the transaction.
    """
    mid = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=3_600
    )

    assert oracle.is_claimable(mid)["claimable"] is True
    assert oracle.is_resolvable(mid)["resolvable"] is False
    assert oracle.is_refundable(mid)["refundable"] is False

    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    assert oracle.is_claimable(mid)["claimable"] is False
    assert oracle.is_resolvable(mid)["resolvable"] is False  # window not open yet
    assert oracle.is_refundable(mid)["refundable"] is False

    warp_by(direct_vm, oracle, SIX_HOURS)
    assert oracle.is_resolvable(mid)["resolvable"] is True

    warp_by(direct_vm, oracle, 3 * DAY)
    assert oracle.is_resolvable(mid)["resolvable"] is False
    assert oracle.is_refundable(mid)["refundable"] is True

    direct_vm.sender = direct_alice
    assert oracle.refund(mid)["status"] == "refunded"
    assert oracle.is_refundable(mid)["refundable"] is False


def test_the_predicates_report_a_pause(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_owner
    oracle.set_paused(True)
    assert oracle.is_claimable(mid)["reason"] == "contract is paused"


def test_the_clock_the_contract_reads_is_exposed(oracle, direct_vm):
    """
    The clock is the transaction timestamp, not wall time, so integrators need
    to be able to read the same value the contract sees.
    """
    first = oracle.now_ts()
    warp_by(direct_vm, oracle, 1_000)
    assert oracle.now_ts() == first + 1_000
