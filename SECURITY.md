# Security policy

RAGX stores people's accounts and the questions they ask, so security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.** Use GitHub's private reporting instead: **Security → Report a vulnerability** on the repository. Include:

- what an attacker can do, and what they need first (an account, the admin role, network access)
- steps to reproduce
- the version or commit you tested

You'll get an acknowledgement within a few days. A fix is prioritised before any public disclosure.

## Scope

In scope:
- authentication, sessions, roles and usage-limit bypasses
- one user seeing another user's questions
- prompt injection that escapes the "evidence is data" boundary
- server-side request forgery through sources
- path traversal through uploads or folder sources

Out of scope:
- rate limits of third-party free model providers
- issues that need a malicious administrator

## Running RAGX safely

- Serve it over **HTTPS** and set `RAGX_COOKIE_SECURE=true`.
- Set `RAGX_SOURCE_ROOTS` so folder sources can only read the directories you intend. Docker Compose does this for you.
- Keep `RAGX_API_KEY` (the automation key) secret. Never give it to a browser.
- Use a **join code** or an allowed email domain if sign-up is open.
- Back up the PostgreSQL database.
