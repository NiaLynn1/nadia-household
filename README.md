# NADIA — Solace Household Operations

Render **Web Service**. Start command: `python main.py`; health check `/health`. Keep existing environment variables `DISCORD_TOKEN`, `SOLACE_GUILD_ID`, `MOMMY_ID`, `DADDY_ID`, `DATABASE_URL`, `OPENAI_API_KEY`. Message Content Intent must be enabled.

## New features
- Nadia discovers existing room channels by their names on startup. Use `/nadia_rooms` to inspect. Assign new or renamed spaces with `/nadia_room_assign` by selecting a Discord channel (no IDs to copy).
- Say `Nadia, start the laundry` or `Nadia, run the dishwasher` in any Solace channel. Chore requests from Nia/Sly start persisted staged simulated tasks.
- `/nadia_job_start`, `/nadia_jobs`, `/nadia_job_cancel`, `/nadia_daily_report`. Job stages advance in background every ~30 seconds and survive Render restarts.
- `/nadia_clinic_visit` logs a fictional appointment and posts in Nadia's clinic. No real medical care or automatic labor onset.
- Existing visual library, photo commands, conversation, and other commands retained.

## Notes and limits
- This is fictional simulation; no real appliances are controlled. Durations are example simulation values.
- Chat follow-ups without the word Nadia are not yet enabled. Name-triggered commands are supported.
- Automatic room discovery matches current known names; unknown rooms can be assigned manually.
- One room per task category; specialized guest-room selection is not yet implemented.
- Avoid running multiple copies of Nadia simultaneously; job progress is transactional, but Discord notification delivery is not guaranteed exactly once if a process crashes between DB update and posting.
- Existing slash command `/nadia_dinner` still suggests menus, while `/nadia_job_start` or a direct natural-language chore request starts a simulated cooking job.
- Existing images and settings tables are preserved. No destructive migrations.

## Suggested test
1. `/nadia_rooms` — verify kitchen, laundry and clinic assignments.
2. In any channel say `Nadia, start a load of laundry.`
3. Check `#laundry-room` for start announcement and `/nadia_jobs` for the countdown.
4. Use `/nadia_job_cancel` with the job ID if you don't want to wait for the full simulation.
