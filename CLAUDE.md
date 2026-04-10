# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI email assistant that manages multiple Gmail inboxes and communicates via Slack DMs. Deployed as Python serverless functions on Vercel with Neon Postgres. Uses Anthropic Claude API for triage, summarization, and draft generation.

## Architecture

```
External Cron (hourly) → GET /api/cron/check_emails
  → fetch new emails from all Gmail accounts (OAuth2)
  → rule-based categorization (lib/rules/)
  → Claude AI triage + summarization
  → store in Postgres
  → send Slack DMs for emails needing attention

Slack DM → POST /api/slack/events
  → verify signature, check owner
  → parse intent via Claude (reply, archive, list, summarize, etc.)
  → execute action (Gmail API / DB query)
  → respond in Slack
```

### Key Directories

- **`api/`** — Vercel serverless function handlers (thin HTTP layer, delegates to `lib/`)
  - `cron/check_emails.py` — hourly email processing pipeline
  - `slack/events.py` — Slack Events API + interactive button handler
  - `auth/gmail_start.py`, `auth/gmail_callback.py` — OAuth2 flow for adding Gmail accounts
  - `health.py` — DB connectivity check
- **`lib/`** — All business logic
  - `models.py` — Pydantic domain models (`Email`, `EmailCategory`, `TriageDecision`, `ParsedCommand`, etc.)
  - `db.py` — Postgres via SQLAlchemy NullPool, session factory, all query functions
  - `db_models.py` — ORM tables: `gmail_accounts`, `emails`, `pending_draft`, `sync_log`
  - `gmail.py` — Gmail API: fetch, send, reply, archive (multi-account)
  - `llm.py` — LLM router: delegates to active provider based on `LLM_PROVIDER` env var
  - `providers/gemini.py` — Google Gemini backend (free tier, default)
  - `providers/claude.py` — Anthropic Claude backend (production)
  - `providers/_prompts.py` — Shared system prompts and prompt builders used by all providers
  - `slack_client.py` — Slack Web API: send DM, update message, Block Kit formatting
  - `triage.py` — Hybrid rule-based + LLM scoring
  - `security.py` — Slack signature verification, cron secret, owner check
  - `crypto.py` — Fernet encryption for OAuth tokens in DB
  - `rules/engine.py` — Pattern-matching rules engine
  - `rules/builtin.py` — Built-in noise filters (newsletters, promotions, social, spam, etc.)

### Data Flow

Cron fetches emails → rules engine categorizes → Claude triages PRIMARY emails → stored in Postgres → Slack DMs sent for attention-needed emails. User replies via Slack → Claude parses intent → action executed → confirmation sent back.

### Draft Verification Flow

User says "reply to X" → Claude generates draft → stored as `pending_draft` (status=pending, expires in 1hr) → Slack message with Send/Cancel buttons → user confirms → Gmail sends reply from correct account with proper threading.

## Environment Variables

See `.env.example`. Key vars: `LLM_PROVIDER` (`gemini` or `claude`), `GOOGLE_GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `DATABASE_URL`, `DATABASE_ENCRYPTION_KEY`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `OWNER_SLACK_USER_ID`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `CRON_SECRET`, `SETUP_SECRET`.

### Switching AI Providers

Set `LLM_PROVIDER=gemini` (default, free) or `LLM_PROVIDER=claude` (production). All AI calls go through `lib/llm.py` which routes to the active provider. To add a new provider, create `lib/providers/yourprovider.py` implementing the same 5 functions (`parse_command`, `triage_emails`, `summarize_email`, `generate_draft`, `edit_draft`) and add a case in `lib/llm.py._provider()`.

## Conventions

- Python 3.10+, deployed on Vercel Python runtime
- Pydantic v2 for models, SQLAlchemy 2.0 ORM with NullPool (serverless)
- Synchronous code (Vercel Python functions are sync)
- Imports use `lib.` prefix (e.g., `from lib.models import Email`)
- Ruff for linting (line-length 88)
- All endpoints use `BaseHTTPRequestHandler` pattern for Vercel Python runtime
- OAuth tokens encrypted with Fernet before storing in Postgres
