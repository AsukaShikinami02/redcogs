import discord
from discord.ui import Button, View
from redbot.core import commands, bank
import random
import json
import os
import asyncio

COST_TO_PLAY = 500
CASE_AMOUNTS = [
    0.01, 1, 5, 10, 25, 50, 75, 100,
    200, 300, 400, 500, 750, 1000,
    5000, 10000, 25000, 50000,
    75000, 100000, 200000, 300000,
    400000, 500000, 750000, 1000000
]

ROUND_STRUCTURE = [6, 5, 4, 3, 2] + [1] * 15  # Opening pattern per round

class DealButtons(View):
    def __init__(self, cog, user_id):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id
    
    @discord.ui.button(label="Accept Deal", style=discord.ButtonStyle.green, emoji="✅")
    async def accept(self, button: Button, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        
        game = self.cog.games[self.user_id]
        offer = game["offers"][-1]
        payout = int(offer)
        
        try:
            await bank.deposit_credits(interaction.user, payout)
        except Exception as e:
            await interaction.response.send_message(f"Error depositing winnings: {e}")
            return

        game["deal_taken"] = True
        self.cog.games.pop(self.user_id)
        self.cog.save()
        
        embed = discord.Embed(
            title="💼 Deal Accepted!",
            description=f"**You've accepted the banker's offer of ${offer:,}!**",
            color=0x00ff00
        )
        embed.add_field(name="💰 Winnings", value=f"${payout:,} has been deposited to your account!")
        await interaction.response.edit_message(embed=embed, view=None)
    
    @discord.ui.button(label="No Deal", style=discord.ButtonStyle.red, emoji="❌")
    async def no_deal(self, button: Button, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        
        game = self.cog.games[self.user_id]
        game["round"] += 1
        self.cog.save()
        
        embed = discord.Embed(
            title="📦 No Deal!",
            description="The game continues!",
            color=0xff9900
        )
        embed.set_footer(text=f"Round {game['round']}")
        await interaction.response.edit_message(embed=embed, view=None)
        await interaction.followup.send(embed=self.cog.build_case_embed(self.user_id))

class DealOrNoDeal(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.games = {}
        self.data_file = "data/dealornodeal/games.json"
        os.makedirs("data/dealornodeal", exist_ok=True)
        if os.path.exists(self.data_file):
            with open(self.data_file, "r") as f:
                self.games = json.load(f)

    def save(self):
        with open(self.data_file, "w") as f:
            json.dump(self.games, f, indent=2)

    def create_new_game(self):
        values = random.sample(CASE_AMOUNTS, 26)
        return {
            "case_values": values,
            "player_case": None,
            "opened_cases": [],
            "round": 1,
            "offers": [],
            "deal_taken": False,
            "final_swap": False,
            "final_stage": False
        }

    def get_remaining_values(self, game):
        return [v for i, v in enumerate(game["case_values"])
                if (i + 1) not in game["opened_cases"] and (i + 1) != game["player_case"]]

    def banker_offer(self, game):
        values = self.get_remaining_values(game)
        average = sum(values) / len(values)
        multiplier = 0.5 + (game["round"] * 0.05)
        random_factor = random.uniform(0.9, 1.1)
        return round(average * multiplier * random_factor, 2)

    def build_progress_bar(self, game):
        total_cases = sum(ROUND_STRUCTURE[:game["round"]])
        opened_this_round = len(game["opened_cases"]) - sum(ROUND_STRUCTURE[:game["round"]-1])
        
        progress = min(opened_this_round / ROUND_STRUCTURE[game["round"]-1], 1)
        filled = int(20 * progress)
        bar = "🟩" * filled + "⬜" * (20 - filled)
        return f"Round {game['round']} Progress: {bar} {int(progress*100)}%"

    def build_case_embed(self, user_id):
        game = self.games[str(user_id)]
        embed = discord.Embed(title="📦 Deal or No Deal", color=0x00ffcc)
        
        # Create a grid of cases (4 rows of 6-7 cases each)
        grid = []
        for i in range(1, 27):
            if i == game["player_case"]:
                grid.append(f"🔒 **{i}**")
            elif i in game["opened_cases"]:
                val = game["case_values"][i - 1]
                grid.append(f"❌ ~~{i}~~")
            else:
                grid.append(f"📦 {i}")
        
        # Split into rows
        for row in [grid[i:i+7] for i in range(0, 26, 7)]:
            embed.add_field(name="\u200b", value=" ".join(row), inline=False)
        
        # Show remaining values
        remaining = self.get_remaining_values(game)
        if remaining:
            embed.add_field(name="Remaining Values", value=", ".join(f"${x:,}" for x in sorted(remaining)), inline=False)
        
        # Show progress bar
        embed.add_field(name="Progress", value=self.build_progress_bar(game), inline=False)
        
        # Show current round and offers
        if game["offers"]:
            embed.set_footer(text=f"Round {game['round']} • Last offer: ${game['offers'][-1]:,}")
        else:
            embed.set_footer(text=f"Round {game['round']}")
        return embed

    @commands.group(invoke_without_command=True)
    async def deal(self, ctx):
        """Play Deal or No Deal"""
        await ctx.send("Use a subcommand: start, pick, open, accept, nodeal, forfeit, or swap.")

    @deal.command()
    async def start(self, ctx):
        user_id = str(ctx.author.id)
        bal = await bank.get_balance(ctx.author)

        if bal < COST_TO_PLAY:
            await ctx.send(f"You need at least ${COST_TO_PLAY} to play!")
            return

        if user_id in self.games:
            await ctx.send("You already have an active game.")
            return

        try:
            await bank.withdraw_credits(ctx.author, COST_TO_PLAY)
        except Exception as e:
            await ctx.send(f"Failed to withdraw {COST_TO_PLAY} credits: {e}")
            return

        self.games[user_id] = self.create_new_game()
        self.save()
        
        embed = discord.Embed(
            title="🎲 Deal or No Deal Started!",
            description=f"${COST_TO_PLAY} has been deducted from your account.",
            color=0x00ffcc
        )
        embed.add_field(
            name="Next Step",
            value="Please pick your case to keep with `!deal pick <case_number>` (1-26).",
            inline=False
        )
        embed.set_thumbnail(url="https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/twitter/259/game-die_1f3b2.png")
        await ctx.send(embed=embed)

    @deal.command()
    async def pick(self, ctx, case: int):
        user_id = str(ctx.author.id)
        if user_id not in self.games:
            await ctx.send("You don't have an active game. Start one with `!deal start`.")
            return

        game = self.games[user_id]
        if game["player_case"] is None:
            if case < 1 or case > 26:
                await ctx.send("Pick a valid case number between 1 and 26.")
                return
            game["player_case"] = case
            self.save()
            
            embed = discord.Embed(
                title="📦 Case Selected!",
                description=f"You chose case #{case} to keep.",
                color=0x00ffcc
            )
            embed.add_field(
                name="Next Step",
                value="Now open cases with `!deal open <case_number>`.",
                inline=False
            )
            embed.set_thumbnail(url="https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/twitter/259/briefcase_1f4bc.png")
            await ctx.send(embed=embed)
        else:
            await ctx.send("You've already picked your case.")

    @deal.command()
    async def open(self, ctx, case: int):
        user_id = str(ctx.author.id)
        if user_id not in self.games:
            await ctx.send("You don't have an active game.")
            return

        game = self.games[user_id]
        if game["deal_taken"]:
            await ctx.send("The game has already ended.")
            return

        if game["player_case"] is None:
            await ctx.send("Pick your case first using `!deal pick <number>`.")
            return

        if case == game["player_case"] or case in game["opened_cases"]:
            await ctx.send("You can't open this case.")
            return

        if case < 1 or case > 26:
            await ctx.send("Pick a valid case number between 1 and 26.")
            return

        # Send loading message
        msg = await ctx.send("🔍 Opening case...")
        
        # Add dramatic pause
        await asyncio.sleep(1.5)
        
        val = game["case_values"][case - 1]
        game["opened_cases"].append(case)
        
        # Create reveal embed
        embed = discord.Embed(
            title=f"Case #{case} Revealed!",
            description=f"💼 The case contained...",
            color=0xff9900
        )
        embed.add_field(name="Value", value=f"**${val:,}**", inline=False)
        
        # Different reactions based on value
        if val >= 100000:
            embed.set_image(url="https://media.giphy.com/media/xT5LMHxhOfscxPfIfm/giphy.gif")
        elif val <= 1:
            embed.set_image(url="https://media.giphy.com/media/l0HU7JI8AfIdK5hxS/giphy.gif")
        
        await msg.edit(content=None, embed=embed)
        
        remaining_unopened = 26 - len(game["opened_cases"]) - 1  # Exclude player's case

        if remaining_unopened == 1:
            game["final_stage"] = True
            self.save()
            
            remaining_case = next(i for i in range(1, 27) if i != game["player_case"] and i not in game["opened_cases"])
            
            embed = discord.Embed(
                title="🔄 Final Decision!",
                description="Only your case and one other remain!",
                color=0x00ffff
            )
            embed.add_field(
                name="Your Case",
                value=f"Case #{game['player_case']} (Unknown value)",
                inline=True
            )
            embed.add_field(
                name="Other Case",
                value=f"Case #{remaining_case} (Unknown value)",
                inline=True
            )
            embed.set_footer(text="Type `!deal swap` to switch or continue with your case")
            await ctx.send(embed=embed)
            return

        opens_required = ROUND_STRUCTURE[game["round"] - 1]
        if len(game["opened_cases"]) >= sum(ROUND_STRUCTURE[:game["round"]]):
            offer = self.banker_offer(game)
            game["offers"].append(offer)
            self.save()
            
            embed = discord.Embed(
                title="☎️ Banker's Offer",
                description=f"The Banker is offering you **${offer:,}**",
                color=0xffff00
            )
            embed.add_field(name="Your options", value="✅ Accept this deal\n❌ Continue playing")
            embed.set_footer(text=f"Round {game['round']}")
            
            view = DealButtons(self, user_id)
            await ctx.send(embed=embed, view=view)
        else:
            self.save()
            await ctx.send(embed=self.build_case_embed(user_id))

    @deal.command(name="accept")
    async def deal_accept(self, ctx):
        user_id = str(ctx.author.id)
        if user_id not in self.games:
            await ctx.send("You don't have an active game.")
            return

        game = self.games[user_id]
        if not game["offers"]:
            await ctx.send("There is no offer yet.")
            return

        offer = game["offers"][-1]
        payout = int(offer)

        embed = discord.Embed(
            title="💼 Deal Accepted!",
            description=f"**You've accepted the banker's offer of ${offer:,}!**",
            color=0x00ff00
        )
        embed.add_field(name="💰 Winnings", value=f"${payout:,} has been deposited to your account!")
        embed.set_thumbnail(url="https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/twitter/259/money-bag_1f4b0.png")

        try:
            await bank.deposit_credits(ctx.author, payout)
        except Exception as e:
            await ctx.send(f"Error depositing winnings: {e}")
            return

        game["deal_taken"] = True
        self.games.pop(user_id)
        self.save()
        await ctx.send(embed=embed)

    @deal.command()
    async def nodeal(self, ctx):
        user_id = str(ctx.author.id)
        if user_id not in self.games:
            await ctx.send("You don't have an active game.")
            return

        game = self.games[user_id]
        game["round"] += 1
        self.save()
        
        embed = discord.Embed(
            title="📦 No Deal!",
            description="The game continues!",
            color=0xff9900
        )
        embed.set_footer(text=f"Round {game['round']}")
        await ctx.send(embed=embed)
        await ctx.send(embed=self.build_case_embed(user_id))

    @deal.command()
    async def swap(self, ctx):
        user_id = str(ctx.author.id)
        if user_id not in self.games:
            await ctx.send("You don't have an active game.")
            return

        game = self.games[user_id]
        if not game.get("final_stage"):
            await ctx.send("You can only swap at the end of the game when 2 cases remain.")
            return

        remaining = [i for i in range(1, 27) if i != game["player_case"] and i not in game["opened_cases"]]
        if not remaining:
            await ctx.send("No case left to swap with.")
            return

        new_case = remaining[0]
        original_value = game["case_values"][game["player_case"] - 1]
        swapped_value = game["case_values"][new_case - 1]

        payout = int(swapped_value)
        try:
            await bank.deposit_credits(ctx.author, payout)
        except Exception as e:
            await ctx.send(f"Error depositing winnings: {e}")
            return

        self.games.pop(user_id)
        self.save()
        
        embed = discord.Embed(
            title="🔄 Case Swapped!",
            description=f"You swapped your case #{game['player_case']} with case #{new_case}",
            color=0x00ffcc
        )
        embed.add_field(
            name="Your new case contained",
            value=f"**${swapped_value:,}**",
            inline=False
        )
        embed.add_field(
            name="Your original case had",
            value=f"${original_value:,}",
            inline=False
        )
        
        if swapped_value > original_value:
            embed.set_thumbnail(url="https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/twitter/259/grinning-face-with-star-eyes_1f929.png")
        else:
            embed.set_thumbnail(url="https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/twitter/259/disappointed-face_1f61e.png")
        
        await ctx.send(embed=embed)

    @deal.command()
    async def forfeit(self, ctx):
        user_id = str(ctx.author.id)
        if user_id in self.games:
            del self.games[user_id]
            self.save()
            
            embed = discord.Embed(
                title="🚫 Game Cancelled",
                description="Your Deal or No Deal game has been forfeited.",
                color=0xff0000
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send("You have no active game.")