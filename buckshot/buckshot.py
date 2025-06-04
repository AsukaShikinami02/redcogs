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

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        # Delete user's leaderboard data
        for guild_id in await self.config.all_guilds():
            async with self.config.guild_from_id(guild_id).leaderboard() as leaderboard:
                if str(user_id) in leaderboard:
                    del leaderboard[str(user_id)]

    @commands.group()
    @commands.guild_only()
    async def buckshot(self, ctx):
        """Buckshot Roulette commands"""
        pass

    @buckshot.command(name="start")
    async def start_game(self, ctx, bet: int):
        """Start a new Buckshot Roulette game with a bet"""
        guild = ctx.guild
        channel = ctx.channel
        author = ctx.author
        
        # Check if game already in progress
        if channel.id in self.games:
            return await ctx.send("A game is already in progress in this channel!")
        
        # Validate bet amount
        min_bet = await self.config.guild(guild).min_bet()
        max_bet = await self.config.guild(guild).max_bet()
        
        if bet < min_bet:
            return await ctx.send(f"Minimum bet is {humanize_number(min_bet)}!")
        if bet > max_bet:
            return await ctx.send(f"Maximum bet is {humanize_number(max_bet)}!")
        
        # Check user's balance
        economy = self.bot.get_cog("Economy")
        if not economy:
            return await ctx.send("Economy cog is not loaded!")
        
        if not await economy.can_spend(author, bet):
            return await ctx.send("You don't have enough credits to make that bet!")
        
        # Deduct bet amount
        await economy.withdraw_credits(author, bet)
        
        # Determine lives (2-4)
        min_lives = await self.config.guild(guild).min_lives()
        max_lives = await self.config.guild(guild).max_lives()
        lives = random.randint(min_lives, max_lives)
        
        # Initialize game
        self.games[channel.id] = {
            "player": author.id,
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
        
        await self._load_gun(channel.id)
        await ctx.send(
            f"🔫 **Buckshot Roulette - Round 1**\n"
            f"**Bet:** {humanize_number(bet)}\n"
            f"**Current Pot:** {humanize_number(bet)}\n"
            f"**Lives:** You: {'❤️' * lives} | Bot: {'❤️' * lives}\n"
            f"**Items:** {humanize_list(game['items']['player'])}\n\n"
            f"Type `{ctx.prefix}buckshot shoot self` or `{ctx.prefix}buckshot shoot bot` to take your shot.\n"
            f"Use `{ctx.prefix}buckshot use <item>` to use an item.\n"
            f"Type `{ctx.prefix}buckshot quit` to forfeit and lose your bet."
        )

    @buckshot.command(name="shoot")
    async def shoot(self, ctx, target: str):
        """Take your turn and shoot yourself or the bot
        Usage: [p]buckshot shoot <self/bot>"""
        channel = ctx.channel
        author = ctx.author
        
        # Check if game exists
        if channel.id not in self.games:
            return await ctx.send("No game in progress! Start one with `buckshot start`.")
        
        game = self.games[channel.id]
        
        # Check if it's the player's turn
        if game["player"] != author.id:
            return await ctx.send("It's not your game!")
        
        # Check if game is already over
        if game["player_lives"] <= 0 or game["bot_lives"] <= 0:
            return await ctx.send("The game is already over!")
        
        # Check if player is handcuffed
        if game["handcuffed"]:
            game["handcuffed"] = False
            return await ctx.send("You're handcuffed and can't shoot this turn!")
        
        # Validate target
        target = target.lower()
        if target not in ["self", "bot"]:
            return await ctx.send("Please specify `self` or `bot` as the target.")
        
        # Player shoots
        await ctx.send(f"🔫 You point the gun at {'yourself' if target == 'self' else 'the bot'} and pull the trigger...")
        await asyncio.sleep(2)
        
        shell = game["shells"][game["position"]]
        game["position"] += 1
        
        if shell == "blank":
            result_msg = "*Click* It's a blank!"
            if target == "self":
                result_msg += " You survive."
            else:
                result_msg += " The bot survives."
            await ctx.send(result_msg)
        else:
            if target == "self":
                game["player_lives"] -= 1
                result_msg = "💥 **BANG!** It's a live round! You lose a life."
                if game["player_lives"] <= 0:
                    await self._end_game(ctx, game, False)
                    return
            else:
                game["bot_lives"] -= 1
                result_msg = "💥 **BANG!** It's a live round! The bot loses a life."
                if game["bot_lives"] <= 0:
                    await self._end_game(ctx, game, True)
                    return
            
            await ctx.send(result_msg)
        
        # Show status
        await ctx.send(
            f"**Lives Remaining:** You: {'❤️' * game['player_lives']} | "
            f"Bot: {'❤️' * game['bot_lives']}\n"
            f"**Shells Left:** {len(game['shells']) - game['position']}"
        )
        
        # Check if we need to reload
        if game["position"] >= len(game["shells"]):
            await self._next_round(ctx, game)
        else:
            await self._bot_turn(ctx, game)
    
    async def _bot_turn(self, ctx, game):
        """Bot's turn to shoot"""
        channel = ctx.channel
        
        # Bot uses items randomly (33% chance)
        if random.random() < 0.33 and game["items"]["bot"]:
            item = random.choice(game["items"]["bot"])
            await self._use_bot_item(ctx, game, item)
            return
        
        # Bot chooses target (weighted 70% to shoot player)
        target = "player" if random.random() < 0.7 else "self"
        
        await ctx.send(f"\n🤖 The bot takes the gun and points it at {'itself' if target == 'self' else 'you'}...")
        await asyncio.sleep(2)
        
        shell = game["shells"][game["position"]]
        game["position"] += 1
        
        if shell == "blank":
            result_msg = "*Click* It's a blank!"
            if target == "self":
                result_msg += " The bot survives."
            else:
                result_msg += " You survive."
            await ctx.send(result_msg)
        else:
            if target == "self":
                game["bot_lives"] -= 1
                result_msg = "💥 **BANG!** It's a live round! The bot loses a life."
                if game["bot_lives"] <= 0:
                    await self._end_game(ctx, game, True)
                    return
            else:
                game["player_lives"] -= 1
                result_msg = "💥 **BANG!** It's a live round! You lose a life."
                if game["player_lives"] <= 0:
                    await self._end_game(ctx, game, False)
                    return
            
            await ctx.send(result_msg)
        
        # Show status
        await ctx.send(
            f"**Lives Remaining:** You: {'❤️' * game['player_lives']} | "
            f"Bot: {'❤️' * game['bot_lives']}\n"
            f"**Shells Left:** {len(game['shells']) - game['position']}"
        )
        
        # Check if we need to reload
        if game["position"] >= len(game["shells"]):
            await self._next_round(ctx, game)
        else:
            await ctx.send(f"\nYour turn! Type `{ctx.prefix}buckshot shoot self` or `{ctx.prefix}buckshot shoot bot`")
    
    async def _use_bot_item(self, ctx, game, item):
        """Bot uses an item"""
        if item not in game["items"]["bot"]:
            return
        
        game["items"]["bot"].remove(item)
        
        if item == "handcuffs":
            game["handcuffed"] = True
            await ctx.send("🤖 The bot uses **handcuffs** on you! You'll be unable to act next turn.")
        elif item == "beer":
            # Remove current shell
            if game["position"] < len(game["shells"]):
                removed_shell = game["shells"].pop(game["position"])
                await ctx.send(f"🤖 The bot uses **beer** to eject a {removed_shell} shell!")
            else:
                await ctx.send("🤖 The bot tries to use **beer** but there are no shells left!")
        elif item == "magnifier":
            if game["position"] < len(game["shells"]):
                next_shell = game["shells"][game["position"]]
                await ctx.send(f"🤖 The bot uses **magnifier** and sees the next shell is a {next_shell}!")
            else:
                await ctx.send("🤖 The bot tries to use **magnifier** but there are no shells left!")
        
        # Continue with bot's turn
        await self._bot_turn(ctx, game)
    
    async def _next_round(self, ctx, game):
        """Advance to the next round"""
        channel = ctx.channel
        game["round"] += 1
        game["current_pot"] *= 2
        await self._load_gun(channel.id)
        
        # Refresh items every 2 rounds
        if game["round"] % 2 == 1:
            game["items"]["player"].extend(["handcuffs", "beer", "magnifier"])
            game["items"]["bot"].extend(["handcuffs", "beer", "magnifier"])
            # Ensure no more than 3 of each item
            game["items"]["player"] = game["items"]["player"][:3]
            game["items"]["bot"] = game["items"]["bot"][:3]
        
        await ctx.send(
            f"🔫 **Round {game['round']}**\n"
            f"**Current Pot:** {humanize_number(game['current_pot'])}\n"
            f"**Lives:** You: {'❤️' * game['player_lives']} | Bot: {'❤️' * game['bot_lives']}\n"
            f"**Items:** {humanize_list(game['items']['player'])}\n\n"
            f"Type `{ctx.prefix}buckshot shoot self` or `{ctx.prefix}buckshot shoot bot` to take your turn."
        )

    async def _load_gun(self, channel_id: int):
        """Load the gun with random shells (2-8 shells, at least 1 live and 1 blank)"""
        game = self.games[channel_id]
        
        min_shells = await self.config.guild(ctx.guild).min_shells()
        max_shells = await self.config.guild(ctx.guild).max_shells()
        num_shells = random.randint(min_shells, max_shells)
        
        # Ensure at least 1 live and 1 blank
        live_rounds = random.randint(1, num_shells - 1)
        blanks = num_shells - live_rounds
        
        # Create shell list
        shells = ["live"] * live_rounds + ["blank"] * blanks
        random.shuffle(shells)
        
        game["shells"] = shells
        game["position"] = 0
        
    @buckshot.command(name="use")
    async def use_item(self, ctx, item: str):
        """Use an item (handcuffs, beer, magnifier)"""
        channel = ctx.channel
        author = ctx.author
        
        # Check if game exists
        if channel.id not in self.games:
            return await ctx.send("No game in progress!")
        
        game = self.games[channel.id]
        
        # Check if it's the player's turn
        if game["player"] != author.id:
            return await ctx.send("It's not your game!")
        
        # Check if game is already over
        if game["player_lives"] <= 0 or game["bot_lives"] <= 0:
            return await ctx.send("The game is already over!")
        
        item = item.lower()
        
        # Check if player has the item
        if item not in game["items"]["player"]:
            return await ctx.send(f"You don't have a {item}!")
        
        game["items"]["player"].remove(item)
        
        if item == "handcuffs":
            game["handcuffed"] = True
            await ctx.send("🔒 You use **handcuffs** on the bot! It will be unable to act next turn.")
            await self._bot_turn(ctx, game)
        elif item == "beer":
            # Remove current shell
            if game["position"] < len(game["shells"]):
                removed_shell = game["shells"].pop(game["position"])
                await ctx.send(f"🍺 You use **beer** to eject a {removed_shell} shell!")
            else:
                await ctx.send("🍺 You try to use **beer** but there are no shells left!")
            await self._bot_turn(ctx, game)
        elif item == "magnifier":
            if game["position"] < len(game["shells"]):
                next_shell = game["shells"][game["position"]]
                await ctx.send(f"🔍 You use **magnifier** and see the next shell is a {next_shell}!")
            else:
                await ctx.send("🔍 You try to use **magnifier** but there are no shells left!")
            await ctx.send(f"Type `{ctx.prefix}buckshot shoot self` or `{ctx.prefix}buckshot shoot bot` to continue.")
    
    async def _end_game(self, ctx, game, player_won: bool):
        """End the game and distribute winnings"""
        channel = ctx.channel
        guild = ctx.guild
        author = ctx.author
        
        economy = self.bot.get_cog("Economy")
        
        if player_won:
            winnings = game["current_pot"]
            await economy.deposit_credits(author, winnings)
            
            # Update leaderboard
            async with self.config.guild(guild).leaderboard() as leaderboard:
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
        del self.games[channel.id]
    
    @buckshot.command(name="quit")
    async def quit_game(self, ctx):
        """Quit the current game and lose your bet"""
        channel = ctx.channel
        author = ctx.author
        
        if channel.id not in self.games:
            return await ctx.send("No game in progress!")
        
        game = self.games[channel.id]
        
        if game["player"] != author.id:
            return await ctx.send("It's not your game to quit!")
        
        await ctx.send(
            "🏳️ You forfeited the game and lost your bet.\n"
            f"**Lost Bet:** {humanize_number(game['bet'])}"
        )
        
        # Clean up game
        del self.games[channel.id]
    
    @buckshot.command(name="leaderboard", aliases=["lb"])
    async def show_leaderboard(self, ctx):
        """Show the Buckshot Roulette leaderboard"""
        leaderboard = await self.config.guild(ctx.guild).leaderboard()
        
        if not leaderboard:
            return await ctx.send("No games have been won yet!")
        
        # Sort leaderboard
        sorted_lb = sorted(
            leaderboard.items(),
            key=lambda x: x[1],
            reverse=True
        )
        
        # Format entries
        entries = []
        for i, (user_id, wins) in enumerate(sorted_lb, 1):
            user = ctx.guild.get_member(int(user_id))
            username = user.display_name if user else f"Unknown User ({user_id})"
            entries.append(f"{i}. {username}: {humanize_number(wins)} wins")
        
        # Pagify if needed
        pages = []
        for page in pagify("\n".join(entries)):
            pages.append(box(page, lang="md"))
        
        if len(pages) == 1:
            await ctx.send(f"**Buckshot Roulette Leaderboard**\n{pages[0]}")
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS)
    
    @buckshot.command(name="stats")
    async def player_stats(self, ctx, user: Optional[discord.Member] = None):
        """Check your or another player's stats"""
        user = user or ctx.author
        leaderboard = await self.config.guild(ctx.guild).leaderboard()
        wins = leaderboard.get(str(user.id), 0)
        
        await ctx.send(
            f"**{user.display_name}'s Buckshot Roulette Stats**\n"
            f"🏆 **Wins:** {humanize_number(wins)}"
        )
    
    @checks.admin_or_permissions(manage_guild=True)
    @buckshot.command(name="setlimits")
    async def set_bet_limits(self, ctx, min_bet: int, max_bet: int):
        """Set the minimum and maximum bet amounts"""
        if min_bet < 0 or max_bet < 0:
            return await ctx.send("Bet amounts cannot be negative!")
        if min_bet > max_bet:
            return await ctx.send("Minimum bet cannot be higher than maximum bet!")
        
        await self.config.guild(ctx.guild).min_bet.set(min_bet)
        await self.config.guild(ctx.guild).max_bet.set(max_bet)
        
        await ctx.send(
            f"Bet limits updated:\n"
            f"**Minimum:** {humanize_number(min_bet)}\n"
            f"**Maximum:** {humanize_number(max_bet)}"
        )
    
    @checks.admin_or_permissions(manage_guild=True)
    @buckshot.command(name="setlives")
    async def set_lives_range(self, ctx, min_lives: int, max_lives: int):
        """Set the minimum and maximum lives range"""
        if min_lives < 1 or max_lives < 1:
            return await ctx.send("Lives cannot be less than 1!")
        if min_lives > max_lives:
            return await ctx.send("Minimum lives cannot be higher than maximum lives!")
        
        await self.config.guild(ctx.guild).min_lives.set(min_lives)
        await self.config.guild(ctx.guild).max_lives.set(max_lives)
        
        await ctx.send(
            f"Lives range updated:\n"
            f"**Minimum:** {min_lives}\n"
            f"**Maximum:** {max_lives}"
        )
    
    @checks.admin_or_permissions(manage_guild=True)
    @buckshot.command(name="setshells")
    async def set_shells_range(self, ctx, min_shells: int, max_shells: int):
        """Set the minimum and maximum shells range"""
        if min_shells < 2 or max_shells < 2:
            return await ctx.send("Must have at least 2 shells!")
        if min_shells > max_shells:
            return await ctx.send("Minimum shells cannot be higher than maximum shells!")
        
        await self.config.guild(ctx.guild).min_shells.set(min_shells)
        await self.config.guild(ctx.guild).max_shells.set(max_shells)
        
        await ctx.send(
            f"Shells range updated:\n"
            f"**Minimum:** {min_shells}\n"
            f"**Maximum:** {max_shells}"
        )