"""Evaluation: deterministic corpus, real-loop harness, baselines, metrics.

Metrics are ALWAYS computed from actual run rows (eval_results / DB state after
a run) — never hardcoded. The harness replays the real agent loop against the
seeded sandbox on a per-case virtual clock.
"""
