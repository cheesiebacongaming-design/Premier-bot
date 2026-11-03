"""Helpers for moving existing app commands under Cog ownership."""

import copy

from discord.ext import commands


def make_command_cog(class_name, description, command_objects):
    """Build a Cog that owns a set of legacy slash command definitions."""
    namespace = {"__module__": __name__, "__doc__": description}

    for original in command_objects:
        command = copy.copy(original)
        command._params = original._params.copy()
        original_callback = original.callback

        def make_dispatch(callback):
            async def dispatch(self, interaction, **kwargs):
                return await callback(interaction, **kwargs)

            dispatch.__name__ = callback.__name__
            dispatch.__qualname__ = f"{class_name}.{callback.__name__}"
            return dispatch

        command._callback = make_dispatch(original_callback)
        namespace[command.name] = command

    cog_type = type(class_name, (commands.Cog,), namespace)
    return cog_type()
