"""
End to end on a live GenLayer network.

Every ``submit_claim`` and ``resolve_deferred`` below is a real consensus round:
the leader renders a live page in a browser, a model reads the metric out of it,
and every validator repeats that independently before applying the contract's
equivalence principle to the leader's observation. No page, model response or
validator vote is mocked.

    gltest tests/integration -v -s --network studionet
"""

import time

import pytest
from gltest.assertions import tx_execution_succeeded
from genlayer_py.types import TransactionStatus

from conftest import (
    DAY,
    GITHUB_URL,
    REWARD,
    create_milestone,
    leader_return,
    now_ts,
    pace,
    read,
)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def creator(accounts):
    """A second account, so the sponsor and creator are genuinely different."""
    return accounts[1]


@pytest.fixture(scope="module")
def as_creator(oracle, creator):
    return oracle.connect(creator)


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------


def test_the_deployment_bootstraps_a_usable_oracle(oracle):
    config = read(oracle.get_config(args=[]))
    assert config["default_band_bps"] == 100
    assert config["paused"] is False
    assert config["fee_bps"] == 0

    platforms = {p["key"]: p for p in read(oracle.list_platforms(args=[]))}
    assert set(platforms) == {"github", "youtube", "x", "npm", "generic_web"}
    assert platforms["github"]["fetch_mode"] == "render_text"
    assert platforms["youtube"]["wait_after_loaded"] == "4s"

    assert "stargazers" in read(oracle.get_metric_keywords(args=["stars"]))
    assert read(oracle.check_url(args=["github", GITHUB_URL]))["ok"] is True


def test_the_contract_clock_is_the_transaction_timestamp(oracle):
    """
    The GenVM clock is pinned to the transaction datetime, which is what makes
    every deadline and defer window reproducible across validators.
    """
    reading = now_ts(oracle)
    assert abs(reading - int(time.time())) < 600


# ---------------------------------------------------------------------------
# A real clear pass
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_reading(oracle, as_creator, creator, gl_client):
    """
    One real adjudication against a live page, reused by the later tests.

    The threshold is deliberately far below the repository's actual star count,
    so the outcome is a clear pass under any plausible drift. The reading it
    returns is then what lets the borderline test target the band precisely
    without hardcoding a number that moves.
    """
    milestone = create_milestone(oracle, creator.address, threshold=5_000)
    before = gl_client.get_balance(creator.address)
    pace()

    receipt = as_creator.submit_claim(args=[milestone]).transact(
        wait_transaction_status=TransactionStatus.FINALIZED,
        wait_triggered_transactions=True,
    )
    assert tx_execution_succeeded(receipt), leader_return(receipt)

    result = read(oracle.get_milestone(args=[milestone]))
    assert result["status"] == "achieved", result
    assert result["observed_value"] >= 5_050, result
    assert result["payout_atto"] == REWARD
    assert result["attempts"] == 1

    pace()
    return {
        "milestone": milestone,
        "value": int(result["observed_value"]),
        "balance_before": before,
        "balance_after": gl_client.get_balance(creator.address),
        "votes": receipt["consensus_data"].get("votes", {}),
    }


def test_a_clear_pass_settles_through_consensus(live_reading, oracle):
    votes = live_reading["votes"]
    assert votes, "no validator votes recorded"
    assert "disagree" not in votes.values(), votes
    assert list(votes.values()).count("agree") >= 1, votes

    treasury = read(oracle.get_treasury(args=[]))
    assert treasury["total_paid_atto"] >= REWARD
    assert treasury["solvent"] is True


def test_the_reward_actually_leaves_the_contract(live_reading):
    """
    The payout is an external message to the chain layer, emitted on
    finalisation, so the test waits for the triggered transaction before
    reading the balance back.
    """
    gained = live_reading["balance_after"] - live_reading["balance_before"]
    assert gained == REWARD, (
        f"creator balance moved by {gained}, expected {REWARD}"
    )


def test_the_evidence_record_explains_the_reading(live_reading, oracle):
    import json

    evidence = json.loads(
        read(oracle.get_evidence(args=[live_reading["milestone"]]))
    )
    assert evidence["found"] is True
    assert evidence["zone"] == "pass"
    assert evidence["value"] == live_reading["value"]
    assert evidence["keyword_scoped"] is True, evidence
    assert evidence["candidates"] > 0
    assert evidence["page_chars"] > 1_000, evidence
    assert evidence["display"], "the reading should quote the page text"
    print(f"\nlive evidence: {evidence}")


def test_the_reading_matches_what_the_page_shows(live_reading):
    """
    go-ethereum has been in the tens of thousands of stars for years. A reading
    far outside that range would mean the model picked the wrong number, which
    the scanner gate cannot catch on its own.
    """
    assert 20_000 <= live_reading["value"] <= 200_000, live_reading["value"]


# ---------------------------------------------------------------------------
# A real clear fail
# ---------------------------------------------------------------------------


def test_a_clear_fail_leaves_the_milestone_claimable(oracle, as_creator, creator):
    milestone = create_milestone(oracle, creator.address, threshold=900_000_000)
    pace()

    receipt = as_creator.submit_claim(args=[milestone]).transact()
    assert tx_execution_succeeded(receipt), leader_return(receipt)

    record = read(oracle.get_milestone(args=[milestone]))
    assert record["status"] == "open", record
    assert record["attempts"] == 1
    assert record["payout_atto"] == 0
    assert record["observed_value"] > 0

    votes = receipt["consensus_data"].get("votes", {})
    assert "disagree" not in votes.values(), votes
    assert read(oracle.is_claimable(args=[milestone]))["claimable"] is True
    pace()


# ---------------------------------------------------------------------------
# A real borderline reading
# ---------------------------------------------------------------------------


def test_a_borderline_reading_defers_rather_than_settling(
    oracle, as_creator, creator, live_reading
):
    """
    The case the primitive exists for, against live data.

    The threshold is set to the count the page was just showing, which puts any
    fresh reading inside the tolerance band. The contract must refuse to settle
    and schedule a re-read instead.
    """
    milestone = create_milestone(oracle, creator.address, threshold=live_reading["value"])
    lines = read(oracle.get_milestone(args=[milestone]))["decision_lines"]
    assert lines["certain_fail_below"] < live_reading["value"] < lines["certain_pass_at"]
    pace()

    receipt = as_creator.submit_claim(args=[milestone]).transact()
    assert tx_execution_succeeded(receipt), leader_return(receipt)

    record = read(oracle.get_milestone(args=[milestone]))
    assert record["status"] == "deferred", record
    assert record["defer_rounds"] == 1
    assert record["payout_atto"] == 0
    assert record["deferred_until"] == record["observed_at"] + 3_600

    votes = receipt["consensus_data"].get("votes", {})
    assert "disagree" not in votes.values(), votes

    # The escrow stays locked while the claim is pending.
    assert read(oracle.get_treasury(args=[]))["escrowed_atto"] >= REWARD
    pace()


def test_a_recheck_before_its_window_is_refused(oracle, as_creator, creator, live_reading):
    """
    A deferred claim cannot be re-read on demand. Waiting is the whole
    mechanism: an immediate re-read would just resample the same ambiguity.
    """
    milestone = create_milestone(
        oracle, creator.address, threshold=live_reading["value"], deadline_in=6 * DAY
    )
    pace()
    receipt = as_creator.submit_claim(args=[milestone]).transact()
    assert tx_execution_succeeded(receipt), leader_return(receipt)
    assert read(oracle.get_milestone(args=[milestone]))["status"] == "deferred"

    status = read(oracle.is_resolvable(args=[milestone]))
    assert status["resolvable"] is False
    assert status["reason"] == "re-check window has not opened"

    pace()
    receipt = as_creator.resolve_deferred(args=[milestone]).transact()
    assert not tx_execution_succeeded(receipt)
    assert "[EXPECTED] re-check unlocks at" in str(leader_return(receipt))
    pace()


def test_a_sponsor_can_concede_a_borderline_claim(
    oracle, as_creator, creator, live_reading, gl_client
):
    """
    The settlement path for a reading the oracle will not call either way. The
    sponsor reads the recorded evidence and chooses to honour it.
    """
    milestone = create_milestone(
        oracle, creator.address, threshold=live_reading["value"], deadline_in=5 * DAY
    )
    pace()
    receipt = as_creator.submit_claim(args=[milestone]).transact()
    assert tx_execution_succeeded(receipt), leader_return(receipt)
    assert read(oracle.get_milestone(args=[milestone]))["status"] == "deferred"

    before = gl_client.get_balance(creator.address)
    pace()
    receipt = oracle.concede(args=[milestone]).transact(
        wait_transaction_status=TransactionStatus.FINALIZED,
        wait_triggered_transactions=True,
    )
    assert tx_execution_succeeded(receipt), leader_return(receipt)

    record = read(oracle.get_milestone(args=[milestone]))
    assert record["status"] == "achieved"
    assert record["payout_atto"] == REWARD
    assert gl_client.get_balance(creator.address) - before == REWARD
    pace()


# ---------------------------------------------------------------------------
# Deterministic guards under consensus
# ---------------------------------------------------------------------------


def test_the_access_guards_reach_consensus(oracle, creator, accounts):
    """
    A rejection is a consensus outcome too. Validators compare these messages
    verbatim, so they have to be identical on every node.
    """
    milestone = create_milestone(oracle, creator.address, threshold=5_000, deadline_in=4 * DAY)
    pace()

    # The sponsor is not the creator, so the sponsor cannot claim.
    receipt = oracle.submit_claim(args=[milestone]).transact()
    assert not tx_execution_succeeded(receipt)
    assert "[EXPECTED] creator only" in str(leader_return(receipt))
    votes = receipt["consensus_data"].get("votes", {})
    assert "disagree" not in votes.values(), votes
    pace()

    # A third party is neither.
    stranger = oracle.connect(accounts[2])
    receipt = stranger.withdraw_claim(args=[milestone]).transact()
    assert not tx_execution_succeeded(receipt)
    assert "[EXPECTED] creator only" in str(leader_return(receipt))
    pace()


def test_an_unknown_milestone_is_rejected_identically_everywhere(oracle):
    receipt = oracle.submit_claim(args=["does-not-exist"]).transact()
    assert not tx_execution_succeeded(receipt)
    assert "[EXPECTED] unknown milestone `does-not-exist`" in str(leader_return(receipt))
    pace()


def test_a_url_outside_the_platform_host_list_is_rejected(oracle, creator):
    deadline = now_ts(oracle) + 3 * DAY
    receipt = oracle.create_milestone(
        args=[
            creator.address,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "github",
            "stars",
            5_000,
            deadline,
            "",
            0,
            0,
            0,
        ]
    ).transact(value=REWARD)
    assert not tx_execution_succeeded(receipt)
    assert "is not covered by platform `github`" in str(leader_return(receipt))
    pace()


# ---------------------------------------------------------------------------
# The comparator views, on chain
# ---------------------------------------------------------------------------


def test_the_equivalence_principle_is_inspectable_on_chain(oracle, live_reading):
    """
    The same comparator the validators ran, replayed as a view so that a
    reviewer can probe the rule without spending a consensus round.
    """
    milestone = live_reading["milestone"]
    value = live_reading["value"]

    agree = read(oracle.preview_agreement(args=[milestone, value, value + 3]))
    assert agree["agree"] is True
    assert agree["leader_zone"] == "pass"

    far = read(oracle.preview_agreement(args=[milestone, value, value // 4]))
    assert far["agree"] is False
    assert "beyond the combined variance allowance" in far["reason"]

    lines = read(oracle.classify_value(args=[milestone, 5_000]))
    assert lines["zone"] == "borderline"
    assert lines["decision_lines"]["certain_pass_at"] == 5_050


def test_the_failure_comparator_is_inspectable_on_chain(oracle):
    def agree(leader, validator):
        return read(oracle.preview_error_agreement(args=[leader, validator]))["agree"]

    assert agree("[EXPECTED] creator only", "[EXPECTED] creator only") is True
    assert agree("[EXPECTED] creator only", "[EXPECTED] sponsor only") is False
    assert agree("[TRANSIENT] a", "[TRANSIENT] b") is True
    assert agree("[LLM_ERROR] a", "[LLM_ERROR] a") is False


# ---------------------------------------------------------------------------
# Escrow exits
# ---------------------------------------------------------------------------


def test_a_milestone_refunds_the_sponsor_after_its_deadline(oracle, gl_client, creator):
    """
    Walks the clock forward for real: a short deadline, a wait, then a refund.
    """
    milestone = create_milestone(oracle, creator.address, threshold=5_000, deadline_in=90)
    sponsor = read(oracle.get_milestone(args=[milestone]))["sponsor"]

    assert read(oracle.is_refundable(args=[milestone]))["refundable"] is False
    time.sleep(100)
    assert read(oracle.is_refundable(args=[milestone]))["refundable"] is True

    before = gl_client.get_balance(sponsor)
    receipt = oracle.refund(args=[milestone]).transact(
        wait_transaction_status=TransactionStatus.FINALIZED,
        wait_triggered_transactions=True,
    )
    assert tx_execution_succeeded(receipt), leader_return(receipt)

    record = read(oracle.get_milestone(args=[milestone]))
    assert record["status"] == "refunded"
    assert gl_client.get_balance(sponsor) - before == REWARD
    pace()


def test_a_sponsor_can_cancel_inside_the_creation_window(oracle, gl_client, creator):
    milestone = create_milestone(
        oracle, creator.address, threshold=5_000, deadline_in=2 * DAY
    )
    sponsor = read(oracle.get_milestone(args=[milestone]))["sponsor"]
    before = gl_client.get_balance(sponsor)

    receipt = oracle.cancel_milestone(args=[milestone]).transact(
        wait_transaction_status=TransactionStatus.FINALIZED,
        wait_triggered_transactions=True,
    )
    assert tx_execution_succeeded(receipt), leader_return(receipt)
    assert read(oracle.get_milestone(args=[milestone]))["status"] == "cancelled"
    assert gl_client.get_balance(sponsor) - before == REWARD
    pace()


def test_escrow_accounting_balances_at_the_end(oracle):
    treasury = read(oracle.get_treasury(args=[]))
    counts = read(oracle.get_status_counts(args=[]))
    print(f"\nfinal treasury: {treasury}")
    print(f"final status counts: {counts}")

    assert treasury["solvent"] is True
    assert treasury["balance_atto"] >= treasury["escrowed_atto"]

    # Every milestone still holding escrow is one that has not settled.
    unsettled = counts["open"] + counts["deferred"] + counts["contested"] + counts["expired"]
    assert treasury["escrowed_atto"] == unsettled * REWARD, (treasury, counts)


# ---------------------------------------------------------------------------
# Administration under consensus
# ---------------------------------------------------------------------------


def test_administration_is_enforced_on_chain(oracle, accounts):
    stranger = oracle.connect(accounts[2])
    receipt = stranger.set_paused(args=[True]).transact()
    assert not tx_execution_succeeded(receipt)
    assert "[EXPECTED] owner only" in str(leader_return(receipt))
    pace()


def test_a_platform_can_be_registered_on_a_live_deployment(oracle):
    receipt = oracle.register_platform(
        args=[
            "devto",
            "DEV Community post",
            "views,likes,comments",
            "dev.to",
            "render_text",
            "2s",
            "reactions",
        ]
    ).transact()
    assert tx_execution_succeeded(receipt), leader_return(receipt)

    record = read(oracle.get_platform(args=["devto"]))
    assert record["metrics"] == ["views", "likes", "comments"]
    assert record["hosts"] == ["dev.to"]
    assert record["enabled"] is True
    assert "reactions" in read(oracle.resolve_keywords(args=["devto", "views"]))
    assert "devto" in [p["key"] for p in read(oracle.list_platforms(args=[]))]
    pace()
