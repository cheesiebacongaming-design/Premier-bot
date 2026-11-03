"""Logical Cog ownership for the bot's existing slash commands."""

from .administration import build_administration_cog
from .agents import build_agents_cog
from .matches import build_matches_cog
from .players import build_players_cog
from .playbooks import build_playbooks_cog
from .premier import build_premier_cog


COG_BUILDERS = (
    build_premier_cog,
    build_matches_cog,
    build_players_cog,
    build_agents_cog,
    build_playbooks_cog,
    build_administration_cog,
)


async def install_command_cogs(bot, tree):
    """Transfer already-defined slash commands into their feature Cogs.

    The original command objects carry the user-facing descriptions, option
    metadata, choices, permissions and autocomplete callbacks. The Cogs own
    the registered copies while their callbacks continue to use the existing
    implementations during this incremental refactor.
    """
    commands_by_name = {command.name: command for command in tree.get_commands()}
    assigned_names = [name for builder in COG_BUILDERS for name in builder.command_names]
    if len(assigned_names) != len(set(assigned_names)):
        raise RuntimeError("A slash command is assigned to more than one Cog")
    builders = {name: builder for builder in COG_BUILDERS for name in builder.command_names}
    missing = set(commands_by_name) - set(builders)
    absent = set(builders) - set(commands_by_name)
    if missing or absent:
        raise RuntimeError(
            f"Slash command Cog mapping mismatch (unassigned={sorted(missing)}, missing={sorted(absent)})"
        )

    for name in commands_by_name:
        tree.remove_command(name)

    for builder in COG_BUILDERS:
        owned_commands = [commands_by_name[name] for name in builder.command_names]
        await bot.add_cog(builder(owned_commands))
