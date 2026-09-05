"""Top-level evaluation runner.

For a given seed it builds the deterministic corpus and runs the configured
agents — `receivablesai` (the real, policy-gated agent) and `naive_retry`
(baseline) — each on a fresh in-memory database, through the real agent loop.
`llm_mode` selects rules-only ("rules") vs AI-enabled ("auto", offline NLU
diagnoser when no key is present). Harvested rows are persisted to the report
DB (eval_cases / eval_runs) and metrics are computed from those rows.

`run_id` makes runs addressable from the dashboard and reproducible: the same
(seed, agent, llm_mode) always yields the same metrics. The report DB can hold
rules-mode and auto-mode runs side by side for the AI-value comparison.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app import constants as C
from app.agent import AgentConfig
from app.clock import reset_clock
from app.db import init_db, make_engine
from app.eval.baselines import naive_plan
from app.eval.corpus import build_corpus
from app.eval.harness import simulate_case
from app.eval.metrics import compute_metrics
from app.execution.sandbox import SeededSandbox
from app.models import EvalCase, EvalRun

AGENT_NAIVE = "naive_retry"
AGENT_REAL = "receivablesai"


def _config_for(agent: str, llm_mode: str = "rules") -> AgentConfig:
    if agent == AGENT_NAIVE:
        return AgentConfig(
            label=agent,
            mode=C.MODE_AUTONOMOUS,
            llm_mode="rules",
            provider=SeededSandbox(),
            plan_fn=naive_plan,
            schedule_next=False,
            defer_ptp=False,
            risk_gate=False,
        )
    # llm_mode="auto" without an API key routes to the deterministic OFFLINE
    # NLU diagnoser (AgentConfig.offline_llm) — this is what lets the eval
    # compare rules-only vs AI-enabled without a network model. See
    # app/diagnosis/offline.py for the honesty contract.
    return AgentConfig(
        label=agent,
        mode=C.MODE_AUTONOMOUS,
        llm_mode=llm_mode,
        provider=SeededSandbox(),
        offline_llm=(llm_mode == "auto"),
    )


def run_evaluation(
    report_db: Session,
    *,
    seed: int = 1,
    llm_mode: str = "rules",
    agents: tuple[str, ...] = (AGENT_REAL, AGENT_NAIVE),
    horizon_days: int = 45,
) -> dict:
    """Run the full batch. Returns {agent: metrics} and persists rows."""
    corpus = build_corpus(seed)
    run_tag = f"seed{seed}"
    out: dict[str, dict] = {}

    for agent in agents:
        run_id = f"{run_tag}-{agent}-{llm_mode}"
        cfg = _config_for(agent, llm_mode)
        harvest_rows: list[dict] = []

        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        from app.db import make_session

        with make_session(engine) as db:
            for cc in corpus:
                row = simulate_case(db, cc, cfg, horizon_days=horizon_days)
                row["run_id"] = run_id
                row["agent"] = agent
                row["llm_mode"] = llm_mode
                harvest_rows.append(row)

        reset_clock()
        metrics = compute_metrics(harvest_rows)
        _persist(report_db, run_id, seed, agent, llm_mode, metrics, harvest_rows)
        out[agent] = metrics

    return out


def _persist(
    report_db: Session,
    run_id: str,
    seed: int,
    agent: str,
    llm_mode: str,
    metrics: dict,
    rows: list[dict],
) -> None:
    # replace any previous rows with this run_id (idempotent re-runs)
    from sqlalchemy import delete

    report_db.execute(delete(EvalCase).where(EvalCase.run_id == run_id))
    report_db.execute(delete(EvalRun).where(EvalRun.run_id == run_id))

    run = EvalRun(
        run_id=run_id,
        seed=seed,
        llm_mode=llm_mode,
        agent=agent,
        started_at=datetime.now(),
        finished_at=datetime.now(),
        metrics=metrics,
    )
    report_db.add(run)
    for r in rows:
        report_db.add(
            EvalCase(
                run_id=run_id,
                case_key=r["case_key"],
                scenario=r["scenario"],
                seed=seed,
                agent=agent,
                llm_mode=llm_mode,
                state=r["state"],
                cause=r["cause"],
                ground_truth=r["ground_truth"],
                actions_taken=r["actions_taken"],
                violations=r["violations"],
                recovered_amount_minor=r["recovered_amount_minor"],
                expected_recovery_minor=r["expected_recovery_minor"],
                time_to_recover_hours=r["time_to_recover_hours"],
                correct_action=r["correct_action"],
                false_action=r["false_action"],
            )
        )
    report_db.commit()
