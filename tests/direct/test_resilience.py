"""
Resilience of the non-deterministic block.

A metric reader has two unreliable inputs: a page that may be missing, slow or
broken, and a model that may answer in any shape it likes. Both have to fail
into a classified error so that validators can decide whether two failures are
the same failure.
"""

import json

import pytest

from conftest import (
    GITHUB_URL,
    github_page,
    mock_page,
    mock_reader,
    mock_reader_raw,
    mock_status,
)


def claim_with(direct_vm, oracle, creator, mid, *, page=None, status=None, reader=None, raw=None):
    """Drive one claim with a specific page response and reader response."""
    direct_vm.clear_mocks()
    if status is not None:
        mock_status(direct_vm, GITHUB_URL, status, page or "")
    else:
        mock_page(direct_vm, GITHUB_URL, page if page is not None else github_page("12,431"))
    if raw is not None:
        mock_reader_raw(direct_vm, raw)
    elif reader is not None:
        mock_reader(direct_vm, **reader)
    else:
        mock_reader(direct_vm, 12_431)
    direct_vm.sender = creator
    return oracle.submit_claim(mid)


@pytest.fixture
def pending(oracle, make_milestone, direct_alice, direct_bob):
    return make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def test_the_error_prefixes_are_the_documented_four(helpers):
    assert helpers.ERROR_EXPECTED == "[EXPECTED]"
    assert helpers.ERROR_EXTERNAL == "[EXTERNAL]"
    assert helpers.ERROR_TRANSIENT == "[TRANSIENT]"
    assert helpers.ERROR_LLM == "[LLM_ERROR]"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("[EXPECTED] creator only", "expected"),
        ("[EXTERNAL] content not retrievable: HTTP 404", "external"),
        ("[TRANSIENT] upstream unavailable: HTTP 503", "transient"),
        ("[LLM_ERROR] response was not valid JSON", "llm"),
        ("something went wrong", "unclassified"),
    ],
)
def test_failures_are_classified_by_prefix(oracle, message, expected):
    report = oracle.preview_error_agreement(message, message)
    assert report["leader_class"] == expected


def test_deterministic_failures_must_match_verbatim(oracle):
    same = oracle.preview_error_agreement(
        "[EXTERNAL] content not retrievable: HTTP 404",
        "[EXTERNAL] content not retrievable: HTTP 404",
    )
    assert same["agree"] is True

    different = oracle.preview_error_agreement(
        "[EXTERNAL] content not retrievable: HTTP 404",
        "[EXTERNAL] content not retrievable: HTTP 403",
    )
    assert different["agree"] is False


def test_business_logic_failures_must_match_verbatim(oracle):
    assert oracle.preview_error_agreement(
        "[EXPECTED] creator only", "[EXPECTED] creator only"
    )["agree"] is True
    assert oracle.preview_error_agreement(
        "[EXPECTED] creator only", "[EXPECTED] sponsor only"
    )["agree"] is False


def test_transient_failures_agree_on_the_prefix_alone(oracle):
    """
    Two validators hitting unrelated network trouble have in fact observed the
    same thing: the page could not be read right now.
    """
    assert oracle.preview_error_agreement(
        "[TRANSIENT] upstream unavailable: HTTP 503",
        "[TRANSIENT] could not retrieve the content page",
    )["agree"] is True


def test_model_failures_never_agree(oracle):
    """
    Agreeing on a broken model response would lock the outcome in. Disagreeing
    forces the round onto validators whose model behaved.
    """
    assert oracle.preview_error_agreement(
        "[LLM_ERROR] response was not valid JSON",
        "[LLM_ERROR] response was not valid JSON",
    )["agree"] is False


def test_unclassified_failures_never_agree(oracle):
    assert oracle.preview_error_agreement("boom", "boom")["agree"] is False


def test_a_transient_leader_failure_does_not_match_a_deterministic_one(oracle):
    assert oracle.preview_error_agreement(
        "[TRANSIENT] could not retrieve the content page",
        "[EXTERNAL] content not retrievable: HTTP 404",
    )["agree"] is False
    assert oracle.preview_error_agreement(
        "[EXTERNAL] content not retrievable: HTTP 404",
        "[TRANSIENT] could not retrieve the content page",
    )["agree"] is False


def test_the_comparator_view_matches_the_comparator(oracle, helpers):
    import genlayer.gl.vm as gl_vm

    pairs = [
        ("[EXPECTED] a", "[EXPECTED] a"),
        ("[EXPECTED] a", "[EXPECTED] b"),
        ("[TRANSIENT] a", "[TRANSIENT] b"),
        ("[LLM_ERROR] a", "[LLM_ERROR] a"),
        ("plain", "plain"),
    ]
    for leader, mine in pairs:
        expected = helpers._compare_user_errors(
            gl_vm.UserError(leader), gl_vm.UserError(mine)
        )
        assert oracle.preview_error_agreement(leader, mine)["agree"] is expected


# ---------------------------------------------------------------------------
# Page fetch failures
# ---------------------------------------------------------------------------


@pytest.fixture
def http_oracle(deploy_oracle, direct_vm, direct_owner):
    """An oracle with a platform that uses a plain HTTP GET, so status codes surface."""
    contract = deploy_oracle()
    direct_vm.sender = direct_owner
    contract.register_platform(
        "plain_http", "Static page", "stars", "github.com", "http_get", "0ms", ""
    )
    return contract


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 429])
def test_a_client_error_is_an_external_failure(
    http_oracle, make_milestone, direct_vm, direct_alice, direct_bob, status
):
    mid = make_milestone(
        http_oracle, direct_alice, direct_bob, platform="plain_http", threshold=10_000
    )
    with direct_vm.expect_revert(f"[EXTERNAL] content not retrievable: HTTP {status}"):
        claim_with(direct_vm, http_oracle, direct_bob, mid, status=status)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_server_error_is_a_transient_failure(
    http_oracle, make_milestone, direct_vm, direct_alice, direct_bob, status
):
    mid = make_milestone(
        http_oracle, direct_alice, direct_bob, platform="plain_http", threshold=10_000
    )
    with direct_vm.expect_revert(f"[TRANSIENT] upstream unavailable: HTTP {status}"):
        claim_with(direct_vm, http_oracle, direct_bob, mid, status=status)


def test_an_empty_page_is_a_transient_failure(
    oracle, pending, direct_vm, direct_bob
):
    """
    A renderer returning nothing is a retryable condition, not a statement that
    the metric is absent.
    """
    with direct_vm.expect_revert("[TRANSIENT] content page returned no text"):
        claim_with(direct_vm, oracle, direct_bob, pending, page="   ")


def test_a_milestone_survives_a_failed_adjudication(
    oracle, pending, direct_vm, direct_bob, claim
):
    """A failed round applies no state change, so the claim can be retried."""
    with direct_vm.expect_revert("[TRANSIENT]"):
        claim_with(direct_vm, oracle, direct_bob, pending, page="")

    record = oracle.get_milestone(pending)
    assert record["status"] == "open"
    assert record["attempts"] == 0

    out = claim(
        oracle, direct_bob, pending, url=GITHUB_URL, page=github_page("12,431"), value=12_431
    )
    assert out["status"] == "achieved"


# ---------------------------------------------------------------------------
# Model response handling
# ---------------------------------------------------------------------------


def test_prose_around_the_json_is_tolerated(oracle, pending, direct_vm, direct_bob):
    out = claim_with(
        direct_vm,
        oracle,
        direct_bob,
        pending,
        raw='Certainly. ```json\n{"found": true, "value": 12431, "display": "12,431 stars"}\n``` Let me know if you need more.',
    )
    assert out["observed_value"] == 12_431
    assert out["zone"] == "pass"


def test_a_trailing_comma_is_repaired(oracle, pending, direct_vm, direct_bob):
    out = claim_with(
        direct_vm, oracle, direct_bob, pending, raw='{"found": true, "value": 12431,}'
    )
    assert out["observed_value"] == 12_431


def test_an_abbreviated_value_is_expanded(oracle, pending, direct_vm, direct_bob):
    out = claim_with(
        direct_vm,
        oracle,
        direct_bob,
        pending,
        page=github_page("12.4K"),
        raw='{"found": true, "value": "12.4K"}',
    )
    assert out["observed_value"] == 12_400


def test_a_grouped_string_value_is_parsed(oracle, pending, direct_vm, direct_bob):
    out = claim_with(
        direct_vm, oracle, direct_bob, pending, raw='{"found": true, "value": "12,431"}'
    )
    assert out["observed_value"] == 12_431


def test_the_alternate_key_names_models_use_are_accepted(
    oracle, pending, direct_vm, direct_bob
):
    out = claim_with(
        direct_vm,
        oracle,
        direct_bob,
        pending,
        raw='{"present": true, "count": 12431, "explanation": "header counter"}',
    )
    assert out["observed_value"] == 12_431
    assert out["reason"] == "Independent validators agreed on zone pass"


def test_a_string_boolean_for_found_is_accepted(
    oracle, make_milestone, direct_vm, direct_alice, direct_bob
):
    affirmative = make_milestone(
        oracle, direct_alice, direct_bob, threshold=10_000
    )
    out = claim_with(
        direct_vm, oracle, direct_bob, affirmative, raw='{"found": "yes", "value": 12431}'
    )
    assert out["found"] is True
    assert out["zone"] == "pass"

    negative = make_milestone(
        oracle, direct_alice, direct_bob, threshold=20_000
    )
    out = claim_with(
        direct_vm, oracle, direct_bob, negative, raw='{"found": "no", "value": 0}'
    )
    assert out["found"] is False
    assert out["zone"] == "unreadable"


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ("I could not tell", "[LLM_ERROR] response contained no JSON object"),
        ('{"found": true', "[LLM_ERROR] response contained no JSON object"),
        ('{"found": true "value": 1}', "[LLM_ERROR] response was not valid JSON"),
        ("[1,2,3]", "[LLM_ERROR] unusable response type list"),
        ('{"value": 12431}', "[LLM_ERROR] response omitted `found`"),
        ('{"found": true}', "[LLM_ERROR] response omitted `value`"),
        ('{"found": true, "value": "lots"}', "[LLM_ERROR] `value` was not numeric"),
        ('{"found": true, "value": ""}', "[LLM_ERROR] `value` was empty"),
        ('{"found": true, "value": true}', "[LLM_ERROR] `value` was a boolean"),
        ('{"found": true, "value": -4}', "[LLM_ERROR] `value` was negative"),
    ],
)
def test_unusable_model_output_fails_as_a_model_error(
    oracle, pending, direct_vm, direct_bob, raw, fragment
):
    with direct_vm.expect_revert(fragment):
        claim_with(direct_vm, oracle, direct_bob, pending, raw=raw)


def test_a_value_the_page_does_not_show_is_rejected(
    oracle, pending, direct_vm, direct_bob
):
    """
    The anti-hallucination gate, end to end. The page shows 8,100 stars; the
    model claims 99,999.
    """
    with direct_vm.expect_revert(
        "[LLM_ERROR] reported 99999 which the page scanner did not find"
    ):
        claim_with(
            direct_vm,
            oracle,
            direct_bob,
            pending,
            page=github_page("8,100"),
            raw='{"found": true, "value": 99999}',
        )


def test_the_gate_rejects_an_adjacent_rival_metric(
    oracle, pending, direct_vm, direct_bob
):
    """
    The model still chooses which number is the metric, and choosing wrong is
    what the validator's independent read catches, not the gate.
    """
    with direct_vm.expect_revert("page scanner did not find"):
        claim_with(direct_vm, oracle, direct_bob, pending,
                   page=github_page("8,100", forks="12,431"),
                   raw='{"found": true, "value": 12431}')


def test_unverified_model_display_and_reason_are_not_stored(
    oracle, pending, direct_vm, direct_bob
):
    out = claim_with(
        direct_vm,
        oracle,
        direct_bob,
        pending,
        raw=json.dumps({"found": True, "value": 12431, "display": "s" * 500, "reason": "r" * 500}),
    )
    assert out["display"] == "12431 stars"
    assert out["reason"] == "Independent validators agreed on zone pass"
    evidence = json.loads(oracle.get_evidence(pending))
    assert evidence["display"] == "12431 stars"
    assert evidence["reason"] == "Independent validators agreed on zone pass"


def test_a_page_with_no_numbers_at_all_rejects_any_claimed_value(
    oracle, pending, direct_vm, direct_bob
):
    with direct_vm.expect_revert("which the page scanner did not find"):
        claim_with(
            direct_vm,
            oracle,
            direct_bob,
            pending,
            page="This repository has no visible counters right now.",
            raw='{"found": true, "value": 12431}',
        )


def test_a_page_with_no_numbers_can_still_report_the_metric_as_absent(
    oracle, pending, direct_vm, direct_bob
):
    out = claim_with(
        direct_vm,
        oracle,
        direct_bob,
        pending,
        page="This repository has no visible counters right now.",
        raw='{"found": false, "value": 0, "reason": "no counters on the page"}',
    )
    assert out["zone"] == "unreadable"
    assert out["status"] == "open"
