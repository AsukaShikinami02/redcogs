import discord
import random
import asyncio
from redbot.core import commands, bank, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

class BuckshotRoulette(commands.Cog):
    """Buckshot Roulette game with buttons, leaderboard, rematch, and stat resets."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=123456789012345678)
        default_user = {
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "games": 0,
        }
        self.config.register_user(**default_user)
        self.active_games = {}  # user_id: game state

    # --- Helper functions ---

    def generate_shells(self):
        # 8 shells, 2–5 live rounds randomly placed
        lives = random.randint(2, 5)
        blanks = 8 - lives
        shells = [True] * lives + [False] * blanks
        random.shuffle(shells)
        return shells

    def hearts(self, count):
        return "❤️" * count + "🖤" * (4 - count)

    def shell_emojis(self, shells):
        return "".join("🔴" if s else "⚪" for s in shells)

    def win_percentage(self, wins, games):
        if games == 0:
            return "0.0%"
        return f"{(wins / games) * 100:.1f}%"

    # --- Commands ---

    @commands.command()
    async def buckshot(self, ctx: commands.Context, amount: int):
        """Start a game of Buckshot Roulette with a bet."""

        if amount <= 0:
            return await ctx.send("❌ Bet must be greater than zero.")
        bal = await bank.get_balance(ctx.author)
        if bal < amount:
            return await ctx.send("❌ You don't have enough credits for that bet.")

        if ctx.author.id in self.active_games:
            return await ctx.send("⚠️ You already have an active game!")

        # Withdraw bet immediately; if you lose you lose it, if win you get double later
        await bank.withdraw_credits(ctx.author, amount)

        # Setup initial game state
        game = {
            "user": ctx.author,
            "bet": amount,
            "user_lives": random.randint(2, 4),
            "bot_lives": random.randint(2, 4),
            "shells": self.generate_shells(),
            "turn": 0,  # even=user, odd=bot
            "log": [],
            "message": None,
            "channel": ctx.channel,
            "rematch": False,
        }
        self.active_games[ctx.author.id] = game

        await ctx.send(
            f"🎲 **Buckshot Roulette started!** {ctx.author.mention} bets {amount} credits.\n"
            f"Both you and the bot start with lives between 2 and 4.\n"
            "On your turn, choose to shoot yourself 💀 or the dealer 🎯.\n"
            "First to lose all lives loses the game."
        )

        await asyncio.sleep(1.5)
        await self.show_status(game)
        await self.prompt_user(game)

    # --- Game Flow ---

    async def show_status(self, game):
        shells_display = self.shell_emojis(game["shells"])
        user_hearts = self.hearts(game["user_lives"])
        bot_hearts = self.hearts(game["bot_lives"])
        status = (
            f"Shells left: {shells_display}\n"
            f"🧍 {game['user'].mention}: {user_hearts} ({game['user_lives']} lives)\n"
            f"🤖 Bot: {bot_hearts} ({game['bot_lives']} lives)\n"
        )
        if game["message"]:
            await game["message"].edit(content=status, view=game["view"])
        else:
            game["message"] = await game["channel"].send(status)

    async def prompt_user(self, game):
        """Prompt user with buttons to choose shooting self or dealer."""
        if game["user_lives"] <= 0 or game["bot_lives"] <= 0:
            return await self.end_game(game)

        # Check shells, reload if empty
        if not game["shells"]:
            game["shells"] = self.generate_shells()
            await game["channel"].send("🔄 Chamber empty! Reloading shells...")

        # Create buttons
        view = BuckshotView(self, game)
        game["view"] = view
        content = (
            f"Your turn, {game['user'].mention}!\n"
            f"Choose who to shoot:"
        )
        if game["message"]:
            await game["message"].edit(content=content, view=view)
        else:
            game["message"] = await game["channel"].send(content, view=view)

    async def user_shoot(self, game, target):
        """Process user's shot: 'self' or 'bot'."""
        if not game["shells"]:
            game["shells"] = self.generate_shells()
            await game["channel"].send("🔄 Chamber empty! Reloading shells...")

        shell = game["shells"].pop(0)
        shooter = "You"
        target_name = "yourself" if target == "self" else "the bot"

        if shell:
            # Live round, target loses a life
            if target == "self":
                game["user_lives"] -= 1
                result = f"💥 {shooter} shot {target_name} — you lost a life!"
            else:
                game["bot_lives"] -= 1
                result = f"💥 {shooter} shot {target_name} — bot lost a life!"
        else:
            result = f"*click* {shooter} shot {target_name} — no damage."

        game["log"].append(result)
        await game["channel"].send(result)
        await asyncio.sleep(1.5)

        # Check end of game
        if game["user_lives"] <= 0 or game["bot_lives"] <= 0:
            return await self.end_game(game)

        # Bot turn next
        await self.bot_turn(game)

    async def bot_turn(self, game):
        if game["user_lives"] <= 0 or game["bot_lives"] <= 0:
            return await self.end_game(game)

        if not game["shells"]:
            game["shells"] = self.generate_shells()
            await game["channel"].send("🔄 Chamber empty! Reloading shells...")

        # Bot logic: simple AI — shoot player if bot lives > 1 else self (random)
        if game["bot_lives"] > 1:
            target = random.choices(["self", "user"], weights=[0.3, 0.7])[0]
        else:
            target = random.choice(["self", "user"])

        shell = game["shells"].pop(0)

        if shell:
            if target == "self":
                game["bot_lives"] -= 1
                result = f"💥 Bot shot itself — bot lost a life!"
            else:
                game["user_lives"] -= 1
                result = f"💥 Bot shot you — you lost a life!"
        else:
            result = f"*click* Bot shot {('itself' if target=='self' else 'you')} — no damage."

        game["log"].append(result)
        await game["channel"].send(result)
        await asyncio.sleep(1.5)

        if game["user_lives"] <= 0 or game["bot_lives"] <= 0:
            return await self.end_game(game)

        # Show status and prompt user again
        await self.show_status(game)
        await self.prompt_user(game)

    async def end_game(self, game):
        user = game["user"]
        bet = game["bet"]
        user_lives = game["user_lives"]
        bot_lives = game["bot_lives"]
        log = game["log"][-6:]  # last 6 logs

        # Determine outcome
        if user_lives > 0 and bot_lives <= 0:
            outcome = "win"
            await bank.deposit_credits(user, bet * 2)
            msg = f"🎉 You won and earned {bet * 2} credits!"
        elif bot_lives > 0 and user_lives <= 0:
            outcome = "lose"
            msg = f"💀 You lost {bet} credits. Better luck next time!"
        else:
            outcome = "draw"
            await bank.deposit_credits(user, bet)  # refund bet on draw
            msg = "🤝 It's a draw! Your bet has been returned."

        # Update stats
        async with self.config.user(user).all() as stats:
            stats["games"] += 1
            stats[outcome + "s"] += 1

        # Clear active game
        self.active_games.pop(user.id, None)

        embed = discord.Embed(
            title="🔚 Buckshot Roulette — Game Over",
            color=discord.Color.green() if outcome == "win" else discord.Color.red() if outcome == "lose" else discord.Color.orange()
        )
        embed.add_field(name="Result", value=msg, inline=False)
        embed.add_field(name="Your final lives", value=self.hearts(user_lives), inline=True)
        embed.add_field(name="Bot final lives", value=self.hearts(bot_lives), inline=True)
        embed.add_field(name="Last Actions", value="\n".join(log), inline=False)

        await game["message"].edit(content=None, embed=embed, view=None)

        # Send rematch buttons
        await self.send_rematch_buttons(game)

    async def send_rematch_buttons(self, game):
        view = RematchView(self, game["bet"], game["user"])
        await game["channel"].send(
            f"{game['user'].mention}, would you like a rematch with the same bet ({game['bet']} credits)?",
            view=view,
        )

    # --- Leaderboard and Stats Commands ---

    @commands.command()
    async def buckshotstats(self, ctx: commands.Context, user: discord.User = None):
        """Show your or another user's Buckshot Roulette stats."""
        user = user or ctx.author
        stats = await self.config.user(user).all()
        embed = discord.Embed(title=f"📊 Buckshot Stats — {user.display_name}", color=discord.Color.blurple())
        embed.add_field(name="Wins", value=stats["wins"])
        embed.add_field(name="Losses", value=stats["losses"])
        embed.add_field(name="Draws", value=stats["draws"])
        embed.add_field(name="Games Played", value=stats["games"])
        win_pct = self.win_percentage(stats["wins"], stats["games"])
        embed.add_field(name="Win Percentage", value=win_pct)
        await ctx.send(embed=embed)

    @commands.command()
    async def buckshotleaderboard(self, ctx: commands.Context):
        """Show the Buckshot Roulette leaderboard."""
        all_data = await self.config.all_users()

        # Filter users who played
        filtered = {
            user_id: data for user_id, data in all_data.items() if data.get("games", 0) > 0
        }

        if not filtered:
            return await ctx.send("No games have been played yet.")

        # Sort by wins descending
        sorted_data = sorted(filtered.items(), key=lambda x: x[1]["wins"], reverse=True)[:10]

        lines = []
        for user_id, data in sorted_data:
            member = ctx.guild.get_member(user_id) or await self.bot.fetch_user(user_id)
            name = member.display_name if member else f"User ID {user_id}"
            wins = data["wins"]
            losses = data["losses"]
            draws = data["draws"]
            games = data["games"]
            win_pct = self.win_percentage(wins, games)
            lines.append(f"{name:<15} | {wins:^4} | {losses:^6} | {draws:^5} | {win_pct:^7}")

        header = "Player          | Wins | Losses | Draws | Win %\n" + "-" * 45
        content = header + "\n" + "\n".join(lines)
        await ctx.send(box(content, lang="ansi"))

    @commands.command()
    @commands.is_owner()
    async def buckshotresetdaily(self, ctx: commands.Context):
        """Reset all users' Buckshot Roulette stats (daily reset)."""
        all_data = await self.config.all_users()
        for user_id in all_data:
            await self.config.user_from_id(user_id).set({
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "games": 0,
            })
        await ctx.send("✅ All Buckshot Roulette stats have been reset (daily).")

    @commands.command()
    @commands.is_owner()
    async def buckshotresetweekly(self, ctx: commands.Context):
        """Reset all users' Buckshot Roulette stats (weekly reset)."""
        # Same as daily for now, can customize
        await self.buckshotresetdaily(ctx)

    # --- Rematch ---

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        # To ensure interactions outside our views don't cause errors
        pass

class BuckshotView(discord.ui.View):
    def __init__(self, cog, game):
        super().__init__(timeout=60)
        self.cog = cog
        self.game = game

    @discord.ui.button(label="💀 Shoot Self", style=discord.ButtonStyle.danger)
    async def shoot_self(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.game["user"].id:
            return await interaction.response.send_message("This is not your game!", ephemeral=True)
        await interaction.response.defer()
        self.stop()
        await self.cog.user_shoot(self.game, "self")

    @discord.ui.button(label="🎯 Shoot Dealer", style=discord.ButtonStyle.primary)
    async def shoot_bot(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.game["user"].id:
            return await interaction.response.send_message("This is not your game!", ephemeral=True)
        await interaction.response.defer()
        self.stop()
        await self.cog.user_shoot(self.game, "bot")

class RematchView(discord.ui.View):
    def __init__(self, cog, bet, user):
        super().__init__(timeout=60)
        self.cog = cog
        self.bet = bet
        self.user = user

    @discord.ui.button(label="🔁 Rematch", style=discord.ButtonStyle.success)
    async def rematch(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("This is not your game!", ephemeral=True)
        await interaction.response.defer()
        self.stop()
        await self.cog.buckshot.callback(self.cog, interaction, self.bet)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("This is not your game!", ephemeral=True)
        await interaction.response.defer()
        self.stop()
        await interaction.message.edit(content="Rematch cancelled.", view=None)