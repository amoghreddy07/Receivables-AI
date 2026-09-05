"""Metrics — computed ONLY from actual harvested run rows, never hardcoded.

Reported for each agent:
  cases, actionable pool (expected recovery > 0)
  recovery rate            = recovered cases / actionable pool
  money recovered          = sum of recovered minor units (and % of expected)
  precision@action         = executed actions matching ground-truth playbook
                             / executed actions (only where GT != NO_ACTION)
  false-action rate        = NO_ACTION cases with >=1 executed action / NO_ACTION cases
  hard violations          = sum of policy-rule audit codes on executed actions
  escalation accuracy      = correct escalations / escalations performed (precision)
                             and correct escalations / should-escalate cases (recall)
  avg time to recover      = mean simulated hours across recovered cases
"""
from __future__ import annotations

from collections import Counter

EXECUTED = ("SUCCEEDED", "FAILED", "EXECUTING")


def compute_metrics(rows: list[dict]) -> dict:
    total = len(rows)
    no_action = [r for r in rows if r["ground_truth"]["playbook"] == "NO_ACTION"]
    recoverable = [r for r in rows if r["expected_recovery_minor"] > 0]
    recovered = [r for r in rows if r["recovered_amount_minor"] > 0]
    expected_sum = sum(r["expected_recovery_minor"] for r in rows)

    executed_actions = 0
    correct_actions = 0
    for r in rows:
        gt_pb = r["ground_truth"]["playbook"]
        for a in r["actions_taken"]:
            if a["status"] not in EXECUTED:
                continue
            if gt_pb == "NO_ACTION":
                continue  # actions on negatives are false actions, not precision hits
            executed_actions += 1
            if a["playbook"] == gt_pb:
                correct_actions += 1

    escalated_rows = [r for r in rows if r["escalated"]]
    should_rows = [r for r in rows if r["should_escalate"]]
    correct_esc = sum(1 for r in escalated_rows if r["should_escalate"])

    violations: Counter[str] = Counter()
    for r in rows:
        for code in r["violations"]:
            violations[code] += 1

    ttr = [r["time_to_recover_hours"] for r in recovered if r["time_to_recover_hours"] is not None]

    by_playbook: dict[str, dict] = {}
    for r in recoverable:
        pb = r["ground_truth"]["playbook"]
        slot = by_playbook.setdefault(pb, {"cases": 0, "recovered": 0, "money": 0})
        slot["cases"] += 1
        slot["money"] += r["recovered_amount_minor"]
        if r["recovered_amount_minor"] > 0:
            slot["recovered"] += 1

    return {
        "total_cases": total,
        "no_action_cases": len(no_action),
        "expected_minor": expected_sum,
        "recovered_cases": len(recovered),
        "recovery_rate": round(len(recovered) / len(recoverable), 4) if recoverable else 0.0,
        "recoverable_cases": len(recoverable),
        "money_recovered_minor": sum(r["recovered_amount_minor"] for r in rows),
        "pct_of_expected": round(100 * sum(r["recovered_amount_minor"] for r in rows) / expected_sum, 1) if expected_sum else 0.0,
        "action_precision": round(correct_actions / executed_actions, 4) if executed_actions else 0.0,
        "executed_actions": executed_actions,
        "false_action_rate": round(sum(1 for r in no_action if r["false_action"]) / len(no_action), 4) if no_action else 0.0,
        "false_action_cases": sum(1 for r in no_action if r["false_action"]),
        "hard_violations": sum(violations.values()),
        "violation_codes": dict(violations),
        "escalation_precision": round(correct_esc / len(escalated_rows), 4) if escalated_rows else 0.0,
        "escalation_recall": round(correct_esc / len(should_rows), 4) if should_rows else 0.0,
        "escalated_cases": len(escalated_rows),
        "should_escalate_cases": len(should_rows),
        "avg_time_to_recover_hours": round(sum(ttr) / len(ttr), 1) if ttr else None,
        "per_playbook": {pb: v for pb, v in sorted(by_playbook.items())},
    }
