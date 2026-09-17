"""Gridkin scenario execution: seed the fixture, run the query, compare oracles.

For one scenario the runner:

1. Loads the named GRIFT fixture through the standard `grift_import` path.
2. Executes the Gryphon query via `explain_gryphon_raw`, capturing the response
   envelope and the SQL the executor issued.
3. Compares both against committed oracle files — reporting envelope mismatch
   and SQL mismatch as separate failure modes.

Snapshot regeneration (`GRIDKIN_UPDATE_SNAPSHOTS`) overwrites the oracle files
with the current output instead of asserting; it is opt-in and the regenerated
diff must be human-reviewed (req-gridkin-oracle-assertion / -snapshot-discipline).

Per spec-gridkin-v0.md: req-gridkin-runner-contract, req-gridkin-explain-snapshot.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
from typing import TYPE_CHECKING, Any

from tap_plugin.gryphon_playground.gridkin.model_oracle import Graph, OracleUnmodeled, evaluate

from tap_grid.exceptions import SearchExecutionError
from tap_grid.grift import grift_import
from tap_grid.gryphon import explain_gryphon_raw
from tap_grid.gryphon.parser import GryphonParseError, parse_gryphon
from tap_grid.services import delete_node

# Rejection scenarios name the exception the query must raise. Kept to the two
# refusal types Gryphon raises: a grammar refusal (parse) and a semantic/
# unsupported-shape refusal (execution).
_REJECTION_TYPES: dict[str, type[Exception]] = {
    "GryphonParseError": GryphonParseError,
    "SearchExecutionError": SearchExecutionError,
}

if TYPE_CHECKING:
    from pathlib import Path

    from tap_plugin.gryphon_playground.gridkin.loader import Scenario


def normalize_sql(text: str) -> str:
    """Whitespace-normalize SQL for comparison: collapse runs, trim, drop blanks.

    Trivial formatting differences do not churn snapshots; everything else —
    table/column names, JOIN structure, predicates, params, stage labels — is
    compared exactly.
    """
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


# A labelless ``MATCH (n)`` compiles to ``entity_type IN (<every registered node
# type>)`` (executor `_execute_bare_type_scan`). That enumeration is an
# *environment fact* — it grows whenever ANY plugin registers a new node type —
# not a property of the query plan under test. Snapshotting it verbatim makes the
# two labelless-scan oracles churn on every new entity type for no behavioral
# reason, so it is redacted to a stable sentinel before comparison: the SQL
# analogue of the envelope's declared member projection (`_ASSERTED_MEMBER_KEYS`).
# The residual filter (`= %s` / `LIKE %s`), the IN-on-`entity_type` shape itself,
# and every other clause are still asserted exactly — and the envelope assertion
# still verifies the rows the scan actually returns. Only this exact column with an
# `IN` appears in any oracle (the bare-type-scan), so the redaction is surgical.
# See spec-gridkin-v0.md req-gridkin-explain-snapshot.
_REGISTRY_SENTINEL = "<entity-type-registry>"
_ENTITY_TYPE_IN_RE = re.compile(r'"tap_entity"\."entity_type" IN \((%s(?:,\s*%s)*)\)')
_PARAMS_LINE_RE = re.compile(r"-- params: (\[.*\])")


def redact_registry_scan(text: str) -> str:
    """Collapse the labelless bare-type-scan's registered-node-type enumeration.

    Replaces the ``entity_type IN (%s, %s, …)`` placeholder run with a single
    ``<entity-type-registry>`` sentinel and drops the matching leading params
    (the registered node types bind first in the WHERE), substituting the same
    sentinel. A no-op for every other statement. Applied to both sides of the SQL
    comparison and to the written snapshot, so the committed oracle is stable
    across registry growth (req-gridkin-explain-snapshot).
    """
    match = _ENTITY_TYPE_IN_RE.search(text)
    if match is None:
        return text
    registry_param_count = match.group(1).count("%s")
    text = _ENTITY_TYPE_IN_RE.sub(f'"tap_entity"."entity_type" IN ({_REGISTRY_SENTINEL})', text, count=1)

    def _trim_params(params_match: re.Match[str]) -> str:
        params = ast.literal_eval(params_match.group(1))
        residual = params[registry_param_count:]
        return "-- params: " + repr([_REGISTRY_SENTINEL, *residual])

    return _PARAMS_LINE_RE.sub(_trim_params, text, count=1)


# ---------------------------------------------------------------------------
# The entity spine column enumeration: CORE's fact, not this corpus's
# ---------------------------------------------------------------------------
#
# Every statement that reaches the spine expands `tap_entity` to its full concrete
# column list, because the executor selects the model rather than a projection:
#
#   SELECT "tap_entity"."id", "tap_entity"."entity_type", "tap_entity"."name", …
#
# That list is a property of the TABLE. It is not `Entity.SPINE_FIELD_NAMES`, not
# `build_spine_surface`, and not anything a declaration in core can hold back — it
# is Django's default column expansion, so it grows the moment a column is added
# whatever core says about the serialization surface. Snapshotting it verbatim made
# one `Entity` column cost a corpus regeneration, a plugin release and a two-record
# boot-tier PR in `tap` (unified-systems-com/tap#487; observed on tap#480, where the
# `SPINE_EXCLUDED` escape hatch did nothing for the 74 `.sql.txt` files that name the
# columns).
#
# Same class of fact as the registry enumeration above, arriving from a different
# owner: core rather than the type registry. Collapsed the same way, to a sentinel on
# both comparison sides and on regeneration.
#
# The collapse is deliberately EXACT-RUN rather than pattern-based, and that is what
# keeps it from going vacuous:
#
#   - The run is DERIVED from the live model (`Entity._meta.concrete_fields`), never
#     copied. A column added in core changes the derivation and the emitted SQL in the
#     same breath, so the two keep matching and the corpus stays green.
#   - Because only the FULL run collapses, an executor that narrowed the spine select
#     to a subset — `.only(...)`, a deferred field, a hand-built projection — does not
#     collapse, does not match a sentinel-bearing oracle, and REDS. That is a real
#     query-plan change and this corpus is still the thing that catches it.
#   - A lone `"tap_entity"."deleted_at" IS NULL` in a WHERE clause is not part of the
#     run and is untouched: predicates are still asserted byte-for-byte.
#
# `TestEntityColumnCollapse` in tests/test_gridkin_internals.py holds the positive
# control — a derivation that stopped matching what Django emits would make this a
# silent no-op, which is the failure mode a redaction has to be guarded against.
_ENTITY_COLUMNS_SENTINEL = "<entity-spine-columns>"


def entity_column_run() -> str:
    """The exact SELECT-list run Django emits for a full `tap_entity` expansion.

    Derived from the live model on every call rather than pinned, so this function
    cannot disagree with the schema it describes — the one fact, one home rule. The
    order is the model's field-definition order, which is what Django emits and what
    the committed snapshots were written from.
    """
    from tap_grid.models import Entity

    table = Entity._meta.db_table
    return ", ".join(f'"{table}"."{field.column}"' for field in Entity._meta.concrete_fields)


def collapse_entity_columns(text: str) -> str:
    """Replace each full `tap_entity` column enumeration with a stable sentinel.

    Idempotent: an already-collapsed oracle contains no run to find, so re-applying
    is a no-op — the obligation `collapse_type_scan_fanout` records for itself, and
    the reason both sides of the comparison can share one pipeline.
    """
    return text.replace(entity_column_run(), _ENTITY_COLUMNS_SENTINEL)


# ---------------------------------------------------------------------------
# Per-type fan-out: snapshot the TEMPLATE, derive the ROSTER
# ---------------------------------------------------------------------------
#
# A labelless `MATCH (n)` with a DATA-LANE filter does not compile to one query.
# The executor scans each registered node type whose model declares the referenced
# field and unions the ids (`_execute_bare_type_scan`: a type lacking the field
# `continue`s and contributes nothing). So the statement COUNT and the table names
# are an environment fact — the same class `redact_registry_scan` above already
# handles for the spine-only branch's `entity_type IN (...)` enumeration, arriving
# here in a different shape: N statements instead of one variable IN-list.
#
# Snapshotting them verbatim froze a stack into the oracle. The committed fixtures
# named `compliance_core__*` tables, so the corpus only passed on a stack with
# compliance_core installed — a plugin this one does not depend on, has no reason
# to know about, and does not declare. That is a monorepo-era assumption: when
# every plugin lived in one tree, "all installed types" was a constant. It is not
# one now, and a per-plugin CI job that installs this plugin's declared deps
# (grid_fixtures, and nothing else) must be able to run this corpus green.
#
# The fix is NOT to stop asserting. The scenario's whole point is that the scan
# crosses types the corpus does not own (`req-grid-traversal-lang-bare-match-2`),
# and the sibling `field_absent` scenario documents what happens when that
# assertion quietly evaporates: when `lotr` was retired no type declared the
# filtered field, the executor emitted no SQL at all, and the scenario passed
# VACUOUSLY. A collapse alone would reintroduce exactly that failure mode.
#
# So the two halves are asserted separately, each where it is stable:
#
#   TEMPLATE (snapshot, exact) — the per-type statements are folded into ONE
#     statement with the table name replaced by `<node-type-table>`. The fold
#     REQUIRES them to be identical modulo that name, so a type compiling to a
#     different plan still fails the text compare. JOIN shape, the deleted_at
#     predicate, the filter, and the params are all still asserted byte-for-byte.
#
#   ROSTER (derived, exact) — `_check_scan_roster` recomputes, from the LIVE
#     registry, the set of node types that declare the scenario's data-lane
#     field(s), and asserts the scan covered exactly that set. Stack-independent
#     by construction, and a stronger claim than the frozen list it replaces: it
#     fails if the scan skips a type that declares the field OR touches one that
#     does not, on whatever stack it runs.
_TABLE_SENTINEL = "<node-type-table>"
_FANOUT_SENTINEL = "<per-type-fanout>"
_STATEMENT_HEADER_RE = re.compile(r"^-- statements? ([^\s]+) · stage: (.+)$")
_TYPE_SCAN_FROM_RE = re.compile(r'^FROM "([a-z0-9_]+)"$', re.MULTILINE)


def _statement_blocks(text: str) -> list[tuple[str, str, str]]:
    """Split rendered SQL into ``(number, stage, body)`` blocks, in order."""
    blocks: list[tuple[str, str, str]] = []
    number = stage = ""
    body: list[str] = []
    for line in text.splitlines():
        header = _STATEMENT_HEADER_RE.match(line.strip())
        if header:
            if stage:
                blocks.append((number, stage, "\n".join(body).strip()))
            number, stage = header.group(1), header.group(2)
            body = []
            continue
        body.append(line)
    if stage:
        blocks.append((number, stage, "\n".join(body).strip()))
    return blocks


def _as_type_scan_template(body: str) -> tuple[str, str] | None:
    """Return ``(table, templated_body)`` if *body* is a single-table per-type scan.

    The entity bulk-fetch that closes a bare scan reads `tap_entity` and is NOT a
    per-type statement, so it is left alone — matched here by requiring exactly one
    distinct FROM table that is not the spine.
    """
    tables = set(_TYPE_SCAN_FROM_RE.findall(body))
    if len(tables) != 1:
        return None
    table = tables.pop()
    if table == "tap_entity":
        return None
    return table, body.replace(f'"{table}"', f'"{_TABLE_SENTINEL}"')


def collapse_type_scan_fanout(text: str) -> tuple[str, tuple[str, ...]]:
    """Fold a run of identical per-type scans into one templated statement.

    Returns ``(collapsed_text, tables_scanned)``. Statements are renumbered with the
    folded run counting as one, so the tail's numbering does not shift with the
    roster either. A no-op (and an empty roster) for SQL with no per-type scan.
    """
    blocks = _statement_blocks(text)
    if not blocks:
        return text, ()

    tables: list[str] = []
    folded: list[tuple[str, str]] = []  # (stage, body) with the fan-out folded
    pending_template: str | None = None
    for _number, stage, body in blocks:
        templated = _as_type_scan_template(body) if stage == "bare-type-scan" else None
        if templated is None:
            pending_template = None
            folded.append((stage, body))
            continue
        table, template_body = templated
        tables.append(table)
        if pending_template == template_body:
            continue  # same plan, different table — already represented
        pending_template = template_body
        folded.append((stage, template_body))

    if not tables:
        return text, ()

    out: list[str] = []
    for index, (stage, body) in enumerate(folded, start=1):
        is_template = _TABLE_SENTINEL in body
        # The COUNT stays out of the snapshot deliberately: it is the roster's size,
        # which is exactly the environment fact being factored out. `_check_scan_roster`
        # asserts it against the live registry instead.
        label = _FANOUT_SENTINEL if is_template else str(index)
        note = " · one per node type declaring the filtered field; roster asserted from the registry" if is_template else ""
        out.append(f"-- statement {label} · stage: {stage}{note}")
        out.append(body)
    # No blank separators: `normalize_sql` (the first stage of the comparison
    # pipeline) strips blank lines, so emitting them here would make the transform
    # non-idempotent — an already-collapsed oracle would re-read one line shorter
    # than a freshly-collapsed actual, and every fan-out scenario would fail on a
    # cosmetic difference. The `-- statement` headers are the delimiter.
    return "\n".join(out).strip() + "\n", tuple(sorted(tables))


def _data_lane_fields(query: str) -> set[str]:
    """The model field names a query's data-lane paths reference.

    Read off the QUERY TEXT on purpose. This is the oracle side of the assertion, so
    it must not borrow the executor's own path-classification — an oracle that asks
    the implementation what it did cannot catch the implementation being wrong. The
    corpus's queries are hand-authored and this shape (`<var>.data.<field>[...]`) is
    the whole of the data lane's grammar; the first segment after `data` is the model
    field, matching how a nested path is stored.
    """
    return set(re.findall(r"\b\w+\.data\.(\w+)", query))


# A labelless node pattern: `(n)` with no `:label` and no edge hanging off it. Only
# these route to the bare type scan; a LABELLED `MATCH (n:t) WHERE n.data.kind = ...`
# also carries data-lane fields but compiles to one scan of its own table, so the
# roster question does not apply to it.
_LABELLESS_MATCH_RE = re.compile(r"MATCH\s*\(\s*\w*\s*\)(?!\s*[-<])")


def _is_labelless_bare_match(query: str) -> bool:
    """True when the query's pattern is a labelless `MATCH (n)` (the bare-scan route)."""
    return _LABELLESS_MATCH_RE.search(query) is not None


def _expected_scan_tables(query: str) -> set[str]:
    """Tables a labelless data-lane scan MUST cover, derived from the live registry.

    The requirement restated independently (`req-grid-traversal-lang-bare-match-2`):
    every registered node type whose model declares ALL the referenced data-lane
    fields is scanned; every type that does not is silently skipped.
    """
    from tap_grid.registry import get_model_class, list_entity_types

    fields = _data_lane_fields(query)
    if not fields or not _is_labelless_bare_match(query):
        return set()
    tables: set[str] = set()
    for entity_type in list_entity_types():
        model = get_model_class(entity_type)
        if fields <= {f.name for f in model._meta.get_fields()}:
            tables.add(model._meta.db_table)
    return tables


def run_scenario(scenario: Scenario, *, update_snapshots: bool = False) -> list[str]:
    """Run one Gridkin scenario.

    Returns a list of failure messages — empty means the scenario passed.
    Envelope and SQL mismatches are separate entries so the caller can tell
    whether behavior changed or the query plan changed.

    With `update_snapshots`, the oracle files are regenerated and an empty list
    is returned (regeneration never "fails").
    """
    _seed_fixture(scenario)

    # Rejection scenario: the query must be refused. The error spec IS the
    # hand-authored oracle — there is nothing to regenerate, so update_snapshots
    # is a no-op here.
    if scenario.expected_error is not None:
        return _check_rejection(scenario)

    result = explain_gryphon_raw(  # TAP-AUTHZ-COV: pytest-only gridkin harness, not a production path
        scenario.query, scenario.params, db_alias="default", layer=scenario.layer
    )
    actual_envelope: dict[str, Any] = result["envelope"]
    actual_sql: str = result["sql"].render()

    if update_snapshots:
        _write_snapshots(scenario, actual_envelope, actual_sql)
        return []

    failures: list[str] = []
    failures.extend(_check_envelope(scenario, actual_envelope))
    failures.extend(_check_sql(scenario, actual_sql))
    failures.extend(_check_scan_roster(scenario, actual_sql))
    failures.extend(_check_oracle(scenario, actual_envelope))
    return failures


def _check_oracle(scenario: Scenario, actual: dict[str, Any]) -> list[str]:
    """Diff the independent model-based reference oracle against the executor.

    The oracle (`model_oracle.evaluate`) computes the correct result a *second*
    way — interpreting the same parsed AST over plain Python objects loaded from
    the same GRIFT fixture, with zero shared ORM-lowering logic. A disagreement
    is a bug in one of the two engines (usually the executor's AST→SQL
    translation — the surface this whole discipline targets), surfaced
    mechanically and independent of which query shape an author happened to pick.

    - Rejection scenarios have no result to model — skipped.
    - A shape the oracle does not yet model raises `OracleUnmodeled` — skipped
      per-scenario (corpus-level coverage is tracked by the standalone
      validator); an unmodeled shape is never a fake green, it is simply not
      asserted here.
    - A scenario with `oracle_divergence` set declares a KNOWN, tracked
      disagreement (e.g. a semantics decision pending a refactor). The check
      asserts the divergence STILL holds — so the exemption cannot silently rot
      once the underlying behavior is fixed.
    """
    if scenario.expected_error is not None:
        return []
    try:
        graph = Graph.from_grift(_oracle_document(scenario), frozenset(scenario.soft_delete))
        parsed = parse_gryphon(scenario.query)
        oracle = evaluate(parsed, graph, scenario.params)
    except OracleUnmodeled:
        return []

    agrees, detail = _oracle_agrees(oracle, parsed, actual)
    if scenario.oracle_divergence is not None:
        if agrees:
            return [
                "ORACLE DIVERGENCE STALE — this scenario declares oracle_divergence "
                f"({scenario.oracle_divergence!r}) but the oracle now AGREES with the "
                "executor. Remove the oracle_divergence field."
            ]
        return []
    if agrees:
        return []
    return [
        "ORACLE DISAGREEMENT — the independent reference oracle computed a different "
        "result than the executor (a translation-fidelity bug in one of them):\n" + detail
    ]


def _oracle_document(scenario: Scenario) -> dict[str, Any]:
    """Merge the scenario's GRIFT fixture(s) into one document for the oracle."""
    batches: list[Any] = []
    for path in scenario.fixture_paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
        batches.extend(doc.get("batches", []))
    return {"batches": batches}


def _oracle_agrees(oracle: Any, parsed: Any, actual: dict[str, Any]) -> tuple[bool, str]:
    """Compare an OracleResult to the executor's envelope by identity / row value."""
    if oracle.kind == "envelope":
        actual_nodes = frozenset(n["entity_id"] for n in actual.get("nodes", []))
        actual_edges = frozenset(e["entity_id"] for e in actual.get("edges", []))
        if oracle.node_ids == actual_nodes and oracle.edge_ids == actual_edges:
            return True, ""
        detail = (
            f"  nodes only-in-oracle={sorted(oracle.node_ids - actual_nodes)}\n"
            f"  nodes only-in-executor={sorted(actual_nodes - oracle.node_ids)}\n"
            f"  edges only-in-oracle={sorted(oracle.edge_ids - actual_edges)}\n"
            f"  edges only-in-executor={sorted(actual_edges - oracle.edge_ids)}"
        )
        return False, detail
    # rows: order-sensitive when ORDER BY is present, else compared as a multiset.
    actual_rows = actual.get("rows", [])
    oracle_rows = list(oracle.rows)
    if parsed.order_by is not None:
        oracle_norm = [json.dumps(r, sort_keys=True) for r in oracle_rows]
        actual_norm = [json.dumps(r, sort_keys=True) for r in actual_rows]
    else:
        oracle_norm = sorted(json.dumps(r, sort_keys=True) for r in oracle_rows)
        actual_norm = sorted(json.dumps(r, sort_keys=True) for r in actual_rows)
    if oracle_norm == actual_norm:
        return True, ""
    return False, f"  oracle rows={oracle_rows}\n  executor rows={actual_rows}"


def _check_rejection(scenario: Scenario) -> list[str]:
    """Assert the query is refused with the expected error type and message.

    The traversal contract includes which queries are *rejected*, not only which
    return rows. `expected_error.type` names the refusal class and the optional
    `message_contains` pins the diagnostic (case-insensitive substring).
    """
    spec = scenario.expected_error
    assert spec is not None  # caller guards this
    want = _REJECTION_TYPES[spec["type"]]
    needle = spec.get("message_contains", "")
    try:
        explain_gryphon_raw(  # TAP-AUTHZ-COV: pytest-only gridkin harness, not a production path
            scenario.query, scenario.params, db_alias="default", layer=scenario.layer
        )
    except want as exc:
        if needle and needle.lower() not in str(exc).lower():
            return [f"REJECTION MESSAGE MISMATCH — expected {spec['type']} containing " f"{needle!r}, got: {exc}"]
        return []
    except Exception as exc:  # noqa: BLE001 — any other type is a contract failure
        return [f"WRONG REJECTION TYPE — expected {spec['type']}, got " f"{type(exc).__name__}: {exc}"]
    return [f"EXPECTED REJECTION — query should have raised {spec['type']} but it succeeded."]


def _seed_fixture(scenario: Scenario) -> None:
    """Seed the test DB: import each GRIFT fixture in order, then apply
    soft-delete directives.

    Multi-fixture scenarios (req-gridkin-multi-fixture-load) declare an
    array of fixture paths. The runner imports each in order; every fixture
    must import cleanly (`result.success && not result.errors`) or the
    scenario fails. Soft-delete directives run once after the last fixture
    loads.
    """
    for fixture_path in scenario.fixture_paths:
        if not fixture_path.is_file():
            raise AssertionError(f"{scenario.scenario_id}: GRIFT fixture not found: {fixture_path}")
        document = json.loads(fixture_path.read_text(encoding="utf-8"))
        result = grift_import(document, dangling_edge_mode="strict")  # TAP-AUTHZ-COV: pytest-only gridkin fixture seed
        # A non-collector direct grift_import caller owns checking result.errors —
        # result.success alone can miss a partially-rejected import. A bad fixture
        # must fail loud, not fake-green (the GRIFT atomic-batch-rejection rule).
        if not result.success or result.errors:
            messages = [issue.message for issue in result.errors]
            raise AssertionError(
                f"{scenario.scenario_id}: GRIFT fixture {fixture_path.name} "
                f"did not import cleanly (success={result.success}): {messages}"
            )

    # GRIFT import is additive/upsert-only (spec-grift-v0.md) — it never
    # tombstones entities. Soft-deleted graph state is set up here instead:
    # each `soft_delete` entity id is deleted through the service-layer
    # delete_node verb, post-import, before the scenario query runs.
    for entity_id in scenario.soft_delete:
        delete_result = delete_node(entity_id)
        if not delete_result.success:
            raise AssertionError(f"{scenario.scenario_id}: soft-delete of {entity_id} failed: {delete_result.errors}")


# ---------------------------------------------------------------------------
# What a member envelope asserts: a DECLARED projection, not core's surface
# ---------------------------------------------------------------------------
#
# A node/edge member arrives carrying the whole entity spine surface, because that
# is what `build_spine_surface` emits. Comparing all of it made this corpus the
# oracle for `Entity.SPINE_FIELD_NAMES` — so adding one column to core's
# serialization surface reddened every scenario at once, and core could not change
# its own contract without shipping a release of this plugin first
# (unified-systems-com/tap#487).
#
# The keys below are what a GRIDKIN SCENARIO IS FOR: which rows a query returns, in
# what order, under what scoping. They are chosen from that purpose, not copied from
# core's field list — a copy would be the same coupling wearing a shorter name.
#
#   entity_id    identity; the thing a scenario is asserting came back
#   entity_type  which type came back — the whole point of a bare or labelled match
#   name         the readable value scenarios filter and order on
#   dimensions   scoping; several scenarios turn on dimension containment
#   deleted_at   liveness. Always null in a result, and asserting that is how the
#                corpus proves the live-only filter is applied rather than assumed
#   version      write behaviour: the soft-delete scenarios observe it move
#   data         the data lane, when the query projects one — plugin-owned, and the
#                one member key that is not core's
#
# Everything else on the surface is core's fact and is asserted in core, by
# `tap_grid/tests/test_core_serialization_contract.py` (unified-systems-com/tap#490),
# which pins the column lists and the spine surface as literals. The detection moved
# to the side that owns the fact; it did not disappear. That distinction is the whole
# argument — tap#487 explicitly REJECTS "let the oracle ignore unknown spine keys",
# because an oracle that tolerates whatever arrives has stopped asserting. A declared
# projection whose complement is pinned elsewhere is a different thing: the fact keeps
# exactly one home.
#
# This SUPERSEDES the older volatile-field redaction. `created_at`, `updated_at` and
# `originating_grid_id` were rewritten to a `<volatile>` sentinel because they vary
# per run and per environment; dropping them achieves that and more, and leaves one
# mechanism on this path instead of two. Their presence on the surface is pinned in
# core along with the rest of it.
#
# `rows` are deliberately NOT projected: a RETURN projection is the query's own
# output, authored by the scenario, and no spine key reaches it (verified across the
# corpus). It is compared whole, in order.
_ASSERTED_MEMBER_KEYS = (
    "entity_id",
    "entity_type",
    "name",
    "dimensions",
    "deleted_at",
    "version",
    "data",
)


def _project_member(member: dict[str, Any]) -> dict[str, Any]:
    """Copy a node/edge envelope down to the keys this corpus asserts."""
    return {key: member[key] for key in _ASSERTED_MEMBER_KEYS if key in member}


def _canonical_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, comparable form of a response envelope.

    - `nodes` and `edges` are sorted by entity_id: a graph envelope's member
      lists are sets, and the executor emits them in DB-discretion order (the
      hub-and-spoke neighbor fetch, for one, has no ORDER BY).
    - Every member is projected down to the keys this corpus asserts — see
      `_ASSERTED_MEMBER_KEYS`. The rest of the spine surface is core's fact and
      is pinned in core, so a column added there does not red this corpus.
    - `rows` (RETURN projection / aggregation output) is left untouched: its
      order can carry meaning and no spine key reaches it.

    Applied to both sides of every comparison and to the written snapshot, so
    the committed expected file is stable across runs and environments, and is
    exactly what the comparison asserts.
    """
    canonical = dict(envelope)
    for key in ("nodes", "edges"):
        members = canonical.get(key)
        if isinstance(members, list):
            projected = [_project_member(m) for m in members]
            canonical[key] = sorted(projected, key=lambda m: str(m.get("entity_id", "")))
    return canonical


def _write_snapshots(scenario: Scenario, envelope: dict[str, Any], sql_text: str) -> None:
    scenario.expected_envelope_path.parent.mkdir(parents=True, exist_ok=True)
    scenario.expected_envelope_path.write_text(
        json.dumps(_canonical_envelope(envelope), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    scenario.expected_sql_path.parent.mkdir(parents=True, exist_ok=True)
    scenario.expected_sql_path.write_text(_normalize_for_compare(sql_text), encoding="utf-8")


def _check_envelope(scenario: Scenario, actual: dict[str, Any]) -> list[str]:
    path = scenario.expected_envelope_path
    if not path.is_file():
        return [_missing(scenario, "envelope", path)]
    expected = _canonical_envelope(json.loads(path.read_text(encoding="utf-8")))
    actual_canonical = _canonical_envelope(actual)
    if expected == actual_canonical:
        return []
    return ["ENVELOPE MISMATCH — the response envelope changed (behavior).\n" + _json_diff(expected, actual_canonical)]


def _normalize_for_compare(text: str) -> str:
    """The full comparison pipeline: whitespace, registry enumeration, entity spine columns, per-type fan-out.

    Applied to both sides of every SQL comparison and to the written snapshot, so the
    committed oracle is exactly what is asserted — nothing is hidden at compare time
    that the file still appears to claim.
    """
    collapsed, _tables = collapse_type_scan_fanout(collapse_entity_columns(redact_registry_scan(normalize_sql(text))))
    return collapsed if collapsed.endswith("\n") else collapsed + "\n"


def _check_sql(scenario: Scenario, actual_sql: str) -> list[str]:
    path = scenario.expected_sql_path
    if not path.is_file():
        return [_missing(scenario, "SQL", path)]
    expected = _normalize_for_compare(path.read_text(encoding="utf-8"))
    actual = _normalize_for_compare(actual_sql)
    if expected == actual:
        return []
    return ["SQL MISMATCH — the executor's compiled SQL changed (query plan).\n" + _line_diff(expected, actual)]


def _check_scan_roster(scenario: Scenario, actual_sql: str) -> list[str]:
    """Assert a bare data-lane scan covered exactly the types declaring the field.

    The half of the fan-out assertion that the collapsed snapshot deliberately does
    not carry (see the fan-out note above). Silent when the query issued no per-type
    scan, so it costs nothing for every other scenario.
    """
    _collapsed, scanned = collapse_type_scan_fanout(redact_registry_scan(normalize_sql(actual_sql)))
    expected = _expected_scan_tables(scenario.query)
    if not expected and not scanned:
        return []  # not a data-lane bare scan — nothing for this check to say
    if expected and not scanned:
        # THE VACUUM. `bare_match`'s field-absent scenario records this happening for
        # real: when `lotr` was retired, no type declared the filtered field, the
        # executor emitted no SQL, and the scenario passed asserting nothing. Caught
        # here directly rather than relying on a snapshot that a regeneration could
        # quietly bake the emptiness into.
        return [
            "SCAN ROSTER VACUOUS — the labelless scan emitted no per-type statement, but "
            f"{len(expected)} registered node type(s) declare {sorted(_data_lane_fields(scenario.query))}. "
            "The scenario would assert nothing."
        ]
    if set(scanned) == expected:
        return []
    missed = sorted(expected - set(scanned))
    extra = sorted(set(scanned) - expected)
    detail = []
    if missed:
        detail.append(f"  NOT scanned but declares the field: {', '.join(missed)}")
    if extra:
        detail.append(f"  scanned but does NOT declare the field: {', '.join(extra)}")
    return [
        "SCAN ROSTER MISMATCH — the labelless scan did not cover exactly the node types "
        f"declaring {sorted(_data_lane_fields(scenario.query))} "
        f"({len(expected)} registered here).\n" + "\n".join(detail)
    ]


def _missing(scenario: Scenario, kind: str, path: Path) -> str:
    return (
        f"{kind.upper()}: expected file missing: {path}\n"
        f"  Author it by hand per the oracle discipline, or run with "
        f"GRIDKIN_UPDATE_SNAPSHOTS=1 to generate it — then review every line "
        f"of the diff before committing (spec-gridkin-v0.md "
        f"req-gridkin-oracle-assertion)."
    )


def _json_diff(expected: Any, actual: Any) -> str:
    exp = json.dumps(expected, indent=2, ensure_ascii=False, sort_keys=True).splitlines()
    act = json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True).splitlines()
    return _line_diff("\n".join(exp), "\n".join(act))


def _line_diff(expected: str, actual: str) -> str:
    diff = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile="expected (oracle)",
        tofile="actual (executor)",
        lineterm="",
    )
    return "\n".join(diff)
