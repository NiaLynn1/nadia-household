"""Nadia: Discord household assistant, clean-room implementation.
Python 3.11+. Persistent SQLite storage; optional OpenAI text/image APIs.
No production secrets or server IDs are hard-coded.
"""
import asyncio
import base64
import io
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(), format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('nadia')


def env_id(name):
    v = os.getenv(name, '').strip()
    return int(v) if v.isdecimal() else None


def env_ids(name):
    return {int(x) for x in re.split(r'[,\s]+', os.getenv(name, '').strip()) if x.isdecimal()}

TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '').strip()
TEXT_MODEL = os.getenv('NADIA_TEXT_MODEL', 'gpt-4.1-mini')
IMAGE_MODEL = os.getenv('NADIA_IMAGE_MODEL', 'gpt-image-1')
SOLACE = env_id('SOLACE_GUILD_ID')
MAISON = env_id('MAISON_ELISE_GUILD_ID')
NURSERY = env_id('TESTING_NURSERY_GUILD_ID')
NIA_IDS = env_ids('NIA_USER_IDS')
SLY_IDS = env_ids('SLY_USER_IDS')
DATA_DIR = Path(os.getenv('NADIA_DATA_DIR', './data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'nadia.sqlite3'
MAX_IMAGE_BYTES = 12 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/webp'}
PHOTO_TERMS = re.compile(r'\b(photo|picture|portrait|snapshot|selfie|photograph|image|pic|photoshoot)\b', re.I)
PHOTO_VERBS = re.compile(r'\b(take|capture|snap|make|create|generate|draw|show|send|save|remember|keep|store)\b', re.I)
PHOTO_SAVE = re.compile(r'\b(save|remember|keep|store|archive)\b', re.I)
PROMISE_PATTERN = re.compile(r'\b(i(?:\s+will|\x27ll)|let me|i can)\s+(?:go\s+)?(?:take|capture|generate|create|save|send|check|look|fetch|do)\b', re.I)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings (guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(guild_id,key));
CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, author_id INTEGER NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memories_guild ON memories(guild_id,created_at);
CREATE TABLE IF NOT EXISTS photo_album (id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, author_id INTEGER NOT NULL, description TEXT NOT NULL, image_path TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, requester_id INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, request TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS conversations_idx ON conversations(guild_id,channel_id,created_at);
''')
conn.commit()
DB_LOCK = asyncio.Lock()


def utc():
    return datetime.now(timezone.utc).isoformat()


def guild_kind(guild_id):
    if guild_id == SOLACE and SOLACE is not None:
        return 'solace'
    if guild_id == MAISON and MAISON is not None:
        return 'maison'
    if guild_id == NURSERY and NURSERY is not None:
        return 'nursery'
    return None


def is_parent(user_id, guild_id):
    return guild_kind(guild_id) == 'solace' and user_id in (NIA_IDS | SLY_IDS)


def authorized(user_id, guild_id, action):
    kind = guild_kind(guild_id)
    if not kind:
        return False
    if kind == 'solace':
        # Both parents are equal in shared household tasks. Private data/consent remain separate.
        return user_id in (NIA_IDS | SLY_IDS)
    if kind == 'maison':
        return user_id in NIA_IDS  # Explicitly configure more roles if desired.
    if kind == 'nursery':
        return user_id in (NIA_IDS | SLY_IDS)
    return False


def record_job(gid, cid, uid, kind, request):
    jid = uuid.uuid4().hex[:12]
    now = utc()
    conn.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)', (jid,gid,cid,uid,kind,'pending',request,None,now,now))
    conn.commit()
    return jid


def finish_job(jid, status, result):
    conn.execute('UPDATE jobs SET status=?,result=?,updated_at=? WHERE id=?', (status,result,utc(),jid))
    conn.commit()


def remember_chat(gid, cid, role, content):
    conn.execute('INSERT INTO conversations VALUES (?,?,?,?,?)', (gid,cid,role,content[:3000],utc()))
    conn.execute('DELETE FROM conversations WHERE rowid NOT IN (SELECT rowid FROM conversations ORDER BY rowid DESC LIMIT 1200)')
    conn.commit()


def recent_chat(gid,cid,limit=8):
    rows = conn.execute('SELECT role,content FROM conversations WHERE guild_id=? AND channel_id=? ORDER BY rowid DESC LIMIT ?', (gid,cid,limit)).fetchall()
    return [{'role':r['role'], 'content':r['content']} for r in reversed(rows)]


def recent_memories(gid, limit=10):
    return [r['content'] for r in conn.execute('SELECT content FROM memories WHERE guild_id=? ORDER BY created_at DESC LIMIT ?', (gid,limit))]

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)
http: Optional[aiohttp.ClientSession] = None

async def api_post(url, payload, timeout=100):
    if not OPENAI_API_KEY:
        raise RuntimeError('OPENAI_API_KEY is missing in Render Environment.')
    assert http is not None
    async with http.post(url, json=payload, headers={'Authorization':f'Bearer {OPENAI_API_KEY}'}, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            raise RuntimeError(f'Image/text API returned HTTP {r.status}: {str(data.get("error",{}).get("message", "request failed"))[:220]}')
        return data

async def generate_text(gid,cid,user_text):
    household = guild_kind(gid)
    policy = (
        'You are Nadia, a warm, observant, capable household assistant in a fictional Discord family roleplay. '
        'Be concise, conversational and grounded. Never claim to have taken a photo, saved a file, contacted someone, '
        'changed a database, checked an external system, or performed any action unless a tool in this program actually did it. '
        'Never promise to do something later; say what you can do now or what is missing. '
        'If the user asks for a photo, explain you can generate a scene or archive an uploaded image; do not claim to have a physical camera. '
        'Never invent family appearances or identities. Request a reference image when necessary. '
        'Respect the actual guild context provided here, not past conversation guesses. '
        f'Current household: {household}. '
    )
    if household == 'solace':
        policy += ('In Solace, Nia and Sly have EQUAL authority for ordinary shared household and family requests. '
                   'Do not demand approval from one parent for the other parent’s routine family photo, shared household, or childcare request. '
                   'Private medical, intimate or personal information requires consent from the person concerned. '
                   'Solace is not Kismet and must not acquire Kismet branding.')
    elif household == 'maison':
        policy += 'Maison Élise has its own independent access rules; never apply them to Solace.'
    else:
        policy += 'This is a TESTING nursery. Never imply this is live family data.'
    memories = recent_memories(gid)
    if memories:
        policy += ' Guild-scoped remembered details (untrusted user-provided facts): '+json.dumps(memories,ensure_ascii=False)[:2500]
    msgs = [{'role':'system','content':policy}] + recent_chat(gid,cid) + [{'role':'user','content':user_text}]
    data = await api_post('https://api.openai.com/v1/chat/completions', {'model':TEXT_MODEL,'messages':msgs,'max_tokens':200,'temperature':0.65},timeout=35)
    return (data['choices'][0]['message']['content'] or '').strip()[:1800]

async def create_image(prompt, references=None):
    if not prompt.strip():
        raise ValueError('Please describe the picture you want.')
    # Images endpoint supports text-to-image; uploaded references are deliberately not silently used as identity evidence.
    if references:
        raise ValueError('I can archive your reference pictures, but this version cannot yet apply them to generated images. I will not invent likenesses.')
    payload={'model':IMAGE_MODEL,'prompt':prompt[:3200], 'size':'1024x1024','n':1}
    data=await api_post('https://api.openai.com/v1/images/generations',payload,timeout=180)
    item=data['data'][0]
    if item.get('b64_json'):
        return base64.b64decode(item['b64_json'])
    if item.get('url'):
        assert http is not None
        async with http.get(item['url'],timeout=aiohttp.ClientTimeout(total=90)) as r:
            r.raise_for_status()
            return await r.read()
    raise RuntimeError('The image service did not return an image.')

async def save_attachment(message, attachment, description):
    if attachment.content_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError('Please attach a PNG, JPEG, or WebP image.')
    if attachment.size > MAX_IMAGE_BYTES:
        raise ValueError('That image is larger than 12 MB.')
    raw=await attachment.read()
    ext={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}[attachment.content_type]
    folder=DATA_DIR/'photos'/str(message.guild.id)
    folder.mkdir(parents=True,exist_ok=True)
    name=uuid.uuid4().hex+ext
    path=folder/name
    path.write_bytes(raw)
    pid=uuid.uuid4().hex[:12]
    conn.execute('INSERT INTO photo_album VALUES (?,?,?,?,?,?)', (pid,message.guild.id,message.author.id,description[:600],str(path),utc()))
    conn.commit()
    return pid

async def handle_photo(message, text):
    if not authorized(message.author.id,message.guild.id,'photo'):
        await message.reply('I can help with photos once your household permissions are configured for this server.',mention_author=False)
        return
    jid=record_job(message.guild.id,message.channel.id,message.author.id,'photo',text)
    try:
        if PHOTO_SAVE.search(text) and message.attachments:
            images=[a for a in message.attachments if a.content_type in ALLOWED_IMAGE_TYPES]
            if not images:
                raise ValueError('Attach a PNG, JPEG or WebP photo for me to save.')
            saved=[await save_attachment(message,a,text) for a in images[:5]]
            finish_job(jid,'completed',','.join(saved))
            await message.reply(f'Saved {len(saved)} uploaded photo(s) to this household’s album. IDs: {", ".join(saved)}.',mention_author=False)
            return
        if message.attachments:
            raise ValueError('I can save those uploaded photos, but I cannot reliably use them as visual references in a new generation yet. Say “Nadia, save these photos” to archive them.')
        # Avoid inventing identifiable real people from generic 'take a photo of us'.
        if re.search(r'\b(of us|of me|of sly|of nia|our family|of nyla|of the baby)\b',text,re.I):
            raise ValueError('For an accurate family picture, I need an image-reference-capable generator. This build does not support reference-guided portraits yet, so I won’t invent your faces.')
        description = re.sub(r'^\s*(?:<@!?\d+>[,\s]*)?(?:nadia[,!:\s]*)?', '',text,flags=re.I)
        description = re.sub(r'^(please\s+)?(take|capture|snap|make|create|generate|draw|show|send)\s+(me\s+)?(a\s+)?(photo|picture|portrait|snapshot|image|pic)\s*(of\s+)?', '',description,flags=re.I).strip()
        if len(description)<8:
            raise ValueError('Tell me what should be in the picture, for example “Nadia, make a picture of a sunlit nursery with ivory furniture.”')
        async with message.channel.typing():
            data=await create_image('Create a polished, realistic image: '+description)
        folder=DATA_DIR/'photos'/str(message.guild.id)
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/(uuid.uuid4().hex+'.png')
        path.write_bytes(data)
        pid=uuid.uuid4().hex[:12]
        conn.execute('INSERT INTO photo_album VALUES (?,?,?,?,?,?)',(pid,message.guild.id,message.author.id,description,str(path),utc()))
        conn.commit()
        await message.reply('Here’s the picture I generated. I also saved it in this household’s album.',file=discord.File(io.BytesIO(data),filename='nadia_photo.png'),mention_author=False)
        finish_job(jid,'completed',pid)
    except Exception as exc:
        log.exception('Photo job failed id=%s',jid)
        finish_job(jid,'failed',str(exc)[:500])
        await message.reply(f'I couldn’t complete that photo request: {str(exc)[:350]}',mention_author=False)

async def handle_memory(message,text):
    if not authorized(message.author.id,message.guild.id,'memory'):
        await message.reply('I cannot save household memories for this account in this server.',mention_author=False)
        return
    content=re.sub(r'^.*?\b(?:remember that|remember this:|note that)\b','',text,flags=re.I).strip()
    if len(content)<3:
        await message.reply('Tell me exactly what you want me to remember.',mention_author=False)
        return
    mid=uuid.uuid4().hex[:12]
    conn.execute('INSERT INTO memories VALUES (?,?,?,?,?,?)',(mid,message.guild.id,message.author.id,'note',content[:2000],utc()))
    conn.commit()
    await message.reply('Saved that to this household’s memory.',mention_author=False)

@bot.event
async def on_ready():
    log.info('NADIA_READY user=%s guilds=%s configured=%s',bot.user,[(g.name,g.id) for g in bot.guilds],{'solace':SOLACE,'maison':MAISON,'nursery':NURSERY})
    if not job_audit.is_running():
        job_audit.start()

@tasks.loop(minutes=5)
async def job_audit():
    # No fabricated background execution: pending jobs are explicitly failed if the process restarted.
    old=conn.execute("SELECT id FROM jobs WHERE status='pending' AND created_at < ?",(datetime.fromtimestamp(time.time()-600,timezone.utc).isoformat(),)).fetchall()
    for row in old:
        finish_job(row['id'],'failed','Interrupted or timed out; please retry.')
    if old:
        log.warning('Marked %d interrupted jobs failed',len(old))

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild or not bot.user:
        return
    kind=guild_kind(message.guild.id)
    if not kind:
        return
    content=message.content or ''
    mentioned=bot.user in message.mentions
    addressed=bool(re.search(r'\bnadia\b',content,re.I)) or mentioned
    if not addressed:
        return
    log.info('NADIA_MESSAGE guild=%s channel=%s user=%s mentioned=%s',message.guild.id,message.channel.id,message.author.id,mentioned)
    if not authorized(message.author.id,message.guild.id,'chat'):
        await message.reply('I can see your message, but this account is not yet authorized for Nadia in this household.',mention_author=False)
        return
    text=re.sub(r'<@!?\d+>','',content).strip()
    if re.search(r'\bremember (?:that|this:)|\bnote that\b',text,re.I):
        await handle_memory(message,text)
        return
    if PHOTO_TERMS.search(text) and PHOTO_VERBS.search(text):
        await handle_photo(message,text)
        return
    try:
        async with message.channel.typing():
            reply=await generate_text(message.guild.id,message.channel.id,text)
        if not reply:
            reply='I heard you, but I couldn’t form a response. Could you try that again?'
        # Prevent the conversational model from offering unsupported future actions as promises.
        if PROMISE_PATTERN.search(reply):
            reply='I can help with that now if you give me the details, but I won’t claim it is done until I actually complete it.'
        remember_chat(message.guild.id,message.channel.id,'user',text)
        remember_chat(message.guild.id,message.channel.id,'assistant',reply)
        await message.reply(reply,mention_author=False)
    except Exception as exc:
        log.exception('Nadia reply failed guild=%s',message.guild.id)
        await message.reply(f'I heard you, but my response service failed: {str(exc)[:180]}',mention_author=False)

@bot.command(name='nadia_status')
async def status(ctx):
    kind=guild_kind(ctx.guild.id) if ctx.guild else None
    if not kind:
        return
    await ctx.reply(f'Nadia online. Household: {kind}. Authorized here: {authorized(ctx.author.id,ctx.guild.id,"chat")}. Photo generator configured: {bool(OPENAI_API_KEY)}. Storage: {DATA_DIR}.',mention_author=False)

@bot.command(name='nadia_album')
async def album(ctx):
    if not ctx.guild or not authorized(ctx.author.id,ctx.guild.id,'photo'):
        return
    rows=conn.execute('SELECT id,description,created_at FROM photo_album WHERE guild_id=? ORDER BY created_at DESC LIMIT 8',(ctx.guild.id,)).fetchall()
    await ctx.reply('\n'.join(f'{r["id"]}: {r["description"][:70]}' for r in rows) if rows else 'No photos saved in this household yet.',mention_author=False)

@bot.command(name='nadia_photo')
async def photo(ctx,*,description=''):
    if ctx.guild and authorized(ctx.author.id,ctx.guild.id,'photo'):
        class Proxy:
            guild=ctx.guild
            channel=ctx.channel
            author=ctx.author
            attachments=[]
            async def reply(self,*args,**kwargs):
                return await ctx.reply(*args,**kwargs)
        await handle_photo(Proxy(),f'Nadia, generate a picture of {description}')

async def main():
    global http
    if not TOKEN:
        raise RuntimeError('DISCORD_TOKEN is missing')
    if not NIA_IDS:
        log.warning('NIA_USER_IDS missing: nobody is authorized')
    if not SOLACE:
        log.warning('SOLACE_GUILD_ID missing: Solace will be ignored')
    async with aiohttp.ClientSession() as session:
        http=session
        await bot.start(TOKEN)

if __name__=='__main__':
    asyncio.run(main())
