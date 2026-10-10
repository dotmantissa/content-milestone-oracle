"""
Shared fixtures for the Content Milestone Oracle direct-mode tests.

Direct mode runs the contract in process with no server and no consensus. That
makes it the right place to exercise every deterministic branch (input
validation, access control, escrow accounting, the state machine) plus the
leader half of each non-deterministic block against mocked pages and model
responses.

The validator half is exercised here too. ``gltest`` captures the validator
closure every time the contract calls ``gl.vm.run_nondet``, so a test can mock
one page for the leader, swap the mocks, and then run the real validator
function against a different reading via ``direct_vm.run_validator()``. That is
genuine coverage of the equivalence principle, not a stand-in for it.

``tests/integration`` then runs the same paths through real validators.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

CONTRACT = "contracts/content_milestone_oracle.py"
CONTRACT_MODULE = "_contract_content_milestone_oracle"

GEN = 10**18
DAY = 86400
SIX_HOURS = 21600

GITHUB_URL = "https://github.com/genlayerlabs/genvm"
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
GENERIC_URL = "https://blog.example.com/post/42"

DEFAULT_CTOR = {
    "fee_bps": 0,
    "fee_recipient": "",
    "default_band_bps": 100,
    "default_variance_bps": 50,
    "default_variance_floor": 5,
    "defer_seconds": SIX_HOURS,
    "max_defer_rounds": 4,
    "resolution_grace_seconds": 2 * DAY,
    "cancel_window_seconds": 3600,
    "min_reward_atto": 0,
    "max_deadline_horizon": 365 * DAY,
}


# ---------------------------------------------------------------------------
# Page builders. Shapes follow what the real pages render.
# ---------------------------------------------------------------------------


def github_page(stars, forks="412", watchers="87") -> str:
    return (
        "Skip to content genlayerlabs / genvm Public "
        f"Notifications Fork {forks} Star {stars} "
        "Code Issues 23 Pull requests 7 Actions Projects Wiki Security Insights "
        "GenVM is the execution environment for GenLayer Intelligent Contracts. "
        f"{stars} stars {forks} forks {watchers} watching "
        "Releases 41 Contributors 19 Languages Rust 71.2% Python 18.4%"
    )


def youtube_page(views, likes="21K", subs="1.2M") -> str:
    return (
        f"Never Gonna Give You Up {views} views 2 years ago "
        f"{likes} likes Share Download Save "
        f"RickAstleyVEVO {subs} subscribers Subscribe "
        "Comments 184K Sort by"
    )


def generic_page(views) -> str:
    return (
        "Example Engineering Blog How we cut p99 latency in half "
        "Published 12 March 2026 by the platform team. "
        f"{views} views 18 min read Share this post"
    )


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def mock_page(direct_vm, url: str, body: str) -> None:
    """Serve ``body`` for any fetch of ``url``, render or plain GET alike."""
    direct_vm.mock_web(re.escape(url), {"status": 200, "body": body})


def mock_status(direct_vm, url: str, status: int, body: str = "") -> None:
    direct_vm.mock_web(re.escape(url), {"status": status, "body": body})


def mock_reader(direct_vm, value, *, found=True, display="", reason="read from page"):
    """Mock the metric reader's response."""
    direct_vm.mock_llm(
        r"METRIC REQUESTED",
        json.dumps(
            {"found": found, "value": value, "display": display, "reason": reason}
        ),
    )


def mock_reader_raw(direct_vm, raw: str) -> None:
    """Mock a malformed or awkward reader response verbatim."""
    direct_vm.mock_llm(r"METRIC REQUESTED", raw)


def set_scene(direct_vm, url: str, page: str, value, **kwargs) -> None:
    """Point the page fetch and the reader at one consistent reading."""
    direct_vm.clear_mocks()
    mock_page(direct_vm, url, page)
    mock_reader(direct_vm, value, **kwargs)


# ---------------------------------------------------------------------------
# Time control
#
# ``direct_vm.warp`` repoints the clock the contract reads, which is how the
# deadline, cancellation window, defer window and resolution grace window are
# all exercised without waiting.
# ---------------------------------------------------------------------------


def warp_to(direct_vm, unix_seconds: int) -> None:
    moment = datetime.fromtimestamp(int(unix_seconds), tz=timezone.utc)
    direct_vm.warp(moment.isoformat().replace("+00:00", "Z"))


def warp_by(direct_vm, contract, delta_seconds: int) -> int:
    target = contract.now_ts() + int(delta_seconds)
    warp_to(direct_vm, target)
    return target


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def sdk_on_path():
    """
    Put the pinned GenVM SDK on ``sys.path`` before any fixture needs it.

    ``gltest`` normally adds it as a side effect of deploying, which leaves
    ``create_address`` returning raw bytes or ``Address`` instances depending on
    whether some earlier test in the session happened to deploy first. Doing it
    up front makes the address fixtures deterministic.
    """
    from gltest.direct.sdk_loader import setup_sdk_paths

    setup_sdk_paths(Path(CONTRACT).resolve())


def _as_address(value):
    """Normalise a test address to an ``Address`` instance."""
    from genlayer.py.types import Address

    if isinstance(value, Address):
        return value
    return Address(bytes(value))


def _address_fixture(seed):
    """An address fixture that is always an ``Address``, never raw bytes."""

    @pytest.fixture
    def fixture(sdk_on_path):
        from gltest.direct.loader import create_address

        return _as_address(create_address(seed))

    return fixture


direct_owner = _address_fixture("default_sender")
direct_alice = _address_fixture("alice")
direct_bob = _address_fixture("bob")
direct_charlie = _address_fixture("charlie")


@pytest.fixture
def zero_address(sdk_on_path):
    from genlayer.py.types import Address

    return Address(bytes(20))


@pytest.fixture
def deploy_oracle(direct_vm, direct_deploy, direct_owner):
    """Factory for an oracle deployed with explicit constructor arguments."""

    def deploy(**overrides):
        args = dict(DEFAULT_CTOR)
        args.update(overrides)
        direct_vm.sender = direct_owner
        contract = direct_deploy(CONTRACT, *args.values())
        # Direct mode does not credit the contract for payable calls, so seed a
        # generous balance to keep the solvency views meaningful.
        direct_vm.deal(direct_vm._contract_address, 10_000 * GEN)
        return contract

    return deploy


@pytest.fixture
def oracle(deploy_oracle):
    """An oracle with the default configuration."""
    return deploy_oracle()


@pytest.fixture
def helpers(oracle):
    """
    The contract's module-level pure helpers.

    The scanner, the zone classifier and the comparator are deliberately pure
    functions at module scope so they can be tested in isolation, which is
    where the fiddly numeric edge cases belong.
    """
    return sys.modules[CONTRACT_MODULE]


@pytest.fixture
def make_milestone(direct_vm):
    """Create a milestone as ``sponsor`` for ``creator`` and return its id."""

    def create(
        contract,
        sponsor,
        creator,
        *,
        url=GITHUB_URL,
        platform="github",
        metric="stars",
        threshold=10_000,
        deadline_in=7 * DAY,
        reward=5 * GEN,
        memo="",
        band_bps=0,
        variance_bps=0,
        variance_floor=0,
    ):
        now = contract.now_ts()
        direct_vm.sender = sponsor
        direct_vm.value = reward
        try:
            return contract.create_milestone(
                creator,
                url,
                platform,
                metric,
                threshold,
                now + deadline_in,
                memo,
                band_bps,
                variance_bps,
                variance_floor,
            )
        finally:
            direct_vm.value = 0

    return create


@pytest.fixture
def claim(direct_vm):
    """Submit a claim as the creator with a given page and reading."""

    def run(contract, creator, milestone_id, *, url, page, value, **kwargs):
        set_scene(direct_vm, url, page, value, **kwargs)
        direct_vm.sender = creator
        return contract.submit_claim(milestone_id)

    return run
