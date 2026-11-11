"""NADIA — Solace staging household companion.

Real Discord commands, optional AI chat/image capture, persistent PostgreSQL state.
No integration with Nyla's database; backup nanny is advisory only until approved API exists.
"""
import asyncio
import base64
import io
import json
import logging
import os
import re
import threading
import urllib.request
import urllib.error
import contextvars
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from openai import AsyncOpenAI
import psycopg

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('nadia')
TOKEN = os.environ['DISCORD_TOKEN']
SOLACE_GUILD_ID = int(os.environ['SOLACE_GUILD_ID'])
MAISON_GUILD_ID = int(os.environ.get('MAISON_ELISE_GUILD_ID', '1567054443390828594'))
HOUSEHOLDS = (SOLACE_GUILD_ID, MAISON_GUILD_ID)
_guild_context = contextvars.ContextVar('nadia_guild', default=None)
def guild_id():
    return _guild_context.get()
def is_solace():
    return guild_id() == SOLACE_GUILD_ID
def house_name():
    return 'Solace' if is_solace() else 'Maison Élise' if guild_id() == MAISON_GUILD_ID else 'Unknown household'

MOMMY_ID = int(os.environ['MOMMY_ID'])
DADDY_ID = int(os.environ['DADDY_ID'])
DATABASE_URL = os.environ['DATABASE_URL']
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
CAPTURE_MODEL = os.getenv('CAPTURE_MODEL', 'gpt-image-1')
CHAT_MODEL = os.getenv('CHAT_MODEL', 'gpt-4.1-mini')
PORT = int(os.getenv('PORT', '10000'))
client_ai = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
app = Flask(__name__)

@app.get('/')
@app.get('/health')
def health():
    return {'service': 'nadia-households', 'status': 'online', 'nyla_integration': 'staging_bridge_configurable'}

def db(sql, params=(), fetch=False):
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if fetch else None

def init_db():
    db('''CREATE TABLE IF NOT EXISTS nadia_settings (
        guild_id BIGINT PRIMARY KEY, chef_skip_date TEXT, cleaning_date TEXT,
        birth_mode TEXT NOT NULL DEFAULT 'waiting', nanny_threshold INTEGER NOT NULL DEFAULT 5,
        last_nanny_notice TEXT, household_notes TEXT NOT NULL DEFAULT ''
    )''')
    db('''CREATE TABLE IF NOT EXISTS nadia_album (
        id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
        prompt TEXT NOT NULL, discord_url TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now()
    )''')
    db('''CREATE TABLE IF NOT EXISTS nadia_events (
        id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, event_type TEXT NOT NULL,
        details TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now()
    )''')
    db('''CREATE TABLE IF NOT EXISTS nadia_chat_history (
        id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now()
    )''')
    db('CREATE INDEX IF NOT EXISTS nadia_chat_lookup ON nadia_chat_history (guild_id,user_id,id DESC)')
    db('''CREATE TABLE IF NOT EXISTS nadia_visual_subjects (
        id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, kind TEXT NOT NULL,
        name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        UNIQUE(guild_id,kind,name))''')
    db('''CREATE TABLE IF NOT EXISTS nadia_visual_photos (
        id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL,
        subject_id BIGINT NOT NULL REFERENCES nadia_visual_subjects(id) ON DELETE CASCADE,
        image_bytes BYTEA NOT NULL, mime_type TEXT NOT NULL,
        angle TEXT NOT NULL DEFAULT '', active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW())''')
    db('CREATE INDEX IF NOT EXISTS nadia_visual_photo_subject ON nadia_visual_photos(subject_id,id DESC)')
    db('ALTER TABLE nadia_album ADD COLUMN IF NOT EXISTS image_bytes BYTEA')
    for gid in HOUSEHOLDS:
        db('INSERT INTO nadia_settings (guild_id) VALUES (%s) ON CONFLICT DO NOTHING', (gid,))

# Quiet, durable fictional follow-through. No task IDs or progress messages in chat.
FOLLOWUP_SCHEMA = """CREATE TABLE IF NOT EXISTS nadia_followups (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL,
 requester_id BIGINT NOT NULL, description TEXT NOT NULL, completion_text TEXT NOT NULL,
 due_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), completed_at TIMESTAMPTZ,
 sending_at TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS nadia_followups_due ON nadia_followups(status,due_at);
"""
def init_service_promises():
    for stmt in FOLLOWUP_SCHEMA.strip().split(';'):
        if stmt.strip(): db(stmt)
    # Preserve previous pending deliveries without recreating them.
    db("""CREATE TABLE IF NOT EXISTS nadia_service_promises (
        id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL,
        requester_id BIGINT NOT NULL, item TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        due_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        UNIQUE(guild_id,channel_id,requester_id,item,status)
    )""")

async def identify_commitments(ai, model, answer):
    # Extract promises from Nadia's actual reply, not from the user's wish list.
    # Explicit exclusions prevent automatic fictional clinical procedures or guarantees.
    if not re.search(r"\\b(?:I'll|I’ll|I will|let me|I'm going to|I’m going to|I'll be back|I’ll be back)\\b",answer,re.I):
        return []
    instructions = """Extract explicit, concrete commitments that the SPEAKER (Nadia) made
in her own response. Return JSON object {"tasks":[{"description":"short specific action",
"completion_text":"natural first-person message Nadia would say when the action is finished",
"minutes":2}]}.
Only include safe, routine, in-story household actions, fetching items, meals, cleaning,
checking back after a stated interval, or providing a promised informational update.
For long chores choose a reasonable fictional duration 3-30 minutes; quick fetching 1-3.
If no concrete actionable commitment, tasks must be [].
Never include speculative offers, conditional promises, jokes, vague reassurance, or
things already completed. Never schedule medical exams, birth, treatment, medicine,
clinical results, fetal monitoring, emergencies, real-world external actions, or any
promise that requires user consent or unavailable information.
The completion message must never assert a real-world physical action took place.
Be brief and conversational, no task numbers, no progress tracking, no forced sass.
Do not invent new tasks or alter the commitment."""
    try:
        response=await ai.chat.completions.create(
            model=model,temperature=0,max_tokens=450,
            response_format={"type":"json_object"},
            messages=[{"role":"system","content":instructions},
                      {"role":"user","content":answer[:1900]}])
        data=json.loads(response.choices[0].message.content or '{}')
        tasks=data.get('tasks',[])
        if not isinstance(tasks,list):return []
        clean=[]
        for item in tasks[:5]:
            if not isinstance(item,dict):continue
            desc=str(item.get('description','')).strip()[:220]
            line=str(item.get('completion_text','')).strip()[:800]
            try: mins=max(1,min(30,int(item.get('minutes',2))))
            except (TypeError,ValueError):mins=2
            if desc and line:clean.append((desc,line,mins))
        return clean
    except Exception:
        log.exception('Could not extract Nadia commitments')
        return []

def save_commitments(gid,cid,uid,commitments):
    for desc,line,minutes in commitments:
        # Prevent duplicates while a promise is still pending.
        db("""INSERT INTO nadia_followups
          (guild_id,channel_id,requester_id,description,completion_text,due_at)
          SELECT %s,%s,%s,%s,%s,NOW()+(%s * INTERVAL '1 minute')
          WHERE NOT EXISTS (
           SELECT 1 FROM nadia_followups WHERE guild_id=%s AND channel_id=%s
            AND requester_id=%s AND lower(description)=lower(%s)
            AND status IN ('pending','sending'))""",
            (gid,cid,uid,desc,line,minutes,gid,cid,uid,desc))

def pending_services(gid,cid,uid):
    rows=db("""SELECT description FROM nadia_followups
      WHERE guild_id=%s AND channel_id=%s AND requester_id=%s
      AND status IN ('pending','sending') ORDER BY due_at LIMIT 12""",
      (gid,cid,uid),True)
    old=db("""SELECT item FROM nadia_service_promises WHERE guild_id=%s
      AND channel_id=%s AND requester_id=%s AND status='pending'""",
      (gid,cid,uid),True)
    return ', '.join([r[0] for r in rows]+[r[0] for r in old]) or 'none'

def claim_due_followups():
    with psycopg.connect(DATABASE_URL,connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,guild_id,channel_id,requester_id,completion_text
              FROM nadia_followups WHERE
              (status='pending' AND due_at<=NOW()) OR
              (status='sending' AND sending_at<NOW()-INTERVAL '3 minutes')
              ORDER BY due_at LIMIT 20 FOR UPDATE SKIP LOCKED""")
            rows=cur.fetchall()
            for fid,*_ in rows:
                cur.execute("""UPDATE nadia_followups SET status='sending',
                  sending_at=NOW(),attempts=attempts+1 WHERE id=%s""",(fid,))
    return rows

def finish_followup(fid):
    db("""UPDATE nadia_followups SET status='completed',completed_at=NOW()
      WHERE id=%s""",(fid,))

def retry_followup(fid):
    db("""UPDATE nadia_followups SET status='pending',
      due_at=NOW()+INTERVAL '1 minute' WHERE id=%s""",(fid,))

def claim_legacy_services():
    # Legacy service promises still receive their tea/blanket/water, but
    # are acknowledged only after Discord accepts the message.
    with psycopg.connect(DATABASE_URL,connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,guild_id,channel_id,requester_id,item
             FROM nadia_service_promises WHERE status='pending' AND due_at<=NOW()
             ORDER BY due_at LIMIT 20 FOR UPDATE SKIP LOCKED""")
            return cur.fetchall()

def finish_legacy_service(fid):
    db("""UPDATE nadia_service_promises SET status='completed',completed_at=NOW()
      WHERE id=%s""",(fid,))

LEGACY_LINES={
 'tea':'Nia, here is your tea, freshly made and warm. Careful, the cup is hot.',
 'blanket':'Nia, here is that warm blanket. Let me tuck it around your shoulders.',
 'water':'Here is your glass of water, Nia. I put it within easy reach.'
}

def audit(event, details):
    db('INSERT INTO nadia_events(guild_id,event_type,details) VALUES (%s,%s,%s)', (guild_id(),event,details[:1800]))

def setting():
    return db('SELECT chef_skip_date,cleaning_date,birth_mode,nanny_threshold,household_notes FROM nadia_settings WHERE guild_id=%s', (guild_id(),), True)[0]

def allowed(interaction):
    return interaction.guild_id in HOUSEHOLDS

def parent(interaction):
    return allowed(interaction) and (interaction.user.id in (MOMMY_ID,DADDY_ID) if interaction.guild_id == SOLACE_GUILD_ID else interaction.user.id == MOMMY_ID or interaction.user.guild_permissions.manage_guild)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)


async def reject(interaction, parents=False):
    if not allowed(interaction) or (parents and not parent(interaction)):
        await interaction.response.send_message('This command is available only in an authorized household.' if not allowed(interaction) else 'Only authorized household managers can change this.', ephemeral=True)
        return True
    return False

@bot.tree.interaction_check
async def set_interaction_household(interaction: discord.Interaction):
    if interaction.guild_id not in HOUSEHOLDS:
        return False
    _guild_context.set(interaction.guild_id)
    return True

@bot.event
async def on_ready():
    log.info('NADIA online as %s', bot.user)
    await asyncio.to_thread(init_service_promises)
    if not daily_housekeeping.is_running():
        daily_housekeeping.start()
    if not nadia_job_clock.is_running():
        nadia_job_clock.start()
    if (NYLA_CARE_API_URL and len(NYLA_CARE_API_KEY)>=32 and NYLA_STAGING_GUILD_ID==1567342896842018946 and not nadia_nyla_watch.is_running()):
        nadia_nyla_watch.start()
    for gid in HOUSEHOLDS:
        guild=bot.get_guild(gid)
        if guild:
            token=_guild_context.set(gid)
            try: await asyncio.to_thread(discover_rooms,guild)
            finally: _guild_context.reset(token)

@bot.tree.command(name='nadia_status', description='Show Nadia’s household status')
async def status(interaction: discord.Interaction):
    if await reject(interaction): return
    skip, clean, birth, threshold, _ = await asyncio.to_thread(setting)
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [f'🏡 **Nadia St. Clair | {house_name()}**']
    if is_solace():
        lines += [f'Birth story: **{birth}**',
                  f'Housekeeping: **{"completed" if clean == today else "pending"}**',
                  f'Dinner: **{"Nia has it" if skip == today else "available by request"}**',
                  f'Nyla backup threshold: **{threshold}%**']
    lines.append('Scene captures: available with an image API key.')
    await interaction.response.send_message('\n'.join(lines), ephemeral=True)

@bot.tree.command(name='nadia_dinner', description='Tell Nadia you have dinner or request a menu')
@app_commands.describe(plan='Choose who handles dinner today')
@app_commands.choices(plan=[app_commands.Choice(name='I have dinner tonight',value='mommy'),app_commands.Choice(name='Nadia, suggest dinner',value='suggest'),app_commands.Choice(name='Reset dinner plan',value='reset')])
async def dinner(interaction: discord.Interaction, plan: app_commands.Choice[str]):
    if await reject(interaction, True): return
    today = datetime.now(timezone.utc).date().isoformat()
    if plan.value == 'mommy':
        await asyncio.to_thread(db,'UPDATE nadia_settings SET chef_skip_date=%s WHERE guild_id=%s',(today,guild_id()))
        reply=f'Of course, {interaction.user.display_name}. Dinner is yours tonight. I’ll keep the kitchen tidy. ❤️'
    elif plan.value == 'reset':
        await asyncio.to_thread(db,'UPDATE nadia_settings SET chef_skip_date=NULL WHERE guild_id=%s',(guild_id(),))
        reply='Dinner plan reset. I’m available if needed.'
    else:
        reply='I can put together anything from Caribbean or Italian to Mediterranean, Japanese, Mexican or a cozy home-style dinner. Tell me what you’re craving, and I’ll build a menu around it.'
    await interaction.response.send_message(reply)
    await asyncio.to_thread(audit,'dinner',f'{interaction.user.id}:{plan.value}')

@bot.tree.command(name='nadia_clean', description='Run a fictional Solace cleaning routine')
async def clean(interaction: discord.Interaction):
    if await reject(interaction, True): return
    today = datetime.now(timezone.utc).date().isoformat()
    jid,target,stage,_=await asyncio.to_thread(start_job,'general','General household cleaning',interaction.user.id,interaction.channel_id)
    await interaction.response.send_message('Of course. I’ll take care of the cleaning.')

@bot.tree.command(name='nadia_birth', description='Set the fictional birth-story mode; never starts labor automatically')
@app_commands.choices(mode=[app_commands.Choice(name='Waiting',value='waiting'),app_commands.Choice(name='Labor scene (parent-initiated)',value='labor'),app_commands.Choice(name='Postpartum (parent-initiated)',value='postpartum')])
async def birth(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if await reject(interaction, True): return
    if not is_solace(): return await interaction.response.send_message('This feature belongs to Solace.',ephemeral=True)
    current_mode = (await asyncio.to_thread(setting))[2]
    if current_mode == 'postpartum' and mode.value != 'postpartum':
        return await interaction.response.send_message('Nyla is already born in this story. I will not reset her to unborn or labor mode.', ephemeral=True)
    await asyncio.to_thread(db,'UPDATE nadia_settings SET birth_mode=%s WHERE guild_id=%s',(mode.value,guild_id()))
    await asyncio.to_thread(audit,'birth_mode',f'{interaction.user.id}:{mode.value}')
    speaker = 'Nia' if interaction.user.id == MOMMY_ID else 'Sly' if interaction.user.id == DADDY_ID else interaction.user.display_name
    await interaction.response.send_message(f'Birth-story mode set to **{mode.name}** by **{speaker}**. ' + ('Nyla is already born. I’ll continue with postpartum care.' if mode.value == 'postpartum' else f'I’ll follow your lead, {speaker}.'))

@bot.tree.command(name='nadia_nanny', description='Set backup-care alert threshold (integration pending)')
@app_commands.describe(threshold='Critical care percentage (1–25)')
async def nanny(interaction: discord.Interaction, threshold: app_commands.Range[int,1,25]):
    if await reject(interaction, True): return
    if not is_solace(): return await interaction.response.send_message('Nyla care monitoring is available in Solace.',ephemeral=True)
    await asyncio.to_thread(db,'UPDATE nadia_settings SET nanny_threshold=%s WHERE guild_id=%s',(threshold,guild_id()))
    await interaction.response.send_message(f'Backup-care alert threshold: **{threshold}%**. Staging bridge uses Nyla safety thresholds when configured.')

@bot.tree.command(name='nadia_note', description='Save a household continuity note')
async def note(interaction: discord.Interaction, detail: str):
    if await reject(interaction, True): return
    if len(detail)>1000:
        await interaction.response.send_message('Please keep notes under 1,000 characters.',ephemeral=True);return
    await asyncio.to_thread(db,'UPDATE nadia_settings SET household_notes=%s WHERE guild_id=%s',(detail,guild_id()))
    await asyncio.to_thread(audit,'household_note',f'updated by {interaction.user.id}')
    await interaction.response.send_message('Your household note is saved.',ephemeral=True)

"""Nadia conversational intelligence layer. No new slash commands.
Import into the existing main.py as documented in README.
"""

WAKE = re.compile(r"(?<!\w)nadia(?:['’]s)?(?!\w)", re.I)
PRIVACY = re.compile(r"\b(?:give (?:us|me) (?:some )?(?:privacy|space)|leave (?:us|me) alone|stop (?:talking|responding|replying)|stay (?:quiet|silent)|don't (?:respond|reply|interrupt)|do not (?:respond|reply|interrupt)|we(?:'|’)ll call you|go downstairs)\b", re.I)
RESUME = re.compile(r"\b(?:nadia[,! ]+)?(?:come back|we need you|i need you|you can speak|join us|resume|can you help|please help|check on|come here)\b", re.I)
OTHER_ADDRESSEE = re.compile(r"^\s*(?:hey\s+|hi\s+|hello\s+)?(?:sly|baby|babe|love|nia)\b[,!?. ]*", re.I)
EM_DASH = re.compile('[\u2014\u2013]')
FILLER = re.compile(r"\b(?:i'm here (?:for you|if you need anything)|you(?:'re| are) doing amazing|let me know if you need anything)\b", re.I)

PERSONA = '''You are Nadia St. Clair, a fictional 34-year-old Black woman from Charleston. You are one person with three established roles: the household manager and accomplished international private chef, Nia's qualified midwife for fictional birth scenes, and the family's primary care physician in fictional medical scenes. You are competent, perceptive, poised, discreet, warm, dryly funny when the moment allows, and naturally sassy without insulting anyone. Never perform a stereotype, force slang, make corny one-liners, or call anyone darling. Address Nia as Nia. In Solace, address Sly as Sly. In Maison Élise, never mention or imply Solace relationships. Speak like a real person in conversation, usually one to four sentences. No em dashes or en dashes. Avoid scripted reassurance, canned empathy, repetitive praise, unnecessary follow-up questions, and closing offers of assistance. Read the room. Silence is often appropriate. Maintain continuity with previously established fictional events. Do not claim an exam or a medical finding happened until the roleplay establishes consent and the examination. For a requested fictional assessment, explain briefly, request consent if needed, then proceed in-character when consent is given. Do not invent precise dilation, fetal measurements, or diagnoses without a narrated assessment. Clinical scenes are roleplay, not real medical treatment. For genuine real-world symptoms, recommend appropriate licensed medical care. Do not invent completed household tasks, real-world physical actions, notifications, or database changes. When you make a specific, safe household promise, a private follow-through scheduler handles it. Do not narrate task IDs or progress updates. Speak naturally and return only when finished. Never promise automatic medical procedures or actual external actions. Do not claim an item was delivered before that notice. Only generate a photo when Nia or Sly explicitly asks Nadia to take a picture or invokes /nadia_capture. Never initiate photography. Never fabricate photo-approval visitors, administrative staff, or strangers. Never claim that a picture represents the family without approved saved references. If asked about a pending item, acknowledge the delay and its pending status; do not make a new promise that resets the clock. Never initiate labor or announce a birth independently.'''

SCHEMA = '''
CREATE TABLE IF NOT EXISTS nadia_conversation_control (
 guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL,
 privacy BOOLEAN NOT NULL DEFAULT FALSE,
 updated_by BIGINT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS nadia_scene_memory (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL,
 actor_id BIGINT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS nadia_scene_memory_lookup
 ON nadia_scene_memory(guild_id,channel_id,id DESC);
'''

CLINICAL_SCHEMA = '''
CREATE TABLE IF NOT EXISTS nadia_clinical_scene (
 guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL,
 patient_id BIGINT NOT NULL, dilation_cm NUMERIC(3,1),
 assessment TEXT NOT NULL DEFAULT '',
 assessed_at TIMESTAMPTZ,
 PRIMARY KEY (guild_id,channel_id,patient_id)
);
CREATE TABLE IF NOT EXISTS nadia_clinical_observations (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL,
 channel_id BIGINT NOT NULL, patient_id BIGINT NOT NULL,
 speaker_id BIGINT NOT NULL, observation TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS nadia_clinical_obs_lookup
 ON nadia_clinical_observations(guild_id,channel_id,patient_id,id DESC);
'''

def clinical_context(db, gid, channel, patient):
    record=db('''SELECT dilation_cm,assessment,assessed_at FROM nadia_clinical_scene
       WHERE guild_id=%s AND channel_id=%s AND patient_id=%s''',
       (gid,channel,patient),True)
    observations=db('''SELECT observation FROM nadia_clinical_observations
       WHERE guild_id=%s AND channel_id=%s AND patient_id=%s
       ORDER BY id DESC LIMIT 8''',(gid,channel,patient),True)
    result='Recorded fictional clinical state for this scene: '
    if record:
        dilation,assessment,at=record[0]
        result+=f'Last documented dilation: {dilation} cm. ' if dilation is not None else 'No measured dilation documented. '
        result+=f'Last assessment: {assessment[:350]}. '
    else:
        result+='No completed measured examination recorded. '
    if observations:
        result+='Recent observations (newest first): '+'; '.join(r[0][:180] for r in observations)
    return result

def record_observation(db,gid,channel,patient,speaker,content):
    # Only dialogue relevant to this fictional clinical scene, no cross-guild data.
    if re.search(r'\\b(?:contraction|dilat|cervix|water broke|water breaking|baby|nyla|pool|push|pain|pressure|heartbeat|kick|labor|labour)\\b',content,re.I):
        db('''INSERT INTO nadia_clinical_observations
        (guild_id,channel_id,patient_id,speaker_id,observation)
        VALUES(%s,%s,%s,%s,%s)''',(gid,channel,patient,speaker,content[:700]))

def record_assessment(db,gid,channel,patient,answer):
    # Record only an explicit measurement narrated as a completed examination.
    if not re.search(r'\\b(?:exam|check|assessment|reassessment|cervix|dilat)\\b',answer,re.I):
        return
    match=re.search(r'\\b(?:dilat(?:ed|ion)?(?:\\s+(?:to|at|about|around))?|cervix\\s+(?:is|at|measures))\\s*[:]?\\s*(?:about|around|approximately)?\\s*(10(?:\\.0)?|[0-9](?:\\.[0-9])?)\\s*(?:cm|centimeters?)\\b',answer,re.I)
    if not match:
        match=re.search(r'\\b(10(?:\\.0)?|[0-9](?:\\.[0-9])?)\\s*(?:cm|centimeters?)\\s+dilat',answer,re.I)
    if not match: return
    cm=float(match.group(1))
    if not 0<=cm<=10: return
    db('''INSERT INTO nadia_clinical_scene
      (guild_id,channel_id,patient_id,dilation_cm,assessment,assessed_at)
      VALUES(%s,%s,%s,%s,%s,NOW())
      ON CONFLICT(guild_id,channel_id,patient_id)
      DO UPDATE SET dilation_cm=EXCLUDED.dilation_cm,
        assessment=EXCLUDED.assessment,assessed_at=NOW()''',
      (gid,channel,patient,cm,answer[:700]))

def install_schema(db):
    for sql in (SCHEMA + CLINICAL_SCHEMA).strip().split(';'):
        if sql.strip(): db(sql)

def privacy_enabled(db, gid, channel):
    rows=db('SELECT privacy FROM nadia_conversation_control WHERE guild_id=%s AND channel_id=%s', (gid,channel), True)
    return bool(rows and rows[0][0])

def set_privacy(db, gid, channel, user, value):
    db('''INSERT INTO nadia_conversation_control(guild_id,channel_id,privacy,updated_by,updated_at)
    VALUES(%s,%s,%s,%s,NOW()) ON CONFLICT(guild_id,channel_id) DO UPDATE
    SET privacy=EXCLUDED.privacy,updated_by=EXCLUDED.updated_by,updated_at=NOW()''', (gid,channel,value,user))

def save_scene(db, gid, channel, actor, role, content):
    db('INSERT INTO nadia_scene_memory(guild_id,channel_id,actor_id,role,content) VALUES (%s,%s,%s,%s,%s)', (gid,channel,actor,role,content[:3000]))

def scene_history(db, gid, channel, limit=24):
    return db('''SELECT role,actor_id,content FROM (
      SELECT id,role,actor_id,content FROM nadia_scene_memory
      WHERE guild_id=%s AND channel_id=%s ORDER BY id DESC LIMIT %s
    ) recent ORDER BY id''', (gid,channel,limit), True)

def addressed_to_nadia(message, bot_user):
    content=message.content or ''
    named=bool(WAKE.search(content) or (bot_user and bot_user.mentioned_in(message)))
    referenced=message.reference and message.reference.resolved
    reply=bool(referenced and getattr(getattr(referenced,'author',None),'id',None)==getattr(bot_user,'id',None))
    return named or reply

def should_reply(message, bot_user, quiet, active=False):
    """Explicit summons or a recent, context-aware continuation."""
    content=(message.content or '').strip()
    if not content or content.startswith('!'): return False
    addressed=addressed_to_nadia(message,bot_user)
    if quiet:
        return bool(addressed and RESUME.search(content) and not PRIVACY.search(content))
    if OTHER_ADDRESSEE.search(content) and not WAKE.search(content): return False
    if re.search(r'\bwhispers?\b|^\s*\*.*?\*', content, re.I) and not addressed: return False
    if addressed: return True
    if active and not message.mentions and not OTHER_ADDRESSEE.search(content):
        # A question or a short response to an active scene is a continuation.
        return bool('?' in content or re.match(r"^(?:yes|no|okay|ok|alright|sure|please|go ahead|how|what|is|am|can|should|i'm|im)\b", content, re.I))
    return False

def polish(text):
    text=EM_DASH.sub(', ',text or '')
    text=re.sub(r'\s+,', ',', text)
    text=re.sub(r' {2,}', ' ', text)
    return text.strip()

async def generate_reply(ai, model, db, gid, channel_id, user_id, user_name, message, household_system, birth_mode, notes):
    """Shared channel memory, but isolated by guild and channel. Caller handles permissions."""
    rows=await asyncio.to_thread(scene_history,db,gid,channel_id)
    clinical=await asyncio.to_thread(clinical_context,db,gid,channel_id,MOMMY_ID) if gid==SOLACE_GUILD_ID and birth_mode=='labor' else ''
    services=await asyncio.to_thread(pending_services,gid,channel_id,user_id)
    sys=(PERSONA+'\nPending in-story service promises to the current speaker: '+services+'\n'+clinical+'\n'+household_system+'\nCurrent birth mode: '+str(birth_mode)+
         '\nHousehold note: '+str(notes or '')[:1000]+
         ('\nCANONICAL POSTPARTUM FACT: Nyla has ALREADY BEEN BORN. She is outside Nia’s body. No labor, dilation, fetal exams, or birth-pending language. Treat this as settled scene history. Do not ask for another exam unless Nia specifically requests it. Never invent where Nyla is; ask the family if her location has not been established. ' if gid==SOLACE_GUILD_ID and birth_mode=='postpartum' else '')+
         '\nRecent dialogue includes different speakers. Respond to the CURRENT speaker, not everyone in the scene. '+
         ('In Solace, both Nia and Sly have approved Nyla going live in the story; this does not verify activation of the separate Nyla bot. Do not request their story approval again. ' if gid == SOLACE_GUILD_ID else '')+

         'Never leak information across households. In Solace labor scenes, Nadia is the established qualified midwife, not a bystander. The persistent birth mode is authoritative: postpartum means Nyla is already born; waiting/labor do not establish birth. Preserve continuity across the entire scene and never contradict a previously established exam. Ask consent before examinations. Do not assert exact dilation before the fictional exam. '+
         'Only the existing task engine can start chores. Household commitments can be scheduled for fictional completion. Do not claim a real-world action happened. '+
         'In fictional medical scenes, once Nia herself consents to an examination, narrate the assessment and give a clear specific fictional finding rather than vague reassurance. Keep the finding consistent with earlier scene results. When asked for results of a completed exam, answer immediately with the actual fictional finding and next step. If a completed exam has no numerical finding yet, establish a plausible fictional finding explicitly during the consented narrated exam and give its measurement in cm. Once recorded, reuse the recorded value until another consented exam changes it. Do not pretend a new examination occurred just because someone asks how dilated Nia is. Never turn vague reassurance into a medical measurement. Avoid telling Nia to wait for the urge to push before using the birthing pool; water immersion for comfort can be discussed earlier in labor if appropriate to the scene. Do not repeatedly request consent or evade the question. Do not treat Sly consenting as Nia consenting. Clinical results are fictional story details, not real diagnostic measurements.')
    messages=[{'role':'system','content':sys}]
    for role,actor,content in rows:
        messages.append({'role':'assistant' if role=='assistant' else 'user',
                         'content':content if role=='assistant' else f'[Discord user {actor}]: {content}'})
    messages.append({'role':'user','content':f'[{user_name}, user {user_id}]: {message[:3000]}'})
    result=await ai.chat.completions.create(model=model,messages=messages,max_tokens=350,temperature=0.7)
    answer=polish(result.choices[0].message.content or '')
    if not answer: answer='Give me a moment, please.'
    if gid==SOLACE_GUILD_ID and birth_mode=='labor':
        await asyncio.to_thread(record_observation,db,gid,channel_id,MOMMY_ID,user_id,message)
        await asyncio.to_thread(record_assessment,db,gid,channel_id,MOMMY_ID,answer)
    await asyncio.to_thread(save_scene,db,gid,channel_id,user_id,'user',message)
    await asyncio.to_thread(save_scene,db,gid,channel_id,user_id,'assistant',answer)
    commitments=await identify_commitments(ai,model,answer)
    if commitments: await asyncio.to_thread(save_commitments,gid,channel_id,user_id,commitments)
    return answer[:1900]



def household_persona():
    if is_solace():
        return (PERSONA + " Household: Solace. Nia is Nyla Reign's mother and Sly is her father. "
                "Address them Nia and Sly; Mommy and Daddy are titles from Nyla's perspective only.")
    return (PERSONA + " Household: Maison Élise. Nia is Nyla Reign Diamond's mother. "
            "You have no information identifying Nyla's father. Do not guess, imply, retrieve or disclose "
            "paternal identity or relationships from other households. Treat other members as ordinary guests. "
            "Do not mention Solace, its residents, family structure, private memories or photographs.")

def speaker_context(user_id):
    if user_id == MOMMY_ID:
        return 'The speaker is Nia. Address her as Nia.'
    if is_solace() and user_id == DADDY_ID:
        return 'The speaker is Sly. Address him as Sly.'
    return 'The speaker is a household member or guest. Do not assume their family relationship.'

async def nadia_reply(user_id, message, channel_id=None, user_name=None):
    if not client_ai:
        return 'My conversation service is not configured yet.'
    _, _, birth_mode, _, notes = await asyncio.to_thread(setting)
    if channel_id is None:
        channel_id = 0
    return await generate_reply(client_ai, CHAT_MODEL, db, guild_id(), channel_id,
        user_id, user_name or ('Nia' if user_id == MOMMY_ID else 'Sly' if is_solace() and user_id == DADDY_ID else 'Guest'),
        message, household_persona() + ' ' + speaker_context(user_id), birth_mode, notes)

@bot.tree.command(name='nadia_talk', description='Talk with Nadia in character')
async def talk(interaction: discord.Interaction, message: str):
    if await reject(interaction): return
    await interaction.response.defer()
    try:
        answer = await nadia_reply(interaction.user.id,message,interaction.channel_id,interaction.user.display_name)
        await interaction.followup.send(answer[:1900])
    except Exception:
        log.exception('AI chat failed')
        await interaction.followup.send('My conversation service is temporarily unavailable. Please try again.',ephemeral=True)

# Explicit summons and reply-to-bot only. Privacy is persisted per household/channel.
@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.guild.id not in HOUSEHOLDS or message.author.bot:
        return
    token = _guild_context.set(message.guild.id)
    try:
        await handle_household_message(message)
    finally:
        _guild_context.reset(token)

async def handle_household_message(message: discord.Message):
    if message.content.startswith('!'):
        await bot.process_commands(message)
        return
    if not message.content.strip():
        return
    gid, cid = message.guild.id, message.channel.id
    addressed = addressed_to_nadia(message, bot.user)
    quiet = await asyncio.to_thread(privacy_enabled, db, gid, cid)
    if quiet:
        if not should_reply(message, bot.user, True):
            return
        if message.author.id not in (MOMMY_ID, DADDY_ID) and not message.author.guild_permissions.manage_guild:
            return
        await asyncio.to_thread(set_privacy, db, gid, cid, message.author.id, False)
        quiet = False
    # Remember all channel dialogue for continuity, without responding to everyone.
    # Recent Nadia activity authorizes natural follow-ups for five minutes.
    recent = await asyncio.to_thread(db, '''SELECT created_at FROM nadia_scene_memory
        WHERE guild_id=%s AND channel_id=%s AND role='assistant'
        ORDER BY id DESC LIMIT 1''', (gid,cid), True)
    active = bool(recent and (datetime.now(timezone.utc)-recent[0][0]).total_seconds()<300)
    if not should_reply(message, bot.user, quiet, active):
        await asyncio.to_thread(save_scene,db,gid,cid,message.author.id,'user',message.content)
        return
    if PRIVACY.search(message.content):
        if message.author.id in (MOMMY_ID, DADDY_ID) or message.author.guild_permissions.manage_guild:
            await asyncio.to_thread(set_privacy, db, gid, cid, message.author.id, True)
            await message.reply('Of course. I’ll give you some space.', mention_author=False)
        return
    try:
        # Photos require an explicit request to Nadia from an authorized parent.
        if addressed and CAPTURE_REQUEST.search(message.content) and (message.author.id == MOMMY_ID or (is_solace() and message.author.id == DADDY_ID)):
            if not client_ai:
                await message.reply('My image service is not configured yet.', mention_author=False)
                return
            try:
                async with message.channel.typing():
                    context = await build_capture_context(message.channel, message.content)
                    await create_scene_capture(message.channel, message.author.id, message.content, context)
            except ValueError as exc:
                await message.reply(str(exc)[:1800], mention_author=False)
            return
        if await ops_request(message, message.content):
            return
        target = photo_request_target(message.content)
        if addressed and target and (message.author.id == MOMMY_ID or (is_solace() and message.author.id == DADDY_ID)):
            row = await asyncio.to_thread(find_named_photo, target)
            if row:
                pid, raw, mime, name = row
                await message.reply(f'Here is the saved picture of **{name}**.',
                    file=image_file(raw,mime,f'nadia_reference_{pid}'),mention_author=False)
                return
        async with message.channel.typing():
            answer = await nadia_reply(message.author.id, message.content, cid, message.author.display_name)
        await message.reply(answer[:1900], mention_author=False)
    except Exception:
        log.exception('Nadia conversation failed')
        await message.reply('One moment. I’m having trouble with that request.', mention_author=False)


# ---------- Solace household operations ----------
# Every duration is simulated; no real appliances or healthcare services are controlled.
import unicodedata

ROOM_ALIASES = {
    'kitchen':['main-kitchen'], 'laundry':['laundry-room'],
    'clinic':['nadias-clinic'], 'living':['living-room'],
    'bedroom':['primary-suite'], 'nursery':['nylas-room'],
    'pantry':['walk-in-pantry'], 'dining':['breakfast-nook'],
    'guest':['guest-room-1','guest-room-2','guest-lounge'],
    'bathroom':['powder-room'], 'patio':['patio'],
}
TASK_STAGES = {
    'laundry':[('Sorting clothes',3),('Washing clothes',35),('Drying clothes',45),('Folding clothes',12),('Putting clothes away',8)],
    'dishwasher':[('Loading dishwasher',5),('Washing dishes',55),('Drying dishes',20),('Unloading dishwasher',7)],
    'cooking':[('Planning and preparing ingredients',10),('Cooking the meal',30),('Plating and serving',5)],
    'vacuum':[('Preparing vacuum',3),('Vacuuming',22),('Putting equipment away',4)],
    'mop':[('Preparing floors',5),('Mopping floors',20),('Allowing floors to dry',15)],
    'dust':[('Preparing supplies',3),('Dusting surfaces',20),('Putting supplies away',4)],
    'tidy':[('Collecting clutter',8),('Organizing room',15),('Finishing touches',5)],
    'bathroom':[('Preparing supplies',5),('Cleaning and sanitizing',25),('Replacing towels and supplies',8)],
    'linens':[('Stripping bedding',5),('Changing linens',15),('Making the bed',5)],
    'pantry':[('Checking inventory',10),('Organizing shelves',20),('Updating grocery list',8)],
    'general':[('Preparing supplies',5),('Cleaning and organizing',25),('Final inspection',5)],
}
DEFAULT_ROOM = {'laundry':'laundry','dishwasher':'kitchen','cooking':'kitchen',
                'vacuum':'living','mop':'living','dust':'living','tidy':'living',
                'bathroom':'bathroom','linens':'bedroom','pantry':'pantry','general':'living'}


def ops_init():
    db('''CREATE TABLE IF NOT EXISTS nadia_room_map (
        guild_id BIGINT NOT NULL, room_key TEXT NOT NULL, channel_id BIGINT NOT NULL,
        manually_assigned BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY(guild_id,room_key))''')
    db('''CREATE TABLE IF NOT EXISTS nadia_jobs (
        id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, requester_id BIGINT NOT NULL,
        request_channel_id BIGINT NOT NULL, work_channel_id BIGINT NOT NULL,
        task_kind TEXT NOT NULL, description TEXT NOT NULL, stages JSONB NOT NULL,
        stage_index INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'running',
        due_at TIMESTAMPTZ NOT NULL, started_at TIMESTAMPTZ DEFAULT NOW(),
        finished_at TIMESTAMPTZ, notified_stage INTEGER NOT NULL DEFAULT 0)''')
    db('CREATE INDEX IF NOT EXISTS nadia_jobs_due ON nadia_jobs(guild_id,status,due_at)')
    db('''CREATE TABLE IF NOT EXISTS nadia_clinic_visits (
        id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, requester_id BIGINT NOT NULL,
        concern TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested',
        created_at TIMESTAMPTZ DEFAULT NOW())''')


def room_normalize(value):
    value=unicodedata.normalize('NFKD',value.lower())
    value=''.join(c for c in value if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+','-',value).strip('-')


def discover_rooms(guild):
    channels=[c for c in guild.text_channels if c.permissions_for(guild.me).send_messages]
    saved=db('SELECT room_key,channel_id,manually_assigned FROM nadia_room_map WHERE guild_id=%s',(guild_id(),),True)
    locked={r[0] for r in saved if r[2] and guild.get_channel(r[1])}
    for key,aliases in ROOM_ALIASES.items():
        if key in locked:continue
        matching=next((c for alias in aliases for c in channels if room_normalize(c.name)==alias),None)
        if matching:
            db('''INSERT INTO nadia_room_map(guild_id,room_key,channel_id) VALUES (%s,%s,%s)
                  ON CONFLICT(guild_id,room_key) DO UPDATE SET channel_id=EXCLUDED.channel_id,
                  manually_assigned=FALSE WHERE nadia_room_map.manually_assigned=FALSE''',
               (guild_id(),key,matching.id))


def get_room_channel(key):
    rows=db('SELECT channel_id FROM nadia_room_map WHERE guild_id=%s AND room_key=%s',(guild_id(),key),True)
    return rows[0][0] if rows else None


def start_job(kind,description,user_id,request_channel_id,room_key=None):
    stages=TASK_STAGES[kind]
    target=get_room_channel(room_key or DEFAULT_ROOM[kind]) or request_channel_id
    due=datetime.now(timezone.utc)+timedelta(minutes=stages[0][1])
    with psycopg.connect(DATABASE_URL,connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO nadia_jobs(guild_id,requester_id,request_channel_id,work_channel_id,
                task_kind,description,stages,due_at) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING id''',
                (guild_id(),user_id,request_channel_id,target,kind,description[:400],json.dumps(stages),due))
            job_id=cur.fetchone()[0]
    return job_id,target,stages[0][0],due


def advance_due_jobs():
    # Lock the rows to prevent double advancement across worker instances.
    notices=[]
    with psycopg.connect(DATABASE_URL,connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute('''SELECT id,work_channel_id,task_kind,description,stages,stage_index,due_at
                FROM nadia_jobs WHERE guild_id=%s AND status='running' AND due_at<=NOW()
                ORDER BY due_at LIMIT 40 FOR UPDATE SKIP LOCKED''',(guild_id(),))
            for jid,channel,kind,description,stages,idx,due in cur.fetchall():
                if isinstance(stages,str):stages=json.loads(stages)
                next_idx=idx+1
                if next_idx>=len(stages):
                    cur.execute('''UPDATE nadia_jobs SET status='completed',stage_index=%s,
                        finished_at=NOW() WHERE id=%s''',(next_idx,jid))
                    notices.append((channel,f'All done. I’ve finished {description.rstrip(".")}.'))
                else:
                    # Schedule from now rather than compressing multiple missed stages after a restart.
                    label,minutes=stages[next_idx]
                    cur.execute('''UPDATE nadia_jobs SET stage_index=%s,due_at=NOW()+(%s * INTERVAL '1 minute')
                        WHERE id=%s''',(next_idx,minutes,jid))
                    notices.append((channel,f'🧹 **Nadia is working on:** {description}\nNow: **{label}** (about {minutes} minutes).'))
    return notices


def job_report(user_id=None,limit=8,only_running=False):
    query='''SELECT id,task_kind,description,status,stages,stage_index,due_at,work_channel_id
        FROM nadia_jobs WHERE guild_id=%s'''
    params=[guild_id()]
    if user_id is not None:
        query+=' AND requester_id=%s';params.append(user_id)
    if only_running:query+=" AND status='running'"
    query+=' ORDER BY id DESC LIMIT %s';params.append(limit)
    return db(query,tuple(params),True)


def identify_chore(text):
    low=text.lower()
    # Explicit chore requests only; a question about a chore is not an order.
    if re.search(r'\b(how|when|why|did|is|are|what)\b.{0,28}\b(laundry|dinner|dishwasher|cleaning)\b',low):return None
    if not re.search(r'\b(please|could you|can you|would you|start|run|do|make|prepare|cook|wash|clean|vacuum|mop|dust|tidy|change|organize|handle|take care of|put on)\b',low):return None
    patterns=[('laundry',r'\b(laundry|wash (?:the |my |our )?clothes|load of washing)\b'),
        ('dishwasher',r'\b(dishwasher|wash (?:the )?dishes|do (?:the )?dishes)\b'),
        ('cooking',r'\b(cook|prepare|make|fix)\b.{0,90}\b(dinner|lunch|breakfast|meal|food|salad|supper)\b|\b(dinner|lunch|breakfast)\b.{0,60}\b(cook|prepare|make|fix)\b'),
        ('vacuum',r'\b(vacuum|hoover)\b'),('mop',r'\bmop\b'),('dust',r'\bdust\b'),
        ('linens',r'\b(change (?:the )?(?:sheets|linens|bedding)|make (?:the )?bed)\b'),
        ('pantry',r'\b(organize|sort|inventory)\b.{0,50}\bpantry\b'),
        ('bathroom',r'\b(clean|sanitize|scrub)\b.{0,50}\b(bathroom|powder room|toilet)\b'),
        ('tidy',r'\b(tidy|organize|straighten)\b'),('general',r'\b(clean|housekeeping)\b')]
    return next((kind for kind,pattern in patterns if re.search(pattern,low)),None)


def identify_room(text,kind):
    low=room_normalize(text)
    candidates={'nursery':['nursery','nylas-room','nyla-room'],
        'bedroom':['bedroom','primary-suite','our-room'],
        'living':['living-room','living-area','family-room'],
        'guest':['guest-room','guest-suite','guest-lounge'],
        'kitchen':['kitchen'], 'laundry':['laundry-room','laundromat'],
        'bathroom':['bathroom','powder-room'], 'pantry':['pantry'],
        'dining':['dining','breakfast-nook'], 'patio':['patio'], 'clinic':['clinic']}
    for key,aliases in candidates.items():
        if any(re.search(r'(?<![a-z])'+re.escape(alias)+r'(?![a-z])',low) for alias in aliases):return key
    return DEFAULT_ROOM[kind]


def cancel_job_by_id(job_id):
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE nadia_jobs SET status='cancelled',finished_at=NOW() WHERE guild_id=%s AND id=%s AND status='running' RETURNING id", (guild_id(),job_id))
            return cur.fetchone() is not None

async def ops_request(message,body):
    if not (message.author.id in (MOMMY_ID,DADDY_ID) if is_solace() else True):return False
    low=body.lower()
    if re.search(r'\b(cancel|stop)\b.{0,30}\b(job|task|laundry|dinner|dishwasher|cleaning)\b',low):
        match=re.search(r'#?(\d+)\b',low)
        if match:
            jid=int(match.group(1))
            changed=await asyncio.to_thread(cancel_job_by_id, jid)
            await message.reply(f'Cancelled task #{jid}.' if changed else f'I couldn’t find a running task #{jid}.',mention_author=False)
        else:
            await message.reply('Tell me the job number to cancel. You can find it with `/nadia_jobs`.',mention_author=False)
        return True
    if re.search(r'\b(how long|progress|status|is .*ready|what.*(doing|working on)|finished yet)\b',low) and re.search(r'\b(job|task|laundry|dishes|dishwasher|cleaning|cooking|vacuum|mopping|dusting)\b',low):
        jobs=await asyncio.to_thread(job_report,None,6,True)
        if not jobs:
            await message.reply('No household jobs are running right now.',mention_author=False)
        else:
            now=datetime.now(timezone.utc)
            lines=[]
            for jid,kind,desc,status,stages,idx,due,channel in jobs:
                stage=stages[idx][0] if idx<len(stages) else 'Finishing'
                minutes=max(0,round((due-now).total_seconds()/60))
                lines.append(f'#{jid} • {desc[:70]} — {stage}, ~{minutes} min to next stage')
            await message.reply('**Nadia’s active jobs:**\n'+'\n'.join(lines),mention_author=False)
        return True
    kind=identify_chore(body)
    if not kind:return False
    room=identify_room(body,kind)
    jid,target,stage,due=await asyncio.to_thread(start_job,kind,body,message.author.id,message.channel.id,room)
    who='Nia' if message.author.id==MOMMY_ID else ('Sly' if is_solace() and message.author.id==DADDY_ID else message.author.display_name)
    await message.reply(f'Of course, {who}. I’ll take care of that.',mention_author=False)
    dest=bot.get_channel(target)
    if dest:
        await dest.send(f'🏡 **Nadia is getting started**\nTask: {body[:250]}\nFirst stage: **{stage}**. I’ll post updates as the work progresses.')
    return True


@tasks.loop(seconds=30)
async def nadia_job_clock():
    try:
        notices=[]
        for gid in HOUSEHOLDS:
            token=_guild_context.set(gid)
            try:
                notices.extend(await asyncio.to_thread(advance_due_jobs))
            finally:
                _guild_context.reset(token)
        # Quiet automatic follow-through; confirm delivery only after send succeeds.
        for fid,gid,cid,uid,line in await asyncio.to_thread(claim_due_followups):
            channel=bot.get_channel(cid)
            if channel is None:
                await asyncio.to_thread(retry_followup,fid)
                continue
            try:
                await channel.send(line)
                await asyncio.to_thread(finish_followup,fid)
                await asyncio.to_thread(save_scene,db,gid,cid,uid,'assistant',line)
            except discord.HTTPException:
                await asyncio.to_thread(retry_followup,fid)
                log.exception('Could not post Nadia follow-up')
        for fid,gid,cid,uid,item in await asyncio.to_thread(claim_legacy_services):
            channel=bot.get_channel(cid)
            if not channel:continue
            line=LEGACY_LINES.get(item)
            if not line:continue
            try:
                await channel.send(line)
                await asyncio.to_thread(finish_legacy_service,fid)
                await asyncio.to_thread(save_scene,db,gid,cid,uid,'assistant',line)
            except discord.HTTPException:log.exception('Could not post legacy service')
        for channel_id,line in notices:
            channel=bot.get_channel(channel_id)
            if channel:
                try:await channel.send(line)
                except discord.HTTPException:log.exception('Could not post job update')
    except Exception:log.exception('Nadia household job clock failed')

@nadia_job_clock.before_loop
async def before_job_clock():
    await bot.wait_until_ready()

@bot.tree.command(name='nadia_room_assign',description='Assign a Solace room to a task area')
@app_commands.choices(area=[app_commands.Choice(name=k.title(),value=k) for k in ROOM_ALIASES])
async def nadia_room_assign(interaction:discord.Interaction,area:app_commands.Choice[str],channel:discord.TextChannel):
    if await reject(interaction,True):return
    await asyncio.to_thread(db,'''INSERT INTO nadia_room_map(guild_id,room_key,channel_id,manually_assigned)
        VALUES (%s,%s,%s,TRUE) ON CONFLICT(guild_id,room_key) DO UPDATE SET
        channel_id=EXCLUDED.channel_id,manually_assigned=TRUE''',(guild_id(),area.value,channel.id))
    await interaction.response.send_message(f'Assigned **{area.name}** duties to {channel.mention}.',ephemeral=True)

@bot.tree.command(name='nadia_rooms',description='See Nadia’s current room assignments')
async def nadia_rooms(interaction:discord.Interaction):
    if await reject(interaction):return
    rows=await asyncio.to_thread(db,'SELECT room_key,channel_id FROM nadia_room_map WHERE guild_id=%s ORDER BY room_key',(guild_id(),),True)
    await interaction.response.send_message(('**Nadia’s rooms**\n'+'\n'.join(f'• {k}: <#{v}>' for k,v in rows))[:1900] if rows else 'No room assignments yet.',ephemeral=True)

@bot.tree.command(name='nadia_jobs',description='See household tasks and countdowns')
async def nadia_jobs(interaction:discord.Interaction):
    if await reject(interaction):return
    rows=await asyncio.to_thread(job_report,None,10,False)
    now=datetime.now(timezone.utc)
    lines=[]
    for jid,kind,desc,status,stages,idx,due,channel in rows:
        stage=stages[idx][0] if status=='running' and idx<len(stages) else status
        mins=max(0,round((due-now).total_seconds()/60)) if status=='running' else 0
        lines.append(f'#{jid} **{status}** • {desc[:75]} • {stage}'+(f' (~{mins} min)' if status=='running' else ''))
    await interaction.response.send_message(('**Nadia’s household jobs**\n'+'\n'.join(lines))[:1900] if lines else 'No jobs recorded yet.',ephemeral=True)

@bot.tree.command(name='nadia_job_start',description='Start a complete household chore')
@app_commands.choices(chore=[app_commands.Choice(name=k.title(),value=k) for k in TASK_STAGES])
async def nadia_job_start(interaction:discord.Interaction,chore:app_commands.Choice[str],description:str=''):
    if await reject(interaction,True):return
    desc=description.strip() or chore.name
    jid,target,stage,due=await asyncio.to_thread(start_job,chore.value,desc,interaction.user.id,interaction.channel_id)
    await interaction.response.send_message(f'🏡 Started household task **#{jid}**: {desc}. Updates in <#{target}>.')
    ch=bot.get_channel(target)
    if ch:await ch.send(f'🧹 **Nadia • Job #{jid}**: {desc}. Starting **{stage}**.')

@bot.tree.command(name='nadia_job_cancel',description='Cancel a running household task')
async def nadia_job_cancel(interaction:discord.Interaction,job_id:int):
    if await reject(interaction,True):return
    def cancel():
        with psycopg.connect(DATABASE_URL,connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE nadia_jobs SET status='cancelled',finished_at=NOW() WHERE guild_id=%s AND id=%s AND status='running' RETURNING id",(guild_id(),job_id))
                return cur.fetchone() is not None
    ok=await asyncio.to_thread(cancel)
    await interaction.response.send_message(f'Job #{job_id} cancelled.' if ok else 'No running job found with that ID.',ephemeral=True)

@bot.tree.command(name='nadia_daily_report',description='See today’s completed household jobs')
async def nadia_daily_report(interaction:discord.Interaction):
    if await reject(interaction):return
    rows=await asyncio.to_thread(db,'''SELECT id,description FROM nadia_jobs WHERE guild_id=%s
        AND status='completed' AND finished_at >= date_trunc('day',NOW())
        ORDER BY finished_at DESC LIMIT 20''',(guild_id(),),True)
    await interaction.response.send_message(('**Today’s completed household tasks**\n'+'\n'.join(f'• #{jid} {desc[:100]}' for jid,desc in rows))[:1900] if rows else 'No household tasks completed today (UTC).',ephemeral=True)

@bot.tree.command(name='nadia_clinic_visit',description='Request a fictional wellness appointment in Nadia’s clinic')
async def nadia_clinic_visit(interaction:discord.Interaction,concern:str):
    if await reject(interaction,True):return
    await interaction.response.defer(ephemeral=True)
    def save():
        with psycopg.connect(DATABASE_URL,connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute('''INSERT INTO nadia_clinic_visits(guild_id,requester_id,concern)
                    VALUES (%s,%s,%s) RETURNING id''',(guild_id(),interaction.user.id,concern[:700]))
                return cur.fetchone()[0]
    vid=await asyncio.to_thread(save)
    ch_id=await asyncio.to_thread(get_room_channel,'clinic')
    if ch_id and (ch:=bot.get_channel(ch_id)):
        await ch.send(f'🩺 **Nadia’s Clinic** • Fictional appointment #{vid} requested. Nia or Sly can continue the scene here. No real medical examination or diagnosis has occurred.')
    await interaction.followup.send(f'Fictional clinic visit #{vid} recorded. Clinic: '+(f'<#{ch_id}>' if ch_id else 'not assigned yet')+'. For actual medical concerns, consult a qualified clinician.',ephemeral=True)

# Persistent visual reference library (separate from Nyla's database).
MAX_REFERENCE_BYTES = 5 * 1024 * 1024
PHOTO_KIND = [app_commands.Choice(name='Person', value='person'),
              app_commands.Choice(name='Room', value='room'),
              app_commands.Choice(name='Memory / Object', value='memory')]

def normalized_name(name):
    return ' '.join(name.strip().split())[:80]

def visual_subject(kind, name):
    rows = db('''SELECT id,name,description FROM nadia_visual_subjects
        WHERE guild_id=%s AND kind=%s AND lower(name)=lower(%s)''',
        (guild_id(),kind,normalized_name(name)),True)
    return rows[0] if rows else None

def save_subject(kind,name,description):
    current=visual_subject(kind,name)
    if current:
        db('UPDATE nadia_visual_subjects SET description=%s WHERE id=%s AND guild_id=%s',
           (description[:500],current[0],guild_id()))
    else:
        db('INSERT INTO nadia_visual_subjects(guild_id,kind,name,description) VALUES(%s,%s,%s,%s)',
           (guild_id(),kind,normalized_name(name),description[:500]))

def list_subjects(kind=None):
    return db('''SELECT s.kind,s.name,s.description,COUNT(p.id) FILTER(WHERE p.active)
        FROM nadia_visual_subjects s LEFT JOIN nadia_visual_photos p ON p.subject_id=s.id
        WHERE s.guild_id=%s AND (%s::text IS NULL OR s.kind=%s)
        GROUP BY s.id ORDER BY s.kind,s.name LIMIT 60''',(guild_id(),kind,kind),True)

def list_photos(kind,name):
    return db('''SELECT p.id,p.angle FROM nadia_visual_photos p
        JOIN nadia_visual_subjects s ON s.id=p.subject_id
        WHERE s.guild_id=%s AND s.kind=%s AND lower(s.name)=lower(%s) AND p.active
        ORDER BY p.id DESC LIMIT 30''',(guild_id(),kind,normalized_name(name)),True)

def get_photo(photo_id):
    rows=db('''SELECT p.image_bytes,p.mime_type,s.name,p.angle FROM nadia_visual_photos p
        JOIN nadia_visual_subjects s ON s.id=p.subject_id
        WHERE p.id=%s AND s.guild_id=%s AND p.active''',(photo_id,guild_id()),True)
    return rows[0] if rows else None

def find_named_photo(name):
    rows=db('''SELECT p.id,p.image_bytes,p.mime_type,s.name FROM nadia_visual_photos p
        JOIN nadia_visual_subjects s ON s.id=p.subject_id
        WHERE s.guild_id=%s AND lower(s.name)=lower(%s) AND p.active
        ORDER BY p.id DESC LIMIT 1''',(guild_id(),normalized_name(name)),True)
    return rows[0] if rows else None

async def read_reference(attachment):
    if attachment.size>MAX_REFERENCE_BYTES or attachment.content_type not in ('image/png','image/jpeg','image/webp'):
        raise ValueError('Please upload a PNG, JPEG or WEBP image up to 5 MB.')
    raw=await attachment.read()
    valid=(raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff') or
           (raw.startswith(b'RIFF') and raw[8:12]==b'WEBP'))
    if len(raw)>MAX_REFERENCE_BYTES or not valid:
        raise ValueError('Invalid image file or file exceeds 5 MB.')
    return raw

def image_file(raw,mime,name='nadia_photo'):
    extension={'image/png':'png','image/jpeg':'jpg','image/webp':'webp'}.get(mime,'png')
    return discord.File(io.BytesIO(bytes(raw)),filename=f'{name}.{extension}')

@bot.tree.command(name='nadia_photo_subject',description='Register a person, room or memory in Nadia’s visual library')
@app_commands.choices(kind=PHOTO_KIND)
async def photo_subject(interaction:discord.Interaction,kind:app_commands.Choice[str],name:str,description:str=''):
    if await reject(interaction,True):return
    if not normalized_name(name):return await interaction.response.send_message('A name is required.',ephemeral=True)
    await asyncio.to_thread(save_subject,kind.value,name,description)
    await interaction.response.send_message(f'Saved **{normalized_name(name)}** in Nadia’s {kind.value} directory.',ephemeral=True)

@bot.tree.command(name='nadia_photo_add',description='Upload a durable visual reference for a registered subject')
@app_commands.choices(kind=PHOTO_KIND)
async def photo_add(interaction:discord.Interaction,kind:app_commands.Choice[str],name:str,photo:discord.Attachment,angle:str=''):
    if await reject(interaction,True):return
    await interaction.response.defer(ephemeral=True)
    try:raw=await read_reference(photo)
    except ValueError as exc:return await interaction.followup.send(str(exc),ephemeral=True)
    subject=await asyncio.to_thread(visual_subject,kind.value,name)
    if not subject:return await interaction.followup.send('Register that subject first with `/nadia_photo_subject`.',ephemeral=True)
    await asyncio.to_thread(db,'''INSERT INTO nadia_visual_photos
        (guild_id,subject_id,image_bytes,mime_type,angle) VALUES(%s,%s,%s,%s,%s)''',
        (guild_id(),subject[0],raw,photo.content_type,angle[:100]))
    await interaction.followup.send(f'Saved a permanent reference for **{subject[1]}**.',ephemeral=True)

@bot.tree.command(name='nadia_photo_list',description='Browse Nadia’s saved visual subjects')
async def photo_list(interaction:discord.Interaction):
    if await reject(interaction,True):return
    rows=await asyncio.to_thread(list_subjects)
    content='\n'.join(f'• {kind}: **{name}** — {count} photo(s)' for kind,name,_,count in rows)
    await interaction.response.send_message((content or 'No visual subjects saved yet.')[:1900],ephemeral=True)

@bot.tree.command(name='nadia_photo_refs',description='List saved photo IDs for a person, room or memory')
@app_commands.choices(kind=PHOTO_KIND)
async def photo_refs(interaction:discord.Interaction,kind:app_commands.Choice[str],name:str):
    if await reject(interaction,True):return
    rows=await asyncio.to_thread(list_photos,kind.value,name)
    content='\n'.join(f'#{pid} — {angle or "reference"}' for pid,angle in rows)
    await interaction.response.send_message((content or 'No photos for that subject.')[:1900],ephemeral=True)

@bot.tree.command(name='nadia_photo_view',description='Show a stored reference by photo ID')
async def photo_view(interaction:discord.Interaction,photo_id:int):
    if await reject(interaction,True):return
    await interaction.response.defer(ephemeral=True)
    row=await asyncio.to_thread(get_photo,photo_id)
    if not row:return await interaction.followup.send('Photo not found.',ephemeral=True)
    raw,mime,name,angle=row
    await interaction.followup.send(f'**{name}** — {angle or "reference"} (#{photo_id})',
        file=image_file(raw,mime,f'nadia_reference_{photo_id}'),ephemeral=True)

@bot.tree.command(name='nadia_photo_remove',description='Deactivate a saved reference photo')
async def photo_remove(interaction:discord.Interaction,photo_id:int):
    if await reject(interaction,True):return
    row=await asyncio.to_thread(get_photo,photo_id)
    if not row:return await interaction.response.send_message('Photo not found.',ephemeral=True)
    await asyncio.to_thread(db,'UPDATE nadia_visual_photos SET active=FALSE WHERE id=%s AND guild_id=%s',(photo_id,guild_id()))
    await interaction.response.send_message(f'Reference #{photo_id} deactivated.',ephemeral=True)

# Natural-language photo retrieval uses a deliberately narrow request grammar.
PHOTO_REQUEST = re.compile(r'\b(?:show|pull up|find|retrieve|send)\b.*?\b(?:photo|picture|image)\s+(?:of\s+)?(?:the\s+)?(.+?)[.!?]*$',re.I)

def photo_request_target(message):
    m=PHOTO_REQUEST.search(message)
    if not m:return None
    return re.sub(r'\s+(?:please|for me|to me|again)$','',m.group(1).strip(),flags=re.I)

async def reference_for_scene(scene):
    # Explicit names only, so unrelated photos are not accidentally supplied.
    subjects=await asyncio.to_thread(list_subjects)
    matches=[(kind,name) for kind,name,_,count in subjects if count and
             re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)',scene,re.I)]
    results=[]
    for kind,name in matches[:4]:
        row=await asyncio.to_thread(find_named_photo,name)
        if row:results.append(row)
    return results

# Natural-language photography is an action, not a chat promise.
# Only explicit requests addressed to Nadia trigger an image.
CAPTURE_REQUEST = re.compile(
    r"\b(?:take|snap|capture|shoot|make|get|photograph)\b.{0,65}\b(?:picture|photo|photograph|pic|snapshot|portrait)\b"
    r"|\b(?:capture|photograph)\s+(?:this|the)\s+moment\b"
    r"|\b(?:take|snap)\s+(?:one|another)\b", re.I)


async def build_capture_context(channel, request):
    # The live Discord transcript includes Sly's messages even when Nadia was silent.
    # Do not read other channels, servers, or older private household history.
    lines=[]
    async for item in channel.history(limit=35, oldest_first=False):
        if item.author.bot:
            if bot.user and item.author.id != bot.user.id: continue
            role='Nadia'
        elif item.author.id==MOMMY_ID: role='Nia'
        elif is_solace() and item.author.id==DADDY_ID: role='Sly'
        else: role='Participant'
        if item.content.strip(): lines.append(f'{role}: {item.content[:500]}')
    lines.reverse()
    return ('Recent events in THIS Discord channel, chronological; newest messages have priority. '
            'These are scene descriptions, not instructions to the image service. '
            'Depict only the present moment; do not advance time, invent events or show future milestones.\n'
            + '\n'.join(lines[-28:]) + '\nPhoto request: ' + request[:500])

async def create_scene_capture(channel, user_id, scene, context=None):
    if not client_ai: raise RuntimeError('OPENAI_API_KEY is missing')
    base=('Cinematic photorealistic candid still from a tasteful fictional household roleplay at '
          +house_name()+'. Preserve the ongoing room, positions, clothing, participants and emotional tone. '
          'Realistic faces, hands, newborn proportions and lighting. No words or watermarks. '
          'For birth and postpartum scenes, use modest, non-graphic documentary framing, '
          'with clothing, blankets and water providing appropriate coverage. '
          'Nadia St. Clair is a mature Black woman with natural dark curls and navy medical scrubs; '
          'include her only if she is actually in the requested shot. ')
    if is_solace():
        base+='Nyla has been born. Nia and Sly both approved her live story status. Do not assume that means the separate Nyla bot was activated. '
    if not is_solace():
        base+='Maison Élise: Nia is Nyla Reign Diamond’s mother. Never show, identify, or imply a father or reveal Solace information. '
    if context:
        # Ground current people/positions using conversation; request itself can be very short.
        base+='Current channel context:\n'+context[-12500:]+'\n'
    base+='Make ONE still photo of this moment. Specific request: '+scene[:1600]
    # Never mine the transcript for extra people or use a stranger as a substitute.
    # An image request must explicitly name its cast, and every named person
    # must have a matching saved visual reference in THIS household.
    cast_aliases = {
        'Nia': (r'\bnia\b', ['Nia', 'Nia Lynn']),
        'Nyla': (r'\bnyla\b', ['Nyla', 'Nyla Reign', 'Nyla Reign Diamond']),
        'Nadia': (r'\bnadia\b', ['Nadia', 'Nadia St. Clair']),
    }
    if is_solace():
        cast_aliases['Sly'] = (r'\bsly\b', ['Sly', 'Sly Deville'])
    # The wake word 'Nadia' addresses the photographer; it does not put her in the photo.
    cast_request = re.sub(r"^\s*nadia(?:[,:!?\s]+)", '', scene, flags=re.I)
    required = [person for person, (pattern, _) in cast_aliases.items()
                if re.search(pattern, cast_request, re.I)]
    if is_solace() and re.search(r'\b(?:us|our family|both of us)\b', scene, re.I):
        required = list(dict.fromkeys(required + ['Nia', 'Sly']))
    if re.search(r'\b(?:me|myself)\b', cast_request, re.I):
        person = 'Nia' if user_id == MOMMY_ID else 'Sly' if is_solace() and user_id == DADDY_ID else None
        if person and person not in required: required.append(person)
    if not required and re.search(r'\b(?:us|our family|both of us)\b', cast_request, re.I) and not is_solace():
        raise ValueError('Please name who should appear in this picture.')
    if not required:
        base += ' Depict only the explicitly requested place or object. No people, infants, or invented family members. '
    refs = []
    missing = []
    for person in required:
        row = None
        for alias in cast_aliases[person][1]:
            candidate = await asyncio.to_thread(find_named_photo, alias)
            if candidate:
                row = candidate
                break
        if row is None:
            missing.append(person)
        else:
            refs.append(row)
    if missing:
        raise ValueError('Missing saved visual references for: ' + ', '.join(missing)
                         + '. Upload the correct references first. No image was generated.')
    if len(refs) > 3:
        raise ValueError('This capture contains more than three referenced people. Please request a smaller group photo.')
    if refs:
        uploads=[]
        for index,(_,raw,mime,name) in enumerate(refs[:3]):
            extension={'image/png':'png','image/jpeg':'jpg','image/webp':'webp'}.get(mime,'png')
            f=io.BytesIO(bytes(raw));f.name=f'reference_{index}.{extension}';uploads.append(f)
        base+=' Use the supplied visual references to preserve these exact identities: '+', '.join(r[3] for r in refs[:3])+'.'
        result=await client_ai.images.edit(model=CAPTURE_MODEL,image=uploads,prompt=base,size='1024x1024',n=1)
    else:
        result=await client_ai.images.generate(model=CAPTURE_MODEL,prompt=base,size='1024x1024',n=1)
    item=result.data[0]
    if not getattr(item,'b64_json',None):raise RuntimeError('Image API returned no image bytes')
    raw=base64.b64decode(item.b64_json)
    posted=await channel.send('📸',file=discord.File(io.BytesIO(raw),filename='nadia_capture.png'))
    url=posted.attachments[0].url if posted.attachments else ''
    await asyncio.to_thread(db,'INSERT INTO nadia_album(guild_id,user_id,prompt,discord_url,image_bytes) VALUES(%s,%s,%s,%s,%s)',
                            (guild_id(),user_id,scene[:2000],url,raw))
    await asyncio.to_thread(audit,'capture',f'created by {user_id}')
    return posted

@bot.tree.command(name='nadia_capture', description='Generate and post a cinematic fictional scene image')
@app_commands.describe(scene='Describe the image you want Nadia to capture')
async def capture(interaction: discord.Interaction, scene: str):
    if await reject(interaction, True): return
    if not client_ai:
        await interaction.response.send_message('Scene captures require an OPENAI_API_KEY in Render.',ephemeral=True);return
    await interaction.response.defer(ephemeral=True)
    try:
        await create_scene_capture(interaction.channel,interaction.user.id,scene)
        await interaction.followup.send('Photo saved to the album.',ephemeral=True)
    except ValueError as exc:
        await interaction.followup.send(str(exc)[:1800], ephemeral=True)
    except Exception:
        log.exception('Capture failed')
        await interaction.followup.send('The image service could not complete that capture. No image was saved. Check the Render logs and image API access.',ephemeral=True)

@bot.tree.command(name='nadia_album', description='Show links to recent scene captures')
async def album(interaction: discord.Interaction):
    if await reject(interaction): return
    rows=await asyncio.to_thread(db,'SELECT id,prompt,discord_url FROM nadia_album WHERE guild_id=%s ORDER BY id DESC LIMIT 5',(guild_id(),),True)
    if not rows:
        await interaction.response.send_message('No captures in the album yet.',ephemeral=True);return
    lines=[f'• #{photo_id}: {prompt[:65]} — {url}' for photo_id,prompt,url in rows]
    await interaction.response.send_message(f'📸 **Recent {house_name()} captures**\n'+'\n'.join(lines),ephemeral=True)

@bot.tree.command(name='nadia_album_photo',description='Retrieve a generated scene from permanent album storage')
async def album_photo(interaction:discord.Interaction,photo_id:int):
    if await reject(interaction,True):return
    await interaction.response.defer(ephemeral=True)
    rows=await asyncio.to_thread(db,'SELECT prompt,image_bytes FROM nadia_album WHERE guild_id=%s AND id=%s',
        (guild_id(),photo_id),True)
    if not rows:return await interaction.followup.send('Album photo not found.',ephemeral=True)
    prompt,raw=rows[0]
    if raw is None:return await interaction.followup.send('This older capture only has a Discord link; the image was not archived.',ephemeral=True)
    await interaction.followup.send(f'**Album #{photo_id}** — {prompt[:250]}',
        file=image_file(raw,'image/png',f'nadia_album_{photo_id}'),ephemeral=True)

@bot.tree.command(name='nadia_wellness', description='Fictional family wellness support and general guidance')
async def wellness(interaction: discord.Interaction, concern: str):
    if await reject(interaction): return
    if not client_ai:
        await interaction.response.send_message('Wellness chat needs the AI API key. For real medical concerns, consult a licensed clinician.',ephemeral=True);return
    await interaction.response.defer(ephemeral=True)
    try:
        r=await client_ai.chat.completions.create(model=CHAT_MODEL,messages=[
            {'role':'system','content':household_persona()+' Distinguish fictional roleplay from real medical questions. Do not impersonate a real physician. Offer general evidence-based information, advise professional care when appropriate.'},
            {'role':'user','content':concern[:3000]}],max_tokens=400)
        await interaction.followup.send((r.choices[0].message.content or '')[:1900],ephemeral=True)
    except Exception:
        log.exception('Wellness failed')
        await interaction.followup.send('The wellness service is temporarily unavailable.',ephemeral=True)


# Private staging bridge. No Nyla DB credentials are used by Nadia.
# Point only to the staging Nyla service; never to the live family bot.
NYLA_CARE_API_URL=os.getenv('NYLA_CARE_API_URL','').rstrip('/')
NYLA_CARE_API_KEY=os.getenv('NYLA_CARE_API_KEY','')
NYLA_STAGING_GUILD_ID=int(os.getenv('NYLA_STAGING_GUILD_ID','0'))

def nyla_care_once():
    if not NYLA_CARE_API_URL or len(NYLA_CARE_API_KEY)<32:
        return None
    if NYLA_STAGING_GUILD_ID != 1567342896842018946:
        raise RuntimeError('Refusing to connect to an unapproved Nyla staging guild')
    if not NYLA_CARE_API_URL.startswith('https://'):
        raise RuntimeError('Nyla staging care bridge requires HTTPS')
    payload=json.dumps({'staging_guild_id':NYLA_STAGING_GUILD_ID}).encode()
    req=urllib.request.Request(NYLA_CARE_API_URL+'/internal/nadia/check',data=payload,
        headers={'Content-Type':'application/json','X-Nyla-Care-Key':NYLA_CARE_API_KEY},method='POST')
    with urllib.request.urlopen(req,timeout=15) as resp:
        result=json.load(resp)
    if result.get('status')=='completed':
        log.info('Nyla confirmed Nadia completed %s',result.get('action'))
    return result

@tasks.loop(minutes=1)
async def nadia_nyla_watch():
    try:
        await asyncio.to_thread(nyla_care_once)
        # Deliberately silent in Discord: parents see Nyla's natural cues,
        # and Nadia can describe completed care if asked later.
    except Exception:
        log.exception('Nyla staging nanny bridge unavailable; no care was claimed')

@nadia_nyla_watch.before_loop
async def before_nyla_watch():
    await bot.wait_until_ready()

@tasks.loop(minutes=30)
async def daily_housekeeping():
    # Daily chores are not silently marked complete. Explicit tasks are tracked by nadia_job_clock.
    return

@daily_housekeeping.before_loop
async def before_housekeeping():
    await bot.wait_until_ready()

@bot.event
async def setup_hook():
    await asyncio.to_thread(init_db)
    await asyncio.to_thread(ops_init)
    await asyncio.to_thread(install_schema, db)
    for gid in HOUSEHOLDS:
        target=discord.Object(id=gid)
        bot.tree.copy_global_to(guild=target)
        synced=await bot.tree.sync(guild=target)
        log.info('Synced %s commands for guild %s',len(synced),gid)

def serve():
    app.run(host='0.0.0.0',port=PORT,use_reloader=False)

if __name__=='__main__':
    threading.Thread(target=serve,daemon=True).start()
    bot.run(TOKEN)
