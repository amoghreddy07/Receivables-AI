"""Environment-driven configuration.

Every value has a safe default so the project runs out of the box with
`uvicorn app.main:app` and `pytest` — no keys required. Real Razorpay test
credentials and an OpenAI key are OPTIONAL and only enable optional paths.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"


def _data_path(name: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / name


@dataclass(frozen=True)
class Settings:
    # --- database ----------------------------------------------------------- #
    db_url: str = os.getenv("RA_DB_URL", f"sqlite:///{_data_path('receivables.db')}")

    # --- policy / safety ---------------------------------------------------- #
    compliance_max_days: int = int(os.getenv("RA_COMPLIANCE_MAX_DAYS", "120"))
    action_risk_min: float = float(os.getenv("RA_ACTION_RISK_MIN", "0.42"))
    contact_hour_start: int = int(os.getenv("RA_CONTACT_START", "9"))    # 09:00
    contact_hour_end: int = int(os.getenv("RA_CONTACT_END", "21"))       # 21:00
    high_value_auto_limit_minor: int = int(  # >= this amount requires human approval even in AUTONOMOUS
        os.getenv("RA_HIGH_VALUE_AUTO_LIMIT_MINOR", "100000000")          # Rs 10,00,000
    )
    customer_monthly_action_cap: int = int(os.getenv("RA_CUSTOMER_ACTION_CAP", "10"))
    global_daily_action_budget: int = int(os.getenv("RA_GLOBAL_DAILY_BUDGET", "500"))
    approval_timeout_minutes: int = int(os.getenv("RA_APPROVAL_TIMEOUT_MIN", "30"))

    # --- agent modes --------------------------------------------------------- #
    default_mode: str = os.getenv("RA_DEFAULT_MODE", "supervised")  # supervised | autonomous
    llm_mode: str = os.getenv("RA_LLM_MODE", "rules")               # rules | auto (fused)
    llm_timeout_seconds: float = float(os.getenv("RA_LLM_TIMEOUT", "8"))
    llm_model: str = os.getenv("RA_LLM_MODEL", "gpt-4o-mini")

    # --- optional credentials (never required) ------------------------------- #
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    razorpay_key_id: str = os.getenv("RAZORPAY_KEY_ID", "")
    razorpay_key_secret: str = os.getenv("RAZORPAY_KEY_SECRET", "")
    razorpay_webhook_secret: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

    # --- operational ---------------------------------------------------------- #
    demo_mode: bool = os.getenv("RA_DEMO_MODE", "1") == "1"   # enables audit tamper button
    hourly_tick: bool = os.getenv("RA_HOURLY_TICK", "1") == "1"
    eval_seed: int = int(os.getenv("RA_EVAL_SEED", "1"))
    horizon_days: int = int(os.getenv("RA_HORIZON_DAYS", "45"))  # eval simulation horizon

    @property
    def eval_db_url(self) -> str:
        return f"sqlite:///{_data_path('eval_runs.db')}"

    @property
    def corpus_dir(self) -> Path:
        p = PROJECT_ROOT / "data" / "eval_corpus"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
