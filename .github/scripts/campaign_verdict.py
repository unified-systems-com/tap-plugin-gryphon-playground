"""The nightly fuzz campaign's verdict: red the run when the band found a defect.

Reads the campaign summary JSON the `after-tests` artifact carried (written by core's
`scripts/gryphon-fuzz-campaign` via GRYPHON_FUZZ_CAMPAIGN_SUMMARY_COPY), writes the
human-readable result to the job's step summary, and exits:

  0  the band ran and found no defect
  1  >= 1 distinct defect fingerprint (diverge / rejected / crashed) — the run goes RED,
     and every defect is listed with its seed, query and detail plus the steps to
     reproduce one graph, classify (check the oracle first), fix, and lock in
  1  no summary at all — the campaign did not run or did not finish; a nightly that
     produced no verdict is NOT a pass

No issue is filed and nothing is pushed: the red run IS the signal, read on the
double-tap landing page from the `github_actions_run`s github_core collects
(operator ruling 2026-09-14, unified-systems-com/tap#439). Stdlib only; runs on the runner.

usage: python3 campaign_verdict.py <summary.json-or-missing> <main-job-result> <ledger-compare-url-or-empty>
"""

import json
import os
import pathlib
import sys

summary_path, main_result = sys.argv[1], sys.argv[2]
ledger_url = sys.argv[3] if len(sys.argv) > 3 else ""
step_summary = pathlib.Path(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"))


def out(md: str) -> None:
    with step_summary.open("a", encoding="utf-8") as fh:
        fh.write(md.rstrip() + "\n\n")


p = pathlib.Path(summary_path)
if not p.is_file():
    out(
        "### Fuzz campaign — NOT OBSERVED\n\n"
        f"No summary: the `after-tests` step did not run or did not finish (the `vs core main` job's result was "
        f"`{main_result}`; a red suite skips the campaign by design — a campaign over a broken plugin measures "
        "nothing). Open the *vs core main + fuzz campaign* job → *After-tests command*; `after-tests.log` in the "
        "artifact has the tail when the step ran at all."
    )
    print("::error::fuzz campaign produced no summary — not observed, treated as red")
    sys.exit(1)

s = json.loads(p.read_text(encoding="utf-8"))
t = s["totals"]
band = f"seeds {s['base_seed']}–{s['seed_end']} ({s['n_graphs']} graphs × {s['queries_per_graph']} queries = {s['n_queries']})"
head = (
    f"**Band:** {band} · agree {t.get('agree', 0)} · diverge {t.get('diverge', 0)} · rejected {t.get('rejected', 0)} · "
    f"crashed {t.get('crashed', 0)} · unmodeled {t.get('unmodeled', 0)} · oracle-asserted {s['asserted_fraction'] * 100:.1f}%"
)
ledger_line = f"\n**Ledger:** tonight's row is on the ledger branch — open the PR from {ledger_url}" if ledger_url else ""

if s["distinct_defects"] == 0:
    out(f"### Fuzz campaign — clean\n\n{head}{ledger_line}")
    print(f"campaign clean: {s['n_queries']} queries, 0 defects")
    sys.exit(0)

defects = []
for i, d in enumerate(s["defects"], start=1):
    status = d["fingerprint"].split("|", 1)[0]
    defects.append(
        f"#### Defect {i} — `{status}` (graph seed `{d['seed']}`)\n"
        f"```\n{d['query']}\n```\n"
        f"Detail: `{d['detail']}`  \nFingerprint: `{d['fingerprint']}`"
    )
    print(f"::error::fuzz campaign defect {i}: {status} at seed {d['seed']} — {d['query'][:120]}")

out(f"""### Fuzz campaign — {s['distinct_defects']} distinct defect(s), run is RED

{head}{ledger_line}

Each defect is the FIRST instance of a query shape + status; other seeds that tripped the same shape are folded in.

{chr(10).join(defects)}

### Investigate

1. **Reproduce one graph, locally**, on a stack with the plugin installed editable (`scripts/spawn-session.sh <name> cli --dev-plugins gryphon_playground`, or any stack whose profile pins `gryphon_playground`):
   `scripts/dc exec -T -e GRYPHON_FUZZ_CAMPAIGN_BASE=<graph seed> web scripts/gryphon-fuzz-campaign 1 {s['queries_per_graph']}`
   — divergences are deterministic per seed; the run prints every query's classification. It appends a row to the local ledger: discard it.
2. **Classify before touching code.** `diverge`: executor and model oracle disagree — **check the oracle first** (twice it has mirrored an executor bug: tap#433's union scoping, tap#436's predicate drop); the spec is the tie-breaker, and a silent spec is the finding. `rejected`: the executor refused a query the generator considers valid — a grammar/executor gap or a generator over-reach; decide which. `crashed`: an exception escaped — always an executor defect.
3. **Fix, then lock it in.** Every bug earns a gridkin regression scenario under `tap_plugin/gryphon_playground/scenarios/` (oracle-checked, snapshot reviewed) and a row in `gridkin/gryphon-findings.jsonl` naming the subsystem. If the oracle was wrong, fix it in the SAME change or the differential lane reports the corrected executor as a regression.

Bands never repeat, so a real, unfixed defect keeps recurring under new seeds and keeps this nightly red.
""")
sys.exit(1)
