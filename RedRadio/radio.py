import discord
from redbot.core import commands
import aiohttp
import asyncio
import re
import urllib.parse

LASTFM_API_KEY = "067585eb2f42693ad00de3bbd2fda02a"

class Radio(commands.Cog):
    """Radio streaming cog with track info and album art."""

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

    async def get_metadata_from_lastfm(self, artist, title):
        if not artist or not title:
            return None
        params = {
            "method": "track.getInfo",
            "api_key": LASTFM_API_KEY,
            "artist": artist,
            "track": title,
            "format": "json"
        }
        url = "http://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(params)
        try:
            async with self.session.get(url) as resp:
                data = await resp.json()
                track = data.get("track")
                if not track:
                    return None
                album = track.get("album", {})
                image = next((img["#text"] for img in album.get("image", [])[::-1] if img["#text"]), None)
                return {
                    "title": track.get("name"),
                    "artist": track.get("artist", {}).get("name"),
                    "album": album.get("title"),
                    "image": image
                }
        except Exception as e:
            print(f"[LastFM] Error: {e}")
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
        await ctx.send(embed=discord.Embed(
            title="⏹️ Stopped",
            description="The radio stream has been stopped.",
            color=discord.Color.red()
        ))

    async def trackinfo_loop(self):
        await self.bot.wait_until_ready()
        while self.bot.is_ready():
            await asyncio.sleep(30)
            if not self.track_channel:
                continue
            try:
                np_cmd = self.bot.get_command("np")
                if not np_cmd:
                    continue

                msg = await self.track_channel.send("⏳ Fetching metadata...")
                ctx_fake = await self.bot.get_context(msg)
                await np_cmd.invoke(ctx_fake)

                history = [m async for m in self.track_channel.history(limit=5)]
                for m in history:
                    if m.author == self.bot.user and m.id != msg.id:
                        raw_text = m.content
                        await m.delete()
                        break
                else:
                    await msg.delete()
                    continue

                stream_url_match = re.search(r"https?://[^\s]+", raw_text)
                if not stream_url_match:
                    await msg.delete()
                    continue

                stream_url = stream_url_match.group(0)
                metadata = await self.get_stream_metadata(stream_url)

                if not metadata:
                    await msg.delete()
                    continue

                artist = metadata.get("artist")
                title = metadata.get("title")

                if not title or title == self.last_title:
                    await msg.delete()
                    continue

                self.last_title = title
                meta = await self.get_metadata_from_lastfm(artist, title)

                embed = discord.Embed(
                    title="🎶 Now Playing",
                    description=f"**{title}** by **{artist}**" if artist else title,
                    color=discord.Color.teal()
                )
                if meta and meta.get("image"):
                    embed.set_thumbnail(url=meta["image"])
                if meta and meta.get("album"):
                    embed.add_field(name="Album", value=meta["album"], inline=True)

                await self.track_channel.send(embed=embed)
                await msg.delete()

            except Exception as e:
                print(f"[Radio TrackInfo Loop] Error: {e}")
