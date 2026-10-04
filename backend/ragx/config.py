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
    api_key: str | None = None  # if set, required as X-API-Key on every request
    # Comma-separated roots that file/directory sources must live under.
    # Empty = unrestricted (single-user local mode); Docker sets /docs,/app/data.
    source_roots: str = ""
    cors_origins: str = "http://localhost:3000"

    # Role -> "provider:model". Providers: anthropic, openai, gemini, fake.
    generator: str = "anthropic:claude-opus-5-5"
    verifier: str = "gemini:gemini-2.5-pro"
    utility: str = "anthropic:claude-haiku-4-5"  # gate, planner, grader, contextualizer
    judge: str = ""  # eval judge; defaults to verifier
    # Optional fallback chains, comma separated "provider:model" entries.
    generator_fallbacks: str = ""
    verifier_fallbacks: str = ""
    utility_fallbacks: str = ""

    # Embeddings: openai, gemini, voyage, hash (local, offline).
    embedder: str = "hash:hash-512"
    # Reranker: cohere, voyage, llm, lexical.
    reranker: str = "llm:utility"

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
