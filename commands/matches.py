"""Match reporting and session commands."""

from .base import make_command_cog

COMMAND_NAMES = ("results", "mapresults", "play", "matchfeed", "recent")


def build_matches_cog(commands):
    return make_command_cog("MatchesCog", "Match reporting and play sessions.", commands)


build_matches_cog.command_names = COMMAND_NAMES
