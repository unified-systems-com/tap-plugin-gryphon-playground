**The nightly fuzz campaign's ledger, landing by PR.** Every night that runs clean appends one row to `tap_plugin/gryphon_playground/gridkin/fuzz-campaign-log.jsonl` on this branch and comments here with the numbers. Nothing is pushed to `main` by a bot — you merge this when you like, and the branch is recreated from `main` the night after.

## What a row is

One campaign over a **fresh seed band** the executor has never seen (the band is derived from the calendar, so bands never repeat and never collide with the per-commit gate's pinned seed): how many queries ran, how the executor and the model oracle agreed or disagreed, how many **distinct defect fingerprints** appeared, and the oracle-asserted fraction — the denominator that keeps a falling defect rate honest. The trend this exists for is *distinct new defects per 100k queries, declining as the executor hardens*.

## How to review it

- **`distinct_defects` is 0 on every row** — the expected case. Merge whenever; there is nothing to investigate. The commit message of each row carries the run link if you want the log.
- **A row has `distinct_defects` > 0** — that night ALSO filed or updated the issue **"Nightly fuzz campaign found a defect"** with every defect's seed, query and detail, and the steps to reproduce, classify and fix. The ledger row is still true and should still merge: the ledger records what was found, the issue drives the fix. Do not edit rows.
- **`asserted_fraction` drifting down** across rows is the one thing worth a second look even with zero defects: it means the generator is emitting shapes the oracle cannot model, so "no defects" is being measured over a shrinking slice. That is an `OracleUnmodeled` widening, not an executor bug — file it against the oracle.
- **Trend:** `scripts/gryphon-findings` (core) and `fuzz_campaign.trend()` render it; a stack with the plugin installed editable can run `scripts/dc exec -T web scripts/gryphon-fuzz-campaign` to add a local row the same way.

## When to merge

Any time. Timing changes nothing: the band is calendar-derived, so an unmerged night does not cause a repeated band, and a merged one does not skip one. A long-open PR just accumulates rows; merge it before it gets tedious to read.

## When something is wrong with THIS PR

- **No comment for a night** — the campaign did not run or did not finish; the nightly's own owner issue ("Nightly red vs core main", or the campaign issue filed as NOT OBSERVED) says why. This PR does not track that.
- **The same row twice** — the append de-duplicates by exact line; if you see a duplicate, the run's summary changed between two runs on the same day (a re-dispatch). Keep the later one.

_This body is maintained by the nightly workflow (`.github/ledger-pr-body.md`); edit it there._
