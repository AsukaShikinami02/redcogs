import discord
from redbot.core import commands
import aiohttp
import asyncio
import re
import urllib.parse

class RedRadio(commands.Cog):
    """Radio streaming cog with track info from ICY metadata."""

    def __init__(self, bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.stations = []
        self.track_channel = None
        self.last_title = None
        self.current_stream_url = None
        self.track_task = self.bot.loop.create_task(self.trackinfo_loop())

    def cog_unload(self):
        if self.track_task:
            self.track_task.cancel()
        self.bot.loop.create_task(self.session.close())

    async def get_stream_metadata(self, stream_url):
        headers = {"Icy-MetaData": "1", "User-Agent": "Mozilla/5.0"}
        try:
            async with self.session.get(stream_url, headers=headers, timeout=10) as resp:
                metaint = int(resp.headers.get("icy-metaint", 0))
                if metaint == 0:
                    return None
                raw = await resp.content.read(metaint + 4080)
                metadata_offset = metaint
                metadata_length = raw[metadata_offset] * 16
                metadata_content = raw[metadata_offset + 1:metadata_offset + 1 + metadata_length].decode("utf-8", errors="ignore")
                match = re.search(r"StreamTitle='(.*?)';", metadata_content)
                if match:
                    title = match.group(1).strip()
                    if " - " in title:
                        artist, song = title.split(" - ", 1)
                    else:
                        artist, song = None, title
                    return {"title": song.strip(), "artist": artist.strip() if artist else None}
        except Exception as e:
            print(f"[Metadata] Failed to get ICY metadata: {e}")
        return None

    @commands.command()
    async def searchstations(self, ctx, *, query: str):
        url = f"https://de2.api.radio-browser.info/json/stations/byname/{query}"
        async with self.session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("Failed to fetch stations.")
                return
            self.stations = await resp.json()

        if not self.stations:
            await ctx.send("No stations found.")
            return

        embed = discord.Embed(title=f"🔎 Results for '{query}'", color=discord.Color.green())
        for i, s in enumerate(self.stations):
            embed.add_field(
                name=f"{i+1}. {s['name']} ({s['country']})",
                value=f"Bitrate: {s['bitrate']} kbps\nTags: {s['tags'][:100]}",
                inline=False
            )
        await ctx.send(embed=embed)

    @commands.command()
    async def playstation(self, ctx, index: int):
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
        self.current_stream_url = stream_url
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
        stop_cmd = self.bot.get_command("stop")
        if stop_cmd is None:
            await ctx.send("❌ Stop command not found.")
            return
        await ctx.invoke(stop_cmd)
        self.current_stream_url = None
        await ctx.send(embed=discord.Embed(
            title="⏹️ Stopped",
            description="The radio stream has been stopped.",
            color=discord.Color.red()
        ))

    async def trackinfo_loop(self):
        await self.bot.wait_until_ready()
        while self.bot.is_ready():
            await asyncio.sleep(30)
            if not self.track_channel or not self.current_stream_url:
                continue
            try:
                metadata = await self.get_stream_metadata(self.current_stream_url)
                if not metadata:
                    continue

                artist = metadata.get("artist")
                title = metadata.get("title")

                if not title or title == self.last_title:
                    continue

                self.last_title = title

                embed = discord.Embed(
                    title="🎶 Now Playing",
                    description=f"**{title}** by **{artist}**" if artist else title,
                    color=discord.Color.teal()
                )

                await self.track_channel.send(embed=embed)

            except Exception as e:
                print(f"[Radio TrackInfo Loop] Error: {e}")
