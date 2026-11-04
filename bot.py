"""HodeWerner: plays YouTube audio in a Discord voice channel.

Slash commands: /play, /search, /queue, /nowplaying, /skip, /pause, /resume, /stop,
/seek, /replay, /move, /remove, /shuffle, /clear, /loop, /autoplay, /volume,
/playlist save|play|list|show|delete
The bot only serves the server named by GUILD_ID and leaves any other server.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

import discord
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
COOKIES = os.environ.get("YTDLP_COOKIES", "").strip()
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "300"))  # seconds before leaving when idle
MAX_PLAYLIST = int(os.environ.get("MAX_PLAYLIST", "100"))  # max songs taken from one playlist
DEFAULT_VOLUME = int(os.environ.get("DEFAULT_VOLUME", "50"))  # percent
AUTOPLAY_DEFAULT = os.environ.get("AUTOPLAY", "off").lower() in ("1", "on", "true", "yes")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
CACHE_DIR = DATA_DIR / "cache"
CACHE_MAX_MB = int(os.environ.get("CACHE_MAX_MB", "500"))  # downloaded songs kept for replays
IDLE_TEXT = f"{IDLE_TIMEOUT // 60} min" if IDLE_TIMEOUT >= 60 else f"{IDLE_TIMEOUT} seconds"

log = logging.getLogger("hodewerner")

# Colors taken from the logo.
BLUE = discord.Color.from_rgb(74, 120, 190)
GREEN = discord.Color.from_rgb(87, 180, 120)
GREY = discord.Color.from_rgb(60, 64, 72)
RED = discord.Color.from_rgb(220, 80, 80)

BASE_YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
}
if COOKIES:
    BASE_YDL_OPTS["cookiefile"] = COOKIES



LOOP_OFF, LOOP_SONG, LOOP_QUEUE = "off", "song", "queue"
LOOP_LABELS = {LOOP_OFF: "Off", LOOP_SONG: "This song", LOOP_QUEUE: "Whole queue"}

YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/)([A-Za-z0-9_-]{11})")


# ---------------------------------------------------------------- helpers


def fmt_time(seconds: int | float | None) -> str:
    if not seconds:
        return "live" if seconds == 0 else "?"
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text: str) -> int | None:
    """'90', '1:30' or '1:02:03' -> seconds."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return secs


def progress_bar(elapsed: float, total: int | None, width: int = 16) -> str:
    if not total:
        return ""
    pos = min(int(elapsed / total * width), width - 1)
    return "▬" * pos + "🔘" + "▬" * (width - pos - 1)


def video_id(url: str | None) -> str | None:
    m = YT_ID_RE.search(url or "")
    return m.group(1) if m else None


@dataclass
class Track:
    title: str
    page_url: str
    duration: int | None
    thumbnail: str | None
    requested_by: discord.Member
    autoplay: bool = False
    file_path: str | None = None
    download: "asyncio.Task | None" = None

    def link(self) -> str:
        title = self.title.replace("[", "(").replace("]", ")")
        return f"[{title}]({self.page_url})"

    def who(self) -> str:
        return "🤖 Autoplay" if self.autoplay else self.requested_by.mention


def best_thumbnail(info: dict) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    return thumbs[-1]["url"] if thumbs else None


def _resolve(query: str) -> tuple[str | None, list[dict]]:
    """Turn a link or search words into (playlist title or None, list of video infos)."""
    is_url = query.startswith(("http://", "https://"))
    if not is_url:
        opts = {**BASE_YDL_OPTS, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = [e for e in info.get("entries", []) if e]
        if not entries:
            raise ValueError("No results found.")
        return None, entries[:1]

    # A link to a single video (even with &list=) plays that video only.
    # A playlist link (youtube.com/playlist?list=...) queues the whole list.
    opts = {**BASE_YDL_OPTS, "noplaylist": True, "extract_flat": "in_playlist",
            "playlistend": MAX_PLAYLIST}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
    if "entries" in info:
        entries = [e for e in info["entries"] if e and e.get("url")]
        if not entries:
            raise ValueError("That playlist is empty or private.")
        return info.get("title") or "playlist", entries[:MAX_PLAYLIST]
    return None, [info]


def _search(query: str, n: int = 5) -> list[dict]:
    opts = {**BASE_YDL_OPTS, "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    return [e for e in info.get("entries", []) if e and e.get("url")]


def _related(vid: str, n: int = 15) -> list[dict]:
    """Songs from YouTube's 'Mix' radio for a video."""
    opts = {**BASE_YDL_OPTS, "extract_flat": "in_playlist", "playlistend": n}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}&list=RD{vid}", download=False)
    return [e for e in info.get("entries", []) if e and e.get("url")]


def _download(page_url: str) -> tuple[dict, str]:
    """Download a song's audio into the cache. Returns (info, file path)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    opts = {**BASE_YDL_OPTS, "noplaylist": True,
            "outtmpl": str(CACHE_DIR / "%(id)s.%(ext)s"), "overwrites": False}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(page_url, download=True)
    downloads = info.get("requested_downloads") or []
    path = downloads[0].get("filepath") if downloads else None
    if not path or not os.path.exists(path):
        raise RuntimeError("Download finished but the file is missing.")
    os.utime(path)  # mark as recently used for the cache cleanup
    _prune_cache(keep=path)
    return info, path


def _prune_cache(keep: str) -> None:
    """Delete the oldest cached songs once the cache is over CACHE_MAX_MB."""
    try:
        files = sorted((f for f in CACHE_DIR.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        for f in files:
            if total <= CACHE_MAX_MB * 1024 * 1024:
                break
            if str(f) == keep:
                continue
            total -= f.stat().st_size
            f.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Cache cleanup failed: %s", e)


async def run_blocking(func, *args):
    # yt-dlp is blocking, so run it off the event loop.
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


def info_to_track(info: dict, member: discord.Member, autoplay: bool = False) -> Track:
    url = info.get("webpage_url") or info.get("original_url") or info.get("url")
    if url and not url.startswith("http"):  # flat entries can be bare IDs
        url = f"https://www.youtube.com/watch?v={url}"
    return Track(
        title=info.get("title") or "Unknown",
        page_url=url,
        duration=info.get("duration"),
        thumbnail=best_thumbnail(info),
        requested_by=member,
        autoplay=autoplay,
    )


# ---------------------------------------------------------------- saved playlists


class PlaylistStore:
    """Saved playlists, shared by everyone on the server, kept in a JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.error("Could not read %s: %s", path, e)

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def key(name: str) -> str:
        return name.strip().lower()

    def get(self, name: str) -> dict | None:
        return self.data.get(self.key(name))

    def save(self, name: str, owner: str, tracks: list[Track]) -> bool:
        existed = self.key(name) in self.data
        self.data[self.key(name)] = {
            "name": name.strip(),
            "owner": owner,
            "saved": int(time.time()),
            "songs": [
                {"title": t.title, "url": t.page_url, "duration": t.duration, "thumbnail": t.thumbnail}
                for t in tracks
            ],
        }
        self._write()
        return existed

    def delete(self, name: str) -> bool:
        if self.data.pop(self.key(name), None) is None:
            return False
        self._write()
        return True

    def names(self) -> list[str]:
        return sorted((v["name"] for v in self.data.values()), key=str.lower)


# ---------------------------------------------------------------- player


class GuildPlayer:
    def __init__(self, bot: "HodeWerner", guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.loop_mode = LOOP_OFF
        self.autoplay = AUTOPLAY_DEFAULT
        self.volume = DEFAULT_VOLUME / 100
        self.np_message: discord.Message | None = None
        self.history: deque[str] = deque(maxlen=50)  # recent video IDs, so autoplay avoids repeats
        self._lock = asyncio.Lock()
        self._skip_requested = False
        self._stopping = False
        self._idle_task: asyncio.Task | None = None
        self._started_at = 0.0
        self._paused_at: float | None = None
        self._paused_total = 0.0

    @property
    def voice(self) -> discord.VoiceClient | None:
        return self.guild.voice_client  # type: ignore[return-value]

    # ---- timing for the progress bar and /seek
    def elapsed(self) -> float:
        now = self._paused_at or time.monotonic()
        return max(0.0, now - self._started_at - self._paused_total)

    def _reset_clock(self, position: float = 0.0) -> None:
        now = time.monotonic()
        self._started_at = now - position
        self._paused_total = 0.0
        vc = self.voice
        self._paused_at = now if vc and vc.is_paused() else None

    def mark_paused(self) -> None:
        self._paused_at = time.monotonic()

    def mark_resumed(self) -> None:
        if self._paused_at:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None

    def queue_seconds(self) -> int:
        return sum(t.duration or 0 for t in self.queue)

    def make_source(self, file_path: str, start: int = 0) -> discord.AudioSource:
        before = f"-ss {start}" if start else ""
        return discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(file_path, before_options=before, options="-vn"),
            volume=self.volume,
        )

    async def fetch(self, track: Track) -> None:
        """Download a track (once), filling in its details. Safe to call more than once."""
        if track.file_path and os.path.exists(track.file_path):
            return
        if track.download is None or track.download.done() and track.download.exception():
            track.download = asyncio.ensure_future(run_blocking(_download, track.page_url))
        info, path = await asyncio.shield(track.download)
        track.file_path = path
        track.duration = track.duration or info.get("duration")
        track.thumbnail = track.thumbnail or best_thumbnail(info)
        if track.title == "Unknown":
            track.title = info.get("title") or track.title

    def prefetch_next(self) -> None:
        """Start downloading the next song in the background, so it starts without a gap."""
        if self.queue:
            nxt = self.queue[0]

            async def _go():
                try:
                    await self.fetch(nxt)
                except Exception as e:  # noqa: BLE001
                    log.info("Prefetch of %s failed (will retry when it plays): %s", nxt.page_url, e)

            asyncio.create_task(_go())

    # ---- embeds
    def footer_text(self) -> str:
        return (f"Loop: {LOOP_LABELS[self.loop_mode]} · Autoplay: {'On' if self.autoplay else 'Off'}"
                f" · Volume: {round(self.volume * 100)}%")

    def now_playing_embed(self, show_progress: bool = False) -> discord.Embed:
        t = self.current
        assert t is not None
        paused = self.voice is not None and self.voice.is_paused()
        e = discord.Embed(
            title="⏸️ Paused" if paused else "🎶 Now playing",
            description=f"### {t.link()}",
            color=GREY if paused else BLUE,
        )
        if show_progress and t.duration:
            el = self.elapsed()
            e.add_field(
                name="Progress",
                value=f"{progress_bar(el, t.duration)}\n`{fmt_time(el)} / {fmt_time(t.duration)}`",
                inline=False,
            )
        else:
            e.add_field(name="Length", value=fmt_time(t.duration))
        e.add_field(name="Asked for by", value=t.who())
        if self.queue:
            nxt = self.queue[0]
            more = f" (+{len(self.queue) - 1} more)" if len(self.queue) > 1 else ""
            e.add_field(name="Up next", value=f"{nxt.link()}{more}", inline=False)
        elif self.autoplay:
            e.add_field(name="Up next", value="🤖 Autoplay picks a related song", inline=False)
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.set_footer(text=self.footer_text())
        return e

    # ---- playback
    async def play_next(self, interaction: discord.Interaction | None = None) -> None:
        async with self._lock:
            await self._play_next(interaction)

    async def _play_next(self, interaction: discord.Interaction | None) -> None:
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            return  # something is already playing; nothing to do
        finished = self.current
        skipped = self._skip_requested
        self._skip_requested = False
        self.current = None
        await self.clear_buttons()

        if finished and not self._stopping:
            if self.loop_mode == LOOP_SONG and not skipped:
                self.queue.appendleft(finished)
            elif self.loop_mode == LOOP_QUEUE:
                self.queue.append(finished)

        vc = self.voice
        if self._stopping or not vc or not vc.is_connected():
            self._stopping = False
            return

        if not self.queue and finished and self.autoplay:
            await self._add_autoplay(finished)

        while self.queue:
            track = self.queue.popleft()
            try:
                await self.fetch(track)
                source = self.make_source(track.file_path)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not play %s: %s", track.page_url, e)
                await self.send(embed=discord.Embed(
                    title="⚠️ Couldn't play a song, skipping it",
                    description=f"{track.link()}\n`{str(e)[:300]}`",
                    color=RED,
                ))
                continue

            if self._stopping or not vc.is_connected():  # stopped while downloading
                source.cleanup()
                self._stopping = False
                return
            self.cancel_idle()
            self.current = track
            vid = video_id(track.page_url)
            if vid:
                self.history.append(vid)
            vc.play(source, after=self._after)
            self._reset_clock()
            self.prefetch_next()
            conn = getattr(vc, "_connection", None)
            log.info("Playing %s | voice connected=%s dave_version=%s can_encrypt=%s",
                     track.page_url, vc.is_connected(),
                     getattr(conn, "dave_protocol_version", "?"), getattr(conn, "can_encrypt", "?"))
            view = PlayerControls(self)
            embed = self.now_playing_embed()
            if interaction is not None:
                self.np_message = await interaction.followup.send(embed=embed, view=view, wait=True)
            else:
                self.np_message = await self.send(embed=embed, view=view)
            return

        # Nothing left to play.
        if finished or interaction is not None:
            e = discord.Embed(
                title="✅ Queue finished",
                description="Add more with `/play`, or turn on `/autoplay` to keep the music going.",
                color=GREY,
            )
            if IDLE_TIMEOUT:
                e.set_footer(text=f"I'll leave the voice channel if nothing plays for {IDLE_TEXT}.")
            if interaction is not None:
                await interaction.followup.send(embed=e)
            else:
                await self.send(embed=e)
        self.start_idle()

    async def _add_autoplay(self, seed: Track) -> None:
        vid = video_id(seed.page_url)
        if not vid:
            return
        try:
            entries = await run_blocking(_related, vid)
        except Exception as e:  # noqa: BLE001
            log.warning("Autoplay lookup failed for %s: %s", vid, e)
            return
        recent = set(self.history)
        for entry in entries:
            t = info_to_track(entry, self.guild.me, autoplay=True)
            if video_id(t.page_url) not in recent:
                self.queue.append(t)
                return

    def _after(self, error: Exception | None) -> None:
        # Called from the audio thread when a song ends or is stopped.
        if error:
            log.warning("Player error: %s", error)
        asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

    def skip(self) -> None:
        self._skip_requested = True
        if self.voice:
            self.voice.stop()  # the after-callback plays the next song

    def seek(self, position: int) -> None:
        """Jump to a position in the current song by restarting FFmpeg there."""
        vc = self.voice
        if not vc or not self.current or not self.current.file_path or not vc.source:
            raise RuntimeError("Nothing is playing.")
        was_paused = vc.is_paused()
        old = vc.source
        vc.source = self.make_source(self.current.file_path, position)  # this also un-pauses
        old.cleanup()
        if was_paused:
            vc.pause()
        self._reset_clock(position)

    async def stop(self, reason: str | None = None) -> None:
        self.queue.clear()
        self._stopping = bool(self.current)
        self.cancel_idle()
        await self.clear_buttons()
        self.current = None
        if self.voice:
            await self.voice.disconnect(force=False)
        if reason:
            await self.send(embed=discord.Embed(description=reason, color=GREY))

    # ---- idle handling
    def start_idle(self) -> None:
        self.cancel_idle()
        if IDLE_TIMEOUT > 0:
            self._idle_task = asyncio.create_task(self._idle_wait())

    def cancel_idle(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _idle_wait(self) -> None:
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
        except asyncio.CancelledError:
            return
        vc = self.voice
        if vc and not vc.is_playing() and not vc.is_paused():
            self._idle_task = None
            await self.stop(f"👋 Nothing played for {IDLE_TEXT}, so I left the voice channel.")

    # ---- messages
    async def send(self, **kwargs) -> discord.Message | None:
        if not self.text_channel:
            return None
        try:
            return await self.text_channel.send(**kwargs)
        except discord.HTTPException:
            return None

    async def clear_buttons(self) -> None:
        """Remove the buttons from the last 'Now playing' message."""
        msg, self.np_message = self.np_message, None
        if msg:
            try:
                await msg.edit(view=None)
            except discord.HTTPException:
                pass

    async def refresh_np(self) -> None:
        if self.current:
            self.prefetch_next()  # the next song may have changed
        if self.np_message and self.current:
            try:
                await self.np_message.edit(embed=self.now_playing_embed(), view=PlayerControls(self))
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------- buttons


def in_bot_channel(player: GuildPlayer, user: discord.abc.User) -> bool:
    vc = player.voice
    return bool(vc and isinstance(user, discord.Member) and user.voice
                and user.voice.channel == vc.channel)


class PlayerControls(discord.ui.View):
    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player
        vc = player.voice
        paused = vc is not None and vc.is_paused()
        self.pause_resume.emoji = "▶️" if paused else "⏸️"
        self.pause_resume.label = "Resume" if paused else "Pause"
        self.loop.label = f"Loop: {LOOP_LABELS[player.loop_mode]}"
        self.loop.style = (discord.ButtonStyle.primary if player.loop_mode != LOOP_OFF
                           else discord.ButtonStyle.secondary)
        self.autoplay_button.label = f"Autoplay: {'On' if player.autoplay else 'Off'}"
        self.autoplay_button.style = (discord.ButtonStyle.primary if player.autoplay
                                      else discord.ButtonStyle.secondary)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if in_bot_channel(self.player, interaction.user):
            return True
        await interaction.response.send_message(
            "Join my voice channel to use the buttons.", ephemeral=True)
        return False

    async def _refresh(self, interaction: discord.Interaction) -> None:
        if self.player.current:
            await interaction.response.edit_message(
                embed=self.player.now_playing_embed(), view=PlayerControls(self.player))
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="⏸️", label="Pause", style=discord.ButtonStyle.secondary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, _button: discord.ui.Button):
        vc = self.player.voice
        if vc and vc.is_playing():
            vc.pause()
            self.player.mark_paused()
        elif vc and vc.is_paused():
            vc.resume()
            self.player.mark_resumed()
        await self._refresh(interaction)

    @discord.ui.button(emoji="⏭️", label="Skip", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer()
        cur = self.player.current
        await interaction.channel.send(embed=discord.Embed(
            description=f"⏭️ {interaction.user.mention} skipped {cur.link()}" if cur else "⏭️ Skipped",
            color=GREY))
        self.player.skip()

    @discord.ui.button(emoji="⏹️", label="Stop", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer()
        await self.player.stop(f"⏹️ {interaction.user.mention} stopped the music and cleared the queue.")

    @discord.ui.button(emoji="🔁", label="Loop: Off", style=discord.ButtonStyle.secondary, row=1)
    async def loop(self, interaction: discord.Interaction, _button: discord.ui.Button):
        order = [LOOP_OFF, LOOP_SONG, LOOP_QUEUE]
        p = self.player
        p.loop_mode = order[(order.index(p.loop_mode) + 1) % len(order)]
        await self._refresh(interaction)

    @discord.ui.button(emoji="🤖", label="Autoplay: Off", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.player.autoplay = not self.player.autoplay
        await self._refresh(interaction)

    @discord.ui.button(emoji="🔀", label="Shuffle", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle(self, interaction: discord.Interaction, _button: discord.ui.Button):
        p = self.player
        if len(p.queue) < 2:
            await interaction.response.send_message("Not enough songs in the queue to shuffle.", ephemeral=True)
            return
        items = list(p.queue)
        random.shuffle(items)
        p.queue = deque(items)
        await self._refresh(interaction)
        await interaction.followup.send(embed=discord.Embed(
            description=f"🔀 {interaction.user.mention} shuffled the queue ({len(items)} songs).", color=GREY))


class SearchPicker(discord.ui.View):
    def __init__(self, author: discord.Member, results: list[dict]):
        super().__init__(timeout=120)
        self.author = author
        self.results = results
        self.message: discord.Message | None = None
        options = [
            discord.SelectOption(
                label=(r.get("title") or "Unknown")[:100],
                description=" · ".join(x for x in (fmt_time(r.get("duration")),
                                                   (r.get("channel") or r.get("uploader") or "")[:60]) if x)[:100],
                value=str(i),
                emoji=f"{i + 1}\ufe0f\u20e3",
            )
            for i, r in enumerate(results)
        ]
        self.select = discord.ui.Select(placeholder="Pick a song to play", options=options)
        self.select.callback = self.on_pick
        self.add_item(self.select)

    async def on_pick(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "This search belongs to someone else. Use `/search` yourself.", ephemeral=True)
            return
        problem = voice_problem(interaction)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        info = self.results[int(self.select.values[0])]
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description=f"🔎 Picked **{info.get('title')}**", color=GREY), view=None)
        await enqueue(interaction, [info_to_track(info, interaction.user)])

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------- client


class HodeWerner(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.players: dict[int, GuildPlayer] = {}
        self.playlists = PlaylistStore(DATA_DIR / "playlists.json")

    def player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self, guild)
        return self.players[guild.id]

    async def setup_hook(self) -> None:
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening, name="/play"))
        for g in self.guilds:
            if g.id != GUILD_ID:
                log.info("Leaving unexpected server %s", g.name)
                await g.leave()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != GUILD_ID:
            await guild.leave()

    async def on_voice_state_update(self, member, before, after) -> None:
        vc = member.guild.voice_client
        if not vc or not vc.channel:
            return
        # Leave when everyone else has left the voice channel.
        if all(m.bot for m in vc.channel.members):
            await self.player(member.guild).stop("👋 Everyone left the voice channel, so I left too.")


bot = HodeWerner()


def in_home_server():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.guild is not None and interaction.guild.id == GUILD_ID

    return app_commands.check(predicate)


def info_embed(text: str, color: discord.Color = GREY) -> discord.Embed:
    return discord.Embed(description=text, color=color)


def voice_problem(interaction: discord.Interaction) -> str | None:
    """Why this user can't add music right now, or None if they can."""
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
        return "Join a voice channel first."
    vc = interaction.guild.voice_client
    if vc and vc.channel != member.voice.channel and (vc.is_playing() or vc.is_paused()):
        return f"I'm already playing in {vc.channel.mention}. Join there to add songs."
    return None


async def require_same_channel(interaction: discord.Interaction) -> GuildPlayer | None:
    """Return the player if the user is in the bot's voice channel, otherwise reply and return None."""
    p = bot.player(interaction.guild)
    if not p.voice:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return None
    if not in_bot_channel(p, interaction.user):
        await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
        return None
    return p


async def enqueue(interaction: discord.Interaction, tracks: list[Track],
                  playlist_title: str | None = None, playlist_icon: str = "📜") -> None:
    """Join the user's channel, add tracks, and report. The interaction must already be responded to."""
    member = interaction.user
    p = bot.player(interaction.guild)
    p.text_channel = interaction.channel
    vc = p.voice
    if vc is None:
        p._stopping = False
        vc = await member.voice.channel.connect(self_deaf=True)
    elif vc.channel != member.voice.channel:
        await vc.move_to(member.voice.channel)
    p.cancel_idle()

    busy = vc.is_playing() or vc.is_paused() or p.current is not None or p._lock.locked()
    wait_before = p.queue_seconds() + (
        max(0, (p.current.duration or 0) - int(p.elapsed())) if p.current else 0)
    # Drop autoplay picks once a person adds something.
    p.queue = deque(t for t in p.queue if not t.autoplay)
    p.queue.extend(tracks)

    if playlist_title:
        total = sum(t.duration or 0 for t in tracks)
        e = discord.Embed(
            title=f"{playlist_icon} Playlist added",
            description=f"**{playlist_title}**\n{len(tracks)} songs · {fmt_time(total)}",
            color=GREEN,
        )
        if tracks[0].thumbnail:
            e.set_thumbnail(url=tracks[0].thumbnail)
        e.set_footer(text=f"Added by {member.display_name}")
        await interaction.followup.send(embed=e)
        if not busy:
            await p.play_next()
        else:
            await p.refresh_np()
        return

    t = tracks[0]
    if not busy:
        await p.play_next(interaction)
        return
    e = discord.Embed(title="➕ Added to queue", description=f"### {t.link()}", color=GREEN)
    e.add_field(name="Position", value=str(len(p.queue)))
    e.add_field(name="Length", value=fmt_time(t.duration))
    e.add_field(name="Plays in about", value=fmt_time(wait_before) if wait_before else "soon")
    if t.thumbnail:
        e.set_thumbnail(url=t.thumbnail)
    e.set_footer(text=f"Added by {member.display_name}")
    await interaction.followup.send(embed=e)
    await p.refresh_np()


# ---------------------------------------------------------------- commands: adding music


@bot.tree.command(description="Play a song or playlist from a YouTube link, or search by words")
@app_commands.describe(query="YouTube link, playlist link, or what to search for")
@in_home_server()
async def play(interaction: discord.Interaction, query: str):
    problem = voice_problem(interaction)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    try:
        playlist_title, infos = await run_blocking(_resolve, query)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(embed=info_embed(f"⚠️ Couldn't find that.\n`{str(e)[:300]}`", RED))
        return
    await enqueue(interaction, [info_to_track(i, interaction.user) for i in infos], playlist_title)


@bot.tree.command(description="Search YouTube and pick from the top 5 results")
@app_commands.describe(query="What to search for")
@in_home_server()
async def search(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    try:
        results = await run_blocking(_search, query)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(embed=info_embed(f"⚠️ Search failed.\n`{str(e)[:300]}`", RED))
        return
    if not results:
        await interaction.followup.send(embed=info_embed("No results found."))
        return
    lines = []
    for i, r in enumerate(results, 1):
        t = info_to_track(r, interaction.user)
        who = r.get("channel") or r.get("uploader")
        lines.append(f"`{i}.` {t.link()} · `{fmt_time(r.get('duration'))}`" + (f" · {who}" if who else ""))
    e = discord.Embed(title=f"🔎 Results for “{query[:80]}”", description="\n".join(lines), color=BLUE)
    e.set_footer(text="Pick one below within 2 minutes.")
    view = SearchPicker(interaction.user, results)
    view.message = await interaction.followup.send(embed=e, view=view, wait=True)


# ---------------------------------------------------------------- commands: info


@bot.tree.command(description="Show the queue")
@app_commands.describe(page="Page number, 10 songs per page")
@in_home_server()
async def queue(interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
    p = bot.player(interaction.guild)
    if not p.current and not p.queue:
        await interaction.response.send_message(embed=info_embed("The queue is empty. Add songs with `/play`."))
        return
    per_page = 10
    pages = max(1, (len(p.queue) + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    e = discord.Embed(title="📋 Queue", color=BLUE)
    if p.current:
        e.add_field(
            name="Now playing",
            value=f"{p.current.link()} · `{fmt_time(p.current.duration)}` · {p.current.who()}",
            inline=False,
        )
    lines = [
        f"`{i}.` {t.link()} · `{fmt_time(t.duration)}` · {t.who()}"
        for i, t in enumerate(list(p.queue)[start:start + per_page], start + 1)
    ]
    e.add_field(name="Up next", value="\n".join(lines) or "Nothing yet.", inline=False)
    e.set_footer(text=f"Page {page}/{pages} · {len(p.queue)} songs · {fmt_time(p.queue_seconds())} total · "
                      + p.footer_text())
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="nowplaying", description="Show the current song")
@in_home_server()
async def nowplaying(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    if not p.current:
        await interaction.response.send_message(embed=info_embed("Nothing is playing."), ephemeral=True)
        return
    await interaction.response.send_message(embed=p.now_playing_embed(show_progress=True))


# ---------------------------------------------------------------- commands: controls


@bot.tree.command(description="Skip the current song")
@in_home_server()
async def skip(interaction: discord.Interaction):
    p = await require_same_channel(interaction)
    if not p:
        return
    if not p.current:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=info_embed(f"⏭️ {interaction.user.mention} skipped {p.current.link()}"))
    p.skip()


@bot.tree.command(description="Pause the music")
@in_home_server()
async def pause(interaction: discord.Interaction):
    p = await require_same_channel(interaction)
    if not p:
        return
    if p.voice.is_playing():
        p.voice.pause()
        p.mark_paused()
        await interaction.response.send_message(embed=info_embed("⏸️ Paused. Use `/resume` to continue."))
        await p.refresh_np()
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(description="Resume the music")
@in_home_server()
async def resume(interaction: discord.Interaction):
    p = await require_same_channel(interaction)
    if not p:
        return
    if p.voice.is_paused():
        p.voice.resume()
        p.mark_resumed()
        await interaction.response.send_message(embed=info_embed("▶️ Resumed."))
        await p.refresh_np()
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(description="Stop, clear the queue, and leave the voice channel")
@in_home_server()
async def stop(interaction: discord.Interaction):
    p = await require_same_channel(interaction)
    if not p:
        return
    await interaction.response.send_message(
        embed=info_embed(f"⏹️ {interaction.user.mention} stopped the music and cleared the queue."))
    await p.stop()


@bot.tree.command(description="Jump to a time in the current song")
@app_commands.describe(time="For example 1:30 or 90")
@in_home_server()
async def seek(interaction: discord.Interaction, time: str):
    p = await require_same_channel(interaction)
    if not p:
        return
    secs = parse_time(time)
    if secs is None:
        await interaction.response.send_message("Use a time like `1:30` or `90`.", ephemeral=True)
        return
    if not p.current:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    if p.current.duration and secs >= p.current.duration:
        await interaction.response.send_message(
            f"This song is only {fmt_time(p.current.duration)} long.", ephemeral=True)
        return
    p.seek(secs)
    await interaction.response.send_message(embed=info_embed(f"⏩ Jumped to `{fmt_time(secs) if secs else '0:00'}`"))


@bot.tree.command(description="Start the current song again from the beginning")
@in_home_server()
async def replay(interaction: discord.Interaction):
    p = await require_same_channel(interaction)
    if not p:
        return
    if not p.current:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    p.seek(0)
    await interaction.response.send_message(embed=info_embed(f"⏮️ Playing {p.current.link()} from the start."))


# ---------------------------------------------------------------- commands: queue


@bot.tree.command(description="Move a song to another place in the queue")
@app_commands.describe(position="Its number in /queue", to="The new number (1 = play next)")
@app_commands.rename(position="from")
@in_home_server()
async def move(interaction: discord.Interaction, position: app_commands.Range[int, 1],
               to: app_commands.Range[int, 1]):
    p = await require_same_channel(interaction)
    if not p:
        return
    n = len(p.queue)
    if position > n or to > n:
        await interaction.response.send_message(f"There are only {n} songs in the queue.", ephemeral=True)
        return
    items = list(p.queue)
    t = items.pop(position - 1)
    items.insert(to - 1, t)
    p.queue = deque(items)
    await interaction.response.send_message(embed=info_embed(f"↕️ Moved {t.link()} to position {to}."))
    await p.refresh_np()


@bot.tree.command(description="Remove a song from the queue")
@app_commands.describe(position="Its number in /queue")
@in_home_server()
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    p = await require_same_channel(interaction)
    if not p:
        return
    if position > len(p.queue):
        await interaction.response.send_message(
            f"There are only {len(p.queue)} songs in the queue.", ephemeral=True)
        return
    t = p.queue[position - 1]
    del p.queue[position - 1]
    await interaction.response.send_message(embed=info_embed(f"🗑️ Removed {t.link()} from the queue."))
    await p.refresh_np()


@bot.tree.command(description="Shuffle the queue")
@in_home_server()
async def shuffle(interaction: discord.Interaction):
    p = await require_same_channel(interaction)
    if not p:
        return
    if len(p.queue) < 2:
        await interaction.response.send_message("Not enough songs in the queue to shuffle.", ephemeral=True)
        return
    items = list(p.queue)
    random.shuffle(items)
    p.queue = deque(items)
    await interaction.response.send_message(embed=info_embed(f"🔀 Shuffled {len(items)} songs."))
    await p.refresh_np()


@bot.tree.command(description="Clear the queue but keep the current song playing")
@in_home_server()
async def clear(interaction: discord.Interaction):
    p = await require_same_channel(interaction)
    if not p:
        return
    n = len(p.queue)
    p.queue.clear()
    await interaction.response.send_message(embed=info_embed(f"🧹 Cleared {n} songs from the queue."))
    await p.refresh_np()


# ---------------------------------------------------------------- commands: settings


@bot.tree.command(description="Repeat the current song or the whole queue")
@app_commands.describe(mode="What to repeat")
@app_commands.choices(mode=[
    app_commands.Choice(name="Off", value=LOOP_OFF),
    app_commands.Choice(name="This song", value=LOOP_SONG),
    app_commands.Choice(name="Whole queue", value=LOOP_QUEUE),
])
@in_home_server()
async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    p = bot.player(interaction.guild)
    p.loop_mode = mode.value
    await interaction.response.send_message(embed=info_embed(f"🔁 Loop: **{LOOP_LABELS[p.loop_mode]}**"))
    await p.refresh_np()


@bot.tree.command(description="Keep playing related songs when the queue runs out")
@app_commands.describe(enabled="On or off")
@app_commands.choices(enabled=[
    app_commands.Choice(name="On", value=1),
    app_commands.Choice(name="Off", value=0),
])
@in_home_server()
async def autoplay(interaction: discord.Interaction, enabled: app_commands.Choice[int]):
    p = bot.player(interaction.guild)
    p.autoplay = bool(enabled.value)
    if not p.autoplay:
        p.queue = deque(t for t in p.queue if not t.autoplay)
    await interaction.response.send_message(embed=info_embed(
        "🤖 Autoplay is **on**. When the queue runs out, I'll pick related songs."
        if p.autoplay else "🤖 Autoplay is **off**."))
    await p.refresh_np()


@bot.tree.command(description="Set the volume")
@app_commands.describe(percent="0 to 100")
@in_home_server()
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]):
    p = await require_same_channel(interaction)
    if not p:
        return
    p.volume = percent / 100
    if isinstance(p.voice.source, discord.PCMVolumeTransformer):
        p.voice.source.volume = p.volume
    icon = "🔇" if percent == 0 else "🔉" if percent < 50 else "🔊"
    await interaction.response.send_message(embed=info_embed(f"{icon} Volume set to **{percent}%**"))
    await p.refresh_np()


# ---------------------------------------------------------------- commands: saved playlists


class PlaylistGroup(app_commands.Group):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.guild is not None and interaction.guild.id == GUILD_ID

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        log.exception("Playlist command failed", exc_info=error)
        send = (interaction.followup.send if interaction.response.is_done()
                else interaction.response.send_message)
        await send("Something went wrong with that playlist command.", ephemeral=True)


playlist_group = PlaylistGroup(name="playlist", description="Saved playlists for everyone on the server")


async def playlist_name_autocomplete(_interaction: discord.Interaction, current: str):
    cur = current.lower()
    return [app_commands.Choice(name=n, value=n) for n in bot.playlists.names() if cur in n.lower()][:25]


@playlist_group.command(name="save", description="Save the current song and queue as a playlist")
@app_commands.describe(name="Name for the playlist (saving with an existing name replaces it)")
async def playlist_save(interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]):
    p = bot.player(interaction.guild)
    tracks = ([p.current] if p.current else []) + list(p.queue)
    tracks = [t for t in tracks if not t.autoplay] or tracks
    if not tracks:
        await interaction.response.send_message("Nothing to save. Play some songs first.", ephemeral=True)
        return
    replaced = bot.playlists.save(name, interaction.user.display_name, tracks)
    total = sum(t.duration or 0 for t in tracks)
    await interaction.response.send_message(embed=discord.Embed(
        title="💾 Playlist updated" if replaced else "💾 Playlist saved",
        description=f"**{name.strip()}**\n{len(tracks)} songs · {fmt_time(total)}\n"
                    f"Play it any time with `/playlist play`.",
        color=GREEN))


@playlist_group.command(name="play", description="Add a saved playlist to the queue")
@app_commands.describe(name="Which playlist", shuffle="Shuffle the songs first")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_play(interaction: discord.Interaction, name: str, shuffle: bool = False):
    pl = bot.playlists.get(name)
    if not pl:
        await interaction.response.send_message(f"There's no playlist called **{name}**.", ephemeral=True)
        return
    problem = voice_problem(interaction)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    songs = list(pl["songs"])
    if shuffle:
        random.shuffle(songs)
    tracks = [
        Track(title=s["title"], page_url=s["url"], duration=s.get("duration"),
              thumbnail=s.get("thumbnail"), requested_by=interaction.user)
        for s in songs
    ]
    await enqueue(interaction, tracks, pl["name"] + (" (shuffled)" if shuffle else ""), "💾")


@playlist_group.command(name="list", description="Show all saved playlists")
async def playlist_list(interaction: discord.Interaction):
    names = bot.playlists.names()
    if not names:
        await interaction.response.send_message(embed=info_embed(
            "No saved playlists yet. Queue some songs and use `/playlist save`."))
        return
    lines = []
    for n in names:
        pl = bot.playlists.get(n)
        total = sum(s.get("duration") or 0 for s in pl["songs"])
        lines.append(f"**{pl['name']}** · {len(pl['songs'])} songs · `{fmt_time(total)}` · by {pl['owner']}")
    await interaction.response.send_message(embed=discord.Embed(
        title="💾 Saved playlists", description="\n".join(lines)[:4000], color=BLUE))


@playlist_group.command(name="show", description="Show the songs in a saved playlist")
@app_commands.describe(name="Which playlist")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_show(interaction: discord.Interaction, name: str):
    pl = bot.playlists.get(name)
    if not pl:
        await interaction.response.send_message(f"There's no playlist called **{name}**.", ephemeral=True)
        return
    lines = []
    for i, s in enumerate(pl["songs"], 1):
        title = s["title"].replace("[", "(").replace("]", ")")
        lines.append(f"`{i}.` [{title}]({s['url']}) · `{fmt_time(s.get('duration'))}`")
    text = ""
    for i, line in enumerate(lines):
        if len(text) + len(line) > 3900:
            text += f"…and {len(lines) - i} more"
            break
        text += line + "\n"
    e = discord.Embed(title=f"💾 {pl['name']}", description=text, color=BLUE)
    e.set_footer(text=f"{len(lines)} songs · saved by {pl['owner']}")
    await interaction.response.send_message(embed=e)


@playlist_group.command(name="delete", description="Delete a saved playlist")
@app_commands.describe(name="Which playlist")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_delete(interaction: discord.Interaction, name: str):
    pl = bot.playlists.get(name)
    if not pl or not bot.playlists.delete(name):
        await interaction.response.send_message(f"There's no playlist called **{name}**.", ephemeral=True)
        return
    await interaction.response.send_message(embed=info_embed(
        f"🗑️ Deleted the playlist **{pl['name']}** ({len(pl['songs'])} songs)."))


bot.tree.add_command(playlist_group)


if __name__ == "__main__":
    discord.utils.setup_logging(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
    bot.run(TOKEN, log_handler=None)
