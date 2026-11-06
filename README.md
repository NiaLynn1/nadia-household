# NADIA — Solace household companion (initial release)

## What is functional
- Discord guild-scoped slash commands for status, dinner plans, daily fictional cleaning, birth-story mode, backup-nanny threshold, notes, chat, wellness, captures, and album.
- PostgreSQL persistence using Nadia's **own** database.
- AI chat and image captures with OpenAI API key; image model availability/billing must be verified.
- HTTP /health endpoint for Render web service and UptimeRobot.
- Automatic fictional housekeeping report once per UTC day (when a channel ID is configured and the service is awake).

## Not yet integrated
- **No automatic Nyla care**. The 5% threshold is stored, but Nadia does not read or alter Nyla's data. A secure approved integration is needed.
- No guaranteed cross-restart or multi-server chat conversation memory; only a household note and saved captures persist.
- No fixed visual identity across image captures unless you later integrate image reference support; prompts alone do not guarantee likeness.
- No real medical practice, physical housework, or physical meal preparation.
- Discord attachment links may expire; album is a link index, not archival image storage.

## Discord
Create app NADIA, invite to Solace with bot + applications.commands, View Channels, Send Messages, Read Message History, Attach Files, Embed Links. No Administrator. Enable Message Content Intent if you intend to add ordinary message listeners later; current slash commands do not require it.

## GitHub
Create private repo `nadia-household`. Upload `main.py`, `requirements.txt`, `Procfile` at repository root.

## PostgreSQL
Create a **new Nadia-only PostgreSQL database** (not Nyla's). Supply the `DATABASE_URL` as a Render secret. SSL may be required by your provider. Schema auto-creates on startup. Do not share secrets in chat.

## Render
New Web Service, Python. Build: `pip install -r requirements.txt`. Start: `python main.py`. Health path: `/health`.

### Environment variables
Required:
- `DISCORD_TOKEN` = NADIA bot token
- `SOLACE_GUILD_ID` = Solace Discord server ID
- `MOMMY_ID` = Mommy's Discord user ID
- `DADDY_ID` = Daddy's Discord user ID
- `DATABASE_URL` = new Nadia database connection string

For AI functionality:
- `OPENAI_API_KEY` = OpenAI API key with image access and billing

Optional:
- `HOUSEHOLD_CHANNEL_ID` = channel for daily fictional housekeeping reports
- `CHAT_MODEL` = `gpt-4.1-mini`
- `CAPTURE_MODEL` = `gpt-image-1`
- `PORT` = assigned automatically by Render

## Commands
`/nadia_status`, `/nadia_dinner`, `/nadia_clean`, `/nadia_birth`, `/nadia_nanny`, `/nadia_note`, `/nadia_talk`, `/nadia_capture`, `/nadia_album`, `/nadia_wellness`.

## Safeguards
- Commands only work in configured Solace server.
- Parent-only for sensitive household settings and captures.
- No automatic birth initiation.
- No writes to Nyla's database.
- Do not assume UptimeRobot is a guaranteed scheduler.
- Image API may charge per image. Keep app private and avoid sharing personal medical data.
