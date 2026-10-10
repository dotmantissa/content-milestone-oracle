"""
Integration tests: real consensus, real pages, real models.

These run against a live GenLayer environment, so every ``submit_claim`` here
is an actual consensus round. The leader fetches the content page and parses the
metric; every validator does the same independently and then applies the
contract's equivalence principle to the leader's observation. Nothing is mocked.

Run against the hosted Studio:

    gltest tests/integration -v -s --network studionet

Notes on running these:

  * StudioNet is gasless and rate limited to 60 requests a minute, so the
    fixtures deploy once per module and the tests pace themselves.
  * The thresholds are derived from what the page actually shows at run time
    rather than hardcoded, because live counts move. ``read_live_metric`` does
    one adjudication to learn the count and the later tests build on it, which
    is also the only honest way to exercise the borderline path against live
    data.
"""

import json
import os
import re
import time
from pathlib import Path

import pytest
from gltest import get_contract_factory
from gltest.assertions import tx_execution_succeeded
from gltest.clients import get_gl_client
from gltest.utils import extract_contract_address
from genlayer_py.types import TransactionStatus

CONTRACT_NAME = "ContentMilestoneOracle"

# A large, long lived repository. The metric is its star count, which is well
# into five figures, so a low threshold is a clear pass under any drift.
GITHUB_URL = "https://github.com/ethereum/go-ethereum"

DAY = 86_400
SIX_HOURS = 21_600
GEN = 10**18

# Gasless transaction execution still requires a funded sponsor for escrow.
REWARD = 10**15

# Constructor arguments used by every test module. A one minute cancellation
# window and a one hour defer window keep the timing assertions quick to read
# without changing any behaviour under test.
CTOR = [
    0,  # fee_bps
    "",  # fee_recipient
    100,  # default_band_bps
    50,  # default_variance_bps
    5,  # default_variance_floor
    3_600,  # defer_seconds
    4,  # max_defer_rounds
    2 * DAY,  # resolution_grace_seconds
    60,  # cancel_window_seconds
    0,  # min_reward_atto
    365 * DAY,  # max_deadline_horizon
]


def pace(seconds: float = 4.0) -> None:
    """
    Stay inside the hosted Studio's per minute request budget.

    StudioNet is shared infrastructure: 60 requests a minute, 1000 an hour, and
    at most 32 in-flight transactions per sender. Exceeding it does not return a
    clean error, it drops the connection, so the suite paces itself rather than
    firing as fast as it can.
    """
    time.sleep(seconds)


def retry_rpc(action, *, attempts: int = 4, backoff: float = 6.0):
    """
    Retry a read through a transient RPC failure.

    A dropped connection to a shared public endpoint says nothing about the
    contract, so a view call retries instead of failing the test. Writes are
    deliberately not retried: a connection reset mid-submission is ambiguous,
    and re-sending could double-submit.
    """
    last = None
    for attempt in range(attempts):
        try:
            return action()
        except Exception as err:  # noqa: BLE001 - any transport failure is retryable
            text = str(err).lower()
            retryable = any(
                marker in text
                for marker in (
                    "connection",
                    "timed out",
                    "timeout",
                    "name resolution",
                    "429",
                    "-32429",
                    "temporarily",
                )
            )
            if not retryable:
                raise
            last = err
            time.sleep(backoff * (attempt + 1))
    raise AssertionError(f"RPC still failing after {attempts} attempts: {last}")


def deployer_account():
    """
    The account to deploy from.

    Defaults to the network's configured account so the suite runs with no
    setup. Set ``DEPLOYER_PRIVATE_KEY`` to deploy from a specific account, which
    is how the published deployment was made. No key is ever committed.
    """
    key = os.environ.get("DEPLOYER_PRIVATE_KEY")
    if not key:
        return None
    from genlayer_py import create_account

    return create_account(account_private_key=key)


@pytest.fixture(scope="module")
def oracle(gl_client):
    """One deployment shared by the module, to stay inside the rate limit."""
    factory = get_contract_factory(CONTRACT_NAME)
    account = deployer_account()

    # Submit exactly once and save the ID before waiting. Receipt polling is
    # read-only and can retry safely; an ambiguous submission cannot.
    tx_id = gl_client.deploy_contract(code=factory.contract_code, args=CTOR, account=account)
    checkpoint("deploy", tx_id)
    receipt = wait_receipt(tx_id)
    assert tx_execution_succeeded(receipt), leader_return(receipt)
    assert_consensus(receipt)
    address = extract_contract_address(receipt)
    checkpoint("deploy", tx_id, receipt=receipt, address=address)
    contract = factory.build_contract(contract_address=address, account=account)
    print(f"\ndeployed {CONTRACT_NAME} at {contract.address}")
    return contract


def read(bound_call):
    """Run a view call, retrying through a transient RPC failure."""
    return retry_rpc(lambda: bound_call.call())


def now_ts(oracle) -> int:
    return int(read(oracle.now_ts(args=[])))


def checkpoint(operation, tx_id, *, receipt=None, address=None):
    """Keep non-secret transaction recovery metadata in ignored artifacts."""
    path = Path("artifacts/live-run.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(path.read_text()) if path.exists() else []
    entry = {"operation": operation, "transaction_id": str(tx_id), "at": int(time.time())}
    if address is not None:
        entry["address"] = address
    if receipt is not None:
        entry["execution_succeeded"] = tx_execution_succeeded(receipt)
        entry["votes"] = receipt.get("consensus_data", {}).get("votes", {})
    state.append(entry)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(path)
    print(f"\n{operation}: {tx_id}", flush=True)


def wait_receipt(tx_id, *, settle=False):
    client = get_gl_client()
    receipt = retry_rpc(lambda: client.wait_for_transaction_receipt(
        transaction_hash=tx_id,
        status=TransactionStatus.FINALIZED if settle else TransactionStatus.ACCEPTED,
        interval=6_000,
        retries=60,
    ))
    if settle:
        for child in receipt.get("triggered_transactions", []):
            child_receipt = retry_rpc(lambda: client.wait_for_transaction_receipt(
                transaction_hash=child, status=TransactionStatus.FINALIZED, interval=6_000, retries=60
            ))
            if child_receipt.get("consensus_data") is not None:
                assert tx_execution_succeeded(child_receipt), child_receipt
            else:
                # Studio native transfers have no GenVM consensus receipt.
                # Verify the native value credit separately; the
                # caller also checks the beneficiary's actual balance delta.
                assert child_receipt.get("type") == 0, child_receipt
                assert child_receipt.get("value_credited") is True, child_receipt
    return receipt


def transact(oracle, method, args, *, value=0, settle=False):
    """Send once, checkpoint, then safely poll the same transaction ID."""
    tx_id = get_gl_client().write_contract(
        address=oracle.address, function_name=method, args=args,
        account=oracle.account, value=value,
    )
    checkpoint(method, tx_id)
    receipt = wait_receipt(tx_id, settle=settle)
    checkpoint(method, tx_id, receipt=receipt)
    assert_consensus(receipt)
    return receipt


def assert_consensus(receipt):
    """Missing votes must not silently pass as validator agreement."""
    votes = receipt.get("consensus_data", {}).get("votes", {})
    assert votes, "no validator votes recorded; run with leader_only: false"
    assert "disagree" not in votes.values(), votes
    assert "agree" in votes.values(), votes
    return votes


def wait_until(oracle, timestamp, *, timeout=180):
    """Wait against the contract clock with bounded, short polling sleeps."""
    stop = time.monotonic() + timeout
    while True:
        remaining = timestamp - now_ts(oracle)
        if remaining <= 0:
            return
        assert time.monotonic() < stop, f"contract clock did not reach {timestamp}"
        time.sleep(min(remaining, 10))


def create_milestone(
    oracle,
    creator,
    *,
    threshold,
    url=GITHUB_URL,
    platform="github",
    metric="stars",
    deadline_in=7 * DAY,
    reward=REWARD,
    memo="",
    band_bps=0,
    variance_bps=0,
    variance_floor=0,
):
    """Create a milestone on chain and return its id."""
    deadline = now_ts(oracle) + deadline_in
    receipt = transact(oracle, "create_milestone", [
            creator,
            url,
            platform,
            metric,
            threshold,
            deadline,
            memo,
            band_bps,
            variance_bps,
            variance_floor,
        ], value=reward)
    assert tx_execution_succeeded(receipt), receipt
    pace()
    # The receipt identifies this call even if another sponsor creates a
    # milestone between acceptance and the next view call.
    returned = leader_return(receipt)
    assert isinstance(returned, str), returned
    match = re.fullmatch(r'"?(m[1-9][0-9]*)"?', returned)
    assert match, f"creation did not return a milestone ID: {returned}"
    return match.group(1)


def leader_return(receipt):
    """The value the contract returned, as recorded in the leader receipt."""
    result = receipt["consensus_data"]["leader_receipt"][0].get("result")
    if isinstance(result, dict) and "payload" in result:
        payload = result["payload"]
        return payload.get("readable") if isinstance(payload, dict) else payload
    return result
