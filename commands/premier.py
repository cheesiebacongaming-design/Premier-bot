"""Premier schedule and team lookup commands."""

from .base import make_command_cog

COMMAND_NAMES = ("schedule", "setschedule", "findteam", "scout")


def build_premier_cog(commands):
    return make_command_cog("PremierCog", "Premier schedule and team tools.", commands)


build_premier_cog.command_names = COMMAND_NAMES
