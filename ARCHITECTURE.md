# Architecture

Comprehensive reference for the email agent project. Written for future Claude sessions.

## What This Is

A personal AI email assistant that manages multiple Gmail inboxes and communicates via Slack. Deployed as Python serverless functions on Vercel with Neon Postgres. Uses Claude Haiku for AI triage, summarization, and draft generation.

**Production URL:** `https://email-agent-fawn.vercel.app`

## Key Design Decisions

### Why Vercel + Python serverless (not Docker/CLI)
The original repo was a CLI tool with Docker. We rebuilt it as serverless because: no persistent process needed, free/cheap hosting, Slack webhook integration is naturally request/response, and the user wanted Slack as the sole UI.

### Why Claude Haiku (not Gemini, not Sonnet)
Started with Gemini free tier but hit unreliable quota limits (rate limited to 0). Switched to Claude. Haiku over Sonnet because the tasks (JSON triage, short summaries, draft generation) don't need a large model. Cost: ~$0.30-1/month.

### Why a dedicated Slack channel (not DMs)
DMs created scattered threads. A `#email-agent` channel gives one place for: cron summaries (top-level messages), user commands (threaded replies), and a browsable history.

### Why tool-calling agent (not command routing)
Originally used intent-parsing (`parse_command` → route to handler). Switched to Claude tool-calling because: natural conversation, can chain multiple actions, handles vague references ("that zip email"), and follow-up messages in threads.

### Why chunked LLM triage (8 per call)
Sending 200 emails in one LLM call truncated the JSON output. Chunks of 8 keep output under `max_tokens=4096` reliably. Cost increase is negligible.

### Why `needs_reply` is checked against Gmail threads
The AI flags `needs_reply=true` during triage, but the user may have already replied in Gmail. Every cron run checks Gmail threads for owner replies. The `needs reply` Slack command also live-checks before displaying. If the email is FROM the owner's account, it's automatically marked as "no reply needed" (ball is in the other person's court).

### Why `APP_URL` env var (not `x-forwarded-host`)
Vercel's `x-forwarded-host` returns deployment-specific URLs (e.g. `email-agent-abc123.vercel.app`) that don't match the Google OAuth redirect URI. `APP_URL` ensures consistent redirect URIs.

## System Architecture

```
External Cron (cron-job.org, every 30 min)
  -> GET /api/cron/check_emails (Bearer CRON_SECRET)
     -> fetch new emails from all Gmail accounts (OAuth2, history API)
     -> rule-based categorization (8 builtin rules)
     -> Claude Haiku triage (chunks of 8, PRIMARY emails only)
     -> store in Postgres
     -> sync reply status from Gmail threads
     -> send summary to #email-agent channel

User types in #email-agent
  -> POST /api/slack/events (Slack signature verified)
     -> run_agent(text, context, tool_executor)
        -> Claude decides which tools to call
        -> tools execute (DB queries, Gmail API, LLM calls)
        -> Claude generates conversational response
     -> reply in thread via Slack Web API

User clicks Send/Cancel button on draft
  -> POST /api/slack/events (interactive payload)
     -> _handle_send or _handle_cancel
     -> Gmail API send_reply (threaded, correct account)
     -> update Slack message
```

## Database Schema

### `gmail_accounts`
Multi-account OAuth storage. Tokens encrypted with Fernet.
- `id` (PK), `email_address` (unique), `encrypted_tokens`, `is_active`, `last_sync_at`, `last_history_id`

### `emails`
Core email storage with triage metadata.
- `id` (PK, Gmail message ID), `account_id` (FK), `thread_id`, `subject`, `sender_email`, `body_text`
- Triage: `category`, `priority`, `triage_score` (0-1), `triage_decision`, `summary`, `needs_reply`
- Status: `replied_at`, `notified_at`, `processed_at`
- Index: `(account_id, date DESC)`

### `pending_draft`
One active draft at a time. Auto-expires after 1 hour.
- `id` (PK), `account_id`, `reply_to_email_id`, `thread_id`, `to_addresses` (JSONB), `subject`, `body_text`
- `status` (pending/sent/cancelled/expired), `slack_message_ts`, `expires_at`

### `user_rules`
User-created ignore/priority rules applied during triage.
- `id` (PK), `rule_type` (ignore/priority), `field` (sender/sender_domain/subject), `operator` (contains/equals), `value`, `action` (auto_archive/boost)

### `sync_log`
Audit trail for cron runs.
- `id` (PK), `account_id`, `emails_fetched`, `emails_new`, `status`, `error_message`

### Migration strategy
No Alembic. The `/api/health` endpoint runs idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements. New columns/tables are added there.

## File Structure

```
api/                              # Vercel serverless functions
  health.py                       # DB check + migrations
  cron/check_emails.py            # Hourly email sync pipeline
  cron/onboard.py                 # 3-month backfill endpoint
  slack/events.py                 # Slack events + interactive buttons + agent
  auth/gmail_start.py             # OAuth2 initiation
  auth/gmail_callback.py          # OAuth2 callback + auto-onboard

lib/                              # All business logic
  agent.py                        # Tool-calling agent (14 tools, Claude tool-use API)
  models.py                       # Pydantic models (Email, TriageResult, etc.)
  db.py                           # Postgres queries (NullPool for serverless)
  db_models.py                    # SQLAlchemy ORM tables
  gmail.py                        # Gmail API (fetch, send, reply, archive, thread check)
  llm.py                          # LLM router (gemini or claude based on env var)
  slack_client.py                 # Slack Web API + Block Kit builders
  triage.py                       # Rule-based scoring + user rule overrides
  onboard.py                      # Onboard logic (shared by endpoint + Slack command)
  security.py                     # Slack sig verify, cron auth, owner check
  crypto.py                       # Fernet encrypt/decrypt for OAuth tokens
  providers/
    claude.py                     # Claude Haiku implementation
    gemini.py                     # Gemini Flash implementation
    _prompts.py                   # Shared system prompts for all providers
  rules/
    engine.py                     # Rules engine (BaseRule, GenericRule, RegexRule)
    builtin.py                    # 8 builtin noise filters
```

## Agent Architecture

The Slack handler (`api/slack/events.py`) routes all user messages through `lib/agent.py`:

1. User text + context (recent emails, pending draft, thread history) sent to Claude
2. Claude responds with tool calls (e.g. `list_emails`, `reply_to_email`)
3. Tool executors in `events.py` (`_tool_list_emails`, `_tool_reply`, etc.) run the actual logic
4. Results sent back to Claude, which generates a natural response
5. Max 5 tool-call rounds per message

**14 tools:** list_emails, get_needs_reply, summarize_email, reply_to_email, dismiss_emails, send_draft, cancel_draft, edit_draft, create_rule, delete_rule, list_rules, get_status, check_reply_status, onboard

**Important:** Tools that display data (list, needs_reply, summarize, status, rules) send Slack blocks directly via `_reply()`. They return `"[Already displayed to user]"` so the agent doesn't duplicate the output.

## Triage Pipeline

### Layer 1: Builtin Rules (free, instant)
8 regex rules categorize obvious noise: social media, newsletters, promotions, forums, automated emails, spam indicators, urgent keywords.

### Layer 2: User Rules (free, instant)
User-created ignore/priority rules override triage decisions. Stored in `user_rules` table. Applied in `lib/triage.py._apply_user_rules()`.

### Layer 3: AI Triage (Claude Haiku, ~$0.001/email)
Only runs on PRIMARY category emails (after rules filter noise). Processes in chunks of 8. Returns attention_score, decision, summary, needs_reply, suggested_action.

### Layer 4: Gmail Reply Check
After AI flags `needs_reply=true`, checks Gmail thread to see if the user already replied. Also checks if the email is FROM the user (no reply needed — ball in other court). Runs during cron AND on-demand when user asks "needs reply".

### Scoring weights
Category 30%, urgency 25%, recency 25%, sender 20%. Threshold: >=0.7 = needs_attention, <=0.4 + noise category = auto_archived.

## Reply Detection Logic

`check_thread_replied()` in `lib/gmail.py`:
1. Fetch full thread via `threads.get(format="metadata")`
2. If the email being checked is FROM the owner's account -> return True (you sent it)
3. Look at messages AFTER the email being checked
4. If any are FROM the owner AND not calendar auto-responses -> return True
5. Skips: accepted/declined/tentative subjects, text/calendar content types

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `DATABASE_URL` | Yes | Neon Postgres (auto-set by Vercel Marketplace) |
| `DATABASE_ENCRYPTION_KEY` | Yes | Fernet key for OAuth token encryption |
| `LLM_PROVIDER` | Yes | `gemini` or `claude` (default: gemini) |
| `ANTHROPIC_API_KEY` | If claude | Claude API key |
| `GOOGLE_GEMINI_API_KEY` | If gemini | Gemini API key |
| `SLACK_BOT_TOKEN` | Yes | Slack bot OAuth token (xoxb-...) |
| `SLACK_SIGNING_SECRET` | Yes | Slack app signing secret |
| `OWNER_SLACK_USER_ID` | Yes | Single authorized Slack user |
| `SLACK_CHANNEL_ID` | No | Dedicated channel (falls back to DM if unset) |
| `GOOGLE_CLIENT_ID` | Yes | Google OAuth2 client ID |
| `GOOGLE_CLIENT_SECRET` | Yes | Google OAuth2 client secret |
| `CRON_SECRET` | Yes | Bearer token for cron endpoints |
| `SETUP_SECRET` | Yes | Secret for OAuth setup URLs |
| `APP_URL` | Yes | Production URL for OAuth redirect consistency |

## Known Issues and Gotchas

### Vercel function timeout (300s)
The onboard with `force=true` across 700+ emails can approach this limit. Without force, it only processes untriaged emails (fast). The cron never hits this since it only processes new emails (~5-20 per run).

### Gmail history API 500 errors
The history API occasionally returns 500 for certain accounts. We fall back to `messages.list` (recent fetch) on both 404 and 500.

### Slack 3-second timeout
Slack retries events if no 200 within 3 seconds. We return 200 immediately and process synchronously. If Slack retries (X-Slack-Retry-Num header), we return 200 without processing (dedup).

### `#N` reference resolution
`#N` references resolve against the last-displayed email list (`_last_displayed_emails` global). This is a module-level global which works within a single Vercel function invocation but resets between invocations. Thread context from `conversations.replies` helps the agent understand follow-up messages.

### `create_all()` vs migrations
`Base.metadata.create_all()` only creates new tables, not new columns. The health endpoint runs explicit `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for columns added after initial deployment. New columns MUST be added there.

### Double messages from agent
Tools that display Slack blocks return `"[Already displayed to user]"` to prevent the agent from re-listing the same data. The agent system prompt also says "DO NOT repeat or reformat the data" after display tools.

## Cost

At typical usage (2 accounts, 30-min cron, ~10 Slack interactions/day):
- **Vercel:** Free tier (Hobby for cron if needed: $20/mo)
- **Neon Postgres:** Free tier
- **Claude Haiku:** ~$0.30-1/month
- **Gmail API:** Free (within quota)
- **Slack API:** Free
- **cron-job.org:** Free

Total: **~$1-3/month**
