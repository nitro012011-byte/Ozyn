import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import os
import json
import ast
import asyncio
import datetime
import re as _re
import uuid
import aiohttp
import io
import time
import base64
import hmac
import hashlib
from urllib.parse import urlparse
from pymongo import MongoClient
from dotenv import load_dotenv
from flask import Flask, request as flask_request
import threading

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG — environment variables
# ══════════════════════════════════════════════════════════════════════════════

# ── Moderator Bot MongoDB (bot.py) ────────────────────────────────────────────
MOD_MONGO_URI = os.getenv("MOD_MONGO_URI", "mongodb://localhost:27017")

# ── Ticket Bot MongoDB (bot__1_.py) ───────────────────────────────────────────
TICKET_MONGO_URI = os.getenv("TICKET_MONGO_URI", "mongodb://localhost:27017")

# ── Discord Token ─────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")

# ── Ticket Bot config ─────────────────────────────────────────────────────────
VOUCH_CHANNEL_ID = int(os.getenv("VOUCH_CHANNEL_ID", "1489662348850495653"))
PROOF_CHANNEL_ID      = int(os.getenv("PROOF_CHANNEL_ID", 0))
DEALER_ROLE_ID        = int(os.getenv("DEALER_ROLE_ID", 0))
BUYER_ROLE_ID         = int(os.getenv("BUYER_ROLE_ID", 0))
HEAD_DEALER_ROLE_ID   = int(os.getenv("HEAD_DEALER_ROLE_ID", 0))
PANEL_CHANNEL_ID      = int(os.getenv("PANEL_CHANNEL_ID", 0))
TRANSCRIPT_CHANNEL_ID = int(os.getenv("TRANSCRIPT_CHANNEL_ID", 0))
TICKET_CATEGORY_ID    = int(os.getenv("TICKET_CATEGORY_ID", 0))
TICKET_MANAGER_ROLE_ID = int(os.getenv("TICKET_MANAGER_ROLE_ID", 0))
IMGBB_API_KEY         = os.getenv("IMGBB_API_KEY")

# ── SellAuth integration ───────────────────────────────────────────────────────
# All optional — if any of these are blank, the SellAuth webhook route just
# no-ops and the rest of the bot runs completely normally.
SELLAUTH_API_KEY        = os.getenv("SELLAUTH_API_KEY", "")
SELLAUTH_SHOP_ID        = os.getenv("SELLAUTH_SHOP_ID", "")
SELLAUTH_WEBHOOK_SECRET = os.getenv("SELLAUTH_WEBHOOK_SECRET", "")
SELLAUTH_ENABLED        = bool(SELLAUTH_API_KEY and SELLAUTH_SHOP_ID)

# ── Shop branding (name) ──────────────────────────────────────────────────────
SHOP_NAME = os.getenv("SHOP_NAME", "Elf Mart")

# ── Panel branding (logo + banner) ──────────────────────────────────────────
def _clean_env_url(name: str, default: str) -> str:
    """Read a URL from env, stripping stray quotes/whitespace that break Discord image embeds.
    Falls back to `default` if the env var is unset or empty after cleaning."""
    raw = os.getenv(name)
    if not raw:
        return default
    cleaned = raw.strip().strip('"').strip("'").strip()
    return cleaned if cleaned else default

PANEL_LOGO_URL = _clean_env_url("PANEL_LOGO_URL", "https://i.ibb.co/6RXW56Zj/IMG-20260531-094220.png")
PANEL_BANNER_URL = _clean_env_url("PANEL_BANNER_URL", "https://i.ibb.co/N6drMJT2/Elfmartbanner.png")

# ── .vouch command config ─────────────────────────────────────────────────────
# Server invite link is kept as a secret env var (never hardcoded) — used on the
# "Not Joined Server?" link button in the .vouch embed.
SERVER_INVITE_LINK = _clean_env_url("SERVER_INVITE_LINK", "")

# ── Crypto ────────────────────────────────────────────────────────────────────
BLOCKCYPHER_TOKEN     = os.getenv("BLOCKCYPHER_TOKEN", "")

# ══════════════════════════════════════════════════════════════════════════════
#  MONGODB — TWO SEPARATE CONNECTIONS
# ══════════════════════════════════════════════════════════════════════════════

# ── Moderator bot database ────────────────────────────────────────────────────
mod_mongo       = MongoClient(MOD_MONGO_URI)
mod_db          = mod_mongo["discord_bot"]
infractions_col = mod_db["infractions"]
config_col      = mod_db["config"]
autoresponders_col = mod_db["autoresponders"]
autopurge_col   = mod_db["autopurge"]
dm_announces_col = mod_db["dm_announces"]
dm_announces_col.create_index("expire_at", expireAfterSeconds=0)  # TTL auto-delete after 3 days

# ── Moderator bot shop collections ────────────────────────────────────────────
balances_col    = mod_db["balances"]
ltc_wallets_col = mod_db["ltc_wallets"]
orders_col      = mod_db["orders"]
feedback_col    = mod_db["feedback"]
afk_col         = mod_db["afk"]
vouch_users_col = mod_db["vouch_users"]  # per-user override for the ID shown in /vouch (alt accounts)

# ── Ticket bot database ────────────────────────────────────────────────────────
ticket_mongo    = MongoClient(TICKET_MONGO_URI)
ticket_db       = ticket_mongo["ticket_bot"]
deals_col       = ticket_db["active_deals"]
panels_col      = ticket_db["panels"]
tickets_col     = ticket_db["tickets"]
ltc_col         = ticket_db["dealer_ltc"]
upi_col         = ticket_db["dealer_upi"]
proof_col       = ticket_db["awaiting_proof"]
proposals_col   = ticket_db["deal_proposals"]
giveaways_col   = ticket_db["giveaways"]          # ← giveaway persistence
scheduled_messages_col = ticket_db["scheduled_messages"]  # ← .setscheuledmessage persistence
tos_col            = mod_db["tos_entries"]          # ← TOS management
emoji_log_col      = mod_db["emoji_log"]            # ← Emoji audit log
autostatusrole_col = mod_db["autostatusrole"]       # ← Auto Status Role config (one doc per guild)

# ── In-memory state (ticket bot) ──────────────────────────────────────────────
active_deals  = {}
awaiting_proof = {}   # channel_id -> deal_doc

# ══════════════════════════════════════════════════════════════════════════════
#  BOT SETUP
# ══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.presences = True  # required for Auto Status Role (custom status detection)

def _get_prefixes(bot_, message):
    """Always allow the default '.' and '!' prefixes, plus any custom
    per-guild prefix the server has configured via .setprefix."""
    prefixes = ["!", "."]
    if message.guild:
        cfg = config_col.find_one({"guild_id": message.guild.id}) or {}
        custom = cfg.get("prefix")
        if custom and custom not in prefixes:
            prefixes.append(custom)
    return prefixes

bot = commands.Bot(command_prefix=_get_prefixes, intents=intents, help_command=None)

# ══════════════════════════════════════════════════════════════════════════════
#  MODERATOR BOT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_config(guild_id: int) -> dict:
    return config_col.find_one({"guild_id": guild_id}) or {}

def set_config(guild_id: int, key: str, value):
    config_col.update_one({"guild_id": guild_id}, {"$set": {key: value}}, upsert=True)

async def send_log(guild: discord.Guild, embed: discord.Embed):
    cfg = get_config(guild.id)
    ch_id = cfg.get("log_channel")
    if ch_id:
        ch = guild.get_channel(int(ch_id))
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

def mod_embed(color, title, **fields) -> discord.Embed:
    e = discord.Embed(title=title, color=color,
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    for k, v in fields.items():
        e.add_field(name=k.replace("_", " ").title(), value=str(v), inline=True)
    return e

def add_infraction(guild_id, user_id, mod_id, action, reason):
    infractions_col.insert_one({
        "guild_id": guild_id, "user_id": user_id, "mod_id": mod_id,
        "action": action, "reason": reason or "No reason provided",
        "timestamp": datetime.datetime.now(datetime.timezone.utc),
    })

def count_warnings(guild_id, user_id) -> int:
    return infractions_col.count_documents({"guild_id": guild_id, "user_id": user_id, "action": "warn"})

def is_mod_check(member: discord.Member, guild_id: int) -> bool:
    if member.guild_permissions.administrator:
        return True
    cfg = get_config(guild_id)
    mod_role_id = cfg.get("mod_role")
    if mod_role_id:
        role = member.guild.get_role(int(mod_role_id))
        if role and role in member.roles:
            return True
    return False

def is_shop_admin_check(member: discord.Member, guild_id: int) -> bool:
    cfg = get_config(guild_id)
    role_id = cfg.get("shop_admin_role")
    if role_id:
        # Safely get the guild — member.guild may not exist for User objects
        guild = getattr(member, "guild", None)
        if guild is None:
            guild = bot.get_guild(guild_id)
        if guild is None:
            return False
        # Re-fetch as Member if we only have a User
        if not isinstance(member, discord.Member):
            member = guild.get_member(member.id)
            if member is None:
                return False
        role = guild.get_role(int(role_id))
        if role and role in member.roles:
            return True
        return False
    # No shop_admin_role set — fall back to server owner only
    guild = getattr(member, "guild", None) or bot.get_guild(guild_id)
    if guild is None:
        return False
    return member.id == guild.owner_id

def check_hierarchy(guild: discord.Guild, member: discord.Member) -> str | None:
    if member == guild.owner:
        return "❌ I cannot action the server owner."
    if member == guild.me:
        return "❌ I cannot action myself."
    if member.top_role >= guild.me.top_role:
        return (
            "❌ **Role Hierarchy Error**\n"
            "My role is not high enough to action this user.\n"
            "Go to **Server Settings → Roles** and drag my role above theirs."
        )
    return None

async def respond(ctx_or_inter, content=None, embed=None, ephemeral=False):
    if isinstance(ctx_or_inter, discord.Interaction):
        if ctx_or_inter.response.is_done():
            await ctx_or_inter.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await ctx_or_inter.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
    else:
        await ctx_or_inter.send(content=content, embed=embed)

def get_guild(ctx_or_inter):
    return ctx_or_inter.guild

def get_author(ctx_or_inter):
    if isinstance(ctx_or_inter, discord.Interaction):
        return ctx_or_inter.user
    return ctx_or_inter.author

# ══════════════════════════════════════════════════════════════════════════════
#  TICKET BOT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_dealer(member):
    return any(r.id == DEALER_ROLE_ID for r in member.roles)

def is_head_dealer(member):
    return any(r.id == HEAD_DEALER_ROLE_ID for r in member.roles)

def is_ticket_manager(member):
    return any(r.id == TICKET_MANAGER_ROLE_ID for r in member.roles)

def can_manage_ticket(member):
    return is_dealer(member) or is_head_dealer(member) or is_ticket_manager(member)

def make_embed(title=None, description=None, color=discord.Color.blurple(),
               fields=None, footer=None, thumbnail=None, image=None):
    embed = discord.Embed(title=title, description=description, color=color)
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)
    return embed

async def upload_to_imgbb(image_bytes: bytes, filename: str = "proof.png") -> str | None:
    if not IMGBB_API_KEY:
        return None
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.imgbb.com/1/upload",
                data={"key": IMGBB_API_KEY, "image": b64, "name": filename},
                timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["data"]["image"]["url"]
    except Exception:
        pass
    return None

def get_dealer_ltc(dealer_id):
    data = ltc_col.find_one({"dealer_id": dealer_id})
    return data["address"] if data else os.getenv("LTC_ADDRESS", "")

def get_dealer_upi(dealer_id):
    data = upi_col.find_one({"dealer_id": dealer_id})
    return data if data else None

# ══════════════════════════════════════════════════════════════════════════════
#  MODERATOR BOT — SHARED LOGIC FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def _ban(ctx_or_inter, member: discord.Member, reason: str):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    err = check_hierarchy(guild, member)
    if err:
        return await respond(ctx_or_inter, content=err, ephemeral=True)
    try:
        await member.send(f"🔨 You have been **banned** from **{guild.name}**.\nReason: {reason}")
    except Exception:
        pass
    try:
        await member.ban(reason=reason)
    except discord.Forbidden:
        return await respond(ctx_or_inter,
            content="❌ **Missing Permissions**\nI need **Ban Members** and my role must be above theirs.",
            ephemeral=True)
    add_infraction(guild.id, member.id, author.id, "ban", reason)
    e = mod_embed(discord.Color.red(), "🔨 Member Banned", user=member, moderator=author, reason=reason)
    await respond(ctx_or_inter, embed=e)
    await send_log(guild, e)

async def _unban(ctx_or_inter, user_id: int, reason: str):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    try:
        user = await bot.fetch_user(user_id)
        await guild.unban(user, reason=reason)
    except discord.NotFound:
        return await respond(ctx_or_inter, content="❌ User not found or is not banned.", ephemeral=True)
    except discord.Forbidden:
        return await respond(ctx_or_inter, content="❌ Missing **Ban Members** permission.", ephemeral=True)
    add_infraction(guild.id, user_id, author.id, "unban", reason)
    e = mod_embed(discord.Color.green(), "✅ Member Unbanned", user=user, moderator=author, reason=reason)
    await respond(ctx_or_inter, embed=e)
    await send_log(guild, e)

async def _kick(ctx_or_inter, member: discord.Member, reason: str):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    err = check_hierarchy(guild, member)
    if err:
        return await respond(ctx_or_inter, content=err, ephemeral=True)
    try:
        await member.send(f"👢 You have been **kicked** from **{guild.name}**.\nReason: {reason}")
    except Exception:
        pass
    try:
        await member.kick(reason=reason)
    except discord.Forbidden:
        return await respond(ctx_or_inter,
            content="❌ **Missing Permissions**\nI need **Kick Members** and my role must be above theirs.",
            ephemeral=True)
    add_infraction(guild.id, member.id, author.id, "kick", reason)
    e = mod_embed(discord.Color.orange(), "👢 Member Kicked", user=member, moderator=author, reason=reason)
    await respond(ctx_or_inter, embed=e)
    await send_log(guild, e)

async def _mute(ctx_or_inter, member: discord.Member, duration: int, reason: str):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    err = check_hierarchy(guild, member)
    if err:
        return await respond(ctx_or_inter, content=err, ephemeral=True)
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=duration)
    try:
        await member.timeout(until, reason=reason)
    except discord.Forbidden:
        return await respond(ctx_or_inter,
            content="❌ **Missing Permissions**\nI need **Moderate Members** permission.",
            ephemeral=True)
    add_infraction(guild.id, member.id, author.id, "mute", reason)
    e = mod_embed(discord.Color.gold(), "🔇 Member Muted",
                  user=member, moderator=author, duration=f"{duration} minutes", reason=reason)
    await respond(ctx_or_inter, embed=e)
    await send_log(guild, e)

async def _unmute(ctx_or_inter, member: discord.Member):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    try:
        await member.timeout(None)
    except discord.Forbidden:
        return await respond(ctx_or_inter,
            content="❌ **Missing Permissions**\nI need **Moderate Members** permission.",
            ephemeral=True)
    e = mod_embed(discord.Color.green(), "🔊 Member Unmuted", user=member, moderator=author)
    await respond(ctx_or_inter, embed=e)
    await send_log(guild, e)

async def _warn(ctx_or_inter, member: discord.Member, reason: str):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    add_infraction(guild.id, member.id, author.id, "warn", reason)
    total = count_warnings(guild.id, member.id)
    e = mod_embed(discord.Color.yellow(), "⚠️ Member Warned",
                  user=member, moderator=author, reason=reason, total_warnings=total)
    await respond(ctx_or_inter, embed=e)
    await send_log(guild, e)
    try:
        await member.send(f"⚠️ You have been warned in **{guild.name}**.\nReason: {reason}\nTotal warnings: {total}")
    except Exception:
        pass

async def _softban(ctx_or_inter, member: discord.Member, reason: str):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    err = check_hierarchy(guild, member)
    if err:
        return await respond(ctx_or_inter, content=err, ephemeral=True)
    try:
        await member.ban(reason=reason, delete_message_days=7)
        await guild.unban(member)
    except discord.Forbidden:
        return await respond(ctx_or_inter,
            content="❌ **Missing Permissions**\nI need **Ban Members** and my role must be above theirs.",
            ephemeral=True)
    add_infraction(guild.id, member.id, author.id, "softban", reason)
    e = mod_embed(discord.Color.orange(), "💨 Member Softbanned", user=member, moderator=author, reason=reason)
    await respond(ctx_or_inter, embed=e)
    await send_log(guild, e)

async def _warnings(ctx_or_inter, member: discord.Member):
    guild = get_guild(ctx_or_inter)
    docs = list(infractions_col.find({"guild_id": guild.id, "user_id": member.id, "action": "warn"})
                .sort("timestamp", -1).limit(10))
    if not docs:
        return await respond(ctx_or_inter, content=f"✅ {member.mention} has no warnings.")
    e = discord.Embed(title=f"⚠️ Warnings for {member}", color=discord.Color.yellow())
    for i, d in enumerate(docs, 1):
        mod = guild.get_member(d["mod_id"]) or d["mod_id"]
        e.add_field(name=f"#{i} — {d['timestamp'].strftime('%Y-%m-%d')}",
                    value=f"Reason: {d['reason']}\nMod: {mod}", inline=False)
    await respond(ctx_or_inter, embed=e)

async def _modlogs(ctx_or_inter, member: discord.Member):
    guild = get_guild(ctx_or_inter)
    docs = list(infractions_col.find({"guild_id": guild.id, "user_id": member.id})
                .sort("timestamp", -1).limit(15))
    if not docs:
        return await respond(ctx_or_inter, content=f"✅ {member.mention} has no moderation history.")
    e = discord.Embed(title=f"📋 Mod Logs for {member}", color=discord.Color.blurple())
    for d in docs:
        mod = guild.get_member(d["mod_id"]) or d["mod_id"]
        e.add_field(name=f"{d['action'].upper()} — {d['timestamp'].strftime('%Y-%m-%d %H:%M')}",
                    value=f"Reason: {d['reason']}\nMod: {mod}", inline=False)
    await respond(ctx_or_inter, embed=e)

async def _userinfo(ctx_or_inter, member: discord.Member):
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    e = discord.Embed(title=f"👤 {member}", color=member.color,
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="ID", value=member.id)
    e.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "N/A")
    e.add_field(name="Created", value=member.created_at.strftime("%Y-%m-%d"))
    e.add_field(name="Roles", value=", ".join(roles) or "None", inline=False)
    e.add_field(name="Warnings", value=count_warnings(get_guild(ctx_or_inter).id, member.id))
    await respond(ctx_or_inter, embed=e)

async def _serverinfo(ctx_or_inter):
    g = get_guild(ctx_or_inter)
    e = discord.Embed(title=f"🏠 {g.name}", color=discord.Color.blurple(),
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    e.add_field(name="Owner", value=g.owner)
    e.add_field(name="Members", value=g.member_count)
    e.add_field(name="Channels", value=len(g.channels))
    e.add_field(name="Roles", value=len(g.roles))
    e.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d"))
    await respond(ctx_or_inter, embed=e)

async def _clear(ctx_or_inter, amount: int):
    if isinstance(ctx_or_inter, discord.Interaction):
        ch = ctx_or_inter.channel
        await ctx_or_inter.response.defer(ephemeral=True)
        deleted = await ch.purge(limit=amount)
        await ctx_or_inter.followup.send(f"🧹 Deleted {len(deleted)} messages.", ephemeral=True)
    else:
        deleted = await ctx_or_inter.channel.purge(limit=amount + 1)
        await ctx_or_inter.send(f"🧹 Deleted {len(deleted)-1} messages.", delete_after=5)

async def _slowmode(ctx_or_inter, seconds: int):
    ch = ctx_or_inter.channel
    await ch.edit(slowmode_delay=seconds)
    msg = "✅ Slowmode disabled." if seconds == 0 else f"⏱️ Slowmode set to {seconds}s."
    await respond(ctx_or_inter, content=msg)

async def _lock(ctx_or_inter, channel: discord.TextChannel = None):
    ch = channel or ctx_or_inter.channel
    ow = ch.overwrites_for(get_guild(ctx_or_inter).default_role)
    ow.send_messages = False
    await ch.set_permissions(get_guild(ctx_or_inter).default_role, overwrite=ow)
    await respond(ctx_or_inter, content=f"🔒 {ch.mention} locked.")

async def _unlock(ctx_or_inter, channel: discord.TextChannel = None):
    ch = channel or ctx_or_inter.channel
    ow = ch.overwrites_for(get_guild(ctx_or_inter).default_role)
    ow.send_messages = None
    await ch.set_permissions(get_guild(ctx_or_inter).default_role, overwrite=ow)
    await respond(ctx_or_inter, content=f"🔓 {ch.mention} unlocked.")

async def _hide(ctx_or_inter, channel: discord.TextChannel = None):
    ch = channel or ctx_or_inter.channel
    ow = ch.overwrites_for(get_guild(ctx_or_inter).default_role)
    ow.view_channel = False
    await ch.set_permissions(get_guild(ctx_or_inter).default_role, overwrite=ow)
    await respond(ctx_or_inter, content=f"👁️ {ch.mention} hidden.")

async def _unhide(ctx_or_inter, channel: discord.TextChannel = None):
    ch = channel or ctx_or_inter.channel
    ow = ch.overwrites_for(get_guild(ctx_or_inter).default_role)
    ow.view_channel = None
    await ch.set_permissions(get_guild(ctx_or_inter).default_role, overwrite=ow)
    await respond(ctx_or_inter, content=f"👁️ {ch.mention} visible.")

async def _channelcreate(ctx_or_inter, name: str, category_id: int = None):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    category = None
    if category_id:
        fetched = guild.get_channel(category_id)
        if fetched is None or not isinstance(fetched, discord.CategoryChannel):
            return await respond(ctx_or_inter,
                content=(f"❌ No category found with ID `{category_id}`.\n"
                         "Enable **Developer Mode** → right-click category → **Copy ID**."),
                ephemeral=True)
        category = fetched
    try:
        channel = await guild.create_text_channel(name, category=category)
    except discord.Forbidden:
        return await respond(ctx_or_inter,
            content="❌ **Missing Permissions**\nI need **Manage Channels** permission.", ephemeral=True)
    e = discord.Embed(title="📢 Channel Created", color=discord.Color.green(),
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.add_field(name="Channel", value=channel.mention)
    e.add_field(name="Category", value=category.name if category else "None")
    e.add_field(name="Channel ID", value=f"`{channel.id}`")
    e.add_field(name="Created By", value=author.mention)
    await respond(ctx_or_inter, embed=e)
    await send_log(guild, e)

async def _deletechannel(ctx_or_inter, channel: discord.TextChannel = None):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    ch = channel or ctx_or_inter.channel
    ch_name = ch.name
    deleting_current = (ch == ctx_or_inter.channel)
    try:
        await ch.delete(reason=f"Deleted by {author}")
    except discord.Forbidden:
        return await respond(ctx_or_inter,
            content="❌ **Missing Permissions**\nI need **Manage Channels** permission.", ephemeral=True)
    except discord.HTTPException as ex:
        return await respond(ctx_or_inter, content=f"❌ Failed to delete channel: `{ex}`", ephemeral=True)
    e = discord.Embed(title="🗑️ Channel Deleted", color=discord.Color.red(),
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.add_field(name="Channel", value=f"`#{ch_name}`")
    e.add_field(name="Deleted By", value=author.mention)
    if not deleting_current:
        await respond(ctx_or_inter, embed=e)
    await send_log(guild, e)

async def _rolecreate(ctx_or_inter, name: str, colour: str = None):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    role_colour = discord.Color.default()
    colour_display = "Default"
    if colour:
        colour_clean = colour.lstrip("#").strip().lower()
        named = {
            "red": discord.Color.red(), "blue": discord.Color.blue(),
            "green": discord.Color.green(), "yellow": discord.Color.yellow(),
            "orange": discord.Color.orange(), "purple": discord.Color.purple(),
            "magenta": discord.Color.magenta(), "gold": discord.Color.gold(),
            "teal": discord.Color.teal(), "blurple": discord.Color.blurple(),
            "white": discord.Color.from_rgb(255, 255, 255),
            "black": discord.Color.from_rgb(0, 0, 0),
            "pink": discord.Color.from_rgb(255, 105, 180),
            "cyan": discord.Color.from_rgb(0, 255, 255),
            "lime": discord.Color.from_rgb(0, 255, 0),
        }
        if colour_clean in named:
            role_colour = named[colour_clean]
            colour_display = colour_clean.title()
        else:
            try:
                hex_val = int(colour_clean, 16)
                if 0 <= hex_val <= 0xFFFFFF:
                    role_colour = discord.Color(hex_val)
                    colour_display = f"#{colour_clean.upper()}"
                else:
                    return await respond(ctx_or_inter, content="❌ Invalid colour value.", ephemeral=True)
            except ValueError:
                return await respond(ctx_or_inter,
                    content="❌ Invalid colour. Use hex like `#ff0000` or a name: "
                            "`red` `blue` `green` `yellow` `orange` `purple` `magenta` "
                            "`gold` `teal` `blurple` `white` `black` `pink` `cyan` `lime`",
                    ephemeral=True)
    try:
        role = await guild.create_role(name=name, color=role_colour)
    except discord.Forbidden:
        return await respond(ctx_or_inter,
            content="❌ **Missing Permissions**\nI need **Manage Roles** permission.", ephemeral=True)
    e = discord.Embed(title="🎨 Role Created", color=role_colour,
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.add_field(name="Role", value=role.mention)
    e.add_field(name="Colour", value=colour_display)
    e.add_field(name="Created By", value=author.mention)
    await respond(ctx_or_inter, embed=e)
    await send_log(guild, e)

def find_role_by_name(guild: discord.Guild, name: str):
    return discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)

async def _role_add(ctx_or_inter, member: discord.Member, role: discord.Role):
    try:
        await member.add_roles(role)
    except discord.Forbidden:
        return await respond(ctx_or_inter,
            content="❌ **Missing Permissions**\nI need **Manage Roles** and my role must be above the target role.",
            ephemeral=True)
    await respond(ctx_or_inter, content=f"✅ Added **{role.name}** to {member.mention}.")

async def _role_remove(ctx_or_inter, member: discord.Member, role: discord.Role):
    try:
        await member.remove_roles(role)
    except discord.Forbidden:
        return await respond(ctx_or_inter,
            content="❌ **Missing Permissions**\nI need **Manage Roles** and my role must be above the target role.",
            ephemeral=True)
    await respond(ctx_or_inter, content=f"✅ Removed **{role.name}** from {member.mention}.")

async def _announce(ctx_or_inter, channel: discord.TextChannel, message: str):
    await channel.send(message)
    await respond(ctx_or_inter, content=f"📢 Sent to {channel.mention}.")

async def _poll(ctx_or_inter, question: str):
    e = discord.Embed(title="📊 Poll", description=question, color=discord.Color.blurple())
    if isinstance(ctx_or_inter, discord.Interaction):
        await ctx_or_inter.response.send_message(embed=e)
        msg = await ctx_or_inter.original_response()
    else:
        msg = await ctx_or_inter.send(embed=e)
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")

async def _report(ctx_or_inter, member: discord.Member, reason: str):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    cfg = get_config(guild.id)
    log_ch_id = cfg.get("log_channel")
    if not log_ch_id:
        return await respond(ctx_or_inter,
            content="⚠️ No log channel set. Ask an admin to run `/setlogchannel`.", ephemeral=True)
    ch = guild.get_channel(int(log_ch_id))
    if ch:
        e = mod_embed(discord.Color.red(), "🚨 User Report",
                      reported=member, reported_by=author, reason=reason)
        await ch.send(embed=e)
    await respond(ctx_or_inter, content="✅ Report submitted to moderators.", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
#  AUTORESPONDER
# ══════════════════════════════════════════════════════════════════════════════

async def _autoresponder(ctx_or_inter, trigger: str, message: str):
    guild = get_guild(ctx_or_inter)
    autoresponders_col.update_one(
        {"guild_id": guild.id, "trigger": trigger.lower()},
        {"$set": {"response": message}}, upsert=True,
    )
    e = discord.Embed(title="🤖 Autoresponder Set", color=discord.Color.blurple())
    e.add_field(name="Trigger", value=f"`{trigger}`", inline=False)
    e.add_field(name="Response", value=message, inline=False)
    await respond(ctx_or_inter, embed=e)

async def _autoresponder_remove(ctx_or_inter, trigger: str):
    guild = get_guild(ctx_or_inter)
    result = autoresponders_col.delete_one({"guild_id": guild.id, "trigger": trigger.lower()})
    if result.deleted_count:
        await respond(ctx_or_inter, content=f"✅ Autoresponder for `{trigger}` removed.")
    else:
        await respond(ctx_or_inter, content=f"❌ No autoresponder found for `{trigger}`.", ephemeral=True)

async def _autoresponder_list(ctx_or_inter):
    guild = get_guild(ctx_or_inter)
    docs = list(autoresponders_col.find({"guild_id": guild.id}))
    if not docs:
        return await respond(ctx_or_inter, content="ℹ️ No autoresponders set for this server.")
    e = discord.Embed(title="🤖 Autoresponders", color=discord.Color.blurple())
    for doc in docs[:25]:
        e.add_field(name=f"`{doc['trigger']}`", value=doc["response"], inline=False)
    await respond(ctx_or_inter, embed=e)

# ══════════════════════════════════════════════════════════════════════════════
#  AUTOPURGE
# ══════════════════════════════════════════════════════════════════════════════

def _parse_duration(raw: str) -> int | None:
    raw = raw.strip().lower()
    if raw.endswith("m"):
        try: return int(raw[:-1]) * 60
        except ValueError: return None
    elif raw.endswith("h"):
        try: return int(raw[:-1]) * 3600
        except ValueError: return None
    elif raw.endswith("d"):
        try: return int(raw[:-1]) * 86400
        except ValueError: return None
    return None

async def _autopurge(ctx_or_inter, channel: discord.TextChannel, timer: str):
    guild = get_guild(ctx_or_inter)
    seconds = _parse_duration(timer)
    if seconds is None or seconds < 60:
        return await respond(ctx_or_inter,
            content="❌ Invalid duration. Use `m`, `h`, or `d`. Minimum is 1m. Example: `10m`, `2h`, `1d`",
            ephemeral=True)
    autopurge_col.update_one(
        {"guild_id": guild.id, "channel_id": channel.id},
        {"$set": {"interval_seconds": seconds, "timer_str": timer, "last_purge": datetime.datetime.now(datetime.timezone.utc)}},
        upsert=True,
    )
    e = discord.Embed(title="🗑️ Autopurge Set", color=discord.Color.orange())
    e.add_field(name="Channel", value=channel.mention, inline=True)
    e.add_field(name="Interval", value=timer, inline=True)
    await respond(ctx_or_inter, embed=e)

async def _autopurge_remove(ctx_or_inter, channel: discord.TextChannel):
    guild = get_guild(ctx_or_inter)
    result = autopurge_col.delete_one({"guild_id": guild.id, "channel_id": channel.id})
    if result.deleted_count:
        await respond(ctx_or_inter, content=f"✅ Autopurge removed from {channel.mention}.")
    else:
        await respond(ctx_or_inter, content=f"❌ No autopurge found for {channel.mention}.", ephemeral=True)

async def _autopurge_list(ctx_or_inter):
    guild = get_guild(ctx_or_inter)
    docs = list(autopurge_col.find({"guild_id": guild.id}))
    if not docs:
        return await respond(ctx_or_inter, content="ℹ️ No autopurge schedules set.")
    e = discord.Embed(title="🗑️ Autopurge Schedules", color=discord.Color.orange())
    for doc in docs:
        ch = guild.get_channel(doc["channel_id"])
        ch_mention = ch.mention if ch else f"`#{doc['channel_id']}`"
        e.add_field(name=ch_mention, value=f"Every `{doc.get('timer_str', '?')}`", inline=False)
    await respond(ctx_or_inter, embed=e)

async def _run_autopurge_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            for doc in list(autopurge_col.find({})):
                last = doc.get("last_purge") or now
                if last.tzinfo is None:
                    last = last.replace(tzinfo=datetime.timezone.utc)
                if (now - last).total_seconds() >= doc.get("interval_seconds", 3600):
                    guild = bot.get_guild(doc["guild_id"])
                    if not guild:
                        continue
                    channel = guild.get_channel(doc["channel_id"])
                    if not channel:
                        continue
                    try:
                        await channel.purge(limit=None)
                        autopurge_col.update_one({"_id": doc["_id"]}, {"$set": {"last_purge": now}})
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(30)

# ══════════════════════════════════════════════════════════════════════════════
#  STICKY MESSAGES & STATUS
# ══════════════════════════════════════════════════════════════════════════════

async def _stickymessage(ctx_or_inter, channel: discord.TextChannel, message: str):
    guild = get_guild(ctx_or_inter)
    config_col.update_one(
        {"guild_id": guild.id, "sticky_channel": channel.id},
        {"$set": {"sticky_text": message, "sticky_msg_id": None}}, upsert=True
    )
    sent = await channel.send(f"📌 {message}")
    config_col.update_one(
        {"guild_id": guild.id, "sticky_channel": channel.id},
        {"$set": {"sticky_msg_id": sent.id}}
    )
    await respond(ctx_or_inter, content=f"✅ Sticky message set in {channel.mention}.")

async def _stickyremove(ctx_or_inter, channel: discord.TextChannel):
    guild = get_guild(ctx_or_inter)
    doc = config_col.find_one({"guild_id": guild.id, "sticky_channel": channel.id})
    if not doc:
        return await respond(ctx_or_inter, content=f"❌ No sticky message found in {channel.mention}.", ephemeral=True)
    prev_id = doc.get("sticky_msg_id")
    if prev_id:
        try:
            prev = await channel.fetch_message(prev_id)
            await prev.delete()
        except Exception:
            pass
    config_col.delete_one({"guild_id": guild.id, "sticky_channel": channel.id})
    await respond(ctx_or_inter, content=f"✅ Sticky message removed from {channel.mention}.")

async def _setstatus(ctx_or_inter, status_type: str, *, message: str):
    guild = get_guild(ctx_or_inter)
    atype = {
        "playing": discord.ActivityType.playing,
        "watching": discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
    }.get(status_type.lower(), discord.ActivityType.playing)
    await bot.change_presence(activity=discord.Activity(type=atype, name=message))
    set_config(guild.id, "bot_status", message)
    set_config(guild.id, "bot_status_type", status_type.lower())
    await respond(ctx_or_inter, content=f"✅ Bot status set to **{status_type.title()}** `{message}`.")

# ══════════════════════════════════════════════════════════════════════════════
#  AUTO STATUS ROLE
# ══════════════════════════════════════════════════════════════════════════════
# Automatically grants a configured role to members whose custom status
# contains a configured piece of text (case-insensitive substring match).
# Members who lose the required text are given a 30s grace period before
# the role is stripped and they're DM'd about it.

# In-memory: guild_id -> {user_id: datetime_utc_when_grace_period_ends}
# Rebuilt naturally as presences are (re)observed; not persisted because a
# restart simply means everyone gets a fresh 30s check, which is safe.
_asr_pending_removal: dict[int, dict[int, datetime.datetime]] = {}

def get_autostatusrole_config(guild_id: int) -> dict | None:
    return autostatusrole_col.find_one({"guild_id": guild_id})

def _asr_extract_status_text(member: discord.Member) -> str:
    """Pull the member's custom status text (if any) as a lowercase string."""
    if member is None:
        return ""
    for activity in getattr(member, "activities", []) or []:
        if isinstance(activity, discord.CustomActivity) and activity.name:
            return activity.name.lower()
    return ""

def _asr_status_matches(member: discord.Member, required_text: str) -> bool:
    status_text = _asr_extract_status_text(member)
    if not status_text:
        return False
    return required_text.lower() in status_text

async def _asr_evaluate_member(member: discord.Member, cfg: dict):
    """Core reconciliation logic for a single member against the Auto Status
    Role config. Safe to call repeatedly (from presence updates, the
    periodic fallback loop, or the manual check command)."""
    if member is None or member.bot:
        return
    guild = member.guild
    if guild is None:
        return
    role = guild.get_role(int(cfg["role_id"]))
    if role is None:
        return  # role was deleted — nothing we can do until reconfigured

    required_text = cfg["status_text"]
    matches = _asr_status_matches(member, required_text)
    has_role = role in member.roles
    guild_pending = _asr_pending_removal.setdefault(guild.id, {})

    if matches:
        # Cancel any pending removal — they're compliant again.
        guild_pending.pop(member.id, None)
        if not has_role:
            try:
                await member.add_roles(role, reason="Auto Status Role: required status detected")
            except (discord.Forbidden, discord.HTTPException):
                pass
    else:
        if has_role and member.id not in guild_pending:
            # Start the 30-second grace period before stripping the role.
            guild_pending[member.id] = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30)
        elif not has_role:
            guild_pending.pop(member.id, None)

async def _asr_process_grace_periods():
    """Checks every guild's pending-removal map for expired grace periods,
    re-verifies the member's status, and removes the role if the required
    text is still missing."""
    now = datetime.datetime.now(datetime.timezone.utc)
    for guild_id, pending in list(_asr_pending_removal.items()):
        if not pending:
            continue
        cfg = get_autostatusrole_config(guild_id)
        if not cfg:
            _asr_pending_removal.pop(guild_id, None)
            continue
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        role = guild.get_role(int(cfg["role_id"]))
        if role is None:
            continue

        for user_id, expires_at in list(pending.items()):
            if now < expires_at:
                continue
            member = guild.get_member(user_id)
            if member is None:
                pending.pop(user_id, None)
                continue
            # Re-check the status now, right before acting.
            if _asr_status_matches(member, cfg["status_text"]):
                pending.pop(user_id, None)
                if role not in member.roles:
                    try:
                        await member.add_roles(role, reason="Auto Status Role: required status detected")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                continue
            # Still missing — remove the role.
            pending.pop(user_id, None)
            if role in member.roles:
                try:
                    await member.remove_roles(role, reason="Auto Status Role: required status not detected after grace period")
                except (discord.Forbidden, discord.HTTPException):
                    pass

async def _run_autostatusrole_loop():
    """Periodic fallback: handles the 30s grace-period expiry (Discord has no
    'timer elapsed' event) and re-syncs any status changes that were missed
    by on_presence_update, e.g. across a restart."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _asr_process_grace_periods()
            for guild in bot.guilds:
                cfg = get_autostatusrole_config(guild.id)
                if not cfg:
                    continue
                role = guild.get_role(int(cfg["role_id"]))
                if role is None:
                    continue
                # Fallback sweep: only over members we already have cached
                # (avoids excessive API calls); presence updates handle the
                # real-time case, this just catches anything missed.
                for member in list(role.members):
                    await _asr_evaluate_member(member, cfg)
        except Exception as e:
            print(f"[autostatusrole_loop] error: {e}")
        await asyncio.sleep(30)

async def _autostatusrole_set(ctx_or_inter, status_text: str, role: discord.Role):
    guild = get_guild(ctx_or_inter)
    autostatusrole_col.update_one(
        {"guild_id": guild.id},
        {"$set": {"status_text": status_text, "role_id": role.id}},
        upsert=True,
    )
    e = discord.Embed(title="✅ Auto Status Role Configured", color=discord.Color.blurple())
    e.add_field(name="Required Status Text", value=f"`{status_text}`", inline=False)
    e.add_field(name="Role", value=role.mention, inline=False)
    e.add_field(name="Note", value="Text can appear anywhere in a member's custom status (case-insensitive).", inline=False)
    await respond(ctx_or_inter, embed=e)

async def _autostatusrole_remove(ctx_or_inter):
    guild = get_guild(ctx_or_inter)
    result = autostatusrole_col.delete_one({"guild_id": guild.id})
    _asr_pending_removal.pop(guild.id, None)
    if result.deleted_count:
        await respond(ctx_or_inter, content="✅ Auto Status Role configuration removed. The feature is now disabled for this server.")
    else:
        await respond(ctx_or_inter, content="❌ Auto Status Role is not configured for this server.", ephemeral=True)

async def _autostatusrole_status(ctx_or_inter):
    guild = get_guild(ctx_or_inter)
    cfg = get_autostatusrole_config(guild.id)
    if not cfg:
        return await respond(ctx_or_inter, content="ℹ️ Auto Status Role is not configured for this server.")
    role = guild.get_role(int(cfg["role_id"]))
    role_display = role.mention if role else f"`{cfg['role_id']}` (role deleted — reconfigure with `autostatusrole set`)"
    e = discord.Embed(title="🔎 Auto Status Role — Status", color=discord.Color.blurple())
    e.add_field(name="Required Status Text", value=f"`{cfg['status_text']}`", inline=False)
    e.add_field(name="Role", value=role_display, inline=False)
    e.add_field(name="Feature Status", value="🟢 Enabled" if role else "🔴 Disabled (role missing)", inline=False)
    pending_count = len(_asr_pending_removal.get(guild.id, {}))
    if pending_count:
        e.add_field(name="Pending Verifications", value=f"{pending_count} member(s) in the 30s grace period", inline=False)
    await respond(ctx_or_inter, embed=e)

async def _autostatusrole_check(ctx_or_inter, member: discord.Member):
    guild = get_guild(ctx_or_inter)
    cfg = get_autostatusrole_config(guild.id)
    if not cfg:
        return await respond(ctx_or_inter, content="❌ Auto Status Role is not configured for this server.", ephemeral=True)
    status_text = _asr_extract_status_text(member)
    matches = _asr_status_matches(member, cfg["status_text"])
    role = guild.get_role(int(cfg["role_id"]))
    e = discord.Embed(
        title="🔎 Auto Status Role — Manual Check",
        color=discord.Color.green() if matches else discord.Color.red(),
    )
    e.add_field(name="Member", value=member.mention, inline=False)
    e.add_field(name="Current Status", value=f"`{status_text}`" if status_text else "*No custom status set*", inline=False)
    e.add_field(name="Required Text", value=f"`{cfg['status_text']}`", inline=False)
    e.add_field(name="Match", value="✅ Yes" if matches else "❌ No", inline=True)
    if role:
        e.add_field(name="Has Role", value="✅ Yes" if role in member.roles else "❌ No", inline=True)
    await respond(ctx_or_inter, embed=e)
    # A manual check also nudges the reconciliation logic immediately.
    if cfg:
        await _asr_evaluate_member(member, cfg)

# ══════════════════════════════════════════════════════════════════════════════
#  DM ANNOUNCE / DM DELETE
# ══════════════════════════════════════════════════════════════════════════════

async def _dmannounce(ctx_or_inter, text: str):
    guild = get_guild(ctx_or_inter)
    author = get_author(ctx_or_inter)
    if isinstance(ctx_or_inter, discord.Interaction):
        await ctx_or_inter.response.defer(ephemeral=True)
    announce_id = str(uuid.uuid4())[:8].upper()
    sent_to = []
    failed = 0
    expire_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)
    for member in [m for m in guild.members if not m.bot]:
        try:
            msg = await member.send(
                f"📢 **Announcement from {guild.name}**\n\n{text}\n\n*Announcement ID: `{announce_id}`*"
            )
            sent_to.append({"user_id": member.id, "msg_id": msg.id})
        except Exception:
            failed += 1
    dm_announces_col.insert_one({
        "announce_id": announce_id, "guild_id": guild.id, "author_id": author.id,
        "text": text, "sent_to": sent_to,
        "created_at": datetime.datetime.now(datetime.timezone.utc), "expire_at": expire_at,
    })
    e = discord.Embed(title="📢 DM Announcement Sent", color=discord.Color.green(),
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.add_field(name="Announcement ID", value=f"`{announce_id}`")
    e.add_field(name="Sent To", value=f"{len(sent_to)} members")
    e.add_field(name="Failed", value=str(failed))
    e.set_footer(text=f"Use /dmdelete {announce_id} to delete the DMs")
    if isinstance(ctx_or_inter, discord.Interaction):
        await ctx_or_inter.followup.send(embed=e, ephemeral=True)
    else:
        await ctx_or_inter.send(embed=e)
    await send_log(guild, e)

async def _dmdelete(ctx_or_inter, announce_id: str):
    guild = get_guild(ctx_or_inter)
    if isinstance(ctx_or_inter, discord.Interaction):
        await ctx_or_inter.response.defer(ephemeral=True)
    doc = dm_announces_col.find_one({"announce_id": announce_id.upper(), "guild_id": guild.id})
    if not doc:
        msg = f"❌ Announcement ID `{announce_id.upper()}` not found or already expired."
        if isinstance(ctx_or_inter, discord.Interaction):
            return await ctx_or_inter.followup.send(msg, ephemeral=True)
        return await ctx_or_inter.send(msg)
    deleted = failed = 0
    for entry in doc.get("sent_to", []):
        try:
            user = bot.get_user(entry["user_id"]) or await bot.fetch_user(entry["user_id"])
            dm = await user.create_dm()
            msg = await dm.fetch_message(entry["msg_id"])
            await msg.delete()
            deleted += 1
        except Exception:
            failed += 1
    dm_announces_col.delete_one({"announce_id": announce_id.upper(), "guild_id": guild.id})
    e = discord.Embed(title="🗑️ DM Announcement Deleted", color=discord.Color.red(),
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.add_field(name="Announcement ID", value=f"`{announce_id.upper()}`")
    e.add_field(name="Messages Deleted", value=str(deleted))
    e.add_field(name="Failed", value=str(failed))
    if isinstance(ctx_or_inter, discord.Interaction):
        await ctx_or_inter.followup.send(embed=e, ephemeral=True)
    else:
        await ctx_or_inter.send(embed=e)
    await send_log(guild, e)

# ══════════════════════════════════════════════════════════════════════════════
#  TICKET BOT — SUPPORTED IMAGE HOSTS
# ══════════════════════════════════════════════════════════════════════════════

SUPPORTED_IMAGE_HOSTS = [
    "i.ibb.co", "ibb.co", "i.imgur.com", "imgur.com",
    "i.postimg.cc", "postimg.cc", "postimages.org",
    "cdn.discordapp.com", "media.discordapp.net",
    "images.unsplash.com", "i.gyazo.com", "gyazo.com",
    "prnt.sc", "i.prntscr.com", "puu.sh", "i.picsum.photos",
    "cdn.statically.io", "telegra.ph", "graph.org",
    "up.picr.de", "i.pinimg.com", "media.tenor.com", "c.tenor.com",
    "media.giphy.com", "i.giphy.com", "s3.amazonaws.com",
    "storage.googleapis.com", "imgpile.com", "snipboard.io",
    "i.snipboard.io", "paste.pics", "imagehost.io",
    "beeimg.com", "imgbox.com", "i.imgbox.com",
]

def is_valid_image_url(url: str) -> tuple[bool, str]:
    url = url.strip()
    if not url.startswith("https://"):
        return False, "URL must start with `https://` for security."
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().lstrip("www.")
        for supported in SUPPORTED_IMAGE_HOSTS:
            if host == supported or host.endswith("." + supported):
                return True, ""
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
            return True, ""
        return False, (
            f"Host `{host}` is not on the supported list.\n"
            "Upload your image to **[imgbb.com](https://imgbb.com)** (free, permanent, no login needed)."
        )
    except Exception:
        return False, "Could not parse the URL. Please check it and try again."

# ══════════════════════════════════════════════════════════════════════════════
#  TICKET BOT — VIEWS & MODALS
# ══════════════════════════════════════════════════════════════════════════════

class ImageURLModal(ui.Modal, title="📸 Submit Proof via Image URL"):
    image_url = ui.TextInput(
        label="Paste your image URL here",
        placeholder="https://i.ibb.co/xxxx/proof.png  or  https://i.imgur.com/xxxx.png",
        style=discord.TextStyle.short, required=True, max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        url = self.image_url.value.strip()
        valid, reason = is_valid_image_url(url)
        if not valid:
            return await interaction.response.send_message(
                embed=make_embed("❌ Invalid Image URL", reason, discord.Color.red()), ephemeral=True)
        deal = awaiting_proof.get(interaction.channel.id)
        if not deal:
            entry = proof_col.find_one({"channel_id": interaction.channel.id})
            if entry:
                deal = entry["deal"]
        if not deal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "No proof request found for this channel.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != deal["dealer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the dealer can submit proof.", discord.Color.red()), ephemeral=True)
        proof_ch = bot.get_channel(PROOF_CHANNEL_ID)
        if proof_ch:
            await proof_ch.send(embed=make_embed(
                title=f"# Sold {deal['product']}",
                description=(f"**Dealer:** <@{deal['dealer']}>\n**Buyer:** <@{deal['buyer']}>\n**Amount:** {deal['amount']}"),
                color=discord.Color.green(), image=url, footer="Proof of delivery — permanent image link"
            ))
        await interaction.response.send_message(
            embed=make_embed("✅ Proof Submitted", f"Your proof has been posted to <#{PROOF_CHANNEL_ID}>.", discord.Color.green()),
            ephemeral=True)
        await interaction.channel.send(embed=make_embed(
            "✅ Proof Forwarded", f"<@{deal['dealer']}>'s proof has been posted to <#{PROOF_CHANNEL_ID}>.", discord.Color.green()))
        awaiting_proof.pop(interaction.channel.id, None)
        proof_col.delete_one({"channel_id": interaction.channel.id})


class ProofChoiceView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🔗 Image URL (Permanent)", style=discord.ButtonStyle.primary, custom_id="proof_via_url")
    async def via_url(self, interaction: discord.Interaction, button: ui.Button):
        deal = awaiting_proof.get(interaction.channel.id)
        if not deal:
            entry = proof_col.find_one({"channel_id": interaction.channel.id})
            deal = entry["deal"] if entry else None
        if not deal or interaction.user.id != deal["dealer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the dealer can submit proof.", discord.Color.red()), ephemeral=True)
        await interaction.response.send_modal(ImageURLModal())

    @ui.button(label="🖼️ Direct Image Upload", style=discord.ButtonStyle.secondary, custom_id="proof_via_image")
    async def via_image(self, interaction: discord.Interaction, button: ui.Button):
        deal = awaiting_proof.get(interaction.channel.id)
        if not deal:
            entry = proof_col.find_one({"channel_id": interaction.channel.id})
            deal = entry["deal"] if entry else None
        if not deal or interaction.user.id != deal["dealer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the dealer can submit proof.", discord.Color.red()), ephemeral=True)
        await interaction.response.send_message(
            embed=make_embed(
                "🖼️ Send Your Image",
                "Please send your proof image as an **attachment** in this channel now.\n"
                "The bot will **automatically upload it to imgbb.com** for a permanent link.",
                discord.Color.blurple(),
                footer="Just attach and send the image — conversion is automatic."
            ), ephemeral=True)


class PaymentChoiceView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="💳 Pay via UPI", style=discord.ButtonStyle.primary, custom_id="persistent_pay_upi")
    async def pay_upi(self, interaction: discord.Interaction, button: ui.Button):
        deal = deals_col.find_one({"channel_id": interaction.channel.id})
        if not deal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Deal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != deal["buyer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer can select a payment method.", discord.Color.red()), ephemeral=True)
        upi_data = get_dealer_upi(deal["dealer"])
        if not upi_data:
            return await interaction.response.send_message(
                embed=make_embed("❌ UPI Not Set", "The dealer has not set a UPI address.", discord.Color.red()), ephemeral=True)
        embed = make_embed(
            title="💳 UPI Payment Details", description="Please pay using the UPI details below.",
            color=discord.Color.blue(),
            fields=[("📦 Product", deal["product"], True), ("💰 Amount", deal["amount"], True),
                    ("🪪 UPI ID", f"`{upi_data['upi_id']}`", False)],
            footer="Scan the QR code or copy the UPI ID to pay.", image=upi_data.get("image_url")
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="🪙 Pay via LTC", style=discord.ButtonStyle.secondary, custom_id="persistent_pay_ltc")
    async def pay_ltc(self, interaction: discord.Interaction, button: ui.Button):
        deal = deals_col.find_one({"channel_id": interaction.channel.id})
        if not deal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Deal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != deal["buyer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer can select a payment method.", discord.Color.red()), ephemeral=True)
        dealer_ltc = get_dealer_ltc(deal["dealer"])
        embed = make_embed(
            title="🪙 LTC Payment Details", description="Please send LTC to the address below.",
            color=discord.Color.gold(),
            fields=[("📦 Product", deal["product"], True), ("💰 Amount", deal["amount"], True),
                    ("🪙 LTC Address", f"{dealer_ltc}", False)],
            footer="Send exact amount and send screenshot once payment is sent."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class PostDealConfirmView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="✅ Confirm Delivery", style=discord.ButtonStyle.success, custom_id="confirm_delivery")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        deal = deals_col.find_one({"channel_id": interaction.channel.id})
        if not deal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Deal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != deal["buyer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer can confirm delivery.", discord.Color.red()), ephemeral=True)
        buyer_member = interaction.guild.get_member(deal["buyer"])
        buyer_role = interaction.guild.get_role(BUYER_ROLE_ID)
        if buyer_member and buyer_role and buyer_role not in buyer_member.roles:
            await buyer_member.add_roles(buyer_role)
        deals_col.delete_one({"channel_id": interaction.channel.id})
        active_deals.pop(interaction.channel.id, None)
        vouch_embed = make_embed(
            title="🎉 Thank You For Your Purchase!",
            description=(f"<@{deal['buyer']}> thanks for buying **{deal['product']}** from us!\nPlease vouch us — no vouch = no warranty."),
            color=discord.Color.green(),
            fields=[("📝 Vouch Text", f"+rep <@{deal['dealer']}> Legit Got {deal['product']} For {deal['amount']}", False),
                    ("📢 Vouch Channel", f"<#{VOUCH_CHANNEL_ID}>", False)],
            footer="We appreciate your trust!"
        )
        await interaction.response.edit_message(
            embed=make_embed("✅ Delivery Confirmed", "You've confirmed receiving the product.", discord.Color.green()), view=None)
        await interaction.channel.send(embed=vouch_embed)
        await send_html_transcript(interaction.channel, deal["buyer"])
        awaiting_proof[interaction.channel.id] = deal
        proof_col.update_one(
            {"channel_id": interaction.channel.id},
            {"$set": {"channel_id": interaction.channel.id, "deal": deal}}, upsert=True)
        await interaction.channel.send(
            content=f"<@{deal['dealer']}>",
            embed=make_embed(
                "📸 Proof Required",
                f"<@{deal['dealer']}>, please choose how you would like to submit your delivery proof.",
                discord.Color.orange(),
                fields=[("🔗 Image URL (Manual — Permanent)", "Already have an imgbb/imgur link? Paste it directly.", False),
                        ("🖼️ Direct Upload (Auto-Convert ✅)", "Send any image attachment — the bot will **automatically upload it to imgbb.com**.", False)],
                footer="Both methods produce a permanent proof link."
            ), view=ProofChoiceView())

    @ui.button(label="❌ Unconfirm Delivery", style=discord.ButtonStyle.danger, custom_id="unconfirm_delivery")
    async def unconfirm(self, interaction: discord.Interaction, button: ui.Button):
        deal = deals_col.find_one({"channel_id": interaction.channel.id})
        if not deal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Deal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != deal["buyer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer can unconfirm delivery.", discord.Color.red()), ephemeral=True)
        deals_col.update_one({"channel_id": interaction.channel.id}, {"$set": {**deal, "confirmed": False}}, upsert=True)
        active_deals[interaction.channel.id] = deal
        awaiting_proof.pop(interaction.channel.id, None)
        proof_col.delete_one({"channel_id": interaction.channel.id})
        buyer_member = interaction.guild.get_member(deal["buyer"])
        buyer_role = interaction.guild.get_role(BUYER_ROLE_ID)
        if buyer_member and buyer_role and buyer_role in buyer_member.roles:
            await buyer_member.remove_roles(buyer_role)
        await interaction.response.send_message(
            embed=make_embed("⚠️ Delivery Unconfirmed",
                "You have unconfirmed the delivery. Please contact the dealer if there's an issue.", discord.Color.orange()),
            ephemeral=True)


class VouchCopyView(ui.LayoutView):
    """Components V2 layout for the .vouch command.

    Shows the vouch text, a button that DMs/ephemerally sends back just the
    raw vouch text (for easy copy-paste), a pointer to the vouch channel, and
    a link button to the server invite (if SERVER_INVITE_LINK is configured).
    Works identically in guild channels and in DMs.
    """
    def __init__(self, vouch_text: str):
        super().__init__(timeout=None)
        self.vouch_text = vouch_text

        container = ui.Container(accent_color=discord.Color.purple())

        container.add_item(ui.TextDisplay(f"# Thanks For Purchasing From {SHOP_NAME}"))
        container.add_item(ui.TextDisplay("⭐ **Please Leave a Vouch for Us!**"))
        container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            f"**Vouch Text**\n```\n{vouch_text}\n```\n"
            f"Copy Text And Paste In <#{VOUCH_CHANNEL_ID}>"
        ))

        copy_button = ui.Button(
            label="Copy Vouch Text", emoji="📋",
            style=discord.ButtonStyle.secondary, custom_id="vouch_copy_button")
        copy_button.callback = self._send_copy
        copy_row = ui.ActionRow()
        copy_row.add_item(copy_button)
        container.add_item(copy_row)

        container.add_item(ui.Separator())

        if SERVER_INVITE_LINK:
            container.add_item(ui.TextDisplay("🔗 **Not Joined Server?** Then Join From Below Link"))
            link_row = ui.ActionRow()
            link_row.add_item(ui.Button(label="Server Link", emoji="🔗",
                                         style=discord.ButtonStyle.link, url=SERVER_INVITE_LINK))
            container.add_item(link_row)
        else:
            container.add_item(ui.TextDisplay(
                "🔗 **Not Joined Server?** Then Join From Below Link\n"
                "*(Server link not configured — set the SERVER_INVITE_LINK secret)*"))

        self.add_item(container)

    async def _send_copy(self, interaction: discord.Interaction):
        # Sends ONLY the raw vouch text, ready to be copy-pasted, in whichever
        # place the button was pressed. In a guild channel, make it ephemeral
        # so it doesn't clutter the channel for everyone else. In a DM, an
        # ephemeral flag is pointless (only the user can ever see it there
        # anyway) so just send it as a normal DM message.
        in_dm = interaction.guild is None
        await interaction.response.send_message(self.vouch_text, ephemeral=not in_dm)


class DealConfirmView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="✅ Confirm Deal", style=discord.ButtonStyle.success, custom_id="persistent_confirm_deal")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        proposal = proposals_col.find_one({"channel_id": interaction.channel.id})
        if not proposal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Proposal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != proposal["buyer_id"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer can confirm the deal.", discord.Color.red()), ephemeral=True)
        deal_doc = {
            "channel_id": interaction.channel.id, "buyer": proposal["buyer_id"],
            "dealer": proposal["dealer_id"], "product": proposal["product"],
            "amount": proposal["amount"], "confirmed": False
        }
        deals_col.update_one({"channel_id": interaction.channel.id}, {"$set": deal_doc}, upsert=True)
        active_deals[interaction.channel.id] = deal_doc
        proposals_col.delete_one({"channel_id": interaction.channel.id})
        deal_embed = make_embed(
            title="🤝 Deal Confirmed", description="The buyer has confirmed the deal details. Please send payment.",
            color=discord.Color.green(),
            fields=[("👤 Buyer", f"<@{proposal['buyer_id']}>", True), ("🧑‍💼 Dealer", f"<@{proposal['dealer_id']}>", True),
                    ("📦 Product", proposal["product"], False), ("💰 Amount", proposal["amount"], False)],
            footer="Dealer will deliver after payment is received."
        )
        await interaction.response.edit_message(
            embed=make_embed("✅ Deal Accepted", "You've confirmed the deal. Please select a payment method below.", discord.Color.green()),
            view=None)
        await interaction.channel.send(embed=deal_embed)
        dealer_upi = get_dealer_upi(proposal["dealer_id"])
        dealer_ltc = get_dealer_ltc(proposal["dealer_id"])
        if dealer_upi:
            payment_embed = make_embed(
                title="💰 Select Payment Method", description="Please choose how you'd like to pay:",
                color=discord.Color.blurple(),
                fields=[("📦 Product", proposal["product"], True), ("💰 Amount", proposal["amount"], True)])
            await interaction.channel.send(content=f"<@{proposal['buyer_id']}>", embed=payment_embed, view=PaymentChoiceView())
        else:
            ltc_embed = make_embed(
                title="🪙 LTC Payment Details", description="Please send payment to the LTC address below.",
                color=discord.Color.gold(),
                fields=[("📦 Product", proposal["product"], True), ("💰 Amount", proposal["amount"], True),
                        ("🪙 LTC Address", f"{dealer_ltc}", False)],
                footer="Send exact amount and DM the dealer once payment is sent.")
            await interaction.channel.send(content=f"<@{proposal['buyer_id']}>", embed=ltc_embed)

    @ui.button(label="❌ Cancel Deal", style=discord.ButtonStyle.danger, custom_id="persistent_cancel_deal")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        proposal = proposals_col.find_one({"channel_id": interaction.channel.id})
        if not proposal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Proposal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id not in (proposal["buyer_id"], proposal["dealer_id"]):
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer or dealer can cancel.", discord.Color.red()), ephemeral=True)
        proposals_col.delete_one({"channel_id": interaction.channel.id})
        await interaction.response.edit_message(
            embed=make_embed("↩️ Deal Cancelled", "The deal was cancelled.", discord.Color.red()), view=None)


class CloseConfirmView(ui.View):
    def __init__(self, invoker_id: int = 0):
        super().__init__(timeout=None)
        self.invoker_id = invoker_id

    @ui.button(label="✅ Yes, Close Ticket", style=discord.ButtonStyle.danger, custom_id="persistent_close_confirm")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.invoker_id:
            return await interaction.response.send_message("Only the command invoker can confirm.", ephemeral=True)
        dealer_role = interaction.guild.get_role(DEALER_ROLE_ID)
        tm_role = interaction.guild.get_role(TICKET_MANAGER_ROLE_ID)
        ticket = tickets_col.find_one({"channel_id": interaction.channel.id})
        owner = interaction.guild.get_member(ticket["owner_id"]) if ticket else None
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            dealer_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if tm_role:
            overwrites[tm_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if owner:
            overwrites[owner] = discord.PermissionOverwrite(view_channel=False)
        await interaction.channel.edit(overwrites=overwrites)
        await interaction.response.edit_message(
            embed=make_embed("🔒 Ticket Closed", "This ticket has been closed.", discord.Color.red()), view=None)
        self.stop()

    @ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary, custom_id="persistent_close_cancel")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.invoker_id:
            return await interaction.response.send_message("Only the command invoker can cancel.", ephemeral=True)
        await interaction.response.edit_message(
            embed=make_embed("↩️ Cancelled", "Ticket close was cancelled.", discord.Color.blurple()), view=None)
        self.stop()


async def _check_existing_ticket(interaction: discord.Interaction) -> bool:
    """Returns True (and sends ephemeral error) if user already has an open ticket."""
    existing = tickets_col.find_one({"owner_id": interaction.user.id, "guild_id": interaction.guild.id})
    if existing:
        existing_ch = interaction.guild.get_channel(existing["channel_id"])
        if existing_ch:
            await interaction.response.send_message(
                embed=make_embed("❌ Ticket Already Open",
                    f"You already have an open ticket: {existing_ch.mention}\n"
                    "Please use that one or ask staff to close it first.",
                    discord.Color.red()), ephemeral=True)
            return True
        else:
            tickets_col.delete_one({"owner_id": interaction.user.id, "guild_id": interaction.guild.id})
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  TICKET MODALS — collect issue/product details before creating channel
# ══════════════════════════════════════════════════════════════════════════════

class SupportTicketModal(ui.Modal, title="Support Ticket"):
    """Modal shown when a user opens a Support Ticket — collects Issue + Additional Info."""
    issue = ui.TextInput(
        label="Issue",
        placeholder="Briefly describe your issue...",
        style=discord.TextStyle.short,
        max_length=200,
        required=True,
    )
    additional_info = ui.TextInput(
        label="Additional Information",
        placeholder="Any extra details that may help us...",
        style=discord.TextStyle.long,
        max_length=500,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        dealer_role = interaction.guild.get_role(DEALER_ROLE_ID)
        tm_role     = interaction.guild.get_role(TICKET_MANAGER_ROLE_ID)
        user_id     = interaction.user.id
        channel_name = f"support-{interaction.user.name}"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user:               discord.PermissionOverwrite(view_channel=True, send_messages=True),
            dealer_role:                    discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if tm_role:
            overwrites[tm_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
        channel  = await interaction.guild.create_text_channel(
            name=channel_name, overwrites=overwrites, category=category)
        tickets_col.update_one({"channel_id": channel.id},
            {"$set": {"owner_id": user_id, "guild_id": interaction.guild.id, "ticket_type": "support"}}, upsert=True)
        tm_mention = tm_role.mention if tm_role else dealer_role.mention
        # Build welcome embed matching Image 1 layout
        issue_text    = str(self.issue)
        add_info_text = str(self.additional_info) or "N/A"
        user_avatar   = interaction.user.display_avatar.url
        ts            = int(interaction.created_at.timestamp())
        details_block = f"Issue: {issue_text}\nAdditional Information: {add_info_text}"
        welcome_embed = make_embed(
            title="Welcome to Your Ticket !",
            description=(
                f"Welcome to your ticket, {interaction.user.name}!\n"
                f"Our team has been notified and will be with you shortly. {tm_mention}\n\n"
                f"<:user:1546740287089545231>  **Owner:** {interaction.user.mention}\n"
                f"<:tix:1546740279644524594>  **Type:** Support\n"
                f"<:time:1546740270941347932>  **Created:** <t:{ts}:R>"
            ),
            color=discord.Color.from_rgb(44, 47, 74),
            thumbnail=user_avatar,
            fields=[
                (f"<:pin:1546740242512355348>  Details:", f"```{details_block}```", False),
            ],
            footer="Use .close to close this ticket when you're done.")
        # Ping user + TicketManager role (hidden ping in content)
        ping = f"{interaction.user.mention} {tm_mention}"
        await channel.send(content=ping, embed=welcome_embed, view=TicketManageView())
        await interaction.response.send_message(
            embed=make_embed("✅ Support Ticket Created", f"Your support ticket: {channel.mention}", discord.Color.green()),
            ephemeral=True)


class BuyTicketModal(ui.Modal, title="Buy Ticket"):
    """Modal shown when a user opens a Buy Ticket — collects Product, Quantity, Payment Mode."""
    product = ui.TextInput(
        label="Product",
        placeholder="What would you like to buy?",
        style=discord.TextStyle.short,
        max_length=200,
        required=True,
    )
    quantity = ui.TextInput(
        label="Quantity",
        placeholder="How many? (e.g. 1, 2, 5...)",
        style=discord.TextStyle.short,
        max_length=50,
        required=True,
    )
    payment_mode = ui.TextInput(
        label="Mode of Payment",
        placeholder="e.g. LTC, UPI, PayPal...",
        style=discord.TextStyle.short,
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        dealer_role = interaction.guild.get_role(DEALER_ROLE_ID)
        tm_role     = interaction.guild.get_role(TICKET_MANAGER_ROLE_ID)
        user_id     = interaction.user.id
        channel_name = f"buy-{interaction.user.name}"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user:               discord.PermissionOverwrite(view_channel=True, send_messages=True),
            dealer_role:                    discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if tm_role:
            overwrites[tm_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
        channel  = await interaction.guild.create_text_channel(
            name=channel_name, overwrites=overwrites, category=category)
        tickets_col.update_one({"channel_id": channel.id},
            {"$set": {"owner_id": user_id, "guild_id": interaction.guild.id, "ticket_type": "buy"}}, upsert=True)
        tm_mention = tm_role.mention if tm_role else dealer_role.mention
        # Build welcome embed matching Image 1 layout
        product_text  = str(self.product)
        qty_text      = str(self.quantity)
        payment_text  = str(self.payment_mode)
        user_avatar   = interaction.user.display_avatar.url
        ts            = int(interaction.created_at.timestamp())
        details_block = f"Product: {product_text}\nQuantity: {qty_text}\nMode of Payment: {payment_text}"
        welcome_embed = make_embed(
            title="Welcome to Your Ticket !",
            description=(
                f"Welcome to your ticket, {interaction.user.name}!\n"
                f"Our team has been notified and will be with you shortly. {tm_mention}\n\n"
                f"<:user:1546740287089545231>  **Owner:** {interaction.user.mention}\n"
                f"<:tix:1546740279644524594>  **Type:** Buy\n"
                f"<:time:1546740270941347932>  **Created:** <t:{ts}:R>"
            ),
            color=discord.Color.from_rgb(44, 47, 74),
            thumbnail=user_avatar,
            fields=[
                (f"<:pin:1546740242512355348>  Details:", f"```{details_block}```", False),
            ],
            footer="Use .close to close this ticket when you're done.")
        # Ping user + TicketManager role
        ping = f"{interaction.user.mention} {tm_mention}"
        await channel.send(content=ping, embed=welcome_embed, view=TicketManageView())
        await interaction.response.send_message(
            embed=make_embed("✅ Buy Ticket Created", f"Your ticket: {channel.mention}", discord.Color.green()),
            ephemeral=True)


class TicketManageView(ui.View):
    """Claim / Close buttons shown inside ticket channels (matching the screenshot)."""
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, custom_id="ticket_manage_claim")
    async def claim_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not can_manage_ticket(interaction.user):
            return await interaction.response.send_message(
                "❌ Only staff can claim a ticket.", ephemeral=True)
        await interaction.response.send_message(
            f"✅ Ticket claimed by {interaction.user.mention}.", ephemeral=False)

    @ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_manage_close")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not can_manage_ticket(interaction.user):
            return await interaction.response.send_message(
                "❌ Only staff can close a ticket.", ephemeral=True)
        deal_doc = deals_col.find_one({"channel_id": interaction.channel.id})
        if deal_doc and not is_head_dealer(interaction.user):
            return await interaction.response.send_message(
                embed=make_embed("⚠️ Active Deal",
                    "Cannot close — there is an active deal in this ticket.\nOnly a Head Dealer can force close.",
                    discord.Color.orange()), ephemeral=True)
        await interaction.response.send_message(
            embed=make_embed("🔒 Close Ticket?",
                "Are you sure you want to close this ticket? The buyer will lose access.",
                discord.Color.orange()),
            view=CloseConfirmView(interaction.user.id))


class PersistentTicketView(ui.View):
    """Full panel — 🛒 Buy Ticket + 🎧 Support Ticket."""
    def __init__(self, timeout=None):
        super().__init__(timeout=timeout)

    # ── Buy Ticket ──────────────────────────────────────────────────────────────
    @ui.button(label="🛒 Buy Ticket", style=discord.ButtonStyle.primary, custom_id="persistent_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if await _check_existing_ticket(interaction):
            return
        await interaction.response.send_modal(BuyTicketModal())

    # ── Support Ticket ──────────────────────────────────────────────────────────
    @ui.button(label="🎧 Support Ticket", style=discord.ButtonStyle.secondary, custom_id="persistent_open_support")
    async def open_support(self, interaction: discord.Interaction, button: ui.Button):
        if await _check_existing_ticket(interaction):
            return
        await interaction.response.send_modal(SupportTicketModal())


class PersistentSupportView(ui.View):
    """Support-only panel — 🎧 Support Ticket button only."""
    def __init__(self, timeout=None):
        super().__init__(timeout=timeout)

    @ui.button(label="🎧 Support Ticket", style=discord.ButtonStyle.secondary, custom_id="persistent_open_support_only")
    async def open_support(self, interaction: discord.Interaction, button: ui.Button):
        if await _check_existing_ticket(interaction):
            return
        await interaction.response.send_modal(SupportTicketModal())


class PanelSelect(ui.Select):
    """Dropdown with Buy / Support options for the main panel."""
    def __init__(self):
        options = [
            discord.SelectOption(label="Buy", emoji="🛒", description="Open a ticket to buy something", value="buy"),
            discord.SelectOption(label="Support", emoji="🎧", description="Open a ticket for support/help", value="support"),
        ]
        super().__init__(placeholder="Make a selection", min_values=1, max_values=1,
                          options=options, custom_id="persistent_panel_select")

    async def callback(self, interaction: discord.Interaction):
        if await _check_existing_ticket(interaction):
            return
        dealer_role = interaction.guild.get_role(DEALER_ROLE_ID)
        tm_role     = interaction.guild.get_role(TICKET_MANAGER_ROLE_ID)
        user_id     = interaction.user.id
        choice      = self.values[0]

        if choice == "buy":
            await interaction.response.send_modal(BuyTicketModal())

        else:  # support
            await interaction.response.send_modal(SupportTicketModal())


class PersistentPanelSelectView(ui.View):
    """Main panel — dropdown menu with Buy / Support options."""
    def __init__(self, timeout=None):
        super().__init__(timeout=timeout)
        self.add_item(PanelSelect())


# ══════════════════════════════════════════════════════════════════════════════
#  TICKET BOT — HTML TRANSCRIPT
# ══════════════════════════════════════════════════════════════════════════════

def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>"))

def _color_int_to_hex(color_int: int) -> str:
    if not color_int:
        return "#5865f2"
    return f"#{color_int:06x}"

async def _permanent_url(att_url: str, filename: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(att_url) as resp:
                img_bytes = await resp.read()
        perm = await upload_to_imgbb(img_bytes, filename)
        if perm:
            return perm
    except Exception:
        pass
    return att_url

async def _render_embed(embed: discord.Embed) -> str:
    color_hex = _color_int_to_hex(embed.color.value if embed.color else 0)
    thumbnail_html = ""
    if embed.thumbnail and embed.thumbnail.url:
        thumb_url = await _permanent_url(embed.thumbnail.url, "thumb.png")
        thumbnail_html = f'<img class="embed-thumbnail" src="{thumb_url}" alt="thumbnail">'
    author_html = ""
    if embed.author and embed.author.name:
        author_icon = f'<img class="embed-author-icon" src="{embed.author.icon_url}" alt="">' if embed.author.icon_url else ""
        author_html = f'<div class="embed-author">{author_icon}<span>{_escape(embed.author.name)}</span></div>'
    title_html = ""
    if embed.title:
        title_text = _escape(embed.title)
        title_html = f'<div class="embed-title"><a href="{embed.url}">{title_text}</a></div>' if embed.url else f'<div class="embed-title">{title_text}</div>'
    desc_html = f'<div class="embed-description">{_escape(embed.description)}</div>' if embed.description else ""
    fields_html = ""
    if embed.fields:
        fields_inner = ""
        for field in embed.fields:
            inline_class = "embed-field-inline" if field.inline else "embed-field"
            fields_inner += f'<div class="{inline_class}"><div class="embed-field-name">{_escape(field.name)}</div><div class="embed-field-value">{_escape(field.value)}</div></div>'
        fields_html = f'<div class="embed-fields">{fields_inner}</div>'
    image_html = ""
    if embed.image and embed.image.url:
        img_url = await _permanent_url(embed.image.url, "embed_image.png")
        image_html = f'<div class="embed-image-wrap"><img class="embed-image" src="{img_url}" alt="embed image"></div>'
    footer_html = ""
    if embed.footer and embed.footer.text:
        footer_icon = f'<img class="embed-footer-icon" src="{embed.footer.icon_url}" alt="">' if embed.footer.icon_url else ""
        footer_html = f'<div class="embed-footer">{footer_icon}<span>{_escape(embed.footer.text)}</span></div>'
    return f'''<div class="embed" style="border-left:4px solid {color_hex};">
        <div class="embed-inner"><div class="embed-content-wrap"><div class="embed-content">
            {author_html}{title_html}{desc_html}{fields_html}{image_html}{footer_html}
        </div>{thumbnail_html}</div></div></div>'''

async def create_html_transcript(channel) -> bytes:
    messages = []
    async for msg in channel.history(oldest_first=True, limit=None):
        messages.append(msg)
    def avatar_color(user_id: int) -> str:
        colors = ["#5865f2","#57f287","#fee75c","#eb459e","#ed4245","#00b0f4","#faa61a"]
        return colors[user_id % len(colors)]
    rows = []
    prev_author_id = prev_ts_minute = None
    for msg in messages:
        ts_dt = msg.created_at
        ts_str = ts_dt.strftime("%d/%m/%Y %I:%M %p")
        ts_minute = ts_dt.strftime("%Y-%m-%d %H:%M")
        is_bot = msg.author.bot
        author_color = avatar_color(msg.author.id)
        initial = msg.author.display_name[0].upper() if msg.author.display_name else "?"
        same_group = (msg.author.id == prev_author_id and ts_minute == prev_ts_minute)
        prev_author_id = msg.author.id
        prev_ts_minute = ts_minute
        reply_html = ""
        if msg.reference and msg.reference.resolved and isinstance(msg.reference.resolved, discord.Message):
            ref = msg.reference.resolved
            ref_content = _escape(ref.content[:80] + ("…" if len(ref.content) > 80 else "")) if ref.content else "[embed/attachment]"
            reply_html = f'<div class="reply-bar"><div class="reply-avatar" style="background:{avatar_color(ref.author.id)};">{ref.author.display_name[0].upper()}</div><span class="reply-name">{_escape(ref.author.display_name)}</span><span class="reply-content">{ref_content}</span></div>'
        content_html = f'<div class="message-content">{_escape(msg.content)}</div>' if msg.content else ""
        attachments_html = ""
        for att in msg.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                perm = await _permanent_url(att.url, att.filename)
                attachments_html += f'<div class="attachment-image-wrap"><img class="attachment-image" src="{perm}" alt="{_escape(att.filename)}"></div>'
            else:
                attachments_html += f'<div class="attachment-file">📎 <a href="{att.url}">{_escape(att.filename)}</a> <span class="file-size">({round(att.size/1024,1)} KB)</span></div>'
        embeds_html = ""
        for emb in msg.embeds:
            embeds_html += await _render_embed(emb)
        reactions_html = ""
        if msg.reactions:
            rxn_items = "".join(f'<span class="reaction"><span class="reaction-emoji">{str(r.emoji)}</span><span class="reaction-count">{r.count}</span></span>' for r in msg.reactions)
            reactions_html = f'<div class="reactions">{rxn_items}</div>'
        bot_badge = '<span class="bot-badge">BOT</span>' if is_bot else ""
        if same_group:
            row = f'<div class="message-row compact"><div class="compact-ts">{ts_dt.strftime("%I:%M %p")}</div><div class="message-body">{reply_html}{content_html}{attachments_html}{embeds_html}{reactions_html}</div></div>'
        else:
            row = f'''<div class="message-row"><div class="avatar-col"><div class="avatar" style="background:{author_color};">{initial}</div></div>
                <div class="message-body">{reply_html}<div class="message-meta"><span class="username" style="color:{author_color};">{_escape(msg.author.display_name)}</span>{bot_badge}<span class="timestamp">{ts_str}</span></div>
                {content_html}{attachments_html}{embeds_html}{reactions_html}</div></div>'''
        rows.append(row)
    rows_html = "\n".join(rows)
    guild_name = channel.guild.name if channel.guild else "Direct Message"
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>#{channel.name} — Transcript</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:"gg sans","Noto Sans",Whitney,"Helvetica Neue",Helvetica,Arial,sans-serif;background:#313338;color:#dbdee1;font-size:16px;line-height:1.375;}}
  .transcript-header{{background:#2b2d31;padding:16px 24px;border-bottom:1px solid #1e1f22;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10;}}
  .header-hash{{color:#80848e;font-size:1.4em;font-weight:700;}}.header-channel{{color:#f2f3f5;font-size:1.05em;font-weight:600;}}
  .header-guild{{color:#80848e;font-size:0.82em;margin-left:auto;}}.header-badge{{background:#5865f2;color:#fff;border-radius:4px;padding:2px 7px;font-size:0.72em;font-weight:700;letter-spacing:.5px;}}
  .messages{{padding:16px 0 40px;}}.message-row{{display:flex;gap:0;padding:2px 16px;min-height:44px;align-items:flex-start;transition:background .1s;}}
  .message-row:hover{{background:#2e3035;}}.message-row.compact{{display:flex;padding:1px 16px;align-items:flex-start;}}.message-row.compact:hover{{background:#2e3035;}}
  .avatar-col{{width:40px;margin-right:16px;padding-top:2px;flex-shrink:0;}}.avatar{{width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:1.1em;color:#fff;flex-shrink:0;}}
  .compact-ts{{width:56px;color:#80848e;font-size:0.68em;text-align:right;padding-right:8px;padding-top:4px;flex-shrink:0;opacity:0;}}.message-row.compact:hover .compact-ts{{opacity:1;}}
  .message-body{{flex:1;min-width:0;}}.message-meta{{display:flex;align-items:baseline;gap:8px;margin-bottom:2px;}}.username{{font-weight:600;font-size:0.95em;cursor:pointer;}}.username:hover{{text-decoration:underline;}}
  .timestamp{{color:#80848e;font-size:0.72em;}}.bot-badge{{background:#5865f2;color:#fff;border-radius:4px;padding:0 5px;font-size:0.62em;font-weight:700;letter-spacing:.5px;line-height:1.6;}}
  .message-content{{color:#dbdee1;font-size:0.92em;word-break:break-word;white-space:pre-wrap;}}
  .reply-bar{{display:flex;align-items:center;gap:6px;margin-bottom:4px;padding-left:8px;border-left:2px solid #4e5058;}}
  .reply-avatar{{width:16px;height:16px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:0.5em;font-weight:700;color:#fff;flex-shrink:0;}}
  .reply-name{{color:#80848e;font-size:0.78em;font-weight:600;}}.reply-content{{color:#80848e;font-size:0.78em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:400px;}}
  .attachment-image-wrap{{margin-top:8px;}}.attachment-image{{max-width:520px;max-height:400px;border-radius:4px;display:block;cursor:zoom-in;}}
  .attachment-file{{display:inline-flex;align-items:center;gap:8px;background:#2b2d31;border:1px solid #1e1f22;border-radius:4px;padding:10px 16px;margin-top:8px;font-size:0.88em;}}
  .attachment-file a{{color:#00aff4;text-decoration:none;}}.attachment-file a:hover{{text-decoration:underline;}}.file-size{{color:#80848e;font-size:0.82em;}}
  .embed{{margin-top:8px;max-width:520px;background:#2b2d31;border-radius:4px;overflow:hidden;}}.embed-inner{{padding:12px 16px 16px;}}
  .embed-content-wrap{{display:flex;gap:16px;}}.embed-content{{flex:1;min-width:0;}}
  .embed-author{{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:0.83em;font-weight:600;color:#dbdee1;}}.embed-author-icon{{width:24px;height:24px;border-radius:50%;}}
  .embed-title{{font-weight:700;font-size:0.92em;color:#00aff4;margin-bottom:6px;}}.embed-title a{{color:#00aff4;text-decoration:none;}}.embed-title a:hover{{text-decoration:underline;}}
  .embed-description{{font-size:0.85em;color:#dbdee1;margin-bottom:8px;white-space:pre-wrap;word-break:break-word;}}
  .embed-fields{{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;}}.embed-field{{width:100%;}}.embed-field-inline{{width:calc(33% - 6px);min-width:100px;}}
  .embed-field-name{{font-size:0.8em;font-weight:700;color:#dbdee1;margin-bottom:2px;}}.embed-field-value{{font-size:0.83em;color:#b5bac1;white-space:pre-wrap;word-break:break-word;}}
  .embed-image-wrap{{margin-top:12px;}}.embed-image{{max-width:100%;max-height:350px;border-radius:4px;display:block;}}.embed-thumbnail{{width:80px;height:80px;object-fit:cover;border-radius:4px;flex-shrink:0;}}
  .embed-footer{{display:flex;align-items:center;gap:6px;margin-top:12px;font-size:0.75em;color:#80848e;}}.embed-footer-icon{{width:16px;height:16px;border-radius:50%;}}
  .reactions{{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px;}}.reaction{{display:inline-flex;align-items:center;gap:4px;background:#2e3035;border:1px solid #3c3f45;border-radius:8px;padding:2px 8px;font-size:0.82em;}}
  .reaction-emoji{{font-size:1em;}}.reaction-count{{color:#b5bac1;font-weight:600;font-size:0.85em;}}
  ::-webkit-scrollbar{{width:8px;}}::-webkit-scrollbar-track{{background:#2b2d31;}}::-webkit-scrollbar-thumb{{background:#1a1b1e;border-radius:4px;}}
</style></head><body>
<div class="transcript-header"><span class="header-hash">#</span><span class="header-channel">{_escape(channel.name)}</span><span class="header-badge">TRANSCRIPT</span><span class="header-guild">{_escape(guild_name)}</span></div>
<div class="messages">{rows_html}</div></body></html>"""
    return html.encode("utf-8")

async def send_html_transcript(channel, buyer_id):
    html_bytes = await create_html_transcript(channel)
    filename = f"{channel.name}_transcript.html"
    try:
        buyer = await bot.fetch_user(buyer_id)
        await buyer.send(
            embed=make_embed("📄 Ticket Transcript", f"Here's your transcript for `#{channel.name}`.", discord.Color.blurple()),
            file=discord.File(io.BytesIO(html_bytes), filename=filename))
    except Exception:
        pass
    transcript_ch = bot.get_channel(TRANSCRIPT_CHANNEL_ID)
    if transcript_ch:
        await transcript_ch.send(
            embed=make_embed("📄 Transcript Saved", f"Transcript for ticket `#{channel.name}`.", discord.Color.blurple()),
            file=discord.File(io.BytesIO(html_bytes), filename=filename))

# ══════════════════════════════════════════════════════════════════════════════
#  SHOP SYSTEM — LTC CRYPTO HELPERS
# ══════════════════════════════════════════════════════════════════════════════

LTC_TO_USD_RATE = None
LTC_RATE_CACHED_AT = None

def _get_balance(user_id: int) -> float:
    doc = balances_col.find_one({"user_id": user_id})
    return float(doc["balance"]) if doc else 0.0

def _add_balance(user_id: int, amount: float):
    balances_col.update_one({"user_id": user_id}, {"$inc": {"balance": amount}}, upsert=True)

import operator as _operator

_CALC_OPS = {
    ast.Add: _operator.add, ast.Sub: _operator.sub,
    ast.Mult: _operator.mul, ast.Div: _operator.truediv,
    ast.FloorDiv: _operator.floordiv, ast.Mod: _operator.mod,
    ast.Pow: _operator.pow, ast.USub: _operator.neg, ast.UAdd: _operator.pos,
}

def _safe_eval_math(expr: str):
    """Evaluates a math expression using ast, never Python's eval() —
    only numbers and +-*/%**() are allowed, nothing else can execute."""
    node = ast.parse(expr, mode="eval").body

    def _ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _CALC_OPS:
            return _CALC_OPS[type(n.op)](_ev(n.left), _ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _CALC_OPS:
            return _CALC_OPS[type(n.op)](_ev(n.operand))
        raise ValueError("Unsupported expression")

    return _ev(node)

def _build_calc_embed(expression: str) -> discord.Embed:
    try:
        result = _safe_eval_math(expression)
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        e = discord.Embed(color=0x5865F2, timestamp=datetime.datetime.now(datetime.timezone.utc))
        e.add_field(name="\u200b", value=(
            f"🧮 **Expression:** `{expression}`\n"
            f"<:tick:1546740263437738055> **Result:** `{result}`"), inline=False)
    except ZeroDivisionError:
        e = discord.Embed(color=0xED4245, description="❌ Can't divide by zero.")
    except Exception:
        e = discord.Embed(color=0xED4245, description=(
            f"❌ Couldn't evaluate `{expression}`. Use only numbers and `+ - * / % **`."))
    return e

def _get_ltc(user_id: int):
    doc = ltc_wallets_col.find_one({"user_id": user_id})
    return doc["ltc_address"] if doc else None

async def _fetch_ltc_address_full(address: str):
    url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}"
    params = {"token": BLOCKCYPHER_TOKEN, "limit": 3} if BLOCKCYPHER_TOKEN else {"limit": 3}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
    except Exception:
        pass
    return None

async def _fetch_ltc_tx(txid: str):
    url = f"https://api.blockcypher.com/v1/ltc/main/txs/{txid}"
    params = {"token": BLOCKCYPHER_TOKEN} if BLOCKCYPHER_TOKEN else {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
    except Exception:
        pass
    return None

async def _get_ltc_usd_rate() -> float:
    global LTC_TO_USD_RATE, LTC_RATE_CACHED_AT
    now = datetime.datetime.now(datetime.timezone.utc)
    if LTC_TO_USD_RATE and LTC_RATE_CACHED_AT and (now - LTC_RATE_CACHED_AT).total_seconds() < 300:
        return LTC_TO_USD_RATE
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "litecoin", "vs_currencies": "usd,eur"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    LTC_TO_USD_RATE = data.get("litecoin", {}).get("usd", 80.0)
                    LTC_RATE_CACHED_AT = now
                    return LTC_TO_USD_RATE
    except Exception:
        pass
    return LTC_TO_USD_RATE or 80.0

async def _dm_tx_confirmation(client: commands.Bot, data: dict, txid: str):
    outputs = data.get("outputs", [])
    confirmations = data.get("confirmations", 0)
    received_at = data.get("received", "Unknown")
    for out in outputs:
        for addr in out.get("addresses", []):
            ltc_val = out.get("value", 0) / 1e8
            doc = ltc_wallets_col.find_one({"ltc_address": addr})
            if not doc:
                continue
            try:
                user = client.get_user(doc["user_id"]) or await client.fetch_user(doc["user_id"])
                dm_embed = discord.Embed(title="<:tick:1546740263437738055> Payment Confirmed!", color=0x57F287,
                                         timestamp=datetime.datetime.now(datetime.timezone.utc))
                dm_embed.add_field(name="\u200b", value=(
                    f"<a:blueverify:1546740183716855872> A LTC transaction to your wallet has been confirmed.\n\n"
                    f"<:cash:1546740198870749224> **Amount:** `{ltc_val:.8f} LTC`\n"
                    f"<:blue1:1546740168923553842> **TXID:** `{txid}`\n"
                    f"<:tick:1546740263437738055> **Confirmations:** `{confirmations}`\n"
                    f"<:curvedarrow:1546740206164643933> **Your Address:** `{addr}`\n"
                    f"<:blue_box:1546740176041283594> **Received:** `{received_at}`"), inline=False)
                dm_embed.set_footer(text=f"LTC Payment")
                await user.send(embed=dm_embed)
            except Exception:
                pass

def _next_order_number(guild_id: int) -> int:
    result = config_col.find_one_and_update(
        {"guild_id": guild_id}, {"$inc": {"order_counter": 1}},
        upsert=True, return_document=True)
    return result.get("order_counter", 1)

# ══════════════════════════════════════════════════════════════════════════════
#  SELLAUTH — auto order-complete embed on purchase
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_sellauth_invoice(invoice_id):
    """Pulls the full invoice from the SellAuth API (webhook only gives an id)."""
    url = f"https://api.sellauth.com/v1/shops/{SELLAUTH_SHOP_ID}/invoices/{invoice_id}"
    headers = {"Authorization": f"Bearer {SELLAUTH_API_KEY}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"SellAuth API {resp.status}: {await resp.text()}")
            return await resp.json()


def _find_sellauth_target_guild():
    """
    No separate 'which guild' setting needed — reuses whichever guild(s)
    already have an order or feedback channel configured, and picks the
    first one the bot is currently in.
    """
    for cfg in config_col.find({
        "$or": [
            {"order_channel": {"$exists": True, "$ne": None}},
            {"feedback_channel": {"$exists": True, "$ne": None}},
        ]
    }):
        guild = bot.get_guild(cfg.get("guild_id"))
        if guild is not None:
            return guild
    return None


def _extract_product_and_price(invoice):
    """Shared, defensive extraction used by both order and feedback flows.
    Appends " (Quantity)" to the product name when more than 1 was bought.
    """
    product = "Unknown product"
    for key in ("cart", "items", "products", "cart_items", "invoice_items"):
        collection = invoice.get(key)
        if collection and isinstance(collection, list):
            names = []
            for entry in collection:
                if not isinstance(entry, dict):
                    continue
                name = (
                    entry.get("product_name")
                    or entry.get("name")
                    or (entry.get("product") or {}).get("name")
                    or (entry.get("product_variant") or {}).get("name")
                )
                if not name:
                    continue
                qty = (
                    entry.get("quantity") or entry.get("qty")
                    or entry.get("amount_purchased") or 1
                )
                try:
                    qty = int(qty)
                except (TypeError, ValueError):
                    qty = 1
                names.append(f"{name} ({qty})" if qty > 1 else name)
            if names:
                product = ", ".join(names)
                break
    if product == "Unknown product":
        product = (
            invoice.get("product_name")
            or invoice.get("product")
            or ((invoice.get("product") or {}).get("name") if isinstance(invoice.get("product"), dict) else None)
        ) or "Unknown product"
        # Top-level quantity fallback, for invoices with no line-item list.
        top_qty = invoice.get("quantity") or invoice.get("qty")
        try:
            top_qty = int(top_qty)
        except (TypeError, ValueError):
            top_qty = None
        if top_qty and top_qty > 1 and product != "Unknown product":
            product = f"{product} ({top_qty})"

    price = (
        invoice.get("price_usd") or invoice.get("paid_usd") or invoice.get("total")
        or invoice.get("price") or invoice.get("amount") or invoice.get("subtotal") or "0"
    )
    return product, f"{price}$"


async def process_sellauth_invoice(invoice_id):
    """
    Fetches a completed SellAuth invoice and posts the SAME order-complete
    embed style as .ordercomplete, with seller always shown as "SellAuth".
    """
    guild = _find_sellauth_target_guild()
    if guild is None:
        print("⚠️ No guild with an order channel configured (.setorderchannel) — skipping SellAuth order post.")
        return

    # Idempotency — SellAuth may retry a webhook delivery; don't double-post.
    if orders_col.find_one({"sellauth_invoice_id": invoice_id}):
        print(f"⏭️ SellAuth invoice #{invoice_id} already posted — skipping duplicate.")
        return

    invoice = await _fetch_sellauth_invoice(invoice_id)

    # Only post once an invoice is actually paid/completed.
    status = (invoice.get("status") or "").lower()
    print(f"📦 SellAuth invoice #{invoice_id} status: {status!r}")
    print(f"📦 SellAuth invoice #{invoice_id} RAW: {json.dumps(invoice, default=str)}")
    if status not in ("completed", "paid"):
        print(f"⏭️ Skipping invoice #{invoice_id} — status '{status}' not completed/paid yet.")
        return

    product, amount = _extract_product_and_price(invoice)

    # Resolve buyer — prefer a linked Discord account so we can @mention +
    # auto-assign the Customer role, otherwise fall back to their email.
    buyer_display = "[SellAuth Buyer](https://elfmart.mysellauth.com)"

    seller_username = "SellAuth"
    order_num = _next_order_number(guild.id)
    now = datetime.datetime.now(datetime.timezone.utc)

    orders_col.insert_one({
        "order_num": order_num, "guild_id": guild.id,
        "seller_id": bot.user.id if bot.user else 0,
        "buyer_id": None,
        "product": product, "amount": amount, "timestamp": now,
        "source": "sellauth", "sellauth_invoice_id": invoice_id
    })

    cfg = get_config(guild.id)
    order_banner  = cfg.get("order_banner", "")
    order_channel_id = cfg.get("order_channel")
    order_ch = guild.get_channel(int(order_channel_id)) if order_channel_id else None
    if order_ch is None:
        print(f"⚠️ No order channel configured for guild {guild.id} — cannot post SellAuth order.")
        return

    order_embed = discord.Embed(
        title="<a:blueverify:1546740183716855872> Order Completed",
        color=0x5865F2, timestamp=now)
    order_embed.add_field(
        name=f"<:cart:1546740191773986977> Order Number: #{order_num}",
        value=(
            f"<:curvedarrow:1546740206164643933> <:Buyer:1546740161205899285> **Buyer:** {buyer_display}\n"
            f"<:curvedarrow:1546740206164643933> <:blue_box:1546740176041283594> **Product:** {product}\n"
            f"<:curvedarrow:1546740206164643933> <:cash:1546740198870749224> **Price:** {amount}"
        ), inline=False)
    if order_banner:
        order_embed.set_image(url=order_banner)
    order_embed.set_footer(text=f"Order Completed by {seller_username} • {guild.name}")

    try:
        await order_ch.send(embed=order_embed)
    except Exception as e:
        print(f"❌ Failed to send SellAuth order embed: {e}")
        return

    # Auto-assign Customer role, and feedback DM — both need a resolvable
    # Discord member, which we don't have since buyer is now a fixed link
    # (see buyer_display above). Skipping both; remove this comment block
    # and restore member-resolution logic above if you want these back.


async def _fetch_sellauth_feedback(feedback_id):
    """GET /v1/shops/{shopId}/feedbacks/{feedbackId}"""
    url = f"https://api.sellauth.com/v1/shops/{SELLAUTH_SHOP_ID}/feedbacks/{feedback_id}"
    headers = {"Authorization": f"Bearer {SELLAUTH_API_KEY}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"SellAuth API {resp.status}: {await resp.text()}")
            return await resp.json()


async def process_sellauth_feedback(feedback_id, fallback_invoice_id=None):
    """
    Fetches a SellAuth feedback entry and posts it using the SAME
    "New FeedBack Recorded!" embed style as the manual feedback flow.
    Works no matter when the feedback comes in relative to delivery —
    it's triggered purely by SellAuth's webhook event, not by anything
    tracked at order time.
    """
    guild = _find_sellauth_target_guild()
    if guild is None:
        print("⚠️ No guild with a feedback/order channel configured — skipping SellAuth feedback post.")
        return

    # Idempotency — don't double-post if SellAuth retries the webhook.
    if feedback_col.find_one({"sellauth_feedback_id": feedback_id}):
        print(f"⏭️ SellAuth feedback #{feedback_id} already posted — skipping duplicate.")
        return

    feedback = await _fetch_sellauth_feedback(feedback_id)
    print(f"⭐ SellAuth feedback #{feedback_id} RAW: {json.dumps(feedback, default=str)}")

    # Rating — try every plausible key. Use explicit None-checks (not `or`)
    # so a genuine 0 doesn't get silently treated as "missing" and replaced
    # by the fallback. Handles ints, floats, and numeric strings.
    raw_rating = None
    for key in ("rating", "stars", "score", "star_rating"):
        val = feedback.get(key)
        if val is not None:
            raw_rating = val
            break
    try:
        stars_float = float(raw_rating)
    except (TypeError, ValueError):
        stars_float = 5.0
    # Some APIs send rating as a 0–1 fraction (e.g. 0.8 = 4/5) instead of
    # 1–5 — if it looks like that scale, convert it up.
    if 0 < stars_float <= 1:
        stars_float *= 5
    stars = max(1, min(5, round(stars_float)))
    star_display = ("<:BlueStar:1546740153916194846> " * stars).strip()
    print(f"⭐ SellAuth feedback #{feedback_id} rating raw={raw_rating!r} -> stars={stars} -> star_display={star_display!r}")

    comment = (
        feedback.get("comment") or feedback.get("message") or feedback.get("reason")
        or feedback.get("text") or "No reason provided"
    )

    # Feedback usually references the invoice it belongs to — fetch it for
    # product/price, same defensive extraction used for orders.
    invoice_id = (
        feedback.get("invoice_id") or feedback.get("invoiceId")
        or (feedback.get("invoice") or {}).get("id")
        or (feedback.get("order") or {}).get("id")
        or feedback.get("order_id")
        or fallback_invoice_id
    )
    print(f"⭐ SellAuth feedback #{feedback_id} resolved invoice_id: {invoice_id!r} (fallback was {fallback_invoice_id!r})")
    product, amount = "Unknown product", "0$"
    if invoice_id:
        try:
            invoice = await _fetch_sellauth_invoice(invoice_id)
            product, amount = _extract_product_and_price(invoice)
        except Exception as e:
            print(f"⚠️ Could not fetch invoice #{invoice_id} for feedback #{feedback_id}: {e}")

    buyer_display = "[SellAuth Buyer](https://elfmart.mysellauth.com)"
    seller_username = "SellAuth"
    now = datetime.datetime.now(datetime.timezone.utc)

    feedback_col.insert_one({
        "guild_id": guild.id, "buyer_id": None,
        "seller_id": bot.user.id if bot.user else 0,
        "product": product, "amount": amount,
        "rating": stars, "comment": comment, "timestamp": now,
        "source": "sellauth", "sellauth_feedback_id": feedback_id,
        "sellauth_invoice_id": invoice_id,
    })

    cfg = get_config(guild.id)
    fb_channel_id = cfg.get("feedback_channel")
    fb_ch = guild.get_channel(int(fb_channel_id)) if fb_channel_id else None
    if fb_ch is None:
        print(f"⚠️ No feedback channel configured for guild {guild.id} (.setfeedbackchannel) — cannot post SellAuth feedback.")
        return

    L = "<:line:1546740235155673090>"
    LINE6 = f"{L} {L} {L} {L} {L} {L}"
    LINE5 = f"{L} {L} {L} {L} {L}"
    unix_ts = int(now.timestamp())

    log_embed = discord.Embed(color=0x5865F2)
    log_embed.add_field(name="# <a:blueverify:1546740183716855872> New FeedBack Recorded!", value=(
        f"{LINE6}\n**<:blue_box:1546740176041283594> Product:** **{product}**\n"
        f"**<:sellerITS:1546740256706142318> Seller:** {seller_username}\n{LINE5}\n"
        f"**<a:02_fox_shop_4:1546740145900757103> Rating:** {star_display}\n"
        f"**<:blue1:1546740168923553842> Reason:** **{comment}**\n"
        f"**<:Buyer:1546740161205899285> Buyer:** {buyer_display}\n-# <t:{unix_ts}:f>"), inline=False)

    # PFP/avatar — SellAuth logo if configured, else the server logo.
    thumb_url = os.getenv("SELLAUTH_LOGO_URL") or (guild.icon.url if guild.icon else None)
    if thumb_url:
        log_embed.set_thumbnail(url=thumb_url)

    try:
        fb_msg = await fb_ch.send(embed=log_embed)
        try:
            await fb_msg.add_reaction("<:tick:1546740263437738055>")
        except Exception:
            pass
    except Exception as e:
        print(f"❌ Failed to send SellAuth feedback embed: {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  SHOP — FEEDBACK VIEW / MODAL
# ══════════════════════════════════════════════════════════════════════════════

class FeedbackView(discord.ui.View):
    def __init__(self, order_num, product, amount, seller_name, seller_id, buyer_id, feedback_channel_id, guild_id):
        super().__init__(timeout=None)
        self.order_num = order_num
        self.product = product
        self.amount = amount
        self.seller_name = seller_name
        self.seller_id = seller_id
        self.buyer_id = buyer_id
        self.feedback_channel_id = feedback_channel_id
        self.guild_id = guild_id

    @discord.ui.button(label="Submit Feedback", style=discord.ButtonStyle.primary, custom_id="feedback_submit_btn")
    async def submit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FeedbackModal(self))


class FeedbackModal(discord.ui.Modal, title="Leave Your Feedback"):
    rating = discord.ui.TextInput(label="Rating (1–5 stars)", placeholder="Enter a number from 1 to 5", min_length=1, max_length=1)
    reason = discord.ui.TextInput(label="Reason / Comment", style=discord.TextStyle.long,
                                   placeholder="Tell us about your experience...", required=False, max_length=500)

    def __init__(self, view: FeedbackView):
        super().__init__()
        self.fb_view = view

    async def on_submit(self, interaction: discord.Interaction):
        v = self.fb_view
        try:
            stars = max(1, min(5, int(self.rating.value.strip())))
        except ValueError:
            stars = 5
        comment = self.reason.value.strip() or "No reason provided"
        star_display = "<:BlueStar:1546740153916194846> " * stars
        feedback_col.insert_one({"order_num": v.order_num, "guild_id": v.guild_id, "buyer_id": v.buyer_id,
            "seller_id": v.seller_id, "product": v.product, "amount": v.amount,
            "rating": stars, "comment": comment, "timestamp": datetime.datetime.now(datetime.timezone.utc)})
        cfg = get_config(v.guild_id)
        fb_banner = cfg.get("feedback_banner", "")
        submitted_embed = discord.Embed(title="<a:blueverify:1546740183716855872> New FeedBack Request",
            color=0x5865F2, timestamp=datetime.datetime.now(datetime.timezone.utc))
        submitted_embed.add_field(name="\u200b", value=(
            f"<:blue_box:1546740176041283594> **Product:** {v.product}\n"
            f"<:cash:1546740198870749224> **Price:** {v.amount}\n"
            f"<:sellerITS:1546740256706142318> **Seller:** {v.seller_name}"), inline=False)
        if fb_banner:
            submitted_embed.set_image(url=fb_banner)
        guild_obj = interaction.client.get_guild(v.guild_id)
        guild_name_display = guild_obj.name if guild_obj else "Server"
        if guild_obj and guild_obj.icon:
            submitted_embed.set_thumbnail(url=guild_obj.icon.url)
        submitted_embed.set_footer(text=guild_name_display)
        done_view = discord.ui.View()
        done_btn = discord.ui.Button(label="Feedback Submitted", style=discord.ButtonStyle.secondary, disabled=True)
        done_view.add_item(done_btn)
        await interaction.response.edit_message(embed=submitted_embed, view=done_view)
        guild = interaction.client.get_guild(v.guild_id)
        if guild:
            fb_ch = guild.get_channel(v.feedback_channel_id)
            if fb_ch:
                buyer = guild.get_member(v.buyer_id) or await interaction.client.fetch_user(v.buyer_id)
                seller = guild.get_member(v.seller_id) or await interaction.client.fetch_user(v.seller_id)
                seller_display = seller.display_name if hasattr(seller, "display_name") else str(seller)
                L = "<:line:1546740235155673090>"
                LINE6 = f"{L} {L} {L} {L} {L} {L}"
                LINE5 = f"{L} {L} {L} {L} {L}"
                unix_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
                log_embed = discord.Embed(color=0x5865F2)
                log_embed.add_field(name="# <a:blueverify:1546740183716855872> New FeedBack Recorded!", value=(
                    f"{LINE6}\n**<:blue_box:1546740176041283594> Product:** **{v.product}**\n"
                    f"**<:cash:1546740198870749224> Price:** **{v.amount}**\n"
                    f"**<:sellerITS:1546740256706142318> Seller:** {seller_display}\n{LINE5}\n"
                    f"**<a:02_fox_shop_4:1546740145900757103> Rating:** {star_display.strip()}\n"
                    f"**<:blue1:1546740168923553842> Reason:** **{comment}**\n"
                    f"**<:Buyer:1546740161205899285> Buyer:** {buyer.mention}\n-# <t:{unix_ts}:f>"), inline=False)
                if hasattr(buyer, "display_avatar") and buyer.display_avatar:
                    log_embed.set_thumbnail(url=buyer.display_avatar.url)
                fb_msg = await fb_ch.send(embed=log_embed)
                try:
                    await fb_msg.add_reaction("<:tick:1546740263437738055>")
                except Exception:
                    pass

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await interaction.response.send_message("❌ Something went wrong submitting feedback.", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")

    # ── Restore moderator bot config ──────────────────────────────────────────
    all_configs = list(config_col.find({}))
    for cfg in all_configs:
        status_text = cfg.get("bot_status")
        status_type = cfg.get("bot_status_type", "playing")
        if status_text:
            atype = {"playing": discord.ActivityType.playing, "watching": discord.ActivityType.watching,
                     "listening": discord.ActivityType.listening, "competing": discord.ActivityType.competing
                     }.get(status_type, discord.ActivityType.playing)
            await bot.change_presence(activity=discord.Activity(type=atype, name=status_text))
    print(f"   Restored mod config for {len(all_configs)} guild(s)")

    # ── Restore ticket bot in-memory state ────────────────────────────────────
    for deal in deals_col.find():
        active_deals[deal["channel_id"]] = deal
    for entry in proof_col.find():
        awaiting_proof[entry["channel_id"]] = entry["deal"]

    # ── Register all persistent views ─────────────────────────────────────────
    bot.add_view(PersistentTicketView())
    bot.add_view(PersistentSupportView())
    bot.add_view(PersistentPanelSelectView())
    bot.add_view(DealConfirmView())
    bot.add_view(PostDealConfirmView())
    bot.add_view(PaymentChoiceView())
    bot.add_view(CloseConfirmView())
    bot.add_view(ProofChoiceView())
    bot.add_view(TicketManageView())

    # ── Customer Panel: register persistent panel + reattach open requests ───
    bot.add_view(CustomerPanelView())
    # Self-heal any requests left "processing" from a crash/restart mid-approval
    customer_verifications_col.update_many(
        {"status": "processing"}, {"$set": {"status": "pending"}, "$unset": {"processing_by": ""}})
    for req in customer_verifications_col.find({"status": {"$in": ["pending", "processing"]}}):
        try:
            bot.add_view(CustomerVerificationView(req["request_id"]))
        except Exception:
            pass

    # ── TOS: register persistent dropdown view + re-attach panels ────────────
    for guild in bot.guilds:
        entries = _get_tos_entries(guild.id)
        if entries:
            bot.add_view(TOSView(entries))
    # Re-attach TOS panel messages so dropdowns work after restart
    for guild in bot.guilds:
        cfg = get_config(guild.id)
        for panel in cfg.get(TOS_PANEL_KEY, []):
            try:
                ch = guild.get_channel(panel["channel_id"])
                if not ch:
                    continue
                msg = await ch.fetch_message(panel["message_id"])
                entries = _get_tos_entries(guild.id)
                await msg.edit(embed=_build_tos_embed(guild), view=TOSView(entries))
            except Exception:
                pass

    # ── Re-attach views to existing panel messages ────────────────────────────
    for panel in panels_col.find({"message_id": {"$exists": True}}):
        try:
            ch = bot.get_channel(panel["channel_id"])
            if not ch:
                continue
            msg = await ch.fetch_message(panel["message_id"])
            if panel.get("panel_type") == "support":
                await msg.edit(view=PersistentSupportView())
            elif panel.get("panel_type") == "select":
                await msg.edit(view=PersistentPanelSelectView())
            else:
                await msg.edit(view=PersistentTicketView())
        except Exception:
            continue

    # ── Sync slash commands ───────────────────────────────────────────────────
    try:
        synced = await bot.tree.sync()
        print(f"   Synced {len(synced)} global slash commands")
    except Exception as e:
        print(f"   Slash sync error: {e}")

    # ── Customer Panel: register /customerpanel + /customerconfig per-guild ──
    # (guild-scoped, since the 100 global slash command slots above are full)
    for guild in bot.guilds:
        await register_customer_panel_guild_commands(guild)
    print(f"   Synced customer panel commands for {len(bot.guilds)} guild(s)")

    # ── Start background tasks ────────────────────────────────────────────────
    bot.loop.create_task(_run_autopurge_loop())
    bot.loop.create_task(_run_autostatusrole_loop())
    refresh_panels.start()

    # ── Giveaway background task ──────────────────────────────────────────────
    giveaway_tick.start()
    active_gw_count = giveaways_col.count_documents({"ended": False})
    print(f"   Giveaway task started — {active_gw_count} active giveaway(s) being tracked")

    # ── Scheduled message background task ────────────────────────────────────
    scheduled_message_tick.start()

    print("✅ All systems ready.")


@bot.event
async def on_member_join(member: discord.Member):
    # Moderator bot: autorole + welcome
    cfg = get_config(member.guild.id)
    autorole_id = cfg.get("autorole")
    if autorole_id:
        role = member.guild.get_role(int(autorole_id))
        if role:
            try:
                await member.add_roles(role)
            except Exception:
                pass
    welcome_ch_id = cfg.get("welcome_channel")
    if welcome_ch_id:
        ch = member.guild.get_channel(int(welcome_ch_id))
        if ch:
            await ch.send(f"👋 Welcome to **{member.guild.name}**, {member.mention}!")

    # Auto Status Role: if the member already has the required status on join
    # (e.g. rejoining), grant the role right away instead of waiting on presence data.
    asr_cfg = get_autostatusrole_config(member.guild.id)
    if asr_cfg:
        try:
            await _asr_evaluate_member(member, asr_cfg)
        except Exception:
            pass


@bot.event
async def on_guild_join(guild: discord.Guild):
    await register_customer_panel_guild_commands(guild)


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    # Auto Status Role: react immediately to custom status changes instead of
    # waiting for the periodic fallback sweep.
    if after is None or after.bot or after.guild is None:
        return
    cfg = get_autostatusrole_config(after.guild.id)
    if not cfg:
        return
    try:
        await _asr_evaluate_member(after, cfg)
    except Exception as e:
        print(f"[autostatusrole] on_presence_update error: {e}")


@bot.event
async def on_member_remove(member: discord.Member):
    # Auto Status Role: clean up any pending grace-period entry so we don't
    # try to action a member who already left.
    guild_pending = _asr_pending_removal.get(member.guild.id)
    if guild_pending:
        guild_pending.pop(member.id, None)


@bot.event
async def on_message(message: discord.Message):
    # Allow bots to use ordercomplete only — block all other bot messages
    if message.author.bot:
        content = message.content.strip()
        prefixes = [".", "!"]
        is_ordercomplete = any(
            content.lower().startswith(p + "ordercomplete") for p in prefixes
        )
        if not is_ordercomplete:
            return
        # discord.py refuses to process commands from bots via process_commands,
        # so we manually create a context and invoke the command instead.
        for prefix in prefixes:
            if content.lower().startswith(prefix + "ordercomplete"):
                args_str = content[len(prefix + "ordercomplete"):].strip()
                ctx = await bot.get_context(message)
                ctx.command = bot.get_command("ordercomplete")
                if ctx.command:
                    ctx.invoked_with = "ordercomplete"
                    ctx.view = discord.ext.commands.view.StringView(args_str)
                    ctx.args = [ctx]
                    ctx.kwargs = {"args": args_str}
                    try:
                        await ctx.command.invoke(ctx)
                    except Exception as e:
                        await message.channel.send(f"❌ ordercomplete error: `{e}`")
                return

    # ── AFK system ──────────────────────────────────────────────────────────────
    if not message.author.bot:
        # If the author was AFK, welcome them back and clear it.
        own_afk = afk_col.find_one({"user_id": message.author.id})
        if own_afk:
            afk_col.delete_one({"user_id": message.author.id})
            try:
                await message.channel.send(
                    f"<:tick:1546740263437738055> Welcome back {message.author.mention}, I removed your AFK.",
                    delete_after=8)
            except Exception:
                pass
        # If this message pings anyone currently AFK, let the author know.
        if message.mentions:
            afk_notices = []
            for pinged in message.mentions:
                if pinged.id == message.author.id:
                    continue
                doc = afk_col.find_one({"user_id": pinged.id})
                if doc:
                    since_ts = int(doc["since"].timestamp())
                    afk_notices.append(
                        f"<:blue1:1546740168923553842> {pinged.mention} is AFK: **{doc.get('reason', 'AFK')}** "
                        f"(since <t:{since_ts}:R>)")
            if afk_notices:
                try:
                    await message.channel.send("\n".join(afk_notices))
                except Exception:
                    pass

    # ── Ticket bot: proof image auto-forwarding ────────────────────────────────
    if message.guild:
        deal = awaiting_proof.get(message.channel.id)
        if deal and message.author.id == deal["dealer"] and message.attachments:
            images = [a for a in message.attachments if a.content_type and a.content_type.startswith("image/")]
            if images:
                processing_msg = await message.channel.send(embed=make_embed(
                    "⏳ Processing Proof...", "Uploading your image to permanent storage. Please wait...", discord.Color.blurple()))
                proof_ch = bot.get_channel(PROOF_CHANNEL_ID)
                uploaded_any = False
                for i, att in enumerate(images):
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(att.url) as resp:
                                img_bytes = await resp.read()
                    except Exception:
                        img_bytes = None
                    permanent_url = await upload_to_imgbb(img_bytes, att.filename) if img_bytes else None
                    final_url = permanent_url or att.url
                    if proof_ch:
                        if i == 0:
                            await proof_ch.send(embed=make_embed(
                                title=f"# Sold {deal['product']}",
                                description=(f"**Dealer:** <@{deal['dealer']}>\n**Buyer:** <@{deal['buyer']}>\n**Amount:** {deal['amount']}"),
                                color=discord.Color.green(), image=final_url, footer="Proof Of Delivery"))
                        else:
                            await proof_ch.send(embed=make_embed(
                                title=f"📸 Additional Proof — {deal['product']}",
                                color=discord.Color.green(), image=final_url, footer="Proof Of Delivery"))
                        uploaded_any = True
                try:
                    await processing_msg.delete()
                except Exception:
                    pass
                if uploaded_any:
                    await message.channel.send(embed=make_embed(
                        "✅ Proof Forwarded", f"Your proof has been permanently saved and posted to <#{PROOF_CHANNEL_ID}>.",
                        discord.Color.green(), footer="Proof Of Delivery"))
                else:
                    await message.channel.send(embed=make_embed(
                        "❌ Upload Failed", "Could not reach the proof channel. Please contact an admin.", discord.Color.red()))
                awaiting_proof.pop(message.channel.id, None)
                proof_col.delete_one({"channel_id": message.channel.id})

    # ── Moderator bot: word filter, anti-invite, sticky, autoresponder ─────────
    # Skip all mod processing for bots — they only reach here for .ordercomplete
    if not message.author.bot and message.guild:
        cfg = get_config(message.guild.id)
        content_lower = message.content.lower()
        filtered = cfg.get("filtered_words", [])
        if any(w in content_lower for w in filtered):
            try:
                await message.delete()
                await message.channel.send(f"🚫 {message.author.mention}, that word is not allowed here.", delete_after=5)
            except Exception:
                pass
            return
        if cfg.get("antiinvite") and "discord.gg/" in content_lower:
            if not message.author.guild_permissions.manage_messages:
                try:
                    await message.delete()
                    await message.channel.send(f"🔗 {message.author.mention}, invite links are not allowed.", delete_after=5)
                except Exception:
                    pass
                return
        # Sticky messages
        sticky = config_col.find_one({"guild_id": message.guild.id, "sticky_channel": message.channel.id})
        if sticky:
            prev_id = sticky.get("sticky_msg_id")
            if prev_id:
                try:
                    prev = await message.channel.fetch_message(prev_id)
                    await prev.delete()
                except Exception:
                    pass
            sent = await message.channel.send(f"📌 {sticky['sticky_text']}")
            config_col.update_one(
                {"guild_id": message.guild.id, "sticky_channel": message.channel.id},
                {"$set": {"sticky_msg_id": sent.id}})
        # Autoresponders
        ar_doc = autoresponders_col.find_one({"guild_id": message.guild.id, "trigger": content_lower})
        if ar_doc:
            try:
                await message.channel.send(ar_doc["response"])
            except Exception:
                pass

    await bot.process_commands(message)


@tasks.loop(minutes=3)
async def refresh_panels():
    for panel in panels_col.find({"message_id": {"$exists": True}}):
        try:
            ch = bot.get_channel(panel["channel_id"])
            if not ch:
                continue
            msg = await ch.fetch_message(panel["message_id"])
            if panel.get("panel_type") == "support":
                await msg.edit(view=PersistentSupportView())
            elif panel.get("panel_type") == "select":
                await msg.edit(view=PersistentPanelSelectView())
            else:
                await msg.edit(view=PersistentTicketView())
        except Exception:
            continue

# ══════════════════════════════════════════════════════════════════════════════
#  PREFIX COMMANDS — MODERATOR BOT
# ══════════════════════════════════════════════════════════════════════════════

@bot.command(name="ban")
async def modban(ctx, member: discord.Member, *, reason="No reason provided"):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _ban(ctx, member, reason)

@bot.command()
async def unban(ctx, user_id: int, *, reason="No reason provided"):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _unban(ctx, user_id, reason)

@bot.command(name="kick")
async def modkick(ctx, member: discord.Member, *, reason="No reason provided"):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _kick(ctx, member, reason)

@bot.command()
async def mute(ctx, member: discord.Member, duration: int = 10, *, reason="No reason provided"):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _mute(ctx, member, duration, reason)

@bot.command()
async def unmute(ctx, member: discord.Member):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _unmute(ctx, member)

@bot.command(name="warn")
async def modwarn(ctx, member: discord.Member, *, reason="No reason provided"):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _warn(ctx, member, reason)

@bot.command()
async def softban(ctx, member: discord.Member, *, reason="Softban"):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _softban(ctx, member, reason)

@bot.command(name="warnings")
async def modwarnings(ctx, member: discord.Member):
    await _warnings(ctx, member)

@bot.command()
async def modlogs(ctx, member: discord.Member):
    await _modlogs(ctx, member)

@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    await _userinfo(ctx, member or ctx.author)

@bot.command()
async def serverinfo(ctx):
    await _serverinfo(ctx)

@bot.command()
async def clear(ctx, amount: int = 10):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _clear(ctx, amount)

@bot.command()
async def purge(ctx, member: discord.Member, amount: int = 20):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    deleted = await ctx.channel.purge(limit=200, check=lambda m: m.author == member)
    await ctx.send(f"🧹 Deleted {len(deleted)} messages from {member.mention}.", delete_after=5)

@bot.command()
async def purgebot(ctx, amount: int = 20):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    deleted = await ctx.channel.purge(limit=200, check=lambda m: m.author.bot)
    await ctx.send(f"🧹 Deleted {len(deleted)} bot messages.", delete_after=5)

@bot.command()
async def slowmode(ctx, seconds: int = 0):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _slowmode(ctx, seconds)

@bot.command()
async def lock(ctx, channel: discord.TextChannel = None):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _lock(ctx, channel)

@bot.command()
async def unlock(ctx, channel: discord.TextChannel = None):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _unlock(ctx, channel)

@bot.command()
async def lockdown(ctx):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    for ch in ctx.guild.text_channels:
        try:
            ow = ch.overwrites_for(ctx.guild.default_role)
            ow.send_messages = False
            await ch.set_permissions(ctx.guild.default_role, overwrite=ow)
        except Exception:
            pass
    await ctx.send("🔒 **Server lockdown activated.**")

@bot.command()
async def hide(ctx, channel: discord.TextChannel = None):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _hide(ctx, channel)

@bot.command()
async def unhide(ctx, channel: discord.TextChannel = None):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _unhide(ctx, channel)

@bot.command()
async def nuke(ctx):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    ch = ctx.channel
    new_ch = await ch.clone(reason=f"Nuked by {ctx.author}")
    await ch.delete()
    await new_ch.send("💥 Channel has been nuked!")

@bot.command()
async def channelcreate(ctx, name: str, category_id: int = None):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _channelcreate(ctx, name, category_id)

@bot.command()
async def deletechannel(ctx, channel: discord.TextChannel = None):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _deletechannel(ctx, channel)

@bot.command()
async def rolecreate(ctx, name: str, colour: str = None):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _rolecreate(ctx, name, colour)

@bot.group(name="filter", invoke_without_command=True)
async def filter_group(ctx):
    await ctx.send("Usage: `!filter add <word>` | `!filter remove <word>` | `!filter list`")

@filter_group.command(name="add")
async def filter_add(ctx, *, word: str):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    config_col.update_one({"guild_id": ctx.guild.id}, {"$addToSet": {"filtered_words": word.lower()}}, upsert=True)
    await ctx.send(f"✅ Added `{word.lower()}` to word filter.")

@filter_group.command(name="remove")
async def filter_remove(ctx, *, word: str):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    config_col.update_one({"guild_id": ctx.guild.id}, {"$pull": {"filtered_words": word.lower()}})
    await ctx.send(f"✅ Removed `{word.lower()}` from word filter.")

@filter_group.command(name="list")
async def filter_list(ctx):
    cfg = get_config(ctx.guild.id)
    words = cfg.get("filtered_words", [])
    await ctx.send(f"🚫 Filtered: `{'`, `'.join(words)}`" if words else "No filtered words set.")

@bot.command()
async def antiinvite(ctx, state: str = "enable"):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    enabled = state.lower() in ("enable", "on", "true", "1")
    set_config(ctx.guild.id, "antiinvite", enabled)
    await ctx.send(f"✅ Anti-invite {'**enabled**' if enabled else '**disabled**'}.")

@bot.group(name="role", invoke_without_command=True)
async def role_group(ctx):
    await ctx.send("Usage: `!role add @user RoleName` | `!role remove @user RoleName`")

@role_group.command(name="add")
async def role_add(ctx, member: discord.Member, *, role_name: str):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    role = find_role_by_name(ctx.guild, role_name)
    if not role:
        return await ctx.send(f"❌ Role `{role_name}` not found.")
    await _role_add(ctx, member, role)

@role_group.command(name="remove")
async def role_remove(ctx, member: discord.Member, *, role_name: str):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    role = find_role_by_name(ctx.guild, role_name)
    if not role:
        return await ctx.send(f"❌ Role `{role_name}` not found.")
    await _role_remove(ctx, member, role)

@bot.command()
async def setlogchannel(ctx, channel: discord.TextChannel):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Admins only.")
    set_config(ctx.guild.id, "log_channel", channel.id)
    await ctx.send(f"✅ Log channel → {channel.mention}")

@bot.command()
async def setmodrole(ctx, role: discord.Role):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Admins only.")
    set_config(ctx.guild.id, "mod_role", role.id)
    await ctx.send(f"✅ Mod role → {role.mention}")

@bot.command()
async def setwelcome(ctx, channel: discord.TextChannel):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Admins only.")
    set_config(ctx.guild.id, "welcome_channel", channel.id)
    await ctx.send(f"✅ Welcome channel → {channel.mention}")

@bot.command()
async def setautorole(ctx, role: discord.Role):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Admins only.")
    set_config(ctx.guild.id, "autorole", role.id)
    await ctx.send(f"✅ Auto-role → {role.mention}")

@bot.command()
async def setprefix(ctx, prefix: str):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Admins only.")
    set_config(ctx.guild.id, "prefix", prefix)
    await ctx.send(f"✅ Custom prefix `{prefix}` added for this server. `.` and `!` always still work.")

@bot.command(name="latency", aliases=["botping"])
async def latency(ctx):
    await ctx.send(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")

@bot.command()
async def announce(ctx, channel: discord.TextChannel, *, message: str):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _announce(ctx, channel, message)

@bot.command()
async def poll(ctx, *, question: str):
    await _poll(ctx, question)

@bot.command()
async def report(ctx, member: discord.Member, *, reason: str):
    await _report(ctx, member, reason)

@bot.command(name="autoresponder")
async def cmd_autoresponder(ctx, trigger: str, *, message: str):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _autoresponder(ctx, trigger, message)

@bot.command(name="autoresponder_remove")
async def cmd_autoresponder_remove(ctx, trigger: str):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _autoresponder_remove(ctx, trigger)

@bot.command(name="autoresponder_list")
async def cmd_autoresponder_list(ctx):
    await _autoresponder_list(ctx)

@bot.command(name="autopurge")
async def cmd_autopurge(ctx, channel: discord.TextChannel, timer: str):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _autopurge(ctx, channel, timer)

@bot.command(name="autopurge_remove")
async def cmd_autopurge_remove(ctx, channel: discord.TextChannel):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _autopurge_remove(ctx, channel)

@bot.command(name="autopurge_list")
async def cmd_autopurge_list(ctx):
    await _autopurge_list(ctx)

@bot.group(name="autostatusrole", aliases=["asr"], invoke_without_command=True)
async def cmd_autostatusrole(ctx):
    await ctx.send(
        "**Auto Status Role**\n"
        f"`{ctx.prefix}autostatusrole set <status text> <role>` — configure\n"
        f"`{ctx.prefix}autostatusrole remove` — disable\n"
        f"`{ctx.prefix}autostatusrole status` — view current config\n"
        f"`{ctx.prefix}autostatusrole check [user]` — manually check a member"
    )

@cmd_autostatusrole.command(name="set")
async def cmd_autostatusrole_set(ctx, *, rest: str):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    # Status text can contain spaces, so the role (mention/ID/name) is parsed
    # off the end of the input rather than using positional args.
    parts = rest.rsplit(" ", 1)
    if len(parts) != 2:
        return await ctx.send(
            f"❌ Usage: `{ctx.prefix}autostatusrole set <status text> <role>`\n"
            f"Example: `{ctx.prefix}autostatusrole set discord.gg/example @Verified`"
        )
    status_text, role_ref = parts[0].strip(), parts[1].strip()
    role = None
    role_id_match = _re.match(r"^<@&(\d+)>$|^(\d+)$", role_ref)
    if role_id_match:
        rid = int(role_id_match.group(1) or role_id_match.group(2))
        role = ctx.guild.get_role(rid)
    if role is None:
        role = find_role_by_name(ctx.guild, role_ref)
    if role is None or not status_text:
        return await ctx.send(
            f"❌ Couldn't parse that. Usage: `{ctx.prefix}autostatusrole set <status text> <role>`\n"
            f"Example: `{ctx.prefix}autostatusrole set discord.gg/example @Verified`"
        )
    if role >= ctx.guild.me.top_role:
        return await ctx.send("❌ **Role Hierarchy Error**\nMy role must be above the configured role so I can manage it.")
    await _autostatusrole_set(ctx, status_text, role)

@cmd_autostatusrole.command(name="remove", aliases=["disable"])
async def cmd_autostatusrole_remove(ctx):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _autostatusrole_remove(ctx)

@cmd_autostatusrole.command(name="status")
async def cmd_autostatusrole_status(ctx):
    await _autostatusrole_status(ctx)

@cmd_autostatusrole.command(name="check")
async def cmd_autostatusrole_check(ctx, member: discord.Member = None):
    member = member or ctx.author
    await _autostatusrole_check(ctx, member)

@bot.command()
async def stickymessage(ctx, channel: discord.TextChannel, *, message: str):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _stickymessage(ctx, channel, message)

@bot.command()
async def stickyremove(ctx, channel: discord.TextChannel):
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _stickyremove(ctx, channel)

@bot.command()
async def setstatus(ctx, status_type: str, *, message: str):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Admins only.")
    valid = ["playing", "watching", "listening", "competing"]
    if status_type.lower() not in valid:
        return await ctx.send(f"❌ Invalid type. Choose from: `{'` `'.join(valid)}`")
    await _setstatus(ctx, status_type, message=message)

@bot.command()
async def dmannounce(ctx, *, text: str):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Admins only.")
    await _dmannounce(ctx, text)

@bot.command()
async def dmdelete(ctx, announce_id: str):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Admins only.")
    await _dmdelete(ctx, announce_id)

# ══════════════════════════════════════════════════════════════════════════════
#  PREFIX COMMANDS — TICKET BOT
# ══════════════════════════════════════════════════════════════════════════════

@bot.command()
async def ltc(ctx, address: str):
    if not is_dealer(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "Only dealers can set an LTC address.", discord.Color.red()))
    ltc_col.update_one({"dealer_id": ctx.author.id}, {"$set": {"address": address}}, upsert=True)
    await ctx.send(embed=make_embed("✅ LTC Address Saved", "Your LTC address has been updated.",
        discord.Color.green(), fields=[("Address", f"{address}", False)], footer="This address will be shown to buyers during deals."))

@bot.command()
async def upi(ctx, upi_id: str, image_url: str):
    if not is_dealer(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "Only dealers can set a UPI ID.", discord.Color.red()))
    upi_col.update_one({"dealer_id": ctx.author.id}, {"$set": {"upi_id": upi_id, "image_url": image_url}}, upsert=True)
    await ctx.send(embed=make_embed("✅ UPI Details Saved", "Your UPI payment details have been updated.",
        discord.Color.green(),
        fields=[("UPI ID", f"`{upi_id}`", False), ("QR Image URL", image_url, False)],
        footer="Buyers will see UPI as a payment option in your deals."))

@bot.command()
async def panel(ctx):
    """Send main panel — Buy / Support dropdown selection."""
    embed = discord.Embed(
        description=(
            "• To buy something → 1st option\n"
            "• Need support/help → 2nd option\n\n"
            "**Rules:**\n"
            "• No selling or promotions here\n"
            "• No random DMs for deals\n"
            "• Making tickets for fun = Ban"),
        color=discord.Color.purple())
    embed.set_author(name=SHOP_NAME, icon_url=PANEL_LOGO_URL)
    embed.set_thumbnail(url=PANEL_LOGO_URL)
    embed.set_image(url=PANEL_BANNER_URL)
    embed.set_footer(text="Opening a ticket = automatically accepting our ToS")
    msg = await ctx.send(embed=embed, view=PersistentPanelSelectView())
    panels_col.update_one({"message_id": msg.id},
        {"$set": {"channel_id": ctx.channel.id, "message_id": msg.id, "panel_type": "select"}}, upsert=True)
    try:
        await ctx.message.delete()
    except Exception:
        pass

@bot.command(name="setvouchuser")
async def setvouchuser_cmd(ctx, userid: str = None):
    """Prefix-only: .setvouchuser <userid>

    Sets which user ID shows up in YOUR /vouch cards instead of your own —
    handy for sellers who run /vouch from a main account but want the vouch
    to credit an alt. Pass `.setvouchuser reset` to go back to your own ID.
    """
    if not userid:
        return await ctx.send(
            "❌ Usage: `.setvouchuser <userid>` — or `.setvouchuser reset` to clear your override.")

    if userid.lower() in ("reset", "clear", "none"):
        vouch_users_col.delete_one({"user_id": ctx.author.id})
        return await ctx.send("✅ Vouch user override cleared — `/vouch` will show your own ID again.")

    if not userid.isdigit():
        return await ctx.send("❌ That doesn't look like a valid user ID — numbers only (or `reset`).")

    vouch_users_col.update_one(
        {"user_id": ctx.author.id},
        {"$set": {"user_id": ctx.author.id, "vouch_user_id": int(userid)}},
        upsert=True)
    await ctx.send(f"✅ Your `/vouch` cards will now credit <@{userid}> instead of your own account.")


# ── /vouch (slash, DM-enabled) + .vouch (prefix) — shared logic ──────────────
def _is_vouch_authorized(user_id: int) -> bool:
    """Works in DMs too: looks the user up across every guild the bot shares
    with them instead of relying on a guild context."""
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member and can_manage_ticket(member):
            return True
    return False


def _build_vouch_text(user_id: int, text: str, amount: str) -> str:
    # If the invoker has set an alt via .setvouchuser, credit that ID instead.
    override = vouch_users_col.find_one({"user_id": user_id})
    target_id = override["vouch_user_id"] if override else user_id
    return f"+rep <@{target_id}> Legit Got {text} For {amount}"


@bot.command(name="vouch")
async def vouch_cmd(ctx, *, text_and_amount: str = None):
    """Prefix command: .vouch <text> <amount>

    Generates a copyable vouch card for the buyer to paste in the vouch
    channel. Works in both guild channels and DMs.
    """
    if not text_and_amount or len(text_and_amount.split()) < 2:
        return await ctx.send("❌ Usage: `.vouch <text> <amount>` — e.g. `.vouch Discord Nitro $5`")

    # Last whitespace-separated token is the amount; everything before it is
    # the item/text, so multi-word items like "Discord Nitro" still work.
    item_text, _, amount = text_and_amount.rpartition(" ")
    item_text, amount = item_text.strip(), amount.strip()

    if not _is_vouch_authorized(ctx.author.id):
        return await ctx.send("❌ Only sellers/dealers can use this command.")

    vouch_text = _build_vouch_text(ctx.author.id, item_text, amount)
    await ctx.send(view=VouchCopyView(vouch_text))


# Registered globally (not guild-scoped) since guild-only commands never show
# up in DMs. allowed_installs + allowed_contexts is what makes Discord surface
# this command inside DM channels. Requires discord.py >= 2.4.
@bot.tree.command(name="vouch", description="Get a copyable vouch card to hand to a buyer")
@app_commands.describe(text="What was purchased, e.g. Discord Nitro", amount="Amount paid, e.g. $5")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def slash_vouch(inter: discord.Interaction, text: str, amount: str):
    if not _is_vouch_authorized(inter.user.id):
        return await inter.response.send_message(
            "❌ Only sellers/dealers can use this command.", ephemeral=True)

    vouch_text = _build_vouch_text(inter.user.id, text, amount)
    await inter.response.send_message(view=VouchCopyView(vouch_text))



def _get_verify_url(env_name: str) -> str | None:
    """Read a verify-button URL from env, strip stray quotes/whitespace, validate it's http(s)."""
    raw = os.getenv(env_name)
    if not raw:
        return None
    cleaned = raw.strip().strip('"').strip("'").strip()
    if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
        return None
    return cleaned


class VerifyView(ui.View):
    """Verification panel — two link buttons (URLs set via env secrets VERIFY_1_URL / VERIFY_2_URL)."""
    def __init__(self, url1: str, url2: str):
        super().__init__(timeout=None)
        self.add_item(ui.Button(label="Verify 1", style=discord.ButtonStyle.link,
                                 emoji="🔐", url=url1))
        self.add_item(ui.Button(label="Verify 2", style=discord.ButtonStyle.link,
                                 emoji="🛡️", url=url2))


VERIFY_EMBED_DESCRIPTION = (
    f"👋 **Welcome to {SHOP_NAME}!** ✨\n\n"
    "🔐 To access the server, please verify using **Verify 1** or **Verify 2** below.\n"
    "⚡ If one option doesn't work, simply use the other.\n\n"
    "🛡️ **Why verify?**\n"
    "• Keep the server safe from spam and unauthorized accounts.\n"
    "• Stay connected in case Discord ever removes or terminates the server.\n\n"
    "❓ **Need help?**\n"
    "🎫 Open a ticket in <#1515371239537905776> and provide proof of the verification error.\n\n"
    f"💙 Thank you for helping keep **{SHOP_NAME}** secure! ✨🛡️"
)


@bot.command()
async def verify(ctx):
    """Send the server verification panel — 🔐 Verify 1 + 🛡️ Verify 2 buttons."""
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    url1 = _get_verify_url("VERIFY_1_URL")
    url2 = _get_verify_url("VERIFY_2_URL")
    if not url1 or not url2:
        missing = []
        if not url1: missing.append("`VERIFY_1_URL`")
        if not url2: missing.append("`VERIFY_2_URL`")
        return await ctx.send(
            f"❌ {', '.join(missing)} is missing or invalid (must start with `http://` or `https://`).\n"
            "Set it in your bot's secrets/env and **restart the bot**, then run `.verify` again."
        )
    embed = discord.Embed(description=VERIFY_EMBED_DESCRIPTION, color=discord.Color.blue())
    await ctx.send(embed=embed, view=VerifyView(url1, url2))
    try:
        await ctx.message.delete()
    except Exception:
        pass


# NOTE: the standalone "/verify" slash command has been merged into the single
# "/panel" command (see near PersistentPanelSelectView / _post_verify_panel) —
# the ".verify" prefix command above still works unchanged.


@bot.command()
async def supppanel(ctx):
    """Send support-only panel — 🎧 Support Ticket button only."""
    embed = make_embed(
        title="🛠️ Manual Verification",
        description=(
            "Can't verify? Open a ticket and upload proof showing the verification error.\n\n"
            "⚠️ Do not open tickets for fun.\n"
            "⚠️ Do not ping staff after opening a ticket.\n\n"
            "We will review your ticket as soon as possible.\n\n"
            "❌ If you don't want to verify, you may leave the server."),
        color=discord.Color.green(),
        footer="Only you and our team can see your ticket.")
    msg = await ctx.send(embed=embed, view=PersistentSupportView())
    panels_col.update_one({"message_id": msg.id},
        {"$set": {"channel_id": ctx.channel.id, "message_id": msg.id, "panel_type": "support"}}, upsert=True)
    try:
        await ctx.message.delete()
    except Exception:
        pass

@bot.command()
async def deal(ctx, *, args=""):
    if not is_dealer(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "Only dealers can use `.deal`.", discord.Color.red()))
    if args.strip().lower() == "done":
        deal_doc = active_deals.get(ctx.channel.id) or deals_col.find_one({"channel_id": ctx.channel.id})
        if not deal_doc:
            return await ctx.send(embed=make_embed("❌ No Active Deal", "There is no active deal in this channel.", discord.Color.red()))
        confirm_embed = make_embed(
            title="📦 Mark Deal as Done?",
            description=f"Are you sure you want to mark this deal as complete?\n<@{deal_doc['buyer']}> will be asked to confirm delivery.",
            color=discord.Color.orange(),
            fields=[("📦 Product", deal_doc["product"], True), ("💰 Amount", deal_doc["amount"], True)])

        class DealDoneConfirmView(ui.View):
            def __init__(self_inner):
                super().__init__(timeout=30)

            @ui.button(label="✅ Yes, Mark Done", style=discord.ButtonStyle.success)
            async def yes(self_inner, interaction: discord.Interaction, button: ui.Button):
                if interaction.user.id != ctx.author.id:
                    return await interaction.response.send_message("Only the dealer can confirm.", ephemeral=True)
                delivery_embed = make_embed(
                    title="📦 Delivery Confirmation Required",
                    description=f"<@{deal_doc['buyer']}>, please confirm you received your item.",
                    color=discord.Color.blurple(),
                    fields=[("📦 Product", deal_doc["product"], True), ("💰 Amount", deal_doc["amount"], True)],
                    footer="Click 'Confirm Delivery' once you've received your product.")
                await interaction.response.edit_message(
                    embed=make_embed("✅ Sent", "Delivery confirmation sent to the buyer.", discord.Color.green()), view=None)
                await ctx.send(content=f"<@{deal_doc['buyer']}>", embed=delivery_embed, view=PostDealConfirmView())
                self_inner.stop()

            @ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
            async def no(self_inner, interaction: discord.Interaction, button: ui.Button):
                if interaction.user.id != ctx.author.id:
                    return await interaction.response.send_message("Only the dealer can cancel.", ephemeral=True)
                await interaction.response.edit_message(
                    embed=make_embed("↩️ Cancelled", "Deal done was cancelled.", discord.Color.blurple()), view=None)
                self_inner.stop()

        await ctx.send(embed=confirm_embed, view=DealDoneConfirmView())
        return
    # Create deal
    matches = _re.findall(r"\[(.*?)\]", args)
    if len(matches) < 2:
        return await ctx.send(embed=make_embed("❌ Invalid Usage",
            "**Usage:** `.deal [Product] [Amount]`\n**Example:** `.deal [Netflix 1M] [0.005 LTC]`", discord.Color.red()))
    product, amount = matches[0], matches[1]
    ticket = tickets_col.find_one({"channel_id": ctx.channel.id})
    if not ticket:
        return await ctx.send(embed=make_embed("❌ No Ticket Found",
            "Could not detect ticket owner. Ensure this is a ticket channel.", discord.Color.red()))
    buyer = ctx.guild.get_member(ticket["owner_id"])
    if not buyer:
        return await ctx.send(embed=make_embed("❌ Buyer Not Found",
            "The ticket owner could not be found in this server.", discord.Color.red()))
    existing_proposal = proposals_col.find_one({"channel_id": ctx.channel.id})
    if existing_proposal:
        return await ctx.send(embed=make_embed("⚠️ Proposal Already Pending",
            f"There is already a pending deal proposal in this channel for **{existing_proposal['product']}** at **{existing_proposal['amount']}**.\n"
            "The buyer must confirm or cancel it before you can create a new one.", discord.Color.orange()))
    proposals_col.update_one({"channel_id": ctx.channel.id}, {"$set": {
        "channel_id": ctx.channel.id, "buyer_id": buyer.id, "dealer_id": ctx.author.id,
        "product": product, "amount": amount}}, upsert=True)
    preview_embed = make_embed(
        title="🤝 Deal Proposal",
        description=f"<@{buyer.id}>, please review the deal details below and confirm if everything is correct.",
        color=discord.Color.orange(),
        fields=[("👤 Buyer", f"<@{buyer.id}>", True), ("🧑‍💼 Dealer", f"<@{ctx.author.id}>", True),
                ("📦 Product", product, False), ("💰 Amount", amount, False)],
        footer="Only the buyer can confirm or cancel this deal.")
    await ctx.send(content=f"<@{buyer.id}>", embed=preview_embed, view=DealConfirmView())

@bot.command()
async def close(ctx):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    deal_doc = deals_col.find_one({"channel_id": ctx.channel.id})
    if deal_doc and not is_head_dealer(ctx.author):
        return await ctx.send(embed=make_embed("⚠️ Active Deal",
            "Cannot close — there is an active deal in this ticket.\nOnly a Head Dealer can force close.", discord.Color.orange()))
    await ctx.send(embed=make_embed("🔒 Close Ticket?", "Are you sure you want to close this ticket? The buyer will lose access.", discord.Color.orange()),
        view=CloseConfirmView(ctx.author.id))

@bot.command()
async def reopen(ctx):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    ticket = tickets_col.find_one({"channel_id": ctx.channel.id})
    owner = ctx.guild.get_member(ticket["owner_id"]) if ticket else None
    dealer_role = ctx.guild.get_role(DEALER_ROLE_ID)
    tm_role = ctx.guild.get_role(TICKET_MANAGER_ROLE_ID)
    overwrites = {ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                  dealer_role: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
    if tm_role:
        overwrites[tm_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    if owner:
        overwrites[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    await ctx.channel.edit(overwrites=overwrites)
    await ctx.send(embed=make_embed("🔓 Ticket Reopened", "Buyer access has been restored.", discord.Color.green()))

@bot.command()
async def rename(ctx, *, new_name):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    clean = new_name.strip().replace("[", "").replace("]", "").replace(" ", "-").lower()
    if ctx.channel.name.startswith("support-"):
        new_channel_name = f"support-{clean}"
    elif ctx.channel.name.startswith("buy-"):
        new_channel_name = f"buy-{clean}"
    else:
        new_channel_name = f"ticket-{clean}"
    await ctx.channel.edit(name=new_channel_name)
    await ctx.send(embed=make_embed("✏️ Ticket Renamed", f"Channel renamed to `{new_channel_name}`.", discord.Color.blurple()))

@bot.command()
async def add(ctx, member: discord.Member):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    await ctx.channel.set_permissions(member, view_channel=True, send_messages=True)
    await ctx.send(embed=make_embed("➕ Member Added", f"{member.mention} has been added to this ticket.", discord.Color.green()))

@bot.command()
async def remove(ctx, member: discord.Member):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    await ctx.channel.set_permissions(member, view_channel=False, send_messages=False)
    await ctx.send(embed=make_embed("➖ Member Removed", f"{member.mention} has been removed from this ticket.", discord.Color.orange()))

@bot.command()
async def delete(ctx):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    deal_doc = deals_col.find_one({"channel_id": ctx.channel.id})
    if deal_doc and not is_head_dealer(ctx.author):
        return await ctx.send(embed=make_embed("⚠️ Active Deal",
            "Cannot delete ticket — there is an active deal.\nOnly a Head Dealer can force delete.", discord.Color.orange()))
    await ctx.send(embed=make_embed("🗑️ Deleting Ticket", "This ticket will be **permanently deleted** in 3 seconds...", discord.Color.red()))
    await asyncio.sleep(3)
    tickets_col.delete_one({"channel_id": ctx.channel.id})
    deals_col.delete_one({"channel_id": ctx.channel.id})
    panels_col.delete_one({"channel_id": ctx.channel.id})
    active_deals.pop(ctx.channel.id, None)
    await ctx.channel.delete()

@bot.command()
async def dispute(ctx, *, reason: str = "No reason provided."):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    ticket = tickets_col.find_one({"channel_id": ctx.channel.id})
    deal_doc = deals_col.find_one({"channel_id": ctx.channel.id})
    is_buyer = ticket and ctx.author.id == ticket.get("owner_id")
    if not is_buyer and not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "Only the buyer or a dealer can raise a dispute.", discord.Color.red()))
    head_dealer_role = ctx.guild.get_role(HEAD_DEALER_ROLE_ID)
    dispute_embed = make_embed(
        title="⚠️ Dispute Raised",
        description=f"A dispute has been raised in this ticket by {ctx.author.mention}.",
        color=discord.Color.red(),
        fields=[("👤 Raised By", ctx.author.mention, True),
                ("📦 Product", deal_doc["product"] if deal_doc else "N/A", True),
                ("💰 Amount", deal_doc["amount"] if deal_doc else "N/A", True),
                ("📝 Reason", reason, False), ("📌 Channel", ctx.channel.mention, False)],
        footer="A Head Dealer will review this shortly.")
    await ctx.send(content=head_dealer_role.mention if head_dealer_role else "", embed=dispute_embed)
    log_ch = bot.get_channel(TRANSCRIPT_CHANNEL_ID)
    if log_ch:
        await log_ch.send(embed=dispute_embed)

@bot.command(name="tickettransfer", aliases=["ttransfer"])
async def tickettransfer(ctx, new_dealer: discord.Member):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    if not can_manage_ticket(new_dealer):
        return await ctx.send(embed=make_embed("❌ Invalid Target", f"{new_dealer.mention} is not a dealer or ticket manager.", discord.Color.red()))
    if new_dealer.id == ctx.author.id:
        return await ctx.send(embed=make_embed("❌ Invalid", "You can't transfer a ticket to yourself.", discord.Color.red()))
    await ctx.channel.set_permissions(new_dealer, view_channel=True, send_messages=True)
    await ctx.send(embed=make_embed("🔁 Ticket Transferred",
        f"This ticket has been transferred from {ctx.author.mention} to {new_dealer.mention}.",
        discord.Color.blurple(), fields=[("📌 New Handler", new_dealer.mention, False)], footer="The new dealer has been notified."))
    try:
        await new_dealer.send(embed=make_embed("📨 Ticket Transferred To You",
            f"{ctx.author.mention} has transferred a ticket to you.", discord.Color.blurple(),
            fields=[("📌 Channel", ctx.channel.mention, False), ("🏠 Server", ctx.guild.name, False)],
            footer="Head over to the ticket channel to assist."))
    except discord.Forbidden:
        pass

@bot.command()
async def remind(ctx, minutes: int, *, message: str = "This is your reminder!"):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    if minutes < 1 or minutes > 1440:
        return await ctx.send(embed=make_embed("❌ Invalid Time", "Please provide a time between 1 and 1440 minutes.", discord.Color.red()))
    await ctx.send(embed=make_embed("⏱️ Reminder Set", f"I'll remind you in **{minutes} minute(s)**.",
        discord.Color.blurple(), fields=[("📝 Message", message, False)], footer=f"Set by {ctx.author.display_name}"))
    channel_id, author_id, author_name = ctx.channel.id, ctx.author.id, ctx.author.display_name

    async def send_reminder():
        await asyncio.sleep(minutes * 60)
        ch = bot.get_channel(channel_id)
        if ch:
            await ch.send(content=f"<@{author_id}>", embed=make_embed(
                "⏰ Reminder", message, discord.Color.gold(),
                footer=f"Reminder set {minutes} minute(s) ago by {author_name}"))

    asyncio.create_task(send_reminder())

@bot.command()
async def call(ctx, member: discord.Member = None):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    if member is None:
        ticket = tickets_col.find_one({"channel_id": ctx.channel.id})
        if not ticket:
            return await ctx.send(embed=make_embed("❌ No Ticket Data", "Could not find the ticket owner.", discord.Color.red()))
        member = ctx.guild.get_member(ticket["owner_id"])
        if not member:
            return await ctx.send(embed=make_embed("❌ User Not Found", "The ticket owner is no longer in the server.", discord.Color.red()))
    try:
        await member.send(embed=make_embed("📣 You're Being Called!", f"**{ctx.author.display_name}** is calling you in your ticket.",
            discord.Color.orange(),
            fields=[("📌 Channel", ctx.channel.mention, False), ("🏠 Server", ctx.guild.name, False)],
            footer="Please head back to your ticket channel."))
        await ctx.send(embed=make_embed("📣 Called!", f"{member.mention} has been DM'd to return to this ticket.", discord.Color.green()))
    except discord.Forbidden:
        await ctx.send(embed=make_embed("⚠️ DM Failed", f"Could not DM {member.mention} — their DMs are closed. Pinging them here instead.", discord.Color.orange()))
        await ctx.send(content=f"Hey {member.mention}, you're needed in this ticket!")

@bot.command()
async def transcript(ctx):
    if not (ctx.channel.name.startswith("ticket-") or ctx.channel.name.startswith("buy-") or ctx.channel.name.startswith("support-")):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    await ctx.send(embed=make_embed("📄 Generating Transcript...", "Please wait a moment.", discord.Color.blurple()))
    ticket = tickets_col.find_one({"channel_id": ctx.channel.id})
    buyer_id = ticket["owner_id"] if ticket else None
    await send_html_transcript(ctx.channel, buyer_id)
    await ctx.send(embed=make_embed("✅ Transcript Sent",
        "The transcript has been posted to the log channel" + (f" and DMed to <@{buyer_id}>." if buyer_id else "."),
        discord.Color.green()))

@bot.command()
async def proofsend(ctx, member: discord.Member, amount: str, *, item: str):
    if not is_dealer(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "Only dealers can use `.proofsend`.", discord.Color.red()))
    if not ctx.message.attachments:
        return await ctx.send(embed=make_embed("❌ No Image",
            "Please attach a proof image to the command message.\n**Usage:** `.proofsend @user <amount> <item>` with an image attached.",
            discord.Color.red()))
    images = [a for a in ctx.message.attachments if a.content_type and a.content_type.startswith("image/")]
    if not images:
        return await ctx.send(embed=make_embed("❌ Invalid Attachment",
            "The attached file is not an image. Please attach a PNG, JPG, or similar image file.", discord.Color.red()))
    processing_msg = await ctx.send(embed=make_embed("⏳ Processing...", "Uploading proof image to permanent storage...", discord.Color.blurple()))
    att = images[0]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(att.url) as resp:
                img_bytes = await resp.read()
    except Exception:
        img_bytes = None
    permanent_url = await upload_to_imgbb(img_bytes, att.filename) if img_bytes else None
    final_url = permanent_url or att.url
    footer_text = "Proof Of Delivery"
    try:
        await processing_msg.delete()
    except Exception:
        pass
    proof_ch = bot.get_channel(PROOF_CHANNEL_ID)
    if not proof_ch:
        return await ctx.send(embed=make_embed("❌ Proof Channel Not Found",
            "Could not find the proof channel. Check your PROOF_CHANNEL_ID config.", discord.Color.red()))
    await proof_ch.send(embed=make_embed(title=f"# Sold {item}",
        description=(f"**Dealer:** <@{ctx.author.id}>\n**Buyer:** <@{member.id}>\n**Amount:** {amount}"),
        color=discord.Color.green(), image=final_url, footer=footer_text))
    await ctx.send(embed=make_embed("✅ Proof Posted", f"Proof for **{item}** has been posted to <#{PROOF_CHANNEL_ID}>.",
        discord.Color.green(),
        fields=[("👤 Buyer", member.mention, True), ("💰 Amount", amount, True), ("📦 Item", item, False)],
        footer=footer_text))
    for extra in images[1:]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(extra.url) as resp:
                    extra_bytes = await resp.read()
            extra_url = await upload_to_imgbb(extra_bytes, extra.filename) or extra.url
        except Exception:
            extra_url = extra.url
        await proof_ch.send(embed=make_embed(title=f"📸 Additional Proof — {item}",
            color=discord.Color.green(), image=extra_url, footer=footer_text))

# ══════════════════════════════════════════════════════════════════════════════
#  PREFIX COMMANDS — SHOP SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

@bot.command(name="setshopadminrole")
async def setshopadminrole(ctx, role: discord.Role):
    if ctx.author.id != ctx.guild.owner_id:
        return await ctx.send("❌ Server owner only.")
    set_config(ctx.guild.id, "shop_admin_role", role.id)
    await ctx.send(f"<:tick:1546740263437738055> Shop admin role set to {role.mention}.")

@bot.command(name="orderbanner")
async def orderbanner(ctx, image_link: str):
    if not is_shop_admin_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ Shop admin role required.")
    set_config(ctx.guild.id, "order_banner", image_link)
    await ctx.send("<:tick:1546740263437738055> Order complete banner updated!")

@bot.command(name="feedbackbanner")
async def feedbackbanner(ctx, image_link: str):
    if not is_shop_admin_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ Shop admin role required.")
    set_config(ctx.guild.id, "feedback_banner", image_link)
    await ctx.send("<:tick:1546740263437738055> Feedback banner updated!")

@bot.command(name="setfeedbackchannel")
async def setfeedbackchannel(ctx, channel: discord.TextChannel):
    if not is_shop_admin_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ Shop admin role required.")
    set_config(ctx.guild.id, "feedback_channel", channel.id)
    await ctx.send(f"<:tick:1546740263437738055> Feedback log channel set to {channel.mention}")

@bot.command(name="setorderchannel")
async def setorderchannel(ctx, channel: discord.TextChannel):
    if not is_shop_admin_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ Shop admin role required.")
    set_config(ctx.guild.id, "order_channel", channel.id)
    await ctx.send(f"<:tick:1546740263437738055> Order complete channel set to {channel.mention}")

@bot.command(name="changeorderno")
async def changeorderno(ctx, number: int = None):
    if not is_shop_admin_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ Shop admin role required.")
    if number is None or number < 1:
        return await ctx.send("**Usage:** `.changeorderno <number>`\nExample: `.changeorderno 5` - next order will be #5")
    # Store n-1 because _next_order_number increments before returning
    config_col.update_one(
        {"guild_id": ctx.guild.id},
        {"$set": {"order_counter": number - 1}},
        upsert=True
    )
    await ctx.send(f"✅ Order counter updated — next order will be **#{number}**")

@bot.command(name="ordercomplete")
async def ordercomplete(ctx, *, args: str):
    # Re-fetch author as Member (needed when ctx.author is a bot/User object)
    author = ctx.guild.get_member(ctx.author.id) or ctx.author

    # Bots are already gated in on_message (only ordercomplete is allowed through),
    # so skip the shop admin check for them to prevent false permission failures.
    if not ctx.author.bot and not is_shop_admin_check(author, ctx.guild.id):
        return await ctx.send("❌ Shop admin role required.")

    # Syntax: .ordercomplete [Product] [Amount] @user
    # Optional seller override for bots: .ordercomplete [Product] [Amount] @user seller:SellerName
    # Supports: proper Discord mention <@123>, plain @Username, or raw numeric ID
    pattern = _re.compile(r'\[(.+?)\]\s*\[(.+?)\]\s*(?:<@!?(\d+)>|@([^\s<]+)|(\d{17,20}))')
    match = pattern.search(args)
    if not match:
        return await ctx.send(
            "⚠️ **Usage:** `.ordercomplete [Product Name] [Amount] @user`\n"
            "Example: `.ordercomplete [Netflix Premium] [9.99$] @John`\n"
            "Bot usage: `.ordercomplete [Netflix Premium] [9.99$] @John seller:SellerName`"
        )

    product = match.group(1).strip()
    amount  = match.group(2).strip()
    # Default currency: if amount is a plain number (e.g. "5" or "9.99"), append "$"
    if _re.fullmatch(r'\d+(\.\d+)?', amount):
        amount = amount + "$"

    # Resolve buyer — could be a Discord mention ID, plain @username, or raw numeric ID
    mention_id = match.group(3)   # captured from <@123>
    plain_name = match.group(4)   # captured from @Username
    raw_id     = match.group(5)   # captured from bare numeric ID

    buyer_id = None
    buyer    = None

    if mention_id:
        buyer_id = int(mention_id)
        buyer = ctx.guild.get_member(buyer_id) or await bot.fetch_user(buyer_id)
    elif raw_id:
        buyer_id = int(raw_id)
        buyer = ctx.guild.get_member(buyer_id) or await bot.fetch_user(buyer_id)
    elif plain_name:
        plain_lower = plain_name.lower()
        # Try get_member_named first (handles Name#discrim and display names)
        buyer = ctx.guild.get_member_named(plain_name)
        if not buyer:
            # Fallback: case-insensitive search through cached members
            buyer = discord.utils.find(
                lambda m: m.name.lower() == plain_lower or m.display_name.lower() == plain_lower,
                ctx.guild.members
            )
        if buyer:
            buyer_id = buyer.id

    # Optional seller name override — useful when a bot runs the command
    seller_override_match = _re.search(r'seller:(.+?)(?:\s|$)', args, _re.IGNORECASE)
    if seller_override_match:
        seller_username = seller_override_match.group(1).strip()
        seller_id = ctx.author.id  # store bot's ID but display override name
    else:
        seller_username = author.display_name
        seller_id = author.id

    if not buyer:
        return await ctx.send("❌ Could not find that user.")

    order_num = _next_order_number(ctx.guild.id)
    now = datetime.datetime.now(datetime.timezone.utc)
    orders_col.insert_one({
        "order_num": order_num, "guild_id": ctx.guild.id,
        "seller_id": seller_id, "buyer_id": buyer_id,
        "product": product, "amount": amount, "timestamp": now
    })

    cfg = get_config(ctx.guild.id)
    order_banner    = cfg.get("order_banner", "")
    fb_banner       = cfg.get("feedback_banner", "")
    fb_channel_id   = cfg.get("feedback_channel", ctx.channel.id)
    guild_name      = ctx.guild.name

    # Resolve order channel — fall back to current channel if not configured or not found
    order_channel_id = cfg.get("order_channel")
    if order_channel_id:
        order_ch = ctx.guild.get_channel(int(order_channel_id))
        if order_ch is None:
            await ctx.send(f"⚠️ Order channel (ID `{order_channel_id}`) not found — sending here instead.")
            order_ch = ctx.channel
    else:
        order_ch = ctx.channel

    order_embed = discord.Embed(
        title="<a:blueverify:1546740183716855872> Order Completed",
        color=0x5865F2, timestamp=now)
    order_embed.add_field(
        name=f"<:cart:1546740191773986977> Order Number: #{order_num}",
        value=(
            f"<:curvedarrow:1546740206164643933> <:Buyer:1546740161205899285> **Buyer:** {buyer.mention}\n"
            f"<:curvedarrow:1546740206164643933> <:blue_box:1546740176041283594> **Product:** {product}\n"
            f"<:curvedarrow:1546740206164643933> <:cash:1546740198870749224> **Price:** {amount}"
        ), inline=False)
    if order_banner:
        order_embed.set_image(url=order_banner)
    order_embed.set_footer(text=f"Order Completed by {seller_username} • {guild_name}")
    try:
        await order_ch.send(embed=order_embed)
    except discord.Forbidden:
        await ctx.send(f"❌ Missing permission to send in the order channel <#{order_ch.id}>. Please check bot permissions.")
        return
    except Exception as e:
        await ctx.send(f"❌ Failed to send order embed: `{e}`")
        return

    fb_embed = discord.Embed(
        title="<a:blueverify:1546740183716855872> New FeedBack Request",
        color=0x5865F2, timestamp=now)
    fb_embed.add_field(name="\u200b", value=(
        f"<:blue_box:1546740176041283594> **Product:** {product}\n"
        f"<:cash:1546740198870749224> **Price:** {amount}\n"
        f"<:sellerITS:1546740256706142318> **Seller:** {seller_username}"
    ), inline=False)
    if fb_banner:
        fb_embed.set_image(url=fb_banner)
    if ctx.guild.icon:
        fb_embed.set_thumbnail(url=ctx.guild.icon.url)

    # ── Auto-assign Customer role to the buyer ────────────────────────────────
    buyer_member = ctx.guild.get_member(buyer_id)
    if buyer_member and BUYER_ROLE_ID:
        customer_role = ctx.guild.get_role(BUYER_ROLE_ID)
        if customer_role and customer_role not in buyer_member.roles:
            try:
                await buyer_member.add_roles(customer_role, reason=f"Auto-assigned after order #{order_num}")
            except Exception:
                pass  # silently skip if missing perms

    fb_view = FeedbackView(
        order_num=order_num, product=product, amount=amount,
        seller_name=seller_username, seller_id=seller_id,
        buyer_id=buyer_id, feedback_channel_id=fb_channel_id,
        guild_id=ctx.guild.id)
    try:
        await buyer.send(embed=fb_embed, view=fb_view)
        await ctx.send(f"✅ Order #{order_num} completed. Feedback DM sent to {buyer.mention}.")
    except discord.Forbidden:
        await ctx.send(
            f"✅ Order #{order_num} completed. ⚠️ Could not DM {buyer.mention} — their DMs are closed.")
    except Exception as e:
        await ctx.send(f"✅ Order #{order_num} completed. ⚠️ Feedback DM failed: `{e}`")


async def _send_ltc_balance_embed(ctx_or_inter, address: str, is_slash: bool = False):
    """Shared by .mybal, .bal <address>, and their slash equivalents."""
    async def _reply(content=None, embed=None):
        if is_slash:
            if ctx_or_inter.response.is_done():
                await ctx_or_inter.followup.send(content=content, embed=embed)
            else:
                await ctx_or_inter.response.send_message(content=content, embed=embed)
        else:
            await ctx_or_inter.send(content=content, embed=embed)

    if not BLOCKCYPHER_TOKEN:
        return await _reply("❌ LTC balance checking isn't configured on this bot (missing `BLOCKCYPHER_TOKEN`). This command is unavailable right now.")

    await _reply("<:curvedarrow:1546740206164643933> Fetching balance…")
    data = await _fetch_ltc_address_full(address)
    if not data:
        return await _reply("❌ Could not reach BlockCypher, or that address is invalid. Try again shortly.")
    usd_rate = await _get_ltc_usd_rate()
    eur_rate = usd_rate * 0.92
    pending_ltc   = data.get("unconfirmed_balance", 0) / 1e8
    confirmed_ltc = data.get("balance", 0) / 1e8
    total_ltc     = data.get("total_received", 0) / 1e8
    txrefs = data.get("txrefs", [])[:3]
    tx_lines = []
    for tx in txrefs:
        tx_hash = tx.get("tx_hash", "unknown")
        tx_val = tx.get("value", 0) / 1e8
        spent = tx.get("spent", False)
        tx_type = "Sent" if spent else "Received"
        tx_lines.append(f"<:curvedarrow:1546740206164643933> **{tx_type}** · `${tx_val * usd_rate:.2f}`\n[View transaction](https://live.blockcypher.com/ltc/tx/{tx_hash}/)")
    e = discord.Embed(title=f"{address[:18]}…{address[-6:]} · Balance", color=0x5865F2,
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.add_field(name="<:cash:1546740198870749224> Pending Balance",
        value=f"`{pending_ltc * eur_rate:.2f} € | {pending_ltc * usd_rate:.2f} $ | {pending_ltc:.6f} LTC`", inline=False)
    e.add_field(name="<:cash:1546740198870749224> Confirmed Balance",
        value=f"`{confirmed_ltc * eur_rate:.2f} € | {confirmed_ltc * usd_rate:.2f} $ | {confirmed_ltc:.6f} LTC`", inline=False)
    e.add_field(name="<:cash:1546740198870749224> Total Amount Received",
        value=f"`{total_ltc * eur_rate:.2f} € | {total_ltc * usd_rate:.2f} $ | {total_ltc:.6f} LTC`", inline=False)
    e.add_field(name="<:blue1:1546740168923553842> Last 3 Transactions",
        value="\n\n".join(tx_lines) if tx_lines else "*No recent transactions*", inline=False)
    e.set_footer(text="The only utility bot you need")
    await _reply(embed=e)


@bot.command(name="mybal")
async def mybal(ctx):
    address = _get_ltc(ctx.author.id)
    if not address:
        return await ctx.send("<:blue1:1546740168923553842> You haven't set an LTC wallet yet. Use `.setltc <address>` (or `.setmybal <address>`) first.")
    await _send_ltc_balance_embed(ctx, address)

@bot.tree.command(name="mybal", description="Check the LTC balance of your saved default address")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def slash_mybal_check(inter: discord.Interaction):
    address = _get_ltc(inter.user.id)
    if not address:
        return await inter.response.send_message("<:blue1:1546740168923553842> You haven't set an LTC wallet yet. Use `/setmybal` first.")
    await _send_ltc_balance_embed(inter, address, is_slash=True)

@bot.command(name="bal")
async def bal(ctx, address: str):
    """Check the LTC balance of ANY address — unlike .mybal, doesn't need to be saved."""
    await _send_ltc_balance_embed(ctx, address)

@bot.tree.command(name="bal", description="Check the LTC balance of any address")
@app_commands.describe(address="Any LTC wallet address to look up")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def slash_bal(inter: discord.Interaction, address: str):
    await _send_ltc_balance_embed(inter, address, is_slash=True)

@bot.command(name="setmybal")
async def setmybal(ctx, address: str):
    """Alias of .setltc — sets the default address .mybal checks."""
    await setltc(ctx, address)

@bot.tree.command(name="setmybal", description="Set your default LTC address for /mybal")
@app_commands.describe(address="Your LTC wallet address")
async def slash_setmybal(inter: discord.Interaction, address: str):
    ltc_wallets_col.update_one({"user_id": inter.user.id},
        {"$set": {"ltc_address": address, "updated_at": datetime.datetime.now(datetime.timezone.utc)}}, upsert=True)
    e = discord.Embed(title="<:tick:1546740263437738055> LTC Wallet Saved", color=0x5865F2,
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.add_field(name="\u200b", value=(
        f"<:Buyer:1546740161205899285> **User:** {inter.user.mention}\n"
        f"<:blue1:1546740168923553842> <:cash:1546740198870749224> **Address:** `{address}`"), inline=False)
    e.set_footer(text="LTC Wallet")
    await inter.response.send_message(embed=e)

@bot.command(name="addbal")
async def addbal(ctx, member: discord.Member, amount: float):
    if not is_shop_admin_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ Shop admin role required.")
    _add_balance(member.id, amount)
    new_bal = _get_balance(member.id)
    e = discord.Embed(color=0x57F287, description=(
        f"<:tick:1546740263437738055> Added `${amount:.2f}` to {member.mention}'s balance.\n"
        f"<:cash:1546740198870749224> **New Balance:** `${new_bal:.2f}`"))
    await ctx.send(embed=e)

@bot.command(name="ctest")
async def ctest(ctx):
    """Sends every custom emoji this server has, so you can eyeball that
    they all rendered/uploaded correctly after a migration. Prefix-only —
    purely a visual sanity check, no need for a slash version."""
    try:
        # Fetch live from Discord's API instead of trusting the gateway
        # cache — the cache can lag behind (e.g. emojis added/removed after
        # the bot connected), which was making this look like it only
        # listed a fixed handful of "hardcoded" emojis instead of every
        # custom emoji actually on the server.
        emojis = await ctx.guild.fetch_emojis()
    except discord.HTTPException:
        emojis = ctx.guild.emojis  # fall back to the cache if the API call fails
    if not emojis:
        return await ctx.send("❌ This server has no custom emojis.")
    emojis = sorted(emojis, key=lambda e: e.name.lower())
    chunk, chunks = "", []
    for e in emojis:
        line = f"{e} `{e.name}`\n"
        if len(chunk) + len(line) > 1900:
            chunks.append(chunk)
            chunk = ""
        chunk += line
    if chunk:
        chunks.append(chunk)
    await ctx.send(f"Found **{len(emojis)}** custom emoji(s) on this server:")
    for c in chunks:
        await ctx.send(c)

@bot.command(name="calc")
async def calc(ctx, *, expression: str):
    await ctx.send(embed=_build_calc_embed(expression))

@bot.tree.command(name="calc", description="Evaluate a math expression")
@app_commands.describe(expression="e.g. (12 + 8) * 3 / 4")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def slash_calc(inter: discord.Interaction, expression: str):
    await inter.response.send_message(embed=_build_calc_embed(expression))

@bot.command(name="afk")
async def afk(ctx, *, reason: str = "AFK"):
    afk_col.update_one(
        {"user_id": ctx.author.id},
        {"$set": {"reason": reason, "since": datetime.datetime.now(datetime.timezone.utc)}},
        upsert=True)
    e = discord.Embed(color=0x5865F2, description=(
        f"<:tick:1546740263437738055> {ctx.author.mention} is now AFK: **{reason}**"))
    await ctx.send(embed=e)

@bot.tree.command(name="afk", description="Set yourself as AFK")
@app_commands.describe(reason="Why you're AFK (optional)")
async def slash_afk(inter: discord.Interaction, reason: str = "AFK"):
    afk_col.update_one(
        {"user_id": inter.user.id},
        {"$set": {"reason": reason, "since": datetime.datetime.now(datetime.timezone.utc)}},
        upsert=True)
    e = discord.Embed(color=0x5865F2, description=(
        f"<:tick:1546740263437738055> {inter.user.mention} is now AFK: **{reason}**"))
    await inter.response.send_message(embed=e)

@bot.command(name="setltc")
async def setltc(ctx, address: str):
    ltc_wallets_col.update_one({"user_id": ctx.author.id},
        {"$set": {"ltc_address": address, "updated_at": datetime.datetime.now(datetime.timezone.utc)}}, upsert=True)
    e = discord.Embed(title="<:tick:1546740263437738055> LTC Wallet Saved", color=0x5865F2,
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.add_field(name="\u200b", value=(
        f"<:Buyer:1546740161205899285> **User:** {ctx.author.mention}\n"
        f"<:blue1:1546740168923553842> <:cash:1546740198870749224> **Address:** `{address}`"), inline=False)
    e.set_footer(text=f"LTC Wallet")
    await ctx.send(embed=e)

@bot.command(name="tx")
async def tx_command(ctx, txid: str):
    if not is_shop_admin_check(ctx.author, ctx.guild.id):
        return await ctx.send("❌ Shop admin role required.")
    await ctx.send(f"<:curvedarrow:1546740206164643933> Looking up TX `{txid}`…")
    data = await _fetch_ltc_tx(txid)
    if not data:
        return await ctx.send("❌ Could not fetch that transaction. Double-check the TXID or ensure `BLOCKCYPHER_TOKEN` is set.")
    confirmations = data.get("confirmations", 0)
    total_ltc = data.get("total", 0) / 1e8
    received_at = data.get("received", "Unknown")
    tx_embed = discord.Embed(title="<:cash:1546740198870749224> Transaction Recorded", color=0xA4C2F4,
                              timestamp=datetime.datetime.now(datetime.timezone.utc))
    tx_embed.add_field(name="\u200b", value=(
        f"<:blue1:1546740168923553842> **TXID:** `{txid}`\n"
        f"<:cash:1546740198870749224> **Amount:** `{total_ltc:.8f} LTC`\n"
        f"<:tick:1546740263437738055> **Confirmations:** `{confirmations}`\n"
        f"<:curvedarrow:1546740206164643933> **Received:** `{received_at}`"), inline=False)
    tx_embed.set_footer(text=f"BlockCypher • LTC")
    await ctx.send(embed=tx_embed)
    await _dm_tx_confirmation(bot, data, txid)

@bot.command(name="checktx")
async def checktx(ctx, txid: str):
    await ctx.send(f"<:curvedarrow:1546740206164643933> Checking TX `{txid}`…")
    data = await _fetch_ltc_tx(txid)
    if not data:
        return await ctx.send("❌ Transaction not found. Check the TXID or try again later.")
    confirmations = data.get("confirmations", 0)
    total_ltc = data.get("total", 0) / 1e8
    received_at = data.get("received", "Unknown")
    confirmed = confirmations >= 1
    status_icon = "<:tick:1546740263437738055>" if confirmed else "<:blue1:1546740168923553842>"
    status_text = (f"**Confirmed** ({confirmations} confirmation{'s' if confirmations != 1 else ''})"
                   if confirmed else f"**Pending** ({confirmations} confirmations)")
    check_embed = discord.Embed(title=f"{status_icon} Transaction Status",
        color=0x57F287 if confirmed else 0xFEE75C, timestamp=datetime.datetime.now(datetime.timezone.utc))
    check_embed.add_field(name="\u200b", value=(
        f"<:blue1:1546740168923553842> **TXID:** `{txid}`\n"
        f"<:cash:1546740198870749224> **Amount:** `{total_ltc:.8f} LTC`\n"
        f"{status_icon} **Status:** {status_text}\n"
        f"<:curvedarrow:1546740206164643933> **Received:** `{received_at}`"), inline=False)
    check_embed.set_footer(text=f"BlockCypher • LTC")
    await ctx.send(embed=check_embed)
    if confirmed:
        await _dm_tx_confirmation(bot, data, txid)

# ══════════════════════════════════════════════════════════════════════════════
#  HELP COMMAND — MERGED (covers both Moderator + Ticket sections)
# ══════════════════════════════════════════════════════════════════════════════
#  HELP — DROPDOWN MENU SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

HELP_CATEGORIES = {
    "🏠 Overview": {
        "emoji": "🏠",
        "color": 0x5865F2,
        "description": "**Prefix:** `.` or `!` · **Slash:** `/`\n\nSelect a category from the dropdown below to view commands.",
        "fields": [
            ("📦 Categories Available", (
                "🛡️ Moderation\n"
                "📋 Info & Utility\n"
                "🔒 Channel Tools\n"
                "📅 Scheduled Messages\n"
                "🤖 Auto Tools\n"
                "🎫 Ticket System\n"
                "🛒 Shop & LTC\n"
                "🎉 Giveaways\n"
                "😀 Emoji Tools\n"
                "📜 TOS Manager\n"
                "🧾 Customer Verification\n"
                "⚙️ Config & Admin"
            ), False),
            ("🌐 Works in DMs", (
                "`/vouch`, `/mybal`, `/bal`, `/calc` also work as slash "
                "commands in your DMs with the bot, not just in the server."
            ), False),
        ],
    },
    "🛡️ Moderation": {
        "emoji": "🛡️",
        "color": 0xe74c3c,
        "description": "Moderation commands — requires Mod Role.",
        "fields": [
            ("🔒 Moderation Commands", (
                "`.ban @user [reason]` — ban\n"
                "`.kick @user [reason]` — kick\n"
                "`.warn @user [reason]` — warn\n"
                "`.warnings @user` — view warnings\n"
                "`.unban <id>` — unban\n"
                "`.mute @user [min]` — timeout\n"
                "`.unmute @user` — remove timeout\n"
                "`.softban @user` — ban+unban (clears msgs)"
            ), False),
        ],
    },
    "📋 Info & Utility": {
        "emoji": "📋",
        "color": 0x3498db,
        "description": "General info and utility commands.",
        "fields": [
            ("👤 User Info", (
                "`.userinfo [@user]` — member info\n"
                "`.serverinfo` — server info\n"
                "`.modlogs @user` — mod history\n"
                "`.warnings @user` — warning list"
            ), False),
            ("🛠️ Tools", (
                "`.latency` / `.botping` — bot ping\n"
                "`.announce #ch <msg>` — send announcement\n"
                "`.poll <question>` — yes/no poll\n"
                "`.report @user <reason>` — report to mods\n"
                "`.dmannounce <text>` — DM all members\n"
                "`.dmdelete <id>` — delete a DM announcement\n"
                "`.afk [reason]` — set yourself as AFK\n"
                "`.calc <expression>` / `/calc` — evaluate a math expression *(DM-enabled)*"
            ), False),
        ],
    },
    "🔒 Channel Tools": {
        "emoji": "🔒",
        "color": 0xf39c12,
        "description": "Channel and role management commands.",
        "fields": [
            ("🔒 Lock / Visibility", (
                "`.lock [#ch]` — lock channel\n"
                "`.unlock [#ch]` — unlock channel\n"
                "`.lockdown` — lock ALL channels\n"
                "`.hide [#ch]` — hide channel\n"
                "`.unhide [#ch]` — show channel\n"
                "`.nuke` — clone & wipe channel"
            ), False),
            ("🧹 Messages", (
                "`.clear [n]` — delete n messages\n"
                "`.purge @user [n]` — delete user's messages\n"
                "`.purgebot` — delete bot messages\n"
                "`.slowmode [s]` — set slowmode (0 = off)"
            ), False),
            ("🏗️ Create / Delete", (
                "`.channelcreate <name> [cat_id]` — create channel\n"
                "`.deletechannel [#ch]` — delete channel\n"
                "`.rolecreate <name> [colour]` — create role\n"
                "`.role add @user <RoleName>` — give role\n"
                "`.role remove @user <RoleName>` — remove role"
            ), False),
            ("📌 Sticky / Status", (
                "`.stickymessage #ch <msg>` — set sticky\n"
                "`.stickyremove #ch` — remove sticky\n"
                "`.setstatus <type> <msg>` — bot status"
            ), False),
        ],
    },
    "📅 Scheduled Messages": {
        "emoji": "📅",
        "color": 0x9b59b6,
        "description": "Recurring messages that re-send on a timer, deleting the previous copy first.",
        "fields": [
            ("📅 Manage", (
                "`.setscheuledmessage <time> <message>` — schedule a recurring message in this channel\n"
                "`.removeschedulemessage <name/id>` — remove a scheduled message (list if no name given)\n"
                "*Time formats:* `10s` `10m` `2h` `1d`"
            ), False),
        ],
    },
    "🤖 Auto Tools": {
        "emoji": "🤖",
        "color": 0x1abc9c,
        "description": "Autoresponder, autopurge, word filter and anti-invite.",
        "fields": [
            ("🤖 Autoresponder", (
                "`.autoresponder <trigger> <msg>` — set response\n"
                "`.autoresponder_remove <trigger>` — remove\n"
                "`.autoresponder_list` — list all"
            ), False),
            ("🗑️ Autopurge", (
                "`.autopurge #ch <timer>` — auto-delete on schedule\n"
                "`.autopurge_remove #ch` — stop autopurge\n"
                "`.autopurge_list` — list schedules\n"
                "*Timer format: `10m` `2h` `1d`*"
            ), False),
            ("🚫 Filter / Anti-Invite", (
                "`.filter add <word>` — block a word\n"
                "`.filter remove <word>` — unblock\n"
                "`.filter list` — show filtered words\n"
                "`.antiinvite enable/disable` — block invite links"
            ), False),
            ("🟢 Auto Status Role", (
                "`.autostatusrole set <status text> <role>` — configure\n"
                "`.autostatusrole remove` — disable\n"
                "`.autostatusrole status` — view current config\n"
                "`.autostatusrole check [user]` — manually check a member\n"
                "*Grants a role when a member's custom status contains the configured text.*"
            ), False),
        ],
    },
    "🎫 Ticket System": {
        "emoji": "🎫",
        "color": 0x2ecc71,
        "description": "Ticket and deal management. Most commands only work inside ticket channels.",
        "fields": [
            ("🛒 Panel & Payment *(Dealer)*", (
                "`.panel` / `/panel` — post ticket open panel (slash version now shows a dropdown to pick between all 5 panels)\n"
                "`.supppanel` — post support-only panel\n"
                "`.verify` — post server verification panel\n"
                "`.ltc <address>` — set your LTC address\n"
                "`.upi <id> <img_url>` — set UPI + QR"
            ), False),
            ("📝 Vouch *(Dealer)*", (
                "`.vouch <text> <amount>` / `/vouch` — get a copyable vouch card for the buyer *(DM-enabled)*\n"
                "`.setvouchuser <userid>` — credit a different (alt) user ID in your vouch cards instead of your own\n"
                "`.setvouchuser reset` — clear the override"
            ), False),
            ("🤝 Deal *(Dealer)*", (
                "`.deal [Product] [Amount]` — create deal\n"
                "`.deal done` — mark delivery done\n"
                "`.proofsend @user <amount> <item>` — post proof (attach image)"
            ), False),
            ("🎫 Ticket Management *(Dealer / Manager)*", (
                "`.close` — close ticket\n"
                "`.reopen` — reopen ticket\n"
                "`.delete` — permanently delete channel\n"
                "`.rename <name>` — rename channel\n"
                "`.add @user` — add member\n"
                "`.remove @user` — remove member\n"
                "`.tickettransfer / .ttransfer @dealer` — transfer to dealer\n"
                "`.transcript` — save & DM transcript"
            ), False),
            ("🛡️ Dispute & Comms", (
                "`.dispute <reason>` — raise dispute (pings Head Dealer)\n"
                "`.call [@user]` — DM buyer to return\n"
                "`.remind <min> <msg>` — set reminder in ticket"
            ), False),
        ],
    },
    "🛒 Shop & LTC": {
        "emoji": "🛒",
        "color": 0xf1c40f,
        "description": "Shop order system and LTC crypto tools.",
        "fields": [
            ("📦 Orders", (
                "`.ordercomplete [Product] [Amount] @user` — complete order\n"
                "`.changeorderno <number>` — set next order number\n"
                "`.setorderchannel #ch` — order embed channel\n"
                "`.setfeedbackchannel #ch` — feedback log channel\n"
                "`.orderbanner <url>` — order banner image\n"
                "`.feedbackbanner <url>` — feedback banner image\n"
                "`.setshopadminrole @role` — set shop admin"
            ), False),
            ("🪙 LTC & Balance", (
                "`.mybal` / `/mybal` — check your saved LTC wallet balance *(DM-enabled)*\n"
                "`.bal <address>` / `/bal` — check the balance of ANY LTC address *(DM-enabled)*\n"
                "`.setltc <address>` / `.setmybal <address>` — save your default LTC wallet\n"
                "`.tx <txid>` — record TX + DM wallet owner *(admin)*\n"
                "`.checktx <txid>` — check TX status\n"
                "`.addbal @user <amount>` — add balance *(admin)*"
            ), False),
        ],
    },
    "🎉 Giveaways": {
        "emoji": "🎉",
        "color": 0x00c8dc,
        "description": "Start, end, and re-roll giveaways.",
        "fields": [
            ("🎉 Manage *(Mod)*", (
                "`.gstart <time> <winners> <prize>` — start a giveaway\n"
                "`.gend <message_id>` — end a giveaway immediately\n"
                "`.greroll <message_id>` — re-roll winner(s)\n"
                "*Time formats:* `10s` `10m` `2h` `1d`"
            ), False),
        ],
    },
    "😀 Emoji Tools": {
        "emoji": "😀",
        "color": 0xf39c12,
        "description": "Manage custom server emojis — requires Mod Role.",
        "fields": [
            ("😀 Emoji Commands", (
                "`.addemoji <emoji1> [emoji2...]` — add emoji(s) from other servers\n"
                "`.removeemoji <emoji>` — remove an emoji by name or mention\n"
                "`.renameemoji <emoji> <new_name>` — rename an existing emoji\n"
                "`.listemoji` — list all custom emojis in the server\n"
                "`.ctest` — post every custom emoji (visual sanity check)"
            ), False),
        ],
    },
    "📜 TOS Manager": {
        "emoji": "📜",
        "color": 0x9b59b6,
        "description": "Manage the server Terms of Service panel.",
        "fields": [
            ("📜 TOS Panel *(Mod)*", (
                "`.tos` — post the interactive TOS panel"
            ), False),
            ("✏️ Manage Sections *(Admin)*", (
                "`.tosadd [Subject] [Text]` — add a TOS section\n"
                "`.tosremove [Subject]` — remove a TOS section\n"
                "`.tosedit [Subject] [New Text]` — edit a TOS section\n"
                "`.tosreorder [Sub1] [Sub2] ...` — reorder sections\n"
                "`.tosemoji [Subject] [emoji]` — set section emoji"
            ), False),
            ("🔍 View", (
                "`.toslist` — list all TOS sections\n"
                "`.tosview [Subject]` — view full content of a section\n"
                "`.tosorder` — show current order pre-formatted for `.tosreorder`"
            ), False),
        ],
    },
    "🧾 Customer Verification": {
        "emoji": "🧾",
        "color": 0x1abc9c,
        "description": "Invoice/order verification panel for customers claiming a purchase.",
        "fields": [
            ("🧾 Panel *(Mod / Shop Admin)*", (
                "`.customerpanel` — post the Customer Verification panel\n"
                "`/panel` — also lets an admin post this (and the other 4 panels) from one dropdown"
            ), False),
            ("⚙️ Setup *(Admin, slash only)*", (
                "`/customerconfig` — set customer role, verification channel, seller roles, and optional log channel"
            ), False),
        ],
    },
    "⚙️ Config & Admin": {
        "emoji": "⚙️",
        "color": 0x95a5a6,
        "description": "Server configuration — Admin only.",
        "fields": [
            ("🔧 Mod Bot Config", (
                "`.setlogchannel #ch` — log channel\n"
                "`.setmodrole @role` — mod role\n"
                "`.setwelcome #ch` — welcome channel\n"
                "`.setautorole @role` — auto-role on join\n"
                "`.setprefix <prefix>` — change prefix\n"
                "`.setstatus <type> <msg>` — bot status"
            ), False),
            ("🛒 Shop Config", (
                "`.setshopadminrole @role`\n"
                "`.setorderchannel #ch`\n"
                "`.setfeedbackchannel #ch`\n"
                "`.orderbanner <url>`\n"
                "`.feedbackbanner <url>`"
            ), False),
        ],
    },
}


def _build_help_embed(category_name: str) -> discord.Embed:
    cat = HELP_CATEGORIES[category_name]
    e = discord.Embed(
        title=f"{category_name} Commands",
        description=cat["description"],
        color=cat["color"],
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    for name, value, inline in cat["fields"]:
        e.add_field(name=name, value=value, inline=inline)
    e.set_footer(text="Use the dropdown to switch categories • Prefix: . or !")
    return e


class HelpDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=name, emoji=data["emoji"], description=f"View {name} commands")
            for name, data in HELP_CATEGORIES.items()
        ]
        super().__init__(
            placeholder="📂 Select a category...",
            min_values=1, max_values=1,
            options=options,
            custom_id="help_dropdown"
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        embed = _build_help_embed(selected)
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(HelpDropdown())

    async def on_timeout(self):
        # Disable dropdown on timeout
        for item in self.children:
            item.disabled = True


@bot.command(name="help")
async def help_cmd(ctx):
    embed = _build_help_embed("🏠 Overview")
    await ctx.send(embed=embed, view=HelpView())


@bot.tree.command(name="help", description="Show all bot commands")
async def slash_help(inter: discord.Interaction):
    embed = _build_help_embed("🏠 Overview")
    await inter.response.send_message(embed=embed, view=HelpView(), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — MODERATOR BOT
# ══════════════════════════════════════════════════════════════════════════════

def mod_only(inter: discord.Interaction) -> bool:
    return is_mod_check(inter.user, inter.guild_id)

def admin_only(inter: discord.Interaction) -> bool:
    return inter.user.guild_permissions.administrator


@bot.tree.command(name="ban", description="Ban a member from the server (mod bot — no rate limit)")
@app_commands.describe(member="The member to ban", reason="Reason for the ban")
async def slash_modban(inter: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _ban(inter, member, reason)

@bot.tree.command(name="unban", description="Unban a user by their ID")
@app_commands.describe(user_id="The user ID to unban", reason="Reason for the unban")
async def slash_unban(inter: discord.Interaction, user_id: str, reason: str = "No reason provided"):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    try:
        uid = int(user_id)
    except ValueError:
        return await inter.response.send_message("❌ Invalid user ID.", ephemeral=True)
    await _unban(inter, uid, reason)

@bot.tree.command(name="kick", description="Kick a member from the server (mod bot — no rate limit)")
@app_commands.describe(member="The member to kick", reason="Reason for the kick")
async def slash_modkick(inter: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _kick(inter, member, reason)

@bot.tree.command(name="mute", description="Timeout (mute) a member")
@app_commands.describe(member="The member to mute", duration="Duration in minutes", reason="Reason")
async def slash_mute(inter: discord.Interaction, member: discord.Member, duration: int = 10, reason: str = "No reason provided"):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _mute(inter, member, duration, reason)

@bot.tree.command(name="unmute", description="Remove timeout from a member")
@app_commands.describe(member="The member to unmute")
async def slash_unmute(inter: discord.Interaction, member: discord.Member):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _unmute(inter, member)

@bot.tree.command(name="warn", description="Warn a member (mod bot — no rate limit)")
@app_commands.describe(member="The member to warn", reason="Reason for the warning")
async def slash_modwarn(inter: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _warn(inter, member, reason)

@bot.tree.command(name="softban", description="Ban then unban to clear messages")
@app_commands.describe(member="The member to softban", reason="Reason")
async def slash_softban(inter: discord.Interaction, member: discord.Member, reason: str = "Softban"):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _softban(inter, member, reason)

@bot.tree.command(name="warnings", description="View mod-bot warnings for a member")
@app_commands.describe(member="The member to check")
async def slash_modwarnings(inter: discord.Interaction, member: discord.Member):
    await _warnings(inter, member)

@bot.tree.command(name="modlogs", description="View moderation history for a member")
@app_commands.describe(member="The member to check")
async def slash_modlogs(inter: discord.Interaction, member: discord.Member):
    await _modlogs(inter, member)

@bot.tree.command(name="userinfo", description="View info about a member")
@app_commands.describe(member="The member to look up (leave blank for yourself)")
async def slash_userinfo(inter: discord.Interaction, member: discord.Member = None):
    await _userinfo(inter, member or inter.user)

@bot.tree.command(name="serverinfo", description="View server information")
async def slash_serverinfo(inter: discord.Interaction):
    await _serverinfo(inter)

@bot.tree.command(name="clear", description="Delete messages in bulk")
@app_commands.describe(amount="Number of messages to delete")
async def slash_clear(inter: discord.Interaction, amount: int = 10):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _clear(inter, amount)

@bot.tree.command(name="purge", description="Delete messages from a specific user")
@app_commands.describe(member="The member whose messages to delete", amount="Max messages to scan")
async def slash_purge(inter: discord.Interaction, member: discord.Member, amount: int = 20):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    deleted = await inter.channel.purge(limit=200, check=lambda m: m.author == member)
    await inter.followup.send(f"🧹 Deleted {len(deleted)} messages from {member.mention}.", ephemeral=True)

@bot.tree.command(name="purgebot", description="Delete bot messages")
async def slash_purgebot(inter: discord.Interaction):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    deleted = await inter.channel.purge(limit=200, check=lambda m: m.author.bot)
    await inter.followup.send(f"🧹 Deleted {len(deleted)} bot messages.", ephemeral=True)

@bot.tree.command(name="slowmode", description="Set channel slowmode")
@app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)")
async def slash_slowmode(inter: discord.Interaction, seconds: int = 0):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _slowmode(inter, seconds)

@bot.tree.command(name="lock", description="Lock a channel")
@app_commands.describe(channel="Channel to lock (defaults to current)")
async def slash_lock(inter: discord.Interaction, channel: discord.TextChannel = None):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _lock(inter, channel)

@bot.tree.command(name="unlock", description="Unlock a channel")
@app_commands.describe(channel="Channel to unlock (defaults to current)")
async def slash_unlock(inter: discord.Interaction, channel: discord.TextChannel = None):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _unlock(inter, channel)

@bot.tree.command(name="lockdown", description="Lock ALL channels in the server")
async def slash_lockdown(inter: discord.Interaction):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await inter.response.defer()
    for ch in inter.guild.text_channels:
        try:
            ow = ch.overwrites_for(inter.guild.default_role)
            ow.send_messages = False
            await ch.set_permissions(inter.guild.default_role, overwrite=ow)
        except Exception:
            pass
    await inter.followup.send("🔒 **Server lockdown activated.**")

@bot.tree.command(name="hide", description="Hide a channel from regular users")
@app_commands.describe(channel="Channel to hide (defaults to current)")
async def slash_hide(inter: discord.Interaction, channel: discord.TextChannel = None):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _hide(inter, channel)

@bot.tree.command(name="unhide", description="Make a channel visible again")
@app_commands.describe(channel="Channel to unhide (defaults to current)")
async def slash_unhide(inter: discord.Interaction, channel: discord.TextChannel = None):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _unhide(inter, channel)

@bot.tree.command(name="nuke", description="Clone and delete this channel (wipes all messages)")
async def slash_nuke(inter: discord.Interaction):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await inter.response.defer()
    ch = inter.channel
    new_ch = await ch.clone(reason=f"Nuked by {inter.user}")
    await ch.delete()
    await new_ch.send("💥 Channel has been nuked!")

@bot.tree.command(name="channelcreate", description="Create a text channel")
@app_commands.describe(name="Channel name", category_id="Category ID (optional)")
async def slash_channelcreate(inter: discord.Interaction, name: str, category_id: str = None):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    cat_id = None
    if category_id:
        try:
            cat_id = int(category_id)
        except ValueError:
            return await inter.response.send_message("❌ Category ID must be a number.", ephemeral=True)
    await _channelcreate(inter, name, cat_id)

@bot.tree.command(name="deletechannel", description="Delete a channel")
@app_commands.describe(channel="Channel to delete (defaults to current)")
async def slash_deletechannel(inter: discord.Interaction, channel: discord.TextChannel = None):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _deletechannel(inter, channel)

@bot.tree.command(name="rolecreate", description="Create a role with optional colour")
@app_commands.describe(name="Role name", colour="Hex colour (#ff0000) or name (red, blue, gold...)")
async def slash_rolecreate(inter: discord.Interaction, name: str, colour: str = None):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _rolecreate(inter, name, colour)

@bot.tree.command(name="filter_add", description="Add a word to the filter list")
@app_commands.describe(word="Word to filter")
async def slash_filter_add(inter: discord.Interaction, word: str):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    config_col.update_one({"guild_id": inter.guild_id}, {"$addToSet": {"filtered_words": word.lower()}}, upsert=True)
    await inter.response.send_message(f"✅ Added `{word.lower()}` to word filter.")

@bot.tree.command(name="filter_remove", description="Remove a word from the filter list")
@app_commands.describe(word="Word to remove")
async def slash_filter_remove(inter: discord.Interaction, word: str):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    config_col.update_one({"guild_id": inter.guild_id}, {"$pull": {"filtered_words": word.lower()}})
    await inter.response.send_message(f"✅ Removed `{word.lower()}` from word filter.")

@bot.tree.command(name="filter_list", description="List all filtered words")
async def slash_filter_list(inter: discord.Interaction):
    cfg = get_config(inter.guild_id)
    words = cfg.get("filtered_words", [])
    msg = f"🚫 Filtered words: `{'`, `'.join(words)}`" if words else "No filtered words set."
    await inter.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="antiinvite", description="Toggle anti-invite link filter")
@app_commands.describe(state="Enable or disable")
@app_commands.choices(state=[app_commands.Choice(name="Enable", value="enable"), app_commands.Choice(name="Disable", value="disable")])
async def slash_antiinvite(inter: discord.Interaction, state: str = "enable"):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    enabled = state == "enable"
    set_config(inter.guild_id, "antiinvite", enabled)
    await inter.response.send_message(f"✅ Anti-invite {'**enabled**' if enabled else '**disabled**'}.")

@bot.tree.command(name="role_add", description="Give a role to a member")
@app_commands.describe(member="The member", role="The role to give")
async def slash_role_add(inter: discord.Interaction, member: discord.Member, role: discord.Role):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _role_add(inter, member, role)

@bot.tree.command(name="role_remove", description="Remove a role from a member")
@app_commands.describe(member="The member", role="The role to remove")
async def slash_role_remove(inter: discord.Interaction, member: discord.Member, role: discord.Role):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _role_remove(inter, member, role)

@bot.tree.command(name="setlogchannel", description="Set the mod log channel (Admin only)")
@app_commands.describe(channel="The channel to send logs to")
async def slash_setlogchannel(inter: discord.Interaction, channel: discord.TextChannel):
    if not admin_only(inter):
        return await inter.response.send_message("❌ Admins only.", ephemeral=True)
    set_config(inter.guild_id, "log_channel", channel.id)
    await inter.response.send_message(f"✅ Log channel → {channel.mention}")

@bot.tree.command(name="setmodrole", description="Set the moderator role (Admin only)")
@app_commands.describe(role="The mod role")
async def slash_setmodrole(inter: discord.Interaction, role: discord.Role):
    if not admin_only(inter):
        return await inter.response.send_message("❌ Admins only.", ephemeral=True)
    set_config(inter.guild_id, "mod_role", role.id)
    await inter.response.send_message(f"✅ Mod role → {role.mention}")

@bot.tree.command(name="setwelcome", description="Set the welcome message channel (Admin only)")
@app_commands.describe(channel="The welcome channel")
async def slash_setwelcome(inter: discord.Interaction, channel: discord.TextChannel):
    if not admin_only(inter):
        return await inter.response.send_message("❌ Admins only.", ephemeral=True)
    set_config(inter.guild_id, "welcome_channel", channel.id)
    await inter.response.send_message(f"✅ Welcome channel → {channel.mention}")

@bot.tree.command(name="setautorole", description="Set the auto-role for new members (Admin only)")
@app_commands.describe(role="The role to auto-assign")
async def slash_setautorole(inter: discord.Interaction, role: discord.Role):
    if not admin_only(inter):
        return await inter.response.send_message("❌ Admins only.", ephemeral=True)
    set_config(inter.guild_id, "autorole", role.id)
    await inter.response.send_message(f"✅ Auto-role → {role.mention}")

@bot.tree.command(name="latency", description="Check bot latency")
async def slash_ping(inter: discord.Interaction):
    await inter.response.send_message(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")

@bot.tree.command(name="announce", description="Send an announcement to a channel")
@app_commands.describe(channel="Target channel", message="The message to send")
async def slash_announce(inter: discord.Interaction, channel: discord.TextChannel, message: str):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _announce(inter, channel, message)

@bot.tree.command(name="poll", description="Create a yes/no poll")
@app_commands.describe(question="The poll question")
async def slash_poll(inter: discord.Interaction, question: str):
    await _poll(inter, question)

@bot.tree.command(name="report", description="Report a user to the moderators")
@app_commands.describe(member="The member to report", reason="Reason for report")
async def slash_report(inter: discord.Interaction, member: discord.Member, reason: str):
    await _report(inter, member, reason)

@bot.tree.command(name="autoresponder", description="Set an autoresponder trigger and reply")
@app_commands.describe(trigger="The trigger word/phrase", message="The response message")
async def slash_autoresponder(inter: discord.Interaction, trigger: str, message: str):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _autoresponder(inter, trigger, message)

@bot.tree.command(name="autoresponder_remove", description="Remove an autoresponder by trigger")
@app_commands.describe(trigger="The trigger to remove")
async def slash_autoresponder_remove(inter: discord.Interaction, trigger: str):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _autoresponder_remove(inter, trigger)

# @bot.tree.command(name="autoresponder_list", description="List all autoresponders in this server")  # de-registered: over Discord's 100 global slash command limit; .autoresponder_list prefix command still works
async def slash_autoresponder_list(inter: discord.Interaction):
    await _autoresponder_list(inter)

@bot.tree.command(name="autopurge", description="Auto-delete all messages in a channel on a repeating schedule")
@app_commands.describe(channel="The channel to autopurge", timer="Interval, e.g. 10m, 2h, 1d")
async def slash_autopurge(inter: discord.Interaction, channel: discord.TextChannel, timer: str):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _autopurge(inter, channel, timer)

@bot.tree.command(name="autopurge_remove", description="Stop autopurge for a channel")
@app_commands.describe(channel="The channel to stop autopurging")
async def slash_autopurge_remove(inter: discord.Interaction, channel: discord.TextChannel):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _autopurge_remove(inter, channel)

# @bot.tree.command(name="autopurge_list", description="List all autopurge schedules in this server")  # de-registered: over Discord's 100 global slash command limit; .autopurge_list prefix command still works
async def slash_autopurge_list(inter: discord.Interaction):
    await _autopurge_list(inter)

@bot.tree.command(name="stickymessage", description="Set a sticky message in a channel")
@app_commands.describe(channel="Target channel", message="The sticky message text")
async def slash_stickymessage(inter: discord.Interaction, channel: discord.TextChannel, message: str):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _stickymessage(inter, channel, message)

@bot.tree.command(name="stickyremove", description="Remove the sticky message from a channel")
@app_commands.describe(channel="The channel to remove sticky from")
async def slash_stickyremove(inter: discord.Interaction, channel: discord.TextChannel):
    if not mod_only(inter):
        return await inter.response.send_message("❌ You don't have permission.", ephemeral=True)
    await _stickyremove(inter, channel)

@bot.tree.command(name="setstatus", description="Set the bot's status (Admin only)")
@app_commands.describe(message="The status message")
@app_commands.choices(status_type=[
    app_commands.Choice(name="Playing", value="playing"),
    app_commands.Choice(name="Watching", value="watching"),
    app_commands.Choice(name="Listening", value="listening"),
    app_commands.Choice(name="Competing", value="competing"),
])
async def slash_setstatus(inter: discord.Interaction, status_type: str, message: str):
    if not admin_only(inter):
        return await inter.response.send_message("❌ Admins only.", ephemeral=True)
    await _setstatus(inter, status_type, message=message)

@bot.tree.command(name="dmannounce", description="Send a DM to every member in the server (Admin only)")
@app_commands.describe(text="The message to send to all members")
async def slash_dmannounce(inter: discord.Interaction, text: str):
    if not admin_only(inter):
        return await inter.response.send_message("❌ Admins only.", ephemeral=True)
    await _dmannounce(inter, text)

@bot.tree.command(name="dmdelete", description="Delete a DM announcement by ID (Admin only)")
@app_commands.describe(announce_id="The announcement ID shown after /dmannounce")
async def slash_dmdelete(inter: discord.Interaction, announce_id: str):
    if not admin_only(inter):
        return await inter.response.send_message("❌ Admins only.", ephemeral=True)
    await _dmdelete(inter, announce_id)


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — TICKET BOT
# ══════════════════════════════════════════════════════════════════════════════

class _PanelPostError(Exception):
    """Raised by the _post_*_panel helpers for expected config errors — the
    message is shown straight to the admin instead of a generic traceback."""
    pass


async def _post_main_panel(channel):
    embed = discord.Embed(
        description=(
            "• To buy something → 1st option\n"
            "• Need support/help → 2nd option\n\n"
            "**Rules:**\n"
            "• No selling or promotions here\n"
            "• No random DMs for deals\n"
            "• Making tickets for fun = Ban"),
        color=discord.Color.purple())
    embed.set_author(name=SHOP_NAME, icon_url=PANEL_LOGO_URL)
    embed.set_thumbnail(url=PANEL_LOGO_URL)
    embed.set_image(url=PANEL_BANNER_URL)
    embed.set_footer(text="Opening a ticket = automatically accepting our ToS")
    msg = await channel.send(embed=embed, view=PersistentPanelSelectView())
    panels_col.update_one({"message_id": msg.id},
        {"$set": {"channel_id": channel.id, "message_id": msg.id, "panel_type": "select"}}, upsert=True)
    return msg


async def _post_supp_panel(channel):
    embed = make_embed(
        title="🛠️ Manual Verification",
        description=(
            "Can't verify? Open a ticket and upload proof showing the verification error.\n\n"
            "⚠️ Do not open tickets for fun.\n"
            "⚠️ Do not ping staff after opening a ticket.\n\n"
            "We will review your ticket as soon as possible.\n\n"
            "❌ If you don't want to verify, you may leave the server."),
        color=discord.Color.green(),
        footer="Only you and our team can see your ticket.")
    msg = await channel.send(embed=embed, view=PersistentSupportView())
    panels_col.update_one({"message_id": msg.id},
        {"$set": {"channel_id": channel.id, "message_id": msg.id, "panel_type": "support"}}, upsert=True)
    return msg


async def _post_verify_panel(channel):
    url1 = _get_verify_url("VERIFY_1_URL")
    url2 = _get_verify_url("VERIFY_2_URL")
    if not url1 or not url2:
        missing = []
        if not url1: missing.append("`VERIFY_1_URL`")
        if not url2: missing.append("`VERIFY_2_URL`")
        raise _PanelPostError(
            f"{', '.join(missing)} is missing or invalid (must start with `http://` or `https://`). "
            "Set it in your bot's secrets/env and restart the bot, then try `/panel` again.")
    embed = discord.Embed(description=VERIFY_EMBED_DESCRIPTION, color=discord.Color.blue())
    return await channel.send(embed=embed, view=VerifyView(url1, url2))


async def _post_tos_panel(channel, guild):
    entries = _get_tos_entries(guild.id)
    embed = _build_tos_embed(guild)
    view = TOSView(entries)
    msg = await channel.send(embed=embed, view=view)
    cfg = get_config(guild.id)
    panels = cfg.get(TOS_PANEL_KEY, [])
    panels.append({"channel_id": channel.id, "message_id": msg.id})
    set_config(guild.id, TOS_PANEL_KEY, panels)
    return msg


async def _post_customer_panel_from_picker(channel, guild):
    cfg = get_customer_config(guild.id)
    if not is_customer_config_complete(cfg):
        raise _PanelPostError(CUSTOMER_NOT_CONFIGURED_MSG)
    return await channel.send(view=CustomerPanelView())


PANEL_SELECT_OPTIONS = [
    discord.SelectOption(label="Buy / Support Panel", value="main", emoji="🛒",
                          description="Main ticket panel — Buy / Support dropdown"),
    discord.SelectOption(label="Support-Only Panel", value="supp", emoji="🎧",
                          description="Support ticket button only (manual verification)"),
    discord.SelectOption(label="Verification Panel", value="verify", emoji="🔐",
                          description="Server access verification buttons"),
    discord.SelectOption(label="TOS Panel", value="tos", emoji="📜",
                          description="Interactive Terms of Service dropdown"),
    discord.SelectOption(label="Customer Verification Panel", value="customer", emoji="🧾",
                          description="Invoice / order verification panel"),
]
PANEL_SELECT_LABELS = {opt.value: opt.label for opt in PANEL_SELECT_OPTIONS}


class PanelPickerView(ui.View):
    """Ephemeral dropdown shown by /panel — lets an admin pick which of the
    5 panels to post in the current channel, replacing 5 separate slash
    commands with one. Only the admin who ran /panel can use their own menu."""
    def __init__(self, invoker_id: int):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id

    @ui.select(placeholder="Choose a panel to post…", options=PANEL_SELECT_OPTIONS,
               custom_id="panel_picker_select")
    async def pick(self, interaction: discord.Interaction, select: ui.Select):
        if interaction.user.id != self.invoker_id:
            return await interaction.response.send_message(
                "❌ Only the admin who ran `/panel` can use this menu.", ephemeral=True)

        choice = select.values[0]
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            if choice == "main":
                await _post_main_panel(interaction.channel)
            elif choice == "supp":
                await _post_supp_panel(interaction.channel)
            elif choice == "verify":
                await _post_verify_panel(interaction.channel)
            elif choice == "tos":
                await _post_tos_panel(interaction.channel, interaction.guild)
            elif choice == "customer":
                await _post_customer_panel_from_picker(interaction.channel, interaction.guild)
        except _PanelPostError as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        except Exception as e:
            return await interaction.followup.send(f"❌ Failed to post panel: {e}", ephemeral=True)

        await interaction.followup.send(
            f"✅ Posted the **{PANEL_SELECT_LABELS[choice]}** in {interaction.channel.mention}.", ephemeral=True)
        for item in self.children:
            item.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            pass
        self.stop()


@bot.tree.command(name="panel", description="Admin: pick a panel to post from a dropdown (replaces 5 separate commands)")
async def slash_panel(inter: discord.Interaction):
    if inter.guild is None or not isinstance(inter.user, discord.Member) or not inter.user.guild_permissions.administrator:
        return await inter.response.send_message("❌ Only administrators can use `/panel`.", ephemeral=True)
    await inter.response.send_message(
        "Choose which panel to post in this channel:",
        view=PanelPickerView(inter.user.id), ephemeral=True)

# NOTE: /supppanel, /verify, /tos and /customerpanel slash commands were
# merged into this single /panel dropdown to free up global command slots.
# The 5 prefix commands (.panel, .supppanel, .verify, .tos, .customerpanel)
# are untouched and still work exactly as before, independently of /panel.

@bot.tree.command(name="ltc", description="Set your LTC address for payments (Dealer only)")
@app_commands.describe(address="Your LTC wallet address")
async def slash_ltc(inter: discord.Interaction, address: str):
    if not is_dealer(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only dealers can set an LTC address.", discord.Color.red()), ephemeral=True)
    ltc_col.update_one({"dealer_id": inter.user.id}, {"$set": {"address": address}}, upsert=True)
    await inter.response.send_message(embed=make_embed("✅ LTC Address Saved", "Your LTC address has been updated.",
        discord.Color.green(), fields=[("Address", address, False)], footer="This address will be shown to buyers during deals."), ephemeral=True)

@bot.tree.command(name="upi", description="Set your UPI ID and QR code image URL (Dealer only)")
@app_commands.describe(upi_id="Your UPI payment ID", image_url="URL of your UPI QR code image")
async def slash_upi(inter: discord.Interaction, upi_id: str, image_url: str):
    if not is_dealer(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only dealers can set a UPI ID.", discord.Color.red()), ephemeral=True)
    upi_col.update_one({"dealer_id": inter.user.id}, {"$set": {"upi_id": upi_id, "image_url": image_url}}, upsert=True)
    await inter.response.send_message(embed=make_embed("✅ UPI Details Saved", "Your UPI payment details have been updated.",
        discord.Color.green(),
        fields=[("UPI ID", f"`{upi_id}`", False), ("QR Image URL", image_url, False)],
        footer="Buyers will see UPI as a payment option in your deals."), ephemeral=True)

@bot.tree.command(name="deal", description="Create a deal proposal for the ticket buyer (Dealer only)")
@app_commands.describe(product="Product name", amount="Price/amount e.g. 0.005 LTC or 5$")
async def slash_deal(inter: discord.Interaction, product: str, amount: str):
    if not is_dealer(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only dealers can use /deal.", discord.Color.red()), ephemeral=True)
    ticket = tickets_col.find_one({"channel_id": inter.channel.id})
    if not ticket:
        return await inter.response.send_message(embed=make_embed("❌ No Ticket Found", "Could not detect ticket owner. Ensure this is a ticket channel.", discord.Color.red()), ephemeral=True)
    buyer = inter.guild.get_member(ticket["owner_id"])
    if not buyer:
        return await inter.response.send_message(embed=make_embed("❌ Buyer Not Found", "The ticket owner could not be found in this server.", discord.Color.red()), ephemeral=True)
    existing_proposal = proposals_col.find_one({"channel_id": inter.channel.id})
    if existing_proposal:
        return await inter.response.send_message(embed=make_embed("⚠️ Proposal Already Pending",
            f"There is already a pending deal for **{existing_proposal['product']}** at **{existing_proposal['amount']}**.", discord.Color.orange()), ephemeral=True)
    proposals_col.update_one({"channel_id": inter.channel.id}, {"$set": {
        "channel_id": inter.channel.id, "buyer_id": buyer.id, "dealer_id": inter.user.id,
        "product": product, "amount": amount}}, upsert=True)
    preview_embed = make_embed(
        title="🤝 Deal Proposal",
        description=f"<@{buyer.id}>, please review the deal details below and confirm if everything is correct.",
        color=discord.Color.orange(),
        fields=[("👤 Buyer", f"<@{buyer.id}>", True), ("🧑‍💼 Dealer", f"<@{inter.user.id}>", True),
                ("📦 Product", product, False), ("💰 Amount", amount, False)],
        footer="Only the buyer can confirm or cancel this deal.")
    await inter.response.send_message(embed=make_embed("✅ Deal proposal posted.", "", discord.Color.green()), ephemeral=True)
    await inter.channel.send(content=f"<@{buyer.id}>", embed=preview_embed, view=DealConfirmView())

@bot.tree.command(name="dealdone", description="Mark the current deal as done — delivery complete (Dealer only)")
async def slash_dealdone(inter: discord.Interaction):
    if not is_dealer(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only dealers can use /dealdone.", discord.Color.red()), ephemeral=True)
    deal_doc = active_deals.get(inter.channel.id) or deals_col.find_one({"channel_id": inter.channel.id})
    if not deal_doc:
        return await inter.response.send_message(embed=make_embed("❌ No Active Deal", "There is no active deal in this channel.", discord.Color.red()), ephemeral=True)
    delivery_embed = make_embed(
        title="📦 Delivery Confirmation Required",
        description=f"<@{deal_doc['buyer']}>, please confirm you received your item.",
        color=discord.Color.blurple(),
        fields=[("📦 Product", deal_doc["product"], True), ("💰 Amount", deal_doc["amount"], True)],
        footer="Click 'Confirm Delivery' once you've received your product.")
    await inter.response.send_message(embed=make_embed("✅ Delivery confirmation sent to buyer.", "", discord.Color.green()), ephemeral=True)
    await inter.channel.send(content=f"<@{deal_doc['buyer']}>", embed=delivery_embed, view=PostDealConfirmView())

@bot.tree.command(name="close", description="Close this ticket")
async def slash_close(inter: discord.Interaction):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()), ephemeral=True)
    deal_doc = deals_col.find_one({"channel_id": inter.channel.id})
    if deal_doc and not is_head_dealer(inter.user):
        return await inter.response.send_message(embed=make_embed("⚠️ Active Deal",
            "Cannot close — there is an active deal in this ticket.\nOnly a Head Dealer can force close.", discord.Color.orange()), ephemeral=True)
    await inter.response.send_message(embed=make_embed("🔒 Close Ticket?", "Are you sure you want to close this ticket? The buyer will lose access.", discord.Color.orange()),
        view=CloseConfirmView(inter.user.id))

@bot.tree.command(name="reopen", description="Reopen a closed ticket")
async def slash_reopen(inter: discord.Interaction):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()), ephemeral=True)
    ticket = tickets_col.find_one({"channel_id": inter.channel.id})
    owner = inter.guild.get_member(ticket["owner_id"]) if ticket else None
    dealer_role = inter.guild.get_role(DEALER_ROLE_ID)
    tm_role = inter.guild.get_role(TICKET_MANAGER_ROLE_ID)
    overwrites = {inter.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                  dealer_role: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
    if tm_role:
        overwrites[tm_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    if owner:
        overwrites[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    await inter.channel.edit(overwrites=overwrites)
    await inter.response.send_message(embed=make_embed("🔓 Ticket Reopened", "Buyer access has been restored.", discord.Color.green()))

@bot.tree.command(name="rename", description="Rename this ticket channel")
@app_commands.describe(new_name="New name for the ticket (without prefix like ticket-)")
async def slash_rename(inter: discord.Interaction, new_name: str):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()), ephemeral=True)
    clean = new_name.strip().replace("[", "").replace("]", "").replace(" ", "-").lower()
    if inter.channel.name.startswith("support-"):
        new_channel_name = f"support-{clean}"
    elif inter.channel.name.startswith("buy-"):
        new_channel_name = f"buy-{clean}"
    else:
        new_channel_name = f"ticket-{clean}"
    await inter.channel.edit(name=new_channel_name)
    await inter.response.send_message(embed=make_embed("✏️ Ticket Renamed", f"Channel renamed to `{new_channel_name}`.", discord.Color.blurple()))

@bot.tree.command(name="add", description="Add a member to this ticket")
@app_commands.describe(member="The member to add to this ticket")
async def slash_add(inter: discord.Interaction, member: discord.Member):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()), ephemeral=True)
    await inter.channel.set_permissions(member, view_channel=True, send_messages=True)
    await inter.response.send_message(embed=make_embed("➕ Member Added", f"{member.mention} has been added to this ticket.", discord.Color.green()))

@bot.tree.command(name="remove", description="Remove a member from this ticket")
@app_commands.describe(member="The member to remove from this ticket")
async def slash_remove(inter: discord.Interaction, member: discord.Member):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()), ephemeral=True)
    await inter.channel.set_permissions(member, view_channel=False, send_messages=False)
    await inter.response.send_message(embed=make_embed("➖ Member Removed", f"{member.mention} has been removed from this ticket.", discord.Color.orange()))

@bot.tree.command(name="delete", description="Permanently delete this ticket channel")
async def slash_delete(inter: discord.Interaction):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()), ephemeral=True)
    deal_doc = deals_col.find_one({"channel_id": inter.channel.id})
    if deal_doc and not is_head_dealer(inter.user):
        return await inter.response.send_message(embed=make_embed("⚠️ Active Deal",
            "Cannot delete ticket — there is an active deal.\nOnly a Head Dealer can force delete.", discord.Color.orange()), ephemeral=True)
    await inter.response.send_message(embed=make_embed("🗑️ Deleting Ticket", "This ticket will be **permanently deleted** in 3 seconds...", discord.Color.red()))
    await asyncio.sleep(3)
    tickets_col.delete_one({"channel_id": inter.channel.id})
    deals_col.delete_one({"channel_id": inter.channel.id})
    panels_col.delete_one({"channel_id": inter.channel.id})
    active_deals.pop(inter.channel.id, None)
    await inter.channel.delete()

@bot.tree.command(name="dispute", description="Raise a dispute in this ticket")
@app_commands.describe(reason="Reason for the dispute")
async def slash_dispute(inter: discord.Interaction, reason: str = "No reason provided."):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    ticket = tickets_col.find_one({"channel_id": inter.channel.id})
    deal_doc = deals_col.find_one({"channel_id": inter.channel.id})
    is_buyer = ticket and inter.user.id == ticket.get("owner_id")
    if not is_buyer and not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only the buyer or a dealer can raise a dispute.", discord.Color.red()), ephemeral=True)
    head_dealer_role = inter.guild.get_role(HEAD_DEALER_ROLE_ID)
    dispute_embed = make_embed(
        title="⚠️ Dispute Raised",
        description=f"A dispute has been raised in this ticket by {inter.user.mention}.",
        color=discord.Color.red(),
        fields=[("👤 Raised By", inter.user.mention, True),
                ("📦 Product", deal_doc["product"] if deal_doc else "N/A", True),
                ("💰 Amount", deal_doc["amount"] if deal_doc else "N/A", True),
                ("📝 Reason", reason, False), ("📌 Channel", inter.channel.mention, False)],
        footer="A Head Dealer will review this shortly.")
    await inter.response.send_message(content=head_dealer_role.mention if head_dealer_role else "", embed=dispute_embed)
    log_ch = bot.get_channel(TRANSCRIPT_CHANNEL_ID)
    if log_ch:
        await log_ch.send(embed=dispute_embed)

@bot.tree.command(name="tickettransfer", description="Transfer this ticket to another dealer")
@app_commands.describe(new_dealer="The dealer to transfer this ticket to")
async def slash_tickettransfer(inter: discord.Interaction, new_dealer: discord.Member):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(new_dealer):
        return await inter.response.send_message(embed=make_embed("❌ Invalid Target", f"{new_dealer.mention} is not a dealer or ticket manager.", discord.Color.red()), ephemeral=True)
    if new_dealer.id == inter.user.id:
        return await inter.response.send_message(embed=make_embed("❌ Invalid", "You can't transfer a ticket to yourself.", discord.Color.red()), ephemeral=True)
    await inter.channel.set_permissions(new_dealer, view_channel=True, send_messages=True)
    await inter.response.send_message(embed=make_embed("🔁 Ticket Transferred",
        f"This ticket has been transferred from {inter.user.mention} to {new_dealer.mention}.",
        discord.Color.blurple(), fields=[("📌 New Handler", new_dealer.mention, False)], footer="The new dealer has been notified."))
    try:
        await new_dealer.send(embed=make_embed("📨 Ticket Transferred To You",
            f"{inter.user.mention} has transferred a ticket to you.", discord.Color.blurple(),
            fields=[("📌 Channel", inter.channel.mention, False), ("🏠 Server", inter.guild.name, False)],
            footer="Head over to the ticket channel to assist."))
    except discord.Forbidden:
        pass

@bot.tree.command(name="remind", description="Set a reminder in this ticket channel")
@app_commands.describe(minutes="Minutes until the reminder (1–1440)", message="Reminder message text")
async def slash_remind(inter: discord.Interaction, minutes: int, message: str = "This is your reminder!"):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()), ephemeral=True)
    if minutes < 1 or minutes > 1440:
        return await inter.response.send_message(embed=make_embed("❌ Invalid Time", "Please provide a time between 1 and 1440 minutes.", discord.Color.red()), ephemeral=True)
    await inter.response.send_message(embed=make_embed("⏱️ Reminder Set", f"I'll remind you in **{minutes} minute(s)**.",
        discord.Color.blurple(), fields=[("📝 Message", message, False)], footer=f"Set by {inter.user.display_name}"))
    channel_id, author_id, author_name = inter.channel.id, inter.user.id, inter.user.display_name
    async def send_reminder():
        await asyncio.sleep(minutes * 60)
        ch = bot.get_channel(channel_id)
        if ch:
            await ch.send(content=f"<@{author_id}>", embed=make_embed(
                "⏰ Reminder", message, discord.Color.gold(),
                footer=f"Reminder set {minutes} minute(s) ago by {author_name}"))
    asyncio.create_task(send_reminder())

@bot.tree.command(name="call", description="DM the ticket owner to return to this ticket")
@app_commands.describe(member="Member to call (leave blank for the ticket owner)")
async def slash_call(inter: discord.Interaction, member: discord.Member = None):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()), ephemeral=True)
    if member is None:
        ticket = tickets_col.find_one({"channel_id": inter.channel.id})
        if not ticket:
            return await inter.response.send_message(embed=make_embed("❌ No Ticket Data", "Could not find the ticket owner.", discord.Color.red()), ephemeral=True)
        member = inter.guild.get_member(ticket["owner_id"])
        if not member:
            return await inter.response.send_message(embed=make_embed("❌ User Not Found", "The ticket owner is no longer in the server.", discord.Color.red()), ephemeral=True)
    try:
        await member.send(embed=make_embed("📣 You're Being Called!", f"**{inter.user.display_name}** is calling you in your ticket.",
            discord.Color.orange(),
            fields=[("📌 Channel", inter.channel.mention, False), ("🏠 Server", inter.guild.name, False)],
            footer="Please head back to your ticket channel."))
        await inter.response.send_message(embed=make_embed("📣 Called!", f"{member.mention} has been DM'd to return to this ticket.", discord.Color.green()))
    except discord.Forbidden:
        await inter.response.send_message(embed=make_embed("⚠️ DM Failed", f"Could not DM {member.mention} — their DMs are closed. Pinging them here instead.", discord.Color.orange()))
        await inter.channel.send(content=f"Hey {member.mention}, you're needed in this ticket!")

@bot.tree.command(name="transcript", description="Generate and send the HTML transcript for this ticket")
async def slash_transcript(inter: discord.Interaction):
    if not (inter.channel.name.startswith("ticket-") or inter.channel.name.startswith("buy-") or inter.channel.name.startswith("support-")):
        return await inter.response.send_message(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()), ephemeral=True)
    if not can_manage_ticket(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()), ephemeral=True)
    await inter.response.defer()
    await inter.followup.send(embed=make_embed("📄 Generating Transcript...", "Please wait a moment.", discord.Color.blurple()))
    ticket = tickets_col.find_one({"channel_id": inter.channel.id})
    buyer_id = ticket["owner_id"] if ticket else None
    await send_html_transcript(inter.channel, buyer_id)
    await inter.followup.send(embed=make_embed("✅ Transcript Sent",
        "The transcript has been posted to the log channel" + (f" and DMed to <@{buyer_id}>." if buyer_id else "."),
        discord.Color.green()))

@bot.tree.command(name="proofsend", description="Post a proof of delivery image to the proof channel (Dealer only)")
@app_commands.describe(member="The buyer", amount="Amount paid", item="Item name", image="Proof image attachment")
async def slash_proofsend(inter: discord.Interaction, member: discord.Member, amount: str, item: str, image: discord.Attachment):
    if not is_dealer(inter.user):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only dealers can use /proofsend.", discord.Color.red()), ephemeral=True)
    if not image.content_type or not image.content_type.startswith("image/"):
        return await inter.response.send_message(embed=make_embed("❌ Invalid Attachment",
            "The attached file is not an image. Please attach a PNG, JPG, or similar image file.", discord.Color.red()), ephemeral=True)
    await inter.response.defer(ephemeral=True)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(image.url) as resp:
                img_bytes = await resp.read()
    except Exception:
        img_bytes = None
    permanent_url = await upload_to_imgbb(img_bytes, image.filename) if img_bytes else None
    final_url = permanent_url or image.url
    footer_text = "Proof Of Delivery"
    proof_ch = bot.get_channel(PROOF_CHANNEL_ID)
    if not proof_ch:
        return await inter.followup.send(embed=make_embed("❌ Proof Channel Not Found",
            "Could not find the proof channel. Check your PROOF_CHANNEL_ID config.", discord.Color.red()), ephemeral=True)
    await proof_ch.send(embed=make_embed(title=f"# Sold {item}",
        description=(f"**Dealer:** <@{inter.user.id}>\n**Buyer:** <@{member.id}>\n**Amount:** {amount}"),
        color=discord.Color.green(), image=final_url, footer=footer_text))
    await inter.followup.send(embed=make_embed("✅ Proof Posted", f"Proof for **{item}** has been posted to <#{PROOF_CHANNEL_ID}>.",
        discord.Color.green(),
        fields=[("👤 Buyer", member.mention, True), ("💰 Amount", amount, True), ("📦 Item", item, False)],
        footer=footer_text), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — SHOP & LTC
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="setshopadminrole", description="Set the shop admin role (Server Owner only)")
@app_commands.describe(role="The shop admin role")
async def slash_setshopadminrole(inter: discord.Interaction, role: discord.Role):
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("❌ Server owner only.", ephemeral=True)
    set_config(inter.guild_id, "shop_admin_role", role.id)
    await inter.response.send_message(f"<:tick:1546740263437738055> Shop admin role set to {role.mention}.")

@bot.tree.command(name="orderbanner", description="Set the order complete banner image URL")
@app_commands.describe(image_link="URL of the banner image")
async def slash_orderbanner(inter: discord.Interaction, image_link: str):
    if not is_shop_admin_check(inter.user, inter.guild_id):
        return await inter.response.send_message("❌ Shop admin role required.", ephemeral=True)
    set_config(inter.guild_id, "order_banner", image_link)
    await inter.response.send_message("<:tick:1546740263437738055> Order complete banner updated!")

@bot.tree.command(name="feedbackbanner", description="Set the feedback banner image URL")
@app_commands.describe(image_link="URL of the banner image")
async def slash_feedbackbanner(inter: discord.Interaction, image_link: str):
    if not is_shop_admin_check(inter.user, inter.guild_id):
        return await inter.response.send_message("❌ Shop admin role required.", ephemeral=True)
    set_config(inter.guild_id, "feedback_banner", image_link)
    await inter.response.send_message("<:tick:1546740263437738055> Feedback banner updated!")

@bot.tree.command(name="setfeedbackchannel", description="Set the feedback log channel")
@app_commands.describe(channel="The feedback log channel")
async def slash_setfeedbackchannel(inter: discord.Interaction, channel: discord.TextChannel):
    if not is_shop_admin_check(inter.user, inter.guild_id):
        return await inter.response.send_message("❌ Shop admin role required.", ephemeral=True)
    set_config(inter.guild_id, "feedback_channel", channel.id)
    await inter.response.send_message(f"<:tick:1546740263437738055> Feedback log channel set to {channel.mention}")

@bot.tree.command(name="setorderchannel", description="Set the order complete channel")
@app_commands.describe(channel="The order complete channel")
async def slash_setorderchannel(inter: discord.Interaction, channel: discord.TextChannel):
    if not is_shop_admin_check(inter.user, inter.guild_id):
        return await inter.response.send_message("❌ Shop admin role required.", ephemeral=True)
    set_config(inter.guild_id, "order_channel", channel.id)
    await inter.response.send_message(f"<:tick:1546740263437738055> Order complete channel set to {channel.mention}")

@bot.tree.command(name="changeorderno", description="Set the next order number")
@app_commands.describe(number="The next order number (must be 1 or higher)")
async def slash_changeorderno(inter: discord.Interaction, number: int):
    if not is_shop_admin_check(inter.user, inter.guild_id):
        return await inter.response.send_message("❌ Shop admin role required.", ephemeral=True)
    if number < 1:
        return await inter.response.send_message("❌ Order number must be 1 or higher.", ephemeral=True)
    config_col.update_one({"guild_id": inter.guild_id}, {"$set": {"order_counter": number - 1}}, upsert=True)
    await inter.response.send_message(f"✅ Order counter updated — next order will be **#{number}**")

@bot.tree.command(name="ordercomplete", description="Mark an order as complete and send buyer a feedback DM")
@app_commands.describe(product="Product name", amount="Price/amount e.g. 9.99$ or 0.005 LTC", user="The buyer")
async def slash_ordercomplete(inter: discord.Interaction, product: str, amount: str, user: discord.Member):
    if not is_shop_admin_check(inter.user, inter.guild_id):
        return await inter.response.send_message("❌ Shop admin role required.", ephemeral=True)
    await inter.response.defer()
    if _re.fullmatch(r'\d+(\.\d+)?', amount):
        amount = amount + "$"
    seller_username = inter.user.display_name
    seller_id = inter.user.id
    buyer_id = user.id
    buyer = user
    order_num = _next_order_number(inter.guild_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    orders_col.insert_one({
        "order_num": order_num, "guild_id": inter.guild_id,
        "seller_id": seller_id, "buyer_id": buyer_id,
        "product": product, "amount": amount, "timestamp": now
    })
    cfg = get_config(inter.guild_id)
    order_banner  = cfg.get("order_banner", "")
    fb_banner     = cfg.get("feedback_banner", "")
    fb_channel_id = cfg.get("feedback_channel", inter.channel.id)
    order_channel_id = cfg.get("order_channel")
    order_ch = (inter.guild.get_channel(int(order_channel_id)) or inter.channel) if order_channel_id else inter.channel
    order_embed = discord.Embed(title="<a:blueverify:1546740183716855872> Order Completed", color=0x5865F2, timestamp=now)
    order_embed.add_field(name=f"<:cart:1546740191773986977> Order Number: #{order_num}", value=(
        f"<:curvedarrow:1546740206164643933> <:Buyer:1546740161205899285> **Buyer:** {buyer.mention}\n"
        f"<:curvedarrow:1546740206164643933> <:blue_box:1546740176041283594> **Product:** {product}\n"
        f"<:curvedarrow:1546740206164643933> <:cash:1546740198870749224> **Price:** {amount}"), inline=False)
    if order_banner:
        order_embed.set_image(url=order_banner)
    order_embed.set_footer(text=f"Order Completed by {seller_username} • {inter.guild.name}")
    await order_ch.send(embed=order_embed)
    fb_embed = discord.Embed(title="<a:blueverify:1546740183716855872> New FeedBack Request", color=0x5865F2, timestamp=now)
    fb_embed.add_field(name="\u200b", value=(
        f"<:blue_box:1546740176041283594> **Product:** {product}\n"
        f"<:cash:1546740198870749224> **Price:** {amount}\n"
        f"<:sellerITS:1546740256706142318> **Seller:** {seller_username}"), inline=False)
    if fb_banner:
        fb_embed.set_image(url=fb_banner)
    if inter.guild.icon:
        fb_embed.set_thumbnail(url=inter.guild.icon.url)
    buyer_member = inter.guild.get_member(buyer_id)
    if buyer_member and BUYER_ROLE_ID:
        customer_role = inter.guild.get_role(BUYER_ROLE_ID)
        if customer_role and customer_role not in buyer_member.roles:
            try:
                await buyer_member.add_roles(customer_role, reason=f"Auto-assigned after order #{order_num}")
            except Exception:
                pass
    fb_view = FeedbackView(order_num=order_num, product=product, amount=amount,
        seller_name=seller_username, seller_id=seller_id,
        buyer_id=buyer_id, feedback_channel_id=fb_channel_id, guild_id=inter.guild_id)
    try:
        await buyer.send(embed=fb_embed, view=fb_view)
        await inter.followup.send(f"✅ Order #{order_num} completed. Feedback DM sent to {buyer.mention}.")
    except discord.Forbidden:
        await inter.followup.send(f"✅ Order #{order_num} completed. ⚠️ Could not DM {buyer.mention} — their DMs are closed.")

@bot.tree.command(name="addbal", description="Add balance to a user's account (Shop Admin only)")
@app_commands.describe(member="The member to add balance to", amount="Amount to add in USD")
async def slash_addbal(inter: discord.Interaction, member: discord.Member, amount: float):
    if not is_shop_admin_check(inter.user, inter.guild_id):
        return await inter.response.send_message("❌ Shop admin role required.", ephemeral=True)
    _add_balance(member.id, amount)
    new_bal = _get_balance(member.id)
    e = discord.Embed(color=0x57F287, description=(
        f"<:tick:1546740263437738055> Added `${amount:.2f}` to {member.mention}'s balance.\n"
        f"<:cash:1546740198870749224> **New Balance:** `${new_bal:.2f}`"))
    await inter.response.send_message(embed=e)

@bot.tree.command(name="setltc", description="Save your LTC wallet address")
@app_commands.describe(address="Your LTC wallet address")
async def slash_setltc(inter: discord.Interaction, address: str):
    ltc_wallets_col.update_one({"user_id": inter.user.id},
        {"$set": {"ltc_address": address, "updated_at": datetime.datetime.now(datetime.timezone.utc)}}, upsert=True)
    e = discord.Embed(title="<:tick:1546740263437738055> LTC Wallet Saved", color=0x5865F2,
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.add_field(name="\u200b", value=(
        f"<:Buyer:1546740161205899285> **User:** {inter.user.mention}\n"
        f"<:blue1:1546740168923553842> <:cash:1546740198870749224> **Address:** `{address}`"), inline=False)
    e.set_footer(text="LTC Wallet")
    await inter.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="tx", description="Record a TX and DM the wallet owner (Shop Admin only)")
@app_commands.describe(txid="The LTC transaction ID")
async def slash_tx(inter: discord.Interaction, txid: str):
    if not is_shop_admin_check(inter.user, inter.guild_id):
        return await inter.response.send_message("❌ Shop admin role required.", ephemeral=True)
    await inter.response.defer()
    await inter.followup.send(f"<:curvedarrow:1546740206164643933> Looking up TX `{txid}`…")
    data = await _fetch_ltc_tx(txid)
    if not data:
        return await inter.followup.send("❌ Could not fetch that transaction. Double-check the TXID or ensure `BLOCKCYPHER_TOKEN` is set.")
    confirmations = data.get("confirmations", 0)
    total_ltc = data.get("total", 0) / 1e8
    received_at = data.get("received", "Unknown")
    tx_embed = discord.Embed(title="<:cash:1546740198870749224> Transaction Recorded", color=0xA4C2F4,
                              timestamp=datetime.datetime.now(datetime.timezone.utc))
    tx_embed.add_field(name="\u200b", value=(
        f"<:blue1:1546740168923553842> **TXID:** `{txid}`\n"
        f"<:cash:1546740198870749224> **Amount:** `{total_ltc:.8f} LTC`\n"
        f"<:tick:1546740263437738055> **Confirmations:** `{confirmations}`\n"
        f"<:curvedarrow:1546740206164643933> **Received:** `{received_at}`"), inline=False)
    tx_embed.set_footer(text="BlockCypher • LTC")
    await inter.followup.send(embed=tx_embed)
    await _dm_tx_confirmation(bot, data, txid)

# @bot.tree.command(name="checktx", description="Check the status of an LTC transaction")  # de-registered: over Discord's 100 global slash command limit; .checktx prefix command still works
@app_commands.describe(txid="The LTC transaction ID")
async def slash_checktx(inter: discord.Interaction, txid: str):
    await inter.response.defer()
    await inter.followup.send(f"<:curvedarrow:1546740206164643933> Checking TX `{txid}`…")
    data = await _fetch_ltc_tx(txid)
    if not data:
        return await inter.followup.send("❌ Transaction not found. Check the TXID or try again later.")
    confirmations = data.get("confirmations", 0)
    total_ltc = data.get("total", 0) / 1e8
    received_at = data.get("received", "Unknown")
    confirmed = confirmations >= 1
    status_icon = "<:tick:1546740263437738055>" if confirmed else "<:blue1:1546740168923553842>"
    status_text = (f"**Confirmed** ({confirmations} confirmation{'s' if confirmations != 1 else ''})"
                   if confirmed else f"**Pending** ({confirmations} confirmations)")
    check_embed = discord.Embed(title=f"{status_icon} Transaction Status",
        color=0x57F287 if confirmed else 0xFEE75C, timestamp=datetime.datetime.now(datetime.timezone.utc))
    check_embed.add_field(name="\u200b", value=(
        f"<:blue1:1546740168923553842> **TXID:** `{txid}`\n"
        f"<:cash:1546740198870749224> **Amount:** `{total_ltc:.8f} LTC`\n"
        f"{status_icon} **Status:** {status_text}\n"
        f"<:curvedarrow:1546740206164643933> **Received:** `{received_at}`"), inline=False)
    check_embed.set_footer(text="BlockCypher • LTC")
    await inter.followup.send(embed=check_embed)
    if confirmed:
        await _dm_tx_confirmation(bot, data, txid)

@bot.tree.command(name="setprefix", description="Set a custom command prefix for this server (Admin only)")
@app_commands.describe(prefix="The new prefix character(s) e.g. ! or ?")
async def slash_setprefix(inter: discord.Interaction, prefix: str):
    if not inter.user.guild_permissions.administrator:
        return await inter.response.send_message("❌ Admins only.", ephemeral=True)
    set_config(inter.guild_id, "prefix", prefix)
    await inter.response.send_message(f"✅ Custom prefix `{prefix}` added for this server. `.` and `!` always still work.")

# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — GIVEAWAYS
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="gstart", description="Start a giveaway")
@app_commands.describe(duration="Duration e.g. 1h 30m 1d", winners="Number of winners (1–20)", prize="Giveaway prize description")
async def slash_gstart(inter: discord.Interaction, duration: str, winners: int, prize: str):
    if not mod_only(inter):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need mod permissions to start a giveaway.", discord.Color.red()), ephemeral=True)
    seconds = _parse_giveaway_duration(duration)
    if seconds is None or seconds < 10:
        return await inter.response.send_message(embed=make_embed("❌ Invalid Duration", "Minimum duration is `10s`. Use formats like `10m`, `2h`, `1d`.", discord.Color.red()), ephemeral=True)
    if winners < 1 or winners > 20:
        return await inter.response.send_message(embed=make_embed("❌ Invalid Winner Count", "Winners must be a number between **1** and **20**.", discord.Color.red()), ephemeral=True)
    ends_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    await inter.response.defer()
    embed = _giveaway_embed(prize=prize, winners=winners, host_id=inter.user.id, ends_at=ends_at)
    gw_msg = await inter.channel.send(content="<:gift:1546740220148449332> **New Giveaway** <:gift:1546740220148449332>", embed=embed)
    try:
        custom_emoji = discord.utils.get(bot.emojis, name=GIVEAWAY_EMOJI_RAW)
        if custom_emoji:
            await gw_msg.add_reaction(custom_emoji)
        else:
            await gw_msg.add_reaction("🎉")
    except Exception:
        await gw_msg.add_reaction("🎉")
    giveaways_col.insert_one({
        "guild_id": inter.guild_id, "channel_id": inter.channel.id, "message_id": gw_msg.id,
        "host_id": inter.user.id, "prize": prize, "num_winners": winners,
        "ends_at": ends_at, "ended": False, "winner_ids": [],
    })
    await inter.followup.send("✅ Giveaway started!", ephemeral=True)

@bot.tree.command(name="gend", description="End a giveaway immediately")
@app_commands.describe(message_id="The giveaway message ID (right-click message → Copy ID)")
async def slash_gend(inter: discord.Interaction, message_id: str):
    if not mod_only(inter):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need mod permissions to end a giveaway.", discord.Color.red()), ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        return await inter.response.send_message(embed=make_embed("❌ Invalid ID", "Message ID must be a number.", discord.Color.red()), ephemeral=True)
    doc = giveaways_col.find_one({"message_id": mid, "guild_id": inter.guild_id})
    if not doc:
        return await inter.response.send_message(embed=make_embed("❌ Not Found", f"No active giveaway found with message ID `{mid}`.", discord.Color.red()), ephemeral=True)
    if doc.get("ended"):
        return await inter.response.send_message(embed=make_embed("⚠️ Already Ended", "That giveaway has already ended. Use `/greroll` to pick new winners.", discord.Color.orange()), ephemeral=True)
    await inter.response.defer(ephemeral=True)
    await _end_giveaway(doc)
    await inter.followup.send("✅ Giveaway ended.", ephemeral=True)

@bot.tree.command(name="greroll", description="Re-roll winners for a finished giveaway")
@app_commands.describe(message_id="The giveaway message ID (right-click message → Copy ID)")
async def slash_greroll(inter: discord.Interaction, message_id: str):
    if not mod_only(inter):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need mod permissions to re-roll a giveaway.", discord.Color.red()), ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        return await inter.response.send_message(embed=make_embed("❌ Invalid ID", "Message ID must be a number.", discord.Color.red()), ephemeral=True)
    doc = giveaways_col.find_one({"message_id": mid, "guild_id": inter.guild_id})
    if not doc:
        return await inter.response.send_message(embed=make_embed("❌ Not Found", f"No giveaway found with message ID `{mid}`.", discord.Color.red()), ephemeral=True)
    channel = inter.guild.get_channel(doc["channel_id"])
    if not channel:
        return await inter.response.send_message(embed=make_embed("❌ Channel Not Found", "The giveaway channel no longer exists.", discord.Color.red()), ephemeral=True)
    await inter.response.defer()
    try:
        message = await channel.fetch_message(mid)
    except Exception:
        return await inter.followup.send(embed=make_embed("❌ Message Not Found", "Could not fetch the giveaway message. It may have been deleted.", discord.Color.red()))
    ends_at = doc["ends_at"]
    if isinstance(ends_at, (int, float)):
        ends_at = datetime.datetime.fromtimestamp(ends_at, tz=datetime.timezone.utc)
    elif ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=datetime.timezone.utc)
    winner_ids = await _pick_winners(message, doc["num_winners"], doc["host_id"])
    if not winner_ids:
        return await inter.followup.send(embed=discord.Embed(description="😔 No valid entries found — could not pick new winners.", color=discord.Color.red()))
    mentions = " ".join(f"<@{w}>" for w in winner_ids)
    reroll_embed = discord.Embed(
        title="🔄 Giveaway Re-rolled!",
        description=(f"New winner(s): {mentions}\n**Prize:** {doc['prize']}\n\n[Jump to Giveaway]({message.jump_url})"),
        color=discord.Color.from_rgb(0, 200, 220),
        timestamp=datetime.datetime.now(datetime.timezone.utc))
    reroll_embed.set_footer(text=f"Re-rolled by {inter.user.display_name}")
    await inter.channel.send(content=mentions, embed=reroll_embed)
    giveaways_col.update_one({"message_id": mid}, {"$set": {"winner_ids": winner_ids}})
    try:
        updated_embed = _giveaway_embed(prize=doc["prize"], winners=doc["num_winners"],
            host_id=doc["host_id"], ends_at=ends_at, ended=True, winner_ids=winner_ids)
        await message.edit(embed=updated_embed)
    except Exception:
        pass
    await inter.followup.send("✅ Re-rolled!", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — SCHEDULED MESSAGES
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="setscheuledmessage", description="Schedule a recurring message in this channel")
@app_commands.describe(time="Interval e.g. 10m 2h 1d", message="The message to send repeatedly")
async def slash_setscheuledmessage(inter: discord.Interaction, time: str, message: str):
    if not mod_only(inter):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need mod permissions to set a scheduled message.", discord.Color.red()), ephemeral=True)
    interval_seconds = _parse_schedule_interval(time)
    if interval_seconds is None:
        return await inter.response.send_message(embed=make_embed("❌ Invalid Time", "Use formats like `10m`, `2h`, or `1d`.", discord.Color.red()), ephemeral=True)
    name = f"sched-{uuid.uuid4().hex[:8]}"
    now = datetime.datetime.now(datetime.timezone.utc)
    scheduled_messages_col.insert_one({
        "name": name, "guild_id": inter.guild_id, "channel_id": inter.channel.id,
        "message": message, "interval_seconds": interval_seconds,
        "created_by": inter.user.id, "created_at": now, "next_run": now, "last_message_id": None,
    })
    await inter.response.send_message(embed=make_embed("✅ Scheduled Message Set",
        f"This message will be sent here every **{time}**, deleting the previous one first.",
        discord.Color.green(),
        fields=[("🆔 Name/ID", name, False), ("💬 Message", message[:1000], False)]), ephemeral=True)

@bot.tree.command(name="removeschedulemessage", description="Remove a scheduled message by name/ID")
@app_commands.describe(name="The scheduled message name/ID (leave blank to list all)")
async def slash_removeschedulemessage(inter: discord.Interaction, name: str = None):
    if not mod_only(inter):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need mod permissions.", discord.Color.red()), ephemeral=True)
    if not name:
        docs = list(scheduled_messages_col.find({"guild_id": inter.guild_id}))
        if not docs:
            return await inter.response.send_message(embed=make_embed("ℹ️ No Scheduled Messages", "No scheduled messages exist in this server.", discord.Color.blurple()), ephemeral=True)
        listing = "\n".join(f"`{d['name']}` — <#{d['channel_id']}> — {d['message'][:50]}" for d in docs)
        return await inter.response.send_message(embed=make_embed("📋 Scheduled Messages", listing, discord.Color.blurple()), ephemeral=True)
    name = name.strip()
    doc = scheduled_messages_col.find_one({"guild_id": inter.guild_id, "name": name})
    if not doc:
        return await inter.response.send_message(embed=make_embed("❌ Not Found", f"No scheduled message found with name/ID `{name}`.", discord.Color.red()), ephemeral=True)
    guild = bot.get_guild(doc["guild_id"])
    if guild:
        channel = guild.get_channel(doc["channel_id"])
        last_message_id = doc.get("last_message_id")
        if channel and last_message_id:
            try:
                old_msg = await channel.fetch_message(last_message_id)
                await old_msg.delete()
            except Exception:
                pass
    scheduled_messages_col.delete_one({"_id": doc["_id"]})
    await inter.response.send_message(embed=make_embed("🗑️ Scheduled Message Removed",
        f"Scheduled message `{name}` has been removed and will no longer be sent.", discord.Color.green()), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — EMOJI MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="addemoji", description="Add one or more custom emojis from other servers")
@app_commands.describe(emojis="Custom emoji mentions separated by spaces e.g. <:cool:123> <a:wave:456>")
async def slash_addemoji(inter: discord.Interaction, emojis: str):
    if not mod_only(inter):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need mod permissions to add emojis.", discord.Color.red()), ephemeral=True)
    if not inter.guild.me.guild_permissions.manage_emojis:
        return await inter.response.send_message(embed=make_embed("❌ Missing Permission", "I need the **Manage Emojis and Stickers** permission.", discord.Color.red()), ephemeral=True)
    emoji_inputs = emojis.split()
    await inter.response.defer()
    existing_names = [e.name for e in inter.guild.emojis]
    results_ok, results_err = [], []
    for raw in emoji_inputs:
        emoji_id, name, animated = _parse_emoji_input(raw)
        if emoji_id is None:
            results_err.append(f"`{raw}` — Could not parse as a custom emoji or ID.")
            continue
        display_name = name or f"emoji_{emoji_id}"
        img_bytes = await _download_emoji(emoji_id, animated)
        if not img_bytes:
            img_bytes = await _download_emoji(emoji_id, not animated)
            if img_bytes:
                animated = not animated
        if not img_bytes:
            results_err.append(f"`{display_name}` (`{emoji_id}`) — Failed to download from Discord CDN.")
            continue
        safe_name = _safe_emoji_name(display_name, existing_names)
        existing_names.append(safe_name)
        try:
            new_emoji = await inter.guild.create_custom_emoji(name=safe_name, image=img_bytes, reason=f"Added by {inter.user} via /addemoji")
            kind = "Animated" if new_emoji.animated else "Static"
            results_ok.append(f"{new_emoji} `:{new_emoji.name}:` — {kind}")
            emoji_log_col.insert_one({"guild_id": inter.guild_id, "emoji_id": new_emoji.id, "name": new_emoji.name,
                "source_id": emoji_id, "animated": new_emoji.animated, "added_by": inter.user.id,
                "added_at": datetime.datetime.now(datetime.timezone.utc)})
        except discord.HTTPException as e:
            if e.code == 30008: results_err.append(f"`{safe_name}` — Server emoji limit reached.")
            elif e.code == 50013: results_err.append(f"`{safe_name}` — Missing permission to create emojis.")
            else: results_err.append(f"`{safe_name}` — Discord error: `{e.text}`")
        except Exception as e:
            results_err.append(f"`{safe_name}` — Unexpected error: `{e}`")
    desc_parts = []
    if results_ok: desc_parts.append("**✅ Added:**\n" + "\n".join(results_ok))
    if results_err: desc_parts.append("**❌ Failed:**\n" + "\n".join(results_err))
    color = discord.Color.green() if results_ok and not results_err else (discord.Color.orange() if results_ok else discord.Color.red())
    title = f"✅ {len(results_ok)} Emoji(s) Added" if not results_err else f"⚠️ {len(results_ok)} Added, {len(results_err)} Failed"
    await inter.followup.send(embed=make_embed(title, "\n\n".join(desc_parts), color))

@bot.tree.command(name="removeemoji", description="Remove a custom emoji from this server")
@app_commands.describe(emoji="The emoji to remove — mention it or type its name")
async def slash_removeemoji(inter: discord.Interaction, emoji: str):
    if not mod_only(inter):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need mod permissions to remove emojis.", discord.Color.red()), ephemeral=True)
    if not inter.guild.me.guild_permissions.manage_emojis:
        return await inter.response.send_message(embed=make_embed("❌ Missing Permission", "I need the **Manage Emojis and Stickers** permission.", discord.Color.red()), ephemeral=True)
    emoji_id, _, _ = _parse_emoji_input(emoji.strip())
    target_emoji = None
    if emoji_id:
        target_emoji = discord.utils.get(inter.guild.emojis, id=emoji_id)
    if not target_emoji:
        target_emoji = discord.utils.find(lambda e: e.name.lower() == emoji.strip().lstrip(":").rstrip(":").lower(), inter.guild.emojis)
    if not target_emoji:
        return await inter.response.send_message(embed=make_embed("❌ Emoji Not Found", f"Could not find emoji `{emoji}` in this server.", discord.Color.red()), ephemeral=True)
    emoji_name = target_emoji.name
    try:
        await target_emoji.delete(reason=f"Removed by {inter.user} via /removeemoji")
    except discord.Forbidden:
        return await inter.response.send_message(embed=make_embed("❌ Permission Denied", "I can't delete that emoji.", discord.Color.red()), ephemeral=True)
    emoji_log_col.update_one({"guild_id": inter.guild_id, "emoji_id": target_emoji.id},
        {"$set": {"removed_by": inter.user.id, "removed_at": datetime.datetime.now(datetime.timezone.utc)}})
    await inter.response.send_message(embed=make_embed("🗑️ Emoji Removed", f"Emoji `:{emoji_name}:` has been removed from this server.", discord.Color.green()))

# @bot.tree.command(name="renameemoji", description="Rename a custom emoji in this server")  # de-registered: over Discord's 100 global slash command limit; .renameemoji prefix command still works
@app_commands.describe(emoji="The emoji to rename — mention it or type its name", new_name="New name for the emoji")
async def slash_renameemoji(inter: discord.Interaction, emoji: str, new_name: str):
    if not mod_only(inter):
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "You need mod permissions to rename emojis.", discord.Color.red()), ephemeral=True)
    if not inter.guild.me.guild_permissions.manage_emojis:
        return await inter.response.send_message(embed=make_embed("❌ Missing Permission", "I need the **Manage Emojis and Stickers** permission.", discord.Color.red()), ephemeral=True)
    emoji_id, _, _ = _parse_emoji_input(emoji.strip())
    target_emoji = None
    if emoji_id:
        target_emoji = discord.utils.get(inter.guild.emojis, id=emoji_id)
    if not target_emoji:
        target_emoji = discord.utils.find(lambda e: e.name.lower() == emoji.strip().lstrip(":").rstrip(":").lower(), inter.guild.emojis)
    if not target_emoji:
        return await inter.response.send_message(embed=make_embed("❌ Emoji Not Found", f"Could not find emoji `{emoji}` in this server.", discord.Color.red()), ephemeral=True)
    existing_names = [e.name for e in inter.guild.emojis if e.id != target_emoji.id]
    safe_name = _safe_emoji_name(new_name.strip(), existing_names)
    old_name = target_emoji.name
    try:
        await target_emoji.edit(name=safe_name, reason=f"Renamed by {inter.user} via /renameemoji")
    except discord.Forbidden:
        return await inter.response.send_message(embed=make_embed("❌ Permission Denied", "I can't rename that emoji.", discord.Color.red()), ephemeral=True)
    await inter.response.send_message(embed=make_embed("✏️ Emoji Renamed", f"Renamed `:{old_name}:` → `:{safe_name}:`", discord.Color.green()))

# @bot.tree.command(name="listemoji", description="List all custom emojis in this server")  # de-registered: over Discord's 100 global slash command limit; .listemoji prefix command still works
async def slash_listemoji(inter: discord.Interaction):
    emojis = inter.guild.emojis
    if not emojis:
        return await inter.response.send_message(embed=make_embed("ℹ️ No Custom Emojis", "This server has no custom emojis yet. Use `/addemoji` to add some!", discord.Color.blurple()), ephemeral=True)
    static = [e for e in emojis if not e.animated]
    animated_list = [e for e in emojis if e.animated]
    limit = inter.guild.emoji_limit
    desc = f"**Total:** {len(emojis)}/{limit * 2} slots used\n**Static:** {len(static)}/{limit}  |  **Animated:** {len(animated_list)}/{limit}\n\n"
    if static:
        desc += f"**Static Emojis:**\n{' '.join(str(e) for e in static[:40])}\n\n"
    if animated_list:
        desc += f"**Animated Emojis:**\n{' '.join(str(e) for e in animated_list[:40])}"
    if len(emojis) > 80:
        desc += f"\n\n*...and {len(emojis) - 80} more (showing first 80)*"
    await inter.response.send_message(embed=make_embed(f"😀 Emojis — {inter.guild.name}", desc, discord.Color.blurple()), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — TOS SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="tosadd", description="Add a TOS section (Admin only)")
@app_commands.describe(subject="Section title (max 100 chars)", content="TOS text content (max 4000 chars)")
async def slash_tosadd(inter: discord.Interaction, subject: str, content: str):
    if not inter.user.guild_permissions.administrator:
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only administrators can add TOS sections.", discord.Color.red()), ephemeral=True)
    subject = subject.strip()
    content = content.strip()
    if not subject or not content:
        return await inter.response.send_message(embed=make_embed("❌ Empty Field", "Subject and TOS text cannot be empty.", discord.Color.red()), ephemeral=True)
    if len(subject) > 100:
        return await inter.response.send_message(embed=make_embed("❌ Subject Too Long", "Subject must be 100 characters or fewer.", discord.Color.red()), ephemeral=True)
    if len(content) > 4000:
        return await inter.response.send_message(embed=make_embed("❌ Content Too Long", "TOS text must be 4000 characters or fewer.", discord.Color.red()), ephemeral=True)
    count = tos_col.count_documents({"guild_id": inter.guild_id})
    if count >= 25:
        return await inter.response.send_message(embed=make_embed("❌ Limit Reached", "Maximum of **25 TOS sections** (Discord dropdown limit). Remove one first.", discord.Color.red()), ephemeral=True)
    existing = tos_col.find_one({"guild_id": inter.guild_id, "subject": subject})
    if existing:
        return await inter.response.send_message(embed=make_embed("⚠️ Already Exists", f"A TOS section named **{subject}** already exists. Use `/tosedit` to update it.", discord.Color.orange()), ephemeral=True)
    max_order_doc = tos_col.find_one({"guild_id": inter.guild_id}, sort=[("order", -1)])
    next_order = (max_order_doc.get("order", 0) + 1) if max_order_doc else 1
    tos_col.insert_one({"guild_id": inter.guild_id, "subject": subject, "content": content,
        "order": next_order, "emoji": None, "added_by": inter.user.id,
        "added_at": datetime.datetime.now(datetime.timezone.utc)})
    await inter.response.send_message(embed=make_embed("✅ TOS Section Added",
        f"**Subject:** {subject}\n**Preview:** {content[:200]}{'...' if len(content) > 200 else ''}",
        discord.Color.green(), fields=[("📊 Total Sections", str(count + 1), True)]), ephemeral=True)
    await _refresh_all_tos_panels(inter.guild)

@bot.tree.command(name="tosremove", description="Remove a TOS section (Admin only)")
@app_commands.describe(subject="The exact subject/title of the section to remove")
async def slash_tosremove(inter: discord.Interaction, subject: str):
    if not inter.user.guild_permissions.administrator:
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only administrators can remove TOS sections.", discord.Color.red()), ephemeral=True)
    subject = subject.strip()
    result = tos_col.delete_one({"guild_id": inter.guild_id, "subject": subject})
    if result.deleted_count == 0:
        entries = _get_tos_entries(inter.guild_id)
        subjects_list = "\n".join(f"• `{e['subject']}`" for e in entries) or "*(none yet)*"
        return await inter.response.send_message(embed=make_embed("❌ Not Found",
            f"No TOS section named **{subject}** found.\n\n**Available subjects:**\n{subjects_list}", discord.Color.red()), ephemeral=True)
    await inter.response.send_message(embed=make_embed("🗑️ TOS Section Removed", f"Section **{subject}** has been removed from the TOS.", discord.Color.green()), ephemeral=True)
    await _refresh_all_tos_panels(inter.guild)

@bot.tree.command(name="tosedit", description="Edit an existing TOS section (Admin only)")
@app_commands.describe(subject="The exact subject/title of the section to edit", new_content="The updated TOS text")
async def slash_tosedit(inter: discord.Interaction, subject: str, new_content: str):
    if not inter.user.guild_permissions.administrator:
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only administrators can edit TOS sections.", discord.Color.red()), ephemeral=True)
    subject = subject.strip()
    new_content = new_content.strip()
    if not new_content:
        return await inter.response.send_message(embed=make_embed("❌ Empty Content", "The new TOS text cannot be empty.", discord.Color.red()), ephemeral=True)
    if len(new_content) > 4000:
        return await inter.response.send_message(embed=make_embed("❌ Content Too Long", "TOS text must be 4000 characters or fewer.", discord.Color.red()), ephemeral=True)
    result = tos_col.update_one({"guild_id": inter.guild_id, "subject": subject},
        {"$set": {"content": new_content, "edited_by": inter.user.id, "edited_at": datetime.datetime.now(datetime.timezone.utc)}})
    if result.matched_count == 0:
        return await inter.response.send_message(embed=make_embed("❌ Not Found",
            f"No TOS section named **{subject}** found. Use `/toslist` to see all existing subjects.", discord.Color.red()), ephemeral=True)
    await inter.response.send_message(embed=make_embed("✏️ TOS Section Updated",
        f"**Subject:** {subject}\n**New Content Preview:** {new_content[:200]}{'...' if len(new_content) > 200 else ''}",
        discord.Color.green()), ephemeral=True)
    await _refresh_all_tos_panels(inter.guild)

# @bot.tree.command(name="toslist", description="List all TOS sections for this server")  # de-registered: over Discord's 100 global slash command limit; .toslist prefix command still works
async def slash_toslist(inter: discord.Interaction):
    entries = _get_tos_entries(inter.guild_id)
    if not entries:
        return await inter.response.send_message(embed=make_embed("📜 No TOS Sections",
            "No TOS sections have been added yet. Use `/tosadd` to add the first one.", discord.Color.blurple()), ephemeral=True)
    lines = [f"**{i+1}.** {'`' + e.get('emoji','') + '` ' if e.get('emoji') else ''}`{e['subject']}`" for i, e in enumerate(entries)]
    await inter.response.send_message(embed=make_embed(f"📜 TOS Sections — {len(entries)}/25",
        "\n".join(lines), discord.Color.blurple(),
        fields=[("➕ Add", "`/tosadd`", True), ("✏️ Edit", "`/tosedit`", True), ("🗑️ Remove", "`/tosremove`", True),
                ("🔢 Reorder", "`/tosreorder`", True), ("😀 Set Emoji", "`/tosemoji`", True)]), ephemeral=True)

# @bot.tree.command(name="tosorder", description="Show current TOS section order pre-formatted for /tosreorder")  # de-registered: over Discord's 100 global slash command limit; .tosorder prefix command still works
async def slash_tosorder(inter: discord.Interaction):
    entries = _get_tos_entries(inter.guild_id)
    if not entries:
        return await inter.response.send_message(embed=make_embed("📜 No TOS Sections",
            "No TOS sections have been added yet. Use `/tosadd` to add one.", discord.Color.blurple()), ephemeral=True)
    formatted = " ".join(f"[{e['subject']}]" for e in entries)
    numbered  = "\n".join(f"**{i+1}.** `{e['subject']}`" for i, e in enumerate(entries))
    await inter.response.send_message(embed=make_embed(
        "📋 Current TOS Order",
        f"**Current order:**\n{numbered}\n\n"
        f"**Ready-to-use for `/tosreorder`:**\n```\n{formatted}\n```",
        discord.Color.blurple()), ephemeral=True)

# @bot.tree.command(name="tosview", description="View the full content of a specific TOS section")  # de-registered: over Discord's 100 global slash command limit; .tosview prefix command still works
@app_commands.describe(subject="The subject/title of the TOS section")
async def slash_tosview(inter: discord.Interaction, subject: str):
    subject = subject.strip()
    entry = tos_col.find_one({"guild_id": inter.guild_id, "subject": subject})
    if not entry:
        return await inter.response.send_message(embed=make_embed("❌ Not Found",
            f"No TOS section named **{subject}** found. Use `/toslist` to view all sections.", discord.Color.red()), ephemeral=True)
    e = discord.Embed(title=f"📄 {entry['subject']}", description=entry["content"],
        color=discord.Color.blurple(), timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.set_footer(text="Terms of Service")
    await inter.response.send_message(embed=e, ephemeral=True)

# @bot.tree.command(name="tosreorder", description="Reorder TOS sections (Admin only)")  # de-registered: over Discord's 100 global slash command limit; .tosreorder prefix command still works
@app_commands.describe(subjects="Subject names in new order, separated by commas e.g. Rules, Privacy Policy, Refunds")
async def slash_tosreorder(inter: discord.Interaction, subjects: str):
    if not inter.user.guild_permissions.administrator:
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only administrators can reorder TOS sections.", discord.Color.red()), ephemeral=True)
    new_order_subjects = [s.strip() for s in subjects.split(",") if s.strip()]
    if not new_order_subjects:
        return await inter.response.send_message(embed=make_embed("❌ Invalid Format",
            "Provide subjects separated by commas. Example: `Rules, Privacy Policy, Refunds`", discord.Color.red()), ephemeral=True)
    entries = _get_tos_entries(inter.guild_id)
    all_subjects = {e["subject"]: e for e in entries}
    not_found = [s for s in new_order_subjects if s not in all_subjects]
    if not_found:
        return await inter.response.send_message(embed=make_embed("❌ Subject(s) Not Found",
            "These subjects don't exist:\n" + "\n".join(f"• `{s}`" for s in not_found) + "\n\nUse `/toslist` to see valid subjects.", discord.Color.red()), ephemeral=True)
    seen = set()
    ordered = []
    for s in new_order_subjects:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    for e in entries:
        if e["subject"] not in seen:
            ordered.append(e["subject"])
    for i, subject in enumerate(ordered, start=1):
        tos_col.update_one({"guild_id": inter.guild_id, "subject": subject}, {"$set": {"order": i}})
    new_entries = _get_tos_entries(inter.guild_id)
    lines = [f"**{i+1}.** {'`' + e.get('emoji','') + '` ' if e.get('emoji') else ''}`{e['subject']}`" for i, e in enumerate(new_entries)]
    await inter.response.send_message(embed=make_embed("🔢 TOS Reordered", "New order:\n" + "\n".join(lines), discord.Color.green()), ephemeral=True)
    await _refresh_all_tos_panels(inter.guild)

# @bot.tree.command(name="tosemoji", description="Set or remove the emoji for a TOS dropdown entry (Admin only)")  # de-registered: over Discord's 100 global slash command limit; .tosemoji prefix command still works
@app_commands.describe(subject="The TOS section subject", emoji="Emoji to set (unicode or custom), or 'none' to remove")
async def slash_tosemoji(inter: discord.Interaction, subject: str, emoji: str):
    if not inter.user.guild_permissions.administrator:
        return await inter.response.send_message(embed=make_embed("❌ Access Denied", "Only administrators can set TOS emojis.", discord.Color.red()), ephemeral=True)
    subject = subject.strip()
    raw_emoji = emoji.strip()
    entry = tos_col.find_one({"guild_id": inter.guild_id, "subject": subject})
    if not entry:
        entries = _get_tos_entries(inter.guild_id)
        subjects_list = "\n".join(f"• `{e['subject']}`" for e in entries) or "*(none yet)*"
        return await inter.response.send_message(embed=make_embed("❌ Subject Not Found",
            f"No TOS section named **{subject}**.\n\n**Available subjects:**\n{subjects_list}", discord.Color.red()), ephemeral=True)
    if raw_emoji.lower() in ("none", "remove", "clear", "-"):
        tos_col.update_one({"guild_id": inter.guild_id, "subject": subject}, {"$set": {"emoji": None}})
        await inter.response.send_message(embed=make_embed("✅ Emoji Removed", f"Emoji cleared from **{subject}** in the TOS dropdown.", discord.Color.green()), ephemeral=True)
        return await _refresh_all_tos_panels(inter.guild)
    unicode_emoji_pattern = _re.compile(
        r"[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F"
        r"\U00002702-\U000027B0\u2194-\u2199\u231A-\u231B\u23E9-\u23F3"
        r"\u25AA-\u25FE\u2600-\u26FF\u2702-\u27B0]+", _re.UNICODE)
    custom_emoji_pattern = _re.compile(r"<a?:[a-zA-Z0-9_]+:\d+>")
    is_unicode = bool(unicode_emoji_pattern.search(raw_emoji))
    is_custom  = bool(custom_emoji_pattern.match(raw_emoji))
    if not is_unicode and not is_custom:
        return await inter.response.send_message(embed=make_embed("❌ Invalid Emoji",
            "Please provide a valid emoji.\n• Unicode emoji: `📋` `✅`\n• Custom emoji: `<:name:id>`\n• To clear: `none`", discord.Color.red()), ephemeral=True)
    if is_custom:
        m = _re.match(r"<a?:([a-zA-Z0-9_]+):(\d+)>", raw_emoji)
        if m:
            emoji_id = int(m.group(2))
            guild_emoji = discord.utils.get(inter.guild.emojis, id=emoji_id)
            if not guild_emoji:
                return await inter.response.send_message(embed=make_embed("❌ Custom Emoji Not in This Server",
                    "Discord only allows emojis **from this server** to be used in dropdowns.", discord.Color.red()), ephemeral=True)
        emoji_value = raw_emoji
    else:
        emoji_value = unicode_emoji_pattern.search(raw_emoji).group(0)
    tos_col.update_one({"guild_id": inter.guild_id, "subject": subject}, {"$set": {"emoji": emoji_value}})
    await inter.response.send_message(embed=make_embed("😀 TOS Emoji Set",
        f"Emoji for **{subject}** → {emoji_value}\nIt will now appear in the dropdown next to the subject name.", discord.Color.green()), ephemeral=True)
    await _refresh_all_tos_panels(inter.guild)


# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL ERROR HANDLER
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandInvokeError):
        error = error.original
    if isinstance(error, discord.Forbidden):
        return await ctx.send("🚫 **Missing Permissions** — check my role is high enough and I have the right permissions.")
    if isinstance(error, commands.MemberNotFound):
        return await ctx.send(f"❌ **Member Not Found:** `{error.argument}` — use @mention or user ID.")
    if isinstance(error, commands.UserNotFound):
        return await ctx.send(f"❌ **User Not Found:** `{error.argument}`")
    if isinstance(error, commands.RoleNotFound):
        return await ctx.send(f"❌ **Role Not Found:** `{error.argument}` — @mention the role.")
    if isinstance(error, commands.ChannelNotFound):
        return await ctx.send(f"❌ **Channel Not Found:** `{error.argument}` — #mention the channel.")
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(f"⚠️ **Missing Argument:** `{error.param.name}` — run `!help {ctx.command}`")
    if isinstance(error, commands.BadArgument):
        return await ctx.send(f"⚠️ **Invalid Argument** — run `!help {ctx.command}`")
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, discord.HTTPException) and error.code == 50013:
        return await ctx.send("🚫 **Role Hierarchy Error** — drag my role above all member roles in Server Settings → Roles.")
    e = discord.Embed(title="💥 Unexpected Error", color=discord.Color.red(),
                      description=f"```{type(error).__name__}: {error}```")
    await ctx.send(embed=e)
    raise error

@bot.tree.error
async def on_slash_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    msg = f"💥 Error: `{type(error).__name__}: {error}`"
    if inter.response.is_done():
        await inter.followup.send(msg, ephemeral=True)
    else:
        await inter.response.send_message(msg, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  GIVEAWAY SYSTEM
# ══════════════════════════════════════════════════════════════════════════════
#  Commands:
#    .gstart <time> <winners> <prize>   — starts a giveaway
#    .gend   <message_id>               — ends giveaway immediately & picks winners
#    .greroll <message_id>              — re-rolls winner(s) from existing reactions
#
#  Reaction emoji: <a:blueverify:1546740183716855872>  (animated custom emoji)
#  Falls back to 🎉 if the custom emoji is unavailable.
# ══════════════════════════════════════════════════════════════════════════════

GIVEAWAY_EMOJI_STR = "<a:blueverify:1546740183716855872>"  # shown in embeds
GIVEAWAY_EMOJI_RAW = "blueverify"                          # name used for reaction lookup

# ── Duration parser ────────────────────────────────────────────────────────────

def _parse_giveaway_duration(raw: str) -> int | None:
    """Convert strings like '10m', '2h', '1d' to seconds. Returns None on failure."""
    raw = raw.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if raw[-1] in multipliers:
        try:
            return int(raw[:-1]) * multipliers[raw[-1]]
        except ValueError:
            return None
    return None

# ── Embed builder ──────────────────────────────────────────────────────────────

def _giveaway_embed(prize: str, winners: int, host_id: int,
                    ends_at: datetime.datetime, ended: bool = False,
                    winner_ids: list[int] | None = None) -> discord.Embed:
    color = discord.Color.from_rgb(0, 200, 220) if not ended else discord.Color.from_rgb(80, 80, 80)
    unix_ts = int(ends_at.timestamp())

    GE = "<:gift:1546740220148449332>"   # gift emoji for inside embeds

    if ended and winner_ids:
        mentions = ", ".join(f"<@{w}>" for w in winner_ids)
        desc = (
            f"**Prize:** {GE} {prize}\n"
            f"**Winners:** {mentions}\n"
            f"**Hosted by:** <@{host_id}>\n\n"
            f"**Ended:** <t:{unix_ts}:R>"
        )
        title = f"{GE} Giveaway Ended"
    elif ended:
        desc = (
            f"**Prize:** {GE} {prize}\n"
            f"**Winners:** *No valid entries*\n"
            f"**Hosted by:** <@{host_id}>\n\n"
            f"**Ended:** <t:{unix_ts}:R>"
        )
        title = f"{GE} Giveaway Ended"
    else:
        desc = (
            f"**Prize:** {GE} {prize}\n"
            f"**Winners:** {winners}\n"
            f"**Hosted by:** <@{host_id}>\n\n"
            f"React with {GIVEAWAY_EMOJI_STR} to enter!\n"
            f"**Ends:** <t:{unix_ts}:R> (<t:{unix_ts}:f>)"
        )
        title = f"{GE} New Giveaway {GE}"

    embed = discord.Embed(title=title, description=desc, color=color,
                          timestamp=ends_at)
    embed.set_footer(text=f"{'Ended' if ended else 'Ends'} at")
    return embed

# ── Core pick-winner logic ─────────────────────────────────────────────────────

async def _pick_winners(message: discord.Message, num_winners: int,
                        host_id: int) -> list[int]:
    """Fetch all reactions on *message* matching the giveaway emoji, return random winners."""
    import random
    participants: list[int] = []
    for reaction in message.reactions:
        emoji = reaction.emoji
        # Match both animated custom emoji and plain name fallback
        name = emoji.name if hasattr(emoji, "name") else str(emoji)
        if name == GIVEAWAY_EMOJI_RAW or str(emoji) == "🎉":
            async for user in reaction.users():
                if not user.bot and user.id != host_id:
                    participants.append(user.id)
            break  # found our emoji

    if not participants:
        return []
    random.shuffle(participants)
    return participants[:min(num_winners, len(participants))]

# ── End-giveaway helper (shared by timer & .gend) ─────────────────────────────

async def _end_giveaway(doc: dict):
    """Conclude a giveaway: pick winners, edit message, announce, update DB."""
    guild = bot.get_guild(doc["guild_id"])
    if not guild:
        return
    channel = guild.get_channel(doc["channel_id"])
    if not channel:
        return
    try:
        message = await channel.fetch_message(doc["message_id"])
    except Exception:
        return

    ends_at  = doc["ends_at"]
    if isinstance(ends_at, (int, float)):
        ends_at = datetime.datetime.fromtimestamp(ends_at, tz=datetime.timezone.utc)
    elif ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=datetime.timezone.utc)

    winner_ids = await _pick_winners(message, doc["num_winners"], doc["host_id"])

    # Edit the original giveaway message
    ended_embed = _giveaway_embed(
        prize=doc["prize"], winners=doc["num_winners"],
        host_id=doc["host_id"], ends_at=ends_at,
        ended=True, winner_ids=winner_ids)
    try:
        await message.edit(embed=ended_embed)
    except Exception:
        pass

    # Announce winners in the same channel
    if winner_ids:
        mentions = " ".join(f"<@{w}>" for w in winner_ids)
        announce_embed = discord.Embed(
            title="🎊 Giveaway Winners!",
            description=(
                f"Congratulations {mentions}!\n"
                f"You won **{doc['prize']}**!\n\n"
                f"[Jump to Giveaway]({message.jump_url})"
            ),
            color=discord.Color.gold(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        announce_embed.set_footer(text=f"Hosted by user ID {doc['host_id']}")
        await channel.send(content=mentions, embed=announce_embed)
    else:
        await channel.send(embed=discord.Embed(
            description="😔 No valid entries — no winner could be picked.",
            color=discord.Color.red()))

    # Mark as ended in MongoDB
    giveaways_col.update_one(
        {"message_id": doc["message_id"]},
        {"$set": {"ended": True, "winner_ids": winner_ids}})

# ── Background task — checks every 15 s for expired giveaways ─────────────────

@tasks.loop(seconds=15)
async def giveaway_tick():
    await bot.wait_until_ready()
    now = datetime.datetime.now(datetime.timezone.utc)
    for doc in giveaways_col.find({"ended": False}):
        ends_at = doc["ends_at"]
        if isinstance(ends_at, (int, float)):
            ends_at = datetime.datetime.fromtimestamp(ends_at, tz=datetime.timezone.utc)
        elif ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=datetime.timezone.utc)
        if now >= ends_at:
            try:
                await _end_giveaway(doc)
            except Exception as e:
                print(f"[giveaway_tick] error ending giveaway {doc.get('message_id')}: {e}")

# ── .gstart ────────────────────────────────────────────────────────────────────

@bot.command(name="gstart")
async def gstart(ctx, duration: str = None, num_winners: str = None, *, prize: str = None):
    """Start a giveaway.  Usage: .gstart <time> <winners> <prize>
    Examples:  .gstart 1h 1 Level 2 Stake Account
               .gstart 30m 3 Netflix Premium"""
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "You need mod permissions to start a giveaway.", discord.Color.red()))

    # Validate args
    if not duration or not num_winners or not prize:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.gstart <time> <winners> <prize>`\n"
            "**Examples:**\n"
            "`.gstart 1h 1 Level 2 Stake Account`\n"
            "`.gstart 30m 3 Netflix Premium`\n\n"
            "**Time formats:** `10s` `5m` `2h` `1d`",
            discord.Color.red()))

    seconds = _parse_giveaway_duration(duration)
    if seconds is None or seconds < 10:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Duration",
            "Minimum duration is `10s`. Use formats like `10m`, `2h`, `1d`.",
            discord.Color.red()))

    try:
        winners = int(num_winners)
        if winners < 1 or winners > 20:
            raise ValueError
    except ValueError:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Winner Count",
            "Winners must be a number between **1** and **20**.",
            discord.Color.red()))

    ends_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)

    # Delete the invoking command message for a cleaner look
    try:
        await ctx.message.delete()
    except Exception:
        pass

    embed = _giveaway_embed(prize=prize, winners=winners,
                            host_id=ctx.author.id, ends_at=ends_at)
    gw_msg = await ctx.channel.send(content="<:gift:1546740220148449332> **New Giveaway** <:gift:1546740220148449332>", embed=embed)

    # Add the reaction
    try:
        # Try to find the custom emoji in the guild or bot's guilds
        custom_emoji = discord.utils.get(bot.emojis, name=GIVEAWAY_EMOJI_RAW)
        if custom_emoji:
            await gw_msg.add_reaction(custom_emoji)
        else:
            await gw_msg.add_reaction("🎉")
    except Exception:
        await gw_msg.add_reaction("🎉")

    # Persist to MongoDB
    giveaways_col.insert_one({
        "guild_id":    ctx.guild.id,
        "channel_id":  ctx.channel.id,
        "message_id":  gw_msg.id,
        "host_id":     ctx.author.id,
        "prize":       prize,
        "num_winners": winners,
        "ends_at":     ends_at,
        "ended":       False,
        "winner_ids":  [],
    })

# ── .gend ──────────────────────────────────────────────────────────────────────

@bot.command(name="gend")
async def gend(ctx, message_id: int = None):
    """End a giveaway immediately.  Usage: .gend <message_id>"""
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "You need mod permissions to end a giveaway.", discord.Color.red()))

    if not message_id:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.gend <message_id>`\n"
            "Right-click the giveaway message → Copy Message ID.",
            discord.Color.red()))

    doc = giveaways_col.find_one({"message_id": message_id, "guild_id": ctx.guild.id})
    if not doc:
        return await ctx.send(embed=make_embed(
            "❌ Not Found",
            f"No active giveaway found with message ID `{message_id}`.",
            discord.Color.red()))

    if doc.get("ended"):
        return await ctx.send(embed=make_embed(
            "⚠️ Already Ended",
            "That giveaway has already ended. Use `.greroll` to pick new winners.",
            discord.Color.orange()))

    try:
        await ctx.message.delete()
    except Exception:
        pass

    await _end_giveaway(doc)

# ── .greroll ───────────────────────────────────────────────────────────────────

@bot.command(name="greroll")
async def greroll(ctx, message_id: int = None):
    """Re-roll winner(s) for a finished giveaway.  Usage: .greroll <message_id>"""
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "You need mod permissions to re-roll a giveaway.", discord.Color.red()))

    if not message_id:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.greroll <message_id>`\n"
            "Right-click the giveaway message → Copy Message ID.",
            discord.Color.red()))

    doc = giveaways_col.find_one({"message_id": message_id, "guild_id": ctx.guild.id})
    if not doc:
        return await ctx.send(embed=make_embed(
            "❌ Not Found",
            f"No giveaway found with message ID `{message_id}`.\n"
            "Make sure you're using the ID of the **giveaway bot message**, not your command.",
            discord.Color.red()))

    channel = ctx.guild.get_channel(doc["channel_id"])
    if not channel:
        return await ctx.send(embed=make_embed("❌ Channel Not Found",
            "The giveaway channel no longer exists.", discord.Color.red()))

    try:
        message = await channel.fetch_message(message_id)
    except Exception:
        return await ctx.send(embed=make_embed("❌ Message Not Found",
            "Could not fetch the giveaway message. It may have been deleted.", discord.Color.red()))

    try:
        await ctx.message.delete()
    except Exception:
        pass

    ends_at = doc["ends_at"]
    if isinstance(ends_at, (int, float)):
        ends_at = datetime.datetime.fromtimestamp(ends_at, tz=datetime.timezone.utc)
    elif ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=datetime.timezone.utc)

    winner_ids = await _pick_winners(message, doc["num_winners"], doc["host_id"])

    if not winner_ids:
        return await ctx.send(embed=discord.Embed(
            description="😔 No valid entries found — could not pick new winners.",
            color=discord.Color.red()))

    mentions = " ".join(f"<@{w}>" for w in winner_ids)
    reroll_embed = discord.Embed(
        title="🔄 Giveaway Re-rolled!",
        description=(
            f"New winner(s): {mentions}\n"
            f"**Prize:** {doc['prize']}\n\n"
            f"[Jump to Giveaway]({message.jump_url})"
        ),
        color=discord.Color.from_rgb(0, 200, 220),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    reroll_embed.set_footer(text=f"Re-rolled by {ctx.author.display_name}")
    await ctx.channel.send(content=mentions, embed=reroll_embed)

    # Update DB with new winners
    giveaways_col.update_one(
        {"message_id": message_id},
        {"$set": {"winner_ids": winner_ids}})

    # Update the original giveaway message embed too
    try:
        updated_embed = _giveaway_embed(
            prize=doc["prize"], winners=doc["num_winners"],
            host_id=doc["host_id"], ends_at=ends_at,
            ended=True, winner_ids=winner_ids)
        await message.edit(embed=updated_embed)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULED MESSAGES — .setscheuledmessage / .removeschedulemessage
#
#  Sends a recurring message every <interval> in the channel it was set in,
#  deleting the previously-sent copy first. Interval can be h/m/d (and s).
# ══════════════════════════════════════════════════════════════════════════════

def _parse_schedule_interval(raw: str) -> int | None:
    """Convert strings like '10m', '2h', '1d' to seconds. Returns None on failure."""
    raw = raw.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if raw and raw[-1] in multipliers:
        try:
            value = int(raw[:-1])
            if value <= 0:
                return None
            return value * multipliers[raw[-1]]
        except ValueError:
            return None
    return None


async def _send_scheduled_message(doc: dict):
    """Delete the previous message (if any) and send a fresh copy, then update DB."""
    guild = bot.get_guild(doc["guild_id"])
    if not guild:
        return
    channel = guild.get_channel(doc["channel_id"])
    if not channel:
        return

    # Delete the old message before sending the new one
    last_message_id = doc.get("last_message_id")
    if last_message_id:
        try:
            old_msg = await channel.fetch_message(last_message_id)
            await old_msg.delete()
        except Exception:
            pass

    try:
        new_msg = await channel.send(doc["message"])
    except Exception as e:
        print(f"[scheduled_message_tick] error sending '{doc.get('name')}': {e}")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    next_run = now + datetime.timedelta(seconds=doc["interval_seconds"])
    scheduled_messages_col.update_one(
        {"_id": doc["_id"]},
        {"$set": {"last_message_id": new_msg.id, "next_run": next_run, "last_sent": now}})


@tasks.loop(seconds=15)
async def scheduled_message_tick():
    await bot.wait_until_ready()
    now = datetime.datetime.now(datetime.timezone.utc)
    for doc in scheduled_messages_col.find({}):
        next_run = doc.get("next_run")
        if isinstance(next_run, (int, float)):
            next_run = datetime.datetime.fromtimestamp(next_run, tz=datetime.timezone.utc)
        elif next_run and next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=datetime.timezone.utc)
        if next_run and now >= next_run:
            try:
                await _send_scheduled_message(doc)
            except Exception as e:
                print(f"[scheduled_message_tick] error processing '{doc.get('name')}': {e}")


# ── .setscheuledmessage ─────────────────────────────────────────────────────────

@bot.command(name="setscheuledmessage")
async def setscheuledmessage(ctx, time: str = None, *, message: str = None):
    """Schedule a recurring message in this channel.
    Usage: .setscheuledmessage <time> <message>
    Time formats: 10s, 10m, 2h, 1d
    Each time it fires, the previous scheduled message is deleted before the new one is sent."""
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "You need mod permissions to set a scheduled message.", discord.Color.red()))

    if not time or not message:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.setscheuledmessage <time> <message>`\n"
            "**Example:** `.setscheuledmessage 1h Don't forget to check #announcements!`\n\n"
            "**Time formats:** `10s` `10m` `2h` `1d`",
            discord.Color.red()))

    interval_seconds = _parse_schedule_interval(time)
    if interval_seconds is None:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Time",
            "Use formats like `10m`, `2h`, or `1d`.",
            discord.Color.red()))

    name = f"sched-{uuid.uuid4().hex[:8]}"
    now = datetime.datetime.now(datetime.timezone.utc)

    scheduled_messages_col.insert_one({
        "name": name,
        "guild_id": ctx.guild.id,
        "channel_id": ctx.channel.id,
        "message": message,
        "interval_seconds": interval_seconds,
        "created_by": ctx.author.id,
        "created_at": now,
        "next_run": now,             # fire immediately on first tick
        "last_message_id": None,
    })

    await ctx.send(embed=make_embed(
        "✅ Scheduled Message Set",
        f"This message will be sent here every **{time}**, deleting the previous one first.",
        discord.Color.green(),
        fields=[("🆔 Name/ID", name, False), ("💬 Message", message[:1000], False)]))


# ── .removeschedulemessage ──────────────────────────────────────────────────────

@bot.command(name="removeschedulemessage")
async def removeschedulemessage(ctx, *, name: str = None):
    """Remove a scheduled message by its name/ID.
    Usage: .removeschedulemessage <message-name/id>"""
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "You need mod permissions to remove a scheduled message.", discord.Color.red()))

    if not name:
        # List existing scheduled messages for this guild to help the user
        docs = list(scheduled_messages_col.find({"guild_id": ctx.guild.id}))
        if not docs:
            return await ctx.send(embed=make_embed(
                "❌ Invalid Usage",
                "**Usage:** `.removeschedulemessage <message-name/id>`\n"
                "No scheduled messages exist in this server.",
                discord.Color.red()))
        listing = "\n".join(f"`{d['name']}` — <#{d['channel_id']}> — {d['message'][:50]}" for d in docs)
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            f"**Usage:** `.removeschedulemessage <message-name/id>`\n\n**Active scheduled messages:**\n{listing}",
            discord.Color.red()))

    name = name.strip()
    doc = scheduled_messages_col.find_one({"guild_id": ctx.guild.id, "name": name})
    if not doc:
        return await ctx.send(embed=make_embed(
            "❌ Not Found",
            f"No scheduled message found with name/ID `{name}`.\n"
            "Run `.removeschedulemessage` with no arguments to list active ones.",
            discord.Color.red()))

    # Try to delete the last sent message if it still exists
    guild = bot.get_guild(doc["guild_id"])
    if guild:
        channel = guild.get_channel(doc["channel_id"])
        last_message_id = doc.get("last_message_id")
        if channel and last_message_id:
            try:
                old_msg = await channel.fetch_message(last_message_id)
                await old_msg.delete()
            except Exception:
                pass

    scheduled_messages_col.delete_one({"_id": doc["_id"]})
    await ctx.send(embed=make_embed(
        "🗑️ Scheduled Message Removed",
        f"Scheduled message `{name}` has been removed and will no longer be sent.",
        discord.Color.green()))




# ══════════════════════════════════════════════════════════════════════════════
#  EMOJI MANAGEMENT — .addemoji / .removeemoji / .listemoji / .renameemoji
# ══════════════════════════════════════════════════════════════════════════════

def _parse_emoji_input(raw: str):
    """
    Accept a custom emoji mention (<:name:id> or <a:name:id>) or a raw emoji ID.
    Returns (emoji_id, name, animated) or (None, None, None) on failure.
    """
    # Animated emoji
    m = _re.match(r"<a:([a-zA-Z0-9_]+):(\d+)>", raw.strip())
    if m:
        return int(m.group(2)), m.group(1), True
    # Static emoji
    m = _re.match(r"<:([a-zA-Z0-9_]+):(\d+)>", raw.strip())
    if m:
        return int(m.group(2)), m.group(1), False
    # Raw numeric ID — name unknown, not animated (best-effort)
    if raw.strip().isdigit():
        return int(raw.strip()), None, False
    return None, None, None


async def _download_emoji(emoji_id: int, animated: bool) -> bytes | None:
    """Download emoji image bytes from Discord's CDN."""
    ext = "gif" if animated else "png"
    url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=128&quality=lossless"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        pass
    # Fallback: try the other extension
    ext2 = "png" if animated else "gif"
    url2 = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext2}?size=128"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url2, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        pass
    return None


def _safe_emoji_name(name: str, existing_names: list[str]) -> str:
    """
    Sanitize emoji name: only alphanumeric + underscores, length 2-32.
    Resolve duplicates by appending _2, _3, etc.
    """
    sanitized = _re.sub(r"[^a-zA-Z0-9_]", "_", name)[:32]
    if len(sanitized) < 2:
        sanitized = sanitized + "_e"
    candidate = sanitized
    counter = 2
    while candidate.lower() in [n.lower() for n in existing_names]:
        candidate = f"{sanitized[:28]}_{counter}"
        counter += 1
    return candidate


@bot.command(name="addemoji")
async def addemoji(ctx, *emoji_inputs):
    """Add one or more emojis from other servers.
    Usage: .addemoji <emoji1> <emoji2> ...
    Accepts custom emoji mentions or raw emoji IDs."""
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "You need mod permissions to add emojis.", discord.Color.red()))

    if not ctx.guild.me.guild_permissions.manage_emojis:
        return await ctx.send(embed=make_embed(
            "❌ Missing Permission",
            "I need the **Manage Emojis and Stickers** permission to add emojis.",
            discord.Color.red()))

    if not emoji_inputs:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.addemoji <emoji1> <emoji2> ...`\n"
            "**Example:** `.addemoji <:cool:123456789> <a:wave:987654321>`\n\n"
            "You can paste emojis from other servers directly into the command.",
            discord.Color.red()))

    existing_names = [e.name for e in ctx.guild.emojis]
    results_ok  = []
    results_err = []

    status_msg = await ctx.send(embed=make_embed(
        "⏳ Processing...",
        f"Downloading and uploading **{len(emoji_inputs)}** emoji(s). Please wait...",
        discord.Color.blurple()))

    for raw in emoji_inputs:
        emoji_id, name, animated = _parse_emoji_input(raw)
        if emoji_id is None:
            results_err.append(f"`{raw}` — Could not parse as a custom emoji or ID.")
            continue

        # Determine display name
        display_name = name or f"emoji_{emoji_id}"

        # Download image
        img_bytes = await _download_emoji(emoji_id, animated)
        if not img_bytes:
            # Try the opposite extension
            img_bytes = await _download_emoji(emoji_id, not animated)
            if img_bytes:
                animated = not animated
        if not img_bytes:
            results_err.append(f"`{display_name}` (`{emoji_id}`) — Failed to download from Discord CDN.")
            continue

        # Pick a safe, non-duplicate name
        safe_name = _safe_emoji_name(display_name, existing_names)
        existing_names.append(safe_name)  # Reserve it before next iteration

        try:
            new_emoji = await ctx.guild.create_custom_emoji(
                name=safe_name,
                image=img_bytes,
                reason=f"Added by {ctx.author} via .addemoji")
            kind = "Animated" if new_emoji.animated else "Static"
            results_ok.append(f"{new_emoji} `:{new_emoji.name}:` — {kind}")
            # Log to DB
            emoji_log_col.insert_one({
                "guild_id": ctx.guild.id,
                "emoji_id": new_emoji.id,
                "name": new_emoji.name,
                "source_id": emoji_id,
                "animated": new_emoji.animated,
                "added_by": ctx.author.id,
                "added_at": datetime.datetime.now(datetime.timezone.utc),
            })
        except discord.HTTPException as e:
            if e.code == 30008:
                results_err.append(f"`{safe_name}` — Server emoji limit reached.")
            elif e.code == 50013:
                results_err.append(f"`{safe_name}` — Missing permission to create emojis.")
            else:
                results_err.append(f"`{safe_name}` — Discord error: `{e.text}`")
        except Exception as e:
            results_err.append(f"`{safe_name}` — Unexpected error: `{e}`")

    # Build result embed
    desc_parts = []
    if results_ok:
        desc_parts.append("**✅ Added:**\n" + "\n".join(results_ok))
    if results_err:
        desc_parts.append("**❌ Failed:**\n" + "\n".join(results_err))

    color = discord.Color.green() if results_ok and not results_err else (
        discord.Color.orange() if results_ok else discord.Color.red())
    title = (f"✅ {len(results_ok)} Emoji(s) Added" if not results_err
             else f"⚠️ {len(results_ok)} Added, {len(results_err)} Failed")

    result_embed = make_embed(title, "\n\n".join(desc_parts), color)
    try:
        await status_msg.edit(embed=result_embed)
    except Exception:
        await ctx.send(embed=result_embed)


@bot.command(name="removeemoji")
async def removeemoji(ctx, *, emoji_input: str = None):
    """Remove an emoji from this server by name or mention.
    Usage: .removeemoji <:emojiname:> or .removeemoji emojiname"""
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "You need mod permissions to remove emojis.", discord.Color.red()))

    if not ctx.guild.me.guild_permissions.manage_emojis:
        return await ctx.send(embed=make_embed(
            "❌ Missing Permission",
            "I need the **Manage Emojis and Stickers** permission.",
            discord.Color.red()))

    if not emoji_input:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.removeemoji <emoji>` or `.removeemoji emojiname`",
            discord.Color.red()))

    emoji_input = emoji_input.strip()
    # Try to find by mention
    emoji_id, name, _ = _parse_emoji_input(emoji_input)
    target_emoji = None

    if emoji_id:
        target_emoji = discord.utils.get(ctx.guild.emojis, id=emoji_id)
    if not target_emoji:
        # Search by name (case-insensitive)
        target_emoji = discord.utils.find(
            lambda e: e.name.lower() == emoji_input.lstrip(":").rstrip(":").lower(),
            ctx.guild.emojis)

    if not target_emoji:
        return await ctx.send(embed=make_embed(
            "❌ Emoji Not Found",
            f"Could not find emoji `{emoji_input}` in this server.",
            discord.Color.red()))

    emoji_name = target_emoji.name
    try:
        await target_emoji.delete(reason=f"Removed by {ctx.author} via .removeemoji")
    except discord.Forbidden:
        return await ctx.send(embed=make_embed(
            "❌ Permission Denied", "I can't delete that emoji.", discord.Color.red()))

    emoji_log_col.update_one(
        {"guild_id": ctx.guild.id, "emoji_id": target_emoji.id},
        {"$set": {"removed_by": ctx.author.id,
                  "removed_at": datetime.datetime.now(datetime.timezone.utc)}})

    await ctx.send(embed=make_embed(
        "🗑️ Emoji Removed",
        f"Emoji `:{emoji_name}:` has been removed from this server.",
        discord.Color.green()))


@bot.command(name="renameemoji")
async def renameemoji(ctx, emoji_input: str = None, *, new_name: str = None):
    """Rename an existing emoji in this server.
    Usage: .renameemoji <emoji> <new_name>"""
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "You need mod permissions to rename emojis.", discord.Color.red()))

    if not ctx.guild.me.guild_permissions.manage_emojis:
        return await ctx.send(embed=make_embed(
            "❌ Missing Permission",
            "I need the **Manage Emojis and Stickers** permission.",
            discord.Color.red()))

    if not emoji_input or not new_name:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.renameemoji <emoji> <new_name>`\n"
            "**Example:** `.renameemoji <:oldname:123456> coolname`",
            discord.Color.red()))

    emoji_id, _, _ = _parse_emoji_input(emoji_input)
    target_emoji = None
    if emoji_id:
        target_emoji = discord.utils.get(ctx.guild.emojis, id=emoji_id)
    if not target_emoji:
        target_emoji = discord.utils.find(
            lambda e: e.name.lower() == emoji_input.lstrip(":").rstrip(":").lower(),
            ctx.guild.emojis)

    if not target_emoji:
        return await ctx.send(embed=make_embed(
            "❌ Emoji Not Found",
            f"Could not find emoji `{emoji_input}` in this server.",
            discord.Color.red()))

    existing_names = [e.name for e in ctx.guild.emojis if e.id != target_emoji.id]
    safe_name = _safe_emoji_name(new_name.strip(), existing_names)
    old_name = target_emoji.name

    try:
        await target_emoji.edit(name=safe_name, reason=f"Renamed by {ctx.author} via .renameemoji")
    except discord.Forbidden:
        return await ctx.send(embed=make_embed(
            "❌ Permission Denied", "I can't rename that emoji.", discord.Color.red()))

    await ctx.send(embed=make_embed(
        "✏️ Emoji Renamed",
        f"Renamed `:{old_name}:` → `:{safe_name}:`",
        discord.Color.green()))


@bot.command(name="listemoji")
async def listemoji(ctx):
    """List all custom emojis in this server.
    Usage: .listemoji"""
    emojis = ctx.guild.emojis
    if not emojis:
        return await ctx.send(embed=make_embed(
            "ℹ️ No Custom Emojis",
            "This server has no custom emojis yet. Use `.addemoji` to add some!",
            discord.Color.blurple()))

    static = [e for e in emojis if not e.animated]
    animated = [e for e in emojis if e.animated]
    limit = ctx.guild.emoji_limit

    desc = f"**Total:** {len(emojis)}/{limit * 2} slots used\n"
    desc += f"**Static:** {len(static)}/{limit}  |  **Animated:** {len(animated)}/{limit}\n\n"

    if static:
        chunk = " ".join(str(e) for e in static[:40])
        desc += f"**Static Emojis:**\n{chunk}\n\n"
    if animated:
        chunk = " ".join(str(e) for e in animated[:40])
        desc += f"**Animated Emojis:**\n{chunk}"

    if len(emojis) > 80:
        desc += f"\n\n*...and {len(emojis) - 80} more (showing first 80)*"

    await ctx.send(embed=make_embed(
        f"😀 Emojis — {ctx.guild.name}", desc, discord.Color.blurple()))


# ══════════════════════════════════════════════════════════════════════════════
#  TOS SYSTEM — .tos / .tosadd / .tosremove / .toslist / .tosedit
# ══════════════════════════════════════════════════════════════════════════════

# ── TOS Helpers ────────────────────────────────────────────────────────────────

def _get_tos_entries(guild_id: int) -> list[dict]:
    """Return all TOS entries for the guild, sorted by 'order' then 'subject'."""
    return list(tos_col.find({"guild_id": guild_id}).sort([("order", 1), ("subject", 1)]))


def _build_tos_embed(guild: discord.Guild) -> discord.Embed:
    """Build the main TOS panel embed matching the NoxStore/ElfMart layout."""
    entries = _get_tos_entries(guild.id)

    # ── Static body — matches the image exactly (NoxStore → Elf Mart) ──────────
    desc = (
        "**General Rules**\n"
        "─────────────────────────────────────\n"
        "<:dot:1546740213316059186> All **deals must be done inside official\n"
        f"{SHOP_NAME} tickets** — any outside deal is **not our\n"
        "responsibility and no warranty.**\n"
        "<:dot:1546740213316059186> We reserve the **right to change this TOS\n"
        "anytime.**\n"
        "<:dot:1546740213316059186> If we are **unable to deliver your order** = full\n"
        "refund will be issued.\n"
        "<:dot:1546740213316059186> Replacement and refund claims are only\n"
        "valid within **24 hours of product delivery.** Any\n"
        "reports made after 24 hours will be rejected.\n"
        "<:dot:1546740213316059186> We are **not responsible for revokes ,\n"
        "disable  of any products** which doesnt have\n"
        "revoke warranty\n"
        f"**Buying From {SHOP_NAME}  = Accepts Our Tos**\n\n"
        "<:info:1546740227706454116> **Select a category below to view specific\n"
        "TOS**"
        if entries else
        "**General Rules**\n"
        "─────────────────────────────────────\n"
        "<:dot:1546740213316059186> All **deals must be done inside official\n"
        f"{SHOP_NAME} tickets** — any outside deal is **not our\n"
        "responsibility and no warranty.**\n"
        "<:dot:1546740213316059186> We reserve the **right to change this TOS\n"
        "anytime.**\n"
        "<:dot:1546740213316059186> If we are **unable to deliver your order** = full\n"
        "refund will be issued.\n"
        "<:dot:1546740213316059186> Replacement and refund claims are only\n"
        "valid within **24 hours of product delivery.** Any\n"
        "reports made after 24 hours will be rejected.\n"
        "<:dot:1546740213316059186> We are **not responsible for revokes ,\n"
        "disable  of any products** which doesnt have\n"
        "revoke warranty\n"
        f"**Buying From {SHOP_NAME}  = Accepts Our Tos**"
    )

    e = discord.Embed(
        title=f"<:rules1:1546740249424826490> {SHOP_NAME} — Terms of Service",
        description=desc,
        color=discord.Color.blurple())
    return e


class TOSDropdown(ui.Select):
    def __init__(self, entries: list[dict]):
        options = []
        for entry in entries[:25]:  # Discord max 25 options
            # emoji field is either a unicode emoji string or None
            entry_emoji = entry.get("emoji") or None
            options.append(discord.SelectOption(
                label=entry["subject"][:100],
                value=entry["subject"][:100],
                emoji=entry_emoji,          # None = no emoji shown; str = shown left of label
            ))
        super().__init__(
            placeholder="Make a selection",
            min_values=1, max_values=1,
            options=options,
            custom_id="tos_dropdown_select"
        )

    async def callback(self, interaction: discord.Interaction):
        subject = self.values[0]
        entry = tos_col.find_one({"guild_id": interaction.guild_id, "subject": subject})
        if not entry:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Found", f"TOS section `{subject}` no longer exists.", discord.Color.red()),
                ephemeral=True)
        e = discord.Embed(
            title=f"📄 {entry['subject']}",
            description=entry["content"],
            color=discord.Color.blurple(),
            timestamp=datetime.datetime.now(datetime.timezone.utc))
        e.set_footer(text="Terms of Service")
        await interaction.response.send_message(embed=e, ephemeral=True)


class TOSView(ui.View):
    def __init__(self, entries: list[dict]):
        super().__init__(timeout=None)
        if entries:
            self.add_item(TOSDropdown(entries))


async def _refresh_tos_panels(guild_id: int):
    """Update all existing TOS panel messages in a guild after add/remove."""
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    entries = _get_tos_entries(guild_id)
    panel_embed = _build_tos_embed(guild)
    panel_view  = TOSView(entries)
    for doc in tos_col.find({"guild_id": guild_id, "panel_message_id": {"$exists": True}}):
        try:
            ch = guild.get_channel(doc.get("panel_channel_id"))
            if not ch:
                continue
            msg = await ch.fetch_message(doc["panel_message_id"])
            await msg.edit(embed=panel_embed, view=panel_view)
        except Exception:
            pass
    # Also update via a separate panels tracking collection key
    for doc in list(tos_col.find({"guild_id": guild_id, "_tos_panel": True})):
        try:
            ch = guild.get_channel(doc.get("channel_id"))
            if not ch:
                continue
            msg = await ch.fetch_message(doc["message_id"])
            await msg.edit(embed=panel_embed, view=panel_view)
        except Exception:
            pass


# We'll track TOS panel messages in the config collection with a unique key
TOS_PANEL_KEY = "tos_panels"  # config field: list of {channel_id, message_id}


async def _refresh_all_tos_panels(guild: discord.Guild):
    """Reload all TOS panel messages from config and re-edit them."""
    cfg = get_config(guild.id)
    panels = cfg.get(TOS_PANEL_KEY, [])
    if not panels:
        return
    entries    = _get_tos_entries(guild.id)
    embed      = _build_tos_embed(guild)
    view       = TOSView(entries)
    for panel in panels:
        try:
            ch = guild.get_channel(panel["channel_id"])
            if not ch:
                continue
            msg = await ch.fetch_message(panel["message_id"])
            await msg.edit(embed=embed, view=view)
        except Exception:
            pass


# ── .tos ──────────────────────────────────────────────────────────────────────

@bot.command(name="tos")
async def tos_panel(ctx):
    """Send the interactive TOS panel with a dropdown.
    Usage: .tos"""
    if not is_mod_check(ctx.author, ctx.guild.id):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "You need mod permissions to post the TOS panel.", discord.Color.red()))

    entries = _get_tos_entries(ctx.guild.id)
    embed   = _build_tos_embed(ctx.guild)
    view    = TOSView(entries)

    msg = await ctx.send(embed=embed, view=view)

    # Save panel reference so we can auto-update it later
    cfg = get_config(ctx.guild.id)
    panels = cfg.get(TOS_PANEL_KEY, [])
    panels.append({"channel_id": ctx.channel.id, "message_id": msg.id})
    set_config(ctx.guild.id, TOS_PANEL_KEY, panels)

    try:
        await ctx.message.delete()
    except Exception:
        pass


# ── .tosadd ───────────────────────────────────────────────────────────────────

@bot.command(name="tosadd")
async def tosadd(ctx, *, args: str = None):
    """Add a TOS section.
    Usage: .tosadd [Subject] [TOS Text]"""
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "Only administrators can add TOS sections.", discord.Color.red()))

    if not args:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.tosadd [Subject] [TOS Text]`\n"
            "**Example:** `.tosadd [Privacy Policy] [We do not collect personal data...]`\n\n"
            "Both subject and text must be wrapped in `[brackets]`.",
            discord.Color.red()))

    matches = _re.findall(r"\[(.*?)\]", args, _re.DOTALL)
    if len(matches) < 2:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Format",
            "**Usage:** `.tosadd [Subject] [TOS Text]`\n"
            "Make sure both the subject **and** the TOS text are wrapped in `[brackets]`.\n\n"
            "**Example:** `.tosadd [Rules] [Be respectful to all members at all times.]`",
            discord.Color.red()))

    subject = matches[0].strip()
    content = matches[1].strip()

    if not subject or not content:
        return await ctx.send(embed=make_embed(
            "❌ Empty Field",
            "Subject and TOS text cannot be empty.", discord.Color.red()))

    if len(subject) > 100:
        return await ctx.send(embed=make_embed(
            "❌ Subject Too Long",
            "Subject must be 100 characters or fewer.", discord.Color.red()))

    if len(content) > 4000:
        return await ctx.send(embed=make_embed(
            "❌ Content Too Long",
            "TOS text must be 4000 characters or fewer.", discord.Color.red()))

    # Check entry limit (Discord dropdown max = 25)
    count = tos_col.count_documents({"guild_id": ctx.guild.id})
    if count >= 25:
        return await ctx.send(embed=make_embed(
            "❌ Limit Reached",
            "You can have a maximum of **25 TOS sections** (Discord dropdown limit).\n"
            "Use `.tosremove [Subject]` to remove one first.",
            discord.Color.red()))

    existing = tos_col.find_one({"guild_id": ctx.guild.id, "subject": subject})
    if existing:
        return await ctx.send(embed=make_embed(
            "⚠️ Already Exists",
            f"A TOS section named **{subject}** already exists.\n"
            f"Use `.tosedit [Subject] [New Text]` to update it, or `.tosremove [{subject}]` to delete it first.",
            discord.Color.orange()))

    # Auto-assign order = next available position
    max_order_doc = tos_col.find_one(
        {"guild_id": ctx.guild.id}, sort=[("order", -1)])
    next_order = (max_order_doc.get("order", 0) + 1) if max_order_doc else 1

    tos_col.insert_one({
        "guild_id": ctx.guild.id,
        "subject": subject,
        "content": content,
        "order": next_order,
        "emoji": None,
        "added_by": ctx.author.id,
        "added_at": datetime.datetime.now(datetime.timezone.utc),
    })

    await ctx.send(embed=make_embed(
        "✅ TOS Section Added",
        f"**Subject:** {subject}\n**Preview:** {content[:200]}{'...' if len(content) > 200 else ''}",
        discord.Color.green(),
        fields=[("📊 Total Sections", str(count + 1), True)]))

    await _refresh_all_tos_panels(ctx.guild)


# ── .tosremove ────────────────────────────────────────────────────────────────

@bot.command(name="tosremove")
async def tosremove(ctx, *, args: str = None):
    """Remove a TOS section.
    Usage: .tosremove [Subject]"""
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "Only administrators can remove TOS sections.", discord.Color.red()))

    if not args:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.tosremove [Subject]`\n"
            "**Example:** `.tosremove [Privacy Policy]`\n\n"
            "Use `.toslist` to see all current subjects.",
            discord.Color.red()))

    matches = _re.findall(r"\[(.*?)\]", args, _re.DOTALL)
    if not matches:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Format",
            "**Usage:** `.tosremove [Subject]`\n"
            "The subject must be wrapped in `[brackets]`.",
            discord.Color.red()))

    subject = matches[0].strip()
    result = tos_col.delete_one({"guild_id": ctx.guild.id, "subject": subject})

    if result.deleted_count == 0:
        entries = _get_tos_entries(ctx.guild.id)
        subjects_list = "\n".join(f"• `{e['subject']}`" for e in entries) or "*(none yet)*"
        return await ctx.send(embed=make_embed(
            "❌ Not Found",
            f"No TOS section named **{subject}** found.\n\n**Available subjects:**\n{subjects_list}",
            discord.Color.red()))

    await ctx.send(embed=make_embed(
        "🗑️ TOS Section Removed",
        f"Section **{subject}** has been removed from the TOS.",
        discord.Color.green()))

    await _refresh_all_tos_panels(ctx.guild)


# ── .tosedit ──────────────────────────────────────────────────────────────────

@bot.command(name="tosedit")
async def tosedit(ctx, *, args: str = None):
    """Edit the content of an existing TOS section.
    Usage: .tosedit [Subject] [New TOS Text]"""
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "Only administrators can edit TOS sections.", discord.Color.red()))

    if not args:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.tosedit [Subject] [New TOS Text]`\n"
            "**Example:** `.tosedit [Rules] [Updated rule text here...]`",
            discord.Color.red()))

    matches = _re.findall(r"\[(.*?)\]", args, _re.DOTALL)
    if len(matches) < 2:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Format",
            "**Usage:** `.tosedit [Subject] [New TOS Text]`\n"
            "Both subject and new text must be wrapped in `[brackets]`.",
            discord.Color.red()))

    subject = matches[0].strip()
    new_content = matches[1].strip()

    if not new_content:
        return await ctx.send(embed=make_embed(
            "❌ Empty Content", "The new TOS text cannot be empty.", discord.Color.red()))

    if len(new_content) > 4000:
        return await ctx.send(embed=make_embed(
            "❌ Content Too Long", "TOS text must be 4000 characters or fewer.", discord.Color.red()))

    result = tos_col.update_one(
        {"guild_id": ctx.guild.id, "subject": subject},
        {"$set": {"content": new_content,
                  "edited_by": ctx.author.id,
                  "edited_at": datetime.datetime.now(datetime.timezone.utc)}})

    if result.matched_count == 0:
        return await ctx.send(embed=make_embed(
            "❌ Not Found",
            f"No TOS section named **{subject}** found.\n"
            "Use `.toslist` to see all existing subjects.",
            discord.Color.red()))

    await ctx.send(embed=make_embed(
        "✏️ TOS Section Updated",
        f"**Subject:** {subject}\n**New Content Preview:** {new_content[:200]}{'...' if len(new_content) > 200 else ''}",
        discord.Color.green()))

    await _refresh_all_tos_panels(ctx.guild)


# ── .toslist ──────────────────────────────────────────────────────────────────

@bot.command(name="toslist")
async def toslist(ctx):
    """List all TOS sections for this server.
    Usage: .toslist"""
    entries = _get_tos_entries(ctx.guild.id)
    if not entries:
        return await ctx.send(embed=make_embed(
            "📜 No TOS Sections",
            "No TOS sections have been added yet.\n"
            "Use `.tosadd [Subject] [TOS Text]` to add the first one.",
            discord.Color.blurple()))

    lines = [
        f"**{i+1}.** {'`' + e.get('emoji','') + '` ' if e.get('emoji') else ''}`{e['subject']}`"
        for i, e in enumerate(entries)
    ]
    await ctx.send(embed=make_embed(
        f"📜 TOS Sections — {len(entries)}/25",
        "\n".join(lines),
        discord.Color.blurple(),
        fields=[
            ("➕ Add", "`.tosadd [Subject] [Text]`", True),
            ("✏️ Edit", "`.tosedit [Subject] [Text]`", True),
            ("🗑️ Remove", "`.tosremove [Subject]`", True),
            ("🔢 Reorder", "`.tosreorder [Sub1] [Sub2] ...`", True),
            ("😀 Set Emoji", "`.tosemoji [Subject] [emoji]`", True),
        ]))


# ── .tosview ──────────────────────────────────────────────────────────────────

@bot.command(name="tosview")
async def tosview(ctx, *, args: str = None):
    """View the full content of a specific TOS section.
    Usage: .tosview [Subject]"""
    if not args:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.tosview [Subject]`\n"
            "**Example:** `.tosview [Privacy Policy]`\n"
            "Use `.toslist` to see all subjects.",
            discord.Color.red()))

    matches = _re.findall(r"\[(.*?)\]", args, _re.DOTALL)
    subject = matches[0].strip() if matches else args.strip()

    entry = tos_col.find_one({"guild_id": ctx.guild.id, "subject": subject})
    if not entry:
        return await ctx.send(embed=make_embed(
            "❌ Not Found",
            f"No TOS section named **{subject}** found.\n"
            "Use `.toslist` to view all sections.",
            discord.Color.red()))

    e = discord.Embed(
        title=f"📄 {entry['subject']}",
        description=entry["content"],
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.set_footer(text="Terms of Service")
    await ctx.send(embed=e)




# ── .tosorder ─────────────────────────────────────────────────────────────────

@bot.command(name="tosorder")
async def tosorder(ctx):
    """Shows the current TOS section order pre-formatted for use with .tosreorder."""
    entries = _get_tos_entries(ctx.guild.id)
    if not entries:
        return await ctx.send(embed=make_embed(
            "📜 No TOS Sections",
            "No TOS sections have been added yet. Use `.tosadd` to add one.",
            discord.Color.blurple()))

    formatted = " ".join(f"[{e['subject']}]" for e in entries)
    numbered  = "\n".join(f"**{i+1}.** `{e['subject']}`" for i, e in enumerate(entries))

    await ctx.send(embed=make_embed(
        "📋 Current TOS Order",
        f"**Current order:**\n{numbered}\n\n"
        f"**Ready-to-use for `.tosreorder`:**\n```\n.tosreorder {formatted}\n```",
        discord.Color.blurple()))


# ── .tosreorder ───────────────────────────────────────────────────────────────

@bot.command(name="tosreorder")
async def tosreorder(ctx, *, args: str = None):
    """Reorder TOS sections by listing subjects in the new order.
    Usage: .tosreorder [Subject1] [Subject2] [Subject3] ...
    Example: .tosreorder [Rules] [Privacy Policy] [Refunds]
    Any subjects not listed will be appended at the end in their current order."""
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "Only administrators can reorder TOS sections.", discord.Color.red()))

    if not args:
        entries = _get_tos_entries(ctx.guild.id)
        current = "\n".join(f"**{i+1}.** `{e['subject']}`" for i, e in enumerate(entries)) or "*(none)*"
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.tosreorder [Subject1] [Subject2] [Subject3] ...`\n"
            "List every subject in the new order, each wrapped in `[brackets]`.\n\n"
            f"**Current order:**\n{current}",
            discord.Color.red()))

    new_order_subjects = [s.strip() for s in _re.findall(r"\[(.*?)\]", args, _re.DOTALL) if s.strip()]
    if not new_order_subjects:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Format",
            "Wrap each subject in `[brackets]`. Example:\n"
            "`.tosreorder [Rules] [Privacy Policy] [Refunds]`",
            discord.Color.red()))

    entries = _get_tos_entries(ctx.guild.id)
    all_subjects = {e["subject"]: e for e in entries}

    # Validate — every listed subject must exist
    not_found = [s for s in new_order_subjects if s not in all_subjects]
    if not_found:
        return await ctx.send(embed=make_embed(
            "❌ Subject(s) Not Found",
            "These subjects don't exist:\n" + "\n".join(f"• `{s}`" for s in not_found) +
            "\n\nUse `.toslist` to see valid subjects.",
            discord.Color.red()))

    # Deduplicate while preserving order
    seen = set()
    ordered = []
    for s in new_order_subjects:
        if s not in seen:
            seen.add(s)
            ordered.append(s)

    # Append any subjects not mentioned, keeping their relative order
    for e in entries:
        if e["subject"] not in seen:
            ordered.append(e["subject"])

    # Write new order values to DB
    for i, subject in enumerate(ordered, start=1):
        tos_col.update_one(
            {"guild_id": ctx.guild.id, "subject": subject},
            {"$set": {"order": i}})

    # Build confirmation
    new_entries = _get_tos_entries(ctx.guild.id)
    lines = [
        f"**{i+1}.** {'`' + e.get('emoji','') + '` ' if e.get('emoji') else ''}`{e['subject']}`"
        for i, e in enumerate(new_entries)
    ]
    await ctx.send(embed=make_embed(
        "🔢 TOS Reordered",
        "New order:\n" + "\n".join(lines),
        discord.Color.green()))

    await _refresh_all_tos_panels(ctx.guild)


# ── .tosemoji ─────────────────────────────────────────────────────────────────

@bot.command(name="tosemoji")
async def tosemoji(ctx, *, args: str = None):
    """Set or remove the emoji shown next to a TOS subject in the dropdown.
    Usage: .tosemoji [Subject] [emoji]
    To remove the emoji: .tosemoji [Subject] [none]
    Example: .tosemoji [Rules] [📋]"""
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "Only administrators can set TOS emojis.", discord.Color.red()))

    if not args:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.tosemoji [Subject] [emoji]`\n"
            "**Example:** `.tosemoji [Rules] [📋]`\n"
            "To remove an emoji: `.tosemoji [Rules] [none]`\n\n"
            "Both standard Unicode emojis and custom server emojis are supported.",
            discord.Color.red()))

    matches = _re.findall(r"\[(.*?)\]", args, _re.DOTALL)
    if len(matches) < 2:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Format",
            "**Usage:** `.tosemoji [Subject] [emoji]`\n"
            "Both subject and emoji must be wrapped in `[brackets]`.",
            discord.Color.red()))

    subject   = matches[0].strip()
    raw_emoji = matches[1].strip()

    entry = tos_col.find_one({"guild_id": ctx.guild.id, "subject": subject})
    if not entry:
        entries = _get_tos_entries(ctx.guild.id)
        subjects_list = "\n".join(f"• `{e['subject']}`" for e in entries) or "*(none yet)*"
        return await ctx.send(embed=make_embed(
            "❌ Subject Not Found",
            f"No TOS section named **{subject}**.\n\n**Available subjects:**\n{subjects_list}",
            discord.Color.red()))

    # "none" / "remove" / "clear" = delete the emoji
    if raw_emoji.lower() in ("none", "remove", "clear", "-"):
        tos_col.update_one(
            {"guild_id": ctx.guild.id, "subject": subject},
            {"$set": {"emoji": None}})
        await ctx.send(embed=make_embed(
            "✅ Emoji Removed",
            f"Emoji cleared from **{subject}** in the TOS dropdown.",
            discord.Color.green()))
        return await _refresh_all_tos_panels(ctx.guild)

    # Validate: accept a Unicode emoji or a custom emoji mention (<:name:id> / <a:name:id>)
    unicode_emoji_pattern = _re.compile(
        r"[\U0001F000-\U0001FFFF"
        r"\U00002600-\U000027BF"
        r"\U0000FE00-\U0000FE0F"
        r"\U00002702-\U000027B0"
        r"\u2194-\u2199"
        r"\u231A-\u231B"
        r"\u23E9-\u23F3"
        r"\u25AA-\u25FE"
        r"\u2600-\u26FF"
        r"\u2702-\u27B0]+",
        _re.UNICODE)

    custom_emoji_pattern = _re.compile(r"<a?:[a-zA-Z0-9_]+:\d+>")

    is_unicode = bool(unicode_emoji_pattern.search(raw_emoji))
    is_custom  = bool(custom_emoji_pattern.match(raw_emoji.strip()))

    if not is_unicode and not is_custom:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Emoji",
            "Please provide a valid emoji.\n"
            "• Unicode emoji: `📋` `✅` `⚠️`\n"
            "• Custom emoji from this server: `<:name:id>`\n"
            "• To clear: `[none]`",
            discord.Color.red()))

    # For custom emojis, verify the emoji belongs to this guild (Discord only allows guild emojis)
    if is_custom:
        m = _re.match(r"<a?:([a-zA-Z0-9_]+):(\d+)>", raw_emoji.strip())
        if m:
            emoji_id = int(m.group(2))
            guild_emoji = discord.utils.get(ctx.guild.emojis, id=emoji_id)
            if not guild_emoji:
                return await ctx.send(embed=make_embed(
                    "❌ Custom Emoji Not in This Server",
                    "Discord only allows emojis **from this server** to be used in dropdowns.\n"
                    "Use a Unicode emoji instead, or add the emoji to this server first with `.addemoji`.",
                    discord.Color.red()))
            # Store the full mention string — discord.py resolves it from the guild
            emoji_value = raw_emoji.strip()
        else:
            emoji_value = raw_emoji.strip()
    else:
        # Extract just the emoji character(s)
        emoji_value = unicode_emoji_pattern.search(raw_emoji).group(0)

    tos_col.update_one(
        {"guild_id": ctx.guild.id, "subject": subject},
        {"$set": {"emoji": emoji_value}})

    await ctx.send(embed=make_embed(
        "😀 TOS Emoji Set",
        f"Emoji for **{subject}** → {emoji_value}\n"
        "It will now appear in the dropdown next to the subject name.",
        discord.Color.green()))

    await _refresh_all_tos_panels(ctx.guild)


# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOMER PANEL & SELLER INVOICE VERIFICATION SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

customer_verifications_col = mod_db["customer_verifications"]
customer_verifications_col.create_index([("guild_id", 1), ("user_id", 1), ("status", 1)])
customer_verifications_col.create_index("request_id", unique=True)
customer_audit_col = mod_db["customer_verification_audit"]

CUSTOMER_PANEL_ENTER_INVOICE_CUSTOM_ID = "customer_panel_enter_invoice"

CUSTOMER_PANEL_TITLE = f"## __**Purchased from {SHOP_NAME} on SellAuth? Get your Customer Role! 🎉**__"
CUSTOMER_PANEL_DESCRIPTION = (
    f"Simply enter the **SellAuth Invoice ID** of any purchase made through **{SHOP_NAME}**. "
    "Once your purchase is successfully verified, you\u2019ll automatically receive the "
    "Customer Role and gain access to customer benefits! ✨"
)

CUSTOMER_NOT_CONFIGURED_MSG = (
    "❌ Customer verification is not configured yet. Please ask an administrator to configure "
    "the Customer role, verification channel, and seller permissions."
)


# ── Config helpers ─────────────────────────────────────────────────────────────

def get_customer_config(guild_id: int) -> dict:
    cfg = get_config(guild_id)
    return {
        "customer_role_id": cfg.get("customer_role"),
        "verify_channel_id": cfg.get("customer_verify_channel"),
        "seller_role_ids": cfg.get("customer_seller_roles") or [],
        "log_channel_id": cfg.get("customer_verify_log_channel"),
    }


def is_customer_config_complete(cfg: dict) -> bool:
    return bool(cfg["customer_role_id"] and cfg["verify_channel_id"] and cfg["seller_role_ids"])


def is_customer_seller(member: discord.Member, guild_id: int) -> bool:
    if member.guild_permissions.administrator:
        return True
    cfg = get_customer_config(guild_id)
    member_role_ids = {r.id for r in member.roles}
    return any(int(rid) in member_role_ids for rid in cfg["seller_role_ids"])


def member_has_customer_role(member: discord.Member, guild_id: int) -> bool:
    cfg = get_customer_config(guild_id)
    role_id = cfg["customer_role_id"]
    if not role_id:
        return False
    return any(r.id == int(role_id) for r in member.roles)


def _build_verification_embed(member_display: str, user_id: int, invoice: str,
                               status: str, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title="🧾 Customer Verification Request", color=color,
                           timestamp=datetime.datetime.now(datetime.timezone.utc))
    embed.add_field(name="Customer", value=member_display, inline=True)
    embed.add_field(name="User ID", value=str(user_id), inline=True)
    embed.add_field(name="Invoice", value=f"```{invoice[:1000]}```", inline=False)
    embed.add_field(name="Status", value=status, inline=False)
    embed.set_footer(text="🔒 Approve / Decline buttons are for sellers & admins only.")
    return embed


def _disabled_verification_view(request_id: str) -> ui.View:
    view = ui.View(timeout=None)
    view.add_item(ui.Button(label="Approve", emoji="✅", style=discord.ButtonStyle.success,
                             custom_id=f"customer_verify_approve_done:{request_id}", disabled=True))
    view.add_item(ui.Button(label="Decline", emoji="❌", style=discord.ButtonStyle.danger,
                             custom_id=f"customer_verify_decline_done:{request_id}", disabled=True))
    return view


async def _send_customer_audit_log(guild: discord.Guild, action: str, request_doc: dict,
                                    seller: discord.abc.User, timestamp: datetime.datetime,
                                    request_id: str):
    customer_audit_col.insert_one({
        "guild_id": guild.id, "request_id": request_id, "action": action,
        "user_id": request_doc["user_id"], "invoice": request_doc.get("invoice", ""),
        "processed_by": seller.id, "processed_by_name": str(seller), "timestamp": timestamp,
    })
    cfg = get_customer_config(guild.id)
    log_channel_id = cfg.get("log_channel_id")
    if not log_channel_id:
        return
    ch = guild.get_channel(int(log_channel_id))
    if not ch:
        return
    embed = discord.Embed(
        title=f"📋 Customer Verification {action}",
        color=discord.Color.green() if action == "Approved" else discord.Color.red(),
        timestamp=timestamp)
    embed.add_field(name="Action", value=action, inline=True)
    embed.add_field(name="Customer", value=f"<@{request_doc['user_id']}> ({request_doc['user_id']})", inline=True)
    embed.add_field(name="Invoice", value=f"```{str(request_doc.get('invoice', ''))[:500]}```", inline=False)
    embed.add_field(name="Processed By", value=f"{seller.mention} ({seller.id})", inline=True)
    embed.add_field(name="Request ID", value=request_id, inline=True)
    try:
        await ch.send(embed=embed)
    except Exception:
        pass


# ── Modal: invoice submission ───────────────────────────────────────────────────

class CustomerInvoiceModal(ui.Modal, title="SellAuth Invoice Verification"):
    invoice = ui.TextInput(
        label="SellAuth Invoice",
        placeholder="Enter/paste your SellAuth invoice or invoice ID.",
        style=discord.TextStyle.short,
        required=True,
        max_length=300,
    )

    def __init__(self):
        super().__init__(timeout=300)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)

        invoice_value = self.invoice.value.strip()
        if not invoice_value:
            return await interaction.response.send_message(
                "❌ Your invoice cannot be empty. Please try again.", ephemeral=True)

        cfg = get_customer_config(guild.id)
        if not is_customer_config_complete(cfg):
            return await interaction.response.send_message(CUSTOMER_NOT_CONFIGURED_MSG, ephemeral=True)

        member = interaction.user
        if member_has_customer_role(member, guild.id):
            return await interaction.response.send_message(
                "❌ You already have the Customer role. You don't need to submit another invoice.",
                ephemeral=True)

        # Atomically create a pending request only if the user doesn't already have one.
        request_id = uuid.uuid4().hex
        now = datetime.datetime.now(datetime.timezone.utc)
        pre_existing = customer_verifications_col.find_one_and_update(
            {"guild_id": guild.id, "user_id": member.id, "status": {"$in": ["pending", "processing"]}},
            {"$setOnInsert": {
                "request_id": request_id,
                "guild_id": guild.id,
                "user_id": member.id,
                "invoice": invoice_value,
                "status": "pending",
                "submitted_at": now,
                "processed_by": None,
                "processed_by_name": None,
                "processed_at": None,
                "message_id": None,
                "channel_id": None,
            }},
            upsert=True,
        )
        if pre_existing is not None:
            return await interaction.response.send_message(
                "⏳ You already have a pending invoice verification request. Please wait for a seller to review it.",
                ephemeral=True)

        doc = customer_verifications_col.find_one({"guild_id": guild.id, "user_id": member.id, "status": "pending"})
        if doc is None:
            return await interaction.response.send_message(
                "❌ Something went wrong creating your request. Please try again.", ephemeral=True)
        request_id = doc["request_id"]

        verify_channel = guild.get_channel(int(cfg["verify_channel_id"]))
        if verify_channel is None:
            customer_verifications_col.delete_one({"request_id": request_id})
            return await interaction.response.send_message(
                "❌ The verification channel is missing or inaccessible. Please contact an administrator.",
                ephemeral=True)

        embed = _build_verification_embed(member.mention, member.id, invoice_value,
                                           "🕓 Pending Review", discord.Color.gold())
        view = CustomerVerificationView(request_id)
        try:
            msg = await verify_channel.send(embed=embed, view=view)
        except Exception:
            customer_verifications_col.delete_one({"request_id": request_id})
            return await interaction.response.send_message(
                "❌ Failed to submit your invoice for review. Please try again later or contact an administrator.",
                ephemeral=True)

        customer_verifications_col.update_one(
            {"request_id": request_id},
            {"$set": {"message_id": msg.id, "channel_id": msg.channel.id}})

        await interaction.response.send_message(
            "✅ Your invoice has been submitted for review. You'll be notified once a seller processes it.",
            ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        try:
            if interaction.response.is_done():
                await interaction.followup.send("❌ Something went wrong submitting your invoice.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Something went wrong submitting your invoice.", ephemeral=True)
        except Exception:
            pass


# ── View: per-request Approve / Decline buttons (seller-only) ─────────────────

class CustomerVerificationView(ui.View):
    """Persistent view for a single verification request. custom_ids embed the
    request_id so the view can be recreated (e.g. after a bot restart) with
    bot.add_view(CustomerVerificationView(request_id))."""

    def __init__(self, request_id: str):
        super().__init__(timeout=None)
        self.request_id = request_id

        approve_btn = ui.Button(label="Approve", emoji="✅", style=discord.ButtonStyle.success,
                                 custom_id=f"customer_verify_approve:{request_id}")
        approve_btn.callback = self.approve
        decline_btn = ui.Button(label="Decline", emoji="❌", style=discord.ButtonStyle.danger,
                                 custom_id=f"customer_verify_decline:{request_id}")
        decline_btn.callback = self.decline
        self.add_item(approve_btn)
        self.add_item(decline_btn)

    async def approve(self, interaction: discord.Interaction):
        await self._process(interaction, approve=True)

    async def decline(self, interaction: discord.Interaction):
        await self._process(interaction, approve=False)

    async def _already_processed(self, interaction: discord.Interaction):
        fresh = customer_verifications_col.find_one({"request_id": self.request_id})
        if fresh and fresh.get("status") == "processing":
            msg = "⏳ This request is currently being processed by another seller. Please wait a moment."
        else:
            msg = "❌ This request has already been processed."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def _process(self, interaction: discord.Interaction, approve: bool):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)

        if not is_customer_seller(interaction.user, guild.id):
            return await interaction.response.send_message(
                "❌ You don't have permission to process customer verification requests.", ephemeral=True)

        request_id = self.request_id
        now = datetime.datetime.now(datetime.timezone.utc)

        if not approve:
            claimed = customer_verifications_col.find_one_and_update(
                {"request_id": request_id, "status": "pending"},
                {"$set": {"status": "declined", "processed_by": interaction.user.id,
                          "processed_by_name": str(interaction.user), "processed_at": now}})
            if claimed is None:
                return await self._already_processed(interaction)

            status_text = f"❌ Declined by {interaction.user.mention}"
            embed = _build_verification_embed(f"<@{claimed['user_id']}>", claimed["user_id"],
                                               claimed.get("invoice", ""), status_text, discord.Color.red())
            try:
                await interaction.response.edit_message(embed=embed, view=_disabled_verification_view(request_id))
            except Exception:
                pass

            await _send_customer_audit_log(guild, "Declined", claimed, interaction.user, now, request_id)

            try:
                member = guild.get_member(claimed["user_id"]) or await guild.fetch_member(claimed["user_id"])
                await member.send(
                    "❌ Your invoice verification request was declined. Please contact a seller if you believe this was a mistake.")
            except Exception:
                pass
            return

        # ── Approve path: lock the request first so two sellers can't process it at once ──
        claimed = customer_verifications_col.find_one_and_update(
            {"request_id": request_id, "status": "pending"},
            {"$set": {"status": "processing", "processing_by": interaction.user.id}})
        if claimed is None:
            return await self._already_processed(interaction)

        await interaction.response.defer()

        def _revert(reason):
            customer_verifications_col.update_one(
                {"request_id": request_id},
                {"$set": {"status": "pending"}, "$unset": {"processing_by": ""}})
            return reason

        cfg = get_customer_config(guild.id)
        role_id = cfg["customer_role_id"]
        role = guild.get_role(int(role_id)) if role_id else None
        member = guild.get_member(claimed["user_id"])
        if member is None:
            try:
                member = await guild.fetch_member(claimed["user_id"])
            except Exception:
                member = None

        error_msg = None
        if role is None:
            error_msg = _revert("❌ The configured Customer role no longer exists. Please reconfigure it with `/customerconfig`. Request kept pending.")
        elif member is None:
            error_msg = _revert("❌ That member could not be found in this server (they may have left). Request kept pending.")
        elif not guild.me.guild_permissions.manage_roles:
            error_msg = _revert("❌ I don't have permission to manage roles. Request kept pending.")
        elif role >= guild.me.top_role:
            error_msg = _revert("❌ My role is not high enough to assign the Customer role. Please move my role above it. Request kept pending.")

        if error_msg:
            return await interaction.followup.send(error_msg, ephemeral=True)

        try:
            await member.add_roles(role, reason=f"Customer verification approved by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            return await interaction.followup.send(
                _revert("❌ I don't have permission to assign that role. Request kept pending."), ephemeral=True)
        except Exception:
            return await interaction.followup.send(
                _revert("❌ Something went wrong assigning the role. Request kept pending."), ephemeral=True)

        customer_verifications_col.update_one(
            {"request_id": request_id},
            {"$set": {"status": "approved", "processed_by": interaction.user.id,
                      "processed_by_name": str(interaction.user), "processed_at": now},
             "$unset": {"processing_by": ""}})

        status_text = f"✅ Approved by {interaction.user.mention}"
        embed = _build_verification_embed(f"<@{claimed['user_id']}>", claimed["user_id"],
                                           claimed.get("invoice", ""), status_text, discord.Color.green())
        try:
            await interaction.edit_original_response(embed=embed, view=_disabled_verification_view(request_id))
        except Exception:
            pass

        await _send_customer_audit_log(guild, "Approved", claimed, interaction.user, now, request_id)

        try:
            await member.send("✅ Your invoice has been approved! You have been given the Customer role.")
        except Exception:
            pass


# ── Components V2 Customer Panel ────────────────────────────────────────────────

async def handle_customer_panel_enter_invoice(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)

    cfg = get_customer_config(guild.id)
    if not is_customer_config_complete(cfg):
        return await interaction.response.send_message(CUSTOMER_NOT_CONFIGURED_MSG, ephemeral=True)

    if member_has_customer_role(interaction.user, guild.id):
        return await interaction.response.send_message(
            "❌ You already have the Customer role. You don't need to submit another invoice.", ephemeral=True)

    existing_pending = customer_verifications_col.find_one(
        {"guild_id": guild.id, "user_id": interaction.user.id, "status": {"$in": ["pending", "processing"]}})
    if existing_pending:
        return await interaction.response.send_message(
            "⏳ You already have a pending invoice verification request. Please wait for a seller to review it.",
            ephemeral=True)

    await interaction.response.send_modal(CustomerInvoiceModal())


class CustomerPanelContainer(ui.Container):
    header = ui.TextDisplay(CUSTOMER_PANEL_TITLE)
    body = ui.TextDisplay(CUSTOMER_PANEL_DESCRIPTION)
    action_row = ui.ActionRow()

    def __init__(self):
        super().__init__(accent_colour=discord.Color.blurple())

    @action_row.button(label="Enter Invoice", emoji="🧾", style=discord.ButtonStyle.primary,
                        custom_id=CUSTOMER_PANEL_ENTER_INVOICE_CUSTOM_ID)
    async def enter_invoice_button(self, interaction: discord.Interaction, button: ui.Button):
        # Only the clicking user is ever prompted — the modal it opens is scoped
        # to interaction.user, so no separate "other user" guard is needed here.
        await handle_customer_panel_enter_invoice(interaction)


class CustomerPanelView(ui.LayoutView):
    container = CustomerPanelContainer()

    def __init__(self):
        super().__init__(timeout=None)


async def _send_customer_panel(target):
    """target: commands.Context (prefix) or discord.Interaction (slash)."""
    guild = target.guild
    if guild is None:
        return

    cfg = get_customer_config(guild.id)
    if not is_customer_config_complete(cfg):
        if isinstance(target, discord.Interaction):
            await target.response.send_message(CUSTOMER_NOT_CONFIGURED_MSG, ephemeral=True)
        else:
            await target.send(CUSTOMER_NOT_CONFIGURED_MSG)
        return

    view = CustomerPanelView()
    if isinstance(target, discord.Interaction):
        await target.response.send_message(view=view)
    else:
        await target.send(view=view)
        try:
            await target.message.delete()
        except Exception:
            pass


def _customer_panel_permitted(user, guild_id) -> bool:
    if isinstance(user, discord.Member) and user.guild_permissions.administrator:
        return True
    return is_mod_check(user, guild_id) or is_shop_admin_check(user, guild_id)


@bot.command(name="customerpanel")
async def customerpanel_cmd(ctx):
    if not _customer_panel_permitted(ctx.author, ctx.guild.id):
        return await ctx.send("❌ You don't have permission.")
    await _send_customer_panel(ctx)


# NOTE: the standalone "/customerpanel" slash command was merged into the
# single "/panel" dropdown (see PanelPickerView / _post_customer_panel_from_picker)
# to free up global command slots. The ".customerpanel" prefix command above
# still works exactly as before.


# ── /customerconfig — configuration command ────────────────────────────────────

CUSTOMER_CONFIG_ACTIONS = [
    app_commands.Choice(name="Set Customer Role", value="set_customer_role"),
    app_commands.Choice(name="Set Verification Channel", value="set_verify_channel"),
    app_commands.Choice(name="Set Seller Role", value="set_seller_role"),
    app_commands.Choice(name="Set Log Channel (optional)", value="set_log_channel"),
    app_commands.Choice(name="View Configuration", value="view"),
    app_commands.Choice(name="Reset Configuration", value="reset"),
]


@app_commands.command(name="customerconfig", description="Configure the Customer Panel invoice verification system")
@app_commands.describe(
    action="What setting to change",
    role="Role to set (for Set Customer Role / Set Seller Role)",
    channel="Channel to set (for Set Verification Channel / Set Log Channel)",
)
@app_commands.choices(action=CUSTOMER_CONFIG_ACTIONS)
async def customerconfig(inter: discord.Interaction, action: app_commands.Choice[str],
                          role: discord.Role = None, channel: discord.TextChannel = None):
    if not inter.user.guild_permissions.administrator:
        return await inter.response.send_message(
            "❌ Only administrators can configure customer verification.", ephemeral=True)

    guild_id = inter.guild_id
    act = action.value

    if act == "set_customer_role":
        if role is None:
            return await inter.response.send_message("❌ Please provide a role for this action.", ephemeral=True)
        set_config(guild_id, "customer_role", role.id)
        return await inter.response.send_message(f"✅ Customer role set to {role.mention}.", ephemeral=True)

    if act == "set_verify_channel":
        if channel is None:
            return await inter.response.send_message("❌ Please provide a text channel for this action.", ephemeral=True)
        set_config(guild_id, "customer_verify_channel", channel.id)
        return await inter.response.send_message(f"✅ Verification channel set to {channel.mention}.", ephemeral=True)

    if act == "set_seller_role":
        if role is None:
            return await inter.response.send_message("❌ Please provide a role for this action.", ephemeral=True)
        config_col.update_one({"guild_id": guild_id}, {"$addToSet": {"customer_seller_roles": role.id}}, upsert=True)
        return await inter.response.send_message(f"✅ {role.mention} added as an authorized seller role.", ephemeral=True)

    if act == "set_log_channel":
        if channel is None:
            return await inter.response.send_message("❌ Please provide a text channel for this action.", ephemeral=True)
        set_config(guild_id, "customer_verify_log_channel", channel.id)
        return await inter.response.send_message(f"✅ Audit log channel set to {channel.mention}.", ephemeral=True)

    if act == "view":
        cfg = get_customer_config(guild_id)
        role_obj = inter.guild.get_role(int(cfg["customer_role_id"])) if cfg["customer_role_id"] else None
        ch_obj = inter.guild.get_channel(int(cfg["verify_channel_id"])) if cfg["verify_channel_id"] else None
        log_ch_obj = inter.guild.get_channel(int(cfg["log_channel_id"])) if cfg["log_channel_id"] else None
        seller_roles = [inter.guild.get_role(int(rid)) for rid in cfg["seller_role_ids"]]
        seller_roles_text = ", ".join(r.mention for r in seller_roles if r) or "*Not set*"
        embed = make_embed(
            title="🧾 Customer Verification Configuration",
            description=(
                f"**Customer Role:** {role_obj.mention if role_obj else '*Not set*'}\n"
                f"**Verification Channel:** {ch_obj.mention if ch_obj else '*Not set*'}\n"
                f"**Seller Roles:** {seller_roles_text}\n"
                f"**Audit Log Channel:** {log_ch_obj.mention if log_ch_obj else '*Not set (optional)*'}"
            ),
            color=discord.Color.blurple())
        return await inter.response.send_message(embed=embed, ephemeral=True)

    if act == "reset":
        config_col.update_one({"guild_id": guild_id}, {"$unset": {
            "customer_role": "", "customer_verify_channel": "",
            "customer_seller_roles": "", "customer_verify_log_channel": ""}})
        return await inter.response.send_message("✅ Customer verification configuration has been reset.", ephemeral=True)


# ── Guild-scoped registration for the remaining custom slash command ──────────
# NOTE: This bot.py already defines many @bot.tree.command global slash commands,
# close to Discord's hard cap of 100. Registering /customerconfig the normal
# global way (@bot.tree.command) would raise CommandLimitReached at import
# time and crash the whole bot. Instead it's defined above as a standalone
# app_commands.Command object (via @app_commands.command, not @bot.tree.command)
# and is added + synced per-guild here, which uses each guild's own separate
# 100-command budget instead of the shared global one.
# (/customerpanel itself was merged into the global /panel dropdown, so it's
# no longer part of this per-guild registration list.)
CUSTOMER_PANEL_GUILD_COMMANDS = (customerconfig,)


async def register_customer_panel_guild_commands(guild: discord.Guild):
    for cmd in CUSTOMER_PANEL_GUILD_COMMANDS:
        try:
            bot.tree.add_command(cmd, guild=guild, override=True)
        except Exception as e:
            print(f"   ⚠️ Could not register {cmd.name} for guild {guild.id}: {e}")
    try:
        await bot.tree.sync(guild=guild)
    except Exception as e:
        print(f"   ⚠️ Could not sync customer panel commands for guild {guild.id}: {e}")


app = Flask("")

@app.route("/")
def home():
    return "✅ Bot is running 24/7"

@app.route("/sellauth-webhook", methods=["POST"])
def sellauth_webhook():
    """
    Receives SellAuth's HTTP Notification (lightweight — just an event name
    + invoice_id), verifies the signature, then hands off to the bot's async
    event loop to fetch the full invoice and post the order-complete embed.
    Configure this URL (https://your-host/sellauth-webhook) under
    SellAuth Dashboard → Notifications → HTTP Notifications.
    """
    if not SELLAUTH_ENABLED:
        # SellAuth env vars not set — integration is off, bot keeps running fine.
        return ("SellAuth integration not configured, ignoring", 200)

    raw_body = flask_request.get_data()  # raw bytes, required for HMAC

    if SELLAUTH_WEBHOOK_SECRET:
        signature = flask_request.headers.get("X-Signature", "")
        expected = hmac.new(SELLAUTH_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return ("Invalid signature", 401)
    # else: no secret configured — signature check skipped (not recommended,
    # anyone with the URL can trigger fake orders). Set SELLAUTH_WEBHOOK_SECRET
    # to re-enable verification.

    try:
        payload = flask_request.get_json(force=True) or {}
    except Exception:
        return ("Invalid JSON", 400)

    event = payload.get("event", "")
    data = payload.get("data") or {}
    feedback_id = data.get("feedback_id")
    webhook_invoice_id = data.get("invoice_id")  # may be present alongside feedback_id too
    invoice_id = webhook_invoice_id or (data.get("id") if not feedback_id else None)

    # Log every incoming event so you can see the real event name/shape in
    # your console/host logs (e.g. Railway logs) if something isn't firing.
    print(f"📩 SellAuth webhook received — event: {event!r}, data: {data!r}")

    # Don't hard-filter by exact event-name strings (SellAuth's wording
    # varies) — route based on which ID is present instead. A feedback
    # notification carries a feedback_id; an order/invoice notification
    # carries an invoice_id.
    if bot.loop and bot.loop.is_running():
        if feedback_id or "FEEDBACK" in event.upper():
            fid = feedback_id or data.get("id")
            if fid:
                asyncio.run_coroutine_threadsafe(
                    process_sellauth_feedback(fid, fallback_invoice_id=webhook_invoice_id), bot.loop
                )
            else:
                print(f"⚠️ Feedback-looking event had no usable id, ignoring: {payload}")
        elif invoice_id:
            asyncio.run_coroutine_threadsafe(process_sellauth_invoice(invoice_id), bot.loop)
        else:
            print(f"⚠️ SellAuth webhook payload had no recognizable id, ignoring: {payload}")

    return ("OK", 200)

def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

threading.Thread(target=run_flask, daemon=True).start()

# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN (or TOKEN) environment variable not set.")
    bot.run(TOKEN)
