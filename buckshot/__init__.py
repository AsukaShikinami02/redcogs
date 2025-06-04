from .buckshot import BuckshotRoulette

async def setup(bot):
   await bot.add_cog(BuckshotRoulette(bot))