"""
The deterministic page scanner.

Everything the model is allowed to claim it saw comes from this scanner, so its
behaviour is the anti-hallucination guarantee. These tests pin down abbreviation
expansion, locale-ambiguous separators, keyword scoping, and the candidate gate.
"""

import pytest

from conftest import generic_page, github_page, youtube_page


# ---------------------------------------------------------------------------
# Single token interpretation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token,mult,expected_subset",
    [
        ("1234", 1, {1234}),
        ("12,431", 1, {12431, 12, 431}),
        ("12.431", 1, {12431, 12, 431}),
        ("1,234,567", 1, {1234567, 1, 234, 567}),
        ("1.2", 1000, {1200, 1000, 12000}),
        ("12.4", 1000, {12400, 12000, 124000}),
        ("1.5", 1000000, {1500000, 1000000, 15000000}),
        ("2.35", 1000000000, {2350000000, 2000000000}),
        ("12", 1000, {12000}),
        ("0", 1, {0}),
    ],
)
def test_interpret_number_covers_every_plausible_reading(
    helpers, token, mult, expected_subset
):
    produced = set(helpers._interpret_number(token, mult))
    assert expected_subset <= produced, (
        f"{token!r} x{mult} produced {sorted(produced)}"
    )


def test_interpret_number_handles_unicode_group_separators(helpers):
    for separator in helpers._GROUPING_SPACES:
        produced = set(helpers._interpret_number(f"12{separator}431", 1))
        assert 12431 in produced
        assert 12 in produced
        assert 431 in produced


def test_interpret_number_rejects_non_numeric_and_oversized(helpers):
    assert helpers._interpret_number("", 1) == []
    assert helpers._interpret_number("abc", 1) == []
    assert helpers._interpret_number("-5", 1) == []
    assert helpers._interpret_number("1" * 19, 1) == []


# ---------------------------------------------------------------------------
# Page scanning
# ---------------------------------------------------------------------------


def test_scanner_finds_grouped_count_on_a_github_page(helpers):
    candidates = helpers._numeric_candidates(github_page("12,431"))
    assert 12431 in candidates
    assert 412 in candidates


def test_scanner_expands_abbreviations(helpers):
    candidates = helpers._numeric_candidates(youtube_page("1.4B", likes="18M"))
    assert 1400000000 in candidates
    assert 18000000 in candidates


def test_scanner_keeps_the_unabbreviated_reading_too(helpers):
    """A trailing letter may be a magnitude suffix or may not be, so keep both."""
    candidates = helpers._numeric_candidates("5K followers")
    assert 5000 in candidates
    assert 5 in candidates


def test_scanner_ignores_a_suffix_letter_that_starts_a_word(helpers):
    """`Kudos` is not a magnitude suffix, so no multiplication happens."""
    candidates = helpers._numeric_candidates("5 Kudos received")
    assert 5 in candidates
    assert 5000 not in candidates


def test_scanner_ignores_a_suffix_letter_inside_a_word(helpers):
    candidates = helpers._numeric_candidates("version 3 Kubernetes clusters")
    assert 3 in candidates
    assert 3000 not in candidates


def test_expand_abbreviated_commits_to_one_reading(helpers):
    assert helpers._expand_abbreviated("12.4", 1000) == 12400
    assert helpers._expand_abbreviated("12,431", 1) == 12431
    assert helpers._expand_abbreviated("2", 1000000000) == 2000000000
    assert helpers._expand_abbreviated("1,234.5", 1000) == 1234500
    assert helpers._expand_abbreviated("", 1) is None
    assert helpers._expand_abbreviated("abc", 1) is None


def test_scanner_is_sorted_deduplicated_and_capped(helpers):
    text = " ".join(str(n) for n in range(1, 400))
    candidates = helpers._numeric_candidates(text)
    assert candidates == sorted(candidates)
    assert len(candidates) == len(set(candidates))
    assert len(candidates) <= helpers._MAX_CANDIDATES


def test_scanner_drops_values_beyond_the_supported_range(helpers):
    candidates = helpers._numeric_candidates("9" * 17 + " views")
    assert all(value <= helpers._MAX_METRIC_VALUE for value in candidates)


# ---------------------------------------------------------------------------
# Keyword scoping
# ---------------------------------------------------------------------------


def test_evidence_is_scoped_to_the_metric_keywords(helpers):
    page = helpers._squash_whitespace(github_page("12,431"))
    evidence = helpers._collect_evidence(page, ["star", "stars", "stargazers"])
    assert evidence["scoped"] is True
    assert 12431 in evidence["candidates"]
    assert "star" in evidence["context"].lower()


def test_evidence_falls_back_to_the_page_head_when_keywords_are_absent(helpers):
    page = helpers._squash_whitespace(generic_page("8,204"))
    evidence = helpers._collect_evidence(page, ["stargazers"])
    assert evidence["scoped"] is False
    # The fallback is a weaker claim but still bounds the model to real numbers.
    assert 8204 in evidence["candidates"]


def test_evidence_context_is_bounded(helpers):
    page = ("views 1234 " * 20000)[: helpers._MAX_PAGE_SCAN_CHARS]
    evidence = helpers._collect_evidence(page, ["views"])
    assert len(evidence["context"]) <= helpers._MAX_CONTEXT_CHARS + 64
    assert evidence["page_chars"] <= helpers._MAX_PAGE_SCAN_CHARS


def test_evidence_truncates_oversized_pages_before_scanning(helpers):
    page = "x" * (helpers._MAX_PAGE_SCAN_CHARS + 5000) + " 777 views"
    evidence = helpers._collect_evidence(page, ["views"])
    assert evidence["page_chars"] == helpers._MAX_PAGE_SCAN_CHARS
    # The trailing count fell outside the scan window, so it is not claimable.
    assert 777 not in evidence["candidates"]


# ---------------------------------------------------------------------------
# The candidate gate
# ---------------------------------------------------------------------------


def test_gate_accepts_an_exact_candidate(helpers):
    assert helpers._value_is_on_page(12431, [412, 12431, 87]) is True


def test_gate_rejects_an_invented_number(helpers):
    assert helpers._value_is_on_page(99999, [412, 12431, 87]) is False


def test_gate_rejects_everything_when_the_page_yielded_nothing(helpers):
    assert helpers._value_is_on_page(100, []) is False


def test_gate_allows_half_a_percent_of_rounding_slack(helpers):
    # 12.4K expanded as 12400 by the scanner and 12430 by the model.
    assert helpers._value_is_on_page(12430, [12400]) is True
    assert helpers._value_is_on_page(12500, [12400]) is False


def test_gate_slack_never_drops_below_one(helpers):
    assert helpers._value_is_on_page(3, [4]) is True
    assert helpers._value_is_on_page(3, [6]) is False


# ---------------------------------------------------------------------------
# Model value coercion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (12431, 12431),
        ("12431", 12431),
        ("12,431", 12431),
        (" 12431 ", 12431),
        ("12.4K", 12400),
        ("1.5M", 1500000),
        ("2B", 2000000000),
        (0, 0),
    ],
)
def test_coerce_count_accepts_the_shapes_models_actually_return(
    helpers, raw, expected
):
    assert helpers._coerce_count(raw) == expected


@pytest.mark.parametrize("raw", [True, False, "", "   ", "many", None, -5, 10**16])
def test_coerce_count_rejects_unusable_values(helpers, raw):
    with pytest.raises(Exception) as excinfo:
        helpers._coerce_count(raw)
    assert helpers.ERROR_LLM in str(excinfo.value)


# ---------------------------------------------------------------------------
# JSON recovery
# ---------------------------------------------------------------------------


def test_parse_json_object_passes_dicts_through(helpers):
    assert helpers._parse_json_object({"found": True}) == {"found": True}


def test_parse_json_object_strips_prose_and_fences(helpers):
    raw = 'Sure! Here is the result:\n```json\n{"found": true, "value": 5}\n```\nHope that helps.'
    assert helpers._parse_json_object(raw) == {"found": True, "value": 5}


def test_parse_json_object_repairs_trailing_commas(helpers):
    assert helpers._parse_json_object('{"found": true, "value": 5,}')["value"] == 5


@pytest.mark.parametrize(
    "raw", ["no json here", "[1, 2, 3]", '{"found": true,', 42]
)
def test_parse_json_object_rejects_unrecoverable_output(helpers, raw):
    with pytest.raises(Exception) as excinfo:
        helpers._parse_json_object(raw)
    assert helpers.ERROR_LLM in str(excinfo.value)


# ---------------------------------------------------------------------------
# URL and host handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,host",
    [
        ("https://github.com/genlayerlabs/genvm", "github.com"),
        ("https://www.youtube.com/watch?v=abc", "www.youtube.com"),
        ("https://GitHub.COM/x", "github.com"),
        ("https://example.com:8443/a/b", "example.com"),
        ("https://example.com", "example.com"),
        ("https://example.com#frag", "example.com"),
    ],
)
def test_host_extraction(helpers, url, host):
    assert helpers._host_of(url) == host


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x",  # plain HTTP is refused outright
        "ftp://github.com/x",
        "github.com/x",
        "https://evil.com@github.com/x",  # credentials are a spoofing vector
        "https://",
        "https://localhost/x",  # no dot, so not a public host
        "https://[::1]/x",
        "https://exa mple.com/x",
        "https://example.com:notaport/x",
        "https://exa_mple.com/x",
    ],
)
def test_host_extraction_refuses_unsafe_urls(helpers, url):
    assert helpers._host_of(url) == ""


@pytest.mark.parametrize(
    "host,allow,expected",
    [
        ("github.com", ["github.com"], True),
        ("gist.github.com", ["github.com"], True),
        ("notgithub.com", ["github.com"], False),
        ("github.com.evil.io", ["github.com"], False),
        ("anything.example", ["*"], True),
        ("", ["*"], False),
        ("youtu.be", ["youtube.com", "youtu.be"], True),
    ],
)
def test_host_allowlist_matches_exact_and_parent_domains(
    helpers, host, allow, expected
):
    assert helpers._host_allowed(host, allow) is expected


# ---------------------------------------------------------------------------
# Small string utilities used in storage records
# ---------------------------------------------------------------------------


def test_clip_bounds_stored_strings(helpers):
    assert helpers._clip("abcdef", 3) == "abc"
    assert helpers._clip("ab", 10) == "ab"


def test_split_csv_normalises_and_deduplicates(helpers):
    assert helpers._split_csv(" Stars , stars ,FORKS, ") == ["stars", "forks"]
    assert helpers._split_csv("") == []
