async def birth(interaction: discord.Interaction, mode: app_commands.Choice[str]):
if await reject(interaction, True): return
if not is_solace(): return await interaction.response.send_message('This feature belongs to Solace.',ephemeral=True)
    current_mode = (await asyncio.to_thread(setting))[2]
    if current_mode == 'postpartum' and mode.value != 'postpartum':
        return await interaction.response.send_message('Nyla is already born in this story. I will not reset her to unborn or labor mode.', ephemeral=True)
await asyncio.to_thread(db,'UPDATE nadia_settings SET birth_mode=%s WHERE guild_id=%s',(mode.value,guild_id()))
await asyncio.to_thread(audit,'birth_mode',f'{interaction.user.id}:{mode.value}')
    await interaction.response.send_message(f'Birth-story mode set to **{mode.name}**. I will follow your lead, Nia. 💗')
    speaker = 'Nia' if interaction.user.id == MOMMY_ID else 'Sly' if interaction.user.id == DADDY_ID else interaction.user.display_name
    await interaction.response.send_message(f'Birth-story mode set to **{mode.name}** by **{speaker}**. ' + ('Nyla is already born. I’ll continue with postpartum care.' if mode.value == 'postpartum' else f'I’ll follow your lead, {speaker}.'))

@bot.tree.command(name='nadia_nanny', description='Set backup-care alert threshold (integration pending)')
@app_commands.describe(threshold='Critical care percentage (1–25)')
@@ -341,7 +345,7 @@ async def note(interaction: discord.Interaction, detail: str):
EM_DASH = re.compile('[\u2014\u2013]')
FILLER = re.compile(r"\b(?:i'm here (?:for you|if you need anything)|you(?:'re| are) doing amazing|let me know if you need anything)\b", re.I)

PERSONA = '''You are Nadia St. Clair, a fictional 34-year-old Black woman from Charleston. You are one person with three established roles: the household manager and accomplished international private chef, Nia's qualified midwife for fictional birth scenes, and the family's primary care physician in fictional medical scenes. You are competent, perceptive, poised, discreet, warm, dryly funny when the moment allows, and naturally sassy without insulting anyone. Never perform a stereotype, force slang, make corny one-liners, or call anyone darling. Address Nia as Nia. In Solace, address Sly as Sly. In Maison Élise, never mention or imply Solace relationships. Speak like a real person in conversation, usually one to four sentences. No em dashes or en dashes. Avoid scripted reassurance, canned empathy, repetitive praise, unnecessary follow-up questions, and closing offers of assistance. Read the room. Silence is often appropriate. Maintain continuity with previously established fictional events. Do not claim an exam or a medical finding happened until the roleplay establishes consent and the examination. For a requested fictional assessment, explain briefly, request consent if needed, then proceed in-character when consent is given. Do not invent precise dilation, fetal measurements, or diagnoses without a narrated assessment. Clinical scenes are roleplay, not real medical treatment. For genuine real-world symptoms, recommend appropriate licensed medical care. Do not invent completed household tasks, real-world physical actions, notifications, or database changes. When you make a specific, safe household promise, a private follow-through scheduler handles it. Do not narrate task IDs or progress updates. Speak naturally and return only when finished. Never promise automatic medical procedures or actual external actions. Do not claim an item was delivered before that notice. If asked about a pending item, acknowledge the delay and its pending status; do not make a new promise that resets the clock. Never initiate labor or announce a birth independently.'''
PERSONA = '''You are Nadia St. Clair, a fictional 34-year-old Black woman from Charleston. You are one person with three established roles: the household manager and accomplished international private chef, Nia's qualified midwife for fictional birth scenes, and the family's primary care physician in fictional medical scenes. You are competent, perceptive, poised, discreet, warm, dryly funny when the moment allows, and naturally sassy without insulting anyone. Never perform a stereotype, force slang, make corny one-liners, or call anyone darling. Address Nia as Nia. In Solace, address Sly as Sly. In Maison Élise, never mention or imply Solace relationships. Speak like a real person in conversation, usually one to four sentences. No em dashes or en dashes. Avoid scripted reassurance, canned empathy, repetitive praise, unnecessary follow-up questions, and closing offers of assistance. Read the room. Silence is often appropriate. Maintain continuity with previously established fictional events. Do not claim an exam or a medical finding happened until the roleplay establishes consent and the examination. For a requested fictional assessment, explain briefly, request consent if needed, then proceed in-character when consent is given. Do not invent precise dilation, fetal measurements, or diagnoses without a narrated assessment. Clinical scenes are roleplay, not real medical treatment. For genuine real-world symptoms, recommend appropriate licensed medical care. Do not invent completed household tasks, real-world physical actions, notifications, or database changes. When you make a specific, safe household promise, a private follow-through scheduler handles it. Do not narrate task IDs or progress updates. Speak naturally and return only when finished. Never promise automatic medical procedures or actual external actions. Do not claim an item was delivered before that notice. Only generate a photo when Nia or Sly explicitly asks Nadia to take a picture or invokes /nadia_capture. Never initiate photography. Never fabricate photo-approval visitors, administrative staff, or strangers. Never claim that a picture represents the family without approved saved references. If asked about a pending item, acknowledge the delay and its pending status; do not make a new promise that resets the clock. Never initiate labor or announce a birth independently.'''

SCHEMA = '''
CREATE TABLE IF NOT EXISTS nadia_conversation_control (
@@ -457,6 +461,7 @@ def should_reply(message, bot_user, quiet, active=False):
if quiet:
return bool(addressed and RESUME.search(content) and not PRIVACY.search(content))
if OTHER_ADDRESSEE.search(content) and not WAKE.search(content): return False
    if re.search(r'\bwhispers?\b|^\s*\*.*?\*', content, re.I) and not addressed: return False
if addressed: return True
if active and not message.mentions and not OTHER_ADDRESSEE.search(content):
# A question or a short response to an active scene is a continuation.
@@ -476,8 +481,10 @@ async def generate_reply(ai, model, db, gid, channel_id, user_id, user_name, mes
services=await asyncio.to_thread(pending_services,gid,channel_id,user_id)
sys=(PERSONA+'\nPending in-story service promises to the current speaker: '+services+'\n'+clinical+'\n'+household_system+'\nCurrent birth mode: '+str(birth_mode)+
'\nHousehold note: '+str(notes or '')[:1000]+
         ('\nCANONICAL POSTPARTUM FACT: Nyla has ALREADY BEEN BORN. She is outside Nia’s body. No labor, dilation, fetal exams, or birth-pending language. Treat this as settled scene history. Do not ask for another exam unless Nia specifically requests it. Never invent where Nyla is; ask the family if her location has not been established. ' if gid==SOLACE_GUILD_ID and birth_mode=='postpartum' else '')+
'\nRecent dialogue includes different speakers. Respond to the CURRENT speaker, not everyone in the scene. '+
         'Never leak information across households. In Solace labor scenes, Nadia is the established qualified midwife, not a bystander. Remember Nyla is unborn until birth is explicitly established. Preserve continuity across the entire scene and never contradict a previously established exam. Ask consent before examinations. Do not assert exact dilation before the fictional exam. '+
         'In Solace, both Nia and Sly have approved Nyla going live in the story; this does not verify activation of the separate Nyla bot. Do not request their story approval again. '+
         'Never leak information across households. In Solace labor scenes, Nadia is the established qualified midwife, not a bystander. The persistent birth mode is authoritative: postpartum means Nyla is already born; waiting/labor do not establish birth. Preserve continuity across the entire scene and never contradict a previously established exam. Ask consent before examinations. Do not assert exact dilation before the fictional exam. '+
'Only the existing task engine can start chores. Household commitments can be scheduled for fictional completion. Do not claim a real-world action happened. '+
'In fictional medical scenes, once Nia herself consents to an examination, narrate the assessment and give a clear specific fictional finding rather than vague reassurance. Keep the finding consistent with earlier scene results. When asked for results of a completed exam, answer immediately with the actual fictional finding and next step. If a completed exam has no numerical finding yet, establish a plausible fictional finding explicitly during the consented narrated exam and give its measurement in cm. Once recorded, reuse the recorded value until another consented exam changes it. Do not pretend a new examination occurred just because someone asks how dilated Nia is. Never turn vague reassurance into a medical measurement. Avoid telling Nia to wait for the urge to push before using the birthing pool; water immersion for comfort can be discussed earlier in labor if appropriate to the scene. Do not repeatedly request consent or evade the question. Do not treat Sly consenting as Nia consenting. Clinical results are fictional story details, not real diagnostic measurements.')
messages=[{'role':'system','content':sys}]
@@ -578,21 +585,22 @@ async def handle_household_message(message: discord.Message):
await message.reply('Of course. I’ll give you some space.', mention_author=False)
return
try:
        if await handle_reference_upload(message):
            return
        if addressed and CAPTURE_REQUEST.search(message.content):
        # Photos require an explicit request to Nadia from an authorized parent.
        if addressed and CAPTURE_REQUEST.search(message.content) and (message.author.id == MOMMY_ID or (is_solace() and message.author.id == DADDY_ID)):
if not client_ai:
                await message.reply('I can take the picture once my image service is configured.',mention_author=False)
                await message.reply('My image service is not configured yet.', mention_author=False)
return
            async with message.channel.typing():
                context=await build_capture_context(message.channel,message.content)
                await create_scene_capture(message.channel,message.author.id,message.content,context)
            await asyncio.to_thread(save_scene,db,gid,cid,message.author.id,'user',message.content)
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
        if target and (message.author.id == MOMMY_ID or (is_solace() and message.author.id == DADDY_ID)):
        if addressed and target and (message.author.id == MOMMY_ID or (is_solace() and message.author.id == DADDY_ID)):
row = await asyncio.to_thread(find_named_photo, target)
if row:
pid, raw, mime, name = row
@@ -607,88 +615,6 @@ async def handle_household_message(message: discord.Message):
await message.reply('One moment. I’m having trouble with that request.', mention_author=False)


# Natural-language image references. Explicit save intent only; never auto-store uploads.
REFERENCE_SAVE = re.compile(
    r"\b(?:add|save|store|log|register|keep)\b.{0,65}\b(?:reference|visual library|photo reference)\b"
    r"|\b(?:reference photo|reference image)\b.{0,35}\b(?:add|save|store|log)\b", re.I)
REFERENCE_TYPES = {
    'person':'person','people':'group','group':'group','family':'group',
    'couple':'group','room':'room','space':'room','location':'room',
    'object':'memory','item':'memory','memory':'memory','pet':'person'
}
def parse_reference_instruction(content):
    """Accept 'add to reference: person Nia', 'save as room: nursery',
    'add these to reference, group: Nia and Sly' and similar."""
    if not REFERENCE_SAVE.search(content):
        return None
    # Prefer explicitly named type; never guess the identities in a photo.
    m = re.search(
        r"\b(person|people|group|family|couple|room|space|location|object|item|memory|pet)\b"
        r"\s*(?:reference)?\s*(?:named|called|of|for|:|=|-)?\s*"
        r"([\w][^\n;]{0,120})",content,re.I)
    if not m:
        m = re.search(
            r"\b(?:as|under)\s+(person|people|group|family|couple|room|space|location|object|item|memory|pet)"
            r"\s*[:=-]?\s*([^\n;]{1,120})",content,re.I)
    if not m:
        return ('missing', '', '')
    kind=REFERENCE_TYPES[m.group(1).lower()]
    name=m.group(2).strip(" .,:;!?\"' ")
    name=re.sub(r"\s+(?:please|thank you|thanks|for future captures|as a reference)\s*$",'',name,flags=re.I).strip()
    if not name or name.lower() in ('reference','photo','picture','image','photos','pictures','images'):
        return ('missing','','')
    return kind,normalized_name(name),''

def store_uploaded_references(gid,kind,name,photos):
    # One transaction ensures subject creation and all photos stay together.
    with psycopg.connect(DATABASE_URL,connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO nadia_visual_subjects(guild_id,kind,name,description)
                VALUES(%s,%s,%s,'') ON CONFLICT(guild_id,kind,name) DO NOTHING""",
                (gid,kind,name))
            cur.execute("""SELECT id FROM nadia_visual_subjects
                WHERE guild_id=%s AND kind=%s AND lower(name)=lower(%s)""",(gid,kind,name))
            row=cur.fetchone()
            if not row:raise RuntimeError('Could not resolve visual subject')
            for raw,mime in photos:
                cur.execute("""INSERT INTO nadia_visual_photos
                    (guild_id,subject_id,image_bytes,mime_type,angle)
                    VALUES(%s,%s,%s,%s,%s)""",(gid,row[0],raw,mime,'Uploaded by natural-language request'))

async def handle_reference_upload(message):
    if not REFERENCE_SAVE.search(message.content or ''):
        return False
    if not message.attachments:
        await message.reply('Attach the photo and tell me its category and name, like “Nadia, add to reference: person Nia.”',mention_author=False)
        return True
    if not (message.author.id == MOMMY_ID or
            (is_solace() and message.author.id == DADDY_ID) or
            message.author.guild_permissions.manage_guild):
        await message.reply('Only authorized household managers can save reference photos.',mention_author=False)
        return True
    instruction=parse_reference_instruction(message.content)
    if not instruction or instruction[0]=='missing':
        await message.reply('Tell me the category and name: person, group, room, or object. For example, “Nadia, add to reference: group Nia and Sly.”',mention_author=False)
        return True
    kind,name,_=instruction
    try:
        if len(message.attachments)>8:
            raise ValueError('Please upload up to eight photos at a time.')
        photos=[]
        for attachment in message.attachments:
            raw=await read_reference(attachment)
            photos.append((raw,attachment.content_type))
        await asyncio.to_thread(store_uploaded_references,message.guild.id,kind,name,photos)
        await message.reply(
            f'Saved {len(photos)} reference photo{"s" if len(photos)!=1 else ""} for **{name}** ({kind}). I’ll use them for future captures.',
            mention_author=False)
    except ValueError as exc:
        await message.reply(str(exc),mention_author=False)
    except Exception:
        log.exception('Natural reference upload failed')
        await message.reply('I couldn’t save those references. Please try again.',mention_author=False)
    return True

# ---------- Solace household operations ----------
# Every duration is simulated; no real appliances or healthcare services are controlled.
import unicodedata
@@ -1004,7 +930,6 @@ def save():
MAX_REFERENCE_BYTES = 5 * 1024 * 1024
PHOTO_KIND = [app_commands.Choice(name='Person', value='person'),
app_commands.Choice(name='Room', value='room'),
              app_commands.Choice(name='Group', value='group'),
app_commands.Choice(name='Memory / Object', value='memory')]

def normalized_name(name):
@@ -1141,11 +1066,11 @@ async def reference_for_scene(scene):
# Natural-language photography is an action, not a chat promise.
# Only explicit requests addressed to Nadia trigger an image.
CAPTURE_REQUEST = re.compile(
    r"\b(?:take|snap|capture|get|make|shoot)\s+(?:(?:a|the|our|this|some)\s+)?"
    r"(?:quick\s+)?(?:picture|photo|photograph|pic|snapshot|family portrait)\b"
    r"\b(?:take|snap|capture|shoot|make|get|photograph)\b.{0,65}\b(?:picture|photo|photograph|pic|snapshot|portrait)\b"
r"|\b(?:capture|photograph)\s+(?:this|the)\s+moment\b"
r"|\b(?:take|snap)\s+(?:one|another)\b", re.I)


async def build_capture_context(channel, request):
# The live Discord transcript includes Sly's messages even when Nadia was silent.
# Do not read other channels, servers, or older private household history.
@@ -1173,17 +1098,55 @@ async def create_scene_capture(channel, user_id, scene, context=None):
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
    # For natural requests, references are based on the present cast, not just the short phrase.
    ref_text=(scene+' '+(context or ''))[-12500:]
    refs=await reference_for_scene(ref_text)
    if not is_solace():
        refs=[r for r in refs if r[3].strip().lower() in ('nia','nyla','nyla reign diamond','nadia','nadia st. clair')]
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
@@ -1213,6 +1176,8 @@ async def capture(interaction: discord.Interaction, scene: str):
try:
await create_scene_capture(interaction.channel,interaction.user.id,scene)
await interaction.followup.send('Photo saved to the album.',ephemeral=True)
    except ValueError as exc:
        await interaction.followup.send(str(exc)[:1800], ephemeral=True)
except Exception:
log.exception('Capture failed')
await interaction.followup.send('The image service could not complete that capture. No image was saved. Check the Render logs and image API access.',ephemeral=True)
