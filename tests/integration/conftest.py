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

import os
import time

import pytest
from gltest import get_contract_factory
from gltest.assertions import tx_execution_succeeded

CONTRACT_NAME = "ContentMilestoneOracle"

# A large, long lived repository. The metric is its star count, which is well
# into five figures, so a low threshold is a clear pass under any drift.
GITHUB_URL = "https://github.com/ethereum/go-ethereum"

DAY = 86_400
SIX_HOURS = 21_600
GEN = 10**18

# Studio deployments are gasless, so escrow amounts here are nominal.
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
def oracle():
    """One deployment shared by the module, to stay inside the rate limit."""
    factory = get_contract_factory(CONTRACT_NAME)
    account = deployer_account()

    def deploy():
        if account is not None:
            return factory.deploy(args=CTOR, account=account)
        return factory.deploy(args=CTOR)

    contract = retry_rpc(deploy)
    print(f"\ndeployed {CONTRACT_NAME} at {contract.address}")
    return contract


def read(bound_call):
    """Run a view call, retrying through a transient RPC failure."""
    return retry_rpc(lambda: bound_call.call())


def now_ts(oracle) -> int:
    return int(read(oracle.now_ts(args=[])))


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
    receipt = oracle.create_milestone(
        args=[
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
        ]
    ).transact(value=reward)
    assert tx_execution_succeeded(receipt), receipt
    pace()
    # The id is sequential, so the newest milestone is the last one indexed.
    count = int(read(oracle.milestone_count(args=[])))
    return f"m{count}"


def leader_return(receipt):
    """The value the contract returned, as recorded in the leader receipt."""
    return receipt["consensus_data"]["leader_receipt"][0].get("result")
