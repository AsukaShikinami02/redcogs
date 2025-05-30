import discord
from redbot.core import commands, bank
import random, json, os

COST_TO_PLAY = 500
CASE_AMOUNTS = [
    0.01, 1, 5, 10, 25, 50, 75, 100,
    200, 300, 400, 500, 750, 1000,
    5000, 10000, 25000, 50000,
    75000, 100000, 200000, 300000,
    400000, 500000, 750000, 1000000
]

ROUND_STRUCTURE = [6, 5, 4, 3, 2] + [1] * 15  # Opening pattern per round

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

    def build_case_embed(self, user_id):
        game = self.games[str(user_id)]
        desc = ""
        for i in range(1, 27):
            if i == game["player_case"]:
                desc += f"💼 **[{i}]** (Your case)\n"
            elif i in game["opened_cases"]:
                val = game["case_values"][i - 1]
                desc += f"❌ Case {i}: ${val:,}\n"
            else:
                desc += f"🧳 Case {i}\n"
        embed = discord.Embed(title="📦 Deal or No Deal", description=desc, color=0x00ffcc)
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
        await ctx.send(f"${COST_TO_PLAY} deducted. 🎲 Please pick your case to keep with `!deal pick <case_number>` (1-26).")

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
            await ctx.send(f"🎉 You chose case #{case} to keep. Now open cases with `!deal open <case_number>`.")
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

        game["opened_cases"].append(case)
        val = game["case_values"][case - 1]
        await ctx.send(f"💼 Case #{case} had **${val:,}**")

        remaining_unopened = 26 - len(game["opened_cases"]) - 1  # Exclude player's case

        if remaining_unopened == 1:
            game["final_stage"] = True
            self.save()
            await ctx.send("🕵️ Only your case and one other remain. Do you want to `!deal swap` your case or keep it?")
            return

        opens_required = ROUND_STRUCTURE[game["round"] - 1]
        if len(game["opened_cases"]) >= sum(ROUND_STRUCTURE[:game["round"]]):
            offer = self.banker_offer(game)
            game["offers"].append(offer)
            self.save()
            await ctx.send(f"☎️ The Banker offers: **${offer:,}**. Type `!deal accept` to accept or `!deal nodeal` to continue.")
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

        try:
            await bank.deposit_credits(ctx.author, payout)
        except Exception as e:
            await ctx.send(f"Error depositing winnings: {e}")
            return

        game["deal_taken"] = True
        self.games.pop(user_id)
        self.save()
        await ctx.send(f"✅ You accepted the deal and won **${payout:,}**! Game over.")

    @deal.command()
    async def nodeal(self, ctx):
        user_id = str(ctx.author.id)
        if user_id not in self.games:
            await ctx.send("You don't have an active game.")
            return

        game = self.games[user_id]
        game["round"] += 1
        self.save()
        await ctx.send("📦 No Deal! Continue opening cases with `!deal open <case_number>`.")

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
        await ctx.send(f"🔄 You swapped your case #{game['player_case']} with case #{new_case}.")
        await ctx.send(f"🎉 Your new case contained **${swapped_value:,}**! (Your original case had ${original_value:,})")

    @deal.command()
    async def forfeit(self, ctx):
        user_id = str(ctx.author.id)
        if user_id in self.games:
            del self.games[user_id]
            self.save()
            await ctx.send("Your game was cancelled.")
        else:
            await ctx.send("You have no active game.")