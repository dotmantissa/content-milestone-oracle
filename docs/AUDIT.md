# Audit and remediation

The adversarial audit reviewed the oracle, numeric extraction, consensus,
accounting, owner powers, lifecycle, integration examples, deployment tooling,
tests and CI. The audited baseline was commit
`7992b3779a94ddc4e1dbc5ae506802eb0513732d`, deployed at
`0x555a04bDB15aa8E9972ad8EbA65236A3e4f7Ef36`, with source SHA-256
`576c3a6f5a6ec44192117c6afdc165b45f29f77942f8e4dfe7332e28f3338c5e`.
That instance is superseded. Updated source and verification evidence are
described in [VERIFICATION.md](VERIFICATION.md).

## Findings in the original implementation

| Severity | Finding and demonstrated consequence | Remediation |
| --- | --- | --- |
| Critical | Validator numeric tolerance accepted different settlement zones. With threshold 10,000, 10,100/pass and 10,099/borderline agreed in both directions. Threshold 1 also accepted 1/pass against 0/fail. The resulting fixed payout versus deferral/failure depended on the leader. | Require exactly the independently computed zone. Validate the complete typed observation and precision bounds. Settlement consumes that zone and immutable economic terms. |
| High | “Achieved by deadline” was unsupported: a timely reading of 9,990 followed by 11,000 six hours later paid despite achievement first being observed after the deadline. Transaction time was also mistaken for browser observation time. | Explicit sponsor-approved claim-filing cutoff and later-achievement grace; disclose that execution reads current content and cannot prove historical achievement. |
| High | Studio credited native value for a reverted payable call without recording depositor ownership. Owner surplus withdrawal could consume those funds. | Non-reverting sender credit for expected admission rejection; unconditional deposits and credit-funded creation; once-only independent finalized receipt recovery for actual VM faults. Owner sweep disabled. |
| High | A 0.5% source-membership tolerance accepted unprinted counts: 10,050 on the page could become 10,100 and cross the payout boundary. | Exact canonical metric candidates; conservative abbreviation intervals; reject ambiguous locale formats, wrong metric associations, partial counters and malformed expansions. |
| Medium | A configured fee with an empty recipient could activate after funding, reducing a 5 GEN reward to 4.5 GEN. | Snapshot effective fee and recipient when funding, including an effective zero fee when disabled. |
| Medium | Replacing reader keywords/fetch mode or disabling the platform affected already funded milestones. | Snapshot reader, keywords and timing. Administrative changes govern future funding only. |
| Medium | Validators accepted altered leader evidence narratives and invalid unreadable values. Stored evidence could misrepresent what was validated or fail subsequent conversion. | Exact five-field schema; canonical unreadable zeros; deterministic stored display, explanation, reader provenance and observation history. |
| Medium | Queued asynchronous transfers were treated as completed payments; IC callback failure and recovery lacked live proof. | Separate credit allocation, withdrawal ticket, finalized native delivery and recipient application result. Validate parent/child amount, recipient, owner and native credit independently. Restore a failed transfer only once and with backing. |
| Low | The documented poller accessed a missing `recheck_after` for ordinary open/terminal states. Appeal restart and ruling refresh were incomplete. | Durable journal, conditional state branches, stored appeal/original IDs, finalized chain refresh, cancelled/failed execution handling and safe retry of known failed read/exit actions. |

The captured validator closure, rather than only a comparator surrogate, was
used to reproduce the consensus and evidence failures. Existing happy-path
tests passing did not invalidate those counterexamples.

Live remediation checks also found that GitHub loads contributor totals
asynchronously: a zero-second render omitted the count, while a four-second
render exposed it. The corrected default waits four seconds. The displayed
“+ remaining contributors” subtotal is excluded from total-count candidates.
Previously funded reader snapshots retain their original terms.

Wrapper live deployment uncovered missing explicit conversion of SDK string
calldata to stored `Address`; this is corrected and tested. Native callbacks
also need a usable spending path: `redeposit_budget` lets the programme owner
recycle received refunds to oracle credit and redeem them through the verified
withdrawal lifecycle.

A further adversarial recovery test found that “available balance” could
contain another sender's failed deposit. Restoring a failed withdrawal from
that balance would consume the other sender's funds. The final implementation
keeps an uncredited ticket's held reserve protected and uses a separate,
explicit recovery-donation pool for any source-debited shortfall. Failed
deposit recovery and other withdrawals cannot consume that pool. Raw balance
growth is never sufficient proof that a particular withdrawal was refunded.

## Reviewer-style note covering the discovered shortcomings

> The contract cannot be accepted because validator agreement does not bind a
> single settlement outcome: pass, borderline and even small-count failure can
> validate while producing different payments. The advertised historical
> deadline is unsupported, and the scanner permits unprinted payout-changing
> counts. Reverted payable calls can leave credited deposits under owner surplus
> authority, while fee activation and reader changes can alter funded terms.
> Stored evidence and unreadable results are insufficiently validated. Queued
> payments are presented as completed without full IC failure/recovery evidence,
> and the polling example fails on ordinary states. Recovery must also preserve
> the ownership of unrelated failed deposits; unallocated balance cannot prove
> a particular withdrawal was refunded. Reader timing must expose asynchronous
> counts, and received programme funds need a usable spending path. Please bind the outcome,
> preserve depositor ownership and immutable terms, document truthful timing,
> complete lifecycle verification, and submit matching source and deployment.

This note summarizes issues found before remediation was complete, not a
rejection of the corrected implementation. No mission brief was supplied, so verification establishes the
repository's disclosed behavior and does not guarantee Portal acceptance.

## Verification limits

YouTube, X and generic-web presets remain explicitly experimental. Public
sources may change, remove counts or block renderers; unreadability never
authorizes a payout. Historical evidence and binding precedent are not features
of this primitive. There is no hosted frontend/backend in this repository.

Receipt verification uses the deployment's immutable compatible hosted Studio
RPC, independently queried by validators. It is not a cryptographic light
client or a guarantee of compatibility with every GenLayer network. Finalized
native value credit and successful IC application execution are separate facts.
Appeal recovery tests exercise actual client polling and refreshed state with
deterministic receipts; they do not claim a live outcome-changing appeal.

Protocol references: [finality](https://docs.genlayer.com/understand-genlayer-protocol/core-concepts/optimistic-democracy/finality),
[value transfers](https://docs.genlayer.com/developers/intelligent-contracts/features/value-transfers)
and [message scheduling](https://docs.genlayer.com/developers/intelligent-contracts/features/messages).
