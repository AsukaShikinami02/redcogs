from .dealcog import DealOrNoDeal

async def setup(bot):
    await bot.add_cog(DealOrNoDeal(bot))
