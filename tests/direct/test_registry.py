"""
The platform and metric registries, and contract administration.

Both registries are owner-governed and extensible after deployment, so a
builder can add a source without redeploying and without touching any live
milestone's terms.
"""

import pytest

from conftest import DAY, GEN, GITHUB_URL


# ---------------------------------------------------------------------------
# Bootstrapped state
# ---------------------------------------------------------------------------


def test_the_constructor_bootstraps_usable_platforms(oracle):
    keys = [p["key"] for p in oracle.list_platforms()]
    assert keys == ["github", "youtube", "x", "npm", "generic_web"]
    assert all(p["enabled"] for p in oracle.list_platforms())


def test_the_constructor_bootstraps_metric_keywords(oracle):
    metrics = {m["metric"] for m in oracle.list_metrics()}
    assert {"views", "stars", "forks", "likes", "subscribers", "downloads"} <= metrics
    assert "stargazers" in oracle.get_metric_keywords("stars")


def test_a_platform_record_is_fully_inspectable(oracle):
    github = oracle.get_platform("github")
    assert github["key"] == "github"
    assert github["label"] == "GitHub repository"
    assert "stars" in github["metrics"]
    assert github["hosts"] == ["github.com"]
    assert github["fetch_mode"] == "render_text"
    assert github["wait_after_loaded"] == "4s"
    assert github["enabled"] is True
    assert github["registered_at"] == oracle.now_ts()


def test_javascript_heavy_platforms_wait_before_reading(oracle):
    """A count that only appears after hydration needs the renderer to settle."""
    assert oracle.get_platform("youtube")["wait_after_loaded"] == "4s"
    assert oracle.get_platform("x")["wait_after_loaded"] == "5s"
    assert oracle.get_platform("github")["wait_after_loaded"] == "4s"


def test_an_unknown_platform_or_metric_is_reported_clearly(oracle, direct_vm):
    with direct_vm.expect_revert("unknown platform `nope`"):
        oracle.get_platform("nope")
    with direct_vm.expect_revert("unknown metric `nope`"):
        oracle.get_metric_keywords("nope")


# ---------------------------------------------------------------------------
# Registering platforms
# ---------------------------------------------------------------------------


def test_a_new_platform_can_be_added_after_deployment(
    oracle, direct_vm, direct_owner, direct_alice, direct_bob, claim
):
    direct_vm.sender = direct_owner
    oracle.register_platform(
        "devto",
        "DEV Community post",
        "views,likes,comments",
        "dev.to",
        "render_text",
        "2s",
        "reactions,read",
    )

    record = oracle.get_platform("devto")
    assert record["metrics"] == ["views", "likes", "comments"]
    assert record["hosts"] == ["dev.to"]
    assert record["wait_after_loaded"] == "2s"
    assert "devto" in [p["key"] for p in oracle.list_platforms()]

    mid = oracle.now_ts()
    direct_vm.sender = direct_alice
    direct_vm.value = GEN
    try:
        milestone = oracle.create_milestone(
            direct_bob,
            "https://dev.to/someone/a-post-abc",
            "devto",
            "views",
            5_000,
            mid + 7 * DAY,
            "",
            0,
            0,
            0,
        )
    finally:
        direct_vm.value = 0

    out = claim(
        oracle,
        direct_bob,
        milestone,
        url="https://dev.to/someone/a-post-abc",
        page="A post about things. 7,400 views 128 reactions 12 comments",
        value=7_400,
    )
    assert out["zone"] == "pass"


def test_re_registering_a_platform_replaces_it(oracle, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    before = len(oracle.list_platforms())
    oracle.register_platform(
        "github", "GitHub", "stars,forks", "github.com,gist.github.com", "render_html", "1s", ""
    )
    assert len(oracle.list_platforms()) == before
    record = oracle.get_platform("github")
    assert record["fetch_mode"] == "render_html"
    assert record["metrics"] == ["stars", "forks"]
    assert record["hosts"] == ["github.com", "gist.github.com"]


@pytest.mark.parametrize(
    "args,fragment",
    [
        (("A", "l", "stars", "github.com", "render_text", "0ms", ""), "platform key must be"),
        (("x" * 33, "l", "stars", "github.com", "render_text", "0ms", ""), "platform key must be"),
        (("ok", "l", "stars", "github.com", "telepathy", "0ms", ""), "fetch_mode must be one of"),
        (("ok", "l", "stars", "github.com", "render_text", "soon", ""), "wait_after_loaded must look like"),
        (("ok", "l", "stars", "github.com", "render_text", "5m", ""), "wait_after_loaded must look like"),
        (("ok", "l", "", "github.com", "render_text", "0ms", ""), "at least one metric is required"),
        (("ok", "l", "vibes", "github.com", "render_text", "0ms", ""), "metric `vibes` has no keywords"),
        (("ok", "l", "stars", "", "render_text", "0ms", ""), "at least one host is required"),
    ],
)
def test_an_invalid_platform_registration_is_refused(
    oracle, direct_vm, direct_owner, args, fragment
):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert(fragment):
        oracle.register_platform(*args)


def test_all_three_fetch_modes_are_accepted(oracle, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    for index, mode in enumerate(["render_text", "render_html", "http_get"]):
        oracle.register_platform(
            f"mode_{index}", mode, "stars", "github.com", mode, "0ms", ""
        )
        assert oracle.get_platform(f"mode_{index}")["fetch_mode"] == mode


def test_a_platform_can_be_disabled_and_re_enabled(oracle, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    oracle.set_platform_enabled("github", False)
    assert oracle.get_platform("github")["enabled"] is False
    assert oracle.check_url("github", GITHUB_URL)["ok"] is False

    oracle.set_platform_enabled("github", True)
    assert oracle.get_platform("github")["enabled"] is True
    assert oracle.check_url("github", GITHUB_URL)["ok"] is True


def test_disabling_an_unknown_platform_is_refused(oracle, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("unknown platform `nope`"):
        oracle.set_platform_enabled("nope", False)


# ---------------------------------------------------------------------------
# Metric keywords
# ---------------------------------------------------------------------------


def test_a_new_metric_can_be_registered_and_then_used(
    oracle, direct_vm, direct_owner
):
    direct_vm.sender = direct_owner
    oracle.set_metric_keywords("bookmarks", "bookmark,bookmarks,saved")
    assert oracle.get_metric_keywords("bookmarks") == ["bookmark", "bookmarks", "saved"]
    oracle.register_platform(
        "notes", "Notes", "bookmarks", "notes.example", "render_text", "0ms", ""
    )
    assert oracle.get_platform("notes")["metrics"] == ["bookmarks"]


def test_retuning_keywords_changes_what_the_scanner_scopes_to(
    oracle, direct_vm, direct_owner
):
    direct_vm.sender = direct_owner
    before = oracle.resolve_keywords("github", "stars")
    oracle.set_metric_keywords("stars", "stargazer count")
    after = oracle.resolve_keywords("github", "stars")
    assert before != after
    assert "stargazer count" in after
    # The metric's own name always stays usable as a search term.
    assert "stars" in after


def test_keyword_updates_do_not_duplicate_the_registry_index(
    oracle, direct_vm, direct_owner
):
    direct_vm.sender = direct_owner
    before = len(oracle.list_metrics())
    oracle.set_metric_keywords("stars", "star,stars")
    assert len(oracle.list_metrics()) == before
    oracle.set_metric_keywords("brandnew", "brand,new")
    assert len(oracle.list_metrics()) == before + 1


@pytest.mark.parametrize(
    "metric,keywords,fragment",
    [
        ("A", "a,b", "metric must be"),
        ("x" * 33, "a,b", "metric must be"),
        ("ok", "", "at least one keyword is required"),
        ("ok", " , , ", "at least one keyword is required"),
    ],
)
def test_an_invalid_keyword_update_is_refused(
    oracle, direct_vm, direct_owner, metric, keywords, fragment
):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert(fragment):
        oracle.set_metric_keywords(metric, keywords)


# ---------------------------------------------------------------------------
# Ownership and access control
# ---------------------------------------------------------------------------


OWNER_ONLY = [
    ("set_paused", (True,)),
    ("set_consensus_params", (100, 50, 5, 3_600, 2, 0)),
    ("set_economics", (0, 86_400, 0)),
    ("register_platform", ("x1", "l", "stars", "github.com", "render_text", "0ms", "")),
    ("set_platform_enabled", ("github", False)),
    ("set_metric_keywords", ("stars", "star,stars")),
]


@pytest.mark.parametrize("method,args", OWNER_ONLY)
def test_administration_is_owner_only(
    oracle, direct_vm, direct_alice, direct_bob, method, args
):
    for impostor in (direct_alice, direct_bob):
        direct_vm.sender = impostor
        with direct_vm.expect_revert("owner only"):
            getattr(oracle, method)(*args)


def test_address_taking_admin_methods_are_owner_only(
    oracle, direct_vm, direct_alice
):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("owner only"):
        oracle.transfer_ownership(direct_alice)
    with direct_vm.expect_revert("owner only"):
        oracle.set_fee(0, direct_alice)
    with direct_vm.expect_revert("owner only"):
        oracle.withdraw_surplus(direct_alice, GEN)


def test_ownership_transfers(oracle, direct_vm, direct_owner, direct_alice):
    direct_vm.sender = direct_owner
    oracle.transfer_ownership(direct_alice)
    assert oracle.get_owner() == direct_alice.as_hex

    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("owner only"):
        oracle.set_paused(True)

    direct_vm.sender = direct_alice
    oracle.set_paused(True)
    assert oracle.is_paused() is True


def test_ownership_cannot_be_dropped_on_the_floor(
    oracle, direct_vm, direct_owner, zero_address
):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("new owner must not be the zero address"):
        oracle.transfer_ownership(zero_address)


# ---------------------------------------------------------------------------
# Configuration bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,fragment",
    [
        ((5_001, 50, 5, 3_600, 2, 0), "band_bps must be between 0 and 5000"),
        ((-1, 50, 5, 3_600, 2, 0), "band_bps must be between 0 and 5000"),
        ((100, 5_001, 5, 3_600, 2, 0), "variance_bps must be between 0 and 5000"),
        ((100, 50, -1, 3_600, 2, 0), "variance_floor must not be negative"),
        ((100, 50, 5, 59, 2, 0), "defer_seconds must be at least 60"),
        ((100, 50, 5, 3_600, 0, 0), "max_defer_rounds must be between 1 and 32"),
        ((100, 50, 5, 3_600, 33, 0), "max_defer_rounds must be between 1 and 32"),
        ((100, 50, 5, 3_600, 2, -1), "resolution_grace_seconds must not be negative"),
    ],
)
def test_out_of_range_consensus_params_are_refused(
    oracle, direct_vm, direct_owner, args, fragment
):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert(fragment):
        oracle.set_consensus_params(*args)


@pytest.mark.parametrize(
    "args,fragment",
    [
        ((-1, 86_400, 0), "min_reward_atto must not be negative"),
        ((0, 3_599, 0), "max_deadline_horizon must be at least 3600"),
        ((0, 86_400, -1), "cancel_window_seconds must not be negative"),
    ],
)
def test_out_of_range_economics_are_refused(
    oracle, direct_vm, direct_owner, args, fragment
):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert(fragment):
        oracle.set_economics(*args)


# One failing constructor per test: a contract module that raised in __init__
# stays registered, so a second deploy in the same test trips the GenVM
# one-contract-per-module guard rather than the bound under test.
@pytest.mark.parametrize(
    "overrides,fragment",
    [
        ({"default_band_bps": 5_001}, "band_bps must be between 0 and 5000"),
        ({"default_variance_bps": 5_001}, "variance_bps must be between 0 and 5000"),
        ({"defer_seconds": 30}, "defer_seconds must be at least 60"),
        ({"max_defer_rounds": 0}, "max_defer_rounds must be between 1 and 32"),
        ({"max_deadline_horizon": 10}, "max_deadline_horizon must be at least 3600"),
        ({"min_reward_atto": -1}, "min_reward_atto must not be negative"),
    ],
)
def test_the_same_bounds_apply_at_deployment(
    deploy_oracle, direct_vm, overrides, fragment
):
    with direct_vm.expect_revert(fragment):
        deploy_oracle(**overrides)


def test_updated_defaults_apply_to_new_milestones(
    oracle, make_milestone, direct_vm, direct_owner, direct_alice, direct_bob
):
    direct_vm.sender = direct_owner
    oracle.set_consensus_params(500, 200, 50, 7_200, 3, 3_600)
    mid = make_milestone(oracle, direct_alice, direct_bob, threshold=10_000)
    record = oracle.get_milestone(mid)
    assert record["band_bps"] == 500
    assert record["variance_bps"] == 200
    assert record["variance_floor"] == 50
    assert record["decision_lines"]["certain_pass_at"] == 10_500
    assert record["resolution_deadline"] == record["deadline_ts"] + 3_600


def test_a_zero_grace_window_closes_resolution_at_the_deadline(
    deploy_oracle, make_milestone, direct_alice, direct_bob
):
    contract = deploy_oracle(resolution_grace_seconds=0)
    mid = make_milestone(contract, direct_alice, direct_bob, deadline_in=3_600)
    record = contract.get_milestone(mid)
    assert record["resolution_deadline"] == record["deadline_ts"]


def test_pausing_is_reported_and_reversible(oracle, direct_vm, direct_owner):
    assert oracle.is_paused() is False
    direct_vm.sender = direct_owner
    oracle.set_paused(True)
    assert oracle.is_paused() is True
    assert oracle.get_config()["paused"] is True
    oracle.set_paused(False)
    assert oracle.is_paused() is False


def test_the_config_view_exposes_the_vocabulary_integrators_need(oracle):
    config = oracle.get_config()
    assert config["bps_denominator"] == 10_000
    assert config["max_fee_bps"] == 1_000
    assert config["statuses"] == [
        "open",
        "deferred",
        "contested",
        "achieved",
        "expired",
        "refunded",
        "cancelled",
    ]
    assert config["zones"] == ["fail", "borderline", "pass", "unreadable"]
    assert config["fetch_modes"] == ["render_text", "render_html", "http_get"]
