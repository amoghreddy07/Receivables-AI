"""Execution layer — the ONLY place side effects happen.

Consumes validated ActionPlans from the policy engine. Never imports
app.diagnosis (diagnosis may not import execution either — air-gap test).
"""
