"""Agent assignment and lineup commands."""

from .base import make_command_cog

COMMAND_NAMES = ("agent", "assignagent", "lineup", "clearagent")


def build_agents_cog(commands):
    return make_command_cog("AgentsCog", "Agent picks and team lineups.", commands)


build_agents_cog.command_names = COMMAND_NAMES
