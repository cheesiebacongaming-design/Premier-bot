"""Map playbook commands."""

from .base import make_command_cog

COMMAND_NAMES = ("playbook", "createplaybook")


def build_playbooks_cog(commands):
    return make_command_cog("PlaybooksCog", "Map strategy notes and links.", commands)


build_playbooks_cog.command_names = COMMAND_NAMES
