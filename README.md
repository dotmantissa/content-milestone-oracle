# Content Milestone Oracle

A GenLayer intelligent contract that escrows a reward and adjudicates one
verifiable question:

> Did this publicly accessible content reach this measurable metric threshold by
> this deadline?

A sponsor locks the reward and pins the terms: content URL, platform, metric
(views, stars, likes, forks, subscribers and so on), threshold number, deadline.
When the creator believes the milestone was hit, they submit a claim. Every
validator then independently renders the content page, parses the metric the
page displays, and adjudicates the claim. The reward is released only when
validators agree the threshold was cleared.

For deployment verification and network details, see [Deployment](#deployment).

---

## Contents

- [Why this needs consensus](#why-this-needs-consensus)
- [The hard part: metric variance](#the-hard-part-metric-variance)
- [The equivalence principle](#the-equivalence-principle)
- [Deferral and the six hour re-check](#deferral-and-the-six-hour-re-check)
- [Anti-hallucination: the page scanner](#anti-hallucination-the-page-scanner)
- [Failure classification](#failure-classification)
- [Lifecycle](#lifecycle)
- [Escrow and money](#escrow-and-money)
- [The registries](#the-registries)
- [Integration guide](#integration-guide)
- [API reference](#api-reference)
- [Testing](#testing)
- [Deployment](#deployment)
- [Known limits](#known-limits)

---

## Why this needs consensus

Creator grants, performance-based rewards and milestone-triggered payouts all
share one problem: somebody has to confirm the metric was hit. Today that is
either the sponsor trusting a screenshot, or the sponsor doing their own lookup.
Neither is trustless and neither scales past a handful of grants.

The verification itself is not a deterministic computation. It is a read of live
third-party content, and the answer depends on who looks and when. That is
exactly the class of question GenLayer exists for: the contract needs a fact
that no single party should be trusted to assert, and that multiple independent
validators can each check for themselves.

The boundary this contract draws:

| Layer | Owns |
|---|---|
| Frontend / backend | Browsing, non-authoritative previews, notifications, analytics, indexing |
| **This contract** | Escrow, the terms, the adjudication, the validator comparison rule, settlement, the audit trail |
| Platform pages | The raw displayed metric. Never trusted on one read; every validator re-fetches |

Nothing in the contract depends on a caller's claim about the metric. The number
comes from the page, is bounded by a deterministic scanner, and is agreed
between validators before it can move money.

---

## The hard part: metric variance

Two honest validators reading the same page seconds apart will often see
different numbers:

- CDN edges serve cached copies of different ages.
- Platforms aggregate counts asynchronously; the displayed figure lags reality
  by an unpredictable amount.
- The count is genuinely still moving while the consensus round runs.

So a strict equality equivalence principle deadlocks on ordinary traffic. A
naive tolerance, on the other hand, trades the deadlock for a worse problem: if
validators are allowed to differ on the number, they can also differ on the
*conclusion*, and then whichever validator happened to be leader decides whether
a payout occurs.

This contract separates the two concerns. The number is allowed to vary. The
conclusion is not. And when the number cannot support a conclusion either way,
the contract declines to decide and waits instead.

---

## The equivalence principle

Implemented in `_agreement_report`, which returns its full reasoning rather than
a bare boolean. Three gates, each protecting something different.

### Gate 1: self classification

The leader reports a reading and a zone. The validator recomputes the zone from
the leader's own reading and rejects any mismatch. A leader cannot report one
number and label it as another.

### Gate 2: raw proximity

Each honest reading may sit at most one *variance allowance* from the
unobservable true count:

```
allowance(v) = max(variance_floor, v * variance_bps / 10000)
```

Two honest readings therefore differ by at most two allowances, so the gate is:

```
|leader_value - validator_value| <= allowance(leader) + allowance(validator)
```

The proportional term handles large counts; the floor stops small counts being
held to an impossible standard. This gate protects **the number that gets
recorded on chain**: without it, a leader whose reading lands in the right zone
could still write an inflated figure into the milestone's evidence, which every
downstream consumer reads.

### Gate 3: zone reachability

Each reading is classified against a tolerance band straddling the threshold:

```
band  = threshold * band_bps / 10000

pass        if  value >= threshold + band
fail        if  value <  threshold - band
borderline  otherwise
```

The leader's conclusion must be supportable from the validator's own reading,
widened by **the validator's own allowance** (not the combined one):

```
rank(classify(v - allowance(v))) <= rank(leader_zone) <= rank(classify(v + allowance(v)))
```

where `fail < borderline < pass`. This gate protects **the settlement**.
Agreeing that a threshold was cleared because the leader said so and happened to
be nearby is not verification. Note the single allowance: softness near the
threshold is the tolerance band's job, and the band answers it by deferring
rather than by lowering the bar for agreement.

### Worked examples

Threshold 10,000, `band_bps` 100, `variance_bps` 50, `variance_floor` 5. So the
band is 100 wide either side, a certain pass needs 10,100, a certain fail needs
below 9,900, and a reading near 10,000 carries an allowance of 50.

| Leader | Validator | Gap | Combined | Outcome |
|---|---|---|---|---|
| 12,431 | 12,470 | 39 | 124 | agree, `pass` → payout |
| 8,000 | 8,020 | 20 | 80 | agree, `fail` → still claimable |
| 10,050 | 10,020 | 30 | 100 | agree, `borderline` → deferred |
| 10,010 | 9,990 | 20 | 100 | agree, `borderline` (they straddle the threshold but both sit in the band) |
| 50,000 | 12,000 | 38,000 | 310 | **disagree** on gate 2 |
| 10,130 | 10,040 | 90 | 100 | **disagree** on gate 3: within mutual tolerance, but the validator's own reading only supports `borderline` |
| 8,000 labelled `pass` | 8,020 | — | — | **disagree** on gate 1 |

Both comparators are exposed as views, so you can replay the exact rule the
validators run without spending a consensus round:

```bash
genlayer call <address> preview_agreement --args m1 10130 10040
genlayer call <address> preview_error_agreement --args "[TRANSIENT] a" "[TRANSIENT] b"
```

### Readability

If the metric is not on the page at all, the observation is `unreadable` rather
than zero. Both sides must agree on readability: a leader claiming the metric is
missing while validators can read it is a disagreement, and so is the reverse.

---

## Deferral and the six hour re-check

Settlement reads **the zone, never the raw number**. That is the rule that makes
the tolerance safe. A `borderline` reading is a positive statement that the
contract cannot tell the two outcomes apart, so it neither pays nor fails:

1. The milestone moves to `deferred` and `deferred_until` is set to
   `now + defer_seconds` (six hours by default).
2. Anyone may call `resolve_deferred` once that window opens. It is
   permissionless because the outcome always routes to the recorded parties.
3. A `pass` pays the creator. A `fail` releases the claim and the milestone is
   claimable again. Still `borderline` schedules the next re-check.

Waiting is a fair tie-break because engagement metrics are monotonic in the
long run. A genuine near miss resolves itself upward; a stalled one runs out the
sponsor's deadline and refunds.

### Why a re-check may land after the deadline

A claim must be *filed* before the deadline. Its re-check may complete after it.
This is sound rather than lenient: a `borderline` reading is a positive
assertion that the count was already within tolerance of the threshold at claim
time, which is precisely what the band means. The resolution grace window
(`deadline + resolution_grace_seconds`) bounds how long that remains true.

### Why repeated borderline readings do not auto-settle

After `max_defer_rounds` borderline rounds the milestone becomes `contested`.
This is a soft state, not a dead end: re-checks continue and a `pass` or `fail`
still settles it. What it deliberately does **not** do is pick a winner.

Auto-settling after N rounds would reintroduce exactly the knife edge the band
exists to remove, just moved to a different place. The honest options for a
reading nobody can resolve are: wait for the count to move, have the sponsor
accept it voluntarily (`concede`), have the creator drop it
(`withdraw_claim`), or let the grace window end it and refund. All four exist.

### Why the band straddles the threshold

A sponsor asking for 10,000 with a 1% band gets a certain-pass line at 10,100.
That overshoot is deliberate and documented rather than hidden, because the
alternatives are worse:

- A band only *below* the threshold leaves `value >= threshold` as a hard
  boundary, so a leader reading 10,000 and a validator reading 9,998 disagree.
  The knife edge is back.
- A band only *above* the threshold has the same problem at the band edge.

A sponsor who needs a tight line can set `band_bps` low (down to an exact
comparison with `band_bps = -1`) at the cost of more deferrals and more
validator rotation. `get_milestone` returns `decision_lines` with
`certain_pass_at` and `certain_fail_below` spelled out, so nobody has to infer
this.

---

## Anti-hallucination: the page scanner

Models cannot be trusted to read digits off a page, but they are good at
deciding *which* number on a page is the metric. The contract splits those two
jobs and gives the model only the second one.

Before any model sees the page:

1. **Fetch.** `render_text` (headless browser) by default, so JavaScript-rendered
   counts appear. `render_html` and `http_get` are also available per platform.
2. **Scope.** The page is narrowed to windows around the metric's keywords
   (`stars`, `stargazers`, ...). If no keyword appears, it falls back to the page
   head and records `keyword_scoped: false` in the evidence, so the weaker claim
   is visible rather than silent.
3. **Scan.** Every numeric token in those windows is expanded into every
   plausible integer reading: `12.4k` → 12,400; `12,431` → 12,431 (and 12, 431,
   because `.` and `,` are ambiguous across locales and squashed whitespace can
   join two separate numbers). Over-generating is safe. Every value produced is
   literally printed on the page.
4. **Ask.** The model gets the excerpts, the metric's keywords and the scanned
   number list, and is told to pick which number is the metric.
5. **Gate.** The reported value must match a scanned number within half a
   percent, or the observation fails as `[LLM_ERROR]`. The model can choose
   wrong; it can never invent digits.

Choosing the wrong number on the page is what the validators' independent reads
catch, not the scanner. That division of labour is intentional and tested both
ways.

---

## Failure classification

Failures need consensus just as much as successes. Every error carries a prefix
that tells the comparator how much agreement it demands:

| Prefix | Meaning | Agreement rule |
|---|---|---|
| `[EXPECTED]` | Business logic: wrong caller, bad input, closed window | Messages must match verbatim |
| `[EXTERNAL]` | Upstream rejected the request (HTTP 4xx) | Messages must match verbatim |
| `[TRANSIENT]` | Network failure or HTTP 5xx | Agree if both sides are transient |
| `[LLM_ERROR]` | Model returned something unusable | **Never agree**, forcing rotation |

Model failures never agree on purpose. Agreeing on a broken response would lock
bad state in; disagreeing moves the round onto validators whose model behaved.

A failed round applies no state change, so a claim that hit a transient fetch
failure is simply retryable.

---

## Lifecycle

```
                   create_milestone  (payable, sponsor)
                            |
                            v
    cancel_milestone  <-- [open] <----------------------+
    (sponsor, inside       |   ^                        |
     the creation window)  |   |  fail / unreadable     |
                           |   +------------------------+
                  submit_claim (creator)                |
                           |                            |
          +----------------+----------------+           |
          |                |                |           |
        pass          borderline          fail ---------+
          |                |
          v                v
    [achieved]        [deferred] --- resolve_deferred (anyone,
     terminal,             |          after defer_seconds)
     reward paid           |                 |
                           |        +--------+--------+
                           |        |        |        |
                           |      pass  borderline  fail
                           |        |        |        |
                           |        v        |        +--> [open] or [expired]
                           |   [achieved]    |
                           |                 v
                           |     after max_defer_rounds
                           |                 |
                           |                 v
                           |           [contested] -- re-checks continue
                           |                 |
          concede (sponsor) ----------> [achieved]
          withdraw_claim (creator) ---> [open] or [expired]
          grace window elapses -------> refund --> [refunded]
```

| Status | Meaning |
|---|---|
| `open` | Escrow funded, no claim pending; claimable until the deadline |
| `deferred` | Last reading was borderline; a re-check unlocks after the window |
| `contested` | Repeatedly borderline; still re-checkable, no longer laddering |
| `achieved` | Threshold cleared, reward paid. Terminal |
| `expired` | Deadline passed without a clear pass; escrow refundable |
| `refunded` | Escrow returned to the sponsor. Terminal |
| `cancelled` | Withdrawn inside the creation window before any claim. Terminal |

The borderline ladder belongs to a *claim*, not to the milestone: it resets
whenever a claim ends, so a creator who had one borderline reading on an earlier
attempt starts their next claim with a full set of re-checks.

---

## Escrow and money

- `create_milestone` is payable. The GEN sent with the call **is** the escrow.
- A milestone's escrow settles in three ways: a validator-agreed pass, a
  sponsor concession, or a refund to the recorded sponsor.
- `refund` is permissionless. Anyone may poke a stalled milestone because the
  funds always route to the sponsor on record.
- `total_escrowed_atto` tracks every unresolved reward. The owner's withdrawable
  surplus is strictly `balance - total_escrowed`, so escrow can never be swept.
- `get_treasury()` reports solvency, lifetime paid, lifetime refunded and
  lifetime fees.
- A pause blocks new milestones, new claims and new re-checks. It never blocks
  `refund`, `cancel_milestone` or `withdraw_claim`, so a pause cannot trap
  funds.
- `fee_bps` is a one-way ratchet. It can be lowered but never raised, so an
  integrator can read it once and build against it. Capped at 1,000 bps (10%) at
  deployment. A rate with no recipient address is not charged.

Payouts and refunds are external messages from the intelligent contract to the
chain layer, executed by the contract's ghost contract on finalisation.
Beneficiary addresses must therefore be able to receive a plain value transfer.

On hosted Studio, an incoming value transfer can be credited even when a
payable call rolls back. The live host-rejection test confirmed this: no
milestone was created, but its 0.001 GEN became unallocated surplus. Validate
the URL, platform, creator, threshold and deadline before attaching value.
`check_url` covers the URL and platform checks; it is not a full creation
simulation. A rejected creation has no milestone ID to refund; returning that
surplus requires the owner to call `withdraw_surplus` to the original sender.

### Cancellation is deliberately narrow

`cancel_milestone` requires the milestone to be `open`, to have zero
adjudications, and to be inside `cancel_window_seconds` of creation (one hour by
default). A sponsor can correct a mistake; a sponsor cannot pull the reward out
from under a creator who is working towards it. After that window the only exits
are a validator-agreed pass or the deadline refund.

---

## The registries

Both registries are owner-governed and extensible after deployment, so a new
source can be added without redeploying and without touching any live
milestone's terms.

### Platforms

Bootstrapped at deployment: `github`, `youtube`, `x`, `npm`, `generic_web`.

Each platform records its supported metrics, its allowed hosts, its fetch mode
and how long to let a page settle before reading it (`0ms` for GitHub, `4s` for
YouTube, `5s` for X). Host entries match a host exactly or as a parent domain,
and `*` allows any HTTPS host.

```bash
genlayer write <address> register_platform \
  --args devto "DEV Community post" "views,likes,comments" "dev.to" render_text 2s "reactions"
```

URLs are validated at creation time: HTTPS only, a real hostname, no embedded
credentials, and the host must be covered by the platform. `check_url` dry-runs
exactly those checks before any money is locked.

### Metric keywords

Bootstrapped with twelve metrics (`views`, `stars`, `forks`, `likes`,
`subscribers`, `followers`, `downloads`, `watchers`, `reposts`, `comments`,
`contributors`, `issues`). The keywords drive the scanner's keyword scoping, so
widening them makes the scan more forgiving and narrowing them makes it
stricter. `resolve_keywords(platform, metric)` returns exactly the list the
scanner will use, including the platform's own hints.

Changes to either registry affect future adjudications only. A milestone's
`band_bps`, `variance_bps`, `variance_floor`, `defer_seconds`, `max_defer_rounds`,
`resolution_grace_seconds` and `cancel_window_seconds` are copied onto it at
creation and never change. Owner updates apply to new milestones. Existing
claims keep their original re-check and refund windows, and an expired
cancellation window cannot be reopened by changing the defaults.

---

## Integration guide

### 1. Point the CLI at a network

```bash
npm install -g genlayer
genlayer network set studionet
genlayer account import --name sponsor --private-key 0x... --password "..."
genlayer account use sponsor
```

Studio's wallet transaction layer is gasless. Escrowing a reward still requires
GEN: fund the sponsor using Studio's account faucet before creating a milestone.
Other networks or fee-enabled Studio deployments can also require protocol fees.
See the [GenLayer value transfer guide](https://docs.genlayer.com/developers/intelligent-contracts/features/value-transfers).

### 2. Deploy your own instance

```bash
genlayer deploy --contract contracts/content_milestone_oracle.py \
  --args 0 "" 100 50 5 21600 4 172800 3600 0 31536000
```

Constructor arguments, in order:

| Argument | Default | Meaning |
|---|---|---|
| `fee_bps` | `0` | Protocol fee on successful payouts, max 1000. Lowerable only |
| `fee_recipient` | `""` | Hex address receiving the fee. Empty disables it |
| `default_band_bps` | `100` | Tolerance band half-width, in bps of the threshold |
| `default_variance_bps` | `50` | How far one reading may sit from the truth, in bps |
| `default_variance_floor` | `5` | Absolute floor for that allowance |
| `defer_seconds` | `21600` | Wait between re-checks of a borderline reading |
| `max_defer_rounds` | `4` | Borderline rounds before a milestone is contested |
| `resolution_grace_seconds` | `172800` | How long after the deadline a deferred claim may still resolve |
| `cancel_window_seconds` | `3600` | Window in which a sponsor may cancel an unclaimed milestone |
| `min_reward_atto` | `0` | Smallest acceptable escrow, in wei |
| `max_deadline_horizon` | `31536000` | Furthest into the future a deadline may be set |

All have defaults, so `genlayer deploy --contract ...` with no `--args` works.

For a deployment that records its transaction ID, verifies the deployed source
and defaults, and exercises a real payout, use the included script:

```bash
python3 scripts/deploy_studionet.py --smoke-test
# After interrupted receipt polling, resume the saved IDs:
python3 scripts/deploy_studionet.py --resume --smoke-test
```

The script loads `DEPLOYER_PRIVATE_KEY` from your environment or local `.env`
and writes public recovery metadata to ignored `artifacts/studionet-deployment.json`.
If a submission response is lost before its ID is known, it records that
ambiguity and requires you to identify the original transaction before resuming.

### 3. Create a milestone

```bash
# Check the URL first; costs nothing and locks nothing.
genlayer call <address> check_url --args github "https://github.com/your-org/your-repo"
```

Send the reward using the Python SDK's `value` parameter. The installed CLI's
`write` command does not expose a reward-value option; a call without value
reverts with `a reward must be escrowed`. With `DEPLOYER_PRIVATE_KEY` loaded
from your environment:

```python
import os
import time
from genlayer_py import create_account, create_client
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus
from gltest.assertions import tx_execution_succeeded

client = create_client(
    chain=studionet,
    account=create_account(account_private_key=os.environ["DEPLOYER_PRIVATE_KEY"]),
)
tx_id = client.write_contract(
    address="0xOracleAddress",
    function_name="create_milestone",
    args=["0xCreatorAddress", "https://github.com/your-org/your-repo",
          "github", "stars", 10000, int(time.time()) + 7 * 86400,
          "Q2 developer grant", 0, 0, 0],
    value=10**15,  # 0.001 GEN escrow
)
print(tx_id)  # Save this before waiting; poll this ID if interrupted.
receipt = client.wait_for_transaction_receipt(tx_id, status=TransactionStatus.ACCEPTED)
assert tx_execution_succeeded(receipt), receipt
```

`band_bps`, `variance_bps` and `variance_floor` take the contract defaults when
left at `0`. Pass `-1` to request an explicit zero, which makes adjudication
exact and therefore much more likely to need validator rotation.

The call returns the milestone id (`m1`, `m2`, ...). Read the terms back,
including the lines that actually decide the outcome:

```bash
genlayer call <address> get_milestone --args m1
# decision_lines: {threshold: 10000, band: 100,
#                  certain_pass_at: 10100, certain_fail_below: 9900}
# defer_seconds, max_defer_rounds, resolution_grace_seconds and
# cancel_window_seconds also show the milestone's fixed timing terms.
```

### 4. Claim, as the creator

Check first, then claim:

```bash
genlayer call  <address> is_claimable  --args m1
genlayer call  <address> classify_value --args m1 10050   # -> "borderline"
genlayer write <address> submit_claim  --args m1
```

`submit_claim` runs the consensus round inline. The return value carries the
outcome:

```json
{
  "milestone_id": "m1",
  "zone": "pass",
  "status": "achieved",
  "observed_value": 51400,
  "threshold": 10000,
  "attempt": 1,
  "found": true,
  "display": "51.4k stars",
  "reason": "The page explicitly shows the repository has 51.4k stars.",
  "payout_atto": 5000000000000000000,
  "fee_atto": 0,
  "decision_lines": { "certain_pass_at": 10100, "certain_fail_below": 9900 }
}
```

### 5. Drive re-checks

Borderline claims need a poke once their window opens. The predicate views are
built for exactly this, so a bot never sends a doomed transaction:

```python
status = contract.is_resolvable(args=[milestone_id]).call()
if status["resolvable"]:
    contract.resolve_deferred(args=[milestone_id]).transact()
elif status["reason"] == "resolution window closed, call refund":
    contract.refund(args=[milestone_id]).transact()
else:
    sleep_until(status["recheck_after"])
```

`is_claimable`, `is_resolvable` and `is_refundable` each return a boolean, a
human-readable `reason`, and the relevant timestamps. Claim and re-check
predicates account for pauses and disabled readers. A closed resolution window
directs the bot to refund even when the next re-check has not unlocked. These
views describe state and timing eligibility; caller permissions and external
page/model availability are still checked by the write. Each is checked against
the call it predicts in the test suite.

### 6. Call it from another intelligent contract

A grant programme, DAO payroll or incentive platform can wrap this instance:

```python
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *


@gl.contract_interface
class ContentMilestoneOracle:
    class View:
        def get_milestone(self, milestone_id: str) -> dict: ...
        def is_claimable(self, milestone_id: str) -> dict: ...

    class Write:
        def create_milestone(
            self,
            creator: Address,
            content_url: str,
            platform_key: str,
            metric: str,
            threshold: int,
            deadline_ts: int,
            memo: str,
            band_bps: int,
            variance_bps: int,
            variance_floor: int,
        ) -> None: ...


class GrantProgramme(gl.Contract):
    oracle: Address
    tracked: DynArray[str]

    def __init__(self, oracle: Address):
        self.oracle = oracle

    @gl.public.write.payable
    def fund_milestone(self, creator: Address, url: str, threshold: int, deadline: int) -> None:
        """Forward the escrow straight through to the oracle."""
        ContentMilestoneOracle(self.oracle).emit(
            value=gl.message.value, on="finalized"
        ).create_milestone(
            creator, url, "github", "stars", threshold, deadline, "", 0, 0, 0
        )

    @gl.public.view
    def settled(self, milestone_id: str) -> bool:
        """Read the outcome synchronously; no trust in any caller's claim."""
        record = ContentMilestoneOracle(self.oracle).view().get_milestone(milestone_id)
        return str(record["status"]) == "achieved"
```

Reads are synchronous (`.view()`), writes are asynchronous (`.emit()`). Because
`create_milestone` is payable and returns the id only to its direct caller, a
wrapper that needs the id should either read it back from
`list_by_sponsor(wrapper_address, ...)` after the message lands, or key its own
records on the terms it submitted.

### 7. Index events

Every state transition emits an event: `MilestoneCreated`, `RewardToppedUp`,
`ClaimSubmitted`, `ObservationRecorded`, `MilestoneAchieved`,
`MilestoneDeferred`, `MilestoneContested`, `ClaimWithdrawn`, `MilestoneExpired`,
`RewardRefunded`, `MilestoneCancelled`, `PlatformRegistered`,
`MetricKeywordsUpdated`, `ConfigUpdated`.

`ObservationRecorded` fires on every adjudication with the zone, the reading and
the attempt number, which is enough to reconstruct the full history of a
milestone without replaying transactions.

### Calibrating the tolerance for a platform

`preview_agreement` replays the real comparator, so you can measure the spread a
platform actually shows and set `band_bps` and `variance_bps` from data rather
than guesswork:

```python
spread = measure_platform_spread()           # your own sampling
preview = contract.preview_agreement(args=[mid, base, base + spread]).call()
if not preview["agree"]:
    print(preview["reason"])                 # tells you which gate is too tight
```

---

## API reference

### Sponsor

| Method | Notes |
|---|---|
| `create_milestone(creator, content_url, platform_key, metric, threshold, deadline_ts, memo, band_bps, variance_bps, variance_floor) -> str` | Payable. Returns the milestone id |
| `top_up(milestone_id) -> int` | Payable. Adds to an unresolved reward. Returns the new total |
| `cancel_milestone(milestone_id)` | Open, unadjudicated, inside the creation window |
| `concede(milestone_id) -> dict` | Accept a deferred or contested reading and pay the creator |

### Creator

| Method | Notes |
|---|---|
| `submit_claim(milestone_id) -> dict` | Runs the consensus round and settles |
| `withdraw_claim(milestone_id)` | Drop a deferred or contested claim |

### Permissionless

| Method | Notes |
|---|---|
| `resolve_deferred(milestone_id) -> dict` | Re-read once the window opens |
| `refund(milestone_id) -> dict` | Return escrow to the recorded sponsor |

### Owner

| Method | Notes |
|---|---|
| `transfer_ownership(new_owner)` | |
| `set_paused(flag)` | Blocks creation, claims and re-checks only |
| `set_consensus_params(band_bps, variance_bps, variance_floor, defer_seconds, max_defer_rounds, resolution_grace_seconds)` | Affects new milestones only |
| `set_economics(min_reward_atto, max_deadline_horizon, cancel_window_seconds)` | |
| `set_fee(fee_bps, fee_recipient)` | Rate may only be lowered |
| `register_platform(key, label, metrics, hosts, fetch_mode, wait_after_loaded, extra_keywords)` | Comma-separated lists |
| `set_platform_enabled(key, enabled)` | |
| `set_metric_keywords(metric, keywords)` | |
| `withdraw_surplus(to, amount_atto)` | Capped at `balance - total_escrowed` |

### Views

| Method | Returns |
|---|---|
| `get_milestone(id)` | Full record plus `decision_lines` and timing windows |
| `get_evidence(id)` | Latest observation record, compact JSON |
| `milestone_count()`, `list_milestones(offset, limit)` | Pagination, limit capped at 50 |
| `list_by_sponsor(a, offset, limit)`, `list_by_creator(a, offset, limit)` | Party indexes |
| `count_by_sponsor(a)`, `count_by_creator(a)` | |
| `get_status_counts()` | O(1) tallies, maintained on every transition |
| `is_claimable(id)`, `is_resolvable(id)`, `is_refundable(id)` | Boolean plus `reason` |
| `classify_value(id, value)` | Which zone a reading would land in |
| `preview_agreement(id, leader_value, validator_value)` | Replays the full comparator |
| `preview_error_agreement(leader_msg, validator_msg)` | Replays the failure comparator |
| `check_url(platform_key, url)` | Dry-runs the URL and host checks |
| `resolve_keywords(platform_key, metric)` | The exact keyword list the scanner uses |
| `get_config()`, `get_treasury()`, `get_owner()`, `is_paused()`, `now_ts()` | |
| `get_platform(key)`, `list_platforms()`, `get_metric_keywords(m)`, `list_metrics()` | |

---

## Testing

### Direct mode (fast, no server)

```bash
pip install -r requirements.txt
genvm-lint check contracts/content_milestone_oracle.py
pytest tests/direct -q
```

389 tests in about 17 seconds. Every one of the contract's 43 public methods is
exercised. Coverage is organised as:

| File | Covers |
|---|---|
| `test_scanner.py` | Number interpretation, abbreviation expansion, locale ambiguity, keyword scoping, the candidate gate, URL and host parsing |
| `test_equivalence.py` | Band arithmetic, variance allowances, every branch of the comparator, and the **real validator closure** |
| `test_creation.py` | Every input validation, escrow precondition, host check and duplicate rejection |
| `test_claim_flow.py` | The four claim outcomes, claim preconditions, other platforms and metrics |
| `test_deferral.py` | Deferral, re-check windows, post-deadline resolution, the contested state, concede and withdraw |
| `test_settlement.py` | Payouts, fees, the fee ratchet, cancellation, refunds, top-ups, solvency and surplus |
| `test_registry.py` | Both registries, administration, ownership, configuration bounds |
| `test_views.py` | Indexes, pagination, status tallies, the predicate views |
| `test_resilience.py` | Failure classification, HTTP status mapping, and nine ways a model response can be unusable |
| `test_term_guards.py` | Fixed timing terms after admin updates, predicate/write guard parity, closed resolution windows and URL preflight |

The validator half of consensus is genuinely tested in direct mode, not stubbed.
`gltest` captures the closure the contract hands to `gl.vm.run_nondet`, so a test
can serve one page to the leader, swap the mocks, and run the real validator
function against a different reading:

```python
claim(oracle, creator, mid, page=github_page("12,431"), value=12_431)
direct_vm.clear_mocks()
mock_page(direct_vm, GITHUB_URL, github_page("12,470"))
mock_reader(direct_vm, 12_470)
assert direct_vm.run_validator() is True      # climbing count, agreed
```

That covers climbing counts, an inflated leader, agreed borderline readings,
conclusions the validator cannot support, fabricated leader records, one-sided
readability, and a leader failure against a validator success. Closure
serialisability is asserted too, since GenVM pickles both across the WASM
boundary.

### Integration (real consensus, real pages, real models)

```bash
gltest tests/integration -v -s --network studionet
```

Nothing is mocked: the leader renders a live GitHub page in a browser, a model
reads the star count out of it, and every validator repeats that independently.
Thresholds are derived from what the page actually shows at run time rather than
hardcoded, because live counts move. One adjudication learns the count and the
later tests build on it, which is also the only honest way to exercise the
borderline path against live data: the threshold is set to the count the page
was just showing, which puts any fresh reading inside the band.

The suite asserts validator votes, that the reward genuinely leaves the contract
(waiting for the finalised external message before reading balances back), that
deterministic rejections carry identical messages on every node, and that the
final escrow total equals the number of unsettled milestones times the reward.

Verified on StudioNet on 2026-10-10: **21 tests passed in 17 minutes 14 seconds**,
including a permissionless re-check after owner default changes, creator and
sponsor balance movements, cancellation, refund and final escrow accounting.
The test deployment uses shorter timing defaults; the deployment below uses the
documented six-hour production default.

To run the integration suite from a specific funded sponsor account, set
`DEPLOYER_PRIVATE_KEY`. No key is committed to this repository. Submission is
performed once, and transaction IDs are saved before polling in the ignored
`artifacts/live-run.json`. Retry receipt reads for a saved ID after a network
failure; do not resubmit an ambiguous deployment or write. Every transaction
requires actual validator agreement, and payout checks verify execution before
reading balances.

The harness has additional offline regression checks for lost submission
responses, read retries, missing votes, receipt parsing and failed child transfers:

```bash
pytest -p no:gltest -p no:gltest_direct tests/harness -q
```

All 14 offline checks pass. GitHub Actions runs lint, direct tests and these
recovery checks on every push and pull request; live tests require a funded
network account and run separately.

---

## Deployment

Verified on hosted StudioNet on 2026-10-10 with the constructor defaults:

- **Contract:** `0x555a04bDB15aa8E9972ad8EbA65236A3e4f7Ef36`
- **Deployer / owner:** `0xBC1399c55538eC034d4Da550C03c34Ae0C357f53`
- **Runner:** `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
- **Deployment transaction:** `0xecb84b0428e35e197bc68eada8455d6494c7b999749c17f6dc5d4914fbe9bf24`
- **Source SHA-256:** `576c3a6f5a6ec44192117c6afdc165b45f29f77942f8e4dfe7332e28f3338c5e`

The deployed source matches `contracts/content_milestone_oracle.py` byte for
byte. Configuration reads confirmed the owner, six-hour re-check, four-round
contested limit, two-day resolution grace and one-hour cancellation window.
A real 0.001 GEN escrow on milestone `m1` achieved its 5,000-star threshold with
an observed value of 51,400 and validator agreement. The payout was finalised;
contract balance and outstanding escrow are both zero.

- **Smoke creation:** `0x79b6cb7ee62fc48d13db981195a0dd9cb9c773320046f795dc3305e82fc69cf7`
- **Smoke claim:** `0x2835cf79a7c107948efab7520b2b6f8c0ffd19f9d1733bb7a33b27288f70ebe0`

Read the live deployment with:

```bash
genlayer network set studionet
genlayer call 0x555a04bDB15aa8E9972ad8EbA65236A3e4f7Ef36 get_config
genlayer call 0x555a04bDB15aa8E9972ad8EbA65236A3e4f7Ef36 list_platforms
genlayer call 0x555a04bDB15aa8E9972ad8EbA65236A3e4f7Ef36 get_milestone --args m1
```

The corrected source appends timing fields to milestone storage. Historical
instances do not acquire these fixes. The previously documented address
`0x4CC58F7bFaE130C98c0b5bE1A44E73eA9Ef64f3c` could not be found on the current
Studio endpoint and is superseded by the verified deployment above.

The runner version is pinned deliberately. GenLayer networks reject
`py-genlayer:test`, `py-genlayer:latest` and unversioned aliases, and pinning
keeps the execution environment reproducible across validators.

---

## Known limits

Stated plainly, because a verification primitive that hides its edges is worse
than one that does not.

- **The model can pick the wrong number.** The scanner guarantees the reported
  value is printed on the page; it cannot guarantee the value belongs to the
  requested metric. Keyword scoping and an explicit instruction ("if stars were
  requested, do not report forks") reduce this, and validators reading the page
  independently catch it, but two metrics with coincidentally close values on
  the same page remain the hardest case.
- **Private, paywalled or login-gated content cannot be read.** The fetch is
  unauthenticated by design, since credentials cannot be shared with validators.
- **A sponsor chooses the URL.** The host allowlist is hygiene, not a guarantee
  the page is the content it claims to be. The sponsor locks the funds and owns
  that choice.
- **Platform redesigns can break a reader.** A platform moving its counter out
  of rendered text turns readings into `unreadable`, which is recorded without
  penalty and is why the registry is editable without a redeploy.
- **The certain-pass line sits above the threshold.** `threshold * (1 +
  band_bps/10000)`. This is inherent to a straddling band, is returned in
  `decision_lines`, and is tunable per milestone.
- **A permanently borderline milestone refunds the sponsor.** The burden of
  proof sits with the claim. `concede` exists for a sponsor who disagrees with
  that default.
- **Counts are assumed monotonic in the long run.** The six hour re-check is
  fair because engagement metrics climb. A metric that genuinely oscillates
  around a threshold will exhaust its rounds and go contested.

## License

MIT
