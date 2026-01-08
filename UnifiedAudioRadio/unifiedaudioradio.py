import asyncio
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
import discord
from redbot.core import Config, commands

log = logging.getLogger("red.greyasuka.unified_audio_radio")

RB_API = "https://de2.api.radio-browser.info/json"
USER_AGENT = "Red-DiscordBot/UnifiedAudioRadio (GreyHairAsuka APPROVED++)"

SEARCH_LIMIT = 50
PAGE_SIZE = 10
REACTION_TIMEOUT = 35.0

WATCHDOG_INTERVAL = 8.0
SUMMON_GRACE_SECONDS = 10.0
HOME_GRACE_SECONDS = 15.0

REASSURE_TICK = 10.0


def _looks_like_youtube(text: str) -> bool:
    t = (text or "").lower()
    return any(
        x in t
        for x in (
            "youtu.be",
            "youtube.com",
            "youtube ",
            " yt ",
            "ytsearch:",
            "youtubemusic",
        )
    )


@dataclass(frozen=True)
class Station:
    name: str
    country: str
    bitrate: int
    tags: str
    stream_url: str

    @staticmethod
    def from_rb(payload: Dict[str, Any]) -> Optional["Station"]:
        name = (payload.get("name") or "Unnamed Station").strip()
        country = (payload.get("country") or "??").strip()
        bitrate = int(payload.get("bitrate") or 0)
        tags = (payload.get("tags") or "No tags").strip()

        stream_url = (payload.get("url_resolved") or payload.get("url") or "").strip()
        if not stream_url:
            return None

        u = stream_url.lower()
        if not (u.startswith("http://") or u.startswith("https://")):
            return None
        if (
            u.startswith("http://127.")
            or u.startswith("https://127.")
            or u.startswith("http://localhost")
            or u.startswith("https://localhost")
        ):
            return None

        return Station(name=name, country=country, bitrate=bitrate, tags=tags, stream_url=stream_url)


class UnifiedAudioRadio(commands.Cog):
    """
    Grey Hair Asuka Unified Media Safety Layer (Audio + Radio + Watchdog)

    Adds DJ Asuka-specific behavior:
    - Notify DJ when YouTube starts
    - Notify DJ when YouTube stops
    - DJ can request radio back via g!djradio (notifies Grey Hair Asuka)
    - g!rrrestore instantly restores the last saved station
    - NEW: panic recovery + status:
        - g!rrstatus
        - g!rrunlock [home|restore|full]
        - g!rrpanic, g!rrsuspend, g!rrresume, g!rrhard
    """

    AUDIO_SUMMON_ALIASES: Set[str] = {"summon", "join", "connect"}
    AUDIO_DISCONNECT_ALIASES: Set[str] = {"disconnect", "dc", "leave"}
    AUDIO_STOP_ALIASES: Set[str] = {"stop"}
    AUDIO_PLAY_ALIASES: Set[str] = {"play", "local", "playurl", "playlist"}  # best-effort; varies by Audio version

    def __init__(self, bot):
        self.bot = bot
        self.http: Optional[aiohttp.ClientSession] = None

        self.config = Config.get_conf(self, identifier=90210, force_registration=True)
        self.config.register_global(
            # perimeter
            bound=False,
            allowed_guild_id=None,
            control_text_channel_id=None,
            allowed_voice_channel_id=None,
            audit_channel_id=None,
            bound_owner_user_id=None,  # who bound
            # posture
            panic_locked=True,
            autopanic_enabled=True,
            autopanic_reason=None,
            suspended=False,
            suspend_reason=None,
            hard_mode=True,
            # radio state
            stream_url=None,
            station_name=None,
            last_station_name=None,
            last_station_stream_url=None,
            last_search_query=None,
            # audio intent
            audio_intent_active=False,
            audio_intent_started_monotonic=0.0,
            # filters
            min_bitrate_kbps=64,
            block_tags_csv="phonk,earrape,nsfw",
            # DJ Asuka identity
            dj_user_id=None,
            # periodic reassurance (general)
            periodic_reassure_enabled=True,
            reassure_interval_in_vc_sec=300,
            reassure_interval_out_vc_sec=900,
            reassure_use_dm=True,
            reassure_fallback_channel_id=None,
            # DJ start/stop notifications
            dj_notify_youtube_start=True,
            dj_notify_youtube_stop=True,
        )

        self._stations_cache: List[Station] = []
        self._http_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        self._autopanic_lock = asyncio.Lock()

        self._watchdog_task: Optional[asyncio.Task] = None
        self._reassure_task: Optional[asyncio.Task] = None

        self._allow_summon_until: float = 0.0
        self._home_grace_until: float = 0.0

        self._last_reassure_in_vc_ts: float = 0.0
        self._last_reassure_out_vc_ts: float = 0.0

    # -------------------------
    # Red lifecycle
    # -------------------------
    async def cog_load(self) -> None:
        if self.http is None or self.http.closed:
            timeout = aiohttp.ClientTimeout(total=18)
            self.http = aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT})

        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self.bot.loop.create_task(self._watchdog_loop())

        if self._reassure_task is None or self._reassure_task.done():
            self._reassure_task = self.bot.loop.create_task(self._periodic_reassurance_loop())

    def cog_unload(self):
        for t in (self._watchdog_task, self._reassure_task):
            if t and not t.done():
                t.cancel()
        if self.http and not self.http.closed:
            self.bot.loop.create_task(self._close_http())

    async def _close_http(self):
        try:
            if self.http and not self.http.closed:
                await self.http.close()
        except Exception:
            pass

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self.http is None or self.http.closed:
            await self.cog_load()
        return self.http  # type: ignore[return-value]

    # -------------------------
    # Voice abstraction (discord.py VoiceClient vs Red Audio Lavalink Player)
    # -------------------------
    def _vc_connected(self, vc) -> bool:
        if vc is None:
            return False
        meth = getattr(vc, "is_connected", None)
        if callable(meth):
            try:
                return bool(meth())
            except Exception:
                return False
        val = getattr(vc, "connected", None)
        if isinstance(val, bool):
            return val
        val2 = getattr(vc, "is_connected", None)
        if isinstance(val2, bool):
            return val2
        return False

    def _vc_channel_id(self, vc) -> Optional[int]:
        if vc is None:
            return None
        ch = getattr(vc, "channel", None)
        if ch is not None and hasattr(ch, "id"):
            try:
                return int(ch.id)
            except Exception:
                pass
        for attr in ("channel_id", "voice_channel_id"):
            v = getattr(vc, attr, None)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return None

    async def _vc_disconnect(self, vc) -> None:
        if vc is None:
            return
        disc = getattr(vc, "disconnect", None)
        if callable(disc):
            try:
                await disc(force=True)
                return
            except TypeError:
                await disc()
            except Exception:
                pass

    def _player_is_playing(self, guild: discord.Guild) -> bool:
        p = guild.voice_client
        v = getattr(p, "is_playing", None)
        if isinstance(v, bool):
            return v
        if callable(v):
            try:
                return bool(v())
            except Exception:
                return False
        return False

    # -------------------------
    # State helpers
    # -------------------------
    async def _radio_active(self) -> bool:
        return bool(await self.config.stream_url() and await self.config.station_name())

    async def _audio_intent_active(self) -> bool:
        return bool(await self.config.audio_intent_active())

    async def _any_active(self, guild: Optional[discord.Guild] = None) -> bool:
        if await self._radio_active():
            return True
        if await self._audio_intent_active():
            return True
        if guild and self._player_is_playing(guild):
            return True
        return False

    async def _bot_is_home(self, guild: discord.Guild) -> bool:
        allowed_vc_id = await self.config.allowed_voice_channel_id()
        if not allowed_vc_id:
            return False
        vc = guild.voice_client
        if not self._vc_connected(vc):
            return False
        cid = self._vc_channel_id(vc)
        return bool(cid and int(cid) == int(allowed_vc_id))

    # -------------------------
    # Audit / notify
    # -------------------------
    async def _audit_security(self, guild: discord.Guild, reason: str) -> None:
        try:
            log.warning("[SECURITY] %s | guild=%s (%s)", reason, guild.name, guild.id)
            audit_channel_id = await self.config.audit_channel_id()
            if audit_channel_id:
                ch = guild.get_channel(int(audit_channel_id))
                if ch:
                    embed = discord.Embed(title="🛡️ Security", description=reason, color=discord.Color.orange())
                    await ch.send(embed=embed)
        except Exception:
            pass

    async def _control_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        cid = await self.config.control_text_channel_id()
        if not cid:
            return None
        ch = guild.get_channel(int(cid))
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _notify_control(self, guild: discord.Guild, title: str, description: str, color: discord.Color):
        try:
            ch = await self._control_channel(guild)
            if not ch:
                return
            embed = discord.Embed(title=title, description=description, color=color)
            await ch.send(embed=embed)
        except Exception:
            pass

    async def _set_presence(self, label: Optional[str]) -> None:
        try:
            if label:
                await self.bot.change_presence(activity=discord.Game(name=label))
            else:
                await self.bot.change_presence(activity=None)
        except Exception:
            pass

    async def _suspend(self, reason: str) -> None:
        await self.config.suspended.set(True)
        await self.config.suspend_reason.set(reason)
        await self._set_presence(None)

    async def _unsuspend(self) -> None:
        await self.config.suspended.set(False)
        await self.config.suspend_reason.set(None)

    async def _clear_radio_state(self) -> None:
        await self.config.stream_url.set(None)
        await self.config.station_name.set(None)

    async def _clear_audio_intent(self) -> None:
        await self.config.audio_intent_active.set(False)
        await self.config.audio_intent_started_monotonic.set(0.0)

    async def _autopanic(self, guild: discord.Guild, reason: str) -> None:
        if not await self.config.autopanic_enabled():
            return
        async with self._autopanic_lock:
            if await self.config.panic_locked():
                return

            await self.config.panic_locked.set(True)
            await self.config.autopanic_reason.set(reason)

            try:
                await self._vc_disconnect(guild.voice_client)
            except Exception:
                pass

            await self._clear_radio_state()
            await self._clear_audio_intent()
            await self._set_presence(None)

            await self._audit_security(guild, f"Auto-panic: {reason}")
            await self._notify_control(
                guild,
                "🛑 Panic Engaged",
                f"{reason}\n\nUse `rrunlock` in the control channel (while you’re in the locked VC).",
                discord.Color.red(),
            )

    # -------------------------
    # DJ messaging
    # -------------------------
    async def _send_to_dj(self, guild: discord.Guild, embed: discord.Embed) -> None:
        dj_user_id = await self.config.dj_user_id()
        if not dj_user_id:
            return
        member = guild.get_member(int(dj_user_id))
        if not member:
            return

        use_dm = await self.config.reassure_use_dm()
        fallback_id = await self.config.reassure_fallback_channel_id()
        control_id = await self.config.control_text_channel_id()

        if use_dm:
            try:
                await member.send(embed=embed)
                return
            except Exception:
                pass

        ch = None
        if fallback_id:
            ch = guild.get_channel(int(fallback_id))
        if ch is None and control_id:
            ch = guild.get_channel(int(control_id))
        if not isinstance(ch, discord.TextChannel):
            return

        try:
            await ch.send(content=member.mention, embed=embed)
        except Exception:
            pass

    async def _dj_youtube_started(self, guild: discord.Guild):
        if not await self.config.dj_notify_youtube_start():
            return
        last_station = await self.config.last_station_name()
        line = f"Last radio station saved: **{last_station}**" if last_station else "Radio state saved."
        embed = discord.Embed(
            title="🖤 It’s okay.",
            description="YouTube started. I’m still here. You’re safe.\n" + line,
            color=discord.Color.dark_teal(),
        )
        embed.set_footer(text="If you want the radio back, use: g!djradio")
        await self._send_to_dj(guild, embed)

    async def _dj_youtube_stopped(self, guild: discord.Guild):
        if not await self.config.dj_notify_youtube_stop():
            return
        last_station = await self.config.last_station_name()
        msg = (
            f"YouTube stopped. If you want the radio back: **g!djradio**\nLast station: **{last_station}**"
            if last_station
            else "YouTube stopped. If you want the radio back: **g!djradio**"
        )
        embed = discord.Embed(title="🖤 It’s okay.", description=msg, color=discord.Color.dark_teal())
        embed.set_footer(text="Grey Hair Asuka protocol: stable transitions.")
        await self._send_to_dj(guild, embed)

    # -------------------------
    # Cog gating (this cog only)
    # -------------------------
    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False

        cmd = (ctx.command.qualified_name if ctx.command else "").lower()

        # DJ-only command bypass
        if cmd == "djradio":
            allowed_guild_id = await self.config.allowed_guild_id()
            dj_user_id = await self.config.dj_user_id()
            if allowed_guild_id and ctx.guild.id != int(allowed_guild_id):
                return False
            return bool(dj_user_id and ctx.author.id == int(dj_user_id))

        # Owner only for everything else
        try:
            is_owner = await self.bot.is_owner(ctx.author)  # type: ignore[arg-type]
        except Exception:
            is_owner = False
        if not is_owner:
            return False

        prebind_ok = {"rrbind", "rrstatus"}
        if not await self.config.bound():
            if cmd in prebind_ok:
                return True
            await ctx.send(f"Locked. Bind first with `{ctx.clean_prefix}rrbind`.")
            return False

        allowed_guild_id = await self.config.allowed_guild_id()
        if allowed_guild_id and ctx.guild.id != int(allowed_guild_id):
            await self._audit_security(ctx.guild, f"Denied: wrong guild ({ctx.guild.id})")
            return False

        bypass_control = {"rrcontrol", "rrstatus", "rrpanic", "rrunlock", "rrresume", "rrsuspend", "rrhard"}
        control_id = await self.config.control_text_channel_id()
        if control_id and ctx.channel.id != int(control_id):
            if cmd not in bypass_control:
                await self._audit_security(ctx.guild, f"Denied: outside control channel ({ctx.channel.id})")
                return False

        if await self.config.panic_locked():
            panic_ok = {"rrstatus", "rrunlock", "rrpanic", "rrcontrol", "rrhard"}
            if cmd in panic_ok:
                return True
            await ctx.send("Panic lock is active.")
            return False

        return True

    async def _require_owner_in_allowed_vc(self, ctx: commands.Context) -> bool:
        allowed_vc_id = await self.config.allowed_voice_channel_id()
        if not allowed_vc_id:
            await ctx.send("Voice lock is not set. Rebind with rrbind.")
            return False

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("Get in the locked voice channel first.")
            return False

        if ctx.author.voice.channel.id != int(allowed_vc_id):
            await ctx.send("Wrong voice channel.")
            await self._audit_security(ctx.guild, f"Denied: owner not in allowed VC ({ctx.author.voice.channel.id})")
            return False

        return True

    async def _require_bot_home(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            return False
        if not await self._bot_is_home(ctx.guild):
            await ctx.send("Bot is not home. Use `rrhome` while you are in the locked VC.")
            return False
        return True

    # -------------------------
    # Audio wrappers
    # -------------------------
    def _get_cmd(self, name: str):
        return self.bot.get_command(name)

    async def _invoke_audio(self, ctx: commands.Context, name: str, **kwargs) -> Tuple[bool, str]:
        cmd = self._get_cmd(name)
        if cmd is None:
            return False, f"❌ Audio command `{name}` not found."
        try:
            await ctx.invoke(cmd, **kwargs)
            return True, ""
        except Exception as e:
            return False, f"❌ Audio `{name}` failed: `{type(e).__name__}: {e}`"

    async def _audio_play(self, ctx: commands.Context, query: str) -> bool:
        ok, msg = await self._invoke_audio(ctx, "play", query=query)
        if not ok:
            await ctx.send(msg)
            return False
        return True

    async def _audio_stop(self, ctx: commands.Context) -> None:
        await self._invoke_audio(ctx, "stop")

    async def _audio_summon(self, ctx: commands.Context) -> bool:
        ok, msg = await self._invoke_audio(ctx, "summon")
        if not ok:
            await ctx.send(msg)
            return False
        return True

    # -------------------------
    # Audio compatibility tripwire + DJ start/stop detection
    # -------------------------
    async def _audio_cmd_is_allowed(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            return False
        if not await self.config.bound():
            return False

        allowed_guild_id = await self.config.allowed_guild_id()
        if allowed_guild_id and ctx.guild.id != int(allowed_guild_id):
            return False

        try:
            is_owner = await self.bot.is_owner(ctx.author)  # type: ignore[arg-type]
        except Exception:
            is_owner = False
        if not is_owner:
            return False

        control_id = await self.config.control_text_channel_id()
        if control_id and ctx.channel.id != int(control_id):
            return False

        allowed_vc_id = await self.config.allowed_voice_channel_id()
        if not allowed_vc_id:
            return False
        if not ctx.author.voice or not ctx.author.voice.channel:
            return False
        if ctx.author.voice.channel.id != int(allowed_vc_id):
            return False

        return True

    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context):
        try:
            if not ctx.guild or not ctx.command:
                return

            cog = (ctx.command.cog_name or "").lower()
            if cog != "audio":
                return

            name = (ctx.command.name or "").lower()
            allowed = await self._audio_cmd_is_allowed(ctx)

            # If rrhome just authorized summon, ignore.
            if name in self.AUDIO_SUMMON_ALIASES and time.monotonic() <= self._allow_summon_until:
                return

            # Allowed inside perimeter: keep state consistent + DJ notifications.
            if allowed:
                # PLAY: detect YouTube-ish start
                if name in self.AUDIO_PLAY_ALIASES:
                    content = (ctx.message.content or "")
                    if _looks_like_youtube(content):
                        # Save last station if radio was active
                        if await self._radio_active():
                            await self.config.last_station_name.set(await self.config.station_name())
                            await self.config.last_station_stream_url.set(await self.config.stream_url())
                            await self._clear_radio_state()
                            await self._set_presence(None)

                        await self.config.audio_intent_active.set(True)
                        await self.config.audio_intent_started_monotonic.set(float(time.monotonic()))
                        await self._dj_youtube_started(ctx.guild)

                # STOP: stop notifications + clear states
                if name in self.AUDIO_STOP_ALIASES:
                    await self._clear_radio_state()
                    await self._clear_audio_intent()
                    await self._set_presence(None)
                    await self._dj_youtube_stopped(ctx.guild)

                # DISCONNECT: clear + grace windows
                if name in self.AUDIO_DISCONNECT_ALIASES:
                    await self._clear_radio_state()
                    await self._clear_audio_intent()
                    await self._set_presence(None)
                    now = time.monotonic()
                    self._home_grace_until = now + HOME_GRACE_SECONDS
                    self._allow_summon_until = now + SUMMON_GRACE_SECONDS
                    await self._dj_youtube_stopped(ctx.guild)

                return

            # Not allowed: only react if something is active.
            if not await self._any_active(ctx.guild):
                return

            hard = await self.config.hard_mode()
            if hard:
                await self._autopanic(ctx.guild, f"Audio `{name}` used outside perimeter while active")
            else:
                await self._suspend(f"Audio `{name}` used outside perimeter while active")
                await self._notify_control(
                    ctx.guild,
                    "🟡 Suspended (Soft)",
                    f"Audio `{name}` was used outside the perimeter while media was active.\n"
                    f"Use `rrresume` (in control channel, in locked VC) to continue safely.",
                    discord.Color.gold(),
                )

        except Exception:
            pass

    # -------------------------
    # Watchdog
    # -------------------------
    async def _watchdog_loop(self):
        await self.bot.wait_until_ready()
        while self.bot.is_ready():
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL)

                if not await self.config.bound():
                    continue
                if await self.config.panic_locked():
                    continue
                if await self.config.suspended():
                    continue
                if time.monotonic() <= self._home_grace_until:
                    continue

                guild_id = await self.config.allowed_guild_id()
                if not guild_id:
                    continue

                guild = self.bot.get_guild(int(guild_id))
                if not guild:
                    continue

                active = await self._any_active(guild)
                if not active:
                    continue

                if not await self._bot_is_home(guild):
                    await self._autopanic(guild, "Watchdog: media active but bot is not home")
                    continue

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Watchdog loop error")

    # -------------------------
    # Periodic reassurance loop (general)
    # -------------------------
    async def _periodic_reassurance_loop(self):
        await self.bot.wait_until_ready()
        while self.bot.is_ready():
            try:
                await asyncio.sleep(REASSURE_TICK)

                if not await self.config.bound():
                    continue
                if await self.config.panic_locked():
                    continue
                if await self.config.suspended():
                    continue
                if not await self.config.periodic_reassure_enabled():
                    continue

                guild_id = await self.config.allowed_guild_id()
                allowed_vc_id = await self.config.allowed_voice_channel_id()
                dj_user_id = await self.config.dj_user_id()
                if not guild_id or not allowed_vc_id or not dj_user_id:
                    continue

                guild = self.bot.get_guild(int(guild_id))
                if not guild:
                    continue

                if not await self._any_active(guild):
                    continue
                if not await self._bot_is_home(guild):
                    continue

                member = guild.get_member(int(dj_user_id))
                if not member:
                    continue

                dj_in_allowed_vc = bool(
                    member.voice and member.voice.channel and member.voice.channel.id == int(allowed_vc_id)
                )

                radio = await self._radio_active()
                station_name = await self.config.station_name() if radio else None
                last_station = await self.config.last_station_name()

                if radio and station_name:
                    line = f"**Station:** {station_name}"
                elif last_station:
                    line = f"**Media:** playback active\n**Radio saved:** {last_station}"
                else:
                    line = "**Media:** playback active"

                now = time.monotonic()

                if dj_in_allowed_vc:
                    interval = max(int(await self.config.reassure_interval_in_vc_sec() or 0), 30)
                    if now - self._last_reassure_in_vc_ts < interval:
                        continue
                    self._last_reassure_in_vc_ts = now

                    embed = discord.Embed(
                        title="🖤 It’s okay.",
                        description=f"You're safe. I’m here. We’re staying home.\n{line}",
                        color=discord.Color.dark_teal(),
                    )
                    embed.set_footer(text="Grey Hair Asuka protocol: reassurance (in VC).")
                    await self._send_to_dj(guild, embed)
                else:
                    interval = max(int(await self.config.reassure_interval_out_vc_sec() or 0), 60)
                    if now - self._last_reassure_out_vc_ts < interval:
                        continue
                    self._last_reassure_out_vc_ts = now

                    embed = discord.Embed(
                        title="🖤 It’s okay.",
                        description=f"I'm still here. You're safe even if you're not in the room.\n{line}",
                        color=discord.Color.dark_teal(),
                    )
                    embed.set_footer(text="Grey Hair Asuka protocol: reassurance (out of VC).")
                    await self._send_to_dj(guild, embed)

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Periodic reassurance loop error")

    # -------------------------
    # Radio-browser
    # -------------------------
    async def _rb_get_json(self, path: str) -> Optional[Any]:
        session = await self._ensure_http()
        url = f"{RB_API}/{path.lstrip('/')}"
        try:
            async with self._http_lock:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    def _blocked_by_tags(self, station: Station, blocked_tags: List[str]) -> bool:
        blob = f"{station.tags} {station.name}".lower()
        return any(t and t in blob for t in blocked_tags)

    def _page_embed(
        self, ctx: commands.Context, query: str, page: int, total_pages: int, page_items: List[Station]
    ) -> discord.Embed:
        prefix = ctx.clean_prefix
        embed = discord.Embed(
            title=f"🔎 Results for '{query}' (Page {page + 1}/{total_pages})",
            color=discord.Color.green(),
        )
        start_index = page * PAGE_SIZE
        for idx, s in enumerate(page_items, start=start_index + 1):
            tags = s.tags if s.tags else "No tags"
            if len(tags) > 100:
                tags = tags[:97] + "..."
            embed.add_field(
                name=f"{idx}. {s.name} ({s.country})"[:256],
                value=f"Bitrate: {s.bitrate} kbps\nTags: {tags}"[:1024],
                inline=False,
            )
        embed.set_footer(text=f"Owner-only. Use {prefix}playstation <number>.")
        return embed

    # -------------------------
    # Panic / Status helpers + commands
    # -------------------------
    def _fmt_yesno(self, b: bool) -> str:
        return "✅ Yes" if b else "❌ No"

    def _fmt_id(self, x: Optional[int]) -> str:
        return str(x) if x else "—"

    def _fmt_secs(self, seconds: float) -> str:
        seconds = max(0.0, float(seconds))
        m = int(seconds // 60)
        s = int(seconds % 60)
        if m <= 0:
            return f"{s}s"
        return f"{m}m {s}s"

    @commands.is_owner()
    @commands.command()
    async def rrstatus(self, ctx: commands.Context):
        """Show current safety state (panic/suspended), binding, channels, and media state."""
        if ctx.guild is None:
            return

        bound = await self.config.bound()
        allowed_guild_id = await self.config.allowed_guild_id()
        control_id = await self.config.control_text_channel_id()
        allowed_vc_id = await self.config.allowed_voice_channel_id()
        audit_id = await self.config.audit_channel_id()
        bound_owner_id = await self.config.bound_owner_user_id()

        panic = await self.config.panic_locked()
        autopanic_enabled = await self.config.autopanic_enabled()
        autopanic_reason = await self.config.autopanic_reason()

        suspended = await self.config.suspended()
        suspend_reason = await self.config.suspend_reason()
        hard = await self.config.hard_mode()

        radio_active = await self._radio_active()
        station_name = await self.config.station_name()
        stream_url = await self.config.stream_url()
        last_station = await self.config.last_station_name()
        last_station_url = await self.config.last_station_stream_url()

        audio_intent = await self.config.audio_intent_active()
        audio_intent_started = float(await self.config.audio_intent_started_monotonic() or 0.0)

        vc = ctx.guild.voice_client
        vc_connected = self._vc_connected(vc)
        vc_channel_id = self._vc_channel_id(vc)
        bot_home = await self._bot_is_home(ctx.guild)
        player_playing = self._player_is_playing(ctx.guild)

        now = time.monotonic()
        intent_age = self._fmt_secs(now - audio_intent_started) if (audio_intent and audio_intent_started) else "—"

        control_ch = ctx.guild.get_channel(int(control_id)) if control_id else None
        audit_ch = ctx.guild.get_channel(int(audit_id)) if audit_id else None
        allowed_vc = ctx.guild.get_channel(int(allowed_vc_id)) if allowed_vc_id else None
        bot_vc = ctx.guild.get_channel(int(vc_channel_id)) if vc_channel_id else None

        embed = discord.Embed(
            title="🛡️ Unified Audio/Radio Status",
            color=discord.Color.dark_teal() if not panic else discord.Color.red(),
        )

        embed.add_field(
            name="Binding / Perimeter",
            value=(
                f"**Bound:** {self._fmt_yesno(bool(bound))}\n"
                f"**Allowed Guild:** {self._fmt_id(int(allowed_guild_id) if allowed_guild_id else None)}\n"
                f"**Control Channel:** {control_ch.mention if isinstance(control_ch, discord.TextChannel) else self._fmt_id(int(control_id) if control_id else None)}\n"
                f"**Locked VC:** {allowed_vc.mention if allowed_vc else self._fmt_id(int(allowed_vc_id) if allowed_vc_id else None)}\n"
                f"**Audit Channel:** {audit_ch.mention if isinstance(audit_ch, discord.TextChannel) else self._fmt_id(int(audit_id) if audit_id else None)}\n"
                f"**Bound Owner ID:** {self._fmt_id(int(bound_owner_id) if bound_owner_id else None)}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Safety State",
            value=(
                f"**Panic Locked:** {self._fmt_yesno(bool(panic))}\n"
                f"**AutoPanic Enabled:** {self._fmt_yesno(bool(autopanic_enabled))}\n"
                f"**AutoPanic Reason:** {autopanic_reason or '—'}\n"
                f"**Suspended:** {self._fmt_yesno(bool(suspended))}\n"
                f"**Suspend Reason:** {suspend_reason or '—'}\n"
                f"**Hard Mode:** {self._fmt_yesno(bool(hard))}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Bot Voice / Playback",
            value=(
                f"**VC Connected:** {self._fmt_yesno(bool(vc_connected))}\n"
                f"**Bot VC:** {bot_vc.mention if bot_vc else (self._fmt_id(int(vc_channel_id)) if vc_channel_id else '—')}\n"
                f"**Bot Is Home:** {self._fmt_yesno(bool(bot_home))}\n"
                f"**Player Is Playing (Audio):** {self._fmt_yesno(bool(player_playing))}\n"
                f"**Audio Intent Active:** {self._fmt_yesno(bool(audio_intent))}\n"
                f"**Audio Intent Age:** {intent_age}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Radio State",
            value=(
                f"**Radio Active (saved):** {self._fmt_yesno(bool(radio_active))}\n"
                f"**Station:** {station_name or '—'}\n"
                f"**Stream URL:** {stream_url or '—'}\n"
                f"**Last Station:** {last_station or '—'}\n"
                f"**Last URL:** {last_station_url or '—'}"
            ),
            inline=False,
        )

        embed.set_footer(text="Commands: rrstatus | rrunlock [home|restore|full] | rrpanic | rrsuspend | rrresume | rrhard")
        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command()
    async def rrpanic(self, ctx: commands.Context, *, reason: str = "Manual panic engaged"):
        """Manually engage panic: disconnect, clear state, lock commands."""
        if ctx.guild is None:
            return
        if not await self.config.bound():
            await ctx.send("Not bound yet. Use `rrbind` first.")
            return

        await self.config.panic_locked.set(True)
        await self.config.autopanic_reason.set(reason)

        try:
            await self._vc_disconnect(ctx.guild.voice_client)
        except Exception:
            pass

        await self._clear_radio_state()
        await self._clear_audio_intent()
        await self._set_presence(None)

        await self._audit_security(ctx.guild, f"Manual panic: {reason}")
        await self._notify_control(
            ctx.guild,
            "🛑 Panic Engaged (Manual)",
            f"{reason}\n\nUse `rrunlock` in the control channel (while you’re in the locked VC).",
            discord.Color.red(),
        )
        await ctx.send("Panic engaged.")

    @commands.is_owner()
    @commands.command()
    async def rrsuspend(self, ctx: commands.Context, *, reason: str = "Manual suspend"):
        """Soft-stop: blocks playback actions until rrresume (does not lock like panic)."""
        if ctx.guild is None:
            return
        if not await self.config.bound():
            await ctx.send("Not bound yet. Use `rrbind` first.")
            return
        if not await self._require_owner_in_allowed_vc(ctx):
            return

        await self._suspend(reason)

        # Optional: disconnect + clear media so the world is quiet and stable.
        try:
            await self._vc_disconnect(ctx.guild.voice_client)
        except Exception:
            pass
        await self._clear_radio_state()
        await self._clear_audio_intent()

        await self._notify_control(
            ctx.guild,
            "🟡 Suspended",
            f"{reason}\n\nUse `rrresume` (in control channel, in locked VC) to continue.",
            discord.Color.gold(),
        )
        await ctx.send("Suspended.")

    @commands.is_owner()
    @commands.command()
    async def rrresume(self, ctx: commands.Context):
        """Resume after suspension (requires owner in locked VC)."""
        if ctx.guild is None:
            return
        if not await self.config.bound():
            await ctx.send("Not bound yet. Use `rrbind` first.")
            return
        if not await self._require_owner_in_allowed_vc(ctx):
            return

        await self._unsuspend()
        await ctx.send("Resumed. Use `rrhome` if you need to bring her back home.")

    @commands.is_owner()
    @commands.command()
    async def rrhard(self, ctx: commands.Context, mode: Optional[str] = None):
        """
        Toggle hard mode:
        - hard: autopanic on perimeter violation
        - soft: suspend on perimeter violation
        """
        if mode is None:
            cur = bool(await self.config.hard_mode())
            await ctx.send(f"Hard mode is currently: **{cur}**. Use `rrhard on` or `rrhard off`.")
            return

        m = (mode or "").strip().lower()
        if m in ("on", "true", "1", "hard"):
            await self.config.hard_mode.set(True)
            await ctx.send("Hard mode: **ON** (violations trigger panic).")
        elif m in ("off", "false", "0", "soft"):
            await self.config.hard_mode.set(False)
            await ctx.send("Hard mode: **OFF** (violations trigger suspend).")
        else:
            await ctx.send("Use `rrhard on` or `rrhard off`.")

    @commands.is_owner()
    @commands.command()
    async def rrunlock(self, ctx: commands.Context, mode: Optional[str] = None):
        """
        Recover from panic lock.

        Modes:
        - (none)   : just clears panic lock
        - home     : clears panic + summons bot to locked VC
        - restore  : clears panic + summons home + restores last station
        - full     : same as restore
        """
        if ctx.guild is None:
            return
        if not await self.config.bound():
            await ctx.send("Not bound yet. Use `rrbind` first.")
            return

        # Only unlock if owner is in the locked VC (prevents hostile unlocks).
        if not await self._require_owner_in_allowed_vc(ctx):
            return

        # Clear panic + reasons
        await self.config.panic_locked.set(False)
        await self.config.autopanic_reason.set(None)

        # Panic recovery should fully reset posture
        await self._unsuspend()

        # Clear internal flags (prevents "panic still active" / stale-state confusion)
        await self._clear_radio_state()
        await self._clear_audio_intent()
        await self._set_presence(None)

        m = (mode or "").strip().lower()
        do_home = m in ("home", "restore", "full")
        do_restore = m in ("restore", "full")

        if do_home:
            now = time.monotonic()
            self._allow_summon_until = now + SUMMON_GRACE_SECONDS
            self._home_grace_until = now + HOME_GRACE_SECONDS

            ok = await self._audio_summon(ctx)
            if not ok:
                await ctx.send("Unlocked, but summon failed. Try `rrhome`.")
                return

        if do_restore:
            last_name = await self.config.last_station_name()
            last_url = await self.config.last_station_stream_url()

            if last_name and last_url:
                await self._clear_audio_intent()
                await self.config.stream_url.set(last_url)
                await self.config.station_name.set(last_name)

                ok = await self._audio_play(ctx, last_url)
                if ok:
                    await self._set_presence(f"📻 {last_name}")
            else:
                await ctx.send("Unlocked + home, but no saved station exists yet (nothing to restore).")

        await self._notify_control(
            ctx.guild,
            "✅ Panic Cleared",
            f"Panic lock cleared by {ctx.author.mention}."
            + ("\nHome: **YES**" if do_home else "\nHome: **NO**")
            + ("\nRestore attempted: **YES**" if do_restore else "\nRestore attempted: **NO**"),
            discord.Color.green(),
        )

        if do_restore:
            await ctx.send("Unlocked. Home restored (and station restore attempted).")
        elif do_home:
            await ctx.send("Unlocked. She’s coming home.")
        else:
            await ctx.send("Unlocked. Panic is cleared.")

    # -------------------------
    # Owner commands (bind/control/home/radio)
    # -------------------------
    @commands.is_owner()
    @commands.command()
    async def rrbind(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("Join the voice channel you want locked first, then run rrbind again.")
            return

        await self.config.allowed_guild_id.set(ctx.guild.id)
        await self.config.control_text_channel_id.set(ctx.channel.id)
        await self.config.allowed_voice_channel_id.set(ctx.author.voice.channel.id)
        await self.config.bound_owner_user_id.set(ctx.author.id)

        await self.config.bound.set(True)
        await self.config.panic_locked.set(False)
        await self.config.autopanic_enabled.set(True)
        await self.config.autopanic_reason.set(None)

        await self._unsuspend()
        await self._clear_radio_state()
        await self._clear_audio_intent()
        await self._set_presence(None)

        await ctx.send("Bound. Use rrsetdj to set DJ Asuka. Use rrhome to bring her home.")

    @commands.is_owner()
    @commands.command()
    async def rrsetdj(self, ctx: commands.Context, member: discord.Member):
        await self.config.dj_user_id.set(member.id)
        await ctx.send(f"DJ Asuka target set to: {member.mention}")

    @commands.is_owner()
    @commands.command()
    async def rrcontrol(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        if ctx.guild is None:
            return
        if not await self.config.bound():
            await ctx.send(f"Not bound yet. Use `{ctx.clean_prefix}rrbind` first.")
            return

        allowed_guild_id = await self.config.allowed_guild_id()
        if allowed_guild_id and ctx.guild.id != int(allowed_guild_id):
            await ctx.send("Wrong guild.")
            return

        if not await self._require_owner_in_allowed_vc(ctx):
            return

        target = channel or ctx.channel
        await self.config.control_text_channel_id.set(target.id)

        if not await self.config.reassure_fallback_channel_id():
            await self.config.reassure_fallback_channel_id.set(target.id)

        embed = discord.Embed(
            title="🛡️ Control Channel Re-Keyed",
            description=f"Control channel moved to {target.mention}.",
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Grey Hair Asuka protocol: re-key control room.")
        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command()
    async def rrhome(self, ctx: commands.Context):
        if await self.config.panic_locked():
            await ctx.send("Panic lock is active.")
            return
        if not await self._require_owner_in_allowed_vc(ctx):
            return

        if ctx.guild and await self._bot_is_home(ctx.guild):
            await ctx.send("She’s already home.")
            return

        now = time.monotonic()
        self._allow_summon_until = now + SUMMON_GRACE_SECONDS
        self._home_grace_until = now + HOME_GRACE_SECONDS

        await self._unsuspend()

        ok = await self._audio_summon(ctx)
        if not ok:
            return

        if await self._radio_active():
            station = await self.config.station_name()
            if station:
                await self._set_presence(f"📻 {station}")

        await ctx.send("Home command executed.")

    @commands.is_owner()
    @commands.command()
    async def searchstations(self, ctx: commands.Context, *, query: str):
        if not await self._require_owner_in_allowed_vc(ctx):
            return
        if not await self._require_bot_home(ctx):
            return
        if await self.config.suspended():
            await ctx.send("Suspended. Use `rrresume`.")
            return

        q = query.strip()
        if not q:
            await ctx.send("Give me a query.")
            return

        encoded = urllib.parse.quote(q)
        data = await self._rb_get_json(f"stations/byname/{encoded}")
        if not isinstance(data, list) or not data:
            await ctx.send("No stations found.")
            return

        min_bitrate = int(await self.config.min_bitrate_kbps() or 0)
        blocked_csv = (await self.config.block_tags_csv() or "").lower()
        blocked_tags = [t.strip() for t in blocked_csv.split(",") if t.strip()]

        stations: List[Station] = []
        for raw in data[:SEARCH_LIMIT]:
            if not isinstance(raw, dict):
                continue
            s = Station.from_rb(raw)
            if not s:
                continue
            if s.bitrate and s.bitrate < min_bitrate:
                continue
            if self._blocked_by_tags(s, blocked_tags):
                continue
            stations.append(s)

        if not stations:
            await ctx.send("No usable stations after filters.")
            return

        async with self._cache_lock:
            self._stations_cache = stations

        await self.config.last_search_query.set(q)

        pages = [stations[i : i + PAGE_SIZE] for i in range(0, len(stations), PAGE_SIZE)]
        total_pages = len(pages)
        page = 0

        msg = await ctx.send(embed=self._page_embed(ctx, q, page, total_pages, pages[page]))

        controls = ["⏮️", "◀️", "▶️", "⏭️"]
        try:
            for c in controls:
                await msg.add_reaction(c)
        except discord.Forbidden:
            return

        def check(reaction: discord.Reaction, user: discord.abc.User) -> bool:
            return user.id == ctx.author.id and reaction.message.id == msg.id and str(reaction.emoji) in controls

        while True:
            try:
                reaction, user = await self.bot.wait_for("reaction_add", timeout=REACTION_TIMEOUT, check=check)
                emoji = str(reaction.emoji)

                if emoji == "⏮️":
                    page = 0
                elif emoji == "◀️" and page > 0:
                    page -= 1
                elif emoji == "▶️" and page < total_pages - 1:
                    page += 1
                elif emoji == "⏭️":
                    page = total_pages - 1

                await msg.edit(embed=self._page_embed(ctx, q, page, total_pages, pages[page]))

                try:
                    await msg.remove_reaction(reaction, user)
                except discord.Forbidden:
                    pass

            except asyncio.TimeoutError:
                break

        try:
            await msg.clear_reactions()
        except discord.Forbidden:
            pass

    @commands.is_owner()
    @commands.command()
    async def playstation(self, ctx: commands.Context, index: int):
        if not await self._require_owner_in_allowed_vc(ctx):
            return
        if not await self._require_bot_home(ctx):
            return
        if await self.config.suspended():
            await ctx.send("Suspended. Use `rrresume`.")
            return

        async with self._cache_lock:
            stations = list(self._stations_cache)

        if not stations:
            await ctx.send(f"No cached results. Run `{ctx.clean_prefix}searchstations <query>` first.")
            return
        if index < 1 or index > len(stations):
            await ctx.send(f"Invalid station number. Use 1-{len(stations)}.")
            return

        station = stations[index - 1]

        await self.config.last_station_name.set(station.name)
        await self.config.last_station_stream_url.set(station.stream_url)

        await self._clear_audio_intent()

        await self.config.stream_url.set(station.stream_url)
        await self.config.station_name.set(station.name)

        ok = await self._audio_play(ctx, station.stream_url)
        if not ok:
            return

        await self._set_presence(f"📻 {station.name}")

        embed = discord.Embed(
            title="📻 Station Playing",
            description=f"**Station:** {station.name}\n**Country:** {station.country}\n🔗 [Stream Link]({station.stream_url})",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="No track titles. Station name only.")
        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command()
    async def rrrestore(self, ctx: commands.Context):
        """
        Restore the last saved radio station immediately.
        Requires: owner in locked VC + bot home + not suspended/panic.
        """
        if not await self._require_owner_in_allowed_vc(ctx):
            return
        if not await self._require_bot_home(ctx):
            return
        if await self.config.suspended():
            await ctx.send("Suspended. Use `rrresume` first.")
            return
        if await self.config.panic_locked():
            await ctx.send("Panic lock is active. Use `rrunlock restore`.")
            return

        last_name = await self.config.last_station_name()
        last_url = await self.config.last_station_stream_url()

        if not last_name or not last_url:
            await ctx.send("No saved station to restore yet. Use `playstation` first.")
            return

        await self._clear_audio_intent()

        await self.config.stream_url.set(last_url)
        await self.config.station_name.set(last_name)

        ok = await self._audio_play(ctx, last_url)
        if not ok:
            return

        await self._set_presence(f"📻 {last_name}")

        embed = discord.Embed(
            title="📻 Restored",
            description=f"**Station:** {last_name}\n🔗 [Stream Link]({last_url})",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Grey Hair Asuka protocol: instant restore.")
        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command()
    async def stopstation(self, ctx: commands.Context):
        if not await self._require_owner_in_allowed_vc(ctx):
            return
        await self._audio_stop(ctx)
        await self._clear_radio_state()
        await self._set_presence(None)
        await ctx.send(embed=discord.Embed(title="⏹️ Stopped", description="Radio stream stopped.", color=discord.Color.red()))

    # -------------------------
    # DJ command: request radio back (notify Grey Hair Asuka)
    # -------------------------
    @commands.guild_only()
    @commands.command()
    async def djradio(self, ctx: commands.Context, *, note: str = ""):
        if not ctx.guild:
            return

        allowed_guild_id = await self.config.allowed_guild_id()
        if allowed_guild_id and ctx.guild.id != int(allowed_guild_id):
            return

        dj_user_id = await self.config.dj_user_id()
        if not dj_user_id or ctx.author.id != int(dj_user_id):
            return  # silent fail by design

        control = await self._control_channel(ctx.guild)
        if not control:
            return

        owner_id = await self.config.bound_owner_user_id()
        owner_mention = f"<@{int(owner_id)}>" if owner_id else "@owner"

        last_station = await self.config.last_station_name()
        last_line = f"Last station saved: **{last_station}**" if last_station else "No saved station on record."

        note = (note or "").strip()
        note_line = f"\n**DJ note:** {note}" if note else ""

        embed = discord.Embed(
            title="📻 DJ Asuka requested the radio back",
            description=f"{owner_mention}\n{last_line}{note_line}\n\nSuggested action: `g!rrrestore` (or `g!rrunlock restore` if panic)",
            color=discord.Color.dark_teal(),
        )
        embed.set_footer(text="Grey Hair Asuka protocol: restore stability.")
        await control.send(embed=embed)

        confirm = discord.Embed(
            title="🖤 Okay.",
            description="I told Grey Hair Asuka you want the radio back.",
            color=discord.Color.dark_teal(),
        )
        await ctx.send(embed=confirm)


async def setup(bot):
    await bot.add_cog(UnifiedAudioRadio(bot))
