"""
Settlement, escrow accounting and the exits that return money.

Direct mode does not move native value (the chain-layer message is a no-op
without a node), so these tests assert the contract's own accounting: the
escrowed total, the paid total, the refunded total and the fee total. Real value
movement is asserted in ``tests/integration``.
"""

import pytest

from conftest import DAY, GEN, GITHUB_URL, github_page, warp_by


# ---------------------------------------------------------------------------
# Payout and fees
# ---------------------------------------------------------------------------


def test_a_payout_moves_the_whole_reward_when_no_fee_is_set(
    oracle, make_milestone, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431)

    treasury = oracle.get_treasury()
    assert treasury["escrowed_atto"] == 0
    assert treasury["total_paid_atto"] == 5 * GEN
    assert treasury["total_fees_atto"] == 0
    assert treasury["solvent"] is True


def test_a_configured_fee_is_split_out_of_the_payout(
    deploy_oracle, make_milestone, direct_charlie, direct_alice, direct_bob, claim
):
    contract = deploy_oracle(fee_bps=250, fee_recipient=direct_charlie.as_hex)
    mid = make_milestone(contract, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        contract, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431
    )

    expected_fee = 5 * GEN * 250 // 10_000
    assert out["fee_atto"] == expected_fee
    assert out["payout_atto"] == 5 * GEN - expected_fee

    record = contract.get_milestone(mid)
    assert record["fee_atto"] == expected_fee
    assert record["payout_atto"] == 5 * GEN - expected_fee

    treasury = contract.get_treasury()
    assert treasury["total_fees_atto"] == expected_fee
    assert treasury["total_paid_atto"] == 5 * GEN - expected_fee
    assert treasury["escrowed_atto"] == 0


def test_a_fee_with_no_recipient_is_not_charged(
    deploy_oracle, make_milestone, direct_alice, direct_bob, claim
):
    """A rate without a destination must not quietly strand value."""
    contract = deploy_oracle(fee_bps=250, fee_recipient="")
    mid = make_milestone(contract, direct_alice, direct_bob, threshold=10_000)
    out = claim(
        contract, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431
    )
    assert out["fee_atto"] == 0
    assert out["payout_atto"] == 5 * GEN


def test_a_fee_above_the_cap_is_refused_at_deployment(
    deploy_oracle, direct_vm
):
    with direct_vm.expect_revert("fee_bps must be between 0 and 1000"):
        deploy_oracle(fee_bps=1_001)


def test_the_fee_rate_is_a_one_way_ratchet(
    deploy_oracle, direct_vm, direct_owner, direct_charlie
):
    """
    Integrators read the fee once and build against it, so it can only ever be
    lowered. Raising it would let a deployment change the economics under them.
    """
    contract = deploy_oracle(fee_bps=500, fee_recipient=direct_charlie.as_hex)
    direct_vm.sender = direct_owner

    contract.set_fee(200, direct_charlie)
    assert contract.get_config()["fee_bps"] == 200

    with direct_vm.expect_revert("fee_bps may only be lowered, currently 200"):
        contract.set_fee(201, direct_charlie)

    contract.set_fee(0, direct_charlie)
    assert contract.get_config()["fee_bps"] == 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_a_sponsor_can_cancel_a_fresh_unclaimed_milestone(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_alice
    oracle.cancel_milestone(mid)

    record = oracle.get_milestone(mid)
    assert record["status"] == "cancelled"
    assert record["resolved_at"] == oracle.now_ts()

    treasury = oracle.get_treasury()
    assert treasury["escrowed_atto"] == 0
    assert treasury["total_refunded_atto"] == 5 * GEN


def test_cancellation_closes_with_the_window(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    """
    The window is deliberately short. A sponsor can correct a mistake, but
    cannot pull the reward out from under a creator who is working towards it.
    """
    mid = make_milestone(oracle, direct_alice, direct_bob)
    window_ends = oracle.get_milestone(mid)["cancel_window_ends"]
    assert window_ends == oracle.now_ts() + 3_600

    warp_by(direct_vm, oracle, 3_601)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("the cancellation window closed at"):
        oracle.cancel_milestone(mid)


def test_cancellation_is_refused_once_a_claim_has_been_adjudicated(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("8,100"), value=8_100)
    assert oracle.get_milestone(mid)["status"] == "open"

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("has been adjudicated and can no longer be cancelled"):
        oracle.cancel_milestone(mid)


def test_only_the_sponsor_can_cancel(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, direct_charlie
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    for impostor in (direct_bob, direct_charlie):
        direct_vm.sender = impostor
        with direct_vm.expect_revert("sponsor only"):
            oracle.cancel_milestone(mid)


def test_a_settled_milestone_cannot_be_cancelled(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("only an open milestone can be cancelled"):
        oracle.cancel_milestone(mid)


def test_cancellation_works_while_paused(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_owner
    oracle.set_paused(True)
    direct_vm.sender = direct_alice
    oracle.cancel_milestone(mid)
    assert oracle.get_milestone(mid)["status"] == "cancelled"


def test_a_cancelled_milestone_still_blocks_its_duplicate(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    """
    Deduplication is by terms, not by liveness, so a cancelled milestone keeps
    its fingerprint. Re-posting the same grant means changing a term.
    """
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_alice
    oracle.cancel_milestone(mid)
    with direct_vm.expect_revert("already exists as `m1`"):
        make_milestone(oracle, direct_alice, direct_bob)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


def test_an_open_milestone_refunds_once_its_deadline_passes(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob, deadline_in=3_600)
    warp_by(direct_vm, oracle, 3_601)

    direct_vm.sender = direct_alice
    out = oracle.refund(mid)
    assert out["status"] == "refunded"
    assert out["refunded_atto"] == 5 * GEN
    assert out["reason"] == "deadline passed with no successful claim"

    treasury = oracle.get_treasury()
    assert treasury["escrowed_atto"] == 0
    assert treasury["total_refunded_atto"] == 5 * GEN


def test_a_refund_before_the_deadline_is_refused(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    status = oracle.is_refundable(mid)
    assert status["refundable"] is False
    assert status["reason"] == "deadline has not passed"

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("the deadline has not passed yet"):
        oracle.refund(mid)


def test_a_refund_is_permissionless_and_always_routes_to_the_sponsor(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, direct_charlie
):
    mid = make_milestone(oracle, direct_alice, direct_bob, deadline_in=3_600)
    warp_by(direct_vm, oracle, 3_601)

    direct_vm.sender = direct_charlie
    out = oracle.refund(mid)
    assert out["status"] == "refunded"
    assert oracle.get_milestone(mid)["sponsor"] == direct_alice.as_hex


def test_an_expired_milestone_refunds(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000, deadline_in=3_600)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    warp_by(direct_vm, oracle, 2 * 3_600)
    direct_vm.sender = direct_bob
    oracle.withdraw_claim(mid)
    assert oracle.get_milestone(mid)["status"] == "expired"

    direct_vm.sender = direct_alice
    out = oracle.refund(mid)
    assert out["reason"] == "expired"
    assert out["refunded_atto"] == 5 * GEN


@pytest.mark.parametrize("terminal", ["achieved", "refunded", "cancelled"])
def test_a_terminal_milestone_cannot_be_refunded_again(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim, terminal
):
    if terminal == "achieved":
        mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
        claim(
            oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431
        )
    elif terminal == "refunded":
        mid = make_milestone(oracle, direct_alice, direct_bob, deadline_in=3_600)
        warp_by(direct_vm, oracle, 3_601)
        direct_vm.sender = direct_alice
        oracle.refund(mid)
    else:
        mid = make_milestone(oracle, direct_alice, direct_bob)
        direct_vm.sender = direct_alice
        oracle.cancel_milestone(mid)

    assert oracle.get_milestone(mid)["status"] == terminal
    assert oracle.is_refundable(mid)["refundable"] is False
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("holds no escrow"):
        oracle.refund(mid)


def test_refunds_work_while_paused(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob
):
    """A pause must never trap escrow."""
    mid = make_milestone(oracle, direct_alice, direct_bob, deadline_in=3_600)
    warp_by(direct_vm, oracle, 3_601)
    direct_vm.sender = direct_owner
    oracle.set_paused(True)
    direct_vm.sender = direct_alice
    assert oracle.refund(mid)["status"] == "refunded"


# ---------------------------------------------------------------------------
# Topping up
# ---------------------------------------------------------------------------


def test_a_sponsor_can_sweeten_an_open_milestone(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob, reward=5 * GEN)
    direct_vm.sender = direct_alice
    direct_vm.value = 3 * GEN
    try:
        total = oracle.top_up(mid)
    finally:
        direct_vm.value = 0

    assert total == 8 * GEN
    assert oracle.get_milestone(mid)["reward_atto"] == 8 * GEN
    assert oracle.get_treasury()["escrowed_atto"] == 8 * GEN


def test_a_top_up_is_paid_out_with_the_rest(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000, reward=5 * GEN)
    direct_vm.sender = direct_alice
    direct_vm.value = 3 * GEN
    try:
        oracle.top_up(mid)
    finally:
        direct_vm.value = 0

    out = claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431
    )
    assert out["payout_atto"] == 8 * GEN
    assert oracle.get_treasury()["escrowed_atto"] == 0


def test_a_pending_claim_can_still_be_topped_up(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050)
    direct_vm.sender = direct_alice
    direct_vm.value = GEN
    try:
        assert oracle.top_up(mid) == 6 * GEN
    finally:
        direct_vm.value = 0


def test_only_the_sponsor_can_top_up(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_bob
    direct_vm.value = GEN
    try:
        with direct_vm.expect_revert("sponsor only"):
            oracle.top_up(mid)
    finally:
        direct_vm.value = 0


def test_a_zero_top_up_is_refused(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("top up must be positive"):
        oracle.top_up(mid)


def test_a_settled_milestone_cannot_be_topped_up(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431)
    direct_vm.sender = direct_alice
    direct_vm.value = GEN
    try:
        with direct_vm.expect_revert("is achieved"):
            oracle.top_up(mid)
    finally:
        direct_vm.value = 0


# ---------------------------------------------------------------------------
# Solvency and surplus
# ---------------------------------------------------------------------------


def test_escrow_is_tracked_across_many_live_milestones(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    first = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000, reward=2 * GEN)
    second = make_milestone(oracle, direct_alice, direct_bob, threshold=20_000, reward=3 * GEN)
    third = make_milestone(oracle, direct_alice, direct_bob, threshold=30_000, reward=4 * GEN)
    assert oracle.get_treasury()["escrowed_atto"] == 9 * GEN

    claim(oracle, direct_bob, first, url=GITHUB_URL, page=github_page("12,431"), value=12_431)
    assert oracle.get_treasury()["escrowed_atto"] == 7 * GEN

    direct_vm.sender = direct_alice
    oracle.cancel_milestone(second)
    assert oracle.get_treasury()["escrowed_atto"] == 4 * GEN

    warp_by(direct_vm, oracle, 8 * DAY)
    direct_vm.sender = direct_alice
    oracle.refund(third)

    treasury = oracle.get_treasury()
    assert treasury["escrowed_atto"] == 0
    assert treasury["total_paid_atto"] == 2 * GEN
    assert treasury["total_refunded_atto"] == 7 * GEN


def test_the_owner_can_only_withdraw_unescrowed_surplus(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob
):
    make_milestone(oracle, direct_alice, direct_bob, reward=5 * GEN)
    treasury = oracle.get_treasury()
    surplus = treasury["surplus_atto"]
    assert surplus == treasury["balance_atto"] - 5 * GEN

    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("is unescrowed surplus"):
        oracle.withdraw_surplus(direct_owner, surplus + 1)
    oracle.withdraw_surplus(direct_owner, surplus)


def test_only_the_owner_can_withdraw_surplus(
    oracle, direct_vm, direct_alice
):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("owner only"):
        oracle.withdraw_surplus(direct_alice, GEN)


def test_a_zero_surplus_withdrawal_is_refused(oracle, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("amount must be positive"):
        oracle.withdraw_surplus(direct_owner, 0)


def test_solvency_is_reported(oracle, make_milestone, direct_alice, direct_bob):
    make_milestone(oracle, direct_alice, direct_bob)
    treasury = oracle.get_treasury()
    assert treasury["solvent"] is True
    assert treasury["balance_atto"] >= treasury["escrowed_atto"]
