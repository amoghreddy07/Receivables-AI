"""Run the deterministic evaluation batch and print the metrics report.

The headline comparison is rules-only vs AI-enabled vs the naive baseline:

    python -m scripts.run_eval [--seed 1] [--compare] [--json]

    --llm-mode rules|auto   run a single mode (real agent + naive baseline)
    --compare (default)     three-way: receivablesai(rules), receivablesai(auto,
                            offline NLU), naive_retry

AI-enabled (auto) uses the deterministic offline NLU diagnoser when no
OPENAI_API_KEY is set — see app/diagnosis/offline.py (honesty contract).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import init_db, make_engine, make_session  # noqa: E402
from app.eval.runner import AGENT_NAIVE, AGENT_REAL, run_evaluation  # noqa: E402


def _print(label: str, m: dict) -> None:
    print(f"\n===== {label} =====")
    print(f"  cases:                {m['total_cases']}  (recoverable: {m['recoverable_cases']})")
    print(f"  recovered cases:      {m['recovered_cases']}")
    print(f"  recovery rate:        {m['recovery_rate']:.1%}")
    print(f"  money recovered:      Rs.{m['money_recovered_minor'] / 100:,.0f}  ({m['pct_of_expected']:.1f}% of expected)")
    print(f"  action precision:     {m['action_precision']:.1%}  ({m['executed_actions']} executed)")
    print(f"  false-action rate:    {m['false_action_rate']:.1%}  ({m['false_action_cases']} cases)")
    print(f"  hard violations:      {m['hard_violations']}  {dict(m['violation_codes'])}")
    print(f"  escalation prec/rec:  {m['escalation_precision']:.1%} / {m['escalation_recall']:.1%}")
    print(f"  avg time to recover:  {m['avg_time_to_recover_hours']}h")
    print(f"  per-playbook:         {m['per_playbook']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ReceivablesAI evaluation batch")
    parser.add_argument("--seed", type=int, default=settings.eval_seed)
    parser.add_argument("--llm-mode", choices=["rules", "auto"], default=None)
    parser.add_argument("--compare", action="store_true", help="run the three-way rules vs AI vs naive comparison")
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    args = parser.parse_args()

    engine = make_engine(settings.eval_db_url)
    init_db(engine)
    db = make_session(engine)

    all_results: dict[str, dict] = {}
    if args.llm_mode:
        results = run_evaluation(db, seed=args.seed, llm_mode=args.llm_mode)
        for k, v in results.items():
            all_results[f"{k}:{args.llm_mode}"] = v
    else:
        rules = run_evaluation(db, seed=args.seed, llm_mode="rules", agents=(AGENT_REAL, AGENT_NAIVE))
        auto = run_evaluation(db, seed=args.seed, llm_mode="auto", agents=(AGENT_REAL,))
        all_results["receivablesai:rules"] = rules[AGENT_REAL]
        all_results["receivablesai:auto"] = auto[AGENT_REAL]
        all_results["naive_retry:rules"] = rules[AGENT_NAIVE]

    if args.json:
        print(json.dumps(all_results, indent=2, default=str))
        return

    for label, m in all_results.items():
        _print(label, m)


if __name__ == "__main__":
    main()
