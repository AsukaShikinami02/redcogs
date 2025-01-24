from redbot.core import commands
import aiohttp
import discord

class PluralKitIntegration(commands.Cog):
    """A cog to import PluralKit members and use their proxies."""

    def __init__(self, bot):
        self.bot = bot
        self.pluralkit_url = "https://api.pluralkit.me/v2"
        self.members = {}

    async def fetch_members(self, system_id):
        """Fetch PluralKit members using the system ID."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.pluralkit_url}/systems/{system_id}/members") as response:
                if response.status == 200:
                    members = await response.json()
                    self.members = {
                        m["id"]: {
                            "name": m["name"],
                            "avatar_url": m.get("avatar_url", None),
                            "proxy_tags": m.get("proxy_tags", [])
                        }
                        for m in members
                    }
                    return self.members
                elif response.status == 404:
                    raise Exception("System ID not found or member list is private.")
                else:
                    raise Exception(f"Failed to fetch members: {response.status}")

    @commands.command()
    async def import_members(self, ctx, system_id: str):
        """Command to import PluralKit members using the system ID."""
        try:
            members = await self.fetch_members(system_id)
            member_count = len(members)
            await ctx.send(f"Imported {member_count} members successfully from system `{system_id}`.")
        except Exception as e:
            await ctx.send(f"Error importing members: {e}")

    @commands.command()
    async def proxy_message(self, ctx, member_name: str, *, message: str):
        """Send a message using a member's proxy."""
        member = next((m for m in self.members.values() if m["name"].lower() == member_name.lower()), None)
        if not member:
            await ctx.send(f"Member '{member_name}' not found.")
            return

        # Use the member's avatar and name
        avatar_url = member.get("avatar_url") or None
        webhook = await ctx.channel.create_webhook(name=member["name"])
        await webhook.send(
            message,
            username=member["name"],
            avatar_url=avatar_url,
        )
        await webhook.delete()
    
   