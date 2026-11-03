"""Administrative, diagnostics, and help commands."""

from .base import make_command_cog

COMMAND_NAMES = ("clearcache", "testreminder", "testplaycard", "testrankcard", "help", "debug")


def build_administration_cog(commands):
    return make_command_cog("AdministrationCog", "Bot administration and diagnostics.", commands)


build_administration_cog.command_names = COMMAND_NAMES
