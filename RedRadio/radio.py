import discord
from redbot.core import commands
import aiohttp
import asyncio

class RedRadio(commands.Cog):
    """Radio streaming cog using the Audio cog and RadioBrowser API"""

    def __init__(self, bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.stations = []
        self.track_channel = None
        self.last_title = None
        self.track_task = self.bot.loop.create_task(self.trackinfo_loop())

    def cog_unload(self):
        if self.track_task:
            self.track_task.cancel()
        self.bot.loop.create_task(self.session.close())

    @commands.command()
    async def searchstations(self, ctx, *, query: str):
        """Search for radio stations by name or tag."""
        api = f"https://de2.api.radio-browser.info/json/stations/byname/{query}"
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
        """Play a station from the last search."""
        if not self.stations:
            await ctx.send("Please use `!searchstations` first.")
            return
        if index < 1 or index > len(self.stations):
            await ctx.send("Invalid station number.")
            return

        station = self.stations[index - 1]
        stream_url = station["url"]

        audio_cog = self.bot.get_cog("Audio")
        if not audio_cog:
            await ctx.send("❌ Audio cog is not loaded.")
            return

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You must be in a voice channel to play a station.")
            return

        # Use Audio cog's play command
        play_cmd = audio_cog.get_command("play")
        await ctx.invoke(play_cmd, query=stream_url)

        self.track_channel = ctx.channel
        self.last_title = None
        await ctx.send(f"📻 Now playing: **{station['name']}**")

    @commands.command()
    async def stopstation(self, ctx):
        """Stop the radio stream."""
        audio_cog = self.bot.get_cog("Audio")
        if not audio_cog:
            await ctx.send("❌ Audio cog is not loaded.")
            return

        stop_cmd = audio_cog.get_command("stop")
        await ctx.invoke(stop_cmd)
        await ctx.send("⏹️ Stream stopped.")

    @commands.command()
    async def trackinfo(self, ctx):
        """Show the current playing track (if available)."""
        audio_cog = self.bot.get_cog("Audio")
        if not audio_cog:
            await ctx.send("❌ Audio cog is not loaded.")
            return

        np_cmd = audio_cog.get_command("np")  # Now playing
        await ctx.invoke(np_cmd)

    async def trackinfo_loop(self):
        """Periodically fetch and announce track info if available."""
        await self.bot.wait_until_ready()
        while self.bot.is_ready():
            await asyncio.sleep(30)
            if not self.track_channel:
                continue

            try:
                audio_cog = self.bot.get_cog("Audio")
                if not audio_cog:
                    continue

                # Use Audio cog's now playing command via fake context
                np_cmd = audio_cog.get_command("np")
                fake_ctx = await self.bot.get_context(await self.track_channel.send("🔄 Checking track info..."))
                await np_cmd.callback(audio_cog, fake_ctx)
                await asyncio.sleep(1)  # Let the bot clean up messages if needed

            except Exception as e:
                print(f"[RadioCog] Trackinfo loop error: {e}")
