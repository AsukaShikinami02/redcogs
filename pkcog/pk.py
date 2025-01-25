import json
from redbot.core import commands
import aiohttp
import discord

class PluralKitIntegration(commands.Cog):
    """A cog to import PluralKit members and use their proxies."""

    def __init__(self, bot):
        self.bot = bot
        self.pluralkit_url = "https://api.pluralkit.me/v2"
        self.members = {}
        self.load_members()  # Load members from file on startup

    def load_members(self):
        """Load members data from a JSON file."""
        try:
            with open("members.json", "r") as file:
                self.members = json.load(file)
            print("Members data loaded successfully.")
        except FileNotFoundError:
            print("No saved members data found, starting with an empty dictionary.")
            self.members = {}  # Start with an empty dictionary if the file doesn't exist
        except json.JSONDecodeError:
            print("Error decoding members data. The file may be corrupted.")
            self.members = {}
        except Exception as e:
            print(f"Error loading members data: {e}")

    def save_members(self):
        """Save members data to a JSON file."""
        try:
            with open("members.json", "w") as file:
                json.dump(self.members, file, indent=4)
            print("Members data saved successfully.")
        except Exception as e:
            print(f"Error saving members data: {e}")

    async def fetch_members(self, system_id: str) -> dict:
        """
        Fetch members from PluralKit for a given system ID.
        Returns a dictionary where keys are member IDs and values are member data.
        """
        api_url = f"https://api.pluralkit.me/v2/systems/{system_id}/members"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url) as response:
                    if response.status != 200:
                        raise Exception(f"Failed to fetch members: HTTP {response.status}")

                    # Parse the JSON response
                    data = await response.json()

                    # Convert list of members to a dictionary keyed by member ID
                    members = {
                        member["id"]: {
                            "name": member["name"],
                            "avatar_url": member.get("avatar_url", ""),  # Handle missing avatars
                            "proxy_tags": member.get("proxy_tags", []),  # Include proxy tags for later use
                        }
                        for member in data
                    }
                    return members
        except Exception as e:
            raise Exception(f"Error fetching members: {e}")

    @commands.command()
    async def pkimport(self, ctx, system_id: str):
        """Import or update PluralKit members, including avatar updates."""
        try:
            # Fetch the latest members from PluralKit
            new_members = await self.fetch_members(system_id)

            # Initialize tracking variables
            added = []
            removed = []
            updated = []

            # Compare new members with existing ones
            existing_member_ids = set(self.members.keys())
            new_member_ids = set(new_members.keys())

            # Detect additions
            for member_id in new_member_ids - existing_member_ids:
                self.members[member_id] = new_members[member_id]
                added.append(new_members[member_id]["name"])

            # Detect removals
            for member_id in existing_member_ids - new_member_ids:
                removed.append(self.members[member_id]["name"])
                del self.members[member_id]

            # Detect updates (e.g., avatar changes or other field updates)
            for member_id in new_member_ids & existing_member_ids:
                old_data = self.members[member_id]
                new_data = new_members[member_id]

                # Check if any data has changed
                if old_data["avatar_url"] != new_data["avatar_url"]:
                    self.members[member_id]["avatar_url"] = new_data["avatar_url"]
                    updated.append(new_data["name"])

            # Save updated members data to file
            self.save_members()

            # Prepare response messages
            response = []
            if added:
                response.append(f"Added members: {', '.join(added)}.")
            if removed:
                response.append(f"Removed members: {', '.join(removed)}.")
            if updated:
                response.append(f"Updated avatars for: {', '.join(updated)}.")
            if not response:
                response.append("No changes detected.")

            await ctx.send("\n".join(response))
        except Exception as e:
            await ctx.send(f"Error importing members: {e}")

    @commands.command()
    async def proxy(self, ctx, member_name: str, *, message: str):
        """Send a message using a member's proxy and delete the command message."""
        member = next((m for m in self.members.values() if m["name"].lower() == member_name.lower()), None)
        if not member:
            await ctx.send(f"Member '{member_name}' not found.")
            return

        # Use the member's avatar and name
        avatar_url = member.get("avatar_url") or None
        webhook = await ctx.channel.create_webhook(name=member["name"])

        try:
            # Send the webhook message
            await webhook.send(
                message,
                username=member["name"],
                avatar_url=avatar_url,
            )
            # Delete the command message
            await ctx.message.delete()
        except discord.Forbidden:
            await ctx.send("I don't have permission to delete messages in this channel.")
        except discord.HTTPException as e:
            await ctx.send(f"Failed to send the message: {e}")
        finally:
            # Clean up the webhook
            await webhook.delete()
