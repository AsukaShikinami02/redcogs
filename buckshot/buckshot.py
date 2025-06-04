import random
import asyncio
from typing import Optional, Dict, List

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import (
    box,
    pagify,
    humanize_number,
    humanize_list
)
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

class BuckshotRoulette(commands.Cog):
    """Play Buckshot Roulette against the bot - with realistic mechanics!"""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567891)
        self.config.register_guild(
            leaderboard={},
            min_bet=100,
            max_bet=10000,
            min_lives=2,
            max_lives=4,
            min_shells=2,
            max_shells=8
        )
        self.games = {}  # {channel_id: game_data}

    async def _get_economy(self, ctx):
        """Safely get the Economy cog"""
        economy = self.bot.get_cog("Economy")
        if economy is None:
            await ctx.send("❌ The Economy cog is not loaded. Please load it first.")
        return economy

    async def _ensure_account(self, ctx, user: discord.Member):
        """Ensure a user has a bank account"""
        economy = await self._get_economy(ctx)
        if economy is None:
            return False
        
        if not await economy.account_exists(user):
            await economy.bank.create_account(user)
        return True

    async def _can_spend(self, ctx, user: discord.Member, amount: int):
        """Check if a user can spend an amount"""
        economy = await self._get_economy(ctx)
        if economy is None:
            return False
        
        try:
            return await economy.can_spend(user, amount)
        except Exception as e:
            await ctx.send(f"❌ Error checking balance: {e}")
            return False

    async def _withdraw_credits(self, ctx, user: discord.Member, amount: int):
        """Safely withdraw credits"""
        economy = await self._get_economy(ctx)
        if economy is None:
            return False
        
        try:
            await economy.withdraw_credits(user, amount)
            return True
        except Exception as e:
            await ctx.send(f"❌ Error withdrawing credits: {e}")
            return False

    async def _deposit_credits(self, ctx, user: discord.Member, amount: int):
        """Safely deposit credits"""
        economy = await self._get_economy(ctx)
        if economy is None:
            return False
        
        try:
            await economy.deposit_credits(user, amount)
            return True
        except Exception as e:
            await ctx.send(f"❌ Error depositing credits: {e}")
            return False

    @commands.group()
    @commands.guild_only()
    async def buckshot(self, ctx):
        """Buckshot Roulette commands"""
        pass

    @buckshot.command(name="start")
    async def start_game(self, ctx, bet: int):
        """Start a new Buckshot Roulette game with a bet"""
        # Validate bet amount
        min_bet = await self.config.guild(ctx.guild).min_bet()
        max_bet = await self.config.guild(ctx.guild).max_bet()
        
        if bet < min_bet:
            return await ctx.send(f"Minimum bet is {humanize_number(min_bet)}!")
        if bet > max_bet:
            return await ctx.send(f"Maximum bet is {humanize_number(max_bet)}!")

        # Economy checks
        if not await self._ensure_account(ctx, ctx.author):
            return
            
        if not await self._can_spend(ctx, ctx.author, bet):
            return
            
        if not await self._withdraw_credits(ctx, ctx.author, bet):
            return

        # Initialize game
        min_lives = await self.config.guild(ctx.guild).min_lives()
        max_lives = await self.config.guild(ctx.guild).max_lives()
        lives = random.randint(min_lives, max_lives)
        
        self.games[ctx.channel.id] = {
            "player": ctx.author.id,
            "bet": bet,
            "current_pot": bet,
            "round": 1,
            "shells": [],
            "position": 0,
            "player_lives": lives,
            "bot_lives": lives,
            "items": {
                "player": ["handcuffs", "beer", "magnifier"],
                "bot": ["handcuffs", "beer", "magnifier"]
            },
            "handcuffed": False
        }
        
        await self._load_gun(ctx.channel.id)
        await ctx.send(
            f"🔫 **Buckshot Roulette - Round 1**\n"
            f"**Bet:** {humanize_number(bet)}\n"
            f"**Current Pot:** {humanize_number(bet)}\n"
            f"**Lives:** You: {'❤️' * lives} | Bot: {'❤️' * lives}\n"
            f"**Items:** {humanize_list(self.games[ctx.channel.id]['items']['player'])}\n\n"
            f"Type `{ctx.prefix}buckshot shoot self` or `{ctx.prefix}buckshot shoot bot` to take your shot.\n"
            f"Use `{ctx.prefix}buckshot use <item>` to use an item.\n"
            f"Type `{ctx.prefix}buckshot quit` to forfeit and lose your bet."
        )

    # ... (rest of your existing commands with economy checks replaced with the safe methods)

    async def _end_game(self, ctx, game, player_won: bool):
        """End the game and distribute winnings"""
        author = ctx.author
        
        if player_won:
            winnings = game["current_pot"]
            success = await self._deposit_credits(ctx, author, winnings)
            if not success:
                return
            
            # Update leaderboard
            async with self.config.guild(ctx.guild).leaderboard() as leaderboard:
                user_id = str(author.id)
                leaderboard[user_id] = leaderboard.get(user_id, 0) + 1
            
            await ctx.send(
                f"🎉 **You win!**\n"
                f"**Winnings:** {humanize_number(winnings)}\n"
                f"**Total Pot:** {humanize_number(winnings)}"
            )
        else:
            await ctx.send(
                "💀 **You lost!** Better luck next time.\n"
                f"**Lost Bet:** {humanize_number(game['bet'])}"
            )
        
        # Clean up game
        del self.games[ctx.channel.id]

    # ... (rest of your existing methods)