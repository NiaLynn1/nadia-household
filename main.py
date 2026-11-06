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
GUILD_ID = int(os.environ['SOLACE_GUILD_ID'])
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
    return {'service': 'nadia-solace', 'status': 'online', 'nyla_integration': 'not_connected'}

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
    db('INSERT INTO nadia_settings (guild_id) VALUES (%s) ON CONFLICT DO NOTHING', (GUILD_ID,))

def audit(event, details):
    db('INSERT INTO nadia_events(guild_id,event_type,details) VALUES (%s,%s,%s)', (GUILD_ID,event,details[:1800]))

def setting():
    return db('SELECT chef_skip_date,cleaning_date,birth_mode,nanny_threshold,household_notes FROM nadia_settings WHERE guild_id=%s', (GUILD_ID,), True)[0]

def allowed(interaction):
    return interaction.guild_id == GUILD_ID

def parent(interaction):
    return allowed(interaction) and interaction.user.id in (MOMMY_ID,DADDY_ID)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
GUILD = discord.Object(id=GUILD_ID)

async def reject(interaction, parents=False):
    if not allowed(interaction) or (parents and not parent(interaction)):
        await interaction.response.send_message('This command is restricted to the Solace household.' if not allowed(interaction) else 'Only Mommy or Daddy can manage this.', ephemeral=True)
        return True
    return False

@bot.event
async def on_ready():
    log.info('NADIA online as %s', bot.user)
    if not daily_housekeeping.is_running():
        daily_housekeeping.start()

@bot.tree.command(name='nadia_status', description='Show Nadia’s household status', guild=GUILD)
async def status(interaction: discord.Interaction):
    if await reject(interaction): return
    skip, clean, birth, threshold, _ = await asyncio.to_thread(setting)
    today = datetime.now(timezone.utc).date().isoformat()
    await interaction.response.send_message(
        f'🏡 **Nadia St. Clair — Solace**\nBirth story: **{birth}**\n'
        f'Daily housekeeping: **{"completed" if clean==today else "pending"}**\n'
        f'Dinner: **{"Mommy has it" if skip==today else "available by request"}**\n'
        f'Nyla backup threshold: **{threshold}%** (advisory; not connected to Nyla)\n'
        'Scene captures: **available** with an image API key.', ephemeral=True)

@bot.tree.command(name='nadia_dinner', description='Tell Nadia you have dinner or request a menu', guild=GUILD)
@app_commands.describe(plan='Choose who handles dinner today')
@app_commands.choices(plan=[app_commands.Choice(name='I have dinner tonight',value='mommy'),app_commands.Choice(name='Nadia, suggest dinner',value='suggest'),app_commands.Choice(name='Reset dinner plan',value='reset')])
async def dinner(interaction: discord.Interaction, plan: app_commands.Choice[str]):
    if await reject(interaction, True): return
    today = datetime.now(timezone.utc).date().isoformat()
    if plan.value == 'mommy':
        await asyncio.to_thread(db,'UPDATE nadia_settings SET chef_skip_date=%s WHERE guild_id=%s',(today,GUILD_ID))
        reply='Of course, darling. Dinner is yours tonight. I’ll keep the kitchen tidy. ❤️'
    elif plan.value == 'reset':
        await asyncio.to_thread(db,'UPDATE nadia_settings SET chef_skip_date=NULL WHERE guild_id=%s',(GUILD_ID,))
        reply='Dinner plan reset. I’m available if needed.'
    else:
        reply='How about lemon-herb chicken, garlic mashed potatoes, roasted asparagus, and a warm peach crisp? You can tell me you have dinner whenever you like.'
    await interaction.response.send_message(reply)
    await asyncio.to_thread(audit,'dinner',f'{interaction.user.id}:{plan.value}')

@bot.tree.command(name='nadia_clean', description='Run a fictional Solace cleaning routine', guild=GUILD)
async def clean(interaction: discord.Interaction):
    if await reject(interaction, True): return
    today = datetime.now(timezone.utc).date().isoformat()
    await asyncio.to_thread(db,'UPDATE nadia_settings SET cleaning_date=%s WHERE guild_id=%s',(today,GUILD_ID))
    await asyncio.to_thread(audit,'cleaning',f'completed by request from {interaction.user.id}')
    await interaction.response.send_message('✨ Solace is refreshed: kitchen counters, living areas, floors, laundry, and nursery surfaces. All fictional household tasks complete.')

@bot.tree.command(name='nadia_birth', description='Set the fictional birth-story mode; never starts labor automatically', guild=GUILD)
@app_commands.choices(mode=[app_commands.Choice(name='Waiting',value='waiting'),app_commands.Choice(name='Labor scene (parent-initiated)',value='labor'),app_commands.Choice(name='Postpartum (parent-initiated)',value='postpartum')])
async def birth(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if await reject(interaction, True): return
    await asyncio.to_thread(db,'UPDATE nadia_settings SET birth_mode=%s WHERE guild_id=%s',(mode.value,GUILD_ID))
    await asyncio.to_thread(audit,'birth_mode',f'{interaction.user.id}:{mode.value}')
    await interaction.response.send_message(f'Birth-story mode set to **{mode.name}**. I will follow your lead, darling. 💗')

@bot.tree.command(name='nadia_nanny', description='Set backup-care alert threshold (integration pending)', guild=GUILD)
@app_commands.describe(threshold='Critical simulated care percentage (1–25)')
async def nanny(interaction: discord.Interaction, threshold: app_commands.Range[int,1,25]):
    if await reject(interaction, True): return
    await asyncio.to_thread(db,'UPDATE nadia_settings SET nanny_threshold=%s WHERE guild_id=%s',(threshold,GUILD_ID))
    await interaction.response.send_message(f'Backup-care alert threshold: **{threshold}%**. Automatic monitoring is **not active** until Nyla’s approved care API is connected.')

@bot.tree.command(name='nadia_note', description='Save a household continuity note', guild=GUILD)
async def note(interaction: discord.Interaction, detail: str):
    if await reject(interaction, True): return
    if len(detail)>1000:
        await interaction.response.send_message('Please keep notes under 1,000 characters.',ephemeral=True);return
    await asyncio.to_thread(db,'UPDATE nadia_settings SET household_notes=%s WHERE guild_id=%s',(detail,GUILD_ID))
    await asyncio.to_thread(audit,'household_note',f'updated by {interaction.user.id}')
    await interaction.response.send_message('Noted, darling. I’ll remember that for our household scenes.',ephemeral=True)

PERSONA = ('You are Nadia St. Clair, a fictional 34-year-old African-American woman from Charleston, South Carolina with Lowcountry roots. '
           'You are warm, polished, practical, softly witty, discreet, and calm. You are the Solace family’s fictional midwife, household manager, '
           'optional chef, backup nanny, and fictional wellness companion. Nia and Sly are Nyla Reign’s parents, not yours. Address them as Nia and Sly; use Mommy and Daddy only when talking about their roles to Nyla or speaking from Nyla’s perspective. Never claim real cleaning, cooking, '
           'medical treatment or childcare happened. No diagnosis or prescribing; for real emergencies advise real emergency services. '
           'Do not initiate labor or birth on your own; follow parent-led story. Be concise, conversational, and in character.')

def speaker_context(user_id):
    if user_id == MOMMY_ID:
        return 'The current speaker is Nia, Nyla’s mother. Address her as Nia.'
    if user_id == DADDY_ID:
        return 'The current speaker is Sly, Nyla’s father. Address him as Sly.'
    return 'The current speaker is a Solace server member, not necessarily a parent. Do not assume they are Nia or Sly.'

async def nadia_reply(user_id, message):
    if not client_ai:
        return 'My conversation service is not configured yet, darling.'
    _, _, birth_mode, _, notes = await asyncio.to_thread(setting)
    history = await asyncio.to_thread(db, '''SELECT role,content FROM (
        SELECT id,role,content FROM nadia_chat_history
        WHERE guild_id=%s AND user_id=%s ORDER BY id DESC LIMIT 12
    ) AS recent ORDER BY id ASC''', (GUILD_ID, user_id), True)
    system = (PERSONA + ' ' + speaker_context(user_id) +
              f' Current birth-story mode: {birth_mode}. Household note: {notes[:1000]}. '
              'A fictional labor scene must not change birth mode automatically. '
              'You only have access to features explicitly implemented in this bot.')
    messages = [{'role':'system','content':system}]
    messages.extend({'role':role,'content':content} for role,content in history)
    messages.append({'role':'user','content':message[:3000]})
    response = await client_ai.chat.completions.create(model=CHAT_MODEL,messages=messages,max_tokens=450)
    answer = (response.choices[0].message.content or 'I’m here, darling.').strip()
    await asyncio.to_thread(db, '''INSERT INTO nadia_chat_history(guild_id,user_id,role,content)
        VALUES (%s,%s,%s,%s),(%s,%s,%s,%s)''',
        (GUILD_ID,user_id,'user',message[:3000],GUILD_ID,user_id,'assistant',answer[:3500]))
    return answer

@bot.tree.command(name='nadia_talk', description='Talk with Nadia in character', guild=GUILD)
async def talk(interaction: discord.Interaction, message: str):
    if await reject(interaction): return
    await interaction.response.defer()
    try:
        answer = await nadia_reply(interaction.user.id,message)
        await interaction.followup.send(answer[:1900])
    except Exception:
        log.exception('AI chat failed')
        await interaction.followup.send('My conversation service is temporarily unavailable, darling. Please try again.',ephemeral=True)

# Wake on the name Nadia (case-insensitive) or a direct Discord mention in ANY Solace text channel.
NADIA_WAKE = re.compile(r'(?<![\w])nadia(?:[’\']s)?(?![\w])',re.IGNORECASE)
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None or message.guild.id != GUILD_ID:
        return
    if not (NADIA_WAKE.search(message.content) or (bot.user and bot.user.mentioned_in(message))):
        await bot.process_commands(message)
        return
    if not message.content.strip():
        return
    try:
        target=photo_request_target(message.content)
        if target and message.author.id in (MOMMY_ID,DADDY_ID):
            row=await asyncio.to_thread(find_named_photo,target)
            if row:
                pid,raw,mime,name=row
                await message.reply(f'Here is the saved picture of **{name}**, Nia.' if message.author.id==MOMMY_ID else
                    f'Here is the saved picture of **{name}**, Sly.',
                    file=image_file(raw,mime,f'nadia_reference_{pid}'),mention_author=False)
                return
        async with message.channel.typing():
            answer = await nadia_reply(message.author.id, message.content)
        await message.reply(answer[:1900], mention_author=False)
    except Exception:
        log.exception('Nadia name-triggered chat failed')
        await message.reply('I’m having trouble answering just now. Try again in a moment.',mention_author=False)
    await bot.process_commands(message)

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
        (GUILD_ID,kind,normalized_name(name)),True)
    return rows[0] if rows else None

def save_subject(kind,name,description):
    current=visual_subject(kind,name)
    if current:
        db('UPDATE nadia_visual_subjects SET description=%s WHERE id=%s AND guild_id=%s',
           (description[:500],current[0],GUILD_ID))
    else:
        db('INSERT INTO nadia_visual_subjects(guild_id,kind,name,description) VALUES(%s,%s,%s,%s)',
           (GUILD_ID,kind,normalized_name(name),description[:500]))

def list_subjects(kind=None):
    return db('''SELECT s.kind,s.name,s.description,COUNT(p.id) FILTER(WHERE p.active)
        FROM nadia_visual_subjects s LEFT JOIN nadia_visual_photos p ON p.subject_id=s.id
        WHERE s.guild_id=%s AND (%s::text IS NULL OR s.kind=%s)
        GROUP BY s.id ORDER BY s.kind,s.name LIMIT 60''',(GUILD_ID,kind,kind),True)

def list_photos(kind,name):
    return db('''SELECT p.id,p.angle FROM nadia_visual_photos p
        JOIN nadia_visual_subjects s ON s.id=p.subject_id
        WHERE s.guild_id=%s AND s.kind=%s AND lower(s.name)=lower(%s) AND p.active
        ORDER BY p.id DESC LIMIT 30''',(GUILD_ID,kind,normalized_name(name)),True)

def get_photo(photo_id):
    rows=db('''SELECT p.image_bytes,p.mime_type,s.name,p.angle FROM nadia_visual_photos p
        JOIN nadia_visual_subjects s ON s.id=p.subject_id
        WHERE p.id=%s AND s.guild_id=%s AND p.active''',(photo_id,GUILD_ID),True)
    return rows[0] if rows else None

def find_named_photo(name):
    rows=db('''SELECT p.id,p.image_bytes,p.mime_type,s.name FROM nadia_visual_photos p
        JOIN nadia_visual_subjects s ON s.id=p.subject_id
        WHERE s.guild_id=%s AND lower(s.name)=lower(%s) AND p.active
        ORDER BY p.id DESC LIMIT 1''',(GUILD_ID,normalized_name(name)),True)
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

@bot.tree.command(name='nadia_photo_subject',description='Register a person, room or memory in Nadia’s visual library',guild=GUILD)
@app_commands.choices(kind=PHOTO_KIND)
async def photo_subject(interaction:discord.Interaction,kind:app_commands.Choice[str],name:str,description:str=''):
    if await reject(interaction,True):return
    if not normalized_name(name):return await interaction.response.send_message('A name is required.',ephemeral=True)
    await asyncio.to_thread(save_subject,kind.value,name,description)
    await interaction.response.send_message(f'Saved **{normalized_name(name)}** in Nadia’s {kind.value} directory.',ephemeral=True)

@bot.tree.command(name='nadia_photo_add',description='Upload a durable visual reference for a registered subject',guild=GUILD)
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
        (GUILD_ID,subject[0],raw,photo.content_type,angle[:100]))
    await interaction.followup.send(f'Saved a permanent reference for **{subject[1]}**.',ephemeral=True)

@bot.tree.command(name='nadia_photo_list',description='Browse Nadia’s saved visual subjects',guild=GUILD)
async def photo_list(interaction:discord.Interaction):
    if await reject(interaction,True):return
    rows=await asyncio.to_thread(list_subjects)
    content='\n'.join(f'• {kind}: **{name}** — {count} photo(s)' for kind,name,_,count in rows)
    await interaction.response.send_message((content or 'No visual subjects saved yet.')[:1900],ephemeral=True)

@bot.tree.command(name='nadia_photo_refs',description='List saved photo IDs for a person, room or memory',guild=GUILD)
@app_commands.choices(kind=PHOTO_KIND)
async def photo_refs(interaction:discord.Interaction,kind:app_commands.Choice[str],name:str):
    if await reject(interaction,True):return
    rows=await asyncio.to_thread(list_photos,kind.value,name)
    content='\n'.join(f'#{pid} — {angle or "reference"}' for pid,angle in rows)
    await interaction.response.send_message((content or 'No photos for that subject.')[:1900],ephemeral=True)

@bot.tree.command(name='nadia_photo_view',description='Show a stored reference by photo ID',guild=GUILD)
async def photo_view(interaction:discord.Interaction,photo_id:int):
    if await reject(interaction,True):return
    await interaction.response.defer(ephemeral=True)
    row=await asyncio.to_thread(get_photo,photo_id)
    if not row:return await interaction.followup.send('Photo not found.',ephemeral=True)
    raw,mime,name,angle=row
    await interaction.followup.send(f'**{name}** — {angle or "reference"} (#{photo_id})',
        file=image_file(raw,mime,f'nadia_reference_{photo_id}'),ephemeral=True)

@bot.tree.command(name='nadia_photo_remove',description='Deactivate a saved reference photo',guild=GUILD)
async def photo_remove(interaction:discord.Interaction,photo_id:int):
    if await reject(interaction,True):return
    row=await asyncio.to_thread(get_photo,photo_id)
    if not row:return await interaction.response.send_message('Photo not found.',ephemeral=True)
    await asyncio.to_thread(db,'UPDATE nadia_visual_photos SET active=FALSE WHERE id=%s AND guild_id=%s',(photo_id,GUILD_ID))
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

@bot.tree.command(name='nadia_capture', description='Generate and post a cinematic fictional scene image', guild=GUILD)
@app_commands.describe(scene='Describe the image you want Nadia to capture')
async def capture(interaction: discord.Interaction, scene: str):
    if await reject(interaction, True): return
    if not client_ai:
        await interaction.response.send_message('Scene captures require an OPENAI_API_KEY in Render.',ephemeral=True);return
    await interaction.response.defer()
    try:
        prompt=('Cinematic photorealistic still from a tasteful fictional family roleplay at an elegant contemporary home named Solace. '
                'Warm natural light, emotionally expressive, authentic human anatomy, beautiful composition, no text or watermarks. '
                'Maintain modesty and respectful birth photography; no exposed anatomy or graphic delivery. '
                'Nadia St. Clair is a 34-year-old Black woman with natural dark curls in an elegant updo, sage green scrubs. '
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
            await asyncio.to_thread(db,'INSERT INTO nadia_album(guild_id,user_id,prompt,discord_url,image_bytes) VALUES(%s,%s,%s,%s,%s)',(GUILD_ID,interaction.user.id,scene[:2000],url,raw))
            await asyncio.to_thread(audit,'capture',f'created by {interaction.user.id}')
    except Exception:
        log.exception('Capture failed')
        await interaction.followup.send('The image service could not complete that capture. No image was saved. Check the Render logs and image API access.',ephemeral=True)

@bot.tree.command(name='nadia_album', description='Show links to recent scene captures', guild=GUILD)
async def album(interaction: discord.Interaction):
    if await reject(interaction): return
    rows=await asyncio.to_thread(db,'SELECT id,prompt,discord_url FROM nadia_album WHERE guild_id=%s ORDER BY id DESC LIMIT 5',(GUILD_ID,),True)
    if not rows:
        await interaction.response.send_message('No captures in the album yet.',ephemeral=True);return
    lines=[f'• #{photo_id}: {prompt[:65]} — {url}' for photo_id,prompt,url in rows]
    await interaction.response.send_message('📸 **Recent Solace captures**\n'+'\n'.join(lines),ephemeral=True)

@bot.tree.command(name='nadia_album_photo',description='Retrieve a generated scene from permanent album storage',guild=GUILD)
async def album_photo(interaction:discord.Interaction,photo_id:int):
    if await reject(interaction,True):return
    await interaction.response.defer(ephemeral=True)
    rows=await asyncio.to_thread(db,'SELECT prompt,image_bytes FROM nadia_album WHERE guild_id=%s AND id=%s',
        (GUILD_ID,photo_id),True)
    if not rows:return await interaction.followup.send('Album photo not found.',ephemeral=True)
    prompt,raw=rows[0]
    if raw is None:return await interaction.followup.send('This older capture only has a Discord link; the image was not archived.',ephemeral=True)
    await interaction.followup.send(f'**Album #{photo_id}** — {prompt[:250]}',
        file=image_file(raw,'image/png',f'nadia_album_{photo_id}'),ephemeral=True)

@bot.tree.command(name='nadia_wellness', description='Fictional family wellness support and general guidance', guild=GUILD)
async def wellness(interaction: discord.Interaction, concern: str):
    if await reject(interaction): return
    if not client_ai:
        await interaction.response.send_message('Wellness chat needs the AI API key. For real medical concerns, consult a licensed clinician.',ephemeral=True);return
    await interaction.response.defer(ephemeral=True)
    try:
        r=await client_ai.chat.completions.create(model=CHAT_MODEL,messages=[
            {'role':'system','content':PERSONA+' Distinguish fictional roleplay from real medical questions. Do not impersonate a real physician. Offer general evidence-based information, advise professional care when appropriate.'},
            {'role':'user','content':concern[:3000]}],max_tokens=400)
        await interaction.followup.send((r.choices[0].message.content or '')[:1900],ephemeral=True)
    except Exception:
        log.exception('Wellness failed')
        await interaction.followup.send('The wellness service is temporarily unavailable.',ephemeral=True)

@tasks.loop(minutes=30)
async def daily_housekeeping():
    now=datetime.now(timezone.utc)
    # One daily fictional report, UTC date; no action on real-world home.
    today=now.date().isoformat()
    try:
        _,done,_,_,_=await asyncio.to_thread(setting)
        if done==today:return
        channel_id=os.getenv('HOUSEHOLD_CHANNEL_ID','')
        channel=bot.get_channel(int(channel_id)) if channel_id.isdigit() else None
        if not channel:return
        await channel.send('✨ **Nadia’s daily Solace refresh:** living spaces tidied, kitchen refreshed, laundry organized, and nursery checked. Fictional household routine complete. ❤️')
        await asyncio.to_thread(db,'UPDATE nadia_settings SET cleaning_date=%s WHERE guild_id=%s',(today,GUILD_ID))
        await asyncio.to_thread(audit,'daily_cleaning','daily scheduled fictional report')
    except Exception:
        log.exception('Daily housekeeping failed')

@daily_housekeeping.before_loop
async def before_housekeeping():
    await bot.wait_until_ready()

@bot.event
async def setup_hook():
    await asyncio.to_thread(init_db)
    synced=await bot.tree.sync(guild=GUILD)
    log.info('Synced %s Solace slash commands',len(synced))

def serve():
    app.run(host='0.0.0.0',port=PORT,use_reloader=False)

if __name__=='__main__':
    threading.Thread(target=serve,daemon=True).start()
    bot.run(TOKEN)
