import discord
from redbot.core import commands, Config
import aiohttp
import asyncio
import re
import urllib.parse

class RedRadio(commands.Cog):
    """Radio streaming cog with track info from ICY metadata."""

    def __init__(self, bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.config = Config.get_conf(self, identifier=90210, force_registration=True)
        self.config.register_guild(stream_url=None, track_channel=None)
        self.stations = []
        self.last_title = {}
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

                if len(raw) < metadata_offset + 1:
                    return None  # not enough data to read metadata length

                metadata_length = raw[metadata_offset] * 16

                if len(raw) < metadata_offset + 1 + metadata_length:
                    return None  # not enough metadata bytes

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
            await ctx.send("Please use `AS!searchstations` first.")
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

        await self.config.guild(ctx.guild).track_channel.set(ctx.channel.id)
        await self.config.guild(ctx.guild).stream_url.set(stream_url)
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
        await self.config.guild(ctx.guild).stream_url.set(None)
        await ctx.send(embed=discord.Embed(
            title="⏹️ Stopped",
            description="The radio stream has been stopped.",
            color=discord.Color.red()
        ))

    async def trackinfo_loop(self):
        await self.bot.wait_until_ready()
        while self.bot.is_ready():
            await asyncio.sleep(10)
            for guild in self.bot.guilds:
                try:
                    stream_url = await self.config.guild(guild).stream_url()
                    channel_id = await self.config.guild(guild).track_channel()
                    if not stream_url or not channel_id:
                        continue

                    channel = guild.get_channel(channel_id)
                    if not channel:
                        continue

                    metadata = await self.get_stream_metadata(stream_url)
                    if not metadata:
                        continue

                    artist = metadata.get("artist")
                    title = metadata.get("title")

                    if not title or self.last_title.get(guild.id) == title:
                        continue
                    
                    self.last_title[guild.id] = title
                    

                    embed = discord.Embed(
                        title="🎶 Now Playing",
                        description=f"**{title}** by **{artist}**" if artist else title,
                        color=discord.Color.teal()
                    )

                    await channel.send(embed=embed)

                except Exception as e:
                    print(f"[Radio TrackInfo Loop] Error in guild {guild.id}: {e}")

