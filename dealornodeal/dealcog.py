import discord
from redbot.core import commands, Config, bank
import random, json, os
from discord.ui import View, Button

COST_TO_PLAY = 500
CASE_AMOUNTS = [
    0.01, 1, 5, 10, 25, 50, 75, 100,
    200, 300, 400, 500, 750, 1000,
    5000, 10000, 25000, 50000,
    75000, 100000, 200000, 300000,
    400000, 500000, 750000, 1000000
]

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
            "final_phase": False,
            "deal_taken": False,
            "final_case": None
        }

    def get_remaining_values(self, game):
        return [v for i, v in enumerate(game["case_values"])
                if i + 1 not in game["opened_cases"] and (i + 1) != game["player_case"]]

    def banker_offer(self, game):
        values = self.get_remaining_values(game)
        average = sum(values) / len(values)
        multiplier = 0.6 + (game["round"] * 0.05)
        return round(average * multiplier, 2)

    def build_case_embed(self, user_id):
        game = self.games[str(user_id)]
        desc = ""
        for i in range(1, 27):
            if i == game["player_case"]:
                desc += f"\U0001f4bc **[{i}]** (Your case)\n"
            elif i in game["opened_cases"]:
                val = game["case_values"][i - 1]
                desc += f"\u274c Case {i}: ${val:,}\n"
            else:
                desc += f"\U0001f512 Case {i}\n"
        embed = discord.Embed(title="📦 Deal or No Deal", description=desc, color=0x00ffcc)
        embed.set_footer(text=f"Round {game['round']}")
        return embed

    @commands.group()
    async def deal(self, ctx):
        """Play Deal or No Deal"""
        pass

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

        await bank.withdraw_credits(ctx.author, COST_TO_PLAY)
        self.games[user_id] = self.create_new_game()
        self.save()
        await ctx.send(f"$500 deducted. 🎲 Pick your case to keep:",
                       view=self.make_case_view(ctx.author, page=0))

    @deal.command()
    async def forfeit(self, ctx):
        user_id = str(ctx.author.id)
        if user_id in self.games:
            del self.games[user_id]
            self.save()
            await ctx.send("Your game was cancelled.")
        else:
            await ctx.send("You have no active game.")

    def make_case_view(self, user, page=0):
        game = self.games[str(user.id)]
        view = View()
        case_numbers = [i for i in range(1, 27) if i not in game["opened_cases"]]
        max_per_page = 25
        paginated = case_numbers[page*max_per_page:(page+1)*max_per_page]

        for i in paginated:
            view.add_item(self.CaseButton(i, self, user))

        # Add pagination if needed
        if len(case_numbers) > max_per_page:
            if page > 0:
                view.add_item(self.PrevPageButton(user, self, page - 1))
            if (page + 1) * max_per_page < len(case_numbers):
                view.add_item(self.NextPageButton(user, self, page + 1))

        return view

    class CaseButton(Button):
        def __init__(self, case_number, cog, user):
            super().__init__(label=f"Case {case_number}", style=discord.ButtonStyle.primary)
            self.case_number = case_number
            self.cog = cog
            self.user = user

        async def callback(self, interaction):
            user_id = str(self.user.id)
            game = self.cog.games[user_id]

            if game["deal_taken"]:
                await interaction.response.send_message("The game has already ended.", ephemeral=True)
                return

            if game["player_case"] is None:
                game["player_case"] = self.case_number
                self.cog.save()
                await interaction.response.send_message(
                    f"🎉 You chose case #{self.case_number} to keep. Open 6 other cases.",
                    ephemeral=True
                )
            else:
                if self.case_number == game["player_case"] or self.case_number in game["opened_cases"]:
                    await interaction.response.send_message("You can't open this case.", ephemeral=True)
                    return

                game["opened_cases"].append(self.case_number)
                val = game["case_values"][self.case_number - 1]
                await interaction.response.send_message(f"💼 Case #{self.case_number} had **${val:,}**")

                cases_to_open = 6 - game["round"] + 1
                if len(game["opened_cases"]) >= cases_to_open:
                    offer = self.cog.banker_offer(game)
                    game["offers"].append(offer)
                    self.cog.save()
                    view = View()
                    view.add_item(self.cog.DealButton(self.user, offer, self.cog))
                    view.add_item(self.cog.NoDealButton(self.user, self.cog))
                    await interaction.followup.send(f"☎️ The Banker offers: **${offer:,}**\nDeal or No Deal?", view=view)
                else:
                    self.cog.save()
                    await interaction.followup.send(embed=self.cog.build_case_embed(user_id),
                                                   view=self.cog.make_case_view(self.user))

    class DealButton(Button):
        def __init__(self, user, offer, cog):
            super().__init__(label="Deal", style=discord.ButtonStyle.success)
            self.user = user
            self.offer = offer
            self.cog = cog

        async def callback(self, interaction):
            await bank.deposit_credits(self.user, self.offer)
            user_id = str(self.user.id)
            game = self.cog.games[user_id]
            game["deal_taken"] = True
            self.cog.save()
            await interaction.response.send_message(f"✅ You accepted the deal and won **${self.offer:,}**!")

    class NoDealButton(Button):
        def __init__(self, user, cog):
            super().__init__(label="No Deal", style=discord.ButtonStyle.danger)
            self.user = user
            self.cog = cog

        async def callback(self, interaction):
            user_id = str(self.user.id)
            game = self.cog.games[user_id]
            game["round"] += 1
            self.cog.save()
            await interaction.response.send_message("📦 No Deal! Pick more cases:",
                                                    view=self.cog.make_case_view(self.user))

    class PrevPageButton(Button):
        def __init__(self, user, cog, page):
            super().__init__(label="Previous", style=discord.ButtonStyle.secondary)
            self.user = user
            self.cog = cog
            self.page = page

        async def callback(self, interaction):
            await interaction.response.edit_message(view=self.cog.make_case_view(self.user, self.page))

    class NextPageButton(Button):
        def __init__(self, user, cog, page):
            super().__init__(label="Next", style=discord.ButtonStyle.secondary)
            self.user = user
            self.cog = cog
            self.page = page

        async def callback(self, interaction):
            await interaction.response.edit_message(view=self.cog.make_case_view(self.user, self.page))
