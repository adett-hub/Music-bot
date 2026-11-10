"""
Discord music bot — slash commands, per-server queues, yt-dlp + FFmpeg streaming.

Commands: /play /skip /pause /resume /stop /queue /nowplaying /volume
          /shuffle /remove /loop /leave
"""

import asyncio
import json
import logging
import os
import random
import re
from collections import deque
from dataclasses import dataclass

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: instant command sync to one server
COOKIES_FILE = os.getenv("YTDLP_COOKIES")  # optional: cookies.txt if YouTube blocks the host
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "300"))  # seconds before leaving when idle
MAX_PLAYLIST = int(os.getenv("MAX_PLAYLIST", "100"))
DJ_ROLE = os.getenv("DJ_ROLE", "DJ")  # role name (case-insensitive) or role ID

log = logging.getLogger("musicbot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

YDL_BASE = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}
if COOKIES_FILE:
    YDL_BASE["cookiefile"] = COOKIES_FILE

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


# ---------------------------------------------------------------- data

@dataclass
class Track:
    url: str          # page URL or "ytsearch1:..." query (resolved at play time; stream URLs expire)
    title: str
    duration: int | None
    requester: str
    page: str | None = None  # the actual page that played (filled in at play time)

    def label(self) -> str:
        return f"**{self.title}** `{fmt_time(self.duration)}`"


def fmt_time(seconds: int | float | None) -> str:
    if not seconds:
        return "live/?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


class GuildPlayer:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.volume: float = 0.5
        self.loop: str = "off"  # off | track | queue
        self.text_channel: discord.abc.Messageable | None = None
        self.idle_task: asyncio.Task | None = None


# ---------------------------------------------------------------- yt-dlp helpers

def _search_or_resolve(query: str) -> list[dict]:
    """Return a list of {url,title,duration} entries for a URL, playlist, or search."""
    opts = {**YDL_BASE, "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
    if info is None:
        return []
    is_search = not query.startswith(("http://", "https://"))
    entries = info.get("entries")
    if entries is None:
        entries = [info]
    else:
        entries = [e for e in entries if e][: (1 if is_search else MAX_PLAYLIST)]
    out = []
    for e in entries:
        url = e.get("webpage_url") or e.get("url")
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        out.append({"url": url, "title": e.get("title") or "Unknown", "duration": e.get("duration")})
    return out


# ---------------------------------------------------------------- Spotify (via public embed pages)
# Spotify doesn't allow streaming its audio, so we read the song titles from its
# public embed page and play each song by searching YouTube. No API key needed.
# Embed pages list up to ~100 tracks for playlists/albums.

SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?|spotify:)"
    r"(track|album|playlist|artist)[/:]([A-Za-z0-9]+)"
)
SPOTIFY_SHORT = ("spotify.link/", "spotify.app.link/")
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def is_spotify(query: str) -> bool:
    return bool(SPOTIFY_RE.search(query)) or any(s in query for s in SPOTIFY_SHORT)


def _find_entity(obj):
    """Locate the embed's entity dict (has a trackList, or is a single track)."""
    if isinstance(obj, dict):
        if "trackList" in obj or (obj.get("type") == "track" and ("title" in obj or "name" in obj)):
            return obj
        for v in obj.values():
            found = _find_entity(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_entity(v)
            if found:
                return found
    return None


def _clean(s: str | None) -> str:
    return (s or "").replace(" ", " ").strip()


def _to_entry(title: str, artist: str, duration_ms, uri: str | None) -> dict:
    link = None
    if uri and uri.startswith("spotify:track:"):
        link = f"https://open.spotify.com/track/{uri.split(':')[-1]}"
    query = f"{artist} - {title}" if artist else title
    return {
        "url": f"ytsearch1:{query} audio",
        "title": f"{title} — {artist}" if artist else title,
        "duration": int(duration_ms / 1000) if isinstance(duration_ms, (int, float)) else None,
        "link": link,
    }


def parse_spotify_embed(html: str) -> list[dict]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.S)
    if not m:
        raise ValueError("Spotify embed data not found (page format may have changed)")
    entity = _find_entity(json.loads(m.group(1)))
    if not entity:
        raise ValueError("No tracks found in Spotify embed data")

    if "trackList" in entity:
        return [
            _to_entry(_clean(t.get("title") or t.get("name")), _clean(t.get("subtitle")),
                      t.get("duration"), t.get("uri"))
            for t in entity["trackList"][:MAX_PLAYLIST]
            if t.get("title") or t.get("name")
        ]

    artists = ", ".join(_clean(a.get("name")) for a in entity.get("artists") or [] if a.get("name"))
    return [_to_entry(_clean(entity.get("title") or entity.get("name")),
                      artists or _clean(entity.get("subtitle")),
                      entity.get("duration"), entity.get("uri"))]


async def resolve_spotify(query: str) -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(headers=HTTP_HEADERS, timeout=timeout) as session:
        if not SPOTIFY_RE.search(query):  # short link -> follow redirect
            async with session.get(query.strip(), allow_redirects=True) as r:
                query = str(r.url)
        m = SPOTIFY_RE.search(query)
        if not m:
            raise ValueError("Unrecognized Spotify link")
        kind, sid = m.groups()
        async with session.get(f"https://open.spotify.com/embed/{kind}/{sid}") as r:
            r.raise_for_status()
            html = await r.text()
    return parse_spotify_embed(html)


def _stream_url(page_url: str) -> tuple[str, dict]:
    opts = {**YDL_BASE, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(page_url, download=False)
    if "entries" in info:
        info = info["entries"][0]
    return info["url"], info


# ---------------------------------------------------------------- bot

class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.players: dict[int, GuildPlayer] = {}

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Commands synced globally (can take up to an hour to appear)")

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play"))

    def player(self, guild_id: int) -> GuildPlayer:
        return self.players.setdefault(guild_id, GuildPlayer())

    # ---- playback loop

    async def play_next(self, guild: discord.Guild):
        p = self.player(guild.id)
        vc: discord.VoiceClient | None = guild.voice_client
        if not vc or not vc.is_connected():
            return

        finished = p.current
        if finished and p.loop == "track":
            nxt = finished
        else:
            if finished and p.loop == "queue":
                p.queue.append(finished)
            nxt = p.queue.popleft() if p.queue else None

        p.current = nxt
        if nxt is None:
            self.start_idle_timer(guild)
            return
        self.cancel_idle_timer(p)

        try:
            stream, info = await asyncio.to_thread(_stream_url, nxt.url)
        except Exception as e:
            log.warning("Failed to resolve %s: %s", nxt.url, e)
            if p.text_channel:
                await p.text_channel.send(f"⚠️ Couldn't play {nxt.label()} — skipping.")
            p.current = None
            return await self.play_next(guild)

        if nxt.duration is None:
            nxt.duration = info.get("duration")
        nxt.page = info.get("webpage_url") or nxt.page

        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(stream, **FFMPEG_OPTS), volume=p.volume)

        def after(err):
            if err:
                log.error("Player error: %s", err)
            asyncio.run_coroutine_threadsafe(self.play_next(guild), self.loop)

        vc.play(source, after=after)
        if p.text_channel and not (finished is nxt and p.loop == "track"):
            await p.text_channel.send(f"🎶 Now playing {nxt.label()} — requested by {nxt.requester}")

    # ---- idle / auto-disconnect

    def start_idle_timer(self, guild: discord.Guild):
        p = self.player(guild.id)
        self.cancel_idle_timer(p)

        async def wait_and_leave():
            await asyncio.sleep(IDLE_TIMEOUT)
            vc = guild.voice_client
            if vc and not vc.is_playing() and not vc.is_paused():
                await vc.disconnect()
                self.players.pop(guild.id, None)
                if p.text_channel:
                    await p.text_channel.send("👋 Left the voice channel after being idle.")

        p.idle_task = asyncio.create_task(wait_and_leave())

    @staticmethod
    def cancel_idle_timer(p: GuildPlayer):
        if p.idle_task and not p.idle_task.done():
            p.idle_task.cancel()
        p.idle_task = None

    async def on_voice_state_update(self, member, before, after):
        # Leave if everyone else left the bot's channel
        vc = member.guild.voice_client
        if not vc or not vc.channel:
            return
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            p = self.players.pop(member.guild.id, None)
            if p:
                self.cancel_idle_timer(p)
            await vc.disconnect()


bot = MusicBot()


# ---------------------------------------------------------------- DJ role check

class NotDJ(app_commands.CheckFailure):
    pass


def is_dj(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True  # server admins can always control the bot
    for role in member.roles:
        if str(role.id) == DJ_ROLE or role.name.lower() == DJ_ROLE.lower():
            return True
    return False


def dj_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and is_dj(interaction.user):
            return True
        raise NotDJ()
    return app_commands.check(predicate)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, NotDJ):
        msg = f"🎧 You need the **{DJ_ROLE}** role to control the music."
    else:
        log.exception("Command error", exc_info=error)
        msg = "Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ---------------------------------------------------------------- command helpers

async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    user_vc = interaction.user.voice.channel if interaction.user.voice else None
    if user_vc is None:
        await interaction.followup.send("Join a voice channel first.", ephemeral=True)
        return None
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await user_vc.connect(self_deaf=True)
    elif vc.channel != user_vc:
        if vc.is_playing() and len([m for m in vc.channel.members if not m.bot]) > 0:
            await interaction.followup.send(f"I'm busy in {vc.channel.mention}.", ephemeral=True)
            return None
        await vc.move_to(user_vc)
    return vc


def in_same_channel(interaction: discord.Interaction) -> bool:
    vc = interaction.guild.voice_client
    return bool(vc and interaction.user.voice and interaction.user.voice.channel == vc.channel)


async def require_vc(interaction: discord.Interaction) -> discord.VoiceClient | None:
    if not in_same_channel(interaction):
        await interaction.response.send_message("You need to be in my voice channel.", ephemeral=True)
        return None
    return interaction.guild.voice_client


# ---------------------------------------------------------------- commands

@bot.tree.command(description="Play a song or playlist (YouTube/Spotify/SoundCloud link or search terms)")
@app_commands.describe(query="URL or search terms")
@app_commands.guild_only()
@dj_only()
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    p = bot.player(interaction.guild.id)
    p.text_channel = interaction.channel

    spotify = is_spotify(query)
    try:
        if spotify:
            results = await resolve_spotify(query)
        else:
            results = await asyncio.to_thread(_search_or_resolve, query)
    except Exception as e:
        log.warning("Lookup failed for %r: %s", query, e)
        hint = " Make sure the playlist is public." if spotify else " Try another link or search."
        return await interaction.followup.send(f"Couldn't load that.{hint}")
    if not results:
        return await interaction.followup.send("No results.")

    tracks = [Track(r["url"], r["title"], r["duration"], interaction.user.display_name, r.get("link"))
              for r in results]
    p.queue.extend(tracks)

    if len(tracks) == 1:
        msg = f"➕ Queued {tracks[0].label()}" if (vc.is_playing() or vc.is_paused()) else f"▶️ Starting {tracks[0].label()}"
    else:
        msg = f"➕ Queued **{len(tracks)}** tracks" + (" from Spotify (played via YouTube)." if spotify else " from the playlist.")
    await interaction.followup.send(msg)

    if not vc.is_playing() and not vc.is_paused():
        await bot.play_next(interaction.guild)


@bot.tree.command(description="Skip the current track")
@app_commands.guild_only()
@dj_only()
async def skip(interaction: discord.Interaction):
    vc = await require_vc(interaction)
    if not vc:
        return
    if not (vc.is_playing() or vc.is_paused()):
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    p = bot.player(interaction.guild.id)
    if p.loop == "track":
        p.loop = "off"  # skipping should actually move on
    vc.stop()  # triggers `after` -> play_next
    await interaction.response.send_message("⏭️ Skipped.")


@bot.tree.command(description="Pause playback")
@app_commands.guild_only()
@dj_only()
async def pause(interaction: discord.Interaction):
    vc = await require_vc(interaction)
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Paused.")
    elif vc:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(description="Resume playback")
@app_commands.guild_only()
@dj_only()
async def resume(interaction: discord.Interaction):
    vc = await require_vc(interaction)
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.")
    elif vc:
        await interaction.response.send_message("Not paused.", ephemeral=True)


@bot.tree.command(description="Stop playback and clear the queue")
@app_commands.guild_only()
@dj_only()
async def stop(interaction: discord.Interaction):
    vc = await require_vc(interaction)
    if not vc:
        return
    p = bot.player(interaction.guild.id)
    p.queue.clear()
    p.loop = "off"
    p.current = None
    vc.stop()
    await interaction.response.send_message("⏹️ Stopped and cleared the queue.")


@bot.tree.command(description="Show the queue")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction):
    p = bot.player(interaction.guild.id)
    if not p.current and not p.queue:
        return await interaction.response.send_message("The queue is empty.", ephemeral=True)
    lines = []
    if p.current:
        lines.append(f"**Now:** {p.current.label()}")
    for i, t in enumerate(list(p.queue)[:15], start=1):
        lines.append(f"`{i}.` {t.label()} — {t.requester}")
    if len(p.queue) > 15:
        lines.append(f"…and {len(p.queue) - 15} more")
    total = sum(t.duration or 0 for t in p.queue)
    embed = discord.Embed(title="Queue", description="\n".join(lines), color=0x5865F2)
    embed.set_footer(text=f"{len(p.queue)} queued • {fmt_time(total)} total • loop: {p.loop} • vol: {int(p.volume * 100)}%")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(description="Show the current track")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    p = bot.player(interaction.guild.id)
    if not p.current:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    await interaction.response.send_message(f"🎶 {p.current.label()} — requested by {p.current.requester}\n{p.current.page or p.current.url}")


@bot.tree.command(description="Set volume (0–150)")
@app_commands.describe(percent="Volume percent")
@app_commands.guild_only()
@dj_only()
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 150]):
    vc = await require_vc(interaction)
    if not vc:
        return
    p = bot.player(interaction.guild.id)
    p.volume = percent / 100
    if isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = p.volume
    await interaction.response.send_message(f"🔊 Volume set to {percent}%.")


@bot.tree.command(description="Shuffle the queue")
@app_commands.guild_only()
@dj_only()
async def shuffle(interaction: discord.Interaction):
    if not await require_vc(interaction):
        return
    p = bot.player(interaction.guild.id)
    items = list(p.queue)
    random.shuffle(items)
    p.queue = deque(items)
    await interaction.response.send_message(f"🔀 Shuffled {len(items)} tracks.")


@bot.tree.command(description="Remove a track from the queue by its number")
@app_commands.describe(position="Position shown in /queue")
@app_commands.guild_only()
@dj_only()
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    if not await require_vc(interaction):
        return
    p = bot.player(interaction.guild.id)
    if position > len(p.queue):
        return await interaction.response.send_message("No track at that position.", ephemeral=True)
    t = p.queue[position - 1]
    del p.queue[position - 1]
    await interaction.response.send_message(f"🗑️ Removed {t.label()}")


@bot.tree.command(description="Set loop mode")
@app_commands.choices(mode=[
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="track", value="track"),
    app_commands.Choice(name="queue", value="queue"),
])
@app_commands.guild_only()
@dj_only()
async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not await require_vc(interaction):
        return
    bot.player(interaction.guild.id).loop = mode.value
    await interaction.response.send_message(f"🔁 Loop: **{mode.value}**")


@bot.tree.command(description="Disconnect the bot")
@app_commands.guild_only()
@dj_only()
async def leave(interaction: discord.Interaction):
    vc = await require_vc(interaction)
    if not vc:
        return
    p = bot.players.pop(interaction.guild.id, None)
    if p:
        p.queue.clear()
        bot.cancel_idle_timer(p)
    await vc.disconnect()
    await interaction.response.send_message("👋 Bye.")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment or .env file.")
    bot.run(TOKEN, log_handler=None)
