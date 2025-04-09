from .radio import RedRadio

async def setup(bot):
    await bot.add_cog(RedRadio(bot))
