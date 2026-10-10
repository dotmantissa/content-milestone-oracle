# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
Content Milestone Oracle
========================

An escrowed, validator-adjudicated oracle for a single verifiable question:

    Did this publicly accessible piece of content reach this measurable metric
    threshold by this deadline?

A sponsor locks a reward and pins the terms: content URL, platform, metric
(views, stars, likes, forks, subscribers, ...), threshold and deadline. When the
creator believes the milestone was hit they submit a claim. Each validator then
independently fetches the content page, parses the displayed metric, and
adjudicates the claim. The contract pays the creator only when validators agree
the threshold was cleared.

Why this needs consensus
------------------------
Reading a number off a live page is a non-deterministic operation. Two honest
validators can legitimately see different counts: CDN edges serve stale copies,
platforms aggregate asynchronously, and the number is still climbing while the
round runs. A single trusted reader would have to be believed; a strict-equality
equivalence principle would deadlock on every ordinary discrepancy.

This contract therefore applies a three-gate equivalence principle:

  1. Self classification. The leader's zone is recomputed from the leader's own
     reading, so a leader cannot report one number and label it as another.

  2. Raw proximity. Each side's reading may deviate from the unobservable truth
     by at most one variance allowance, so two honest readings may differ by at
     most two. This protects the number that gets recorded on chain.

  3. Zone reachability. Each reading is classified into one of three zones
     against a tolerance band straddling the threshold: ``pass`` above the band,
     ``fail`` below it, ``borderline`` inside it. The leader's conclusion must
     be supportable from the validator's own reading widened by the validator's
     own allowance. This protects the settlement: agreeing that a threshold was
     cleared because the leader said so and happened to be nearby is not
     verification.

Settlement reads the zone, never the raw number, so a reading that cannot be
distinguished from the threshold can never settle on one validator's lucky
sample. Borderline readings do not fail and do not pay. They are deferred: the
contract waits (six hours by default) and re-reads. Engagement metrics are
monotonic, so a genuinely borderline case resolves itself upward with time; a
stalled one runs out the sponsor's deadline and refunds.

Anti-hallucination
------------------
Before any model sees the page, a deterministic scanner extracts every numeric
token near the metric's keywords and expands abbreviations (``12.4k`` -> 12400).
The model is then asked to pick the metric's value, and its answer is rejected
unless it matches one of the scanned numbers. The model decides *which* number
on the page is the metric; it is never permitted to invent the digits.
"""

from genlayer import *

import json
import re
import typing
from dataclasses import dataclass
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Error classification
#
# Validators compare failures as well as successes. The prefix tells the
# comparator how much agreement a given failure demands.
# ---------------------------------------------------------------------------

ERROR_EXPECTED = "[EXPECTED]"
"""Deterministic business-logic failure. Messages must match exactly."""

ERROR_EXTERNAL = "[EXTERNAL]"
"""Upstream rejected the request (4xx). Deterministic enough to match exactly."""

ERROR_TRANSIENT = "[TRANSIENT]"
"""Network or 5xx failure. Both sides failing transiently counts as agreement."""

ERROR_LLM = "[LLM_ERROR]"
"""Model returned something unusable. Never agree; force validator rotation."""


# ---------------------------------------------------------------------------
# Milestone lifecycle
# ---------------------------------------------------------------------------

STATUS_OPEN = "open"
"""Escrow funded, no claim pending. The creator may claim until the deadline."""

STATUS_DEFERRED = "deferred"
"""Last reading was borderline. A re-check unlocks after the defer window."""

STATUS_CONTESTED = "contested"
"""Repeatedly borderline. Still re-checkable, but no longer counting down."""

STATUS_ACHIEVED = "achieved"
"""Threshold cleared and the reward paid out. Terminal."""

STATUS_EXPIRED = "expired"
"""Deadline passed without a clear pass. Escrow refundable to the sponsor."""

STATUS_REFUNDED = "refunded"
"""Escrow returned to the sponsor. Terminal."""

STATUS_CANCELLED = "cancelled"
"""Withdrawn inside the creation window before any claim. Terminal."""

_ALL_STATUSES = (
    STATUS_OPEN,
    STATUS_DEFERRED,
    STATUS_CONTESTED,
    STATUS_ACHIEVED,
    STATUS_EXPIRED,
    STATUS_REFUNDED,
    STATUS_CANCELLED,
)

_TERMINAL_STATUSES = (STATUS_ACHIEVED, STATUS_REFUNDED, STATUS_CANCELLED)


# ---------------------------------------------------------------------------
# Adjudication zones
# ---------------------------------------------------------------------------

ZONE_PASS = "pass"
ZONE_BORDERLINE = "borderline"
ZONE_FAIL = "fail"
ZONE_UNREADABLE = "unreadable"

_ZONE_RANK = {ZONE_FAIL: 0, ZONE_BORDERLINE: 1, ZONE_PASS: 2}
"""Zones are totally ordered by value, which makes zone agreement derivable."""


# ---------------------------------------------------------------------------
# Fetch strategies
# ---------------------------------------------------------------------------

FETCH_RENDER_TEXT = "render_text"
"""Headless browser, readable text. Default; handles JavaScript-rendered counts."""

FETCH_RENDER_HTML = "render_html"
"""Headless browser, full DOM. Use when the count only exists in an attribute."""

FETCH_HTTP_GET = "http_get"
"""Plain HTTP GET. Cheapest; only for static pages and JSON endpoints."""

_FETCH_MODES = (FETCH_RENDER_TEXT, FETCH_RENDER_HTML, FETCH_HTTP_GET)


# ---------------------------------------------------------------------------
# Numeric limits and scanner tuning
# ---------------------------------------------------------------------------

BPS_DENOM = 10000
"""Basis-point denominator. All ratios in this contract are integer bps."""

_MAX_FEE_BPS = 1000
"""Hard cap on the protocol fee: 10 percent. Can only ever be lowered."""

_MAX_BAND_BPS = 5000
_MAX_VARIANCE_BPS = 5000
_MAX_METRIC_VALUE = 10**15
_MAX_URL_LEN = 512
_MAX_MEMO_LEN = 280
_MAX_PAGE_SCAN_CHARS = 400_000
_MAX_CONTEXT_CHARS = 6000
_MAX_CANDIDATES = 96
_KEYWORD_WINDOW = 160
_KEYWORD_HITS_PER_TERM = 8
_MAX_LIST_LIMIT = 50
_MAX_DEFER_ROUNDS_CAP = 32
_EVIDENCE_DISPLAY_LEN = 120
_EVIDENCE_REASON_LEN = 200

_SUFFIX_MULT = {"k": 1000, "m": 1000000, "b": 1000000000}

_GROUPING_SPACES = ("\xa0", "\u202f", "\u2009", " ")
"""Space characters platforms use as digit group separators."""

_TOKEN_RE = re.compile(r"\d[\d.,\xa0\u202f\u2009 ]*\d|\d")
_SUFFIX_RE = re.compile(r"\s{0,2}([KkMmBb])(?![A-Za-z])")
_WAIT_RE = re.compile(r"^\d{1,5}(ms|s)$")
_SEGMENT_RE = re.compile(r"[.,\s]+")
_WHITESPACE_RE = re.compile(r"\s+")  # `\s` already covers the Unicode spaces
_JSON_TRAILING_COMMA_RE = re.compile(r",(?=\s*[}\]])")
_KEY_RE = re.compile(r"^[a-z0-9_]{2,32}$")


# ---------------------------------------------------------------------------
# Bootstrapped registries
#
# A deployment is immediately useful without any admin calls, and the owner can
# extend both registries later without redeploying.
# ---------------------------------------------------------------------------

_DEFAULT_METRIC_KEYWORDS = (
    ("views", "view,views,plays,watched,impressions"),
    ("stars", "star,stars,stargazers"),
    ("forks", "fork,forks,forked"),
    ("likes", "like,likes,upvote,upvotes,reactions"),
    ("subscribers", "subscriber,subscribers"),
    ("followers", "follower,followers"),
    ("downloads", "download,downloads,installs,weekly downloads"),
    ("watchers", "watcher,watchers,watching"),
    ("reposts", "repost,reposts,retweet,retweets,shares"),
    ("comments", "comment,comments,replies"),
    ("contributors", "contributor,contributors"),
    ("issues", "issue,issues"),
)

# key, label, metrics, hosts, fetch_mode, wait, extra keywords
_DEFAULT_PLATFORMS = (
    (
        "github",
        "GitHub repository",
        "stars,forks,watchers,contributors,issues",
        "github.com",
        FETCH_RENDER_TEXT,
        "0ms",
        "used by,sponsor",
    ),
    (
        "youtube",
        "YouTube video or channel",
        "views,likes,subscribers,comments",
        "youtube.com,youtu.be",
        FETCH_RENDER_TEXT,
        "4s",
        "waiting,streamed",
    ),
    (
        "x",
        "X post or profile",
        "likes,reposts,views,followers,comments",
        "x.com,twitter.com",
        FETCH_RENDER_TEXT,
        "5s",
        "quotes,bookmarks",
    ),
    (
        "npm",
        "npm package",
        "downloads",
        "npmjs.com",
        FETCH_RENDER_TEXT,
        "3s",
        "weekly,last week",
    ),
    (
        "generic_web",
        "Any HTTPS page",
        "views,stars,forks,likes,subscribers,followers,downloads,watchers,reposts,comments,contributors,issues",
        "*",
        FETCH_RENDER_TEXT,
        "3s",
        "",
    ),
)


# ---------------------------------------------------------------------------
# Value transfer
#
# Paying a creator or refunding a sponsor is an external message from the
# intelligent contract back to the chain layer, executed by this contract's
# ghost contract. That path works for externally owned accounts and for
# chain-layer addresses. Beneficiaries must be addresses that can receive a
# plain value transfer.
# ---------------------------------------------------------------------------


@gl.evm.contract_interface
class _Payee:
    class View:
        pass

    class Write:
        pass


def _send_value(to: Address, atto: int) -> None:
    if atto <= 0:
        return
    _Payee(to).emit_transfer(value=u256(atto))


# ---------------------------------------------------------------------------
# Pure helpers
#
# Everything below this line is deterministic and side-effect free. The
# equivalence principle, the numeric scanner and the zone classifier all live
# here so that they can be unit tested in isolation and replayed by reviewers.
# ---------------------------------------------------------------------------


def _now_ts() -> int:
    """Transaction timestamp in Unix seconds. Identical on every validator."""
    return int(datetime.now(timezone.utc).timestamp())


def _zero_address() -> Address:
    return Address(bytes(20))


def _as_address(value: typing.Any, label: str) -> Address:
    """
    Normalise an address argument, or fail deterministically.

    The ABI types these parameters as addresses, but a caller can still encode a
    raw twenty byte value. Both denote the same thing, so accept either and turn
    anything else into a classified business-logic error rather than letting it
    surface later as an opaque VM fault. Deterministic errors must match
    verbatim across validators, so they need to be raised deliberately.
    """
    if isinstance(value, Address):
        return value
    try:
        return Address(value)
    except Exception:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} {label} is not a valid address")


def _clip(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit]


def _split_csv(raw: str) -> list[str]:
    out: list[str] = []
    for part in str(raw).split(","):
        item = part.strip().lower()
        if item and item not in out:
            out.append(item)
    return out


def _variance_allowance(value: int, variance_bps: int, variance_floor: int) -> int:
    """
    How far a single honest reading may sit from the unobservable true count.

    Proportional for large counts, with an absolute floor so that small counts
    are not held to an impossible standard.
    """
    scaled = (abs(int(value)) * int(variance_bps)) // BPS_DENOM
    floor = abs(int(variance_floor))
    return scaled if scaled > floor else floor


def _band_width(threshold: int, band_bps: int) -> int:
    """Half-width of the tolerance band straddling the threshold."""
    return (abs(int(threshold)) * int(band_bps)) // BPS_DENOM


def _classify(value: int, threshold: int, band_bps: int) -> str:
    """
    Place a reading in one of three zones against the threshold's band.

    Monotonic in ``value``: raising the reading can only move the zone up the
    ``fail -> borderline -> pass`` ladder, never down. Zone agreement between
    two validators therefore follows from their readings being close.
    """
    band = _band_width(threshold, band_bps)
    if int(value) >= int(threshold) + band:
        return ZONE_PASS
    if int(value) < int(threshold) - band:
        return ZONE_FAIL
    return ZONE_BORDERLINE


def _decision_lines(threshold: int, band_bps: int) -> dict:
    """The two numbers a sponsor and creator actually need to know."""
    band = _band_width(threshold, band_bps)
    return {
        "threshold": int(threshold),
        "band": int(band),
        "certain_pass_at": int(threshold) + band,
        "certain_fail_below": int(threshold) - band,
    }


def _squash_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text)


def _strip_group_spaces(token: str) -> str:
    cleaned = str(token)
    for ch in _GROUPING_SPACES:
        cleaned = cleaned.replace(ch, "")
    return cleaned


def _interpret_number(token: str, mult: int) -> list[int]:
    """
    Every plausible integer reading of one numeric token from a page.

    ``.`` and ``,`` are ambiguous across locales, and squashed whitespace can
    join two separate numbers into one token, so this produces the grouping
    reading, the decimal reading, and each individual digit run. Over-generating
    is safe: the scanner's only job is to bound what the model may claim it saw,
    and every value produced here is literally printed on the page.
    """
    cleaned = _strip_group_spaces(token)
    if not cleaned or not cleaned[0].isdigit():
        return []

    digits_only = cleaned.replace(",", "").replace(".", "")
    if not digits_only.isdigit() or len(digits_only) > 18:
        return []

    out: list[int] = [int(digits_only) * mult]

    comma_at = cleaned.rfind(",")
    dot_at = cleaned.rfind(".")
    cut = comma_at if comma_at > dot_at else dot_at
    if cut > 0:
        head = cleaned[:cut].replace(",", "").replace(".", "")
        frac = cleaned[cut + 1 :].replace(",", "").replace(".", "")
        if head.isdigit() and frac.isdigit() and 0 < len(frac) <= 6:
            scale = 10 ** len(frac)
            out.append((int(head + frac) * mult) // scale)
            out.append(int(head) * mult)

    for segment in _SEGMENT_RE.split(str(token)):
        if segment.isdigit() and len(segment) <= 18:
            out.append(int(segment))

    return out


def _expand_abbreviated(token: str, mult: int) -> typing.Optional[int]:
    """
    The single intended reading of a count the model reported back.

    Unlike :func:`_interpret_number`, which enumerates possibilities for the
    scanner, this commits to one answer: without a magnitude suffix every
    separator is digit grouping, and with one the last separator is a decimal
    point. ``12.4K`` is 12400, ``12,431`` is 12431.
    """
    cleaned = _strip_group_spaces(token)
    if not cleaned or not cleaned[0].isdigit():
        return None
    digits_only = cleaned.replace(",", "").replace(".", "")
    if not digits_only.isdigit() or len(digits_only) > 18:
        return None
    if mult == 1:
        return int(digits_only)

    comma_at = cleaned.rfind(",")
    dot_at = cleaned.rfind(".")
    cut = comma_at if comma_at > dot_at else dot_at
    if cut <= 0:
        return int(digits_only) * mult

    head = cleaned[:cut].replace(",", "").replace(".", "")
    frac = cleaned[cut + 1 :].replace(",", "").replace(".", "")
    if not head.isdigit() or not frac.isdigit() or not 0 < len(frac) <= 6:
        return None
    return (int(head + frac) * mult) // (10 ** len(frac))


def _numeric_candidates(text: str, cap: int = _MAX_CANDIDATES) -> list[int]:
    """
    Deterministic scan of every number the page actually shows.

    Collected in document order (keyword windows put the relevant numbers
    first), de-duplicated, then sorted for a stable prompt.
    """
    seen: dict[int, bool] = {}
    ordered: list[int] = []
    for match in _TOKEN_RE.finditer(text):
        if len(ordered) >= cap:
            break
        tail = text[match.end() : match.end() + 4]
        suffix = _SUFFIX_RE.match(tail)
        mult = _SUFFIX_MULT[suffix.group(1).lower()] if suffix else 1
        readings = _interpret_number(match.group(0), mult)
        if mult != 1:
            readings = readings + _interpret_number(match.group(0), 1)
        for value in readings:
            if value < 0 or value > _MAX_METRIC_VALUE:
                continue
            if value in seen:
                continue
            seen[value] = True
            ordered.append(value)
            if len(ordered) >= cap:
                break
    return sorted(ordered)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def _collect_evidence(text: str, keywords: list[str]) -> dict:
    """
    Narrow a page down to the regions that mention the metric, then scan them.

    Returns the prompt context, the allowed numbers, and whether the context was
    keyword-scoped. Falling back to the page head is a weaker claim ("this
    number is somewhere on the page" rather than "next to the metric word") and
    is reported so that the evidence record stays honest.
    """
    text = text[:_MAX_PAGE_SCAN_CHARS]
    lowered = text.lower()

    spans: list[tuple[int, int]] = []
    for keyword in keywords:
        if not keyword:
            continue
        cursor = 0
        hits = 0
        while hits < _KEYWORD_HITS_PER_TERM:
            found = lowered.find(keyword, cursor)
            if found < 0:
                break
            spans.append(
                (
                    max(0, found - _KEYWORD_WINDOW),
                    min(len(text), found + len(keyword) + _KEYWORD_WINDOW),
                )
            )
            cursor = found + len(keyword)
            hits += 1

    merged = _merge_spans(spans)
    scoped = len(merged) > 0

    def build(regions: list[tuple[int, int]]) -> str:
        parts: list[str] = []
        used = 0
        for start, end in regions:
            if used >= _MAX_CONTEXT_CHARS:
                break
            piece = text[start : min(end, start + (_MAX_CONTEXT_CHARS - used))]
            if piece:
                parts.append(piece)
                used += len(piece)
        return " [...] ".join(parts)

    if scoped:
        context = build(merged)
        candidates = _numeric_candidates(context)
        if not candidates:
            scoped = False
    if not scoped:
        context = build([(0, min(len(text), _MAX_CONTEXT_CHARS))])
        candidates = _numeric_candidates(context)

    return {
        "context": context,
        "candidates": candidates,
        "scoped": scoped,
        "page_chars": len(text),
    }


def _parse_json_object(raw: typing.Any) -> dict:
    """Coerce a model response into a dict, tolerating the usual wrapping."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise gl.vm.UserError(f"{ERROR_LLM} unusable response type {type(raw).__name__}")
    text = raw.strip()
    first = text.find("{")
    last = text.rfind("}")
    if first < 0 or last <= first:
        raise gl.vm.UserError(f"{ERROR_LLM} response contained no JSON object")
    text = _JSON_TRAILING_COMMA_RE.sub("", text[first : last + 1])
    try:
        parsed = json.loads(text)
    except Exception:
        raise gl.vm.UserError(f"{ERROR_LLM} response was not valid JSON")
    if not isinstance(parsed, dict):
        raise gl.vm.UserError(f"{ERROR_LLM} response JSON was not an object")
    return parsed


def _coerce_count(raw: typing.Any) -> int:
    """
    Turn whatever the model put in ``value`` into a non-negative integer.

    Accepts ints, numeric strings, grouped strings and abbreviations, because
    models drift between all of them regardless of instructions.
    """
    if isinstance(raw, bool):
        raise gl.vm.UserError(f"{ERROR_LLM} `value` was a boolean")
    if isinstance(raw, int):
        value = raw
    else:
        token = str(raw).strip()
        if not token:
            raise gl.vm.UserError(f"{ERROR_LLM} `value` was empty")
        mult = 1
        if token[-1] in "kKmMbB":
            mult = _SUFFIX_MULT[token[-1].lower()]
            token = token[:-1].strip()
        expanded = _expand_abbreviated(token, mult)
        if expanded is None:
            raise gl.vm.UserError(f"{ERROR_LLM} `value` was not numeric")
        value = expanded
    if value < 0:
        raise gl.vm.UserError(f"{ERROR_LLM} `value` was negative")
    if value > _MAX_METRIC_VALUE:
        raise gl.vm.UserError(f"{ERROR_LLM} `value` exceeded the supported range")
    return int(value)


def _value_is_on_page(value: int, candidates: list[int]) -> bool:
    """
    The anti-hallucination gate.

    The model chooses which number on the page is the metric. It may not invent
    digits. A half-percent slack absorbs differences in how an abbreviation is
    expanded, nothing more.
    """
    if not candidates:
        return False
    slack = value * 5 // 1000
    if slack < 1:
        slack = 1
    for candidate in candidates:
        if abs(candidate - value) <= slack:
            return True
    return False


def _agreement_report(
    leader: typing.Any,
    mine: dict,
    threshold: int,
    band_bps: int,
    variance_bps: int,
    variance_floor: int,
) -> dict:
    """
    The equivalence principle, as a transparent record rather than a bare bool.

    Three gates, each protecting something different.

    ``self_classification``
        The leader's zone is recomputed from the leader's own value. A leader
        cannot report one reading and label it as another.

    ``raw_proximity``
        The two readings must be mutually consistent. Each honest reading may
        sit one variance allowance from the unobservable true count, so two
        honest readings may differ by two allowances. This gate protects the
        *number that gets recorded*: without it a leader inside the right zone
        could still write an inflated count into the milestone's evidence.

    ``zone_reachability``
        The leader's conclusion must be supportable from this validator's own
        reading, widened by this validator's own allowance. This gate protects
        the *settlement*: agreeing that a threshold was cleared because the
        leader said so and happened to be nearby is not verification. Note the
        single allowance here, not the combined one. Softness near the
        threshold is the tolerance band's job, and the band answers it by
        deferring rather than by lowering the bar for agreement.
    """
    report: dict = {
        "agree": False,
        "reason": "",
        "leader_value": 0,
        "validator_value": int(mine.get("value", 0)),
        "leader_zone": "",
        "validator_zone": str(mine.get("zone", "")),
        "combined_allowance": 0,
        "validator_allowance": 0,
        "gap": 0,
        "span_low_zone": "",
        "span_high_zone": "",
    }

    if not isinstance(leader, dict):
        report["reason"] = "leader result was not an observation record"
        return report

    for key in ("found", "value", "zone"):
        if key not in leader:
            report["reason"] = f"leader observation missing `{key}`"
            return report

    leader_found = bool(leader["found"])
    mine_found = bool(mine.get("found", False))
    if leader_found != mine_found:
        report["reason"] = (
            f"readability mismatch: leader found={leader_found}, validator found={mine_found}"
        )
        return report

    if not leader_found:
        leader_zone = str(leader["zone"])
        report["leader_zone"] = leader_zone
        if leader_zone != ZONE_UNREADABLE or report["validator_zone"] != ZONE_UNREADABLE:
            report["reason"] = "unreadable observation was not labelled unreadable"
            return report
        report["agree"] = True
        report["reason"] = "both sides read the page and found no such metric"
        return report

    raw_leader_value = leader["value"]
    if isinstance(raw_leader_value, bool) or not isinstance(raw_leader_value, int):
        report["reason"] = "leader `value` was not an integer"
        return report
    leader_value = int(raw_leader_value)
    mine_value = int(mine.get("value", 0))
    report["leader_value"] = leader_value
    if leader_value < 0 or leader_value > _MAX_METRIC_VALUE:
        report["reason"] = "leader `value` was out of range"
        return report

    leader_zone = str(leader["zone"])
    report["leader_zone"] = leader_zone
    if leader_zone not in _ZONE_RANK:
        report["reason"] = f"leader zone `{leader_zone}` is not an adjudication zone"
        return report

    recomputed = _classify(leader_value, threshold, band_bps)
    if recomputed != leader_zone:
        report["reason"] = (
            f"leader labelled {leader_value} as `{leader_zone}` but it classifies as `{recomputed}`"
        )
        return report

    leader_allowance = _variance_allowance(leader_value, variance_bps, variance_floor)
    mine_allowance = _variance_allowance(mine_value, variance_bps, variance_floor)
    combined = leader_allowance + mine_allowance
    gap = abs(leader_value - mine_value)
    report["combined_allowance"] = combined
    report["validator_allowance"] = mine_allowance
    report["gap"] = gap

    if gap > combined:
        report["reason"] = (
            f"readings differ by {gap}, beyond the combined variance allowance of {combined}"
        )
        return report

    low = mine_value - mine_allowance
    if low < 0:
        low = 0
    span_low = _classify(low, threshold, band_bps)
    span_high = _classify(mine_value + mine_allowance, threshold, band_bps)
    report["span_low_zone"] = span_low
    report["span_high_zone"] = span_high

    if not (_ZONE_RANK[span_low] <= _ZONE_RANK[leader_zone] <= _ZONE_RANK[span_high]):
        report["reason"] = (
            f"leader concluded `{leader_zone}` but this validator's reading of "
            f"{mine_value} only supports `{span_low}`..`{span_high}`"
        )
        return report

    report["agree"] = True
    report["reason"] = (
        f"readings {leader_value} and {mine_value} differ by {gap} within {combined}, "
        f"and `{leader_zone}` is supportable from {mine_value}"
    )
    return report


def _error_class(message: str) -> str:
    """Name the classification prefix a failure carries, or ``unclassified``."""
    for prefix, name in (
        (ERROR_EXPECTED, "expected"),
        (ERROR_EXTERNAL, "external"),
        (ERROR_TRANSIENT, "transient"),
        (ERROR_LLM, "llm"),
    ):
        if str(message).startswith(prefix):
            return name
    return "unclassified"


def _compare_user_errors(leader: gl.vm.UserError, mine: gl.vm.UserError) -> bool:
    """
    Decide whether two failures count as the same failure.

    Deterministic failures must match verbatim. Transient failures only need to
    be transient on both sides. Model failures never agree, so that consensus
    rotates onto validators whose model behaved.
    """
    leader_msg = str(getattr(leader, "message", ""))
    mine_msg = str(getattr(mine, "message", ""))
    if leader_msg.startswith(ERROR_EXPECTED) or leader_msg.startswith(ERROR_EXTERNAL):
        return leader_msg == mine_msg
    if leader_msg.startswith(ERROR_TRANSIENT) and mine_msg.startswith(ERROR_TRANSIENT):
        return True
    return False


def _host_of(url: str) -> str:
    """
    Extract the host from an HTTPS URL without a URL library.

    Plain HTTP and embedded credentials are rejected outright rather than
    normalised, because both are ways to make a URL read as one origin and
    resolve as another.
    """
    raw = str(url).strip()
    if "\n" in raw or "\r" in raw or " " in raw:
        return ""
    if not raw.lower().startswith("https://"):
        return ""
    rest = raw[8:]
    for separator in ("/", "?", "#"):
        index = rest.find(separator)
        if index >= 0:
            rest = rest[:index]
    if not rest or "@" in rest:
        return ""
    if rest.startswith("["):
        return ""
    if ":" in rest:
        rest, _, port = rest.partition(":")
        if not port.isdigit():
            return ""
    host = rest.lower().strip(".")
    if not host or "." not in host:
        return ""
    for ch in host:
        if not (ch.isalnum() or ch in "-."):
            return ""
    return host


def _host_allowed(host: str, allow: list[str]) -> bool:
    if not host:
        return False
    for entry in allow:
        if entry == "*":
            return True
        if host == entry or host.endswith("." + entry):
            return True
    return False


def _build_prompt(metric: str, keywords: list[str], evidence: dict) -> str:
    candidates = ", ".join(str(value) for value in evidence["candidates"])
    scope_note = (
        "These excerpts are the regions of the page that mention the metric."
        if evidence["scoped"]
        else "The metric keywords were not found, so this is the head of the page."
    )
    return f"""You are reading the visible text of a public web page to report one engagement metric exactly as the page displays it.

METRIC REQUESTED: {metric}
WORDS THE PAGE MAY USE FOR IT: {", ".join(keywords)}

{scope_note}

PAGE EXCERPTS
<<<
{evidence["context"]}
>>>

NUMBERS A DETERMINISTIC SCANNER FOUND IN THOSE EXCERPTS
{candidates if candidates else "(none)"}

Reply with JSON only, in exactly this shape:
{{"found": true, "value": 12400, "display": "12.4K stars", "reason": "one short sentence"}}

Rules:
- `value` must be the full integer count with abbreviations expanded, so 12.4K becomes 12400 and 1.5M becomes 1500000.
- `value` must be one of the scanner numbers listed above. You are choosing which of those numbers is the requested metric, not computing a new one.
- If none of the scanner numbers is the requested metric, reply {{"found": false, "value": 0, "display": "", "reason": "why not"}}.
- Never substitute a different metric. If stars were requested, do not report forks, watchers or issues.
- Do not estimate, extrapolate or add numbers together. Report only what the page displays.
- `display` must be the literal text as it appears on the page.
"""


def _fetch_page(url: str, fetch_mode: str, wait_after_loaded: str) -> str:
    """Retrieve the page, mapping every failure onto a classified error."""
    try:
        if fetch_mode == FETCH_HTTP_GET:
            response = gl.nondet.web.get(url)
            status = int(response.status)
            if 400 <= status < 500:
                raise gl.vm.UserError(
                    f"{ERROR_EXTERNAL} content not retrievable: HTTP {status}"
                )
            if status >= 500:
                raise gl.vm.UserError(
                    f"{ERROR_TRANSIENT} upstream unavailable: HTTP {status}"
                )
            body = response.body or b""
            return bytes(body).decode("utf-8", errors="replace")
        mode = "html" if fetch_mode == FETCH_RENDER_HTML else "text"
        rendered = gl.nondet.web.render(
            url, mode=mode, wait_after_loaded=wait_after_loaded
        )
        return str(rendered)
    except gl.vm.UserError:
        raise
    except Exception:
        # Deliberately message-free: renderer exception text varies between
        # validators, and transient failures agree on the prefix alone.
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} could not retrieve the content page")


def _observe(spec: dict) -> dict:
    """
    One independent reading of the metric. Run by the leader and by every
    validator, with no shared state beyond the immutable ``spec``.
    """
    page = _squash_whitespace(
        _fetch_page(spec["url"], spec["fetch_mode"], spec["wait_after_loaded"])
    )
    if not page.strip():
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} content page returned no text")

    keywords: list[str] = list(spec["keywords"])
    evidence = _collect_evidence(page, keywords)

    raw = gl.nondet.exec_prompt(
        _build_prompt(spec["metric"], keywords, evidence), response_format="json"
    )
    parsed = _parse_json_object(raw)

    found_raw = parsed.get("found", parsed.get("present", None))
    if found_raw is None:
        raise gl.vm.UserError(f"{ERROR_LLM} response omitted `found`")
    if isinstance(found_raw, str):
        found = found_raw.strip().lower() in ("true", "yes", "1")
    else:
        found = bool(found_raw)

    display = _clip(parsed.get("display", ""), _EVIDENCE_DISPLAY_LEN)
    reason = _clip(parsed.get("reason", parsed.get("explanation", "")), _EVIDENCE_REASON_LEN)

    if not found:
        return {
            "found": False,
            "value": 0,
            "zone": ZONE_UNREADABLE,
            "display": display,
            "reason": reason,
            "candidate_count": len(evidence["candidates"]),
            "scoped": bool(evidence["scoped"]),
            "page_chars": int(evidence["page_chars"]),
        }

    if "value" not in parsed and "count" not in parsed:
        raise gl.vm.UserError(f"{ERROR_LLM} response omitted `value`")
    value = _coerce_count(parsed.get("value", parsed.get("count")))

    if not _value_is_on_page(value, evidence["candidates"]):
        raise gl.vm.UserError(
            f"{ERROR_LLM} reported {value} which the page scanner did not find"
        )

    return {
        "found": True,
        "value": value,
        "zone": _classify(value, spec["threshold"], spec["band_bps"]),
        "display": display,
        "reason": reason,
        "candidate_count": len(evidence["candidates"]),
        "scoped": bool(evidence["scoped"]),
        "page_chars": int(evidence["page_chars"]),
    }


def _adjudicate(spec: dict) -> dict:
    """
    Run one adjudication round through consensus.

    The leader reads the page and classifies. Every validator reads the page
    independently and then applies :func:`_agreement_report`. Nothing from the
    leader feeds the validator's own reading, so agreement is evidence of a
    reproducible fact rather than of well-formatted output.
    """

    def leader_fn() -> dict:
        return _observe(spec)

    def validator_fn(leaders_result: gl.vm.Result) -> bool:
        # Read the page first. If this raises, the validator's failure is
        # compared against the leader's failure by _compare_user_errors.
        mine = _observe(spec)
        if not isinstance(leaders_result, gl.vm.Return):
            return False
        return bool(
            _agreement_report(
                leaders_result.calldata,
                mine,
                spec["threshold"],
                spec["band_bps"],
                spec["variance_bps"],
                spec["variance_floor"],
            )["agree"]
        )

    return gl.vm.run_nondet(
        leader_fn, validator_fn, compare_user_errors=_compare_user_errors
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@allow_storage
@dataclass
class PlatformSpec:
    """How to read a given platform. Owner-governed, extensible post-deployment."""

    key: str
    label: str
    metrics: str
    hosts: str
    fetch_mode: str
    wait_after_loaded: str
    extra_keywords: str
    enabled: bool
    registered_at: u256


@allow_storage
@dataclass
class Milestone:
    """One escrowed milestone and its full adjudication history summary."""

    milestone_id: str
    sponsor: Address
    creator: Address
    content_url: str
    platform_key: str
    metric: str
    threshold: u256
    deadline_ts: u256
    reward_atto: u256
    band_bps: u256
    variance_bps: u256
    variance_floor: u256
    status: str
    attempts: u256
    defer_rounds: u256
    observed_value: u256
    observed_at: u256
    deferred_until: u256
    claimed_at: u256
    created_at: u256
    resolved_at: u256
    payout_atto: u256
    fee_atto: u256
    evidence: str
    memo: str


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class MilestoneCreated(gl.Event):
    def __init__(self, milestone_id: str, sponsor: Address, /, **blob): ...


class RewardToppedUp(gl.Event):
    def __init__(self, milestone_id: str, /, **blob): ...


class ClaimSubmitted(gl.Event):
    def __init__(self, milestone_id: str, creator: Address, /, **blob): ...


class ObservationRecorded(gl.Event):
    def __init__(self, milestone_id: str, zone: str, /, **blob): ...


class MilestoneAchieved(gl.Event):
    def __init__(self, milestone_id: str, creator: Address, /, **blob): ...


class MilestoneDeferred(gl.Event):
    def __init__(self, milestone_id: str, /, **blob): ...


class MilestoneContested(gl.Event):
    def __init__(self, milestone_id: str, /, **blob): ...


class ClaimWithdrawn(gl.Event):
    def __init__(self, milestone_id: str, /, **blob): ...


class MilestoneExpired(gl.Event):
    def __init__(self, milestone_id: str, /, **blob): ...


class RewardRefunded(gl.Event):
    def __init__(self, milestone_id: str, sponsor: Address, /, **blob): ...


class MilestoneCancelled(gl.Event):
    def __init__(self, milestone_id: str, /, **blob): ...


class PlatformRegistered(gl.Event):
    def __init__(self, platform_key: str, /, **blob): ...


class MetricKeywordsUpdated(gl.Event):
    def __init__(self, metric: str, /, **blob): ...


class ConfigUpdated(gl.Event):
    def __init__(self, field: str, /, **blob): ...


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class ContentMilestoneOracle(gl.Contract):
    """Escrowed, consensus-adjudicated content milestone grants."""

    owner: Address
    paused: bool

    default_band_bps: u256
    default_variance_bps: u256
    default_variance_floor: u256
    defer_seconds: u256
    max_defer_rounds: u256
    resolution_grace_seconds: u256
    cancel_window_seconds: u256
    min_reward_atto: u256
    max_deadline_horizon: u256

    fee_bps: u256
    fee_recipient: Address

    milestones: TreeMap[str, Milestone]
    milestone_ids: DynArray[str]
    dedupe: TreeMap[str, str]
    by_sponsor: TreeMap[Address, DynArray[str]]
    by_creator: TreeMap[Address, DynArray[str]]
    status_counts: TreeMap[str, u256]

    platforms: TreeMap[str, PlatformSpec]
    platform_keys: DynArray[str]
    metric_keywords: TreeMap[str, str]
    metric_keys: DynArray[str]

    next_sequence: u256
    total_escrowed_atto: u256
    total_paid_atto: u256
    total_refunded_atto: u256
    total_fees_atto: u256

    def __init__(
        self,
        fee_bps: int = 0,
        fee_recipient: str = "",
        default_band_bps: int = 100,
        default_variance_bps: int = 50,
        default_variance_floor: int = 5,
        defer_seconds: int = 21600,
        max_defer_rounds: int = 4,
        resolution_grace_seconds: int = 172800,
        cancel_window_seconds: int = 3600,
        min_reward_atto: int = 0,
        max_deadline_horizon: int = 31536000,
    ):
        """
        :param fee_bps: protocol fee on successful payouts, in bps, max 1000.
            May be lowered later but never raised.
        :param fee_recipient: hex address that receives the fee. Empty disables it.
        :param default_band_bps: tolerance band half-width around the threshold,
            in bps. 100 means a reading must reach threshold + 1 percent to be a
            certain pass, and fall below threshold - 1 percent to be a certain
            fail. Anything between defers.
        :param default_variance_bps: how far one reading may sit from the truth,
            in bps of the reading.
        :param default_variance_floor: absolute floor for that allowance, so
            small counts are not held to an impossible standard.
        :param defer_seconds: wait between re-checks of a borderline reading.
        :param max_defer_rounds: borderline rounds before a milestone is marked
            contested.
        :param resolution_grace_seconds: how long after the deadline a deferred
            or contested milestone may still be resolved before it expires.
        :param cancel_window_seconds: window after creation in which a sponsor
            may cancel an unclaimed milestone.
        :param min_reward_atto: smallest acceptable escrow, in wei.
        :param max_deadline_horizon: furthest into the future a deadline may be set.
        """
        now = _now_ts()

        self.owner = gl.message.sender_address
        self.paused = False

        if fee_bps < 0 or fee_bps > _MAX_FEE_BPS:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} fee_bps must be between 0 and {_MAX_FEE_BPS}"
            )
        self.fee_bps = u256(int(fee_bps))
        self.fee_recipient = (
            Address(fee_recipient) if fee_recipient else _zero_address()
        )

        self._assign_consensus_params(
            default_band_bps,
            default_variance_bps,
            default_variance_floor,
            defer_seconds,
            max_defer_rounds,
            resolution_grace_seconds,
        )
        self._assign_economics(
            min_reward_atto, max_deadline_horizon, cancel_window_seconds
        )

        self.next_sequence = u256(1)
        self.total_escrowed_atto = u256(0)
        self.total_paid_atto = u256(0)
        self.total_refunded_atto = u256(0)
        self.total_fees_atto = u256(0)

        for status in _ALL_STATUSES:
            self.status_counts[status] = u256(0)

        for metric, keywords in _DEFAULT_METRIC_KEYWORDS:
            self.metric_keywords[metric] = keywords
            self.metric_keys.append(metric)

        for (
            key,
            label,
            metrics,
            hosts,
            fetch_mode,
            wait,
            extra,
        ) in _DEFAULT_PLATFORMS:
            self.platforms[key] = PlatformSpec(
                key=key,
                label=label,
                metrics=metrics,
                hosts=hosts,
                fetch_mode=fetch_mode,
                wait_after_loaded=wait,
                extra_keywords=extra,
                enabled=True,
                registered_at=u256(now),
            )
            self.platform_keys.append(key)

    # -- internal guards ---------------------------------------------------

    def _require_owner(self) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} owner only")

    def _require_active(self) -> None:
        if self.paused:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} contract is paused")

    def _get(self, milestone_id: str) -> Milestone:
        milestone = self.milestones.get(milestone_id)
        if milestone is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown milestone `{milestone_id}`")
        return milestone

    def _assign_consensus_params(
        self,
        band_bps: int,
        variance_bps: int,
        variance_floor: int,
        defer_seconds: int,
        max_defer_rounds: int,
        resolution_grace_seconds: int,
    ) -> None:
        if band_bps < 0 or band_bps > _MAX_BAND_BPS:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} band_bps must be between 0 and {_MAX_BAND_BPS}"
            )
        if variance_bps < 0 or variance_bps > _MAX_VARIANCE_BPS:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} variance_bps must be between 0 and {_MAX_VARIANCE_BPS}"
            )
        if variance_floor < 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} variance_floor must not be negative")
        if defer_seconds < 60:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} defer_seconds must be at least 60")
        if max_defer_rounds < 1 or max_defer_rounds > _MAX_DEFER_ROUNDS_CAP:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} max_defer_rounds must be between 1 and {_MAX_DEFER_ROUNDS_CAP}"
            )
        if resolution_grace_seconds < 0:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} resolution_grace_seconds must not be negative"
            )
        self.default_band_bps = u256(int(band_bps))
        self.default_variance_bps = u256(int(variance_bps))
        self.default_variance_floor = u256(int(variance_floor))
        self.defer_seconds = u256(int(defer_seconds))
        self.max_defer_rounds = u256(int(max_defer_rounds))
        self.resolution_grace_seconds = u256(int(resolution_grace_seconds))

    def _assign_economics(
        self, min_reward_atto: int, max_deadline_horizon: int, cancel_window_seconds: int
    ) -> None:
        if min_reward_atto < 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} min_reward_atto must not be negative")
        if max_deadline_horizon < 3600:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} max_deadline_horizon must be at least 3600"
            )
        if cancel_window_seconds < 0:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} cancel_window_seconds must not be negative"
            )
        self.min_reward_atto = u256(int(min_reward_atto))
        self.max_deadline_horizon = u256(int(max_deadline_horizon))
        self.cancel_window_seconds = u256(int(cancel_window_seconds))

    # -- internal state helpers -------------------------------------------

    def _set_status(self, milestone: Milestone, status: str) -> None:
        previous = str(milestone.status)
        if previous == status:
            return
        if previous:
            current = int(self.status_counts.get(previous, u256(0)))
            self.status_counts[previous] = u256(current - 1 if current > 0 else 0)
        milestone.status = status
        self.status_counts[status] = u256(int(self.status_counts.get(status, u256(0))) + 1)

    def _keywords_for(self, platform: PlatformSpec, metric: str) -> list[str]:
        keywords = _split_csv(self.metric_keywords.get(metric, ""))
        if metric not in keywords:
            keywords.insert(0, metric)
        for extra in _split_csv(str(platform.extra_keywords)):
            if extra not in keywords:
                keywords.append(extra)
        return keywords

    def _spec_for(self, milestone: Milestone) -> dict:
        """Flatten everything the non-deterministic block needs into plain values.

        Storage is unreachable from a non-deterministic block, and the closures
        are serialised, so the spec must be built here and carry no references
        back into contract state.
        """
        platform = self.platforms.get(str(milestone.platform_key))
        if platform is None:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} platform `{milestone.platform_key}` is no longer registered"
            )
        if not bool(platform.enabled):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} platform `{milestone.platform_key}` is disabled"
            )
        return {
            "url": str(milestone.content_url),
            "metric": str(milestone.metric),
            "keywords": self._keywords_for(platform, str(milestone.metric)),
            "fetch_mode": str(platform.fetch_mode),
            "wait_after_loaded": str(platform.wait_after_loaded),
            "threshold": int(milestone.threshold),
            "band_bps": int(milestone.band_bps),
            "variance_bps": int(milestone.variance_bps),
            "variance_floor": int(milestone.variance_floor),
        }

    def _record_evidence(
        self, milestone: Milestone, observation: dict, now: int, round_index: int
    ) -> None:
        milestone.evidence = json.dumps(
            {
                "at": int(now),
                "round": int(round_index),
                "found": bool(observation.get("found", False)),
                "value": int(observation.get("value", 0)),
                "zone": str(observation.get("zone", "")),
                "display": _clip(observation.get("display", ""), _EVIDENCE_DISPLAY_LEN),
                "reason": _clip(observation.get("reason", ""), _EVIDENCE_REASON_LEN),
                "candidates": int(observation.get("candidate_count", 0)),
                "keyword_scoped": bool(observation.get("scoped", False)),
                "page_chars": int(observation.get("page_chars", 0)),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def _release(self, milestone: Milestone, now: int) -> dict:
        """Pay the creator. State is settled before any value leaves."""
        gross = int(milestone.reward_atto)
        fee = (gross * int(self.fee_bps)) // BPS_DENOM
        recipient = self.fee_recipient
        if recipient == _zero_address():
            fee = 0
        net = gross - fee

        milestone.payout_atto = u256(net)
        milestone.fee_atto = u256(fee)
        milestone.resolved_at = u256(now)
        self._set_status(milestone, STATUS_ACHIEVED)

        escrowed = int(self.total_escrowed_atto)
        self.total_escrowed_atto = u256(escrowed - gross if escrowed >= gross else 0)
        self.total_paid_atto = u256(int(self.total_paid_atto) + net)
        self.total_fees_atto = u256(int(self.total_fees_atto) + fee)

        creator = milestone.creator
        _send_value(creator, net)
        if fee > 0:
            _send_value(recipient, fee)

        MilestoneAchieved(
            str(milestone.milestone_id),
            creator,
            value=int(milestone.observed_value),
            threshold=int(milestone.threshold),
            payout_atto=net,
            fee_atto=fee,
            at=now,
        ).emit()
        return {"status": STATUS_ACHIEVED, "payout_atto": net, "fee_atto": fee}

    def _refund_to_sponsor(self, milestone: Milestone, now: int, reason: str) -> dict:
        gross = int(milestone.reward_atto)
        milestone.resolved_at = u256(now)
        self._set_status(milestone, STATUS_REFUNDED)

        escrowed = int(self.total_escrowed_atto)
        self.total_escrowed_atto = u256(escrowed - gross if escrowed >= gross else 0)
        self.total_refunded_atto = u256(int(self.total_refunded_atto) + gross)

        sponsor = milestone.sponsor
        _send_value(sponsor, gross)

        RewardRefunded(
            str(milestone.milestone_id),
            sponsor,
            amount_atto=gross,
            reason=reason,
            at=now,
        ).emit()
        return {"status": STATUS_REFUNDED, "refunded_atto": gross, "reason": reason}

    def _resolution_deadline(self, milestone: Milestone) -> int:
        return int(milestone.deadline_ts) + int(self.resolution_grace_seconds)

    def _apply_observation(
        self, milestone: Milestone, observation: dict, now: int
    ) -> dict:
        """
        Turn an agreed observation into a state transition.

        Settlement reads the zone, never the raw number. A borderline reading is
        a statement that the contract cannot tell the two outcomes apart, so it
        waits instead of guessing.
        """
        milestone_id = str(milestone.milestone_id)
        zone = str(observation.get("zone", ZONE_UNREADABLE))
        value = int(observation.get("value", 0))

        milestone.attempts = u256(int(milestone.attempts) + 1)
        milestone.observed_value = u256(value)
        milestone.observed_at = u256(now)
        self._record_evidence(milestone, observation, now, int(milestone.attempts))

        ObservationRecorded(
            milestone_id,
            zone,
            value=value,
            threshold=int(milestone.threshold),
            attempt=int(milestone.attempts),
            at=now,
        ).emit()

        result: dict = {
            "milestone_id": milestone_id,
            "zone": zone,
            "observed_value": value,
            "threshold": int(milestone.threshold),
            "attempt": int(milestone.attempts),
            "found": bool(observation.get("found", False)),
            "display": _clip(observation.get("display", ""), _EVIDENCE_DISPLAY_LEN),
            "reason": _clip(observation.get("reason", ""), _EVIDENCE_REASON_LEN),
            "decision_lines": _decision_lines(
                int(milestone.threshold), int(milestone.band_bps)
            ),
        }

        if zone == ZONE_PASS:
            milestone.deferred_until = u256(0)
            result.update(self._release(milestone, now))
            return result

        if zone == ZONE_BORDERLINE:
            rounds = int(milestone.defer_rounds) + 1
            milestone.defer_rounds = u256(rounds)
            milestone.deferred_until = u256(now + int(self.defer_seconds))
            if rounds >= int(self.max_defer_rounds):
                self._set_status(milestone, STATUS_CONTESTED)
                MilestoneContested(
                    milestone_id,
                    rounds=rounds,
                    value=value,
                    recheck_after=int(milestone.deferred_until),
                    resolution_deadline=self._resolution_deadline(milestone),
                    at=now,
                ).emit()
            else:
                self._set_status(milestone, STATUS_DEFERRED)
                MilestoneDeferred(
                    milestone_id,
                    rounds=rounds,
                    value=value,
                    recheck_after=int(milestone.deferred_until),
                    at=now,
                ).emit()
            result["status"] = str(milestone.status)
            result["recheck_after"] = int(milestone.deferred_until)
            result["defer_rounds"] = rounds
            return result

        # fail or unreadable: the claim did not carry. The creator keeps the
        # right to claim again while the deadline stands. The borderline ladder
        # belongs to a claim rather than to the milestone, so it resets here
        # just as it does on `withdraw_claim`: a fresh claim gets a fresh set of
        # re-checks.
        milestone.deferred_until = u256(0)
        milestone.claimed_at = u256(0)
        milestone.defer_rounds = u256(0)
        if now > int(milestone.deadline_ts):
            self._set_status(milestone, STATUS_EXPIRED)
            MilestoneExpired(
                milestone_id,
                zone=zone,
                value=value,
                at=now,
            ).emit()
        else:
            self._set_status(milestone, STATUS_OPEN)
        result["status"] = str(milestone.status)
        return result

    # -- administration ----------------------------------------------------

    @gl.public.write
    def transfer_ownership(self, new_owner: Address) -> None:
        """Hand administration to another address."""
        self._require_owner()
        new_owner = _as_address(new_owner, "new_owner")
        if new_owner == _zero_address():
            raise gl.vm.UserError(f"{ERROR_EXPECTED} new owner must not be the zero address")
        self.owner = new_owner
        ConfigUpdated("owner", value=new_owner.as_hex, at=_now_ts()).emit()

    @gl.public.write
    def set_paused(self, flag: bool) -> None:
        """
        Halt new milestones and new adjudications.

        Refunds, cancellations and claim withdrawals stay open while paused, so
        a pause can never trap escrowed funds.
        """
        self._require_owner()
        self.paused = bool(flag)
        ConfigUpdated("paused", value=bool(flag), at=_now_ts()).emit()

    @gl.public.write
    def set_consensus_params(
        self,
        band_bps: int,
        variance_bps: int,
        variance_floor: int,
        defer_seconds: int,
        max_defer_rounds: int,
        resolution_grace_seconds: int,
    ) -> None:
        """
        Update the defaults applied to newly created milestones.

        Milestones already created keep the band and variance they were created
        with, so a sponsor's terms cannot be changed under them.
        """
        self._require_owner()
        self._assign_consensus_params(
            band_bps,
            variance_bps,
            variance_floor,
            defer_seconds,
            max_defer_rounds,
            resolution_grace_seconds,
        )
        ConfigUpdated(
            "consensus_params",
            band_bps=int(band_bps),
            variance_bps=int(variance_bps),
            variance_floor=int(variance_floor),
            defer_seconds=int(defer_seconds),
            max_defer_rounds=int(max_defer_rounds),
            resolution_grace_seconds=int(resolution_grace_seconds),
            at=_now_ts(),
        ).emit()

    @gl.public.write
    def set_economics(
        self,
        min_reward_atto: int,
        max_deadline_horizon: int,
        cancel_window_seconds: int,
    ) -> None:
        """Update escrow floors and timing limits for new milestones."""
        self._require_owner()
        self._assign_economics(
            min_reward_atto, max_deadline_horizon, cancel_window_seconds
        )
        ConfigUpdated(
            "economics",
            min_reward_atto=int(min_reward_atto),
            max_deadline_horizon=int(max_deadline_horizon),
            cancel_window_seconds=int(cancel_window_seconds),
            at=_now_ts(),
        ).emit()

    @gl.public.write
    def set_fee(self, fee_bps: int, fee_recipient: Address) -> None:
        """
        Change the protocol fee. The rate may only ever be lowered.

        A one-way ratchet means an integrator can read the fee once and rely on
        it, rather than re-checking before every payout.
        """
        self._require_owner()
        fee_recipient = _as_address(fee_recipient, "fee_recipient")
        if fee_bps < 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} fee_bps must not be negative")
        if int(fee_bps) > int(self.fee_bps):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} fee_bps may only be lowered, currently {int(self.fee_bps)}"
            )
        self.fee_bps = u256(int(fee_bps))
        self.fee_recipient = fee_recipient
        ConfigUpdated(
            "fee",
            fee_bps=int(fee_bps),
            fee_recipient=fee_recipient.as_hex,
            at=_now_ts(),
        ).emit()

    @gl.public.write
    def register_platform(
        self,
        key: str,
        label: str,
        metrics: str,
        hosts: str,
        fetch_mode: str,
        wait_after_loaded: str,
        extra_keywords: str,
    ) -> None:
        """
        Add or replace a platform reader.

        ``metrics``, ``hosts`` and ``extra_keywords`` are comma separated.
        ``hosts`` entries match a host exactly or as a parent domain, and ``*``
        allows any HTTPS host.
        """
        self._require_owner()
        key = str(key).strip().lower()
        if not _KEY_RE.match(key):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} platform key must be 2-32 chars of a-z, 0-9 or underscore"
            )
        if fetch_mode not in _FETCH_MODES:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} fetch_mode must be one of {', '.join(_FETCH_MODES)}"
            )
        if not _WAIT_RE.match(str(wait_after_loaded)):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} wait_after_loaded must look like `0ms` or `3s`"
            )
        metric_list = _split_csv(metrics)
        if not metric_list:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} at least one metric is required")
        for metric in metric_list:
            if self.metric_keywords.get(metric) is None:
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} metric `{metric}` has no keywords; register it first"
                )
        host_list = _split_csv(hosts)
        if not host_list:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} at least one host is required")

        now = _now_ts()
        is_new = self.platforms.get(key) is None
        self.platforms[key] = PlatformSpec(
            key=key,
            label=_clip(label, 120),
            metrics=",".join(metric_list),
            hosts=",".join(host_list),
            fetch_mode=str(fetch_mode),
            wait_after_loaded=str(wait_after_loaded),
            extra_keywords=",".join(_split_csv(extra_keywords)),
            enabled=True,
            registered_at=u256(now),
        )
        if is_new:
            self.platform_keys.append(key)
        PlatformRegistered(
            key,
            metrics=",".join(metric_list),
            hosts=",".join(host_list),
            fetch_mode=str(fetch_mode),
            replaced=not is_new,
            at=now,
        ).emit()

    @gl.public.write
    def set_platform_enabled(self, key: str, enabled: bool) -> None:
        """
        Enable or disable a platform reader.

        Disabling blocks new milestones and new adjudications on that platform.
        Existing escrow stays refundable.
        """
        self._require_owner()
        key = str(key).strip().lower()
        platform = self.platforms.get(key)
        if platform is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown platform `{key}`")
        platform.enabled = bool(enabled)
        ConfigUpdated("platform_enabled", platform=key, enabled=bool(enabled), at=_now_ts()).emit()

    @gl.public.write
    def set_metric_keywords(self, metric: str, keywords: str) -> None:
        """
        Register or retune the words a page may use for a metric.

        These drive the keyword-scoped scan that bounds what the model may
        claim it saw, so widening them makes the scanner more forgiving and
        narrowing them makes it stricter.
        """
        self._require_owner()
        metric = str(metric).strip().lower()
        if not _KEY_RE.match(metric):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} metric must be 2-32 chars of a-z, 0-9 or underscore"
            )
        keyword_list = _split_csv(keywords)
        if not keyword_list:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} at least one keyword is required")
        is_new = self.metric_keywords.get(metric) is None
        self.metric_keywords[metric] = ",".join(keyword_list)
        if is_new:
            self.metric_keys.append(metric)
        MetricKeywordsUpdated(
            metric, keywords=",".join(keyword_list), replaced=not is_new, at=_now_ts()
        ).emit()

    @gl.public.write
    def withdraw_surplus(self, to: Address, amount_atto: int) -> None:
        """
        Sweep balance that is not backing a live milestone.

        Escrow is never withdrawable by the owner: the surplus is strictly the
        contract balance minus every unresolved reward.
        """
        self._require_owner()
        to = _as_address(to, "to")
        if amount_atto <= 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} amount must be positive")
        surplus = int(self.balance) - int(self.total_escrowed_atto)
        if surplus < 0:
            surplus = 0
        if int(amount_atto) > surplus:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} only {surplus} wei is unescrowed surplus"
            )
        _send_value(to, int(amount_atto))
        ConfigUpdated(
            "surplus_withdrawn",
            to=to.as_hex,
            amount_atto=int(amount_atto),
            at=_now_ts(),
        ).emit()

    # -- sponsor lifecycle -------------------------------------------------

    @gl.public.write.payable
    def create_milestone(
        self,
        creator: Address,
        content_url: str,
        platform_key: str,
        metric: str,
        threshold: int,
        deadline_ts: int,
        memo: str = "",
        band_bps: int = 0,
        variance_bps: int = 0,
        variance_floor: int = 0,
    ) -> str:
        """
        Lock a reward against one verifiable milestone.

        The GEN sent with this call is the escrow. It leaves the contract only
        on a validator-agreed pass, on a sponsor concession, or as a refund.

        ``band_bps``, ``variance_bps`` and ``variance_floor`` take the contract
        defaults when left at 0. Pass -1 to request an explicit zero, which
        makes the adjudication exact and therefore much more likely to need
        validator rotation.

        :returns: the milestone id, used by every other method.
        """
        self._require_active()
        now = _now_ts()
        reward = int(gl.message.value)

        if reward <= 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} a reward must be escrowed")
        if reward < int(self.min_reward_atto):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} reward below the minimum of {int(self.min_reward_atto)} wei"
            )
        creator = _as_address(creator, "creator")
        if creator == _zero_address():
            raise gl.vm.UserError(f"{ERROR_EXPECTED} creator must not be the zero address")
        if creator == gl.message.contract_address:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} creator must not be this contract")

        url = str(content_url).strip()
        if not url or len(url) > _MAX_URL_LEN:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} content_url must be 1-{_MAX_URL_LEN} chars"
            )
        host = _host_of(url)
        if not host:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} content_url must be a plain https URL with a hostname"
            )

        key = str(platform_key).strip().lower()
        platform = self.platforms.get(key)
        if platform is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown platform `{key}`")
        if not bool(platform.enabled):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} platform `{key}` is disabled")
        if not _host_allowed(host, _split_csv(str(platform.hosts))):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} host `{host}` is not covered by platform `{key}`"
            )

        metric_key = str(metric).strip().lower()
        if metric_key not in _split_csv(str(platform.metrics)):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} platform `{key}` does not support metric `{metric_key}`"
            )
        if self.metric_keywords.get(metric_key) is None:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} metric `{metric_key}` has no registered keywords"
            )

        if int(threshold) < 1 or int(threshold) > _MAX_METRIC_VALUE:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} threshold must be between 1 and {_MAX_METRIC_VALUE}"
            )
        if int(deadline_ts) <= now:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} deadline must be in the future")
        if int(deadline_ts) > now + int(self.max_deadline_horizon):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} deadline is beyond the {int(self.max_deadline_horizon)}s horizon"
            )
        if len(str(memo)) > _MAX_MEMO_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} memo exceeds {_MAX_MEMO_LEN} chars")

        resolved_band = self._resolve_override(band_bps, int(self.default_band_bps), _MAX_BAND_BPS, "band_bps")
        resolved_variance = self._resolve_override(
            variance_bps, int(self.default_variance_bps), _MAX_VARIANCE_BPS, "variance_bps"
        )
        resolved_floor = self._resolve_override(
            variance_floor, int(self.default_variance_floor), _MAX_METRIC_VALUE, "variance_floor"
        )

        sponsor = gl.message.sender_address
        fingerprint = Keccak256(
            "|".join(
                [
                    sponsor.as_hex,
                    creator.as_hex,
                    url,
                    key,
                    metric_key,
                    str(int(threshold)),
                    str(int(deadline_ts)),
                ]
            ).encode("utf-8")
        ).digest().hex()
        existing = self.dedupe.get(fingerprint)
        if existing is not None:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} identical milestone already exists as `{existing}`"
            )

        sequence = int(self.next_sequence)
        milestone_id = f"m{sequence}"
        self.next_sequence = u256(sequence + 1)

        self.milestones[milestone_id] = Milestone(
            milestone_id=milestone_id,
            sponsor=sponsor,
            creator=creator,
            content_url=url,
            platform_key=key,
            metric=metric_key,
            threshold=u256(int(threshold)),
            deadline_ts=u256(int(deadline_ts)),
            reward_atto=u256(reward),
            band_bps=u256(resolved_band),
            variance_bps=u256(resolved_variance),
            variance_floor=u256(resolved_floor),
            status="",
            attempts=u256(0),
            defer_rounds=u256(0),
            observed_value=u256(0),
            observed_at=u256(0),
            deferred_until=u256(0),
            claimed_at=u256(0),
            created_at=u256(now),
            resolved_at=u256(0),
            payout_atto=u256(0),
            fee_atto=u256(0),
            evidence="",
            memo=_clip(memo, _MAX_MEMO_LEN),
        )
        self._set_status(self.milestones[milestone_id], STATUS_OPEN)

        self.milestone_ids.append(milestone_id)
        self.dedupe[fingerprint] = milestone_id
        self.by_sponsor.get_or_insert_default(sponsor).append(milestone_id)
        self.by_creator.get_or_insert_default(creator).append(milestone_id)

        self.total_escrowed_atto = u256(int(self.total_escrowed_atto) + reward)

        MilestoneCreated(
            milestone_id,
            sponsor,
            creator=creator.as_hex,
            content_url=url,
            platform=key,
            metric=metric_key,
            threshold=int(threshold),
            deadline_ts=int(deadline_ts),
            reward_atto=reward,
            band_bps=resolved_band,
            certain_pass_at=_decision_lines(int(threshold), resolved_band)["certain_pass_at"],
            at=now,
        ).emit()
        return milestone_id

    def _resolve_override(
        self, requested: int, default: int, limit: int, field: str
    ) -> int:
        """0 means take the default, -1 means an explicit zero, else validate."""
        value = int(requested)
        if value == 0:
            return int(default)
        if value == -1:
            return 0
        if value < 0 or value > limit:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} {field} must be -1, 0, or between 1 and {limit}"
            )
        return value

    @gl.public.write.payable
    def top_up(self, milestone_id: str) -> int:
        """
        Add to an unresolved milestone's reward. Sponsor only.

        :returns: the new reward in wei.
        """
        self._require_active()
        milestone = self._get(milestone_id)
        if gl.message.sender_address != milestone.sponsor:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} sponsor only")
        if str(milestone.status) in _TERMINAL_STATUSES or str(milestone.status) == STATUS_EXPIRED:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} milestone `{milestone_id}` is {milestone.status}"
            )
        added = int(gl.message.value)
        if added <= 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} top up must be positive")
        total = int(milestone.reward_atto) + added
        milestone.reward_atto = u256(total)
        self.total_escrowed_atto = u256(int(self.total_escrowed_atto) + added)
        RewardToppedUp(
            str(milestone_id), added_atto=added, reward_atto=total, at=_now_ts()
        ).emit()
        return total

    @gl.public.write
    def cancel_milestone(self, milestone_id: str) -> None:
        """
        Withdraw an unclaimed milestone inside the creation window.

        Deliberately narrow: a sponsor can correct a mistake in the first
        ``cancel_window_seconds``, but cannot pull the reward out from under a
        creator who is working towards it. After the window the only exits are a
        validator-agreed pass or the deadline refund.
        """
        milestone = self._get(milestone_id)
        if gl.message.sender_address != milestone.sponsor:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} sponsor only")
        if str(milestone.status) != STATUS_OPEN:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} only an open milestone can be cancelled, this one is {milestone.status}"
            )
        if int(milestone.attempts) > 0:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} milestone has been adjudicated and can no longer be cancelled"
            )
        now = _now_ts()
        window_ends = int(milestone.created_at) + int(self.cancel_window_seconds)
        if now > window_ends:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} the cancellation window closed at {window_ends}"
            )

        gross = int(milestone.reward_atto)
        milestone.resolved_at = u256(now)
        self._set_status(milestone, STATUS_CANCELLED)
        escrowed = int(self.total_escrowed_atto)
        self.total_escrowed_atto = u256(escrowed - gross if escrowed >= gross else 0)
        self.total_refunded_atto = u256(int(self.total_refunded_atto) + gross)
        sponsor = milestone.sponsor
        _send_value(sponsor, gross)
        MilestoneCancelled(str(milestone_id), amount_atto=gross, at=now).emit()

    @gl.public.write
    def concede(self, milestone_id: str) -> dict:
        """
        Accept a borderline reading and pay the creator. Sponsor only.

        The settlement path for a milestone the oracle cannot call either way:
        the sponsor looks at the recorded reading and chooses to honour it. Only
        available while a claim is deferred or contested, so it can never be
        used to bypass adjudication.
        """
        milestone = self._get(milestone_id)
        if gl.message.sender_address != milestone.sponsor:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} sponsor only")
        if str(milestone.status) not in (STATUS_DEFERRED, STATUS_CONTESTED):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} only a deferred or contested claim can be conceded, this one is {milestone.status}"
            )
        now = _now_ts()
        milestone.deferred_until = u256(0)
        outcome = self._release(milestone, now)
        outcome["milestone_id"] = str(milestone_id)
        outcome["conceded"] = True
        return outcome

    # -- creator lifecycle -------------------------------------------------

    @gl.public.write
    def submit_claim(self, milestone_id: str) -> dict:
        """
        Claim that the milestone has been reached, and adjudicate it now.

        Every validator fetches the content page independently, parses the
        metric, and decides whether it agrees with the leader's zone. A pass
        pays out in this transaction; a borderline reading schedules a re-check;
        a fail leaves the milestone claimable until the deadline.
        """
        self._require_active()
        milestone = self._get(milestone_id)
        if gl.message.sender_address != milestone.creator:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} creator only")
        if str(milestone.status) != STATUS_OPEN:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} milestone `{milestone_id}` is {milestone.status}, not open"
            )
        now = _now_ts()
        if now > int(milestone.deadline_ts):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} the deadline passed at {int(milestone.deadline_ts)}"
            )

        spec = self._spec_for(milestone)
        milestone.claimed_at = u256(now)
        ClaimSubmitted(
            str(milestone_id),
            milestone.creator,
            threshold=int(milestone.threshold),
            at=now,
        ).emit()

        observation = _adjudicate(spec)
        return self._apply_observation(milestone, observation, now)

    @gl.public.write
    def withdraw_claim(self, milestone_id: str) -> None:
        """
        Drop a deferred or contested claim. Creator only.

        Returns the milestone to open if the deadline still stands, so the
        creator can try again once the count has clearly moved.
        """
        milestone = self._get(milestone_id)
        if gl.message.sender_address != milestone.creator:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} creator only")
        if str(milestone.status) not in (STATUS_DEFERRED, STATUS_CONTESTED):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} no pending claim on `{milestone_id}`, it is {milestone.status}"
            )
        now = _now_ts()
        milestone.deferred_until = u256(0)
        milestone.defer_rounds = u256(0)
        milestone.claimed_at = u256(0)
        if now > int(milestone.deadline_ts):
            self._set_status(milestone, STATUS_EXPIRED)
        else:
            self._set_status(milestone, STATUS_OPEN)
        ClaimWithdrawn(str(milestone_id), status=str(milestone.status), at=now).emit()

    # -- permissionless resolution ----------------------------------------

    @gl.public.write
    def resolve_deferred(self, milestone_id: str) -> dict:
        """
        Re-read a deferred or contested milestone. Callable by anyone.

        Engagement metrics only climb, so a reading that was genuinely too close
        to call resolves itself given time. A pass pays the creator; a clear
        fail releases the claim; still borderline schedules the next re-check.

        A re-check is allowed after the deadline because the claim that produced
        the borderline reading was filed before it, and a borderline reading is
        a positive statement that the count was already within tolerance of the
        threshold at claim time. The resolution grace window bounds how long
        that remains true.
        """
        self._require_active()
        milestone = self._get(milestone_id)
        status = str(milestone.status)
        if status not in (STATUS_DEFERRED, STATUS_CONTESTED):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} `{milestone_id}` is {status}, nothing is deferred"
            )
        now = _now_ts()
        if now < int(milestone.deferred_until):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} re-check unlocks at {int(milestone.deferred_until)}"
            )
        limit = self._resolution_deadline(milestone)
        if now > limit:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} the resolution window closed at {limit}; call refund"
            )

        spec = self._spec_for(milestone)
        observation = _adjudicate(spec)

        if status == STATUS_CONTESTED and str(observation.get("zone")) == ZONE_BORDERLINE:
            # A contested milestone keeps re-checking without laddering further.
            # Record the reading, push the next window, leave it contested.
            milestone.attempts = u256(int(milestone.attempts) + 1)
            milestone.observed_value = u256(int(observation.get("value", 0)))
            milestone.observed_at = u256(now)
            milestone.deferred_until = u256(now + int(self.defer_seconds))
            self._record_evidence(milestone, observation, now, int(milestone.attempts))
            ObservationRecorded(
                str(milestone_id),
                ZONE_BORDERLINE,
                value=int(observation.get("value", 0)),
                threshold=int(milestone.threshold),
                attempt=int(milestone.attempts),
                at=now,
            ).emit()
            return {
                "milestone_id": str(milestone_id),
                "status": STATUS_CONTESTED,
                "zone": ZONE_BORDERLINE,
                "observed_value": int(observation.get("value", 0)),
                "threshold": int(milestone.threshold),
                "attempt": int(milestone.attempts),
                "found": bool(observation.get("found", False)),
                "display": _clip(observation.get("display", ""), _EVIDENCE_DISPLAY_LEN),
                "reason": _clip(observation.get("reason", ""), _EVIDENCE_REASON_LEN),
                "recheck_after": int(milestone.deferred_until),
                "resolution_deadline": limit,
                "decision_lines": _decision_lines(
                    int(milestone.threshold), int(milestone.band_bps)
                ),
            }

        return self._apply_observation(milestone, observation, now)

    @gl.public.write
    def refund(self, milestone_id: str) -> dict:
        """
        Return escrow to the sponsor once the milestone can no longer be met.

        Permissionless, because the funds always route to the recorded sponsor.
        Valid when the milestone already expired, when it is open past its
        deadline, or when a deferred or contested claim outlived the resolution
        grace window.
        """
        milestone = self._get(milestone_id)
        status = str(milestone.status)
        now = _now_ts()

        if status == STATUS_EXPIRED:
            reason = "expired"
        elif status == STATUS_OPEN:
            if now <= int(milestone.deadline_ts):
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} the deadline has not passed yet ({int(milestone.deadline_ts)})"
                )
            reason = "deadline passed with no successful claim"
        elif status in (STATUS_DEFERRED, STATUS_CONTESTED):
            limit = self._resolution_deadline(milestone)
            if now <= limit:
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} the claim is still resolvable until {limit}"
                )
            reason = "unresolved borderline claim outlived the resolution window"
        else:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} `{milestone_id}` is {status} and holds no escrow"
            )

        if status != STATUS_EXPIRED:
            MilestoneExpired(
                str(milestone_id), zone="", value=int(milestone.observed_value), at=now
            ).emit()
        outcome = self._refund_to_sponsor(milestone, now, reason)
        outcome["milestone_id"] = str(milestone_id)
        return outcome

    # -- views -------------------------------------------------------------

    @gl.public.view
    def get_owner(self) -> str:
        return self.owner.as_hex

    @gl.public.view
    def is_paused(self) -> bool:
        return bool(self.paused)

    @gl.public.view
    def get_config(self) -> dict:
        """Every tunable that affects adjudication and escrow."""
        return {
            "owner": self.owner.as_hex,
            "paused": bool(self.paused),
            "default_band_bps": int(self.default_band_bps),
            "default_variance_bps": int(self.default_variance_bps),
            "default_variance_floor": int(self.default_variance_floor),
            "defer_seconds": int(self.defer_seconds),
            "max_defer_rounds": int(self.max_defer_rounds),
            "resolution_grace_seconds": int(self.resolution_grace_seconds),
            "cancel_window_seconds": int(self.cancel_window_seconds),
            "min_reward_atto": int(self.min_reward_atto),
            "max_deadline_horizon": int(self.max_deadline_horizon),
            "fee_bps": int(self.fee_bps),
            "fee_recipient": self.fee_recipient.as_hex,
            "bps_denominator": BPS_DENOM,
            "max_fee_bps": _MAX_FEE_BPS,
            "statuses": list(_ALL_STATUSES),
            "zones": [ZONE_FAIL, ZONE_BORDERLINE, ZONE_PASS, ZONE_UNREADABLE],
            "fetch_modes": list(_FETCH_MODES),
        }

    def _view_of(self, milestone: Milestone) -> dict:
        threshold = int(milestone.threshold)
        band_bps = int(milestone.band_bps)
        return {
            "milestone_id": str(milestone.milestone_id),
            "sponsor": milestone.sponsor.as_hex,
            "creator": milestone.creator.as_hex,
            "content_url": str(milestone.content_url),
            "platform": str(milestone.platform_key),
            "metric": str(milestone.metric),
            "threshold": threshold,
            "deadline_ts": int(milestone.deadline_ts),
            "reward_atto": int(milestone.reward_atto),
            "band_bps": band_bps,
            "variance_bps": int(milestone.variance_bps),
            "variance_floor": int(milestone.variance_floor),
            "status": str(milestone.status),
            "attempts": int(milestone.attempts),
            "defer_rounds": int(milestone.defer_rounds),
            "observed_value": int(milestone.observed_value),
            "observed_at": int(milestone.observed_at),
            "deferred_until": int(milestone.deferred_until),
            "claimed_at": int(milestone.claimed_at),
            "created_at": int(milestone.created_at),
            "resolved_at": int(milestone.resolved_at),
            "payout_atto": int(milestone.payout_atto),
            "fee_atto": int(milestone.fee_atto),
            "evidence": str(milestone.evidence),
            "memo": str(milestone.memo),
            "decision_lines": _decision_lines(threshold, band_bps),
            "cancel_window_ends": int(milestone.created_at)
            + int(self.cancel_window_seconds),
            "resolution_deadline": self._resolution_deadline(milestone),
        }

    @gl.public.view
    def get_milestone(self, milestone_id: str) -> dict:
        """Full record for one milestone, including its decision lines."""
        return self._view_of(self._get(milestone_id))

    @gl.public.view
    def get_evidence(self, milestone_id: str) -> str:
        """The latest observation record, as compact JSON."""
        return str(self._get(milestone_id).evidence)

    @gl.public.view
    def milestone_count(self) -> int:
        return len(self.milestone_ids)

    def _page(self, ids: typing.Any, offset: int, limit: int) -> list[dict]:
        total = len(ids)
        start = max(0, int(offset))
        count = int(limit)
        if count <= 0 or count > _MAX_LIST_LIMIT:
            count = _MAX_LIST_LIMIT
        out: list[dict] = []
        index = start
        while index < total and len(out) < count:
            milestone = self.milestones.get(str(ids[index]))
            if milestone is not None:
                out.append(self._view_of(milestone))
            index += 1
        return out

    @gl.public.view
    def list_milestones(self, offset: int, limit: int) -> list[dict]:
        """Newest-last page over every milestone. ``limit`` is capped at 50."""
        return self._page(self.milestone_ids, offset, limit)

    @gl.public.view
    def list_by_sponsor(self, sponsor: Address, offset: int, limit: int) -> list[dict]:
        ids = self.by_sponsor.get(_as_address(sponsor, "sponsor"))
        if ids is None:
            return []
        return self._page(ids, offset, limit)

    @gl.public.view
    def list_by_creator(self, creator: Address, offset: int, limit: int) -> list[dict]:
        ids = self.by_creator.get(_as_address(creator, "creator"))
        if ids is None:
            return []
        return self._page(ids, offset, limit)

    @gl.public.view
    def count_by_sponsor(self, sponsor: Address) -> int:
        ids = self.by_sponsor.get(_as_address(sponsor, "sponsor"))
        return 0 if ids is None else len(ids)

    @gl.public.view
    def count_by_creator(self, creator: Address) -> int:
        ids = self.by_creator.get(_as_address(creator, "creator"))
        return 0 if ids is None else len(ids)

    @gl.public.view
    def get_status_counts(self) -> dict:
        """O(1) status tallies, maintained on every transition."""
        return {status: int(self.status_counts.get(status, u256(0))) for status in _ALL_STATUSES}

    @gl.public.view
    def get_platform(self, key: str) -> dict:
        platform = self.platforms.get(str(key).strip().lower())
        if platform is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown platform `{key}`")
        return {
            "key": str(platform.key),
            "label": str(platform.label),
            "metrics": _split_csv(str(platform.metrics)),
            "hosts": _split_csv(str(platform.hosts)),
            "fetch_mode": str(platform.fetch_mode),
            "wait_after_loaded": str(platform.wait_after_loaded),
            "extra_keywords": _split_csv(str(platform.extra_keywords)),
            "enabled": bool(platform.enabled),
            "registered_at": int(platform.registered_at),
        }

    @gl.public.view
    def list_platforms(self) -> list[dict]:
        out: list[dict] = []
        for index in range(len(self.platform_keys)):
            platform = self.platforms.get(str(self.platform_keys[index]))
            if platform is None:
                continue
            out.append(
                {
                    "key": str(platform.key),
                    "label": str(platform.label),
                    "metrics": _split_csv(str(platform.metrics)),
                    "hosts": _split_csv(str(platform.hosts)),
                    "fetch_mode": str(platform.fetch_mode),
                    "wait_after_loaded": str(platform.wait_after_loaded),
                    "enabled": bool(platform.enabled),
                }
            )
        return out

    @gl.public.view
    def get_metric_keywords(self, metric: str) -> list[str]:
        keywords = self.metric_keywords.get(str(metric).strip().lower())
        if keywords is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown metric `{metric}`")
        return _split_csv(str(keywords))

    @gl.public.view
    def list_metrics(self) -> list[dict]:
        out: list[dict] = []
        for index in range(len(self.metric_keys)):
            metric = str(self.metric_keys[index])
            out.append(
                {"metric": metric, "keywords": _split_csv(str(self.metric_keywords.get(metric, "")))}
            )
        return out

    @gl.public.view
    def resolve_keywords(self, platform_key: str, metric: str) -> list[str]:
        """
        Exactly the keyword list the scanner will use for this pairing.

        Lets an integrator see why a page scan did or did not find the metric
        before any reward is locked.
        """
        platform = self.platforms.get(str(platform_key).strip().lower())
        if platform is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown platform `{platform_key}`")
        return self._keywords_for(platform, str(metric).strip().lower())

    @gl.public.view
    def get_treasury(self) -> dict:
        """Escrow solvency. ``surplus_atto`` is the only owner-withdrawable part."""
        balance = int(self.balance)
        escrowed = int(self.total_escrowed_atto)
        return {
            "balance_atto": balance,
            "escrowed_atto": escrowed,
            "surplus_atto": balance - escrowed if balance > escrowed else 0,
            "solvent": balance >= escrowed,
            "total_paid_atto": int(self.total_paid_atto),
            "total_refunded_atto": int(self.total_refunded_atto),
            "total_fees_atto": int(self.total_fees_atto),
        }

    @gl.public.view
    def check_url(self, platform_key: str, content_url: str) -> dict:
        """Dry-run the URL and host checks that ``create_milestone`` applies."""
        platform = self.platforms.get(str(platform_key).strip().lower())
        host = _host_of(content_url)
        if platform is None:
            return {"ok": False, "host": host, "reason": "unknown platform"}
        allow = _split_csv(str(platform.hosts))
        if not host:
            return {
                "ok": False,
                "host": "",
                "reason": "not a plain https URL with a hostname",
                "allowed_hosts": allow,
            }
        allowed = _host_allowed(host, allow)
        return {
            "ok": bool(allowed and bool(platform.enabled)),
            "host": host,
            "allowed_hosts": allow,
            "host_allowed": allowed,
            "platform_enabled": bool(platform.enabled),
            "fetch_mode": str(platform.fetch_mode),
            "reason": "" if allowed else f"host `{host}` is not covered by this platform",
        }

    @gl.public.view
    def classify_value(self, milestone_id: str, value: int) -> dict:
        """
        Which zone a given reading would land in for this milestone.

        The same function the leader applies on chain, exposed so a creator can
        tell whether a claim is worth filing yet.
        """
        milestone = self._get(milestone_id)
        threshold = int(milestone.threshold)
        band_bps = int(milestone.band_bps)
        return {
            "value": int(value),
            "zone": _classify(int(value), threshold, band_bps),
            "variance_allowance": _variance_allowance(
                int(value), int(milestone.variance_bps), int(milestone.variance_floor)
            ),
            "decision_lines": _decision_lines(threshold, band_bps),
        }

    @gl.public.view
    def preview_agreement(
        self, milestone_id: str, leader_value: int, validator_value: int
    ) -> dict:
        """
        Replay the equivalence principle against two hypothetical readings.

        This is the actual comparator the validator runs, with the same
        milestone parameters, returning its full reasoning. Reviewers can probe
        the consensus rule without spending a consensus round, and integrators
        can calibrate ``band_bps`` and ``variance_bps`` against the spread they
        see in practice on a given platform.
        """
        milestone = self._get(milestone_id)
        threshold = int(milestone.threshold)
        band_bps = int(milestone.band_bps)
        leader = {
            "found": True,
            "value": int(leader_value),
            "zone": _classify(int(leader_value), threshold, band_bps),
        }
        mine = {
            "found": True,
            "value": int(validator_value),
            "zone": _classify(int(validator_value), threshold, band_bps),
        }
        report = _agreement_report(
            leader,
            mine,
            threshold,
            band_bps,
            int(milestone.variance_bps),
            int(milestone.variance_floor),
        )
        report["decision_lines"] = _decision_lines(threshold, band_bps)
        return report

    @gl.public.view
    def is_claimable(self, milestone_id: str) -> dict:
        """Whether ``submit_claim`` would be accepted right now, and why not."""
        milestone = self._get(milestone_id)
        now = _now_ts()
        status = str(milestone.status)
        if self.paused:
            return {"claimable": False, "reason": "contract is paused", "status": status}
        if status != STATUS_OPEN:
            return {
                "claimable": False,
                "reason": f"milestone is {status}",
                "status": status,
            }
        if now > int(milestone.deadline_ts):
            return {
                "claimable": False,
                "reason": "deadline passed",
                "status": status,
                "deadline_ts": int(milestone.deadline_ts),
            }
        return {
            "claimable": True,
            "reason": "",
            "status": status,
            "creator": milestone.creator.as_hex,
            "deadline_ts": int(milestone.deadline_ts),
            "decision_lines": _decision_lines(
                int(milestone.threshold), int(milestone.band_bps)
            ),
        }

    @gl.public.view
    def is_resolvable(self, milestone_id: str) -> dict:
        """Whether ``resolve_deferred`` would be accepted right now, and why not."""
        milestone = self._get(milestone_id)
        now = _now_ts()
        status = str(milestone.status)
        if status not in (STATUS_DEFERRED, STATUS_CONTESTED):
            return {
                "resolvable": False,
                "reason": f"milestone is {status}",
                "status": status,
            }
        unlock = int(milestone.deferred_until)
        limit = self._resolution_deadline(milestone)
        if now < unlock:
            return {
                "resolvable": False,
                "reason": "re-check window has not opened",
                "status": status,
                "recheck_after": unlock,
                "resolution_deadline": limit,
            }
        if now > limit:
            return {
                "resolvable": False,
                "reason": "resolution window closed, call refund",
                "status": status,
                "resolution_deadline": limit,
            }
        return {
            "resolvable": True,
            "reason": "",
            "status": status,
            "recheck_after": unlock,
            "resolution_deadline": limit,
        }

    @gl.public.view
    def is_refundable(self, milestone_id: str) -> dict:
        """Whether ``refund`` would be accepted right now, and why not."""
        milestone = self._get(milestone_id)
        now = _now_ts()
        status = str(milestone.status)
        if status == STATUS_EXPIRED:
            return {"refundable": True, "reason": "expired", "status": status}
        if status == STATUS_OPEN:
            if now > int(milestone.deadline_ts):
                return {
                    "refundable": True,
                    "reason": "deadline passed with no successful claim",
                    "status": status,
                }
            return {
                "refundable": False,
                "reason": "deadline has not passed",
                "status": status,
                "deadline_ts": int(milestone.deadline_ts),
            }
        if status in (STATUS_DEFERRED, STATUS_CONTESTED):
            limit = self._resolution_deadline(milestone)
            if now > limit:
                return {
                    "refundable": True,
                    "reason": "unresolved borderline claim outlived the resolution window",
                    "status": status,
                }
            return {
                "refundable": False,
                "reason": "claim is still resolvable",
                "status": status,
                "resolution_deadline": limit,
            }
        return {"refundable": False, "reason": f"milestone is {status}", "status": status}

    @gl.public.view
    def preview_error_agreement(
        self, leader_message: str, validator_message: str
    ) -> dict:
        """
        Replay the failure comparator against two hypothetical error messages.

        Failures need consensus just as much as successes. This exposes the rule:
        deterministic failures must match verbatim, transient failures only need
        to be transient on both sides, and model failures never agree so that
        consensus rotates onto validators whose model behaved.
        """
        leader = gl.vm.UserError(str(leader_message))
        mine = gl.vm.UserError(str(validator_message))
        agree = _compare_user_errors(leader, mine)
        return {
            "agree": bool(agree),
            "leader_message": str(leader_message),
            "validator_message": str(validator_message),
            "leader_class": _error_class(str(leader_message)),
            "validator_class": _error_class(str(validator_message)),
            "rule": (
                "deterministic failures must match verbatim; transient failures "
                "agree on the prefix alone; model failures never agree"
            ),
        }

    @gl.public.view
    def now_ts(self) -> int:
        """The transaction timestamp this contract sees. Useful for calibration."""
        return _now_ts()
