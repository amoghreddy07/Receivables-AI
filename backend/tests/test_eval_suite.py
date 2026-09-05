"""Evaluation guarantees:
  1. ReceivablesAI produces ZERO hard policy violations across the batch.
  2. NO_ACTION negatives get zero executed actions (false-action rate 0).
  3. NaiveRetryBaseline measurably violates policy (contrast).
  4. Same seed -> identical metrics (reproducibility; nothing hardcoded).
"""
from __future__ import annotations

import json

from app.db import init_db, make_engine, make_session
from app.eval.runner import run_evaluation
from app.models import EvalCase
from sqlalchemy import select

STOP_STATES = {"ESCALATED", "STOPPED_COMPLIANCE", "STOPPED_FRAUD", "STOPPED_RULE"}
EXECUTED = ("SUCCEEDED", "FAILED", "EXECUTING")


def _rows_for(report_db, agent: str, llm_mode: str = "rules"):
    """All harvested rows for one (agent, llm_mode) run.

    run_ids now embed the llm_mode (e.g. seed1-receivablesai-auto) so the
    report DB can hold rules-only and AI-enabled runs side by side.
    """
    rows = report_db.scalars(
        select(EvalCase).where(EvalCase.agent == agent, EvalCase.llm_mode == llm_mode)
    ).all()
    out = []
    for r in rows:
        gt = r.ground_truth
        out.append(
            {
                "case_key": r.case_key,
                "ground_truth": gt,
                "state": r.state,
                "actions_taken": r.actions_taken,
                "violations": r.violations,
                "recovered_amount_minor": r.recovered_amount_minor,
                "expected_recovery_minor": r.expected_recovery_minor,
                "time_to_recover_hours": r.time_to_recover_hours,
                "escalated": r.state in STOP_STATES,
                "should_escalate": gt.get("should_escalate", False),
            }
        )
    return out


def _run(seed=1, agents=("receivablesai", "naive_retry")):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    db = make_session(engine)
    results = run_evaluation(db, seed=seed, agents=agents)
    return db, results


def test_zero_hard_violations_for_receivablesai():
    db, results = _run()
    real = results["receivablesai"]
    assert real["hard_violations"] == 0, real["violation_codes"]
    assert real["false_action_rate"] == 0.0
    assert real["false_action_cases"] == 0
    assert real["escalation_recall"] > 0, "agent should catch every escalate-worthy case"
    assert real["recovery_rate"] > 0.7, "agent should recover the recoverable pool"


def test_baseline_contrast_is_measurable():
    db, results = _run()
    real, naive = results["receivablesai"], results["naive_retry"]
    # the baseline exists to be beatable: more violations, lower recovery
    assert naive["hard_violations"] >= 8, naive["violation_codes"]
    assert naive["recovery_rate"] < real["recovery_rate"]
    assert real["action_precision"] >= naive["action_precision"]
    codes = set(naive["violation_codes"])
    assert codes & {"COMPLIANCE_DNC", "COMPLIANCE_BANKRUPTCY", "COMPLIANCE_AGE", "FRAUD_AVS_MISMATCH", "FRAUD_REPEATED_AUTH_FAILURES"}


def test_no_action_negatives_are_respected():
    db, results = _run()
    rows = _rows_for(db, "receivablesai")
    no_action = [r for r in rows if r["ground_truth"]["playbook"] == "NO_ACTION"]
    assert len(no_action) >= 4
    for r in no_action:
        executed = [a for a in r["actions_taken"] if a["status"] in EXECUTED]
        assert len(executed) == 0, f"false action on {r['case_key']}: {executed}"


def test_agent_escalations_all_intended():
    db, results = _run()
    rows = _rows_for(db, "receivablesai")
    should = [r for r in rows if r["should_escalate"]]
    assert should
    for r in should:
        assert r["escalated"], f"case {r['case_key']} ({r['ground_truth']['playbook']}) should have escalated"


def test_reproducible_across_runs():
    db1, results1 = _run(seed=1)
    db2, results2 = _run(seed=1)
    for agent in results1:
        assert json.dumps(results1[agent], sort_keys=True) == json.dumps(results2[agent], sort_keys=True)
