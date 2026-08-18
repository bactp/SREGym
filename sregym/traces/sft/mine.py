"""Batch-mine oracle-verified trajectories in a ``traces.db`` into ``train.jsonl``.

Usage::

    python -m sregym.traces.sft.mine results/traces.db train.jsonl
    python -m sregym.traces.sft.mine results/traces.db train.jsonl --problem k8s_target_port-misconfig

Only mines problem_ids that have a registered milestone detector
(``sregym/traces/sft/problems/``) and only trajectories where
``mitigation_success`` is oracle-verified True. Everything skipped is logged
by reason, never silently dropped -- see ``--help`` output / stderr for the
per-problem/per-trajectory tally.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from sregym.traces import store
from sregym.traces.sft import cut
from sregym.traces.sft.detectors import has_detector, registered_problem_ids

logger = logging.getLogger(__name__)


def mine(db_path: Path, *, problem_id: str | None = None, category: str = "uncategorized") -> tuple[list[dict], Counter]:
    """Return (records, tally). ``tally`` counts outcomes for reporting, e.g.
    ``{"mined": N, "no_detector": N, "build_error:<problem>": N}``."""
    tally: Counter = Counter()
    records: list[dict] = []

    summaries = store.query(problem_id=problem_id, mitigation_success=True, db_path=db_path)
    for summary in summaries:
        if not has_detector(summary.problem_id or ""):
            tally["no_detector"] += 1
            continue
        traj = store.get(summary.trajectory_id, db_path)
        if traj is None:
            tally["missing_trajectory"] += 1
            continue
        try:
            new_records = cut.build_records(traj, category=category)
        except Exception:
            logger.warning("Failed to cut %s (problem_id=%s)", summary.trajectory_id, summary.problem_id, exc_info=True)
            tally[f"build_error:{summary.problem_id}"] += 1
            continue
        if not new_records:
            tally["zero_examples"] += 1
            continue
        records.extend(new_records)
        tally["mined"] += 1

    return records, tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sregym.traces.sft.mine",
        description="Mine oracle-verified trajectories into an SFT train.jsonl.",
    )
    parser.add_argument("db", type=Path, help="Path to traces.db.")
    parser.add_argument("out", type=Path, help="Output train.jsonl path.")
    parser.add_argument("--problem", help="Restrict to one problem_id (default: every problem with a detector).")
    parser.add_argument("--category", default="uncategorized", help="Dataset category tag written onto each record.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if not args.db.exists():
        parser.error(f"db does not exist: {args.db}")

    logger.info("Detectors registered for: %s", ", ".join(registered_problem_ids()) or "(none)")
    records, tally = mine(args.db, problem_id=args.problem, category=args.category)

    with args.out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_task = Counter(r["task"] for r in records)
    logger.info("Wrote %d example(s) from %d episode(s) -> %s", len(records), tally["mined"], args.out)
    logger.info("By task: %s", dict(by_task))
    logger.info("Skipped: %s", dict(tally))
    return 0


if __name__ == "__main__":
    sys.exit(main())
