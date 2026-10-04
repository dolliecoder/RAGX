# RAGX: self-healing RAG platform

RAGX answers questions from your documents with **verified, cited answers**, and **repairs itself**. It heals a bad answer while the query is running, and it fixes the root cause (index, chunks, vocabulary, config, prompts) offline so the same failure does not happen again.

The design and the reasoning behind every choice are in [ARCHITECTURE.md](ARCHITECTURE.md). In one line, it combines ideas from three systems:

- **Google Search:** hybrid BM25 + dense retrieval, a ranking cascade, twiddlers, NavBoost-style click signals, incremental indexing, and changes tested before launch.
- **Gemini grounding:** a gate that decides whether to retrieve, span-level citations, and a per-claim grounding check with support and contradiction scores.
- **Claude Research / Contextual Retrieval:** contextualized chunks, rerank 150 → 20, orchestrator-worker deep research, a citation pass, checkpoints and gradual rollouts.

```
        ┌──────── Reflex loop (per query, seconds) ────────────────────────────────────────┐
query → gate → plan/fan-out → BM25 ∥ dense ∥ exact → RRF → cascade rerank → twiddlers → CRAG grade
        → [heal: widen ▸ rewrite ▸ cold tier + exact ▸ neighbours ▸ web] → generate with span citations
        → independent verifier → [heal: repair citations ▸ drop unsupported ▸ conflict mode ▸ re-retrieve]
        → verified | partial (+ async deep research) | honest "not found"
        └──────────────────────────────────────────────────────────────────────────────────┘
   every step → trace ("Glue" log) + user signals (thumbs, re-asks, copies, citation clicks)
        ┌──────── Repair loop (background, minutes) ──────────────────────────────────────┐
mine failures → diagnose root cause (12 types) → propose config/data fix → eval gate (targets + golden set)
  → low risk: apply · medium: canary → promote/rollback on live metrics · high: human approval
  → every fixed failure becomes a regression test · every change is versioned and reversible
        └─────────────────────────────────────────────────────────────────────────────────┘
   Resilience loop: provider fallback chains + circuit breakers, graceful degradation, durable jobs
   with checkpoints, blue/green document versions, rollback of config versions
```

## Quick start (Docker)

```bash
cp .env.example .env
```

Edit `.env`. Add API keys for the providers you use, and set `RAGX_DOCS_DIR` to a folder of documents. Then:

```bash
docker compose up -d --build
```

Open http://localhost:3000. Create a knowledge base on **Knowledge**, then add the source path `/docs` (your mounted folder) or upload files, then ask questions on **Ask**.

Without any API keys RAGX runs in **offline mode**. Deterministic heuristics stand in for the models, so the whole platform works and the UI shows an "offline provider" badge, but answers are only extractive. Add keys to get real quality.

## Local development

Backend (Python 3.11+):

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate      # Windows; on Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"            # add ",postgres" to use PostgreSQL (psycopg wheels need Python <= 3.13 on Windows)
ragx serve                          # API on http://localhost:8000 (SQLite by default)
pytest                              # 31 tests, offline and deterministic
```

To run the test suite against PostgreSQL + pgvector, set `RAGX_TEST_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db`.

Web dashboard (Node 20+):

```bash
cd web
npm install
npm run dev                         # http://localhost:3000; set RAGX_API_URL if the API is not on :8000
```

## CLI

The CLI is a thin client of the API (`RAGX_API_URL`, `RAGX_API_KEY`):

```bash
ragx kb create handbook
ragx source add handbook ./docs                  # folder, file or https:// URL; crawled and indexed
ragx upload handbook policy.pdf faq.docx
ragx ask handbook "How many vacation days do new employees get?" --show-trace
ragx feedback <trace_id> --rating -1 --correction "25 days"
ragx repair run handbook                         # diagnose, propose, eval-gate and apply fixes now
ragx repair list handbook
ragx fix approve <fix_id>                        # high-risk fixes wait for a human
ragx golden import handbook tests.jsonl
ragx eval run handbook --min-pass 0.85           # exits non-zero below threshold, so it works as a CI gate
ragx config set gate_threshold 0.25
ragx providers                                   # chains, circuit breakers, offline warnings
```

## Configuration

**Process settings** are environment variables, all prefixed `RAGX_`. See [.env.example](.env.example).

- **Model roles** use the form `provider:model`, with providers `anthropic | openai | gemini | fake`:
  - `GENERATOR` writes answers. The default is `anthropic:claude-opus-5-5`.
  - `VERIFIER` is the independent grounding checker. It should be a *different* provider or model; the default is `gemini:gemini-2.5-pro`.
  - `UTILITY` handles the gate, planner, grader, reranker and contextualizer. The default is `anthropic:claude-haiku-4-5`.
  - `JUDGE` scores evals and defaults to the verifier.
  - Each role can have `*_FALLBACKS`, a comma-separated chain that is used when a provider fails (circuit breaker).

  Model names change. Set the ones your accounts have access to, and check them with `ragx providers`.
- **Retrieval:** `EMBEDDER` is `openai | gemini | voyage | hash`, and `RERANKER` is `cohere | voyage | llm:utility | lexical`.
- **Web fallback:** `WEB_SEARCH_PROVIDER` is `tavily | brave`. It is used only if `web_fallback_enabled` is true in the runtime config, and that is off by default.
- **Security:**
  - `API_KEY` makes every API call require an `X-API-Key` header. The web proxy adds it server-side, so the key never reaches the browser.
  - `SOURCE_ROOTS` restricts folder and file sources to the listed paths. Docker sets it to `/docs,/app/data`.

**Runtime config** is versioned in the database and editable in **Settings**. It holds everything the Repair loop may tune: the gate threshold, tier budgets, retrieval sizes (`retrieve_k=150 → stage1_k=50 → final_k=20`), fusion weights, twiddler rules (`doc_boosts`, `max_chunks_per_doc`), `query_aliases`, verification thresholds (`min_groundedness=0.9`, `max_contradiction=0.1`, `min_coverage=0.8`), canary settings and prompt add-ons.

## How self-healing works in practice

1. **During a query**, if the right evidence is missing, the Reflex loop tries a fixed ladder:
   - the next-ranked candidates,
   - query rewrites,
   - the cold tier and exact identifier search,
   - chunks adjacent to the evidence,
   - the web, only if enabled.

   Once evidence is found, the verifier removes claims it cannot support, and the answer reports anything still unanswered. Research-level questions are sent to a background deep-research job.
2. **In the background**, the Repair loop reads those traces and diagnoses *why* the first pass failed. For example, a heal that succeeded via a query rewrite means users and the documents use different words. It then proposes a fix, such as adding the users' phrasing to that chunk's index context in a staged copy of the document.
3. **Every fix goes through the eval gate.** It is run against the failing queries and the golden set, on both the current and the candidate configuration. It must improve the targets without regressing the golden set.
4. **Risk policy:**
   - Low-risk fixes are applied automatically.
   - Medium-risk changes run as a canary and are promoted or rolled back automatically from live metrics.
   - High-risk changes (prompt edits, quarantining documents, content gaps) wait on **Repair** for your approval.
5. **Every applied fix** becomes a golden regression test and can be rolled back with one click.

## Project layout

```
backend/ragx/
  api.py, cli.py            HTTP API and CLI
  config.py, control.py     settings, versioned runtime config, canary routing, audit
  ingestion/                parsers (pdf/docx/html/md/txt), chunker, contextualizer, crawler, versions
  retrieval/                BM25 + dense index, exact match, web, RRF, rerankers, twiddlers
  reflex/engine.py, deep.py query-time healing pipeline; orchestrator-worker deep research
  repair/                   failure miner + diagnoser; proposer, eval gate, apply/canary/rollback
  evals.py, metrics.py      golden-set runner + LLM judge; health SLOs
  llm/                      provider adapters, fallback chains, offline provider, embeddings
  jobs.py, tasks.py         durable checkpointed jobs, scheduler loops
web/                        Next.js dashboard: Ask, Health, Traces, Repair, Knowledge, Evals, Settings
```

## Limits and next steps

- Dense search runs in memory over pgvector-stored embeddings. That is comfortable up to around a few hundred thousand chunks; beyond that, move to a pgvector HNSW index or Qdrant.
- Fine-tuning learned rankers on interaction logs (architecture phase P5) is not implemented. Helpfulness priors from feedback are.
- Single-tenant only. Multi-tenant isolation (per-tenant indexes, traces and priors) is the next step for cloud deployment.
