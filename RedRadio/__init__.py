from .radio import UnifiedAudioRadio

async def setup(bot):

    await bot.add_cog(UnifiedAudioRadio(bot))
