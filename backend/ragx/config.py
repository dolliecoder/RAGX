"""Configuration.

Two layers:
- ``Settings``: process-level settings from environment (secrets, URLs, providers).
- ``RuntimeConfig``: tunable behaviour (thresholds, budgets, rules, prompts). It is
  versioned in the database so the Repair loop can propose, canary, promote and
  roll back changes to it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAGX_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./ragx.db"
    data_dir: Path = Path("./data")
    # Service key for automation/CLI: X-API-Key with this value acts as an admin.
    # Browsers never get it; people sign in with accounts.
    api_key: str | None = None
    # Comma-separated emails that are made admins when they sign up or log in.
    admin_emails: str = ""
    # Session cookie: set Secure when served over HTTPS (deployment).
    cookie_secure: bool = False
    session_days: int = 30
    # Public address of the website, used in links inside emails.
    app_url: str = "http://localhost:3000"

    # Outgoing email (password reset, email verification, invites). Optional.
    # Any SMTP service works, e.g. Gmail with an app password, Brevo or Resend free tiers.
    # smtp_host="memory" keeps emails in memory (tests / development).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""  # e.g. "RAGX <no-reply@yourcollege.edu>"
    smtp_tls: str = "starttls"  # starttls | ssl | none
    # Comma-separated roots that file/directory sources must live under.
    # Empty = unrestricted (single-user local mode); Docker sets /docs,/app/data.
    source_roots: str = ""
    cors_origins: str = "http://localhost:3000"

    # Role -> "provider:model".
    # Free providers: gemini (free tier key), groq (free key), openrouter (":free" models),
    # ollama (local open-source models, no key). Paid: anthropic, openai. Offline: fake.
    # Defaults are free; without any key or local model RAGX runs in offline mode.
    generator: str = "gemini:gemini-3.5-flash-lite"
    verifier: str = "gemini:gemini-3.1-flash-lite"  # a different model checks the answers
    utility: str = "gemini:gemini-3.5-flash-lite"  # gate, planner, grader, contextualizer
    judge: str = ""  # eval judge; defaults to verifier
    # Optional fallback chains, comma separated "provider:model" entries.
    generator_fallbacks: str = ""
    verifier_fallbacks: str = ""
    utility_fallbacks: str = ""

    # Embeddings: gemini, ollama, openai, voyage, hash (built-in, offline).
    embedder: str = "gemini:gemini-embedding-001"
    # Reranker: llm (uses the utility model), lexical (built-in), cohere, voyage.
    reranker: str = "llm:utility"

    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    openrouter_api_key: str | None = None
    ollama_url: str = "http://localhost:11434"
    ollama_num_ctx: int = 16384  # context window given to local models
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"  # also LM Studio / vLLM
    voyage_api_key: str | None = None
    cohere_api_key: str | None = None

    # Web fallback (only used by the heal ladder when enabled in RuntimeConfig).
    web_search_provider: str = "none"  # none, tavily, brave
    tavily_api_key: str | None = None
    brave_api_key: str | None = None

    # Background loops (seconds, 0 disables).
    repair_interval: int = 1800
    crawl_interval: int = 300
    canary_interval: int = 600

    request_timeout: float = 90.0


class TierBudget(BaseModel):
    max_subqueries: int
    max_heal_rounds: int
    max_llm_calls: int


class RuntimeConfig(BaseModel):
    """Everything the Repair loop is allowed to tune. Versioned in the DB."""

    # Gate / router
    gate_threshold: float = 0.3
    long_context_max_tokens: int = 200_000
    fast: TierBudget = TierBudget(max_subqueries=1, max_heal_rounds=1, max_llm_calls=8)
    standard: TierBudget = TierBudget(max_subqueries=5, max_heal_rounds=3, max_llm_calls=30)
    deep_workers: int = 4
    deep_max_llm_calls: int = 120
    auto_escalate_to_deep: bool = True

    # Retrieval
    retrieve_k: int = 150
    stage1_k: int = 50
    final_k: int = 20
    generate_k: int = 12
    rrf_k: int = 60
    retriever_weights: dict[str, float] = Field(
        default_factory=lambda: {"bm25": 1.0, "dense": 1.0, "exact": 0.8, "web": 0.6}
    )
    quality_prior_weight: float = 0.15
    helpfulness_prior_weight: float = 0.2

    # Twiddlers
    max_chunks_per_doc: int = 6
    dedupe_jaccard: float = 0.9
    freshness_boost: float = 0.1
    doc_boosts: dict[str, float] = Field(default_factory=dict)  # doc_id -> multiplier
    query_aliases: dict[str, list[str]] = Field(default_factory=dict)  # term -> aliases

    # Verification thresholds
    min_groundedness: float = 0.9
    max_contradiction: float = 0.1
    min_coverage: float = 0.8
    grader_min_relevant: int = 1

    # Web fallback
    web_fallback_enabled: bool = False
    web_max_results: int = 5

    # Prompt overrides (prompt name -> extra instructions appended to system prompt)
    prompt_addenda: dict[str, str] = Field(default_factory=dict)

    # Repair loop
    canary_pct: float = 0.2
    canary_min_samples: int = 20
    eval_regression_tolerance: float = 0.02
    eval_golden_sample: int = 50

    def budget(self, tier: str) -> TierBudget:
        return self.fast if tier == "fast" else self.standard


class PlanLimits(BaseModel):
    daily_questions: int = 30  # 0 = unlimited
    per_minute: int = 5
    deep_per_day: int = 2
    max_concurrent: int = 2


class AccessPolicy(BaseModel):
    """Who may sign up and how much they may use. Stored separately from
    RuntimeConfig so that config rollbacks never change access rules."""

    signup_enabled: bool = True
    allowed_email_domains: list[str] = Field(default_factory=list)  # empty = any domain
    join_code: str = ""  # empty = not required
    # Students must click the link in a verification email before asking questions.
    # Only enforced when outgoing email is configured.
    require_email_verification: bool = False
    default_plan: str = "free"
    plans: dict[str, PlanLimits] = Field(
        default_factory=lambda: {
            "free": PlanLimits(),
            "pro": PlanLimits(daily_questions=300, per_minute=15, deep_per_day=20, max_concurrent=4),
        }
    )
    # Whole-site daily cap protecting a shared (e.g. free-tier) model quota. 0 = none.
    global_daily_questions: int = 0

    def limits_for(self, plan: str) -> PlanLimits:
        return self.plans.get(plan) or self.plans.get(self.default_plan) or PlanLimits()


@lru_cache
def get_settings() -> Settings:
    return Settings()
