#!/usr/bin/env python3
"""
full_userbot.py
Userbot (Telethon) with:
 - dot-prefixed commands (.help, .scrap, .add_admin, .remove_admin, .add_whitelist, .remove_whitelist, .adminlist, .whitelist, .stats, .claim_owner)
 - owner set to 7941244038
 - session file name: ayush_userbot
 - admin/whitelist, per-user usage limits (default 10 for non-admins)
 - logs actions to actions.log and stores persistent data in data.json
 - simple .scrap flow: send a t.me link, bot asks "how many?", then downloads that many messages (start msg + next N-1) and uploads them to requester
 - progress bars in monospace with MB/s for download/upload
IMPORTANT: Only access chats that the logged-in account can legally access.
"""

import os
import re
import time
import json
import asyncio
from typing import Optional

from telethon import TelegramClient, events
from telethon.errors import BadRequestError, UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest

# ----------------- CONFIG -----------------
API_ID = int(os.environ.get("TG_API_ID", "YOUR_API"))
API_HASH = os.environ.get("TG_API_HASH", "API_HASH")
SESSION = os.environ.get("TG_SESSION_NAME", "ayush_userbot")  # as requested
DOWNLOAD_DIR = os.environ.get("TG_DOWNLOAD_DIR", "/tmp/downloads")
BOT_CREATOR = os.environ.get("BOT_CREATOR", "@mahadev_ki_iccha")   # set to your credit text if you want
OWNER_ID = int(os.environ.get("OWNER_ID", "7941244038"))     # fixed owner
DATA_FILE = os.environ.get("TG_DATA_FILE", "data.json")
LOG_FILE = os.environ.get("TG_LOG_FILE", "actions.log")
USAGE_LIMIT_NON_ADMIN = int(os.environ.get("USAGE_LIMIT_NON_ADMIN", "10"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)



from telethon.sessions import StringSession

TG_STRING_SESSION = os.environ.get("TG_STRING_SESSION", "").strip()

if TG_STRING_SESSION:
    client = TelegramClient(StringSession(TG_STRING_SESSION), API_ID, API_HASH)
else:
    client = TelegramClient(SESSION, API_ID, API_HASH)

# pending requests dict for ongoing .scrap sessions
PENDING = {}

# ----------------- data persistence -----------------
def _init_data():
    return {
        "owner_id": OWNER_ID,
        "admins": [],
        "whitelist": [],
        "users": {}  # key: str(uid) -> {name, first_seen, last_seen, usage_count}
    }

def load_data():
    if not os.path.exists(DATA_FILE):
        d = _init_data()
        save_data(d)
        return d
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        d = _init_data()
        save_data(d)
        return d

def save_data(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)

DATA = load_data()

# ----------------- logging -----------------
def log_action(user: str, uid: int, command: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    line = f"[{ts}] {user} ({uid}) ran: {command}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line.strip())

# ----------------- helpers -----------------
def is_owner(uid: int) -> bool:
    return DATA.get("owner_id") == uid

def is_admin(uid: int) -> bool:
    return uid in DATA.get("admins", []) or is_owner(uid)

def is_whitelisted(uid: int) -> bool:
    return uid in DATA.get("whitelist", []) or is_admin(uid)

def record_user(uid: int, name: str):
    key = str(uid)
    now = int(time.time())
    users = DATA.setdefault("users", {})
    if key not in users:
        users[key] = {"name": name, "first_seen": now, "last_seen": now, "usage_count": 0}
    else:
        users[key]["name"] = name
        users[key]["last_seen"] = now
    save_data(DATA)

def increment_usage(uid: int) -> int:
    key = str(uid)
    users = DATA.setdefault("users", {})
    if key not in users:
        users[key] = {"name": "", "first_seen": int(time.time()), "last_seen": int(time.time()), "usage_count": 0}
    users[key]["usage_count"] = users[key].get("usage_count", 0) + 1
    save_data(DATA)
    return users[key]["usage_count"]

def format_id_name(uid: Optional[int]) -> str:
    if not uid:
        return "None"
    u = DATA.get("users", {}).get(str(uid))
    if u and u.get("name"):
        return f"{u['name']} (`{uid}`)"
    return f"`{uid}`"

def list_admins_text():
    admins = DATA.get("admins", [])
    if not admins:
        return "No admins set."
    return "\n".join(f"- {format_id_name(a)}" for a in admins)

def list_whitelist_text():
    wl = DATA.get("whitelist", [])
    if not wl:
        return "Whitelist is empty."
    return "\n".join(f"- {format_id_name(w)}" for w in wl)
# regex for Telegram message links
link_re = re.compile(
    r'(?:https?://)?t\.me/(?P<kind>c/|\+)?(?P<chat>[\w\d_-]+|\d+)/(?P<msg_id>\d+)',
    re.IGNORECASE
)
# ----------------- parse link -----------------
def parse_tme_link(text: str):
    m = link_re.search(text)
    if not m:
        return None, None, None
    kind = m.group("kind")
    chat = m.group("chat")
    msg_id = int(m.group("msg_id"))
    if kind == "c/":
        try:
            chatid = int(f"-100{chat}")
            return chatid, msg_id, None
        except Exception:
            return None, None, None
    elif kind == "+":
        invite = f"+{chat}"
        return invite, msg_id, invite
    else:
        return chat, msg_id, None

# ----------------- progress bar -----------------
async def progress_bar(current, total, message, action="📥 Downloading", start_time=None):
    if not message:
        return
    percent = 100 if total == 0 else int(current * 100 / total) if total else 0
    filled_blocks = percent // 10
    bar = "█" * filled_blocks + "░" * (10 - filled_blocks)
    elapsed = (time.time() - start_time) if start_time else 0.0
    speed = (current / (1024 * 1024)) / elapsed if elapsed > 0 else 0.0
    text = (
        "```\n"
        f"{action}: {percent:>3}% [{bar}]\n"
        f"Speed : {speed:6.2f} MB/s\n"
        "```"
        f"\n<b>Bot created by:</b> {BOT_CREATOR}"
    )
    try:
        await message.edit(text, parse_mode="html")
    except Exception:
        pass

# ----------------- event handlers -----------------

# record every user who sends a message to the logged-in account
@client.on(events.NewMessage)
async def record_every_user(event):
    sender = await event.get_sender()
    if not sender:
        return
    uid = sender.id
    name = (sender.username or f"{sender.first_name or ''} {sender.last_name or ''}").strip()
    if not name:
        name = str(uid)
    record_user(uid, name)

# ---- help ----
HELP_TEXT = f"""
Available commands (prefix is a dot `.`):

.owner/admin/whitelist:
 .claim_owner               - claim ownership if none set (one-time)
 .add_admin <id>            - owner only
 .remove_admin <id>         - owner only
 .adminlist                 - show admins (owner/admin only)

.whitelist:
 .add_whitelist <id>        - admins only
 .remove_whitelist <id>     - admins only
 .whitelist                 - show whitelist (admins only)

.usage & info:
 .help                     - show this help
 .stats                    - owner only; show users & usage stats
 .scrap <t.me link>        - start scrap flow (placeholder: downloads messages the logged-in account can access)
"""

@client.on(events.NewMessage(pattern=r"^\.help"))
async def cmd_help(event):
    sender = await event.get_sender()
    uid = sender.id if sender else event.chat_id
    log_action(sender.username or sender.first_name or "unknown", uid, ".help")
    await event.reply(HELP_TEXT)

# ---- claim owner ----
@client.on(events.NewMessage(pattern=r"^\.claim_owner\b"))
async def cmd_claim_owner(event):
    sender = await event.get_sender()
    uid = sender.id
    if DATA.get("owner_id"):
        await event.reply(f"Owner already set to {format_id_name(DATA['owner_id'])}.")
        return
    DATA["owner_id"] = uid
    save_data(DATA)
    log_action(sender.username or sender.first_name or "unknown", uid, ".claim_owner")
    await event.reply(f"You ({format_id_name(uid)}) are now the owner.")

# ---- admin management ----
@client.on(events.NewMessage(pattern=r"^\.add_admin\s+(\d+)$"))
async def cmd_add_admin(event):
    sender = await event.get_sender()
    if not is_owner(sender.id):
        return await event.reply("Only the owner can add admins.")
    uid = int(event.pattern_match.group(1))
    if uid == DATA.get("owner_id"):
        return await event.reply("Owner is already admin.")
    if uid in DATA.get("admins", []):
        return await event.reply("This user is already an admin.")
    DATA.setdefault("admins", []).append(uid)
    save_data(DATA)
    log_action(sender.username or sender.first_name or "unknown", sender.id, f".add_admin {uid}")
    await event.reply(f"Added admin: {format_id_name(uid)}")

@client.on(events.NewMessage(pattern=r"^\.remove_admin\s+(\d+)$"))
async def cmd_remove_admin(event):
    sender = await event.get_sender()
    if not is_owner(sender.id):
        return await event.reply("Only the owner can remove admins.")
    uid = int(event.pattern_match.group(1))
    if uid in DATA.get("admins", []):
        DATA["admins"].remove(uid)
        save_data(DATA)
        log_action(sender.username or sender.first_name or "unknown", sender.id, f".remove_admin {uid}")
        await event.reply(f"Removed admin: {format_id_name(uid)}")
    else:
        await event.reply("That user was not an admin.")

@client.on(events.NewMessage(pattern=r"^\.adminlist\b"))
async def cmd_admin_list(event):
    sender = await event.get_sender()
    if not is_admin(sender.id):
        return await event.reply("Admins only.")
    await event.reply("Admins:\n" + list_admins_text())

# ---- whitelist management ----
@client.on(events.NewMessage(pattern=r"^\.add_whitelist\s+(\d+)$"))
async def cmd_add_whitelist(event):
    sender = await event.get_sender()
    if not is_admin(sender.id):
        return await event.reply("Only admins can add to whitelist.")
    uid = int(event.pattern_match.group(1))
    if uid in DATA.get("whitelist", []):
        return await event.reply("Already whitelisted.")
    DATA.setdefault("whitelist", []).append(uid)
    save_data(DATA)
    log_action(sender.username or sender.first_name or "unknown", sender.id, f".add_whitelist {uid}")
    await event.reply(f"Whitelisted {format_id_name(uid)}")

@client.on(events.NewMessage(pattern=r"^\.remove_whitelist\s+(\d+)$"))
async def cmd_remove_whitelist(event):
    sender = await event.get_sender()
    if not is_admin(sender.id):
        return await event.reply("Only admins can remove from whitelist.")
    uid = int(event.pattern_match.group(1))
    if uid in DATA.get("whitelist", []):
        DATA["whitelist"].remove(uid)
        save_data(DATA)
        log_action(sender.username or sender.first_name or "unknown", sender.id, f".remove_whitelist {uid}")
        await event.reply(f"Removed from whitelist: {format_id_name(uid)}")
    else:
        await event.reply("That user is not whitelisted.")

@client.on(events.NewMessage(pattern=r"^\.whitelist\b"))
async def cmd_whitelist(event):
    sender = await event.get_sender()
    if not is_admin(sender.id):
        return await event.reply("Admins only.")
    await event.reply("Whitelist:\n" + list_whitelist_text())

# ---- stats ----
@client.on(events.NewMessage(pattern=r"^\.stats\b"))
async def cmd_stats(event):
    sender = await event.get_sender()
    if not is_owner(sender.id):
        return await event.reply("Only the owner can view stats.")
    users = DATA.get("users", {})
    total_users = len(users)
    top = sorted(users.items(), key=lambda kv: kv[1].get("usage_count", 0), reverse=True)[:10]
    top_lines = [f"- {info.get('name','<no name>')} (`{uid}`): {info.get('usage_count',0)}" for uid, info in [(k, v) for k, v in top] ]
    admins_text = list_admins_text()
    whitelist_text = list_whitelist_text()
    owner_text = format_id_name(DATA.get("owner_id"))
    text = (
        f"📊 Bot Stats\n\nOwner: {owner_text}\n"
        f"Total users recorded: {total_users}\n\nTop users by usage:\n" + ("\n".join(top_lines) if top_lines else "None\n") +
        f"\nAdmins:\n{admins_text}\n\nWhitelist:\n{whitelist_text}"
    )
    await event.reply(text)

# ---- placeholder .scrap flow (asks how many) ----
@client.on(events.NewMessage(pattern=r"^\.scrap\b(?:\s+(.*))?"))
async def cmd_scrap_start(event):
    sender = await event.get_sender()
    uid = sender.id
    text = (event.raw_text or "").strip()
    # record and log
    name = (sender.username or sender.first_name or str(uid))
    record_user(uid, name)
    log_action(name, uid, text)

    # permission check
    if not is_whitelisted(uid):
        return await event.reply("You are not whitelisted. Contact an admin to get access.")

    # enforce non-admin usage limit
    if not is_admin(uid):
        usage_after = increment_usage(uid)
        if usage_after > USAGE_LIMIT_NON_ADMIN:
            return await event.reply(f"Usage limit reached ({USAGE_LIMIT_NON_ADMIN}). Contact an admin.")
        # else continue

    # parse link from command arg or message
    # user can send: .scrap https://t.me/chan/123
    arg = event.pattern_match.group(1)
    link_text = arg.strip() if arg else ""
    if not link_text:
        # maybe the user sent only ".scrap" - ask for link
        PENDING[uid] = {"entity": None, "msg_id": None}
        return await event.reply("Send the t.me message link (like `https://t.me/channel/123`) you want to fetch from.")
    # else parse the link now
    entity, msg_id, invite = parse_tme_link(link_text)
    if not entity:
        return await event.reply("Couldn't parse t.me link. Send like: https://t.me/channelname/123 or https://t.me/c/1234567/123")
    # try join if invite
    if isinstance(entity, str) and entity.startswith("+"):
        try:
            invite_full = f"https://t.me/{entity}"
            try:
                await client(JoinChannelRequest(invite_full))
            except TypeError:
                await client(JoinChannelRequest(entity))
        except UserAlreadyParticipantError:
            pass
        except Exception as e:
            await event.reply(f"Warning: could not auto-join: {e}\nPlease join manually if needed.")

    # save pending and ask for count
    PENDING[uid] = {"entity": entity, "msg_id": msg_id}
    await event.reply("How many messages (including this one) should I fetch? Reply with a number (e.g. 10).")

# ---- when user replies with a number ----
@client.on(events.NewMessage(pattern=r"^\d+$"))
async def cmd_scrap_count(event):
    sender = await event.get_sender()
    uid = sender.id
    text = event.raw_text.strip()
    if uid not in PENDING:
        return  # not part of pending flow
    try:
        count = int(text)
    except Exception:
        await event.reply("Send a valid number.")
        return
    pending = PENDING.pop(uid)
    entity = pending.get("entity")
    msg_id = pending.get("msg_id")
    if not entity or not msg_id:
        await event.reply("No link pending. Start again with `.scrap <link>`.")
        return
    await event.reply(f"Starting fetch: {count} messages from {entity} starting at {msg_id} ...")
    # run actual fetch/send
    await scrape_and_send(event, uid, entity, msg_id, count)

# ----------------- scrape_and_send -----------------
async def scrape_and_send(event, user_id, entity, start_msg_id, count):
    """Fetch start_msg_id + next (count-1) newer messages (forward) and send to user_id.
       Only messages the logged-in user can access will succeed.
    """
    # resolve entity
    try:
        chat_entity = await client.get_entity(entity)
    except Exception as e:
        await client.send_message(user_id, f"❌ Could not access that chat: {e}\nMake sure the logged-in account can view it.")
        return

    # fetch start message
    try:
        first_msg = await client.get_messages(chat_entity, ids=start_msg_id)
    except Exception as e:
        await client.send_message(user_id, f"❌ Failed to fetch the starting message: {e}")
        return

    if not first_msg:
        await client.send_message(user_id, "❌ Starting message not found or not accessible.")
        return

    msgs = [first_msg]
    remaining = max(0, count - 1)
    if remaining > 0:
        try:
            # fetch newer messages (ids > start_msg_id)
            async for m in client.iter_messages(chat_entity, min_id=start_msg_id, limit=remaining, reverse=True):
                msgs.append(m)
                if len(msgs) >= count:
                    break
        except Exception as e:
            await client.send_message(user_id, f"⚠️ Warning while fetching additional messages: {e}\nProceeding with available messages.")

    await client.send_message(user_id, f"Found {len(msgs)} messages. Beginning download/upload...\n<b>Bot created by:</b> {BOT_CREATOR}", parse_mode="html")

    sent = 0
    failed = 0
    for m in msgs:
        try:
            if m.media:
                status = await client.send_message(user_id, f"📥 Preparing to download `{m.id}`...\n\n<b>Bot created by:</b> {BOT_CREATOR}", parse_mode="html")
                orig_name = getattr(m.file, "name", None) or "file"
                safe_name = orig_name.replace("/", "_").replace("\\", "_")
                save_as = os.path.join(DOWNLOAD_DIR, f"{m.id}_{safe_name}")
                start_time = time.time()
                filename = await client.download_media(
                    m,
                    file=save_as,
                    progress_callback=lambda downloaded, total: asyncio.create_task(
                        progress_bar(downloaded, total or 0, status, "📥 Downloading", start_time)
                    )
                )
                await status.edit("📤 Uploading to you now...\n\n<b>Bot created by:</b> " + BOT_CREATOR, parse_mode="html")
                await client.send_file(
                    user_id,
                    filename,
                    caption=(m.text or "") + f"\n\n<b>Bot created by:</b> {BOT_CREATOR}",
                    parse_mode="html",
                    progress_callback=lambda uploaded, total: asyncio.create_task(
                        progress_bar(uploaded, total or 0, status, "📤 Uploading", start_time)
                    )
                )
                await status.edit(f"✅ Done `{m.id}`\n<b>Bot created by:</b> {BOT_CREATOR}", parse_mode="html")
                try:
                    os.remove(filename)
                except Exception:
                    pass
            else:
                # text-only message
                text_to_send = (m.text or "<no text>") + f"\n\n<b>Bot created by:</b> {BOT_CREATOR}"
                await client.send_message(user_id, text_to_send, parse_mode="html")
            sent += 1
            await asyncio.sleep(0.7)
        except BadRequestError as bre:
            failed += 1
            await client.send_message(user_id, f"Failed to process message {m.id}: {bre}\n<b>Bot created by:</b> {BOT_CREATOR}", parse_mode="html")
        except Exception as e:
            failed += 1
            await client.send_message(user_id, f"Error with message {m.id}: {e}\n<b>Bot created by:</b> {BOT_CREATOR}", parse_mode="html")

    await client.send_message(user_id, f"✅ Finished. Sent: {sent}. Failed: {failed}.\n<b>Bot created by:</b> {BOT_CREATOR}", parse_mode="html")

# ----------------- start -----------------
async def main():
    print("Starting user session. You will be asked for phone & OTP once (session saved).")
    await client.start()  # interactive login first time
    print("Logged in. User session active.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting...")

