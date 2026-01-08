import asyncio
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from redbot.core import Config, commands

log = logging.getLogger("red.greyasuka.radio")

RB_API = "https://de2.api.radio-browser.info/json"
USER_AGENT = "Red-DiscordBot/RedRadio (GreyHairAsuka APPROVED++)"

SEARCH_LIMIT = 50
PAGE_SIZE = 10
REACTION_TIMEOUT = 35.0

WATCHDOG_INTERVAL = 8.0
SUMMON_GRACE_SECONDS = 6.0

REASSURE_TICK = 10.0  # internal tick; sending is interval-gated


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
        # Fail-closed: only http(s), no localhost.
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


class RedRadio(commands.Cog):
    """
    Grey Hair Asuka Approved Radio Cog (Red) + DJ Asuka reassurance:

    Perimeter:
    - One guild, one control text channel, one allowed VC (set by rrbind)
    - Owner-only for every command in this cog (enforced by cog_check + @is_owner)

    Stability:
    - Owner must be in the allowed VC to do any control action
    - Bot must be "home" (in allowed VC) to search/play/stop stations
    - rrhome is the ONLY allowed path to Audio summon
      -> If Audio summon is used directly (even by owner), auto-panic (unless rrhome just authorized it)

    DJ Asuka reassurance:
    - If station is set and bot is home, periodically reassures DJ Asuka:
        * IN-VC cadence (default 5 min)
        * OUT-VC cadence (default 15 min)
      Delivery: DM-first, fallback to a configured channel (or control channel)
    """

    def __init__(self, bot):
        self.bot = bot
        self.http: Optional[aiohttp.ClientSession] = None

        self.config = Config.get_conf(self, identifier=90210, force_registration=True)
        self.config.register_global(
            # playback state (station only)
            stream_url=None,
            station_name=None,
            last_search_query=None,
            # perimeter
            allowed_guild_id=None,
            control_text_channel_id=None,
            allowed_voice_channel_id=None,
            # filters
            min_bitrate_kbps=64,
            block_tags_csv="phonk,earrape,nsfw",
            # security + ops
            audit_channel_id=None,
            bound=False,
            panic_locked=True,
            autopanic_enabled=True,
            autopanic_reason=None,
            # DJ Asuka identity
            dj_user_id=None,
            # periodic reassurance
            periodic_reassure_enabled=True,
            reassure_interval_in_vc_sec=300,     # 5 minutes
            reassure_interval_out_vc_sec=900,    # 15 minutes
            reassure_use_dm=True,
            reassure_fallback_channel_id=None,   # if DMs fail/disabled; else control channel
        )

        self._stations_cache: List[Station] = []
        self._http_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        self._autopanic_lock = asyncio.Lock()

        self._watchdog_task: Optional[asyncio.Task] = None
        self._reassure_task: Optional[asyncio.Task] = None

        # rrhome authorization window for Audio summon
        self._allow_summon_until: float = 0.0

        # Rate limits for reassurance
        self._last_reassure_in_vc_ts: float = 0.0
        self._last_reassure_out_vc_ts: float = 0.0

    # -------------------------
    # Lifecycle
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
        # Cancel loops
        for t in (self._watchdog_task, self._reassure_task):
            if t and not t.done():
                t.cancel()

        # Close http safely
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

    def format_help_for_context(self, ctx: commands.Context) -> str:
        try:
            is_owner = self.bot.is_owner(ctx.author)  # type: ignore[arg-type]
        except Exception:
            is_owner = False
        return super().format_help_for_context(ctx) if is_owner else "Restricted."

    # -------------------------
    # Gates
    # -------------------------
    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False

        try:
            is_owner = await self.bot.is_owner(ctx.author)  # type: ignore[arg-type]
        except Exception:
            is_owner = False
        if not is_owner:
            return False

        if not await self.config.bound():
            if ctx.command and ctx.command.qualified_name in {"rrbind", "rrstatus"}:
                return True
            await ctx.send(f"Locked. Bind first with `{ctx.clean_prefix}rrbind`.")
            return False

        allowed_guild_id = await self.config.allowed_guild_id()
        control_text_channel_id = await self.config.control_text_channel_id()

        if allowed_guild_id and ctx.guild.id != allowed_guild_id:
            await self._audit_security(ctx.guild, f"Denied: wrong guild ({ctx.guild.id})")
            return False

        if control_text_channel_id and ctx.channel.id != control_text_channel_id:
            await self._audit_security(ctx.guild, f"Denied: outside control channel ({ctx.channel.id})")
            return False

        if await self.config.panic_locked():
            if ctx.command and ctx.command.qualified_name in {"rrstatus", "rrunlock", "rrpanic"}:
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

        if ctx.author.voice.channel.id != allowed_vc_id:
            await ctx.send("Wrong voice channel.")
            await self._audit_security(ctx.guild, f"Denied: owner not in allowed VC ({ctx.author.voice.channel.id})")
            return False

        return True

    async def _bot_is_home(self, guild: discord.Guild) -> bool:
        allowed_vc_id = await self.config.allowed_voice_channel_id()
        if not allowed_vc_id:
            return False
        vc = guild.voice_client
        return bool(vc and vc.is_connected() and vc.channel and vc.channel.id == allowed_vc_id)

    async def _require_bot_home(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            return False

        allowed_vc_id = await self.config.allowed_voice_channel_id()
        if not allowed_vc_id:
            await ctx.send("Voice lock is not set. Rebind with rrbind.")
            return False

        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected() or not vc.channel:
            await ctx.send("Bot is not in voice. Use `rrhome` while you are in the locked VC.")
            return False

        if vc.channel.id != allowed_vc_id:
            await self._autopanic(ctx.guild, f"Control attempted while bot in wrong VC ({vc.channel.id})")
            await ctx.send("Bot is in the wrong voice channel. Auto-panic engaged.")
            return False

        return True

    # -------------------------
    # Audit + panic
    # -------------------------
    async def _audit_security(self, guild: discord.Guild, reason: str) -> None:
        try:
            log.warning("[SECURITY] %s | guild=%s (%s)", reason, guild.name, guild.id)
            audit_channel_id = await self.config.audit_channel_id()
            if audit_channel_id:
                ch = guild.get_channel(audit_channel_id)
                if ch:
                    embed = discord.Embed(title="🛡️ Security", description=reason, color=discord.Color.orange())
                    await ch.send(embed=embed)
        except Exception:
            pass

    async def _autopanic(self, guild: discord.Guild, reason: str) -> None:
        if not await self.config.autopanic_enabled():
            return

        async with self._autopanic_lock:
            if await self.config.panic_locked():
                return

            await self.config.panic_locked.set(True)
            await self.config.autopanic_reason.set(reason)

            # Disconnect from VC
            try:
                vc = guild.voice_client
                if vc and vc.is_connected():
                    await vc.disconnect(force=True)
            except Exception:
                pass

            await self._clear_state()
            await self._set_presence_station(None)
            await self._audit_security(guild, f"Auto-panic: {reason}")

    # -------------------------
    # Tripwires
    # -------------------------
    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context):
        """
        rrhome is the only approved path to Audio summon.
        If Audio summon is invoked directly (even by owner), auto-panic unless rrhome authorized it very recently.
        """
        try:
            if not ctx.guild or not ctx.command:
                return
            if not await self.config.bound():
                return

            allowed_guild_id = await self.config.allowed_guild_id()
            if allowed_guild_id and ctx.guild.id != allowed_guild_id:
                return

            # Detect Audio summon
            if ctx.command.name != "summon":
                return
            if not ctx.command.cog_name or ctx.command.cog_name.lower() != "audio":
                return

            if time.monotonic() <= self._allow_summon_until:
                return

            await self._autopanic(ctx.guild, "Unauthorized Audio summon (not via rrhome)")
        except Exception:
            pass

    # -------------------------
    # Watchdog loop
    # -------------------------
    async def _watchdog_loop(self):
        await self.bot.wait_until_ready()

        while self.bot.is_ready():
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL)

                if not await self.config.bound():
                    continue

                guild_id = await self.config.allowed_guild_id()
                vc_id = await self.config.allowed_voice_channel_id()
                if not guild_id or not vc_id:
                    continue

                guild = self.bot.get_guild(int(guild_id))
                if not guild:
                    continue

                if await self.config.panic_locked():
                    continue

                stream_url = await self.config.stream_url()
                home = await self._bot_is_home(guild)

                # If station is set, bot must remain home
                if stream_url and not home:
                    await self._autopanic(guild, "Watchdog: station set but bot is not home")
                    continue

                # If connected but wrong channel, panic
                vc = guild.voice_client
                if vc and vc.is_connected() and vc.channel and vc.channel.id != int(vc_id):
                    await self._autopanic(guild, f"Watchdog: bot in wrong VC ({vc.channel.id})")
                    continue

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Watchdog loop error")

    # -------------------------
    # DJ Asuka reassurance delivery
    # -------------------------
    async def _send_to_dj(self, guild: discord.Guild, member: discord.Member, embed: discord.Embed) -> None:
        use_dm = await self.config.reassure_use_dm()
        fallback_id = await self.config.reassure_fallback_channel_id()
        control_id = await self.config.control_text_channel_id()

        # DM first
        if use_dm:
            try:
                await member.send(embed=embed)
                return
            except Exception:
                pass

        # Fallback channel if DM fails/disabled
        ch = None
        if fallback_id:
            ch = guild.get_channel(int(fallback_id))
        if ch is None and control_id:
            ch = guild.get_channel(int(control_id))
        if ch is None:
            return

        try:
            await ch.send(content=member.mention, embed=embed)
        except Exception:
            pass

    async def _periodic_reassurance_loop(self):
        await self.bot.wait_until_ready()

        while self.bot.is_ready():
            try:
                await asyncio.sleep(REASSURE_TICK)

                if not await self.config.bound():
                    continue
                if await self.config.panic_locked():
                    continue
                if not await self.config.periodic_reassure_enabled():
                    continue

                guild_id = await self.config.allowed_guild_id()
                allowed_vc_id = await self.config.allowed_voice_channel_id()
                dj_user_id = await self.config.dj_user_id()
                stream_url = await self.config.stream_url()
                station_name = await self.config.station_name()

                if not guild_id or not allowed_vc_id or not dj_user_id:
                    continue

                # Only reassure when a station is actually set
                if not stream_url or not station_name:
                    continue

                guild = self.bot.get_guild(int(guild_id))
                if not guild:
                    continue

                # Bot must be home
                if not await self._bot_is_home(guild):
                    continue

                member = guild.get_member(int(dj_user_id))
                if not member:
                    continue

                # Is DJ in the allowed VC?
                dj_in_allowed_vc = bool(
                    member.voice
                    and member.voice.channel
                    and member.voice.channel.id == int(allowed_vc_id)
                )

                now = time.monotonic()

                if dj_in_allowed_vc:
                    interval = int(await self.config.reassure_interval_in_vc_sec() or 0)
                    if interval < 30:
                        interval = 30
                    if now - self._last_reassure_in_vc_ts < interval:
                        continue
                    self._last_reassure_in_vc_ts = now

                    embed = discord.Embed(
                        title="🖤 It’s okay.",
                        description=f"You're safe. I’m here. We’re staying home.\n**Station:** {station_name}",
                        color=discord.Color.dark_teal(),
                    )
                    embed.set_footer(text="Grey Hair Asuka protocol: reassurance (in VC).")
                    await self._send_to_dj(guild, member, embed)

                else:
                    interval = int(await self.config.reassure_interval_out_vc_sec() or 0)
                    if interval < 60:
                        interval = 60
                    if now - self._last_reassure_out_vc_ts < interval:
                        continue
                    self._last_reassure_out_vc_ts = now

                    embed = discord.Embed(
                        title="🖤 It’s okay.",
                        description=f"I'm still here. You're safe even if you're not in the room.\n**Station:** {station_name}",
                        color=discord.Color.dark_teal(),
                    )
                    embed.set_footer(text="Grey Hair Asuka protocol: reassurance (out of VC).")
                    await self._send_to_dj(guild, member, embed)

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Periodic reassurance loop error")

    # -------------------------
    # Radio-browser + Audio helpers
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

    async def _set_presence_station(self, station_name: Optional[str]) -> None:
        try:
            if station_name:
                await self.bot.change_presence(activity=discord.Game(name=f"📻 {station_name}"))
            else:
                await self.bot.change_presence(activity=None)
        except Exception:
            pass

    async def _audio_play(self, ctx: commands.Context, stream_url: str) -> bool:
        play_cmd = self.bot.get_command("play")
        if play_cmd is None:
            await ctx.send("❌ Audio cog / `play` not found.")
            return False
        await ctx.invoke(play_cmd, query=stream_url)
        return True

    async def _audio_stop(self, ctx: commands.Context) -> bool:
        stop_cmd = self.bot.get_command("stop")
        if stop_cmd is None:
            await ctx.send("❌ Audio cog / `stop` not found.")
            return False
        await ctx.invoke(stop_cmd)
        return True

    async def _clear_state(self) -> None:
        await self.config.stream_url.set(None)
        await self.config.station_name.set(None)

    def _blocked_by_tags(self, station: Station, blocked_tags: List[str]) -> bool:
        blob = f"{station.tags} {station.name}".lower()
        return any(t and t in blob for t in blocked_tags)

    def _page_embed(self, ctx: commands.Context, query: str, page: int, total_pages: int, page_items: List[Station]) -> discord.Embed:
        prefix = ctx.clean_prefix
        embed = discord.Embed(
            title=f"🔎 Results for '{query}' (Page {page + 1}/{total_pages})",
            color=discord.Color.green(),
        )
        start_index = page * PAGE_SIZE
        for idx, s in enumerate(page_items, start=start_index + 1):
            tags = s.tags
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
    # Owner commands
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

        await self.config.bound.set(True)
        await self.config.panic_locked.set(False)
        await self.config.autopanic_enabled.set(True)
        await self.config.autopanic_reason.set(None)

        await ctx.send("Bound. Use rrsetdj to set DJ Asuka. Use rrhome to bring her home.")

    @commands.is_owner()
    @commands.command()
    async def rrstatus(self, ctx: commands.Context):
        bound = await self.config.bound()
        panic = await self.config.panic_locked()
        reason = await self.config.autopanic_reason()
        g = await self.config.allowed_guild_id()
        t = await self.config.control_text_channel_id()
        v = await self.config.allowed_voice_channel_id()
        station = await self.config.station_name()
        stream = await self.config.stream_url()
        dj = await self.config.dj_user_id()
        pr = await self.config.periodic_reassure_enabled()
        inv = await self.config.reassure_interval_in_vc_sec()
        outv = await self.config.reassure_interval_out_vc_sec()
        dm = await self.config.reassure_use_dm()
        fb = await self.config.reassure_fallback_channel_id()

        home = await self._bot_is_home(ctx.guild) if ctx.guild else False

        desc = [
            f"**Bound:** {bound}",
            f"**Panic:** {panic}",
            f"**Reason:** {reason}" if reason else "",
            f"**Home:** {home}",
            "",
            f"**Allowed Guild:** `{g}`" if g else "",
            f"**Control Channel:** `{t}`" if t else "",
            f"**Allowed Voice:** `{v}`" if v else "",
            "",
            f"**Station:** {station}" if station else "**Station:** (none)",
            f"**Stream set:** {bool(stream)}",
            "",
            f"**DJ Asuka user:** `{dj}`" if dj else "**DJ Asuka user:** (not set)",
            f"**Periodic reassure:** {pr}",
            f"**Intervals:** in-VC={int(inv)//60}m, out-VC={int(outv)//60}m",
            f"**DM first:** {dm}",
            f"**Fallback channel:** `{fb}`" if fb else "**Fallback channel:** (control channel)",
        ]
        embed = discord.Embed(
            title="🛡️ Grey Hair Asuka Radio Status",
            description="\n".join([x for x in desc if x]),
            color=discord.Color.dark_teal(),
        )
        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command()
    async def rrpanic(self, ctx: commands.Context):
        # Manual panic requires presence in allowed VC
        if not await self._require_owner_in_allowed_vc(ctx):
            return

        await self.config.panic_locked.set(True)
        await self.config.autopanic_reason.set("Manual panic")

        try:
            await self._audio_stop(ctx)
        except Exception:
            pass

        try:
            if ctx.guild and ctx.guild.voice_client and ctx.guild.voice_client.is_connected():
                await ctx.guild.voice_client.disconnect(force=True)
        except Exception:
            pass

        await self._clear_state()
        await self._set_presence_station(None)
        await ctx.send("🛑 Panic engaged. Controls locked.")

    @commands.is_owner()
    @commands.command()
    async def rrunlock(self, ctx: commands.Context):
        # Unlock requires owner present in allowed VC
        if not await self._require_owner_in_allowed_vc(ctx):
            return

        await self.config.panic_locked.set(False)
        await self.config.autopanic_reason.set(None)
        await ctx.send("Controls unlocked.")

        # Optional: auto-run rrhome immediately (owner already present)
        await self.rrhome(ctx)

    @commands.is_owner()
    @commands.command()
    async def rrsetdj(self, ctx: commands.Context, member: discord.Member):
        await self.config.dj_user_id.set(member.id)
        await ctx.send(f"DJ Asuka target set to: {member.mention}")

    @commands.is_owner()
    @commands.command()
    async def rrreassuretoggle(self, ctx: commands.Context, enabled: bool):
        await self.config.periodic_reassure_enabled.set(bool(enabled))
        await ctx.send(f"Periodic reassurance: **{enabled}**")

    @commands.is_owner()
    @commands.command()
    async def rrreassureinterval(self, ctx: commands.Context, minutes_in_vc: int, minutes_out_vc: Optional[int] = None):
        if minutes_in_vc < 1:
            minutes_in_vc = 1
        if minutes_out_vc is None:
            minutes_out_vc = max(5, minutes_in_vc * 3)
        if minutes_out_vc < 1:
            minutes_out_vc = 1

        await self.config.reassure_interval_in_vc_sec.set(minutes_in_vc * 60)
        await self.config.reassure_interval_out_vc_sec.set(minutes_out_vc * 60)
        await ctx.send(f"Intervals set: in-VC={minutes_in_vc}m, out-VC={minutes_out_vc}m")

    @commands.is_owner()
    @commands.command()
    async def rrreassuredm(self, ctx: commands.Context, enabled: bool):
        await self.config.reassure_use_dm.set(bool(enabled))
        await ctx.send(f"Reassurance DMs enabled: **{enabled}**")

    @commands.is_owner()
    @commands.command()
    async def rrreassurechannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """
        Set where reassurance goes if DMs fail/disabled. Defaults to control channel if unset.
        """
        if channel is None:
            await self.config.reassure_fallback_channel_id.set(None)
            await ctx.send("Reassurance fallback channel cleared (will use control channel).")
        else:
            await self.config.reassure_fallback_channel_id.set(channel.id)
            await ctx.send(f"Reassurance fallback channel set to: {channel.mention}")

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

        summon_cmd = self.bot.get_command("summon")
        if summon_cmd is None:
            await ctx.send("Audio `summon` command not found.")
            return

        self._allow_summon_until = time.monotonic() + SUMMON_GRACE_SECONDS
        await ctx.invoke(summon_cmd)
        await ctx.send("Home command executed.")

    @commands.is_owner()
    @commands.command()
    async def searchstations(self, ctx: commands.Context, *, query: str):
        if not await self._require_owner_in_allowed_vc(ctx):
            return
        if not await self._require_bot_home(ctx):
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

        async with self._cache_lock:
            stations = list(self._stations_cache)

        if not stations:
            await ctx.send(f"No cached results. Run `{ctx.clean_prefix}searchstations <query>` first.")
            return

        if index < 1 or index > len(stations):
            await ctx.send(f"Invalid station number. Use 1-{len(stations)}.")
            return

        station = stations[index - 1]

        await self.config.stream_url.set(station.stream_url)
        await self.config.station_name.set(station.name)

        ok = await self._audio_play(ctx, station.stream_url)
        if not ok:
            return

        await self._set_presence_station(station.name)

        embed = discord.Embed(
            title="📻 Station Playing",
            description=f"**Station:** {station.name}\n**Country:** {station.country}\n🔗 [Stream Link]({station.stream_url})",
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command()
    async def stopstation(self, ctx: commands.Context):
        if not await self._require_owner_in_allowed_vc(ctx):
            return
        if not await self._require_bot_home(ctx):
            return

        ok = await self._audio_stop(ctx)
        if not ok:
            return

        await self._clear_state()
        await self._set_presence_station(None)

        await ctx.send(embed=discord.Embed(title="⏹️ Stopped", description="Radio stream stopped.", color=discord.Color.red()))
