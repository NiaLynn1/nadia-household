# NADIA — Solace update

Nadia responds when a member writes **Nadia** (case-insensitive, including “Nadia’s”) or directly mentions the bot in **any text channel in the configured Solace server**. The `/nadia_talk` command still works.

## Deploy
Replace the root `main.py` in your private GitHub repository with this version (and keep `requirements.txt` and `Procfile`). Deploy on Render with existing environment variables.

**Discord Developer Portal → Bot → Privileged Gateway Intents → Message Content Intent: ON**. Also grant the bot **View Channel, Read Message History, Send Messages** in every room where she should respond. She cannot see channels she cannot access.

Existing PostgreSQL tables remain; a new `nadia_chat_history` table is created automatically. Chat memory stores the latest six user/assistant exchanges **per speaker across all Solace channels**, persisted in PostgreSQL. Do not store real sensitive medical information in chat. This is short-term context, not unlimited memory.

## Test
In two different Solace channels, send “Nadia, can you hear me?” and “Hey Nadia, what did I just ask you?”; check that she replies and remembers the first message. Test as both Nia and Sly to confirm identity.

**Note:** A mention of Nadia in any conversation triggers a reply, including incidental mentions; there is no passive listening to messages without her name. Nadia does not automatically start labor or integrate with Nyla's database.

## Visual library update
Parent-only commands: `/nadia_photo_subject` (register person/room/memory), `/nadia_photo_add` (upload image), `/nadia_photo_list`, `/nadia_photo_refs`, `/nadia_photo_view`, `/nadia_photo_remove`, and `/nadia_album_photo` (new captures only). References are stored as image bytes in Nadia's PostgreSQL database, separate from Nyla's database. Existing Discord-link-only album records are not automatically recovered.

Nadia can retrieve an exact named subject from a message such as `Nadia, show me the picture of Nursery` in any Solace channel. For reliable matching, use the registered subject name exactly. Photo generation through `/nadia_capture` now includes the newest saved reference for up to four named subjects in the scene prompt, where supported by the configured image model. Image-edit API calls can cost money and reference likeness is not guaranteed. No automatic connection to Nyla's images or care database is included.

This code has passed Python syntax compilation only. Test in Discord/Render before relying on it. PostgreSQL storage size grows with uploads; use private database credentials and protect server access.
