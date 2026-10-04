# RAGX — Self-Healing RAG Platform Architecture

> Status: design draft v0.1 (2026-10-04). No code yet.
> Built by picking the strongest idea from **Google Search**, **Gemini grounding**, and **Claude's search/RAG systems** for each healing need.

---

## 0. The core idea in one paragraph

Most "self-healing RAG" systems only retry a bad answer. RAGX heals at **three speeds**:

| Loop | Speed | What it heals | Borrowed from |
|---|---|---|---|
| **Reflex loop** | milliseconds–seconds, inside one query | bad retrieval, unsupported claims, weak queries | Gemini (grounding gate + verifier), Claude (agentic re-search), CRAG |
| **Repair loop** | minutes–days, offline | the *corpus, index, chunking, prompts, thresholds* that caused failures | Google (Glue logs → NavBoost / retraining, rater-gated launches), Claude (self-improving prompts) |
| **Resilience loop** | always on | crashes, provider outages, bad deploys, stale indexes | Claude (checkpoints, rainbow deploys, tracing), Google (Caffeine incremental indexing) |

Every query produces a **trace**. Every failure in a trace becomes a **diagnosis**. Every diagnosis becomes a **proposed fix**. Every fix must **pass the eval gate** before it ships. That is the "self-healing" contract.

---

## 1. Picking the winner for each healing need

| # | Healing need | Google | Gemini | Claude | **Winner → what RAGX uses** | Why |
|---|---|---|---|---|---|---|
| 1 | Decide *whether / how much* to retrieve | – | Dynamic retrieval: 0–1 score vs threshold | Effort-scaling rules (1 agent / 3–10 calls … 10+ subagents) | **Gemini score + Claude effort tiers** | Gemini gives a cheap, tunable number; Claude gives the budget tiers that the number maps to |
| 2 | Understand & expand the query | Query fan-out (AI Mode) | Model writes its own search queries | Lead agent plans; broad → narrow | **Google fan-out** for breadth, **Claude broad→narrow** for depth | Fan-out is the best recall tool for multi-part questions; broad→narrow avoids overly specific dead queries |
| 3 | Make chunks retrievable | Precomputed per-doc quality signals | – | **Contextual Retrieval** (50–100-token context prefix per chunk) | **Claude Contextual Retrieval + Google per-doc quality scores** | Contextual Retrieval has measured gains (−35% → −49% failures); quality scores let later stages demote junk |
| 4 | Keep index fresh | **Caffeine** incremental indexing, recrawl scheduling | – | Claude Code: read live source, no index lag | **Google incremental indexing + Claude live-read fallback** | Incremental per-doc reindex for scale; live read for "the index might be stale" cases |
| 5 | Recall | **BM25 + neural matching (hybrid)** | Google Search under the hood | Contextual BM25 + embeddings; grep-style agentic search | **Hybrid (BM25 + dense) + agentic exact-match retriever** | Every one of the three systems ends up hybrid; agentic search catches IDs, code, names embeddings miss |
| 6 | Precision | **Ranking cascade** (cheap → expensive) | Vertex Ranking API (semantic reranker) | Rerank top-150 → top-20 | **Google cascade with a Claude/Vertex-style cross-encoder stage** | Cascades keep cost down; reranking gives the biggest single jump (→ −67% failures) |
| 7 | Business rules after ranking | **Twiddlers** (dedupe, diversity, demote, boost) | – | Source-quality heuristics (avoid SEO farms) | **Google Twiddlers** | Clean, separate layer for rules; easy to add healing actions (e.g., "quarantine this doc") without retraining |
| 8 | Judge retrieval quality | NavBoost (after the fact, from clicks) | – | Subagent self-evaluation with interleaved thinking | **CRAG-style retrieval grader** (+ Claude interleaved judgment in deep mode) | Needed *before* generation; CRAG is plug-in and needs no model training (unlike Self-RAG) |
| 9 | Judge the answer | Rater evaluation (offline) | **Check Grounding**: per-claim support score + contradiction score | CitationAgent | **Gemini/Vertex Check-Grounding design + Claude CitationAgent** | Per-claim entailment + contradiction is the most precise "is this answer true to the sources" sensor available |
| 10 | Citation format | – | **groundingSupports** (span → source chunk indices) | cited_text + url | **Gemini span-level citations, enriched with Claude's cited_text** | Spans let us verify, highlight and heal claim-by-claim |
| 11 | Hard questions | Deep Search (hundreds of queries) | Deep Research (async, plan → search → reason → report) | **Orchestrator–worker multi-agent** with external memory | **Claude orchestrator–worker, run async like Gemini Deep Research** | Claude's design is the best documented (+90% vs single agent); Gemini's async UX suits long jobs |
| 12 | Learn from users | **Glue logs → NavBoost; logs train RankEmbed** | – | – | **Google** | Only Google has a proven feedback-to-ranking loop at scale |
| 13 | Fix the system itself | Raters gate ranking launches | – | Claude rewrites its own prompts / tool descriptions | **Claude self-improvement, gated by Google-style eval-before-launch** | Automatic fixes are only safe if a regression gate blocks the bad ones |
| 14 | Fault tolerance | Incremental index, tiered storage | Async jobs survive app close | **Checkpoint/resume, rainbow deploys, full tracing** | **Claude** (+ Google tiered index) | Directly addresses agent-pipeline failure modes |
| 15 | Evaluation | Rater guidelines | – | **LLM-judge rubric** (factual, citation, completeness, source quality, efficiency) | **Claude rubric + Google rater-style golden set** | Automated scoring for speed, human-graded golden set for trust |

---

## 2. System overview

```
                        ┌───────────────────────────────────────────────┐
                        │                  RAGX CONTROL PLANE            │
                        │  Config registry · Thresholds · Prompt versions│
                        │  Index versions · Feature flags · Deploy rings │
                        └───────────────▲───────────────────▲───────────┘
                                        │ promotes           │ reads
 ┌──────────────┐   ┌──────────────────┴───┐        ┌──────┴────────────────┐
 │ SOURCES       │   │  INGESTION PLANE      │        │  QUERY PLANE           │
 │ files, wikis, │──▶│  Crawler/Connectors   │        │  (Reflex loop)          │
 │ DBs, web, APIs│   │  Parse → Chunk →      │        │  Gate → Plan → Retrieve │
 └──────────────┘   │  Contextualize → Score │        │  → Rank → Grade →       │
                    │  → Dual index (tiers)  │        │  Generate → Verify →    │
                    └──────────┬────────────┘        │  Heal → Answer          │
                               │                      └──────┬─────────────────┘
                               ▼                             │ every step emits
                    ┌────────────────────────┐               ▼
                    │  KNOWLEDGE STORES       │     ┌──────────────────────────┐
                    │  Doc registry · BM25 ·  │◀────│  TRACE & SIGNAL LOG       │
                    │  Vector · Hot/Warm/Cold │     │  ("Glue"): queries, chunks│
                    │  · Live-read adapters   │     │  scores, verdicts, user   │
                    └──────────▲─────────────┘     │  feedback, costs, latency │
                               │ fixes applied      └──────────┬───────────────┘
                    ┌──────────┴────────────────────────────────▼──────────────┐
                    │  REPAIR PLANE (slow loop)                                  │
                    │  Failure miner → Clusterer → Diagnoser → Fix proposer →    │
                    │  Eval gate (golden set + LLM judge) → Canary → Promote /   │
                    │  Rollback                                                  │
                    └────────────────────────────────────────────────────────────┘
          Resilience loop wraps everything: checkpoints · circuit breakers ·
          provider fallback · blue/green indexes · rainbow deploys · OpenTelemetry
```

---

## 3. Ingestion plane (borrowing Google's indexing + Claude's Contextual Retrieval)

### 3.1 Connectors and crawl scheduler (Googlebot-inspired)
- Each source has a connector (file system, Git, Confluence/Notion, SQL, web, S3...).
- **Recrawl priority** = f(change frequency observed, document importance, query demand, last failure). Documents that keep causing "stale content" diagnoses are recrawled more often. *This is itself a healing action.*
- Change detection by content hash; only changed docs are reprocessed (**Caffeine-style incremental indexing**, no full rebuilds).

### 3.2 Parse → structure
- Layout-aware parsing (headings, tables, code blocks, lists) into a **document tree**.
- Keep structure metadata on every chunk: `doc_id, section_path, page, heading chain, version, timestamp, ACL`.

### 3.3 Chunking (versioned strategy)
- Default: structure-aware chunks (~400–800 tokens), split on headings, small overlap.
- **Chunking strategy is a versioned config**, not hard-coded, so the Repair loop can re-chunk a single document or document type with a different strategy and A/B test it.

### 3.4 Contextualization (Claude Contextual Retrieval)
- For each chunk: small/cheap LLM gets *whole document (cached) + chunk*, writes a 50–100-token context: what document, what section, what entity the chunk is about.
- Stored separately from the raw chunk so it can be regenerated by the Repair loop (e.g., when a diagnosis says "chunk lacked entity context").
- **Alias/entity enrichment**: extract entities, acronyms and synonyms into a per-chunk keyword field; healing can add aliases later ("users call X 'Y'").

### 3.5 Document quality signals (Google precomputed signals)
Computed at index time, stored per doc/chunk:
- freshness (age vs. expected update cadence), authority (source tier set by admin), completeness, duplicate cluster id, contradiction flags, historical **helpfulness prior** (from the feedback log, NavBoost-style).

### 3.6 Dual indexing + tiers
- **Lexical index** (BM25) over `context + chunk + aliases`.
- **Dense index** over `context + chunk`.
- **Tiers** (Google index tiers): *Hot* (high-demand, high-quality, in-memory), *Warm* (default), *Cold* (archived/low-quality, searched only when earlier tiers fail). Healing can promote/demote.
- **Index versions**: every rebuild of a document or the embedding model produces a new versioned index; queries read through an alias, enabling blue/green swaps and instant rollback.

### 3.7 Small-corpus shortcut (Anthropic's advice)
If a tenant/collection is under ~200K tokens, skip retrieval: load it all into context with prompt caching. The gate (§4.1) routes these automatically.

---

## 4. Query plane — the Reflex loop

```
 query
   │
   ▼
[4.1 Gate & Router] ──(no retrieval)──────────────────────────────▶ [4.7 Generate]
   │ tier: FAST | STANDARD | DEEP
   ▼
[4.2 Planner / Fan-out] → N sub-queries (+ HyDE / keyword variants)
   │
   ▼
[4.3 Retrieve in parallel]  BM25 ∥ Dense ∥ Agentic exact-match ∥ Live source
   │
   ▼
[4.4 Fuse (RRF) → Cascade rerank 150 → 50 → 20]
   │
   ▼
[4.5 Twiddlers]  ACL · dedupe · diversity · freshness · quarantine · priors
   │
   ▼
[4.6 Retrieval Grader (CRAG)] ──INCORRECT/AMBIGUOUS──▶ [4.9 Heal ladder] ─┐
   │ CORRECT                                                              │
   ▼                                                                       │
[4.7 Generate with span citations]                                         │
   │                                                                       │
   ▼                                                                       │
[4.8 Verifier: claim split → support / contradiction / coverage]           │
   │ pass                          │ fail                                  │
   ▼                               └──────────▶ [4.9 Heal ladder] ◀────────┘
 answer + citations + confidence               (bounded by budget)
```

### 4.1 Gate & Router (Gemini dynamic retrieval + Claude effort scaling + Adaptive-RAG)
- A small classifier/LLM call outputs:
  - `retrieval_need ∈ [0,1]` (Gemini-style score)
  - `complexity ∈ {simple, multi-part, research}`
  - `freshness_need ∈ [0,1]` (does this need live data?)
- Routing:
  - `retrieval_need < τ_gate` (start 0.3, tunable) → answer directly (still verified for safety on high-risk domains).
  - **FAST**: 1 query, hybrid retrieve, rerank, generate. Budget ≈ 1–3 LLM calls.
  - **STANDARD**: fan-out 3–6 sub-queries, full pipeline with heal ladder. Budget ≈ 5–10 calls.
  - **DEEP**: hand to the Research orchestrator (§4.10), runs async.
- `τ_gate` and tier boundaries are **tuned by the Repair loop** from logged outcomes.

### 4.2 Planner / Fan-out (Google AI Mode + Claude broad→narrow)
- Decompose into sub-questions covering entities, constraints, comparisons.
- For each sub-question generate variants: natural-language (for dense), keyword form (for BM25), exact identifiers (for agentic search), optionally HyDE hypothetical-answer text.
- Start **broad**, narrow on later rounds.
- Session memory: follow-up questions reuse the previous plan and evidence (AI Mode keeps context across ~30 turns).

### 4.3 Retrievers, in parallel
1. **BM25** (contextual) — exact terms, rare words.
2. **Dense** (contextual embeddings) — meaning.
3. **Agentic exact-match** (Claude Code style) — grep/regex/SQL/structured filters for IDs, error codes, names, numbers. Runs against source of truth, so no index lag.
4. **Live source / web** (Gemini grounding / Claude web_search) — only when `freshness_need` is high or as CRAG fallback.
- Each retriever returns candidates + its own score + which sub-query produced it.

### 4.4 Fusion and cascade ranking (Google cascade, Claude 150→20)
- **Reciprocal Rank Fusion** across retrievers and sub-queries → top-150.
- Stage 1: cheap scoring (fused score + quality prior) → top-50.
- Stage 2: **cross-encoder reranker** (Cohere/Voyage/Vertex-style) → top-20.
- Optional Stage 3 (DEEP only): LLM listwise rerank.

### 4.5 Twiddlers (Google)
Small, independent, ordered rules. Each is config, can be added by the Repair loop:
- **ACL filter** (always first; never leak).
- **Dedupe** (same duplicate-cluster id → keep best).
- **Diversity** (max K chunks per document/source for multi-part queries).
- **Freshness boost** when `freshness_need` high.
- **Quarantine demote** for docs flagged stale/contradictory/injection-suspicious.
- **Helpfulness prior** (NavBoost-like): boost/demote chunks by historical success for this *query cluster*.

### 4.6 Retrieval grader (CRAG)
- Lightweight grader (small model or LLM) scores each top chunk against each sub-question: `CORRECT / AMBIGUOUS / INCORRECT`, plus **coverage**: which sub-questions have at least one CORRECT chunk.
- Decision:
  - All sub-questions covered → generate.
  - Some uncovered → heal *only those sub-questions* (targeted, cheaper).
  - Nothing correct → heal ladder from step 2.
- **Knowledge refinement** (CRAG decompose-recompose): strip irrelevant sentences from chunks before generation to reduce noise.

### 4.7 Generation with span citations (Gemini groundingSupports format)
Output contract (the generator must produce this structure):
- `answer_text`
- `supports[]`: `{start, end, chunk_ids[], cited_text}`
- `unsupported_spans[]` the model itself admits it could not ground
- `abstentions[]`: sub-questions it could not answer
Prompt rules: answer only from evidence, cite every factual sentence, say "not found" explicitly. Retrieved text is treated as **data, never instructions** (prompt-injection defense).

### 4.8 Verifier (Gemini Check Grounding + Claude CitationAgent)
1. **Claim decomposition**: split answer into atomic claims.
2. **Support score** per claim: is it *fully* entailed by its cited chunks? (partial = unsupported, same strict rule as Check Grounding).
3. **Contradiction score** per claim (anti-citations): does any retrieved chunk contradict it?
4. **Citation accuracy**: does the cited chunk actually contain the claim (CitationAgent role)? Fix mis-pointed citations by searching the evidence set.
5. **Coverage/helpfulness**: does the answer address every sub-question?
- Aggregate → `groundedness ∈ [0,1]`, `contradiction ∈ [0,1]`, `coverage ∈ [0,1]`.
- Pass if `groundedness ≥ τ_ground` (start 0.9), `contradiction ≤ τ_contra` (start 0.1), `coverage ≥ τ_cov`.
- Use a **different model (or at least different prompt) from the generator** to avoid self-agreement bias.

### 4.9 The Heal Ladder (query-time healing actions)
Ordered cheapest → most expensive. Each step is targeted at the *failing sub-question or claim*, not the whole query. Stops when verified or the budget runs out.

| Step | Trigger | Action | Source of idea |
|---|---|---|---|
| H1 | unsupported claim but evidence exists | **Repair answer**: rewrite/remove the claim, re-cite | Check Grounding + CitationAgent |
| H2 | sub-question uncovered | **Rewrite query** (broaden, synonyms, decompose further) | Claude broad→narrow |
| H3 | still uncovered | **Switch/add retriever**: agentic exact-match, cold tier, raise top-K | Claude Code agentic search, Google tiers |
| H4 | corpus likely missing / stale | **Live-source / web fallback** (if allowed for this tenant) | CRAG fallback, Gemini grounding |
| H5 | contradictions between sources | **Conflict resolution**: prefer fresher + higher-authority, *and* show the conflict to the user | Google quality signals |
| H6 | multi-hop / complex | **Escalate to DEEP** orchestrator | Claude multi-agent |
| H7 | budget exhausted | **Honest partial answer**: answer the verified parts, list what could not be verified | Self-RAG IsSup idea |

Every step writes to the trace: what failed, what was tried, whether it worked. **These records are the training data for the Repair loop.**

Budgets: max heal rounds (e.g., 2 for FAST, 4 for STANDARD), token budget, latency budget. Prevents Claude's documented failure "endless searching for nonexistent sources".

### 4.10 DEEP mode — Research orchestrator (Claude multi-agent, Gemini async UX)
- **Lead agent** (strong model, extended thinking): writes a research plan to **external memory** (persisted), decides number of workers by effort-scaling rules.
- **Worker agents** (cheaper model), 3–5 in parallel, each with: objective, output format, allowed tools, stop condition, budget. Each runs its own mini Reflex loop (retrieve → grade → re-search) with interleaved reasoning.
- Workers return **condensed findings + evidence references** (not raw text — avoids "game of telephone").
- Lead synthesizes, checks coverage, spawns follow-up workers if gaps remain.
- **Citation agent** + Verifier pass on the final report.
- Runs as a **durable async job**: checkpoint after every step, resumable after crash, user notified when done (Gemini Deep Research UX).
- Guardrails from Claude's failure list: cap on workers per complexity, de-duplicated task assignment, source-quality preferences, broad-first queries.

---

## 5. Trace & Signal Log ("Glue")

Every query writes one structured trace (OpenTelemetry spans + event records):

- query, normalized query, **query cluster id** (embedding-clustered)
- gate scores, tier, plan, sub-queries
- per retriever: candidates, scores; fusion & rerank positions; twiddlers applied
- grader verdicts per chunk; verifier per claim (support, contradiction, citation ok)
- heal steps taken and their outcome
- final answer, confidence, cost, latency, model versions, **config/prompt/index versions**
- **Implicit user signals** (NavBoost analogs):
  - *good*: citation clicked & no re-ask, copy, thumbs-up, conversation ends satisfied
  - *bad*: immediate rephrase of same question (= Google "bad click / pogo-stick"), thumbs-down, "that's wrong" follow-up
- Explicit feedback with optional correction text.

Privacy: PII redaction at write, per-tenant retention, signals aggregated per query-cluster for ranking priors.

---

## 6. Repair plane — the slow, system-healing loop

```
 Trace log ─▶ [6.1 Failure miner] ─▶ [6.2 Clusterer] ─▶ [6.3 Diagnoser]
                                                          │ root cause
                                                          ▼
       [6.7 Monitor & rollback] ◀─ [6.6 Canary] ◀─ [6.5 Eval gate] ◀─ [6.4 Fix proposer]
```

### 6.1 Failure miner
Selects traces where: verifier failed, heal ladder exhausted, abstained, negative user signal, high cost/latency, or a later trace contradicts an earlier answer.

### 6.2 Clusterer
Groups failures by query cluster, document, source, failure type — one fix should solve many failures. Ranked by **impact = frequency × severity**.

### 6.3 Diagnoser — root-cause taxonomy
An LLM diagnoser replays the trace (with counterfactual probes) and assigns a cause:

| Root cause | Evidence pattern in trace | Typical fix |
|---|---|---|
| **Missing content** | no relevant chunk in any tier or retriever; live source had the answer | Flag content gap to owners; add source/connector; auto-ingest the live source if permitted |
| **Stale content** | chunk retrieved but contradicted by fresher source / user correction | Recrawl, raise recrawl priority, mark old version superseded |
| **Bad chunking** | answer split across chunks; chunk cut mid-table/section | Re-chunk that doc/doc type with another strategy |
| **Missing context** | right chunk exists but ranked low; chunk text lacks entity names | Regenerate contextual prefix; add aliases |
| **Vocabulary gap** | user term ≠ doc term; BM25 misses, dense weak | Add synonyms/aliases to keyword field; query-rewrite rule |
| **Ranking miss** | correct chunk in top-150 but not top-20 | Adjust fusion weights, add helpfulness prior, reranker fine-tune data |
| **Twiddler error** | correct chunk removed by dedupe/diversity/quarantine | Fix rule config |
| **Conflicting sources** | contradiction score high across chunks | Mark authority, deprecate one, surface to owner |
| **Generation error** | evidence correct but claim unsupported | Prompt fix, stricter citation rule, model change |
| **Gate error** | answered without retrieval and was wrong (or retrieved when not needed) | Tune `τ_gate`, add examples to gate |
| **Injection / poisoned doc** | retrieved text tried to instruct the model | Quarantine doc, alert |

### 6.4 Fix proposer
Produces a **Fix Proposal**: what to change, scope (one doc / doc type / global), expected affected queries, risk level. Fixes are *config & data changes*, never arbitrary code changes:
- re-chunk / re-contextualize / add aliases / reindex doc set
- recrawl schedule change, supersede document version
- twiddler rule add/edit, fusion weight change, threshold change
- prompt version change (Claude-style: an LLM rewrites prompt/tool descriptions using the failure examples)
- add the failing query (with correct answer if known) to the **golden set**
- training pairs for future reranker/embedding fine-tuning (RankEmbed-style, phase 3)

### 6.5 Eval gate (Google "rater-gated launches" + Claude LLM-judge)
- **Golden set**: curated questions with expected answers/evidence (starts ~20–50, grows automatically from fixed production failures — every healed failure becomes a regression test).
- Run the candidate config on: (a) the failure cluster it targets, (b) the full golden set.
- Score with LLM-judge rubric: factual accuracy, citation accuracy, completeness, source quality, efficiency (cost/latency) + retrieval metrics (recall@20, MRR) where labels exist.
- **Accept only if**: target failures improve AND no golden-set regression beyond tolerance.
- Risk policy: low-risk (alias add, single-doc rechunk) → auto-apply; medium (thresholds, twiddlers) → auto-apply with canary; high (prompt/global/model changes, deleting content) → **human approval in dashboard**.

### 6.6 Canary / rainbow rollout (Claude)
- New config/index version serves a small % of traffic; old version keeps serving in-flight and deep jobs.
- Compare live metrics: groundedness, heal rate, negative signals, cost.

### 6.7 Monitor & auto-rollback
- Rollback automatically if metrics degrade; every change is versioned and reversible (index aliases, config registry).
- Healing has an **audit log**: who/what changed, why (linked diagnosis + traces), eval results.

### 6.8 Learning-to-rank (phase 3, Google RankEmbed/NavBoost)
Once enough signals exist: fine-tune reranker/embeddings on (query, good chunk, bad chunk) triples from traces + golden set. Gated by the same eval process.

---

## 7. Resilience loop (infrastructure healing)

- **Durable workflows**: every multi-step pipeline (ingestion jobs, DEEP research, repair jobs) runs on a durable workflow engine with step checkpoints → resume, not restart.
- **Provider fallback**: LLM, embedding, reranker and web-search providers behind an adapter layer; circuit breakers + fallback chains (e.g., primary reranker down → skip stage 2 and mark answers lower confidence).
- **Graceful degradation**: if dense index down → BM25 + agentic; if verifier down → return answer marked "unverified".
- **Blue/green indexes**: index rebuilds never touch the live index; swap by alias.
- **Embedding drift guard**: changing embedding model = new index version + eval gate, never in-place.
- **Health checks** on connectors (auth expired, source removed, schema changed) → self-repair attempt (retry, re-auth notice) → alert.
- **Observability**: OpenTelemetry traces, metrics, logs; per-stage latency/cost dashboards.

---

## 8. Data stores

| Store | Holds | Suggested tech (to be confirmed) |
|---|---|---|
| Object store | raw documents, parsed trees, snapshots | local disk / S3-compatible (MinIO) |
| Document registry | docs, versions, chunks, context prefixes, quality signals, ACLs, tiers | PostgreSQL |
| Lexical index | BM25 over context+chunk+aliases | OpenSearch / Tantivy / Postgres FTS (small scale) |
| Vector index | contextual embeddings, versioned | Qdrant / pgvector |
| Trace & signal log | traces, feedback, heal records | Postgres (+ ClickHouse at scale) |
| Eval store | golden set, eval runs, judge scores | PostgreSQL |
| Control plane | configs, prompts, thresholds, versions, deploy rings, audit | PostgreSQL |
| Agent memory | DEEP plans, worker findings, checkpoints | workflow engine state + Postgres |
| Cache | embedding cache, prompt cache, query cache | Redis |

---

## 9. Key metrics (SLOs the platform optimizes)

- **Retrieval failure rate** @20 (golden set) — Anthropic's headline metric
- **Groundedness** (avg support score), **contradiction rate**
- **Answer coverage**, **abstention rate** (healthy, not zero)
- **Heal success rate** = failures fixed inside the Reflex loop / failures detected
- **Repeat-failure rate** = same query-cluster failing again after a Repair fix (should trend to 0)
- **Mean time to heal** (corpus issue detected → fix promoted)
- **Negative signal rate** (rephrase-after-answer, thumbs-down)
- Cost per answer, p50/p95 latency per tier

---

## 10. Security & trust

- ACL enforced as the first twiddler and inside retrievers (pre-filter), never after generation.
- Retrieved content is data, never instructions; injection detector flags and quarantines docs.
- Auto-healing can't delete source content; at most it can quarantine or supersede, and destructive actions need a human.
- Every healed change is auditable and reversible.
- Tenant isolation for indexes, traces, and learned priors.

---

## 11. CLI or website? → **Both, on one API core. The website is the main product.**

**Recommendation**: build the engine as an **API service** (`ragx-core`). Then:
1. **Web dashboard (primary)**
2. **CLI (secondary, thin client of the same API)**

Why the website must be primary for a *self-healing* platform:
- Healing is only trusted if people can **see** it: trace viewer (every stage, every score), heal ladder replay, failure clusters, diagnoses, fix proposals with before/after eval diffs. That's visual work a terminal does badly.
- **Human approval** of high-risk fixes needs a review UI (approve / reject / edit).
- Non-developers (content owners) must receive "content gap" and "stale document" tickets.
- Live metrics (groundedness trends, heal success) are dashboards.
- Chat-with-citations UX (highlight cited spans, show conflicts) is a web experience.

Why still have a CLI:
- Developers: `ingest`, `reindex`, `eval run`, `golden add`, `heal propose/apply`, `trace show` scripted locally.
- **CI/CD**: run the eval gate in pipelines; fail a build if groundedness regresses.
- Headless/server use and automation.

Order of build: core engine + CLI first (fastest way to test the pipeline), web dashboard immediately after — the CLI is the dev tool, the website is the product.

Web dashboard pages:
1. **Ask** — chat with span-highlighted citations, confidence, "verified/partial" badges, conflict callouts
2. **Trace explorer** — waterfall of every stage, scores, heal steps
3. **Health** — SLO dashboard, trends, alerts
4. **Failures & diagnoses** — clusters ranked by impact, root cause, linked traces
5. **Fix proposals** — diff, eval results, approve/reject, canary status, rollback button
6. **Knowledge** — sources, connectors health, freshness, tiers, quarantined docs, content-gap tickets
7. **Evals** — golden set management, run history, rubric scores
8. **Settings** — providers, thresholds, budgets, risk policy, ACLs

---

## 12. Build roadmap

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0 – Foundations** | Ingestion (parse, chunk, contextualize), hybrid index, RRF + reranker, generation with span citations, trace log, CLI, golden set v0 (~30 Qs), LLM-judge eval | Baseline retrieval failure rate & groundedness measured |
| **P1 – Reflex loop** | Gate/router, fan-out, CRAG grader, Verifier (support + contradiction), heal ladder H1–H5, H7, budgets | Groundedness ↑, heal success rate tracked |
| **P2 – Web dashboard** | Ask, Trace explorer, Health, Knowledge pages; feedback capture | Every answer inspectable end-to-end |
| **P3 – Repair loop** | Failure miner, clusterer, diagnoser, fix proposer, eval gate, canary, rollback, approvals UI | First fixes auto-applied with no golden-set regression |
| **P4 – DEEP mode** | Orchestrator–worker, external memory, durable async jobs, citation agent | Complex multi-hop queries answered with full verification |
| **P5 – Learning** | Helpfulness priors (NavBoost-like), threshold auto-tuning, reranker/embedding fine-tuning from logs | Repeat-failure rate trending to ~0 |

---

## 13. Open decisions (need answers before P0)

1. **Corpus**: what data, what formats, how big, how often it changes? (<200K tokens → long-context shortcut)
2. **LLM provider(s)**: Claude, Gemini, local, or mixed? (Verifier ideally a different model/prompt from generator)
3. **Web fallback allowed?** (private-data deployments may forbid it)
4. **Language/stack**: Python backend + TypeScript web is the default suggestion
5. **Deployment**: local single-machine, Docker, or cloud? Single or multi-tenant?
6. **Risk policy**: which fix types may auto-apply without a human?

---

## Sources

- Anthropic — [Multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system), [Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval), [Web fetch tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool)
- Google/Gemini — [Grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search), [Dynamic retrieval](https://dejan.ai/blog/how-google-decides-when-to-use-gemini-grounding-for-user-queries/), [Check Grounding API](https://docs.cloud.google.com/generative-ai-app-builder/docs/check-grounding), [Vertex Ranking API](https://cloud.google.com/blog/products/ai-machine-learning/launching-our-new-state-of-the-art-vertex-ai-ranking-api), [Deep Research](https://gemini.google/overview/deep-research/), [AI Mode fan-out](https://www.mariehaynes.com/ai-mode-query-fan-out/)
- Google Search — [DOJ ranking-signal testimony](https://searchengineland.com/google-abc-ranking-signals-455360), [RankEmbed/Glue](https://ppc.land/googles-rankembed-system-forced-to-share-secrets-in-antitrust-remedy/), [Twiddlers (leak analysis)](https://www.resoneo.com/google-leak-part-2-understanding-the-twiddler-framework/), [NavBoost](https://www.hobo-web.co.uk/navboost-how-google-uses-large-scale-user-interaction-data-to-rank-websites/)
- Research — [CRAG](https://arxiv.org/pdf/2401.15884), [Self-RAG overview](https://beancount.io/bean-labs/research-logs/2026/05/09/self-rag-learning-to-retrieve-generate-critique-self-reflection)
