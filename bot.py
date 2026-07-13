import asyncio
import json
import os
import re
import random
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import parse_qs

import discord
from discord.ext import commands, tasks

from voice_rpc import select_voice_channel, leave_voice_channel, VoiceRPCError
from twitch_api import set_stream_title, TwitchAPIError

TOKEN = (os.environ.get("DISCORD_TOKEN", "") or "").strip()


def _parse_id_set(env_name: str) -> set:
    raw = (os.environ.get(env_name, "") or "").strip()
    if not raw:
        return set()
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out


GUILD_IDS = _parse_id_set("DISCORD_GUILD_ID")
CHANNEL_IDS = _parse_id_set("DISCORD_CHANNEL_ID")
TWITCH_URL = (os.environ.get("TWITCH_URL", "") or "").strip() or "https://www.twitch.tv/unrealtournamentstream"
WEBSITE_URL = (os.environ.get("WEBSITE_URL", "") or "").strip() or "https://zkyp.nl/UT"

STOP_USER_IDS = _parse_id_set("STOP_USER_IDS")
BLACKLIST_USER_IDS = _parse_id_set("BLACKLIST_USER_IDS")

C00LBOT2_ID = int(os.environ.get("C00LBOT2_USER_ID", "910934089828274227") or "0")

BLACKLIST_DRAMA = [
    "Nope.",
    "Not for you.",
    "Go away.",
    "No noobs.",
    "Access denied. Cope.",
]

STATE_DIR = Path(os.environ.get("UTSTREAM_STATE_DIR", str(Path.home() / ".config" / "utstream")))
TARGET_FILE = STATE_DIR / "target.txt"
PENDING_FILE = STATE_DIR / "pending_target.txt"
WHITELIST_FILE = STATE_DIR / "whitelist.txt"
IDLE_UNTIL_FILE = STATE_DIR / "idle_until"
STATE_FILE = STATE_DIR / "state.txt"

STREAM_SCRIPT = "/home/zkyp/bin/stream_session.sh"
STOP_SCRIPT = "/home/zkyp/bin/stream_stop.sh"
RESETALL_SCRIPT = "/home/zkyp/bin/utstream_resetall.sh"

CONTROLLER_PGREP = ["/usr/bin/pgrep", "-f", "/home/zkyp/bin/stream_session.sh"]

TARGET_RE = re.compile(r"^(?P<ip>(?:\d{1,3}\.){3}\d{1,3}):(?P<port>\d{1,5})(?P<qs>\?.+)?$")

SPECTATE_RE = re.compile(r"Spectate:\s*((?:\d{1,3}\.){3}\d{1,3}:\d{1,5}(?:\?\S+)?)", re.IGNORECASE)

BUTTON_TIMEOUT_SECONDS = 1800

COLOR_IDLE = discord.Color.blurple()
COLOR_LIVE = discord.Color.green()
COLOR_WAITING = discord.Color.gold()
COLOR_ERROR = discord.Color.red()

TEAM_CHANNEL_NAMES = [f"Red Team #{i}" for i in range(1, 8)] + [f"Blue Team #{i}" for i in range(1, 8)]

VOICE_SWITCH_COOLDOWN_SECONDS = 120
VOICE_AUTO_LEAVE_DELAY_SECONDS = 60
LAST_FILL_MAX_AGE_SECONDS = 1800  # how long a parsed pug fill stays offerable/usable

_last_voice_switch_ts = 0.0
_last_seen_game_state = None
_pending_auto_leave_task = None

_handled_fill_messages = set()


def guard_channel(ctx):
    if GUILD_IDS and (not ctx.guild or ctx.guild.id not in GUILD_IDS):
        return False
    if CHANNEL_IDS and (not ctx.channel or ctx.channel.id not in CHANNEL_IDS):
        return False
    return True


def guard_interaction_channel(interaction):
    if GUILD_IDS and (not interaction.guild or interaction.guild.id not in GUILD_IDS):
        return False
    if CHANNEL_IDS and (not interaction.channel or interaction.channel.id not in CHANNEL_IDS):
        return False
    return True


def is_stop_user(user_id):
    return user_id in STOP_USER_IDS


def is_blacklisted(user_id):
    return user_id in BLACKLIST_USER_IDS


def drama_line():
    return random.choice(BLACKLIST_DRAMA)


def _normalize_channel_name(name):
    cleaned = "".join(c for c in name if c.isalnum() or c in "# ")
    return " ".join(cleaned.lower().split())


def find_voice_channel(guild, name):
    target = _normalize_channel_name(name)
    for ch in guild.voice_channels:
        if _normalize_channel_name(ch.name) == target:
            return ch
    return None


def allow_voice_switch_now(user_id):
    global _last_voice_switch_ts
    if is_stop_user(user_id):
        return True
    now = time.time()
    if now - _last_voice_switch_ts < VOICE_SWITCH_COOLDOWN_SECONDS:
        return False
    _last_voice_switch_ts = now
    return True


_uid_cache = {}


def uid_of_user(username):
    if username in _uid_cache:
        return _uid_cache[username]
    cp = subprocess.run(["id", "-u", username], capture_output=True, text=True)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "id -u failed")
    _uid_cache[username] = int(cp.stdout.strip())
    return _uid_cache[username]


def _runtime_env_for_user(uid):
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = "/run/user/" + str(uid)
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/" + str(uid) + "/bus"
    return env


def run_bg_as_zkyp(cmd):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WHITELIST_FILE.touch(exist_ok=True)
    zkyp_uid = uid_of_user("zkyp")
    env = _runtime_env_for_user(zkyp_uid)
    full_cmd = cmd if os.geteuid() == zkyp_uid else ["sudo", "-n", "-u", "zkyp"] + cmd
    p = subprocess.Popen(full_cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    return p.pid


def run_fg_as_zkyp(cmd):
    zkyp_uid = uid_of_user("zkyp")
    env = _runtime_env_for_user(zkyp_uid)
    full_cmd = cmd if os.geteuid() == zkyp_uid else ["sudo", "-n", "-u", "zkyp"] + cmd
    return subprocess.run(full_cmd, env=env, capture_output=True, text=True)


def controller_running():
    cp = run_fg_as_zkyp(CONTROLLER_PGREP)
    return cp.returncode == 0


def human_duration(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return str(h) + "h " + str(m) + "m " + str(s) + "s"
    if m:
        return str(m) + "m " + str(s) + "s"
    return str(s) + "s"


def idle_remaining_seconds():
    try:
        if not IDLE_UNTIL_FILE.exists():
            return 0
        ts = int((IDLE_UNTIL_FILE.read_text().strip() or "0"))
        now = int(datetime.now(tz=timezone.utc).timestamp())
        return max(0, ts - now)
    except Exception:
        return 0


def is_waiting():
    return idle_remaining_seconds() > 0


def normalize_target(raw):
    t = (raw or "").strip()
    if t.lower().startswith("unreal://"):
        t = t[9:].strip()
    return t


def sanitize_target(t):
    m = TARGET_RE.match(normalize_target(t))
    if not m:
        return normalize_target(t)
    return m.group("ip") + ":" + m.group("port")


def whitelist_allows(ip, port):
    WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    WHITELIST_FILE.touch(exist_ok=True)
    want_ip = ip.strip()
    want_ip_port = ip.strip() + ":" + port.strip()
    for raw in WHITELIST_FILE.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        line = line.split("?", 1)[0].strip()
        if line == want_ip or line == want_ip_port:
            return True
    return False


def make_embed(title, description="", color=None):
    e = discord.Embed(title=title, description=description, color=color or COLOR_IDLE)
    e.set_footer(text="UT stream bot")
    return e


def _has_password_query(qs):
    if not qs:
        return False
    parsed = parse_qs(qs[1:] if qs.startswith("?") else qs, keep_blank_values=True)
    lower = {k.lower(): v for k, v in parsed.items()}
    vals = lower.get("password", [])
    return any((v or "").strip() for v in vals)


# Last parsed pug fill info, used to build the Twitch stream title
# and the "stream the last game" button. Persisted so restarts don't lose it.
_last_fill_info = None
LAST_FILL_FILE = STATE_DIR / "last_fill.json"


def _save_last_fill(info):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_FILL_FILE.write_text(json.dumps(info))
    except Exception as ex:
        print("last_fill save failed: " + repr(ex))


def _load_last_fill():
    global _last_fill_info
    try:
        if LAST_FILL_FILE.exists():
            _last_fill_info = json.loads(LAST_FILL_FILE.read_text())
            print("last_fill loaded from disk: " + repr(_last_fill_info))
    except Exception as ex:
        print("last_fill load failed: " + repr(ex))


_load_last_fill()

PUG_ID_RE = re.compile(r"\[#(\d+)\]")
MAP_RE = re.compile(r"Map:\**\s*\**([A-Za-z0-9\[\]\-_.]+)")
RED_STRENGTH_RE = re.compile(r"Red Team[\*\s]*\[(\d+)\]", re.IGNORECASE)
BLUE_STRENGTH_RE = re.compile(r"Blue Team[\*\s]*\[(\d+)\]", re.IGNORECASE)
PLAYER_RE = re.compile(r"\*\*([^*]+?)\*\*\s*\((\d[\d.,]*k?)\)")


def _embed_all_text(embed):
    parts = []
    if embed.title:
        parts.append(embed.title)
    if embed.description:
        parts.append(embed.description)
    for f in embed.fields:
        parts.append(f.name or "")
        parts.append(f.value or "")
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


def parse_fill_info(embed, target):
    text = _embed_all_text(embed)

    pug_id = None
    m = PUG_ID_RE.search(text)
    if m:
        pug_id = m.group(1)

    map_name = None
    m = MAP_RE.search(text)
    if m:
        map_name = m.group(1)

    red_idx = text.lower().find("red team")
    blue_idx = text.lower().find("blue team")
    red_players, blue_players = [], []
    red_strength = blue_strength = None

    if red_idx != -1 and blue_idx != -1 and blue_idx > red_idx:
        red_section = text[red_idx:blue_idx]
        blue_section = text[blue_idx:]
        map_idx = blue_section.find("Map:")
        if map_idx != -1:
            blue_section = blue_section[:map_idx]

        m = RED_STRENGTH_RE.search(red_section)
        if m:
            red_strength = m.group(1)
        m = BLUE_STRENGTH_RE.search(blue_section)
        if m:
            blue_strength = m.group(1)

        red_players = [p[0].strip() for p in PLAYER_RE.findall(red_section)]
        blue_players = [p[0].strip() for p in PLAYER_RE.findall(blue_section)]

    return {
        "target": sanitize_target(target),
        "raw_target": normalize_target(target),
        "pug_id": pug_id,
        "map": map_name,
        "red_strength": red_strength,
        "blue_strength": blue_strength,
        "red_players": red_players,
        "blue_players": blue_players,
        "ts": time.time(),
    }


def build_stream_title(info):
    if not info:
        return "UTCTF"
    parts = ["UTCTF"]

    map_name = info.get("map") or ""
    if map_name.upper().startswith("CTF-"):
        map_name = map_name[4:]
    if map_name:
        parts.append(map_name)

    if info.get("pug_id"):
        parts.append("#" + info["pug_id"])

    def team_bit(label, strength, players):
        bit = label
        if strength:
            bit += " [" + strength + "]"
        if players:
            bit += " " + " ".join(players)
        return bit

    if info.get("red_players") or info.get("blue_players"):
        parts.append(team_bit("RED", info.get("red_strength"), info.get("red_players")))
        parts.append("vs")
        parts.append(team_bit("BLUE", info.get("blue_strength"), info.get("blue_players")))

    title = " ".join(parts)
    if len(title) > 140:
        title = title[:140]
    return title


def extract_spectate_target(embed):
    texts = []
    if embed.title:
        texts.append(embed.title)
    if embed.description:
        texts.append(embed.description)
    if embed.footer and embed.footer.text:
        texts.append(embed.footer.text)
    if embed.author and embed.author.name:
        texts.append(embed.author.name)
    for f in embed.fields:
        texts.append(f.name or "")
        texts.append(f.value or "")
    for t in texts:
        m = SPECTATE_RE.search(t)
        if m:
            return m.group(1).strip()
    return None


_last_start_ts = 0.0


def allow_start_now():
    global _last_start_ts
    now = time.time()
    if now - _last_start_ts < 2.0:
        return False
    _last_start_ts = now
    return True


async def _update_twitch_title(safe_target):
    """Set the Twitch title from the last parsed fill, if it matches this target.
    Runs in the background; never blocks or fails the stream start."""
    try:
        info = _last_fill_info
        if info and info.get("target") == safe_target and time.time() - info.get("ts", 0) < LAST_FILL_MAX_AGE_SECONDS:
            title = build_stream_title(info)
        else:
            title = "UTCTF"
        applied = await asyncio.to_thread(set_stream_title, title)
        print("Twitch title set: " + applied)
    except TwitchAPIError as ex:
        print("Twitch title update failed: " + str(ex))
    except Exception as ex:
        print("Twitch title update error: " + repr(ex))


async def try_start_stream(target_raw, requester_id, requester_mention):
    if is_blacklisted(requester_id):
        return "blacklisted", make_embed("Nope", drama_line(), color=COLOR_ERROR)

    normalized = normalize_target(target_raw)
    m = TARGET_RE.match(normalized)
    if not m:
        return "bad_format", make_embed("Bad request", "Invalid target format.", color=COLOR_ERROR)

    ip = m.group("ip")
    port = m.group("port")
    qs = m.group("qs") or ""

    if not _has_password_query(qs):
        return "missing_password", make_embed("Missing password", "Use ?password=...", color=COLOR_ERROR)

    if not whitelist_allows(ip, port):
        return "rejected", make_embed("Rejected", ip + ":" + port + " not whitelisted.", color=COLOR_ERROR)

    safe_target = sanitize_target(normalized)
    running = await asyncio.to_thread(controller_running)
    waiting = is_waiting()

    if running and waiting:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            TARGET_FILE.write_text(normalized + "\n")
        except Exception as ex:
            return "start_failed", make_embed("Failed", "Could not write target: " + str(ex), color=COLOR_ERROR)

        asyncio.create_task(_update_twitch_title(safe_target))
        e = make_embed("Queued for resume", "Target: " + safe_target, color=COLOR_WAITING)
        e.add_field(name="Mode", value="waiting -> resuming", inline=False)
        e.add_field(name="Idle timeout", value=human_duration(idle_remaining_seconds()), inline=True)
        e.add_field(name="Requested by", value=requester_mention, inline=True)
        return "queued", e

    if running and not waiting:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            PENDING_FILE.write_text(normalized + "\n")
        except Exception as ex:
            return "start_failed", make_embed("Failed", "Could not queue target: " + str(ex), color=COLOR_ERROR)

        asyncio.create_task(_update_twitch_title(safe_target))
        e = make_embed("Queued as next game", "A game is currently live.\nTarget " + safe_target + " starts automatically when it ends.", color=COLOR_WAITING)
        e.add_field(name="Requested by", value=requester_mention, inline=True)
        return "queued_next", e

    if not allow_start_now():
        return "busy", make_embed("Busy", "Try again in a moment.", color=COLOR_ERROR)

    e = make_embed("LIVE NOW", "Target: " + safe_target, color=COLOR_LIVE)
    e.add_field(name="Requested by", value=requester_mention, inline=True)

    try:
        await asyncio.to_thread(run_bg_as_zkyp, [STREAM_SCRIPT, normalized])
    except Exception as ex:
        return "start_failed", make_embed("Start failed", str(ex), color=COLOR_ERROR)

    asyncio.create_task(_update_twitch_title(safe_target))
    return "started", e


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def build_help_embed():
    e = discord.Embed(
        title="UT Stream Bot",
        description="Discord-controlled Unreal Tournament spectator stream.",
        color=COLOR_IDLE,
    )
    e.add_field(
        name="📷 Streaming",
        value=(
            "`!stream ip:port?password=...` — start streaming a server\n"
            "`!stream status` — current status\n"
            "`!stream stop` — stop (restricted)\n"
            "`!watch` — get the watch link"
        ),
        inline=False,
    )
    e.add_field(
        name="🎧 Voice comms",
        value=(
            "`!rt1`–`!rt7` / `!bt1`–`!bt7` — voicebot listens to that team\n"
            "`!commsmenu` — pick a team from a dropdown\n"
            "`!leavevoice` — voicebot leaves voice\n"
            "2 minute cooldown between switches."
        ),
        inline=False,
    )
    e.add_field(
        name="⚡ Automatic",
        value="When a pug fills, Stream/Watch buttons and a comms dropdown appear automatically.",
        inline=False,
    )
    e.set_footer(text="UT stream bot")
    return e


class InfoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        if TWITCH_URL:
            self.add_item(discord.ui.Button(label="Watch pug", emoji="📺", style=discord.ButtonStyle.link, url=TWITCH_URL))
        if WEBSITE_URL:
            self.add_item(discord.ui.Button(label="Website", emoji="🌐", style=discord.ButtonStyle.link, url=WEBSITE_URL))

    @discord.ui.button(label="Help", emoji="❓", style=discord.ButtonStyle.secondary, custom_id="utbot:help")
    async def help_button(self, interaction, button):
        await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)


class CommsSelect(discord.ui.Select):
    def __init__(self, row=None):
        options = [discord.SelectOption(label=name, value=name) for name in TEAM_CHANNEL_NAMES]
        super().__init__(placeholder="Listen to a team's comms...", options=options, row=row)

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not guard_interaction_channel(interaction):
            await interaction.followup.send("Not allowed in this channel.", ephemeral=True)
            return

        if not allow_voice_switch_now(interaction.user.id):
            remaining = int(VOICE_SWITCH_COOLDOWN_SECONDS - (time.time() - _last_voice_switch_ts))
            await interaction.followup.send("On cooldown, try again in " + str(remaining) + "s.", ephemeral=True)
            return

        channel_name = self.values[0]
        channel = find_voice_channel(interaction.guild, channel_name)
        if not channel:
            await interaction.followup.send("No voice channel named " + channel_name, ephemeral=True)
            return

        try:
            await asyncio.to_thread(select_voice_channel, str(channel.id))
        except VoiceRPCError as ex:
            await interaction.followup.send("Failed: " + str(ex), ephemeral=True)
            return

        await interaction.followup.send("🎧 Joined " + channel_name + " for comms.", ephemeral=True)


class StreamButtonView(discord.ui.View):
    def __init__(self, target, timeout=BUTTON_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.target = target
        self.message = None
        if TWITCH_URL:
            self.add_item(discord.ui.Button(label="Watch pug", emoji="📺", style=discord.ButtonStyle.link, url=TWITCH_URL, row=0))
        self.add_item(CommsSelect(row=1))

    @discord.ui.button(label="Stream", emoji="📷", style=discord.ButtonStyle.primary, row=0)
    async def stream_button(self, interaction, button):
        await interaction.response.defer()

        if not guard_interaction_channel(interaction):
            await interaction.followup.send("Not allowed in this channel.", ephemeral=True)
            return

        status, embed = await try_start_stream(self.target, interaction.user.id, interaction.user.mention)

        if status in ("queued", "queued_next", "started"):
            verb = "Live" if status == "started" else "Queued"
            self.remove_item(button)
            link_button = discord.ui.Button(
                label=verb + " - " + interaction.user.display_name,
                emoji="📺",
                style=discord.ButtonStyle.link,
                url=TWITCH_URL,
                row=0,
            )
            self.add_item(link_button)
            await interaction.message.edit(embed=embed, view=self)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.style != discord.ButtonStyle.link:
                item.disabled = True
            elif isinstance(item, discord.ui.Select):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class CommsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CommsSelect())


@bot.command(name="watch")
async def watch(ctx):
    if not guard_channel(ctx):
        return
    await ctx.reply(
        embed=make_embed("📺 Watch the stream", "Live UT pug coverage — buttons below.", color=COLOR_IDLE),
        view=InfoView(),
    )


@bot.command(name="commsmenu")
async def comms_menu(ctx):
    if not guard_channel(ctx):
        return
    await ctx.reply(embed=make_embed("Team Comms", "Pick a channel to listen to."), view=CommsView())


async def _try_post_stream_buttons(message, source):
    if not C00LBOT2_ID or message.author.id != C00LBOT2_ID:
        return
    print("C00LBOT2 [" + source + "] msg=" + str(message.id) + " embeds=" + str(len(message.embeds)))
    if not message.embeds:
        return
    if message.id in _handled_fill_messages:
        print("C00LBOT2 [" + source + "] msg=" + str(message.id) + " already handled")
        return

    for embed in message.embeds:
        target = extract_spectate_target(embed)
        print("C00LBOT2 [" + source + "] msg=" + str(message.id) + " extracted=" + repr(target))
        if target:
            global _last_fill_info
            _last_fill_info = parse_fill_info(embed, target)
            _save_last_fill(_last_fill_info)
            print("C00LBOT2 fill parsed: " + repr(_last_fill_info))
            _handled_fill_messages.add(message.id)
            if len(_handled_fill_messages) > 500:
                _handled_fill_messages.clear()
            running = await asyncio.to_thread(controller_running)
            waiting = is_waiting()
            view = StreamButtonView(target=target)
            if running and not waiting:
                reply_embed = make_embed(
                    "Another game is live",
                    "The stream is currently showing a different game.\n"
                    "This one can start once the current game ends - the Stream button "
                    "will queue it during the waiting window.",
                    color=COLOR_WAITING,
                )
            elif running and waiting:
                reply_embed = make_embed(
                    "Stream is waiting",
                    "The stream is idling between games. Click Stream to put this game live.",
                    color=COLOR_IDLE,
                )
            else:
                reply_embed = make_embed("Ready to stream", "Click below to start streaming this game.", color=COLOR_IDLE)
            try:
                sent = await message.reply(embed=reply_embed, view=view)
            except Exception as ex:
                print("C00LBOT2 reply failed (" + repr(ex) + "), falling back to channel.send")
                try:
                    sent = await message.channel.send(embed=reply_embed, view=view)
                except Exception as ex2:
                    print("C00LBOT2 channel.send ALSO failed: " + repr(ex2))
                    _handled_fill_messages.discard(message.id)
                    return
            view.message = sent
            print("C00LBOT2 buttons posted for msg=" + str(message.id))
            return


@bot.listen("on_message")
async def watch_for_c00lbot2(message):
    await _try_post_stream_buttons(message, "create")


@bot.listen("on_message_edit")
async def watch_for_c00lbot2_edit(before, after):
    await _try_post_stream_buttons(after, "edit")


@bot.listen("on_raw_message_edit")
async def watch_for_c00lbot2_raw_edit(payload):
    if payload.cached_message is not None:
        return
    try:
        channel = bot.get_channel(payload.channel_id)
        if channel is None:
            print("C00LBOT2 [raw_edit] channel " + str(payload.channel_id) + " not found")
            return
        message = await channel.fetch_message(payload.message_id)
    except Exception as ex:
        print("C00LBOT2 [raw_edit] fetch failed: " + repr(ex))
        return
    await _try_post_stream_buttons(message, "raw_edit")


@bot.group(name="stream", invoke_without_command=True)
async def stream(ctx, target: str = ""):
    if not guard_channel(ctx):
        return

    if target.strip().lower() == "help":
        await ctx.reply(embed=build_help_embed(), view=InfoView())
        return

    if not target:
        running = await asyncio.to_thread(controller_running)
        waiting = is_waiting()

        if running and not waiting:
            e = make_embed("LIVE NOW", "A game is currently being streamed.", color=COLOR_LIVE)
            await ctx.reply(embed=e, view=InfoView())
            return

        info = _last_fill_info
        if info and info.get("raw_target") and time.time() - info.get("ts", 0) < LAST_FILL_MAX_AGE_SECONDS:
            age_min = int((time.time() - info.get("ts", 0)) // 60)
            pug_bit = "Pug #" + info["pug_id"] if info.get("pug_id") else "A pug"
            map_bit = info.get("map") or "unknown map"
            e = make_embed(
                "Stream the last game?",
                pug_bit + " on " + map_bit + " filled " + str(age_min) + "m ago.",
                color=COLOR_IDLE,
            )
            view = StreamButtonView(target=info["raw_target"])
            sent = await ctx.reply(embed=e, view=view)
            view.message = sent
            return

        await ctx.reply(embed=build_help_embed(), view=InfoView())
        return

    status, embed = await try_start_stream(target, ctx.author.id, ctx.author.mention)
    view = None
    if status in ("queued", "queued_next", "started", "already_live") and TWITCH_URL:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Watch pug", emoji="📺", style=discord.ButtonStyle.link, url=TWITCH_URL))
    await ctx.reply(embed=embed, view=view)


@stream.command(name="status")
async def status(ctx):
    if not guard_channel(ctx):
        return

    running = await asyncio.to_thread(controller_running)
    waiting = is_waiting()
    left = idle_remaining_seconds()

    if running and waiting:
        title, color = "Status: waiting", COLOR_WAITING
    elif running:
        title, color = "LIVE NOW", COLOR_LIVE
    else:
        title, color = "Status: idle", COLOR_IDLE

    e = make_embed(title, "", color=color)
    if waiting:
        e.add_field(name="Waiting left", value=human_duration(left), inline=False)

    view = None
    if running and TWITCH_URL:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Watch pug", emoji="📺", style=discord.ButtonStyle.link, url=TWITCH_URL))
    await ctx.reply(embed=e, view=view)


@stream.command(name="stop")
async def stop(ctx):
    if not guard_channel(ctx):
        return
    if is_blacklisted(ctx.author.id):
        await ctx.reply(embed=make_embed("Nope", drama_line(), color=COLOR_ERROR))
        return
    if not is_stop_user(ctx.author.id):
        await ctx.reply(embed=make_embed("Nope", "Not allowed.", color=COLOR_ERROR))
        return

    cp = await asyncio.to_thread(run_fg_as_zkyp, [STOP_SCRIPT])
    if cp.returncode == 0:
        try:
            await asyncio.to_thread(leave_voice_channel)
        except VoiceRPCError as ex:
            print("voicebot leave on stop failed: " + repr(ex))
        await ctx.reply(embed=make_embed("Stopped", "UT + OBS (and controller) stopped. Voicebot disconnected.", color=COLOR_IDLE))
    else:
        out = (cp.stderr or cp.stdout or "").strip()[-900:] or "Unknown error."
        await ctx.reply(embed=make_embed("Stop failed", "```" + out + "```", color=COLOR_ERROR))


def _team_command_factory(channel_name):
    async def _cmd(ctx):
        if not guard_channel(ctx):
            return
        if not allow_voice_switch_now(ctx.author.id):
            remaining = int(VOICE_SWITCH_COOLDOWN_SECONDS - (time.time() - _last_voice_switch_ts))
            await ctx.reply(embed=make_embed("On cooldown", "Try again in " + str(remaining) + "s.", color=COLOR_ERROR))
            return

        channel = find_voice_channel(ctx.guild, channel_name)
        if not channel:
            await ctx.reply(embed=make_embed("Not found", "No voice channel named " + channel_name, color=COLOR_ERROR))
            return

        try:
            await asyncio.to_thread(select_voice_channel, str(channel.id))
        except VoiceRPCError as ex:
            await ctx.reply(embed=make_embed("Failed", str(ex), color=COLOR_ERROR))
            return

        await ctx.reply(embed=make_embed("Switched", "Voicebot joined " + channel_name, color=COLOR_LIVE))
    return _cmd


for i in range(1, 8):
    bot.add_command(commands.Command(_team_command_factory("Red Team #" + str(i)), name="rt" + str(i)))
    bot.add_command(commands.Command(_team_command_factory("Blue Team #" + str(i)), name="bt" + str(i)))


@bot.command(name="leavevoice")
async def leave_voice(ctx):
    if not guard_channel(ctx):
        return
    if not allow_voice_switch_now(ctx.author.id):
        remaining = int(VOICE_SWITCH_COOLDOWN_SECONDS - (time.time() - _last_voice_switch_ts))
        await ctx.reply(embed=make_embed("On cooldown", "Try again in " + str(remaining) + "s.", color=COLOR_ERROR))
        return

    try:
        await asyncio.to_thread(leave_voice_channel)
    except VoiceRPCError as ex:
        await ctx.reply(embed=make_embed("Failed", str(ex), color=COLOR_ERROR))
        return

    await ctx.reply(embed=make_embed("Left", "Voicebot left the voice channel.", color=COLOR_IDLE))


async def _delayed_auto_leave():
    await asyncio.sleep(VOICE_AUTO_LEAVE_DELAY_SECONDS)
    try:
        current = STATE_FILE.read_text().strip()
    except Exception:
        return
    if current != "LIVE":
        try:
            await asyncio.to_thread(leave_voice_channel)
        except VoiceRPCError:
            pass


@tasks.loop(seconds=3)
async def watch_game_state_for_voice():
    global _last_seen_game_state, _pending_auto_leave_task
    try:
        current = STATE_FILE.read_text().strip()
    except Exception:
        return

    if _last_seen_game_state is not None and _last_seen_game_state != "IDLE" and current == "IDLE":
        # Stream fully ended -> disconnect voicebot immediately
        try:
            await asyncio.to_thread(leave_voice_channel)
        except VoiceRPCError:
            pass
    elif _last_seen_game_state == "LIVE" and current == "WAITING":
        # Game ended, might resume -> linger 60s then leave if nothing resumed
        if _pending_auto_leave_task is None or _pending_auto_leave_task.done():
            _pending_auto_leave_task = asyncio.create_task(_delayed_auto_leave())

    _last_seen_game_state = current


_persistent_views_added = False


@bot.event
async def on_ready():
    global _persistent_views_added
    if not _persistent_views_added:
        bot.add_view(InfoView())
        _persistent_views_added = True
    if not watch_game_state_for_voice.is_running():
        watch_game_state_for_voice.start()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN missing")
    bot.run(TOKEN)
