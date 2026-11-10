"""
Discord music bot — slash commands, per-server queues, yt-dlp + FFmpeg streaming.

Commands: /play /skip /pause /resume /stop /queue /nowplaying /volume
          /shuffle /remove /loop /leave
"""

import asyncio
import logging
import os
import random
from collections import deque
from dataclasses import dataclass

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
    url: str          # page URL (re-resolved at play time; stream URLs expire)
    title: str
    duration: int | None
    requester: str

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

@bot.tree.command(description="Play a song or playlist (YouTube/SoundCloud URL or search terms)")
@app_commands.describe(query="URL or search terms")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    p = bot.player(interaction.guild.id)
    p.text_channel = interaction.channel

    try:
        results = await asyncio.to_thread(_search_or_resolve, query)
    except Exception as e:
        log.warning("Lookup failed for %r: %s", query, e)
        return await interaction.followup.send("Couldn't find or load that. Try another link or search.")
    if not results:
        return await interaction.followup.send("No results.")

    tracks = [Track(r["url"], r["title"], r["duration"], interaction.user.display_name) for r in results]
    p.queue.extend(tracks)

    if len(tracks) == 1:
        msg = f"➕ Queued {tracks[0].label()}" if (vc.is_playing() or vc.is_paused()) else f"▶️ Starting {tracks[0].label()}"
    else:
        msg = f"➕ Queued **{len(tracks)}** tracks from the playlist."
    await interaction.followup.send(msg)

    if not vc.is_playing() and not vc.is_paused():
        await bot.play_next(interaction.guild)


@bot.tree.command(description="Skip the current track")
@app_commands.guild_only()
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
async def pause(interaction: discord.Interaction):
    vc = await require_vc(interaction)
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Paused.")
    elif vc:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(description="Resume playback")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    vc = await require_vc(interaction)
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.")
    elif vc:
        await interaction.response.send_message("Not paused.", ephemeral=True)


@bot.tree.command(description="Stop playback and clear the queue")
@app_commands.guild_only()
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
    await interaction.response.send_message(f"🎶 {p.current.label()} — requested by {p.current.requester}\n{p.current.url}")


@bot.tree.command(description="Set volume (0–150)")
@app_commands.describe(percent="Volume percent")
@app_commands.guild_only()
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
async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not await require_vc(interaction):
        return
    bot.player(interaction.guild.id).loop = mode.value
    await interaction.response.send_message(f"🔁 Loop: **{mode.value}**")


@bot.tree.command(description="Disconnect the bot")
@app_commands.guild_only()
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
