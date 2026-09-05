"""Diagnosis layer (the AI side).

AIR-GAP: nothing in this package may import `app.execution` (the layer that
touches Razorpay/sandbox) nor any HTTP client other than the optional LLM
adapter. `tests/test_air_gap.py` enforces this structurally (AST scan) and at
runtime (sys.modules check).

The diagnosis output is advisory data only: cause + confidence + evidence +
a recommended playbook. The policy engine (app.policy) is the only layer that
plans/authorizes actions.
"""
