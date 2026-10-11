"""
Milestone creation: input validation, escrow, host checks and duplicate
rejection.

Everything a sponsor can get wrong is rejected before any money is locked, and
every rejection carries the deterministic error prefix so that validators
compare the failure verbatim.
"""

import pytest

from conftest import (
    DAY,
    GEN,
    GENERIC_URL,
    GITHUB_URL,
    YOUTUBE_URL,
)


def create(
    direct_vm,
    contract,
    sponsor,
    creator,
    *,
    url=GITHUB_URL,
    platform="github",
    metric="stars",
    threshold=10_000,
    deadline_offset=7 * DAY,
    reward=5 * GEN,
    memo="",
    band_bps=0,
    variance_bps=0,
    variance_floor=0,
):
    """Call ``create_milestone`` directly so each argument can be perturbed."""
    now = contract.now_ts()
    direct_vm.sender = sponsor
    direct_vm.value = reward
    try:
        result = contract.create_milestone(
            creator,
            url,
            platform,
            metric,
            threshold,
            now + deadline_offset,
            memo,
            band_bps,
            variance_bps,
            variance_floor,
        )
        if isinstance(result, dict):
            from genlayer import gl
            raise gl.vm.UserError(result["reason"])
        return result
    finally:
        direct_vm.value = 0


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_creation_records_every_term_the_sponsor_pinned(
    oracle, direct_vm, direct_alice, direct_bob
):
    mid = create(
        direct_vm,
        oracle,
        direct_alice,
        direct_bob,
        threshold=25_000,
        memo="Q2 developer grant",
    )
    record = oracle.get_milestone(mid)

    assert record["milestone_id"] == mid
    assert record["sponsor"] == direct_alice.as_hex
    assert record["creator"] == direct_bob.as_hex
    assert record["content_url"] == GITHUB_URL
    assert record["platform"] == "github"
    assert record["metric"] == "stars"
    assert record["threshold"] == 25_000
    assert record["reward_atto"] == 5 * GEN
    assert record["status"] == "open"
    assert record["attempts"] == 0
    assert record["memo"] == "Q2 developer grant"
    assert record["created_at"] == oracle.now_ts()


def test_creation_escrows_the_value_sent_with_the_call(
    oracle, direct_vm, direct_alice, direct_bob
):
    before = oracle.get_treasury()["escrowed_atto"]
    create(direct_vm, oracle, direct_alice, direct_bob, reward=7 * GEN)
    assert oracle.get_treasury()["escrowed_atto"] == before + 7 * GEN


def test_ids_are_sequential_and_indexed(oracle, direct_vm, direct_alice, direct_bob):
    first = create(direct_vm, oracle, direct_alice, direct_bob, threshold=1_000)
    second = create(direct_vm, oracle, direct_alice, direct_bob, threshold=2_000)
    assert [first, second] == ["m1", "m2"]
    assert oracle.milestone_count() == 2
    assert oracle.count_by_sponsor(direct_alice) == 2
    assert oracle.count_by_creator(direct_bob) == 2


def test_defaults_are_copied_onto_the_milestone(
    oracle, direct_vm, direct_alice, direct_bob
):
    mid = create(direct_vm, oracle, direct_alice, direct_bob)
    record = oracle.get_milestone(mid)
    config = oracle.get_config()
    assert record["band_bps"] == config["default_band_bps"]
    assert record["variance_bps"] == config["default_variance_bps"]
    assert record["variance_floor"] == config["default_variance_floor"]


def test_later_default_changes_do_not_touch_existing_milestones(
    oracle, direct_vm, direct_owner, direct_alice, direct_bob
):
    """A sponsor's terms cannot be rewritten under them."""
    mid = create(direct_vm, oracle, direct_alice, direct_bob)
    direct_vm.sender = direct_owner
    oracle.set_consensus_params(2_000, 1_000, 100, 3_600, 2, 0)
    assert oracle.get_milestone(mid)["band_bps"] == 100
    assert oracle.get_config()["default_band_bps"] == 2_000


# ---------------------------------------------------------------------------
# Escrow preconditions
# ---------------------------------------------------------------------------


def test_a_milestone_with_no_reward_is_refused(
    oracle, direct_vm, direct_alice, direct_bob
):
    with direct_vm.expect_revert("a reward must be escrowed"):
        create(direct_vm, oracle, direct_alice, direct_bob, reward=0)


def test_a_reward_below_the_configured_minimum_is_refused(
    deploy_oracle, direct_vm, direct_alice, direct_bob
):
    contract = deploy_oracle(min_reward_atto=GEN)
    with direct_vm.expect_revert("below the minimum"):
        create(direct_vm, contract, direct_alice, direct_bob, reward=GEN // 2)
    assert create(direct_vm, contract, direct_alice, direct_bob, reward=GEN) == "m1"


# ---------------------------------------------------------------------------
# Beneficiary validation
# ---------------------------------------------------------------------------


def test_the_zero_address_cannot_be_a_creator(oracle, direct_vm, direct_alice):
    with direct_vm.expect_revert("creator must not be the zero address"):
        create(direct_vm, oracle, direct_alice, bytes(20))


def test_the_contract_itself_cannot_be_a_creator(oracle, direct_vm, direct_alice):
    with direct_vm.expect_revert("creator must not be this contract"):
        create(direct_vm, oracle, direct_alice, direct_vm._contract_address)


def test_a_sponsor_may_fund_their_own_milestone(
    oracle, direct_vm, direct_alice
):
    """Self-funded milestones are a legitimate public commitment, not a bug."""
    assert create(direct_vm, oracle, direct_alice, direct_alice) == "m1"


# ---------------------------------------------------------------------------
# URL and platform validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,fragment",
    [
        ("", "content_url must be 1-512 chars"),
        ("https://x.com/a" + "b" * 600, "content_url must be 1-512 chars"),
        ("http://github.com/x", "must be a plain https URL"),
        ("github.com/x", "must be a plain https URL"),
        ("https://evil.com@github.com/x", "must be a plain https URL"),
        ("https://localhost/x", "must be a plain https URL"),
    ],
)
def test_unsafe_urls_are_refused(oracle, direct_vm, direct_alice, direct_bob, url, fragment):
    with direct_vm.expect_revert(fragment):
        create(direct_vm, oracle, direct_alice, direct_bob, url=url)


def test_a_url_outside_the_platform_host_list_is_refused(
    oracle, direct_vm, direct_alice, direct_bob
):
    with direct_vm.expect_revert("is not covered by platform `github`"):
        create(direct_vm, oracle, direct_alice, direct_bob, url=YOUTUBE_URL)


def test_a_platform_subdomain_is_accepted(oracle, direct_vm, direct_alice, direct_bob):
    assert create(
        direct_vm,
        oracle,
        direct_alice,
        direct_bob,
        url="https://gist.github.com/someone/abc123",
    ) == "m1"


def test_the_generic_platform_accepts_any_https_host(
    oracle, direct_vm, direct_alice, direct_bob
):
    assert create(
        direct_vm,
        oracle,
        direct_alice,
        direct_bob,
        url=GENERIC_URL,
        platform="generic_web",
        metric="views",
    ) == "m1"


def test_an_unknown_platform_is_refused(oracle, direct_vm, direct_alice, direct_bob):
    with direct_vm.expect_revert("unknown platform `tiktok`"):
        create(direct_vm, oracle, direct_alice, direct_bob, platform="tiktok")


def test_a_disabled_platform_is_refused(
    oracle, direct_vm, direct_owner, direct_alice, direct_bob
):
    direct_vm.sender = direct_owner
    oracle.set_platform_enabled("github", False)
    with direct_vm.expect_revert("platform `github` is disabled"):
        create(direct_vm, oracle, direct_alice, direct_bob)


def test_check_url_previews_the_same_decision(oracle):
    good = oracle.check_url("github", GITHUB_URL)
    assert good["ok"] is True
    assert good["host"] == "github.com"
    assert good["fetch_mode"] == "render_text"

    wrong_host = oracle.check_url("github", YOUTUBE_URL)
    assert wrong_host["ok"] is False
    assert "not covered" in wrong_host["reason"]

    not_https = oracle.check_url("github", "http://github.com/x")
    assert not_https["ok"] is False
    assert not_https["host"] == ""

    unknown = oracle.check_url("tiktok", GITHUB_URL)
    assert unknown["ok"] is False
    assert unknown["reason"] == "unknown platform"


# ---------------------------------------------------------------------------
# Metric validation
# ---------------------------------------------------------------------------


def test_a_metric_the_platform_does_not_support_is_refused(
    oracle, direct_vm, direct_alice, direct_bob
):
    with direct_vm.expect_revert("does not support metric `subscribers`"):
        create(direct_vm, oracle, direct_alice, direct_bob, metric="subscribers")


def test_an_unregistered_metric_is_refused(oracle, direct_vm, direct_alice, direct_bob):
    with direct_vm.expect_revert("does not support metric `vibes`"):
        create(direct_vm, oracle, direct_alice, direct_bob, metric="vibes")


def test_metric_names_are_case_insensitive(oracle, direct_vm, direct_alice, direct_bob):
    mid = create(direct_vm, oracle, direct_alice, direct_bob, metric="STARS")
    assert oracle.get_milestone(mid)["metric"] == "stars"


# ---------------------------------------------------------------------------
# Threshold and deadline validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("threshold", [0, -1, 10**16])
def test_an_out_of_range_threshold_is_refused(
    oracle, direct_vm, direct_alice, direct_bob, threshold
):
    with direct_vm.expect_revert("threshold must be between"):
        create(direct_vm, oracle, direct_alice, direct_bob, threshold=threshold)


@pytest.mark.parametrize("offset", [0, -1, -DAY])
def test_a_deadline_in_the_past_is_refused(
    oracle, direct_vm, direct_alice, direct_bob, offset
):
    with direct_vm.expect_revert("deadline must be in the future"):
        create(direct_vm, oracle, direct_alice, direct_bob, deadline_offset=offset)


def test_a_deadline_beyond_the_horizon_is_refused(
    oracle, direct_vm, direct_alice, direct_bob
):
    with direct_vm.expect_revert("beyond the"):
        create(
            direct_vm, oracle, direct_alice, direct_bob, deadline_offset=400 * DAY
        )


def test_an_overlong_memo_is_refused(oracle, direct_vm, direct_alice, direct_bob):
    with direct_vm.expect_revert("memo exceeds"):
        create(direct_vm, oracle, direct_alice, direct_bob, memo="x" * 281)


# ---------------------------------------------------------------------------
# Consensus parameter overrides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,kwargs",
    [
        ("band_bps", {"band_bps": 5_001}),
        ("band_bps", {"band_bps": -2}),
        ("variance_bps", {"variance_bps": 5_001}),
        ("variance_bps", {"variance_bps": -2}),
        ("variance_floor", {"variance_floor": -2}),
    ],
)
def test_out_of_range_overrides_are_refused(
    oracle, direct_vm, direct_alice, direct_bob, field, kwargs
):
    with direct_vm.expect_revert(f"{field} must be -1, 0, or between"):
        create(direct_vm, oracle, direct_alice, direct_bob, **kwargs)


def test_overrides_are_stored_verbatim(oracle, direct_vm, direct_alice, direct_bob):
    mid = create(
        direct_vm,
        oracle,
        direct_alice,
        direct_bob,
        band_bps=250,
        variance_bps=300,
        variance_floor=42,
    )
    record = oracle.get_milestone(mid)
    assert record["band_bps"] == 250
    assert record["variance_bps"] == 300
    assert record["variance_floor"] == 42


# ---------------------------------------------------------------------------
# Duplicate rejection
# ---------------------------------------------------------------------------


def test_an_identical_milestone_is_refused(oracle, direct_vm, direct_alice, direct_bob):
    first = create(direct_vm, oracle, direct_alice, direct_bob)
    with direct_vm.expect_revert(f"already exists as `{first}`"):
        create(direct_vm, oracle, direct_alice, direct_bob)


def test_changing_any_term_makes_it_a_different_milestone(
    oracle, direct_vm, direct_alice, direct_bob, direct_charlie
):
    create(direct_vm, oracle, direct_alice, direct_bob)
    assert create(direct_vm, oracle, direct_alice, direct_bob, threshold=10_001)
    assert create(direct_vm, oracle, direct_alice, direct_bob, deadline_offset=8 * DAY)
    assert create(direct_vm, oracle, direct_alice, direct_charlie)
    assert create(direct_vm, oracle, direct_bob, direct_bob)
    assert create(
        direct_vm, oracle, direct_alice, direct_bob, url=GITHUB_URL + "/tree/main"
    )
    assert create(direct_vm, oracle, direct_alice, direct_bob, metric="forks")
    assert oracle.milestone_count() == 7


def test_the_reward_is_not_part_of_the_fingerprint(
    oracle, direct_vm, direct_alice, direct_bob
):
    """Two sponsors cannot sidestep deduplication by nudging the reward."""
    create(direct_vm, oracle, direct_alice, direct_bob, reward=5 * GEN)
    with direct_vm.expect_revert("already exists"):
        create(direct_vm, oracle, direct_alice, direct_bob, reward=6 * GEN)


# ---------------------------------------------------------------------------
# Pause
# ---------------------------------------------------------------------------


def test_creation_is_blocked_while_paused(
    oracle, direct_vm, direct_owner, direct_alice, direct_bob
):
    direct_vm.sender = direct_owner
    oracle.set_paused(True)
    with direct_vm.expect_revert("contract is paused"):
        create(direct_vm, oracle, direct_alice, direct_bob)

    direct_vm.sender = direct_owner
    oracle.set_paused(False)
    assert create(direct_vm, oracle, direct_alice, direct_bob) == "m1"
