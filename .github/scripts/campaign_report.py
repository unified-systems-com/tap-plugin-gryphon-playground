"""Turn a nightly fuzz-campaign summary into the owner-issue reporter's inputs.

Reads the campaign summary JSON the `after-tests` artifact carried (written by
core's `scripts/gryphon-fuzz-campaign` via GRYPHON_FUZZ_CAMPAIGN_SUMMARY_COPY) and
emits two GITHUB_OUTPUT values for `nightly-owner-issue.yml`:

  result  success | failure | cancelled
          success   — the band ran and found no defect
          failure   — ≥1 distinct defect fingerprint (diverge / rejected / crashed)
          cancelled — no summary at all: the campaign did not run, which the reporter
                      renders as NOT OBSERVED, never as green
  body    what a cold reader does next: every defect with its seed, query and detail;
          how to reproduce ONE graph; how to classify; how it closes

Stdlib only; no Django, no network. Runs on the runner, not in the container.

usage: python3 campaign_report.py <summary.json-or-missing> <main-job-result> <ledger-pr-url-or-empty>
"""

import json
import os
import pathlib
import sys

summary_path, main_result, ledger_pr = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
out = pathlib.Path(os.environ["GITHUB_OUTPUT"])


def emit(result: str, body: str) -> None:
    with out.open("a", encoding="utf-8") as fh:
        fh.write(f"result={result}\n")
        fh.write("body<<__CAMPAIGN_BODY__\n")
        fh.write(body.rstrip() + "\n")
        fh.write("__CAMPAIGN_BODY__\n")
    print(f"campaign report: result={result}")


p = pathlib.Path(summary_path)
if not p.is_file():
    emit(
        "cancelled",
        "The nightly fuzz campaign produced **no summary** — the `after-tests` step did not run or did not finish "
        f"(the `vs core main` job's own result was `{main_result}`: a red plugin suite skips the campaign by design, "
        "since a campaign over a broken plugin measures nothing).\n\n"
        "**Investigate:** open the run above → job `vs core main` → step *After-tests command*; the `after-tests` "
        "artifact carries `after-tests.log` when the step ran at all. A missing artifact means the tests failed first — "
        "fix that, and the campaign resumes on the next nightly.",
    )
    sys.exit(0)

s = json.loads(p.read_text(encoding="utf-8"))
t = s["totals"]
band = f"seeds {s['base_seed']}–{s['seed_end']} ({s['n_graphs']} graphs × {s['queries_per_graph']} queries = {s['n_queries']})"
head = (
    f"**Band:** {band} · agree {t.get('agree', 0)} · diverge {t.get('diverge', 0)} · rejected {t.get('rejected', 0)} · "
    f"crashed {t.get('crashed', 0)} · unmodeled {t.get('unmodeled', 0)} · oracle-asserted {s['asserted_fraction'] * 100:.1f}%"
)
ledger_line = f"\n**Ledger:** this night's row is on the open ledger PR — {ledger_pr}\n" if ledger_pr else ""

if s["distinct_defects"] == 0:
    emit("success", head + ledger_line)
    sys.exit(0)

defects = []
for i, d in enumerate(s["defects"], start=1):
    status = d["fingerprint"].split("|", 1)[0]
    defects.append(
        f"### Defect {i} — `{status}` (graph seed `{d['seed']}`)\n"
        f"```\n{d['query']}\n```\n"
        f"Detail: `{d['detail']}`\n"
        f"Fingerprint: `{d['fingerprint']}`\n"
    )

body = f"""{head}

**{s['distinct_defects']} distinct defect fingerprint(s)** — each is the FIRST instance of a query shape + status; other seeds that tripped the same shape are folded in.
{ledger_line}
{chr(10).join(defects)}
## Investigate

1. **Reproduce one graph, locally**, on a stack with the plugin installed editable (`scripts/spawn-session.sh <name> cli --dev-plugins gryphon_playground`, or any stack whose profile pins `gryphon_playground`):
   ```
   scripts/dc exec -T -e GRYPHON_FUZZ_CAMPAIGN_BASE=<graph seed> web scripts/gryphon-fuzz-campaign 1 {s['queries_per_graph']}
   ```
   That re-runs exactly the graph above (divergences are deterministic per seed) and prints every query's classification. Note the run appends a row to the local ledger — discard it, do not commit it.
2. **Classify** before touching code:
   - `diverge` — the executor and the model oracle disagree on the answer. **Check the oracle first.** Twice now the oracle has mirrored an executor bug and been "right" by agreement (tap#433's union scoping, tap#436's predicate drop); the spec is the tie-breaker, and if the spec is silent, that is the finding.
   - `rejected` — the executor refused a query the generator considers valid: a grammar/executor gap, or the generator emitting a shape the language does not promise. Decide which; either is a fix.
   - `crashed` — an exception escaped: always an executor defect.
3. **Fix, then lock it in.** Every bug earns a gridkin regression scenario in `tap_plugin/gryphon_playground/scenarios/` (oracle-checked, snapshot reviewed) and a row in `gridkin/gryphon-findings.jsonl` naming the subsystem — that ledger is what turns bug locality into refactor targets. If the oracle was wrong, fix the oracle in the SAME change or the differential lane reports the corrected executor as a regression.

## Close

This issue closes itself on the next nightly whose fresh band finds no defect. Bands never repeat, so a real, unfixed defect keeps recurring under new seeds and keeps this issue red; a one-off that never recurs was still a real query with a wrong answer — do not close it by hand without a scenario that pins the shape.
"""
emit("failure", body)
