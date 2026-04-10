# Email Agent

AI email assistant that manages multiple Gmail inboxes through Slack. Talk to it naturally — it triages, summarizes, drafts replies, and learns what to ignore.

## What It Does

- **Monitors your inboxes** — connects to multiple Gmail accounts, checks for new emails every 30 minutes
- **Filters noise automatically** — newsletters, promotions, social media, and spam are silently archived. Only emails that matter reach you
- **Notifies you in Slack** — one summary message per sync with emails needing your attention, grouped by account
- **Talks naturally** — no rigid commands. Say "what needs my attention?" or "reply to the Zip interview email saying Tuesday works"
- **Drafts replies with AI** — describe what you want to say, the bot drafts a professional email, you review and send with one click
- **Tracks what needs a reply** — flags emails waiting for your response and clears them automatically when you reply in Gmail
- **Learns your preferences** — tell it "ignore emails from linkedin.com" and it remembers

## How It Works

```
Gmail (multiple accounts)
  -> Vercel serverless function (every 30 min via external cron)
  -> Rule-based noise filtering (8 builtin rules)
  -> Claude Haiku AI triage (scores, summaries, needs-reply detection)
  -> Neon Postgres (email storage, triage metadata, user rules)
  -> Slack notification (summary in #email-agent channel)

User types in Slack
  -> Claude tool-calling agent decides what to do
  -> Executes tools (list, summarize, reply, dismiss, create rules, etc.)
  -> Responds conversationally
```

## Features

**Email Management**
- Multi-account Gmail support (work, personal, school)
- Incremental sync via Gmail history API
- Send replies from the correct account with proper threading
- Archive and mark-as-read via Slack commands
- Draft verification flow — review before sending

**AI Triage**
- Rule-based noise filtering (social, newsletters, promotions, spam, forums, automated)
- Claude Haiku scores emails 0-1 on attention needed
- Flags emails that need your reply (detects questions, requests, scheduling)
- Checks Gmail threads to see if you already responded
- User rules override triage (ignore/priority patterns)

**Slack Interface**
- Dedicated channel for all bot communication
- Natural language — just talk to it
- Threaded replies keep the channel clean
- Send/Cancel buttons on draft reviews
- "Add Gmail account" link in help command

**Example Interactions**
```
You: what needs my attention?
Bot: [shows list of important emails]

You: summarize #3
Bot: [AI summary of that email]

You: reply to the interview email saying Tuesday at 2pm works
Bot: [shows draft with Send/Cancel buttons]

You: ignore everything from caltvexeccentral
Bot: Rule created: ignore emails where sender contains caltvexeccentral

You: needs reply
Bot: [live-checks Gmail, shows only truly unreplied emails]
```

## Setup

### Prerequisites
- [Vercel](https://vercel.com) account (free tier works)
- [Slack](https://api.slack.com/apps) app with bot token
- [Google Cloud](https://console.cloud.google.com) project with Gmail API enabled
- [Anthropic](https://console.anthropic.com) API key (Claude Haiku, ~$1/month)

### 1. Deploy to Vercel

```bash
git clone https://github.com/vrlh/email-agent.git
cd email-agent
npm i -g vercel
vercel
```

Add Neon Postgres: Vercel Dashboard -> Storage -> Add -> Neon Postgres

### 2. Set Environment Variables

In Vercel Dashboard -> Settings -> Environment Variables:

| Variable | Value |
|----------|-------|
| `LLM_PROVIDER` | `claude` |
| `ANTHROPIC_API_KEY` | Your Claude API key |
| `SLACK_BOT_TOKEN` | `xoxb-...` from Slack app |
| `SLACK_SIGNING_SECRET` | From Slack app Basic Information |
| `OWNER_SLACK_USER_ID` | Your Slack member ID |
| `SLACK_CHANNEL_ID` | Channel ID for `#email-agent` |
| `GOOGLE_CLIENT_ID` | From Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | From Google Cloud Console |
| `DATABASE_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `CRON_SECRET` | Any random string (`openssl rand -hex 32`) |
| `SETUP_SECRET` | Any random string |
| `APP_URL` | Your Vercel production URL (e.g. `https://email-agent-xyz.vercel.app`) |

### 3. Create Slack App

1. [api.slack.com/apps](https://api.slack.com/apps) -> Create New App -> From Scratch
2. Bot Token Scopes: `chat:write`, `im:history`, `im:read`, `im:write`, `channels:history`
3. Event Subscriptions -> Enable -> Request URL: `https://YOUR-APP.vercel.app/api/slack/events`
4. Subscribe to: `message.im`, `message.channels`
5. Interactivity -> Enable -> Request URL: same URL
6. Install to workspace

### 4. Create Google OAuth Credentials

1. [Google Cloud Console](https://console.cloud.google.com) -> APIs & Services -> Enable Gmail API
2. Credentials -> Create OAuth 2.0 Client ID (Web application)
3. Authorized redirect URI: `https://YOUR-APP.vercel.app/api/auth/gmail_callback`
4. OAuth consent screen -> Add your email as test user

### 5. Deploy and Initialize

```bash
vercel --prod

# Create database tables
curl https://YOUR-APP.vercel.app/api/health

# Connect your first Gmail account (open in browser)
https://YOUR-APP.vercel.app/api/auth/gmail_start?secret=YOUR_SETUP_SECRET
```

### 6. Set Up Cron

Go to [cron-job.org](https://cron-job.org) (free):
- URL: `https://YOUR-APP.vercel.app/api/cron/check_emails`
- Schedule: every 30 minutes
- Header: `Authorization: Bearer YOUR_CRON_SECRET`

### 7. Test

In `#email-agent`:
- `status` — verify accounts connected
- `onboard` — scan last 3 months of emails
- `needs reply` — see what you owe responses to
- `help` — see all commands

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for full technical documentation including:
- Design decisions and rationale
- Database schema
- Request flow diagrams
- Triage pipeline details
- Agent tool-calling architecture
- Known issues and gotchas
- Cost breakdown

## Tech Stack

- **Runtime:** Python on Vercel serverless functions
- **Database:** Neon Postgres (via Vercel Marketplace)
- **AI:** Claude Haiku 4.5 (tool-calling agent for Slack, triage + summarization + drafts)
- **Email:** Gmail API with OAuth2 (multi-account)
- **Chat:** Slack Web API + Events API
- **Encryption:** Fernet (AES-128) for OAuth tokens at rest

## Cost

At typical usage (2 Gmail accounts, 30-min cron, ~10 Slack interactions/day):

| Service | Cost |
|---------|------|
| Vercel | Free |
| Neon Postgres | Free |
| Claude Haiku | ~$1/month |
| Gmail API | Free |
| Slack API | Free |
| cron-job.org | Free |
| **Total** | **~$1/month** |

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/health` | GET | DB check + migrations |
| `/api/cron/check_emails` | GET | Hourly email sync (Bearer auth) |
| `/api/cron/onboard` | GET | 3-month backfill (Bearer auth) |
| `/api/slack/events` | POST | Slack events + buttons |
| `/api/auth/gmail_start` | GET | OAuth2 initiation |
| `/api/auth/gmail_callback` | GET | OAuth2 callback |

## License

MIT
