"""Gridkin requirement-traceability coverage matrix.

Maps each spec RID cited in a scenario's `covers` field to the scenarios that
cover it, and flags Gryphon-spec RIDs that no scenario covers — the derived
traceability matrix the Gryphon validation audit identified as missing.

Per spec-gridkin-v0.md: req-gridkin-req-traceability.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tap_plugin.gryphon_playground.gridkin.loader import Scenario

# The Gryphon-language specs live in CORE (tap_grid), not in this plugin. Locate them
# through the installed `tap_grid` package rather than by walking up from this file:
# the old `PLUGIN_ROOT.parent.parent` hop assumed the monorepo layout
# (plugins/<slug>/ two levels under the repo root) and silently resolved to nothing
# once this plugin moved into its own repository. `tap_grid` is always importable
# wherever this corpus can run — the plugin only executes inside a TAP harness.
_SPEC_NAMES = (
    "spec-grid-traversal-language.md",
    "spec-grid-traversal-execution.md",
    "spec-grid-gryphon-multihop-aggregation.md",
)


def _spec_dir() -> Path | None:
    """Directory holding the core Gryphon specs, or None if core ships without them."""
    try:
        import tap_grid
    except ImportError:  # pragma: no cover - core is always present in a TAP harness
        return None
    spec_dir = Path(tap_grid.__file__).resolve().parent / "specs"
    return spec_dir if spec_dir.is_dir() else None


def gryphon_spec_paths() -> tuple[Path, ...]:
    """The spec files the matrix reports against; empty when core specs are unavailable.

    Empty is a REAL state (a core distribution that ships no specs), and callers must
    treat it as "cannot report", not as "nothing is uncovered" — an empty id set would
    otherwise make every `covers` entry look unvalidated and every requirement look
    covered. `tests/` skips loudly on empty rather than asserting a vacuous truth.
    """
    spec_dir = _spec_dir()
    if spec_dir is None:
        return ()
    return tuple(spec_dir / name for name in _SPEC_NAMES)


_RID_RE = re.compile(r"\breq-grid-[a-z0-9.]+(?:-[a-z0-9.]+)*\b")
# An ACID is a RID with a trailing -<number>; the gap report tracks
# requirement-level RIDs, not individual acceptance criteria.
_ACID_TAIL_RE = re.compile(r"-\d+$")


def build_matrix(scenarios: Iterable[Scenario]) -> dict[str, list[str]]:
    """RID -> sorted list of scenario_ids that cover it."""
    matrix: dict[str, list[str]] = {}
    for scenario in scenarios:
        for rid in scenario.covers:
            matrix.setdefault(rid, []).append(scenario.scenario_id)
    return {rid: sorted(ids) for rid, ids in sorted(matrix.items())}


def gryphon_spec_ids() -> set[str]:
    """Every `req-grid-*` id — requirement RIDs and ACIDs — in the Gryphon specs.

    Used to flag `covers` entries that match no real spec id (a typo or a
    stale id) — an unvalidated `covers` would otherwise count as false coverage.
    """
    ids: set[str] = set()
    for spec in gryphon_spec_paths():
        if spec.is_file():
            ids.update(_RID_RE.findall(spec.read_text(encoding="utf-8")))
    return ids


def gryphon_spec_rids() -> set[str]:
    """Requirement-level RIDs only (ACIDs filtered out) — for the gap list."""
    return {sid for sid in gryphon_spec_ids() if not _ACID_TAIL_RE.search(sid)}


def render(scenarios: Iterable[Scenario]) -> str:
    """Render the coverage matrix and the uncovered-RID gap list as text."""
    scenarios = list(scenarios)
    matrix = build_matrix(scenarios)
    covered = set(matrix)
    uncovered = sorted(gryphon_spec_rids() - covered)

    lines: list[str] = [
        f"{len(scenarios)} scenario(s) cover {len(covered)} requirement / ACID id(s).",
        "",
    ]
    if matrix:
        lines.append("Covered:")
        for rid, ids in matrix.items():
            lines.append(f"  {rid}")
            for scenario_id in ids:
                lines.append(f"      {scenario_id}")
    else:
        lines.append("Covered: (none — no scenarios discovered)")
    lines.append("")
    if uncovered:
        lines.append(f"Uncovered Gryphon-spec requirements ({len(uncovered)}):")
        lines.extend(f"  {rid}" for rid in uncovered)
    else:
        lines.append("Uncovered Gryphon-spec requirements: (none)")

    # Flag covers entries that match no real spec id — a typo or stale id would
    # otherwise sit in the matrix above as false coverage.
    cited = {rid for scenario in scenarios for rid in scenario.covers}
    unrecognized = sorted(cited - gryphon_spec_ids())
    lines.append("")
    if unrecognized:
        lines.append(f"Unrecognized `covers` ids — typo or stale ({len(unrecognized)}):")
        lines.extend(f"  {rid}" for rid in unrecognized)
    else:
        lines.append("Unrecognized `covers` ids: (none)")
    return "\n".join(lines)
