"""
The equivalence principle.

This is the part of the primitive that decides whether a payout happens, so it
gets tested three ways:

  * ``_classify`` / ``_variance_allowance`` / ``_agreement_report`` as pure
    functions, across every branch;
  * through ``preview_agreement``, the view that exposes the comparator on chain
    so that reviewers and integrators can probe it without a consensus round;
  * through ``direct_vm.run_validator()``, which replays the real validator
    closure that the contract handed to ``gl.vm.run_nondet``. The leader reads
    one page, the mocks are swapped, and the validator reads a different one.
    That is the actual consensus path, not a stand-in for it.
"""

import pytest

from conftest import GITHUB_URL, github_page, mock_page, mock_reader, set_scene

# Defaults used throughout: threshold 10000, band 100 bps, variance 50 bps,
# variance floor 5. So the band is 100 wide either side, the certain-pass line
# is 10100, the certain-fail line is 9900, and a reading near 10000 carries an
# allowance of 50.
THRESHOLD = 10_000


# ---------------------------------------------------------------------------
# Band arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,zone",
    [
        (0, "fail"),
        (9_000, "fail"),
        (9_899, "fail"),
        (9_900, "borderline"),
        (10_000, "borderline"),
        (10_099, "borderline"),
        (10_100, "pass"),
        (50_000, "pass"),
    ],
)
def test_classify_places_readings_against_the_band(helpers, value, zone):
    assert helpers._classify(value, THRESHOLD, 100) == zone


def test_a_zero_band_collapses_to_a_plain_comparison(helpers):
    assert helpers._classify(9_999, THRESHOLD, 0) == "fail"
    assert helpers._classify(10_000, THRESHOLD, 0) == "pass"
    assert helpers._classify(10_001, THRESHOLD, 0) == "pass"


def test_classify_is_monotonic(helpers):
    ranks = [
        helpers._ZONE_RANK[helpers._classify(value, THRESHOLD, 100)]
        for value in range(9_800, 10_200, 7)
    ]
    assert ranks == sorted(ranks)


def test_decision_lines_state_the_two_numbers_that_matter(helpers):
    lines = helpers._decision_lines(THRESHOLD, 100)
    assert lines == {
        "threshold": 10_000,
        "band": 100,
        "certain_pass_at": 10_100,
        "certain_fail_below": 9_900,
    }


def test_band_width_scales_with_the_threshold(helpers):
    assert helpers._band_width(1_000_000, 100) == 10_000
    assert helpers._band_width(10, 100) == 0  # integer math floors small bands


# ---------------------------------------------------------------------------
# Variance allowance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 5),  # the floor protects small counts
        (100, 5),
        (1_000, 5),
        (10_000, 50),
        (1_000_000, 5_000),
    ],
)
def test_variance_allowance_is_proportional_with_a_floor(helpers, value, expected):
    assert helpers._variance_allowance(value, 50, 5) == expected


def test_variance_allowance_honours_an_explicit_zero(helpers):
    assert helpers._variance_allowance(10_000, 0, 0) == 0


# ---------------------------------------------------------------------------
# The comparator, gate by gate
# ---------------------------------------------------------------------------


def observation(value, zone=None, found=True, helpers=None):
    if zone is None:
        zone = helpers._classify(value, THRESHOLD, 100)
    return {"found": found, "value": value, "zone": zone}


def report(helpers, leader_value, validator_value, **leader_overrides):
    leader = observation(leader_value, helpers=helpers)
    leader.update(leader_overrides)
    return helpers._agreement_report(
        leader, observation(validator_value, helpers=helpers), THRESHOLD, 100, 50, 5
    )


def test_two_clear_passes_agree(helpers):
    result = report(helpers, 12_431, 12_470)
    assert result["agree"] is True
    assert result["leader_zone"] == "pass"
    assert result["gap"] == 39
    assert result["combined_allowance"] == 124


def test_two_clear_fails_agree(helpers):
    result = report(helpers, 8_000, 8_020)
    assert result["agree"] is True
    assert result["leader_zone"] == "fail"


def test_two_borderline_readings_agree(helpers):
    """
    The case the primitive exists for: both validators land inside the band, so
    they agree that the outcome cannot be called, and the contract defers.
    """
    result = report(helpers, 10_050, 10_020)
    assert result["agree"] is True
    assert result["leader_zone"] == "borderline"


def test_readings_that_straddle_the_threshold_still_agree_on_borderline(helpers):
    """9990 and 10010 straddle 10000 but both sit inside the band."""
    result = report(helpers, 10_010, 9_990)
    assert result["agree"] is True
    assert result["leader_zone"] == "borderline"


def test_readings_beyond_the_combined_allowance_disagree(helpers):
    result = report(helpers, 50_000, 12_000)
    assert result["agree"] is False
    assert "beyond the combined variance allowance" in result["reason"]


def test_a_leader_conclusion_the_validator_cannot_support_disagrees(helpers):
    """
    Leader reads 10130 (a clear pass), validator reads 10040 (borderline). The
    two are within mutual tolerance, but the validator's own reading cannot
    support clearing the certain-pass line, so it refuses.
    """
    result = report(helpers, 10_130, 10_040)
    assert result["gap"] <= result["combined_allowance"]
    assert result["agree"] is False
    assert "only supports" in result["reason"]
    assert result["span_low_zone"] == "borderline"
    assert result["span_high_zone"] == "borderline"


def test_the_reverse_straddle_also_disagrees(helpers):
    """Leader reads borderline, validator's reading only supports a clear pass."""
    result = report(helpers, 10_099, 10_190)
    assert result["agree"] is False
    assert result["leader_zone"] == "borderline"
    assert result["span_low_zone"] == "pass"


def test_a_leader_that_mislabels_its_own_reading_disagrees(helpers):
    result = report(helpers, 8_000, 8_020, zone="pass")
    assert result["agree"] is False
    assert "classifies as `fail`" in result["reason"]


def test_a_leader_claiming_an_impossible_zone_disagrees(helpers):
    result = report(helpers, 12_431, 12_470, zone="definitely")
    assert result["agree"] is False
    assert "not an adjudication zone" in result["reason"]


def test_readability_mismatch_disagrees(helpers):
    found = observation(12_431, helpers=helpers)
    missing = {"found": False, "value": 0, "zone": "unreadable"}
    assert helpers._agreement_report(found, missing, THRESHOLD, 100, 50, 5)["agree"] is False
    assert helpers._agreement_report(missing, found, THRESHOLD, 100, 50, 5)["agree"] is False


def test_both_sides_finding_no_metric_agree(helpers):
    missing = {"found": False, "value": 0, "zone": "unreadable"}
    result = helpers._agreement_report(missing, dict(missing), THRESHOLD, 100, 50, 5)
    assert result["agree"] is True
    assert "found no such metric" in result["reason"]


def test_an_unreadable_observation_must_be_labelled_unreadable(helpers):
    bad = {"found": False, "value": 0, "zone": "fail"}
    missing = {"found": False, "value": 0, "zone": "unreadable"}
    assert helpers._agreement_report(bad, missing, THRESHOLD, 100, 50, 5)["agree"] is False


@pytest.mark.parametrize(
    "leader",
    [
        "not a dict",
        42,
        None,
        {},
        {"found": True, "value": 1},
        {"found": True, "zone": "pass"},
        {"value": 1, "zone": "pass"},
    ],
)
def test_a_malformed_leader_record_disagrees(helpers, leader):
    result = helpers._agreement_report(
        leader, observation(12_431, helpers=helpers), THRESHOLD, 100, 50, 5
    )
    assert result["agree"] is False
    assert result["reason"]


@pytest.mark.parametrize("value", [True, "12431", 1.5, -1, 10**16])
def test_a_non_integer_or_out_of_range_leader_value_disagrees(helpers, value):
    leader = {"found": True, "value": value, "zone": "pass"}
    result = helpers._agreement_report(
        leader, observation(12_431, helpers=helpers), THRESHOLD, 100, 50, 5
    )
    assert result["agree"] is False


def test_zero_tolerance_demands_an_exact_match(helpers):
    leader = {"found": True, "value": 10_000, "zone": "pass"}
    exact = {"found": True, "value": 10_000, "zone": "pass"}
    off_by_one = {"found": True, "value": 10_001, "zone": "pass"}
    assert helpers._agreement_report(leader, exact, THRESHOLD, 0, 0, 0)["agree"] is True
    assert (
        helpers._agreement_report(leader, off_by_one, THRESHOLD, 0, 0, 0)["agree"]
        is False
    )


# ---------------------------------------------------------------------------
# The comparator as exposed on chain
# ---------------------------------------------------------------------------


def test_preview_agreement_matches_the_pure_comparator(
    oracle, helpers, make_milestone, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    for leader_value, validator_value in [
        (12_431, 12_470),
        (8_000, 8_020),
        (10_050, 10_020),
        (50_000, 12_000),
        (10_130, 10_040),
    ]:
        on_chain = oracle.preview_agreement(mid, leader_value, validator_value)
        pure = report(helpers, leader_value, validator_value)
        assert on_chain["agree"] == pure["agree"], (leader_value, validator_value)
        assert on_chain["reason"] == pure["reason"]


def test_preview_agreement_reports_the_decision_lines(
    oracle, make_milestone, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    preview = oracle.preview_agreement(mid, 10_050, 10_020)
    assert preview["decision_lines"]["certain_pass_at"] == 10_100
    assert preview["decision_lines"]["certain_fail_below"] == 9_900


def test_classify_value_lets_a_creator_check_before_claiming(
    oracle, make_milestone, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    assert oracle.classify_value(mid, 9_000)["zone"] == "fail"
    assert oracle.classify_value(mid, 10_000)["zone"] == "borderline"
    assert oracle.classify_value(mid, 10_100)["zone"] == "pass"
    assert oracle.classify_value(mid, 10_000)["variance_allowance"] == 50


def test_per_milestone_overrides_change_the_decision_lines(
    oracle, make_milestone, direct_alice, direct_bob
):
    tight = make_milestone(
        oracle, direct_alice, direct_bob, threshold=THRESHOLD, band_bps=10
    )
    loose = make_milestone(
        oracle,
        direct_alice,
        direct_bob,
        threshold=THRESHOLD,
        band_bps=1_000,
        deadline_in=8 * 86_400,
    )
    assert oracle.get_milestone(tight)["decision_lines"]["certain_pass_at"] == 10_010
    assert oracle.get_milestone(loose)["decision_lines"]["certain_pass_at"] == 11_000


def test_an_explicit_zero_override_makes_adjudication_exact(
    oracle, make_milestone, direct_alice, direct_bob
):
    mid = make_milestone(
        oracle,
        direct_alice,
        direct_bob,
        threshold=THRESHOLD,
        band_bps=-1,
        variance_bps=-1,
        variance_floor=-1,
    )
    record = oracle.get_milestone(mid)
    assert record["band_bps"] == 0
    assert record["variance_bps"] == 0
    assert record["variance_floor"] == 0
    assert oracle.preview_agreement(mid, 10_000, 10_000)["agree"] is True
    assert oracle.preview_agreement(mid, 10_000, 10_001)["agree"] is False


# ---------------------------------------------------------------------------
# The real validator closure
# ---------------------------------------------------------------------------


def run_validator_against(direct_vm, url, page, value, **kwargs):
    """Swap in a different page and reading, then replay the real validator."""
    direct_vm.clear_mocks()
    mock_page(direct_vm, url, page)
    mock_reader(direct_vm, value, **kwargs)
    return direct_vm.run_validator()


def test_validator_agrees_when_it_reads_a_slightly_higher_count(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    """
    The ordinary case. The leader reads the page, the validator reads it moments
    later and sees a few more stars because the count is still climbing.
    """
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431)
    assert (
        run_validator_against(direct_vm, GITHUB_URL, github_page("12,470"), 12_470)
        is True
    )


def test_validator_disagrees_when_the_leader_inflated_the_count(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431)
    assert (
        run_validator_against(direct_vm, GITHUB_URL, github_page("8,100"), 8_100)
        is False
    )


def test_validator_agrees_that_a_borderline_reading_is_borderline(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    out = claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,050"), value=10_050
    )
    assert out["zone"] == "borderline"
    assert (
        run_validator_against(direct_vm, GITHUB_URL, github_page("10,020"), 10_020)
        is True
    )


def test_validator_rejects_a_leader_reading_it_cannot_support(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    claim(
        oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("10,130"), value=10_130
    )
    assert (
        run_validator_against(direct_vm, GITHUB_URL, github_page("10,040"), 10_040)
        is False
    )


def test_validator_rejects_an_injected_leader_result(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    """
    A leader that fabricates an observation record outright, rather than reading
    a page, is caught by the self-classification and proximity gates.
    """
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("8,000"), value=8_000)
    direct_vm.clear_mocks()
    mock_page(direct_vm, GITHUB_URL, github_page("8,000"))
    mock_reader(direct_vm, 8_000)
    assert (
        direct_vm.run_validator(
            leader_result={"found": True, "value": 999_999, "zone": "pass"}
        )
        is False
    )
    assert (
        direct_vm.run_validator(
            leader_result={"found": True, "value": 8_000, "zone": "pass"}
        )
        is False
    )


def test_validator_disagrees_when_only_the_leader_could_read_the_metric(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431)
    assert (
        run_validator_against(
            direct_vm, GITHUB_URL, github_page("12,431"), 0, found=False
        )
        is False
    )


def test_validator_agrees_when_neither_side_can_read_the_metric(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    set_scene(direct_vm, GITHUB_URL, github_page("12,431"), 0, found=False)
    direct_vm.sender = direct_bob
    out = oracle.submit_claim(mid)
    assert out["zone"] == "unreadable"
    assert (
        run_validator_against(
            direct_vm, GITHUB_URL, github_page("12,470"), 0, found=False
        )
        is True
    )


def test_validator_disagrees_when_the_leader_failed_and_it_did_not(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    """
    ``run_nondet`` rejects a mismatch in outcome kind, and the validator also
    returns False explicitly, so a leader failure plus a validator success can
    never be mistaken for agreement.
    """
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
    claim(oracle, direct_bob, mid, url=GITHUB_URL, page=github_page("12,431"), value=12_431)
    direct_vm.clear_mocks()
    mock_page(direct_vm, GITHUB_URL, github_page("12,470"))
    mock_reader(direct_vm, 12_470)
    assert (
        direct_vm.run_validator(leader_error=RuntimeError("[TRANSIENT] fetch failed"))
        is False
    )


def test_the_nondeterministic_closures_are_serialisable(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob, claim
):
    """
    GenVM serialises both closures to cross the WASM boundary, so neither may
    capture storage handles or other unpicklable state.
    """
    direct_vm.check_pickling = True
    try:
        mid = make_milestone(oracle, direct_alice, direct_bob, threshold=THRESHOLD)
        out = claim(
            oracle,
            direct_bob,
            mid,
            url=GITHUB_URL,
            page=github_page("12,431"),
            value=12_431,
        )
        assert out["zone"] == "pass"
    finally:
        direct_vm.check_pickling = False
