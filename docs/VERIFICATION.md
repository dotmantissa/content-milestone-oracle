# Remediation verification

The corrected production-default deployment is
`0xA7D8Ef332de26346ca3f15E21558B500Afe424Be` on hosted StudioNet. Its source
bytes, production defaults, finalized independent reading and native payment
were verified. This is evidence of the disclosed behavior, not a guarantee of
Portal acceptance or compatibility with an unspecified mission.

| Production evidence | Value |
| --- | --- |
| Oracle source SHA-256 | `dda2e0ff490436c33124b868d99714a75975f06a7f9c942f674eae4e1da0e78a` |
| Finalized deployment | `0x6dc7058e354bf286de0d26c8e04148af65866069c2579def73f5a84001853091` |
| Milestone creation | `0x802c3fcef28254ca0ff35831f24c52d006929f44d3838917984f1b192b0cdaeb` |
| Finalized GitHub claim | `0xbca90a75bfd2871cc7cd2d775b432d8bb2c5cd2bd042dfb10330c1e33a393fda` |
| Finalized withdrawal parent | `0x0560d3a44ad8c818cf4d9136d116ebcb3d7128bd725fa70740f1db45c1ae4a88` |
| Finalized native child | `0x68539347e308490b18c03266881397edcf31fcbb466d75472aa73cdbb470dd3e` |
| Independent finalized confirmation | `0x6abcc02aad6f68824c3aca7a978a5bba8f5368fec9b9d8b10eed05f088b68577` |

`gen_getContractCode` decoded bytes equal the local source exactly. CLI reads
also verified the default configuration. The smoke case read a representative
51,400 GitHub stars against threshold 5,000 and allocated/delivered `10**15`
atto GEN. The recipient balance increased by that amount. Afterward, native
balance, escrow, credits, reservations, queued transfers and recovery donations
were all zero; confirmed disbursement was `10**15` and solvency was true.

The hosted RPC briefly returned HTTP 502 after the claim was submitted. The
deployment script resumed the stored claim and withdrawal IDs; it did not
redeploy, escrow again or repeat the claim/payment.

The prior deployment `0x555a04bDB15aa8E9972ad8EbA65236A3e4f7Ef36` is superseded
and does not contain the remediation.

| Audit finding | Change | Evidence |
| --- | --- | --- |
| Different payout outcomes could validate | Independently equal settlement zones, complete typed observation schema and precision bounds | Validator boundary and small-count direct regressions; live comparator rejection |
| Unsupported historical deadline claim | Explicit claim-filing cutoff and disclosed achievement during fixed grace | On-chain terms, late-growth regression and README |
| Failed payable calls could lose depositor ownership | Sender credit on expected rejection; unconditional deposit/credit creation; once-only finalized VM-fault recovery; owner sweep disabled | Rejected-deposit, receipt/replay direct tests and live balance recovery |
| Numeric slack and ambiguous extraction | Exact canonical source membership; metric adjacency; rival-label rejection; conservative abbreviation bounds | Wrong-metric, injection and precision regressions; live sources |
| Fee activation/recipient changes altered funded terms | Effective fee/recipient snapshotted on creation | Existing disabled-fee activation and fixed-fee regressions |
| Owner could change funded reader rules | Reader/keyword snapshot; disabling only affects new funding | Replacement/disable/recheck and evidence-history regression |
| Leader narratives treated as evidence | Five-field consensus observation; deterministic evidence metadata | Strict schema and unbound metadata rejection regressions |
| Achievement confused with completed asynchronous payment | Separate credits/tickets; finalized independent parent/child receipt confirmation; backed failed-payment restoration | Direct proof binding/replay/failure tests; live native/IC lifecycle |
| Poller omitted states and appeal recovery | Durable driver polls stored appeal and transaction IDs, reads finalized ruling, handles terminal/open/deferred/contested states | Offline full appeal/restart tests and live withdrawal driver |
| Recovery could mix failed withdrawal and unrecorded depositor funds | Keep failed ticket reserves protected; source-debited restoration requires a separate explicit donation pool | Adversarial cross-owner recovery regression; protected pool/deposit/withdrawal solvency tests |

Experimental YouTube, X and generic-web presets are explicitly not advertised
as validated live integrations. Receipt recovery relies on a compatible hosted
Studio RPC; cross-network support is not claimed. Native IC value credit and
successful recipient application execution are recorded separately.

Local validation after remediation: 461 direct tests passed, 37 offline lifecycle
and recovery tests passed, oracle lint passed (53 methods) and grant-wrapper lint
passed (9 methods). The focused appeal tests exercise stored-ID polling, missing
receipts, original-ID and separate-ID appeal representations, finalized ruling
refresh and leaving appealed state after successful, failed or cancelled appeal execution. These
are offline lifecycle tests, not a claimed live outcome-changing network appeal.

Additional numeric regression coverage includes consistently ordered
label/count chains, nearby duration counters, ambiguous decimal/grouping
suffixes, malformed abbreviations and fractional count rejection.

Live checks across remediation runs covered GitHub pass/fail/borderline and
contested resolution, actual native payout/refund/cancellation, forks, watchers,
issues, asynchronously loaded contributors, and the official npm downloads
API. Some baseline cases preceded the final parser/accounting revisions; the
entire direct/offline suite ran against final source, and final-source live
checks exercised protected recovery donations, depositor recovery, IC callbacks,
wrapper fund recycling and production-default payout. This is not represented
as one uninterrupted full live run against a single source revision.

The verified nine-method wrapper is
`0xE8B6d5f394b1509f71a95A5903D499CBa2809508`, deployed by
`0xd662345559326e4ee3980c33dd442fd184af20ce0f4e42a65817c6157b89883f`.
Its source bytes match `contracts/grant_programme.py`, SHA-256
`6b89fcd186ab1873070e4e6824c6b1c5fa214d4c9fe16d6793687de3e0864fc6`.
Funding, credit-funded creation, cancellation, an internal refund callback,
redeposit and EOA redemption were executed on chain. The EOA ticket was
confirmed by `0x37ca92ef1baa043025fbf87116b1edb3e4b9478eea74e038f9efe9250caf199f`
after an integration helper initially mishandled the native child's absent
GenVM consensus receipt; the existing transfer was not replayed. The helper now
checks native value credit separately. The uninterrupted corrected full case
passed in 743.50 seconds against final-source oracle
`0x549274EF268B954b4F05172d59269791B3Ea1aAf`, including the exact EOA balance
increase. Its final confirmation was
`0x9cff888d4a738f51b6972569fc8f4097a5f212c69b4bc1ee35e09079b96b8958`.

Across the documented remediation runs, all 33 distinct live integration cases
passed after focused fixes/reruns. Final local and matching code CI checks also
passed; the workflow is [Contract checks](https://github.com/dotmantissa/content-milestone-oracle/actions/workflows/tests.yml).

An isolated deposit-fault fixture also verified the programme's own-sender
recovery forwarding and EOA redemption. A deliberately rejecting IC receiver
proved that native credit can succeed while application execution fails; the
oracle recorded both facts and rejected a duplicate credit restoration.
