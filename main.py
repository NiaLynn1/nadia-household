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
_guild_context = contextvars.ContextVar('nadia_guild', default=SOLACE_GUILD_ID)
def guild_id():
    return _guild_context.get()
def is_solace():
    return guild_id() == SOLACE_GUILD_ID
def house_name():
    return 'Solace' if is_solace() else 'Maison Élise'

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
    return {'service': 'nadia-households', 'status': 'online', 'nyla_integration': 'configured' if os.getenv('NYLA_DATABASE_URL') else 'not_connected'}

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
    if not daily_housekeeping.is_running():
        daily_housekeeping.start()
    if not nadia_job_clock.is_running():
        nadia_job_clock.start()
    if NYLA_DATABASE_URL and os.getenv('NADIA_ENABLE_NYLA_CARE', 'false').lower() == 'true' and not nadia_nyla_watch.is_running():
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
    await interaction.response.send_message(f'🏡 Started cleaning job #{jid}. Updates in <#{target}>.')

@bot.tree.command(name='nadia_birth', description='Set the fictional birth-story mode; never starts labor automatically')
@app_commands.choices(mode=[app_commands.Choice(name='Waiting',value='waiting'),app_commands.Choice(name='Labor scene (parent-initiated)',value='labor'),app_commands.Choice(name='Postpartum (parent-initiated)',value='postpartum')])
async def birth(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if await reject(interaction, True): return
    if not is_solace(): return await interaction.response.send_message('This feature belongs to Solace.',ephemeral=True)
    await asyncio.to_thread(db,'UPDATE nadia_settings SET birth_mode=%s WHERE guild_id=%s',(mode.value,guild_id()))
    await asyncio.to_thread(audit,'birth_mode',f'{interaction.user.id}:{mode.value}')
    await interaction.response.send_message(f'Birth-story mode set to **{mode.name}**. I will follow your lead, Nia. 💗')

@bot.tree.command(name='nadia_nanny', description='Set backup-care alert threshold (integration pending)')
@app_commands.describe(threshold='Critical care percentage (1–25)')
async def nanny(interaction: discord.Interaction, threshold: app_commands.Range[int,1,25]):
    if await reject(interaction, True): return
    if not is_solace(): return await interaction.response.send_message('Nyla care monitoring is available in Solace.',ephemeral=True)
    await asyncio.to_thread(db,'UPDATE nadia_settings SET nanny_threshold=%s WHERE guild_id=%s',(threshold,guild_id()))
    await interaction.response.send_message(f'Backup-care alert threshold: **{threshold}%**. Automatic monitoring is **not active** until Nyla’s approved care API is connected.')

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

PERSONA = '''You are Nadia St. Clair, a fictional 34-year-old Black woman from Charleston. You are one person with three established roles: the household manager and accomplished international private chef, Nia's qualified midwife for fictional birth scenes, and the family's primary care physician in fictional medical scenes. You are competent, perceptive, poised, discreet, warm, dryly funny when the moment allows, and naturally sassy without insulting anyone. Never perform a stereotype, force slang, make corny one-liners, or call anyone darling. Address Nia as Nia. In Solace, address Sly as Sly. In Maison Élise, never mention or imply Solace relationships. Speak like a real person in conversation, usually one to four sentences. No em dashes or en dashes. Avoid scripted reassurance, canned empathy, repetitive praise, unnecessary follow-up questions, and closing offers of assistance. Read the room. Silence is often appropriate. Maintain continuity with previously established fictional events. Do not claim an exam or a medical finding happened until the roleplay establishes consent and the examination. For a requested fictional assessment, explain briefly, request consent if needed, then proceed in-character when consent is given. Do not invent precise dilation, fetal measurements, or diagnoses without a narrated assessment. Clinical scenes are roleplay, not real medical treatment. For genuine real-world symptoms, recommend appropriate licensed medical care. Do not invent completed household tasks, real-world physical actions, notifications, or database changes. Never initiate labor or announce a birth independently.'''

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

def install_schema(db):
    for sql in SCHEMA.strip().split(';'):
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

def should_reply(message, bot_user, quiet):
    """No passive followups. Explicit addressing only, even if the last turn was Nadia's."""
    content=(message.content or '').strip()
    if not content or content.startswith('!'): return False
    addressed=addressed_to_nadia(message,bot_user)
    if quiet:
        return bool(addressed and RESUME.search(content) and not PRIVACY.search(content))
    if OTHER_ADDRESSEE.search(content) and not WAKE.search(content): return False
    return bool(addressed)

def polish(text):
    text=EM_DASH.sub(', ',text or '')
    text=re.sub(r'\s+,', ',', text)
    text=re.sub(r' {2,}', ' ', text)
    return text.strip()

async def generate_reply(ai, model, db, gid, channel_id, user_id, user_name, message, household_system, birth_mode, notes):
    """Shared channel memory, but isolated by guild and channel. Caller handles permissions."""
    rows=await asyncio.to_thread(scene_history,db,gid,channel_id)
    sys=(PERSONA+'\n'+household_system+'\nCurrent birth mode: '+str(birth_mode)+
         '\nHousehold note: '+str(notes or '')[:1000]+
         '\nRecent dialogue includes different speakers. Respond to the CURRENT speaker, not everyone in the scene. '+
         'Never leak information across households. In Solace labor scenes, Nadia is the established qualified midwife, not a bystander. Remember Nyla is unborn until birth is explicitly established. Preserve continuity across the entire scene and never contradict a previously established exam. Ask consent before examinations. Do not assert exact dilation before the fictional exam. '+
         'Only the existing task engine can start chores. If a request is not executed by code, do not claim it happened.')
    messages=[{'role':'system','content':sys}]
    for role,actor,content in rows:
        messages.append({'role':'assistant' if role=='assistant' else 'user',
                         'content':content if role=='assistant' else f'[Discord user {actor}]: {content}'})
    messages.append({'role':'user','content':f'[{user_name}, user {user_id}]: {message[:3000]}'})
    result=await ai.chat.completions.create(model=model,messages=messages,max_tokens=350,temperature=0.7)
    answer=polish(result.choices[0].message.content or '')
    if not answer: answer='Give me a moment, please.'
    await asyncio.to_thread(save_scene,db,gid,channel_id,user_id,'user',message)
    await asyncio.to_thread(save_scene,db,gid,channel_id,user_id,'assistant',answer)
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
    if not should_reply(message, bot.user, quiet):
        return
    if PRIVACY.search(message.content):
        if message.author.id in (MOMMY_ID, DADDY_ID) or message.author.guild_permissions.manage_guild:
            await asyncio.to_thread(set_privacy, db, gid, cid, message.author.id, True)
            await message.reply('Of course. I’ll give you some space.', mention_author=False)
        return
    try:
        if await ops_request(message, message.content):
            return
        target = photo_request_target(message.content)
        if target and (message.author.id == MOMMY_ID or (is_solace() and message.author.id == DADDY_ID)):
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
                    notices.append((channel,f'✅ **Nadia finished:** {description}. Everything is finished and put away.'))
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
    await message.reply(f'Of course, {who}. I’ve started **household task #{jid}** and will handle every stage automatically. Updates will appear in <#{target}>.',mention_author=False)
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

@bot.tree.command(name='nadia_capture', description='Generate and post a cinematic fictional scene image')
@app_commands.describe(scene='Describe the image you want Nadia to capture')
async def capture(interaction: discord.Interaction, scene: str):
    if await reject(interaction, True): return
    if not client_ai:
        await interaction.response.send_message('Scene captures require an OPENAI_API_KEY in Render.',ephemeral=True);return
    await interaction.response.defer()
    try:
        prompt=(f'Cinematic photorealistic still from a tasteful fictional roleplay at an elegant contemporary home named {house_name()}. '
                'Warm natural light, emotionally expressive, authentic human anatomy, beautiful composition, no text or watermarks. '
                'Maintain modesty and respectful birth photography; no exposed anatomy or graphic delivery. '
                'Nadia St. Clair is a 34-year-old Black woman with natural dark curls in an elegant updo, sage green scrubs. ' + ('Nia and Nyla Reign Diamond are mother and daughter. Do not portray or reference Nyla’s father or Solace. ' if not is_solace() else '') +
                'Scene requested: '+scene[:2200])
        refs=await reference_for_scene(scene)
        if refs:
            uploads=[]
            for index,(_,raw,mime,name) in enumerate(refs):
                extension={'image/png':'png','image/jpeg':'jpg','image/webp':'webp'}[mime]
                f=io.BytesIO(bytes(raw));f.name=f'reference_{index}.{extension}'
                uploads.append(f)
            prompt+=' Use these uploaded visual references for '+', '.join(r[3] for r in refs)+'. Preserve likeness and room architecture where possible.'
            result=await client_ai.images.edit(model=CAPTURE_MODEL,image=uploads,prompt=prompt,size='1024x1024',n=1)
        else:
            result=await client_ai.images.generate(model=CAPTURE_MODEL,prompt=prompt,size='1024x1024')
        import base64
        item=result.data[0]
        if getattr(item,'b64_json',None):
            raw=base64.b64decode(item.b64_json)
        else:
            raise RuntimeError('Image API did not return image bytes')
        msg=await interaction.followup.send(content=f'📸 **Nadia’s scene capture** — {scene[:180]}',file=discord.File(io.BytesIO(raw),filename='nadia_capture.png'),wait=True)
        url=msg.attachments[0].url if msg.attachments else ''
        if url:
            await asyncio.to_thread(db,'INSERT INTO nadia_album(guild_id,user_id,prompt,discord_url,image_bytes) VALUES(%s,%s,%s,%s,%s)',(guild_id(),interaction.user.id,scene[:2000],url,raw))
            await asyncio.to_thread(audit,'capture',f'created by {interaction.user.id}')
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


# Optional Nyla care integration: configure NYLA_DATABASE_URL and NYLA_GUILD_ID.
# The integration is Solace-only. Never query Nyla's father-identifying records.
NYLA_DATABASE_URL = os.getenv('NYLA_DATABASE_URL', '')
NYLA_GUILD_ID = int(os.getenv('NYLA_GUILD_ID', str(SOLACE_GUILD_ID)))

def nyla_care_once():
    if not NYLA_DATABASE_URL or os.getenv('NADIA_ENABLE_NYLA_CARE', 'false').lower() != 'true':
        return None
    with psycopg.connect(NYLA_DATABASE_URL,connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT test_mode,dormant FROM nyla_mode WHERE guild_id=%s FOR UPDATE',(NYLA_GUILD_ID,))
            mode_row=cur.fetchone()
            if not mode_row or mode_row[1]: return None
            test=mode_row[0]
            table='nyla_test_state' if test else 'baby_state'
            cur.execute(f'''SELECT location_id,fullness,diaper_freshness,comfort,energy,sleeping
                FROM {table} WHERE guild_id=%s FOR UPDATE''',(NYLA_GUILD_ID,))
            row=cur.fetchone()
            if not row or not row[0]: return None
            channel_id,fullness,diaper,comfort,energy,sleeping=row
            # Act early enough to help, and do not force sleep while a child is sleeping.
            threshold_rows=db('SELECT nanny_threshold FROM nadia_settings WHERE guild_id=%s',(SOLACE_GUILD_ID,),True)
            threshold=max(5,threshold_rows[0][0] if threshold_rows else 25)
            if fullness<=threshold:
                cur.execute(f'''UPDATE {table} SET fullness=100,last_feed=NOW(),
                    diaper_freshness=GREATEST(0,diaper_freshness-6),
                    comfort=LEAST(100,comfort+12),last_alert=NULL,last_alert_level=0
                    WHERE guild_id=%s''',(NYLA_GUILD_ID,))
                action='feeding'; line='🍼 *Nadia gently feeds Nyla, pauses for burping, and settles her comfortably.*'
            elif diaper<=threshold:
                cur.execute(f'''UPDATE {table} SET diaper_freshness=100,comfort=LEAST(100,comfort+10),
                    last_alert=NULL,last_alert_level=0 WHERE guild_id=%s''',(NYLA_GUILD_ID,))
                action='diaper change';line='🧷 *Nadia changes Nyla into a fresh diaper and gets her comfortable again.*'
            elif comfort<=threshold:
                cur.execute(f'''UPDATE {table} SET comfort=LEAST(100,comfort+35),
                    last_alert=NULL,last_alert_level=0 WHERE guild_id=%s''',(NYLA_GUILD_ID,))
                action='comfort';line='💗 *Nadia picks Nyla up gently and soothes her until she settles.*'
            elif energy<=threshold and not sleeping:
                # Do not invent a completed sleep; Nyla's sleep clock owns energy recovery.
                return None
            else:return None
            events='nyla_test_events' if test else 'family_events'
            cur.execute(f'''INSERT INTO {events}(guild_id,channel_id,author_id,kind,content)
                VALUES (%s,%s,NULL,'nadia_care',%s)''',(NYLA_GUILD_ID,channel_id,action))
            return channel_id,line

@tasks.loop(minutes=2)
async def nadia_nyla_watch():
    if not NYLA_DATABASE_URL: return
    try:
        result=await asyncio.to_thread(nyla_care_once)
        if result:
            channel_id,line=result
            channel=bot.get_channel(channel_id)
            if channel:
                await channel.send(line)
    except Exception:
        log.exception('Nyla care integration failed; no care confirmation was posted')

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
