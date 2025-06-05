import discord 
from discord.ext import commands 
from redbot.core import commands as red_commands, bank, Config
from redbot.core.utils.views import ConfirmView 
import random 
from typing import List

class BuckshotView(discord.ui.View): 
  def init(self, cog, ctx, bet, player_live,
      bot_lives): super().init(timeout=60)
      self.cog = cog self.ctx = ctx self.bet 
      bet self.player_lives = player_lives
      self.bot_lives = bot_lives self.shells
      self._load_shells() self.current_index
      self.message = None

def _load_shells(self):
    lives = random.randint(2, 6)
    blanks = 8 - lives
    return random.sample(["💥"] * lives + ["🧨"] * blanks, 8)

def _format_lives(self, lives: int) -> str:
    return "❤️" * lives

def _format_shells(self) -> str:
    return " ".join(self.shells[self.current_index:])

async def _display_status(self, interaction):
    embed = discord.Embed(title="🔫 Buckshot Roulette", color=discord.Color.red())
    embed.add_field(name="Your Lives", value=f"{self._format_lives(self.player_lives)} ({self.player_lives})", inline=True)
    embed.add_field(name="Dealer Lives", value=f"{self._format_lives(self.bot_lives)} ({self.bot_lives})", inline=True)
    embed.add_field(name="Chamber", value=f"{self._format_shells()}", inline=False)
    await interaction.response.edit_message(embed=embed, view=self)

async def handle_round(self, interaction, target):
    shell = self.shells[self.current_index]
    self.current_index += 1
    target_name = "You" if target == "player" else "Dealer"

    if shell == "💥":
        if target == "player":
            self.player_lives -= 1
        else:
            self.bot_lives -= 1

    if self.player_lives <= 0:
        await interaction.followup.send("💀 You died! Dealer wins.")
        await bank.withdraw_credits(self.ctx.author, self.bet)
        await self.cog.update_stats(self.ctx.author.id, loss=1)
        self.stop()
        return
    elif self.bot_lives <= 0:
        await interaction.followup.send("🏆 Dealer died! You win!")
        await bank.deposit_credits(self.ctx.author, self.bet * 2)
        await self.cog.update_stats(self.ctx.author.id, win=1)
        self.stop()
        return

    if self.current_index >= len(self.shells):
        self.shells = self._load_shells()
        self.current_index = 0

    await self._display_status(interaction)

@discord.ui.button(label="Shoot Yourself", style=discord.ButtonStyle.danger)
async def shoot_self(self, interaction: discord.Interaction, button: discord.ui.Button):
    if interaction.user != self.ctx.author:
        await interaction.response.send_message("This isn't your game!", ephemeral=True)
        return
    await self.handle_round(interaction, "player")

@discord.ui.button(label="Shoot Dealer", style=discord.ButtonStyle.primary)
async def shoot_dealer(self, interaction: discord.Interaction, button: discord.ui.Button):
    if interaction.user != self.ctx.author:
        await interaction.response.send_message("This isn't your game!", ephemeral=True)
        return
    await self.handle_round(interaction, "bot")

class Buckshot(commands.Cog): def init(self, bot): self.bot = bot self.config = Config.get_conf(self, identifier=938274982374, force_registration=True) self.config.register_user(wins=0, losses=0)

async def update_stats(self, user_id: int, win: int = 0, loss: int = 0):
    user = self.config.user_from_id(user_id)
    wins = await user.wins()
    losses = await user.losses()
    await user.wins.set(wins + win)
    await user.losses.set(losses + loss)

@red_commands.command()
async def buckshot(self, ctx: commands.Context, amount: int):
    bal = await bank.get_balance(ctx.author)
    if amount <= 0:
        await ctx.send("❌ You must bet more than 0 credits.")
        return
    if bal < amount:
        await ctx.send("❌ You don't have enough credits.")
        return

    player_lives = random.randint(2, 4)
    bot_lives = random.randint(2, 4)

    view = BuckshotView(self, ctx, amount, player_lives, bot_lives)
    embed = discord.Embed(title="🎲 Buckshot Roulette Starting!", description=f"Bet: {amount} credits\nChoose who to shoot.", color=discord.Color.red())
    view.message = await ctx.send(embed=embed, view=view)
    await view._display_status(view.message)

@red_commands.command()
async def buckshotlb(self, ctx: commands.Context):
    all_data = await self.config.all_users()
    sorted_data = sorted(all_data.items(), key=lambda kv: (kv[1].get("wins", 0) / (kv[1].get("wins", 0) + kv[1].get("losses", 0))) if (kv[1].get("wins", 0) + kv[1].get("losses", 0)) > 0 else 0, reverse=True)

    lines = []
    for idx, (user_id, stats) in enumerate(sorted_data[:10], start=1):
        user = self.bot.get_user(user_id) or f"User {user_id}"
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        total = wins + losses
        win_pct = (wins / total * 100) if total > 0 else 0
        lines.append(f"**{idx}.** {user} - 🏆 {wins} | 💀 {losses} | 📊 {win_pct:.1f}%")

    if not lines:
        lines = ["No games played yet!"]

    embed = discord.Embed(title="🏅 Buckshot Roulette Leaderboard", description="\n".join(lines), color=discord.Color.gold())
    await ctx.send(embed=embed)

