# Contributing to RAGX

Thanks for helping. RAGX is a self-healing RAG platform: it answers questions from documents, verifies every claim, and repairs its own failures. Contributions of every size are welcome: bug reports, docs, tests, new free-model providers, UI work.

## Ground rules

- **Free first.** RAGX must stay fully usable with free models (Gemini free tier, Groq, OpenRouter `:free` models, local Ollama). Paid providers are optional extras, never requirements.
- **Measure, don't guess.** Changes to retrieval, prompts or healing should come with a test or an eval result showing they help.
- **Keep the safety properties.** Answers must stay grounded and cited. Self-repair must stay eval-gated and reversible. Student data must stay private to its owner.

## Development setup

```bash
git clone https://github.com/<you>/RAGX.git && cd RAGX

# backend (Python 3.11+)
cd backend
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                          # offline and deterministic, no API keys needed

# web (Node 20+)
cd ../web
npm install
npm run dev
```

The test suite uses a built-in offline model, so it never calls a paid or rate-limited API. To run it against PostgreSQL + pgvector, set `RAGX_TEST_DATABASE_URL`.

## Before you open a pull request

- `pytest` passes in `backend/`.
- This passes too:

  ```bash
  ruff check ragx tests --select F,E9,B --ignore B008,B904,B905
  ```
- `npx tsc --noEmit` and `npm run build` pass in `web/`.
- New behaviour has a test. Bug fixes include a test that failed before the fix.
- No secrets, personal data or real student questions appear in code, tests or fixtures.

## Where things live

| Area | Path |
|---|---|
| Query-time healing pipeline | `backend/ragx/reflex/engine.py` |
| Retrieval (BM25, dense, exact, fusion, rerank, twiddlers) | `backend/ragx/retrieval/` |
| Self-repair loop (diagnose, propose, eval gate, canary) | `backend/ragx/repair/` |
| Model providers | `backend/ragx/llm/` |
| Accounts, roles, usage limits | `backend/ragx/auth.py`, `limits.py`, `api_accounts.py` |
| Dashboard | `web/app/` |

Adding a free model provider is a great first contribution. Implement `complete()` in `llm/providers.py`, register it in `llm/registry.py`, and add a mocked-HTTP test like those in `tests/test_free_providers.py`.

## License of contributions

RAGX is licensed under **AGPL-3.0-or-later**. By submitting a contribution you agree that it is licensed under the same terms.
