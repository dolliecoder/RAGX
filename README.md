# RAGX: the self-healing RAG platform

**Ask questions about your documents. Get answers you can check. Watch the system fix its own mistakes.**

RAGX is a free, open-source question-answering platform for your own documents (PDF, Word, HTML, Markdown, text). Unlike a plain chatbot over files, it:

- **Shows its sources.** Every sentence links to the exact passage it came from.
- **Checks itself.** A second, independent model verifies every claim. Unsupported claims are removed, and if the answer isn't in your documents it says so instead of guessing.
- **Heals while answering.** When the first search misses, it rewrites the query, looks further, and tries exact matches before giving up.
- **Repairs itself over time.** In the background it works out *why* a question failed and proposes a fix. It tests the fix before applying it, and rolls it back if things get worse. Every fixed failure becomes a permanent regression test.
- **Runs on free models.** Gemini's free tier, Groq, OpenRouter's free models, or fully local open-source models with Ollama. No credit card needed.
- **Is ready to share.** People sign up with email or **Continue with Google**. You get per-person daily limits, optional invite codes or domain limits, and a private question history for each person. Password reset and email confirmation work with any free email service.
- **Deploys in one command.** HTTPS, nightly backups and a step-by-step guide, including a free-forever server option. See [DEPLOY.md](DEPLOY.md).

> **Status: early (v0.1).** The engine, self-repair loop, accounts and dashboard work and are covered by 60+ automated tests. It has not yet been used at scale; expect rough edges, and please report them.

## Run it in 5 minutes

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/).

1. **Get the code and the settings file:**

   ```bash
   git clone https://github.com/dolliecoder/RAGX.git
   ```

   ```bash
   cd RAGX && cp .env.example .env
   ```

2. **Pick a free model setup** (details below). The quickest is to paste a free Gemini key from [aistudio.google.com](https://aistudio.google.com) into `.env`:
   ```
   RAGX_GEMINI_API_KEY=your-key
   ```
   Also put your email in `RAGX_ADMIN_EMAILS=` so your account becomes the admin.

3. **Put your documents in a `docs` folder** inside `RAGX`.

4. **Start it:**

   ```bash
   docker compose up -d --build
   ```

5. **Open http://localhost:3000**, sign up with your admin email, and go to **Knowledge**. Create a knowledge base, add the source `/docs`, then ask a question on **Ask**.

No key yet? It still starts, in **offline mode**. Everything works, but answers are simple extracts.

**Put it online:** [DEPLOY.md](DEPLOY.md) walks through HTTPS, backups and a free server, step by step.

## Free model setups

RAGX uses three model roles, plus embeddings for search:
- a **generator** that writes answers,
- an **independent verifier** that fact-checks them,
- a **utility** model for small helper steps.

All three setups below are free. Choose in `.env`; [.env.example](.env.example) has each one ready to copy.

| Setup | What you need | Speed | Limits | Privacy |
|---|---|---|---|---|
| **A. Gemini free tier** (default) | Free key from aistudio.google.com | ~5–15 s per answer | Daily request limits per model (lite models are the most generous) | Google may use free-tier requests to improve its products |
| **B. Local open models (Ollama)** | [Ollama](https://ollama.com) + ~8 GB free RAM | 30–90 s on a laptop CPU; fast with a GPU | None | Nothing leaves your machine |
| **C. Mix free clouds** | Free keys from [Groq](https://console.groq.com) / [OpenRouter](https://openrouter.ai) (`:free` models) + Gemini | Fast | Each provider's free limits; RAGX fails over between them | Per provider |

For setup B, download the models once:

```bash
ollama pull qwen3:4b
```

```bash
ollama pull gemma3:4b
```

```bash
ollama pull nomic-embed-text
```

Then use the `ollama:` lines from `.env.example`. Docker reaches Ollama on your computer automatically; on a server you can run the bundled container instead with `docker compose --profile ollama up -d`.

Model names change every few months. To see which models your key or Ollama install can use right now:

```bash
ragx models gemini
```

(also `groq`, `openrouter` and `ollama`). In the dashboard, **Settings → Model providers** shows what is connected, which local models still need downloading, and each model's context size. RAGX adapts to small local context windows automatically.

Paid providers (Anthropic, OpenAI, LM Studio and vLLM through an OpenAI-compatible URL) are supported too, but never required.

## For a class: accounts and limits

- **Become admin:** put your email in `RAGX_ADMIN_EMAILS`, then sign up with it. Everyone else becomes a student.
- **Invite students:**
  1. Go to **Users → Access & limits**.
  2. Set your college email domain.
  3. Click **Generate** for a join code, then **copy link**.
  4. Share the link with your class.

  Emails aren't verified yet, so the join code is the real gate. Change it every term.
- **Limits per plan** (`free`, `pro`):
  - questions per day, per minute, and at once
  - deep-research runs per day
  - per-student overrides
  - an optional **site-wide daily cap**, so a shared free quota can't be drained by one person

  Admins are never limited, and questions that fail because of a provider outage aren't counted.
- **Privacy:** students see only their own questions. Student "this is wrong" corrections wait for admin review before they're used as tests.
- **Security:** scrypt password hashes, HttpOnly session cookies, CSRF protection, lockout after 5 failed sign-ins, an audit log of every admin action, and safe defaults for folder sources. See [SECURITY.md](SECURITY.md).
- **Email (optional):** set the `RAGX_SMTP_*` values in `.env`, using Gmail with an app password, Brevo or Resend free tiers. This turns on:
  - "forgot password" links
  - invites, where people choose their own password from a link
  - optional required email confirmation, which makes the email-domain rule trustworthy
- **Locked out?** Run this on the server:

  ```bash
  docker compose exec api ragx admin reset-password you@college.edu
  ```

## How it works

The full design and the reasoning behind it are in [ARCHITECTURE.md](ARCHITECTURE.md). In short, RAGX combines published ideas from web search, Gemini grounding and Anthropic's Contextual Retrieval:

```
        ┌──────── Reflex loop (per question, seconds) ───────────────────────────────────────┐
question → gate → plan / fan-out → keyword ∥ semantic ∥ exact search → fuse → rerank → rules → grade
        → [heal: look further ▸ rewrite ▸ archived docs + exact match ▸ neighbouring passages ▸ web]
        → write answer with citations → independent verifier
        → [heal: fix citations ▸ drop unsupported claims ▸ surface conflicts ▸ search again]
        → verified | partial (+ background deep research) | honest "not found"
        └────────────────────────────────────────────────────────────────────────────────────┘
   every step → trace + user signals (thumbs, re-asks, copies, citation clicks)
        ┌──────── Repair loop (background, minutes) ─────────────────────────────────────────┐
find failures → diagnose root cause (12 types) → propose a data or config fix
  → test it on the failing questions + regression set
  → low risk: apply · medium: trial on part of the traffic, then keep or undo · high: ask an admin
  → every fixed failure becomes a regression test · every change is versioned and reversible
        └────────────────────────────────────────────────────────────────────────────────────┘
```

## Dashboard and command line

The **web dashboard** has these pages:
- **Ask**
- **Health** (quality metrics)
- **Traces** (every step of every answer)
- **Repair** (diagnoses and fixes to approve)
- **Knowledge** (documents and sources)
- **Evals** (regression tests)
- **Users**
- **Settings**

The **CLI** (`pip install -e backend`) talks to the same API:

```bash
ragx login
ragx kb create handbook
ragx source add handbook ./docs              # folder, file or https:// URL
ragx ask handbook "How many vacation days do new employees get?" --show-trace
ragx repair run handbook                     # diagnose and fix now
ragx eval run handbook --min-pass 0.85       # non-zero exit below threshold: use as a CI gate
ragx models groq                             # free models you can use
ragx users list
```

## Develop

```bash
cd backend && python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

The backend tests are offline and deterministic, so no API keys are needed:

```bash
pytest
```

```bash
cd web && npm install && npm run dev
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Adding another free model provider is a great first contribution.

### Project layout

```
backend/ragx/
  api.py, api_accounts.py, cli.py   HTTP API, accounts API, CLI
  auth.py, limits.py, deps.py       accounts, sessions, roles, usage limits
  ingestion/                        parsers, chunker, contextualizer, crawler, document versions
  retrieval/                        keyword + semantic index, exact match, web, fusion, rerankers, rules
  reflex/                           query-time healing pipeline, deep research
  repair/                           diagnoser, fix proposer, eval gate, trial/rollback
  llm/                              free + paid providers, fallback chains, offline model, embeddings
web/                                Next.js dashboard
```

## Known limits

- Without email settings, there is no self-service password reset; admins reset passwords from the Users page.
- Semantic search runs in memory, which is fine up to a few hundred thousand passages; beyond that, add a pgvector HNSW index.
- One organisation per install (no multi-tenancy yet).
- Free tiers change their limits and model names often; `ragx models` and **Settings** help you keep up.

## License

RAGX is free software under the **[GNU AGPL-3.0](LICENSE)** (or any later version). In plain words:
- You may use, study, change and share it, including commercially.
- If you **run a modified version as a service for others**, you must offer those users the source code of your version.

This keeps improvements open for everyone.
