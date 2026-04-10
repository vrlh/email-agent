# Product Requirements

## Core Concept

Personal AI email assistant that manages multiple Gmail inboxes and communicates with the user exclusively through Slack DMs.

## Requirements

### Multi-Account Gmail Integration
- Connects to multiple Gmail accounts simultaneously (work, personal, school)
- Each account authenticated independently via OAuth2 refresh tokens
- Full read/write access: read emails, send replies, create drafts, archive messages, mark as read

### Automated Email Monitoring
- Checks all connected inboxes on an hourly schedule
- AI-powered triage classifies every new email: needs reply or not, priority level (urgent/normal/low), one-line summary
- Filters out noise automatically — newsletters, marketing, automated notifications, receipts, shipping updates, password resets, calendar invites with no question, CC'd threads with no action item

### Smart Notifications via Slack DM
- Sends an individual Slack DM for each email that needs the user's attention
- Each notification includes: which account it's from, sender, subject, summary, and a suggested action
- Urgent emails (deadlines, boss/professor, time-sensitive) are flagged with priority indicators
- Emails that don't need attention are silently processed and stored — no notification spam

### Two-Way Slack Communication
- User can DM the bot using natural language — no rigid command syntax
- Bot understands intent and maps it to actions:
  - **Reply**: "Reply to John's email saying I'll be there Thursday"
  - **Draft**: "Draft a reply to the meeting invite — say I can do 2pm"
  - **Summarize**: "Summarize the thread from Sarah about the Q3 budget"
  - **Archive**: "Archive all the low priority emails"
  - **List**: "What emails need my attention?"
- Bot responds with confirmation of what it did

### Email Sending with Verification
- When the user asks to reply, the bot drafts the reply and shows it in Slack first
- User must explicitly confirm ("send") before the email is actually sent
- User can edit the draft ("edit: change Thursday to Friday") or cancel
- Only one pending reply at a time to keep the interaction simple

### Sends Emails on User's Behalf
- Replies are sent from the correct Gmail account (the one that received the original email)
- Replies are threaded properly (same Gmail thread, correct headers)
- User can also request drafts without sending — just to see what a reply would look like

### Actions in Gmail
- Archive emails (removes from inbox)
- Mark emails as replied (tracked in the system)
- All actions performed on the correct account automatically

### Status Dashboard
- Web dashboard showing: emails processed today, emails needing reply, replies sent
- Last-checked time per account
- Recent emails table with account, sender, subject, priority, and status

### Owner-Only Access
- Bot only responds to DMs from a single configured Slack user
- All other users are silently ignored
- Cron endpoint protected by secret token
- Slack requests verified via cryptographic signature

### Hosting & Cost
- Runs on Vercel free (Hobby) tier
- Estimated cost: ~$1-3/month (only Claude API usage)
- Gmail API, Slack API, and Postgres all within free tier limits
