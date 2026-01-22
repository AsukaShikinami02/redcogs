from .runapuronline import RunapurOnline

async def setup(bot):
    await bot.add_cog(RunapurOnline(bot))
