# NADIA — Solace + Maison Élise upgrade

This package contains a revised `main.py` built from the user's existing NADIA code. It preserves the PostgreSQL tables, task timers, commands, and photo library, while adding separate guild contexts. It has passed Python syntax compilation but **has not been tested against live Discord, PostgreSQL or OpenAI services**. Deploy to staging first.

## Render environment variables

Required (existing): `DISCORD_TOKEN`, `SOLACE_GUILD_ID`, `MOMMY_ID`, `DADDY_ID`, `DATABASE_URL`, `OPENAI_API_KEY`.

New: `MAISON_ELISE_GUILD_ID=1567054443390828594`.

Optional **Nyla care integration**: `NYLA_DATABASE_URL` is the PostgreSQL connection string for Nyla's database (if the bots share a database, set it equal to `DATABASE_URL`); `NYLA_GUILD_ID` is the original guild ID where Nyla's `nyla_mode` and `nyla_test_state`/`baby_state` rows exist (normally the Solace ID). Without these, Nadia will not modify Nyla's needs or claim she did.

Nyla's care integration is **Solace-only**, and uses Nyla's actual TEST/OFFICIAL mode to select the correct state table. It ignores dormant TEST. It checks every two minutes, uses row locks, and updates feeding, diaper, or comfort in the Nyla database. It doesn't force sleep or fabricate energy recovery. Care messages post to Nyla's current room only after the database transaction commits. Configure `/nadia_nanny` to set the desired threshold (suggest 25). This is fictional roleplay, not real childcare.

## Improvements

- Guild-specific prompt, chat history, jobs, rooms, settings, albums and reference images; Maison Élise receives no Solace context. Nia is recognized in both; Sly's father relationship is only provided in Solace.
- Everyone can call Nadia by name and request household tasks in Maison Élise. Managers can configure her using slash commands; Nia and Sly retain Solace management permissions.
- 3-minute same-user, same-channel follow-up window; replies to Nadia are recognized as follow-ups.
- Natural chore announcements with no 'simulated' phrasing in ordinary user-facing job messages. Task stages remain fictional; no real appliances are controlled.
- Wider international menu persona; no 'darling' address.
- Global slash commands copied/synced to both guilds in `setup_hook`.

## Deploy/test checklist

1. Back up the current NADIA `main.py` and database. Stop duplicate Render instances.
2. Invite NADIA to Maison Élise and enable Discord Message Content Intent.
3. Add the new Render environment variables. **Do not post connection strings or bot tokens in Discord.**
4. Replace `main.py` in the NADIA GitHub repo with the included file; keep `requirements.txt` and `Procfile`.
5. Deploy to staging and check Render logs for command sync in both guilds.
6. In each server run `/nadia_rooms` and `/nadia_status`; test `Nadia, start the laundry` and the subsequent progress update.
7. Test a follow-up without repeating Nadia's name in the same channel within three minutes.
8. Test Maison privacy with questions about Nyla's father and attempts to retrieve Solace photos. Test commands in both servers.
9. Only after reviewing Nyla's live state and backup, configure `NYLA_DATABASE_URL`, set `/nadia_nanny threshold:25` in Solace, and use Nyla's TEST `/test_needs` for a supervised care test.

## Important caveats

- No real-world chores or medical care are performed.
- Automatic Nyla caregiving requires database credentials with read/write access to Nyla's schema. Avoid granting broad permissions if a restricted database role can be used.
- This update does **not** change Nyla's own Discord bot to multi-household mode. That remains a separate update. Do not invite Nyla to Maison Élise yet.
- Discord progress delivery may be lost if Render crashes after a DB stage advance and before a message is sent. No guarantee of exactly-once messages.
- The 3-minute follow-up window is held in memory and resets on restart; persisted AI chat history remains in PostgreSQL.
