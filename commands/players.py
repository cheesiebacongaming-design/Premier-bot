"""Player account and rank commands."""

from .base import make_command_cog

COMMAND_NAMES = ("register", "unregister", "accounts", "rank", "player")


def build_players_cog(commands):
    return make_command_cog("PlayersCog", "Player accounts and rank information.", commands)


build_players_cog.command_names = COMMAND_NAMES
