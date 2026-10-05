export type KB = { id: string; name: string; description: string; visibility: "all" | "admins"; documents: number; tokens: number; created_at: string };

export type Citation = {
  n: number;
  chunk_id: string | null;
  doc_id: string | null;
  title: string;
  section: string;
  page: number | null;
  url: string | null;
  cited_text: string;
  origin: string;
};

export type Span = { start: number; end: number; text: string; citations: number[] };

export type Metrics = {
  groundedness?: number | null;
  contradiction?: number;
  coverage?: number | null;
  citation_accuracy?: number;
  claims?: number;
  confidence?: number;
};

export type Answer = {
  status: "verified" | "partial" | "failed" | "error" | "direct" | "escalated";
  answer: string;
  sentences: Span[];
  citations: Citation[];
  unanswered: string[];
  conflicts: string[];
  route: string;
  metrics: Metrics;
  job_id: string | null;
  trace_id?: string;
  usage?: Usage;
  config_version: number;
  llm: { calls: number; tokens_in: number; tokens_out: number; by_task: Record<string, number> };
  latency_ms: number;
  healed: boolean;
  offline: boolean;
};

export type TraceBrief = {
  id: string;
  query: string;
  route: string;
  status: string;
  groundedness: number | null;
  contradiction: number | null;
  coverage: number | null;
  confidence: number | null;
  healed: boolean;
  negative_signal: boolean;
  llm_calls: number;
  latency_ms: number;
  config_version: number;
  is_eval: boolean;
  session_id: string | null;
  user_id?: string | null;
  user_email?: string | null;
  created_at: string;
};

export type Step = { stage: string; t_ms: number; [k: string]: unknown };

export type Trace = TraceBrief & {
  kb_id: string;
  answer: string;
  tokens_in: number;
  tokens_out: number;
  data: {
    steps: Step[];
    diag: {
      heals: Record<string, unknown>[];
      rescues: Record<string, unknown>[];
      unsupported: Record<string, unknown>[];
      degraded: string[];
      errors: string[];
      rewrites?: { subq: number; queries: string[]; new_terms: string[] }[];
    };
    subquestions: { question: string; variants: string[]; identifiers: string[] }[];
    evidence: { id: string; doc_id: string | null; verdict: string; subqs: number[]; origin: string; url: string | null }[];
    sentences: Span[];
    citations: Citation[];
    claims?: { sentence: number; claim: string; support: string; supporting: number[]; contradicting: number[] }[];
    unanswered: string[];
    conflicts: string[];
    metrics: Metrics;
    llm: { calls: number; by_task: Record<string, number> };
    job_id: string | null;
  };
  feedback: { kind: string; rating: number; comment: string; correction: string; created_at: string }[];
};

export type Job = {
  id: string;
  kb_id: string | null;
  kind: string;
  status: "queued" | "running" | "done" | "failed";
  input: Record<string, unknown>;
  state: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string | null;
  attempts: number;
  created_at: string;
  updated_at: string;
};

export type Diagnosis = {
  id: string;
  root_cause: string;
  target: string;
  summary: string;
  evidence: Record<string, unknown>;
  trace_ids: string[];
  impact: number;
  status: string;
  created_at: string;
  updated_at: string;
};

export type FixEval = {
  passed?: boolean;
  error?: string;
  targets?: { n: number; baseline: number; candidate: number };
  golden?: {
    baseline: { n: number; pass_rate?: number; judge_overall?: number };
    candidate: { n: number; pass_rate?: number; judge_overall?: number };
    comparison: { regression: boolean; drops?: Record<string, unknown> };
    regressed_items: string[];
  };
  golden_missing?: boolean;
  canary?: { canary: Record<string, number>; active: Record<string, number> };
};

export type Fix = {
  id: string;
  diagnosis_id: string | null;
  kind: string;
  params: Record<string, unknown>;
  risk: "low" | "medium" | "high";
  status: string;
  rationale: string;
  eval: FixEval;
  config_version: number | null;
  created_at: string;
  applied_at: string | null;
};

export type Source = {
  id: string;
  kind: string;
  uri: string;
  recursive: boolean;
  authority: number;
  status: string;
  last_error: string | null;
  last_crawled_at: string | null;
};

export type Doc = {
  id: string;
  uri: string;
  title: string;
  mime: string;
  status: string;
  tier: string;
  version: number;
  authority: number;
  quality: { score?: number; freshness?: number; parse_quality?: number; injection_chunks?: number; age_days?: number | null };
  token_count: number;
  chunk_strategy: Record<string, unknown>;
  modified_at: string | null;
  last_ingested_at: string | null;
  next_crawl_at: string | null;
  error: string | null;
  chunks: number | null;
};

export type ChunkRow = {
  id: string;
  ord: number;
  status: string;
  version: number;
  section: string;
  page: number | null;
  context: string;
  aliases: string[];
  flags: string[];
  tokens: number;
  text: string;
  embedding_model: string;
};

export type Golden = {
  id: string;
  question: string;
  expected_answer: string;
  expected_doc_ids: string[];
  origin: string;
  active: boolean;
  created_at: string;
};

export type EvalRun = { id: string; purpose: string; config_version: number | null; summary: Record<string, unknown>; created_at: string };

export type HealthMetrics = {
  window_days: number;
  queries: number;
  by_status: Record<string, number>;
  verified_rate: number | null;
  groundedness: number | null;
  contradiction_rate: number | null;
  coverage: number | null;
  abstention_rate: number | null;
  heal_rate: number | null;
  heal_success_rate: number | null;
  repeat_failure_rate: number | null;
  negative_signal_rate: number | null;
  latency_p50_ms: number;
  latency_p95_ms: number;
  avg_llm_calls: number | null;
  tokens: { in: number; out: number };
  mean_time_to_heal_s: number | null;
  series: { day: string; n: number; verified_rate: number; groundedness: number | null }[];
  corpus: { documents: Record<string, number>; chunks: number; tokens: number };
  repair: { open_diagnoses: number; needs_human: number; pending_approval: number; applied_fixes: number; golden_items: number };
  config: { active: number; canary: number | null; canary_pct: number | null };
};

export type ProviderStatus = {
  roles: Record<string, { chain: { provider: string; state: string; last_error: string }[]; notes: string[]; offline: boolean }>;
  verifier_independent: boolean;
  ollama: { url: string; running: boolean; missing: string[] } | null;
  context_tokens: Record<string, number>;
  embedder: { model: string; note: string };
  reranker: string;
  web_search: { provider: string; available: boolean };
};

export type Usage = {
  plan: string;
  questions_today: number;
  daily_limit: number | null;
  remaining: number | null;
  deep_today: number;
  deep_limit: number | null;
  per_minute: number | null;
  resets_at: string;
};

export type User = {
  id: string;
  email: string;
  name: string;
  role: "user" | "admin";
  plan: string;
  daily_limit_override: number | null;
  active: boolean;
  must_change_password: boolean;
  email_verified: boolean;
  created_at: string;
  last_login_at: string | null;
  last_seen_at: string | null;
  usage: Usage | null;
};

export type PlanLimits = { daily_questions: number; per_minute: number; deep_per_day: number; max_concurrent: number };

export type AccessPolicy = {
  signup_enabled: boolean;
  allowed_email_domains: string[];
  join_code: string;
  require_email_verification: boolean;
  default_plan: string;
  plans: Record<string, PlanLimits>;
  global_daily_questions: number;
};

export type AuthOptions = {
  signup_enabled: boolean;
  allowed_email_domains: string[];
  requires_join_code: boolean;
  email_enabled: boolean;
  require_email_verification: boolean;
};

export type UsageStats = {
  days: { day: string; questions: number; deep: number; active_users: number; tokens_in: number; tokens_out: number }[];
  top_users_today: { email: string; questions: number }[];
  today: { questions: number; global_limit: number | null };
  users: { total: number; active_today: number };
};
