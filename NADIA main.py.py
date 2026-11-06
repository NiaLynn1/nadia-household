"""NADIA — Solace staging household companion.

Real Discord commands, optional AI chat/image capture, persistent PostgreSQL state.
No integration with Nyla's database; backup nanny is advisory only until approved API exists.
"""
import asyncio
import io
import json
import logging
import os
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
           'optional chef, backup nanny, and fictional wellness companion. Mommy and Daddy are primary parents. Never claim real cleaning, cooking, '
           'medical treatment or childcare happened. No diagnosis or prescribing; for real emergencies advise real emergency services. '
           'Do not initiate labor or birth on your own; follow parent-led story. Be concise, conversational, and in character.')

@bot.tree.command(name='nadia_talk', description='Talk with Nadia in character', guild=GUILD)
async def talk(interaction: discord.Interaction, message: str):
    if await reject(interaction): return
    if not client_ai:
        await interaction.response.send_message('My conversation API key has not been configured yet.',ephemeral=True);return
    await interaction.response.defer()
    try:
        _,_,birth_mode,_,notes=await asyncio.to_thread(setting)
        response=await client_ai.chat.completions.create(model=CHAT_MODEL,messages=[
            {'role':'system','content':PERSONA+f' Current birth mode: {birth_mode}. Household note: {notes[:1000]}'},
            {'role':'user','content':message[:3500]}],max_tokens=350)
        await interaction.followup.send(response.choices[0].message.content[:1900] or 'I’m here, darling.')
    except Exception:
        log.exception('AI chat failed')
        await interaction.followup.send('My conversation service is temporarily unavailable, darling. Please try again.',ephemeral=True)

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
            await asyncio.to_thread(db,'INSERT INTO nadia_album(guild_id,user_id,prompt,discord_url) VALUES(%s,%s,%s,%s)',(GUILD_ID,interaction.user.id,scene[:2000],url))
            await asyncio.to_thread(audit,'capture',f'created by {interaction.user.id}')
    except Exception:
        log.exception('Capture failed')
        await interaction.followup.send('The image service could not complete that capture. No image was saved. Check the Render logs and image API access.',ephemeral=True)

@bot.tree.command(name='nadia_album', description='Show links to recent scene captures', guild=GUILD)
async def album(interaction: discord.Interaction):
    if await reject(interaction): return
    rows=await asyncio.to_thread(db,'SELECT prompt,discord_url FROM nadia_album WHERE guild_id=%s ORDER BY id DESC LIMIT 5',(GUILD_ID,),True)
    if not rows:
        await interaction.response.send_message('No captures in the album yet.',ephemeral=True);return
    lines=[f'• {prompt[:65]} — {url}' for prompt,url in rows]
    await interaction.response.send_message('📸 **Recent Solace captures**\n'+'\n'.join(lines),ephemeral=True)

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
