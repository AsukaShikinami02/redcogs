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
        """Search for radio stations by name"""
        url = f"https://de2.api.radio-browser.info/json/stations/byname/{query}"
        async with self.session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("Failed to fetch stations.")
                return
            self.stations = await resp.json()

        if not self.stations:
            await ctx.send("No stations found.")
            return

        embed = discord.Embed(
            title=f"🔎 Results for '{query}'",
            description="Use `!playstation <number>` to play a station.",
            color=discord.Color.green()
        )

        for i, s in enumerate(self.stations):
            embed.add_field(
                name=f"{i+1}. {s['name']} ({s['country']})",
                value=f"Bitrate: {s['bitrate']} kbps\nTags: {s['tags'][:100]}",
                inline=False
            )

        await ctx.send(embed=embed)

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

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You must be in a voice channel to play a station.")
            return

        play_cmd = self.bot.get_command("play")
        if play_cmd is None:
            await ctx.send("❌ Audio cog or `play` command not found.")
            return

        await ctx.invoke(play_cmd, query=stream_url)

        self.track_channel = ctx.channel
        self.last_title = None

        embed = discord.Embed(
            title=f"📻 Now Playing: {station['name']}",
            description=f"🎧 Country: {station['country']}\n🔗 [Stream Link]({stream_url})",
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Use !stopstation to stop playback.")
        await ctx.send(embed=embed)

    @commands.command()
    async def stopstation(self, ctx):
        """Stop the radio stream."""
        stop_cmd = self.bot.get_command("stop")
        if stop_cmd is None:
            await ctx.send("❌ Stop command not found.")
            return
        await ctx.invoke(stop_cmd)
        await ctx.send(embed=discord.Embed(
            title="⏹️ Stopped",
            description="The radio stream has been stopped.",
            color=discord.Color.red()
        ))

    @commands.command()
    async def trackinfo(self, ctx):
        """Show now-playing track info as an embed (by parsing np)."""
        np_cmd = self.bot.get_command("np")
        if not np_cmd:
            await ctx.send("❌ Now Playing command not found.")
            return

        temp_msg = await ctx.send("⏳ Getting track info...")
        fake_ctx = await self.bot.get_context(temp_msg)
        await np_cmd.invoke(fake_ctx)

        history = [msg async for msg in ctx.channel.history(limit=5)]
        for msg in history:
            if msg.author == self.bot.user and msg.id != temp_msg.id:
                content = msg.content
                await msg.delete()
                await temp_msg.delete()
                break
        else:
            await temp_msg.edit(content="❌ Couldn't get track info.")
            return

        embed = discord.Embed(
            title="🎶 Now Playing",
            description=content,
            color=discord.Color.purple()
        )
        await ctx.send(embed=embed)

    async def trackinfo_loop(self):
        """Auto-post current track every 30 seconds (if changed)."""
        await self.bot.wait_until_ready()
        while self.bot.is_ready():
            await asyncio.sleep(30)
            if not self.track_channel:
                continue
            try:
                np_cmd = self.bot.get_command("np")
                if not np_cmd:
                    continue

                msg = await self.track_channel.send("⏳ Checking track info...")
                fake_ctx = await self.bot.get_context(msg)
                await np_cmd.invoke(fake_ctx)

                history = [m async for m in self.track_channel.history(limit=5)]
                for m in history:
                    if m.author == self.bot.user and m.id != msg.id:
                        content = m.content
                        await m.delete()
                        break
                else:
                    await msg.delete()
                    continue

                await msg.delete()

                if content and content != self.last_title:
                    embed = discord.Embed(
                        title="🎶 Now Playing",
                        description=content,
                        color=discord.Color.dark_teal()
                    )
                    await self.track_channel.send(embed=embed)
                    self.last_title = content

            except Exception as e:
                print(f"[RadioCog] Track info loop error: {e}")
