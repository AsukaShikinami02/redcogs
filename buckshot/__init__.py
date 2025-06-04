from .buckshot import BuckshotRoulette

def setup(bot):
    bot.add_cog(BuckshotRoulette(bot))