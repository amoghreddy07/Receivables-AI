# ReceivablesAI — Project Blueprint

Deep engineering handoff document. Everything here is traceable to the current
source tree (`backend/app`). Where a behaviour is worth calling out it is cited
as `file · function` (and `file:line` where the line was verified while writing
this). Thresholds are quoted from the code, not from memory.

**One-line architecture:** the agent detects revenue at risk, diagnoses the
cause from structured signals *and* unstructured customer communication, and
only ever executes what the deterministic policy engine authorizes.

> **AI proposes. Policy decides. Executor acts. Audit records.**

---

## 1. Module tree (actual layout)

```
backend/
├── pyproject.toml          # name = "receivablesai"; deps; optional "llm" extra (openai)
├── app/
│   ├── __init__.py
│   ├── agent.py            # orchestrator: DETECT -> DIAGNOSE -> DECIDE -> EXECUTE -> OBSERVE -> AUDIT
│   ├── audit/__init__.py   # append-only SHA-256 hash chain + verify()
│   ├── clock.py            # swappable clock (real vs VirtualClock)
│   ├── config.py           # Settings dataclass, env-driven, safe defaults
│   ├── constants.py        # single source of truth for states/causes/playbooks/actions/codes
│   ├── db.py               # engine/session helpers (SQLite, Postgres-friendly)
│   ├── detection.py        # risk scoring, case open/refresh, credit_received, set_state, escalate
│   ├── ingestion.py        # canonical event model, idempotent store + apply
│   ├── diagnosis/
│   │   ├── context_builder.py   # redacted read-only snapshot for the diagnoser
│   │   ├── rules_classifier.py  # deterministic classifier (abstains on free text)
│   │   ├── rules_map.py         # error-code -> cause signal map (shared vocabulary)
│   │   ├── offline.py           # deterministic NLU stand-in (eval/demo AI path)
│   │   ├── llm_diagnoser.py     # optional real-LLM one-shot diagnosis (never required)
│   │   └── fusion.py            # rules+LLM fusion with exact semantics + thresholds
│   ├── policy/
│   │   ├── engine.py      # plan_action — the ONLY authorizer of side effects
│   │   ├── rules.py       # deterministic compliance/fraud/stopping rules
│   │   └── playbooks.py   # data-driven playbook registry (plain Python dicts)
│   ├── execution/
│   │   ├── provider.py        # PaymentProvider ABC (the execution contract)
│   │   ├── sandbox.py         # SeededSandbox — deterministic counterparty model
│   │   ├── razorpay_client.py # optional real test-mode Razorpay adapter
│   │   └── executors.py       # whitelist + idempotency-keyed exactly-once execution
│   ├── eval/
│   │   ├── corpus.py    # 63 deterministic B2B cases with ground truth
│   │   ├── harness.py   # replays the real agent loop per case on a virtual clock
│   │   ├── baselines.py # naive_retry policy-less baseline (plan_fn bypass)
│   │   ├── metrics.py   # metrics computed ONLY from harvested rows
│   │   └── runner.py    # run_evaluation(): corpus x agents -> report DB
│   ├── main.py          # FastAPI console (read-only views + 3 scoped POSTs)
│   ├── models.py        # SQLAlchemy 2.0 ORM
│   ├── presentation.py  # display-only label/format helpers (template namespace "P")
│   ├── static/dashboard.css
│   └── templates/       # base, overview, cases, case_detail, approvals, evaluation, audit, _eval_status
├── scripts/
│   ├── run_eval.py      # CLI batch runner
│   └── seed_demo.py     # deterministic 9-case demo seeder
└── tests/               # 12 files, 62 tests
```

Note: `app/diagnosis/rules_classifier.py`, `app/execution/sandbox.py` and
`app/execution/razorpay_client.py` docstrings reference `docs/SIGNAL_MAP.md`,
`docs/SANDBOX.md` and `docs/ARCHITECTURE.md`. **Those files do not exist in the
repo** — the signal vocabulary and honesty contracts are actually implemented in
code (`app/policy/rules.py` signal sets, `app/diagnosis/rules_map.py`,
`app/execution/sandbox.py`), and this blueprint is the current reference.

---

## 2. Module responsibilities, data flow, call-outs

### 2.1 `config.py` — Settings
`Settings` dataclass (`config.py · Settings`), frozen, defaults read from env at
import time. All policy knobs are here (values below under §7). Data paths:
`DATA_DIR = PROJECT_ROOT / "data"`; `eval_db_url` property points at
`data/eval_runs.db`. The demo DB is `data/demo.db` (set by `RA_DB_URL` in the
launcher/seeder), while the code default `db_url` is `data/receivables.db`.

### 2.2 `clock.py` — swappable time
All timestamps flow through `now()`. Tests and the eval harness install a
`VirtualClock` (`clock.py · VirtualClock`) so runs are bit-reproducible;
`reset_clock()` (clock.py:62) restores the real clock. Policy window checks
compare hour-of-day only.

### 2.3 `db.py` — persistence
`make_engine` / `init_db` / `make_session` / `get_db` (FastAPI dependency).
SQLite enables `PRAGMA foreign_keys=ON`. Eval and tests pass their own
in-memory engines so runs are isolated.

### 2.4 `models.py` — schema
- Money is **integer paise** everywhere (`amount_minor`), never float.
- `Customer` (with `behavior_profile` + `meta` JSON — the deterministic sandbox
  seed), `ComplianceFlag` (DNC / BANKRUPTCY / LEGAL_HOLD), `Invoice`
  (`rzr_invoice_id` unique, `outstanding_minor` property), `Payment`.
- `RiskCase` — one row per invoice, the unit the agent works on; carries
  `state`, `cause`, `diagnosis_path`, `recovered_amount_minor`, `next_action_at`.
- `CaseMessage` — inbound/outbound free text (emails, AP comms, promises) that
  the deterministic classifier cannot read; the reason rules-only escalates.
- `RiskEvent` — idempotency store for incoming events (`event_id` unique).
- `Diagnosis`, `Intervention` (idempotency-keyed), `Approval`, `Escalation`,
  `PTPPromise`, `AuditEvent` (hash-chained), and the eval tables `EvalCase` /
  `EvalRun`.

### 2.5 `ingestion.py` — canonical events, exactly-once
Money/revenue signals enter as canonical events (`payment.failed`,
`payment.captured`, `invoice.paid`, …). `store_event` dedups on
`risk_events.event_id` (duplicate delivery / crash-and-replay can never
double-apply). `apply_event_to_entities` turns failure events into `Payment`
rows and money events into credits via `detection.credit_received` — the single
place `invoice.paid_amount_minor` moves.

### 2.6 `detection.py` — risk scoring + lifecycle
`risk_score_for` is a transparent weighted model (base 0.25; amount buckets
≥₹50k/₹10k/₹1k; +0.20 any failed attempt; +0.05×confidence for technical/funds/
auth error classes; +min(0.25, days/600) overdue) — it *ranks and queues*, it
never authorizes (the policy engine does). `open_or_refresh_case` creates a
`RiskCase` when revenue is at risk; `credit_received` credits both invoice and
case and drives `RECOVERED`; `set_state` enforces the allowed-transition table
(`constants.py`) and writes `state_changed` audit events; `escalate` creates an
`Escalation` row and moves the case to `ESCALATED` (terminal).

### 2.7 `diagnosis/` — two disjoint evidence layers
The rules classifier and the NLU/LLM diagnoser see **disjoint evidence on
purpose** (stated in `offline.py` docstring): the classifier reads persisted
facts (error codes, invoice state, promises, compliance flags) and *never* the
message text; the NLU/LLM reads the redacted `communication` transcript and
*never* payment error codes.

- `context_builder.py · build_snapshot` — one read-only, redacted dict: emails
  masked, phones truncated, digit runs scrubbed, transcript truncated at 1200
  chars. The model sees exactly this dict.
- `rules_classifier.py · classify` — priority: compliance context (labels cause
  `overdue`, the policy engine will hard-stop regardless) → missed promise
  (`ptp_missed`, 0.95) → failed payment error code via `rules_map` (only counts
  as a hard structured signal at confidence ≥ `_STRUCTURED_SIGNAL_CONF = 0.85`;
  ≥2 same-class failures +0.1) → **abstain** (`insufficient_context`, 0.35,
  `proposed_playbook=""`) when ≥60-char inbound messages exist → invoice-state
  inference last, only when no free text contradicts it.
- `offline.py · offline_diagnose` — deterministic NLU over the snapshot's
  inbound transcript. Branches, in order: documentation blocker (willing payer,
  wrong GST/PO → `documentation_issue`, 0.88), dispute/refusal tokens
  (`invoice_dispute`, 0.92, never collect), AP queue people+flow
  (`awaiting_ap_approval`, 0.85), explicit payment promise with a parseable
  concrete date ≥ as-of (`ptp_offered`, 0.9, carries `promised_date_iso`);
  else `unknown` 0.3. Every result quotes its evidence. Honesty contract: this
  is a deterministic *emulation* of the model's intended prompt behaviour —
  never presented as a real model.
- `llm_diagnoser.py · diagnose_with_llm` — optional one-shot call, strict JSON
  schema (root cause enum + recommended playbook enum shared with the rules),
  `temperature=0`. **Never raises**: missing key, timeout, HTTP error, malformed
  JSON all degrade to `None` → rules-only path with `DIAGNOSED_BY_RULES` audit
  marker. Lazy-imports `openai` so it is never required to import/run.
- `fusion.py · fuse` — exact semantics (unit-tested in
  `tests/test_fusion_fallback.py`):
  - rules abstained + LLM present → LLM carries (`path=llm`); LLM `unknown` or
    confidence < 0.5 → flagged, escalate (never act blind).
  - agreement → `conf = 0.5*rule + 0.5*llm`; `≥0.65` autonomous,
    `0.5–0.65` requires approval, `<0.5` flagged/escalate
    (`AUTONOMOUS_CONF = 0.65`, `APPROVAL_CONF_LOW = 0.5`).
  - disagreement → **never auto-act**, escalate with both diagnoses audited.
  - LLM unavailable → pure rules; abstention still escalates, never guesses.

### 2.8 `policy/` — the only authorizer
- `engine.py · plan_action` — decision order: compliance → fraud → playbook
  resolution (from `CAUSE_TO_PLAYBOOK`; a proposed playbook different from the
  mapping is logged as `POLICY_OVERRIDE`) → stopping rules → gates → verdict.
  `promise_accept` without an extracted promised date escalates
  (`PROMISE_DATE_MISSING`). Returns an `ActionPlan` (verdict, playbook, action,
  params, reasons, escalate_code, defer_to, stop_state). `audit_only=True`
  evaluates rules without producing an execution plan — used by the evaluator
  to audit baseline actions.
- `rules.py` — all rules deterministic functions of persisted data + clock
  (details in §7). Signal vocabulary sets (`SIGNAL_AVS_CODES`,
  `SIGNAL_FRAUD_TEXTS`, …) are shared with diagnosis via `rules_map.py` so the
  two layers can never disagree.
- `playbooks.py` — data-driven registry (plain Python dicts; an early
  external-config approach was replaced by code-level dicts during development).
  Per playbook: `causes`,
  `max_attempts`, `cooldown_hours`, `channels`, `actions`, `human_gate`,
  `exhaust_reason`. Refunds/discounts/amount-edits/legal threats are **not** in
  the registry at all. `CAUSE_TO_PLAYBOOK` is the deterministic cause→playbook
  table the engine consults.

### 2.9 `execution/` — bounded, exactly-once
- `provider.py · PaymentProvider` — the contract both sandbox and real client
  implement; every op returns a normalized dict incl. `pending_payment` (money
  arriving later) and `failure_code`.
- `sandbox.py · SeededSandbox` — deterministic counterparty model. Two worlds:
  structured profiles (`pays_after_reminder` pays on reminder step 1, etc.) and
  **scenario-gated** profiles (`docs_issue` pays only on
  `resend_corrected_invoice`, `ap_queue` only on `request_ap_update`,
  `ptp_offer_will_pay` only on `confirm_ptp`, dispute/never-pays never pay).
  Any other dispatch succeeds as a dispatch but produces **no simulated
  payment** — the world responds only to the *executed action*, never to the
  diagnosis or the LLM's reasoning. Offsets: seeded via
  `invoice.meta["scenario_offset_hours"]` or a deterministic hash → 24–72h.
- `razorpay_client.py · RazorpayClient` — optional; active only when
  `RAZORPAY_KEY_ID`+`RAZORPAY_KEY_SECRET` are set. Maps the playbook ops onto
  real test-mode endpoints (`payment_link.create`, `invoice.notify`); a failed
  charge "retry" is honestly modelled as a fresh payment link (no retry endpoint
  exists). Never fabricates `pending_payment` hints. Docstring mentions webhook
  verification in an `app/api` module — **that module does not exist**; live
  webhook ingestion is future scope.
- `executors.py · execute_action` — last whitelist gate before any provider
  call (`action not in PLAYBOOKS[playbook].actions` → audited block); deterministic
  `build_idempotency_key` (`case:action:attempt`); existing key → returns the
  stored intervention **without a second provider call**. Never auto-retries
  (the agent loop owns scheduling). `record_promise` persists accepted
  promises idempotently.

### 2.10 `audit/__init__.py` — tamper-evident log
Append-only. `hash_i = SHA256(prev_hash + canonical_json(payload))` with
`prev_hash = "GENESIS"` for the first event; `canonical_json` uses
`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` so
whitespace/ordering can never change a hash. `audit_event` appends inside the
caller's transaction; `verify()` walks the whole chain and returns the first
broken row; `case_events` returns a case's events by `seq`.

### 2.11 `agent.py` — orchestration
`step_case` is one DETECT→DIAGNOSE→DECIDE→EXECUTE→OBSERVE pass: refresh risk
posture → PTP deferral (an active future promise sets `next_action_at` and
returns `deferred_ptp`) → `diagnose_case` (rules → optional LLM/offline NLU →
fusion; a fused LLM-only low-confidence diagnosis is rewritten to
`insufficient_context` so it escalates) → risk-gate watch → `plan_action` →
audit `policy_decision` → dispatch on verdict (BLOCKED → terminal stop with an
`Escalation` row; ESCALATE → `escalate()`; DEFERRED → `next_action_at`;
REQUIRES_APPROVAL → `_park_for_approval` cancelling older pending actions;
else `execute_action`) → `_observe_execution` (schedule next ladder step,
drain pending money, escalate when a ladder is exhausted). Batch entry points:
`ingest_events` (idempotent), `run_tick` (approval expiry, pending-money drain,
overdue detection, due scheduled actions), `decide_approval` (§5).

### 2.12 `eval/` — deterministic batch evaluation
- `corpus.py · build_corpus(seed)` — 63 cases, deterministic for a seed. Ground
  truth (`playbook`, `expected_recovery_minor`, `should_escalate`) is used
  **only for scoring**, never to steer the agent or sandbox. Covers technical /
  funds / auth / expired failures, dunning ladders, partial balances, DNC /
  bankruptcy / 120-vs-121 boundary, fraud hard stops, high-value gate, PTP
  kept/broken, after-hours deferral, NO_ACTION negatives (self-heals +
  low-risk watch), and the unstructured set (docs blocker ×3 incl. a
  late-pay variant, AP queue ×2, explicit promises ×2, dispute/refusal ×3,
  DNC and bankruptcy notice emails, fraud corroboration email).
- `harness.py · simulate_case` — replays the real agent loop per case on a
  per-case `VirtualClock` until the case settles or the 45-day horizon ends;
  harvests self-contained rows (state, actions, violations, money, TTR).
  `_audit_intervention` re-audits executed baseline actions against the real
  policy rules (un-enforced) so violations are measurable.
- `baselines.py · naive_plan` — policy-less: retry any failed payment once,
  remind any overdue once, regardless of compliance/fraud/time/promises. Runs
  through the same loop mechanics via `AgentConfig.plan_fn` (bypasses the
  engine; the evaluator audits afterwards).
- `metrics.py · compute_metrics` — metrics ONLY from harvested rows (see §6).
- `runner.py · run_evaluation` — corpus × agents (`receivablesai`,
  `naive_retry`) × `llm_mode` (`rules` | `auto`) on fresh in-memory DBs;
  `run_id = seed{seed}-{agent}-{mode}` makes runs addressable and
  idempotently replaceable in the report DB (`data/eval_runs.db`). `auto`
  without an API key routes to the offline NLU via `offline_llm=True`
  (production never sets this).

### 2.13 `main.py` — dashboard
Read-only views over persisted rows plus three strictly-scoped POSTs (module
docstring). Demo labelling uses `meta.demo`/`meta.simulated` written by the
seeder (`_is_demo_case` / `_is_simulated_case` read `invoice.meta`), and the
`demo_db_active()` guard = `RA_DEMO_MODE=1` **and** DB basename `demo.db`.
Routes: `/` (main.py:361), `/cases` (main.py:430), `/cases/{id}` (main.py:522),
`/approvals` (main.py:683), `POST /approvals/{id}/decide` (main.py:698),
`/eval` (main.py:754), `/eval/status` (main.py:793, HTMX poll fragment),
`POST /eval/run` (main.py:826, background thread + `_eval_state`), `/audit`
(main.py:842), `POST /demo/tamper` (main.py:890), `POST /demo/reset`
(main.py:932). See §5 for the approval contract.

### 2.14 `presentation.py` — display-only
Label maps (`STATE_LABELS`, `VERDICT_LABELS`, `PATH_LABELS`, …) and formatters
(`inr_major`, `pct`, `fmt_dt`); registered as the `P` namespace in Jinja. No
business logic.

---

## 3. State machine / status transitions

**Case states** (`constants.py`): `OPEN`, `PENDING_APPROVAL`, `RECOVERED`,
`CLOSED_NOOP`, `STOPPED_COMPLIANCE`, `STOPPED_FRAUD`, `STOPPED_RULE`,
`ESCALATED`. Terminal set: `RECOVERED`, `CLOSED_NOOP`, the three `STOPPED_*`,
`ESCALATED`. Transient states (`EXECUTING`, …) exist only inside a synchronous
step and are captured in the audit trail, never persisted on the case.

Allowed transitions (enforced by `detection.set_state` via
`constants.allowed_transition`; a disallowed move logs
`transition_warning` and still applies — defensive, non-crashing):

```
OPEN ──────────────► OPEN, PENDING_APPROVAL, RECOVERED, CLOSED_NOOP,
                     STOPPED_COMPLIANCE, STOPPED_FRAUD, STOPPED_RULE, ESCALATED
PENDING_APPROVAL ──► OPEN (declined→watch), RECOVERED, STOPPED_*, ESCALATED, CLOSED_NOOP
```

Transition examples from the code paths: invoice paid + prior action →
`RECOVERED`; paid before any action → `CLOSED_NOOP` (self-healed); policy
BLOCKED → the matching `STOPPED_*` + an open `Escalation` row; human
DECLINE / approval timeout → `CLOSED_NOOP`; ladder exhausted / low confidence /
dispute → `ESCALATED`.

**Intervention statuses** (`models.py`): `PENDING_APPROVAL | APPROVED |
DECLINED | EXECUTING | SUCCEEDED | FAILED | CANCELLED`.

---

## 4. Diagnosis pipeline (LLM proposes)

1. `context_builder.build_snapshot` — redacted case dict incl. `communication`.
2. `rules_classifier.classify` — deterministic facts; abstains
   (`insufficient_context`) when substantive free text is present and no hard
   structured signal decides.
3. `llm_diagnoser.diagnose_with_llm` (real model) or `offline.offline_diagnose`
   (deterministic stand-in, `llm_mode=auto` without key + `offline_llm=True`)
   — reads the transcript, returns structured cause/confidence/evidence.
4. `fusion.fuse` — exact combination semantics (§2.7); fused output may be
   rewritten to `insufficient_context` when an LLM-only reading is below the
   autonomous band, forcing escalation instead of blind action.
5. `policy.engine.plan_action` — compliance → fraud → playbook → stopping
   rules → gates. The LLM's proposal is advisory input to this; every decision
   is audited as `policy_decision` (agent.py), and the LLM call itself is
   audited as `llm_diagnosis` / `llm_unavailable_rules_fallback`.

---

## 5. Approval lifecycle

1. **Park** — `agent.step_case` on a `REQUIRES_APPROVAL` verdict calls
   `_park_for_approval`: cancels any older pending intervention for the case
   (single pending action), creates an `Intervention(status=PENDING_APPROVAL)`
   with its reserved idempotency key, audits `approval_requested`, and sets the
   case to `PENDING_APPROVAL`.
2. **Present** — `main.py:698 approval_decision` is the only UI entry point
   (`POST /approvals/{intervention_id}/decide`, form fields `decision`,
   `reason`, `actor`, `next`). It validates the decision enum and the
   `next` redirect target, then delegates entirely to
   `agent.decide_approval`. **No approval logic lives in the UI.**
3. **Decide** — `agent.decide_approval` guards (intervention exists; status is
   `PENDING_APPROVAL`; decision in `{APPROVED, DECLINED}`), writes the
   `Approval` row + `human_decision` audit event, then:
   - `DECLINED` → intervention `DECLINED`, case `CLOSED_NOOP` (reason recorded).
   - `APPROVED` → intervention `EXECUTING`, rebuilds the `ActionPlan` from the
     parked intervention's own fields, audits `approval_granted_executing`, and
     `_run_parked` executes through the same executor/provider paths with the
     reserved idempotency key; outcome audited as `action_result`.
4. **Timeout** — `agent.run_tick._expire_stale_approvals` auto-declines
   (`decided_by="system-timeout"`, safe-default DECLINED) interventions older
   than `RA_APPROVAL_TIMEOUT_MIN` (30) minutes; case → `CLOSED_NOOP`.
5. **Decline reason (server)** — `main.py:698`: `DECLINED` with a blank /
   whitespace `reason` re-renders the approvals inbox (HTTP 200) with an inline
   `decline_error` and leaves the intervention pending — never a bare
   400/422/500. `APPROVED` never requires a reason.

---

## 6. Evaluation methodology

Reproducible end-to-end without network or credentials: `auto` mode without an
API key uses the offline NLU diagnoser (runner.py:45-55), and every run is on a
fresh in-memory DB under a per-case virtual clock (harness.py).

**Pipeline** (runner.py): `build_corpus(seed)` → for each agent × mode, seed
each case via the real ORM + sandbox, replay the real agent loop
(`simulate_case`), harvest rows → `compute_metrics` → persist to
`data/eval_runs.db` under `run_id = seed1-{agent}-{mode}` (idempotent replace).
Two agents: `receivablesai` (real, policy-gated; runner.py:39-52) and
`naive_retry` (policy-less `plan_fn` bypass, audited afterwards).

**Metric definitions** (metrics.py, computed only from harvested rows):

| Metric | Definition (metrics.py) |
|---|---|
| recovery rate | recovered cases / recoverable cases (`expected_recovery_minor > 0`) |
| money recovered | sum of `recovered_amount_minor` (+ `pct_of_expected`) |
| action precision | executed actions matching the ground-truth playbook / executed actions (only where GT ≠ NO_ACTION) |
| false-action rate | NO_ACTION cases with ≥1 executed action / NO_ACTION cases |
| hard violations | sum of policy-rule audit codes across executed actions (`_audit_intervention`) |
| escalation precision/recall | correct escalations / escalations performed; correct / should-escalate |
| avg time to recover | mean simulated hours across recovered cases |
| per-playbook | recovered cases + money by ground-truth playbook |

**Honesty properties** (harness.py, corpus.py, sandbox.py): ground truth is
used only for scoring; the sandbox responds to persisted case data
(`behavior_profile`, error codes, dates, scenario seeds) and the *executed
action*, never to the diagnosis or the model's reasoning; the naive baseline is
audited with the same policy rules it ignores, so its 13 violations are
measurable, not asserted.

**Fresh results (seed 1)**: AI-enabled 97.4% / rules-only 82.0% / naive 51.3%;
₹61,78,000 (96.9%) / ₹45,86,000 (71.9%) / ₹38,38,000 (60.2%); precision
100% / 100% / 57.6%; hard violations 0 / 0 / 13. Rules-only recovers 0 of the
unstructured docs/AP/promise cases (the classifier abstains → escalate), which
is the entire AI-value delta.

---

## 7. Failure / stop conditions (with real threshold values)

**Policy order** (engine.py): compliance → fraud → stopping → gates.

Compliance hard stops (rules.py `compliance_hits`):
- `BANKRUPTCY` flag → `COMPLIANCE_BANKRUPTCY`
- `DNC` flag → `COMPLIANCE_DNC`; `LEGAL_HOLD` flag → also `COMPLIANCE_DNC`
- `days_overdue > 120` → `COMPLIANCE_AGE` (`RA_COMPLIANCE_MAX_DAYS=120`;
  exactly 120 is **not** blocked — boundary tests cover 120/121)

Fraud hard stops (rules.py `fraud_hits`):
- AVS codes `{AVS_FAILED, AVS_MISMATCH, RISK_AVS_FAILED}` on the latest failed
  payment → `FRAUD_AVS_MISMATCH`
- fraud-lock text (`fraud`, `suspicious`, `risk_lock`, `blocked_by_bank_risk`)
  on any recent attempt → `FRAUD_SUSPICIOUS_ACTIVITY`
- ≥3 authorization failures (`AUTH_FAILED`, `AUTHENTICATION_FAILED`,
  `2FA_FAILED`, `AUTH_DECLINED_BY_ISSUER`) within 7 days →
  `FRAUD_REPEATED_AUTH_FAILURES`

Stopping rules (rules.py `stopping_hits`): `MAX_ATTEMPTS_REACHED` (hard, per
playbook `max_attempts` — e.g. dunning 3, retry 2); deferrable soft blockers →
`DEFERRED`: `IN_COOLDOWN` (per-playbook `cooldown_hours`),
`OUTSIDE_CONTACT_HOURS` (window `RA_CONTACT_START=9` … `RA_CONTACT_END=21`;
escalate action exempt), `CUSTOMER_CAP` (`RA_CUSTOMER_ACTION_CAP=10`/month),
`GLOBAL_BUDGET` (`RA_GLOBAL_DAILY_BUDGET=500`/day). Hard stopping rule hits →
`ESCALATE` (never silently dropped). Ladder exhaustion →
`ESC_LADDER_EXHAUSTED` / `ESC_PTP_BROKEN`.

Gates (engine.py): `HIGH_VALUE_GATE` — `amount_at_risk_minor >=
RA_HIGH_VALUE_AUTO_LIMIT_MINOR` (₹10,00,000 paise) → `REQUIRES_APPROVAL` even
in autonomous mode; `SUPERVISED_MODE` (`RA_DEFAULT_MODE=supervised`) → every
action `REQUIRES_APPROVAL`.

Other escalate codes: `LOW_CONFIDENCE` (fusion-flagged / abstained),
`INVOICE_DISPUTE` (dispute cause routes to humans, never auto-collected),
`PROMISE_DATE_MISSING` (ptp_offered without an extractable date).

Fusion thresholds (fusion.py): autonomous ≥ 0.65; requires approval
0.5–0.65; below 0.5 or any disagreement → flagged, never autonomous.

Execution safety: whitelist gate at the executor (executors.py); idempotency
keys (case:action:attempt); duplicate events dropped at ingestion.

---

## 8. Key invariants (guarded by tests)

1. **Air gap** — `tests/test_air_gap.py`: diagnosis modules never import
   execution/Razorpay code; the offline NLU reads only the `communication`
   snapshot field; the rules classifier never reads message text.
2. **Policy is the only authorizer** — baselines bypass it via `plan_fn` only
   for measurement, and are audited afterwards.
3. **Exactly-once** — `tests/test_idempotency_flows.py`: duplicate events and
   duplicate idempotency keys never double-execute or double-credit.
4. **Compliance boundary** — `tests/test_compliance_boundary.py`: 120 allowed,
   121 blocked.
5. **Fraud is deterministic** — `tests/test_fraud_rules.py`: no agent, LLM
   included, can retry a fraud-signalled payment.
6. **Hash chain integrity** — `tests/test_hashchain.py`: `verify()` catches any
   payload mutation.
7. **Fusion/fallback semantics** — `tests/test_fusion_fallback.py`.
8. **AI value is real** — `tests/test_ai_eval_gap.py` /
   `tests/test_unstructured_ai_value.py`: rules abstain where the answer is in
   free text; the AI-enabled mode recovers what rules-only cannot.
9. **Approval contract** — `tests/test_approval_flow.py` +
   `tests/test_approval_modal_contract.py`: APPROVED/DECLINED transmission,
   blank-decline rejection, approved-path unchanged.
10. **Eval reproducibility** — `tests/test_eval_suite.py`: same seed →
    identical metrics; metrics never hardcoded.

---

## 9. Known gaps / honest caveats

- The three `docs/*.md` files referenced in code docstrings do not exist; this
  blueprint is the current documentation (see §1 note).
- No live webhook receiver (`app/api` mentioned in `razorpay_client.py`
  docstring is not in the tree); events enter via `ingest_events`/`run_tick`.
- "AI-enabled" eval = offline NLU stand-in; real-LLM runs require an API key
  and are intentionally outside the deterministic eval.
- The dashboard is single-user and unauthenticated; production deployment
  (Postgres, auth, cron ticks) is future work.