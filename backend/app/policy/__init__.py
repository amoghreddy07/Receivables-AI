"""Policy layer: the ONLY authorizer of actions in the system.

The LLM proposes (diagnosis.recommended_playbook); policy disposes. Nothing in
this package ever calls an LLM.
"""
