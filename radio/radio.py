import discord
from redbot.core import commands
import aiohttp
import asyncio

class Radio(commands.Cog):
    """Radio streaming cog using Audio cog and RadioBrowser API"""

    def __init__(self, bot):
        self.bot = bot
        self.stations = []
        self.session = aiohttp.ClientSession()
        self.track_channel = None
        self.last_title = None
        self.track_task = self.bot.loop.create_task(self.trackinfo_loop())

    def cog_unload(self):
        if self.track_task:
            self.track_task.cancel()
        self.bot.loop.create_task(self.session.close())

    async def get_audio_cog(self):
        audio_cog = self.bot.get_cog("Audio")
        if not audio_cog:
            raise RuntimeError("❌ Audio cog is not loaded.")
        return audio_cog

    async def get_player(self, ctx):
        audio_cog = await self.get_audio_cog()
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You must be in a voice channel.")
            return None

        vc = ctx.author.voice.channel
        player = audio_cog.get_player(ctx.guild.id)
        if not player:
            player = await audio_cog.connect(vc)
        return player

    @commands.command()
    async def searchstations(self, ctx, *, query: str):
        """Search radio stations by name or tag."""
        api = f"https://de1.api.radio-browser.info/json/stations/byname{query}"
        async with self.session.get(api) as resp:
            if resp.status != 200:
                await ctx.send("Failed to fetch stations.")
                return
            self.stations = await resp.json()

        if not self.stations:
            await ctx.send("No stations found.")
            return

        msg = "\n".join(f"{i+1}. {s['name']} - {s['country']}" for i, s in enumerate(self.stations))
        await ctx.send(f"🔎 Found stations for `{query}`:\n```{msg}```\nUse `!playstation <number>` to play one.")

    @commands.command()
    async def playstation(self, ctx, index: int):
        """Play a station from last search."""
        if not self.stations:
            await ctx.send("Use `!searchstations` first.")
            return
        if index < 1 or index > len(self.stations):
            await ctx.send("Invalid station number.")
            return

        station = self.stations[index - 1]
        stream_url = station["url"]

        player = await self.get_player(ctx)
        if not player:
            return

        await player.play(ctx.author, stream_url)
        self.track_channel = ctx.channel
        self.last_title = None
        await ctx.send(f"📻 Now playing: **{station['name']}**")

    @commands.command()
    async def stopstation(self, ctx):
        """Stop the radio stream."""
        audio_cog = await self.get_audio_cog()
        player = audio_cog.get_player(ctx.guild.id)
        if player and player.is_playing:
            await player.stop()
            await ctx.send("⏹️ Stream stopped.")
        else:
            await ctx.send("Nothing is playing.")

    @commands.command()
    async def trackinfo(self, ctx):
        """Show now-playing track metadata."""
        audio_cog = await self.get_audio_cog()
        player = audio_cog.get_player(ctx.guild.id)
        if not player or not player.is_playing:
            await ctx.send("Nothing is playing.")
            return

        current = await player.current()
        title = getattr(current, "title", None)
        if title:
            await ctx.send(f"🎶 Now playing: `{title}`")
        else:
            await ctx.send("No track info available for this stream.")

    async def trackinfo_loop(self):
        await self.bot.wait_until_ready()
        while self.bot.is_ready():
            await asyncio.sleep(30)
            if not self.track_channel:
                continue
            try:
                audio_cog = self.bot.get_cog("Audio")
                if not audio_cog:
                    continue
                player = audio_cog.get_player(self.track_channel.guild.id)
                if player and player.is_playing:
                    current = await player.current()
                    title = getattr(current, "title", None)
                    if title and title != self.last_title:
                        await self.track_channel.send(f"🎶 Now playing: `{title}`")
                        self.last_title = title
            except Exception as e:
                print(f"[RadioCog] Trackinfo error: {e}")
