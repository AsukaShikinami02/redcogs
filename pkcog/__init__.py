from .pk import PluralKitIntegration

async def setup(bot):
    await bot.add_cog(PluralKitIntegration(bot))
