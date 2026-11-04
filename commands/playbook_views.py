"""Interactive, read-only browsing for playbook strategies."""

import math

import discord

from services.playbook_service import _url
from services.playbook_store import PlaybookDataError


def _safe(value):
    return discord.utils.escape_mentions(str(value or ""))


def _chunks(text, limit=3400):
    text = str(text or "")
    if not text:
        return ["No details provided."]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def strategy_pages(strategy):
    lines = [f"**Description**\n{_safe(strategy.get('objective')) or 'No description provided.'}"]
    if strategy.get("steps"):
        lines.append("**Execution**\n" + "\n".join(f"{i}. {_safe(step)}" for i, step in enumerate(strategy["steps"], 1)))
    if strategy.get("assignments"):
        assignments = []
        for assignment in strategy["assignments"]:
            parts = [_safe(assignment.get(key)) for key in ("role", "player", "agent", "starting_position", "responsibilities", "utility")]
            assignments.append(" · ".join(part for part in parts if part))
        lines.append("**Assignments**\n" + "\n".join(part for part in assignments if part))
    if strategy.get("utility"):
        lines.append("**Utility**\n" + _safe(strategy["utility"]))
    if strategy.get("links"):
        lines.append("**Reference links**\n" + "\n".join(_safe(link) for link in strategy["links"]))
    diagram = strategy.get("diagram") or {}
    if diagram.get("markers") or diagram.get("image_url") or diagram.get("image_asset"):
        lines.append("**Tactical diagram**\nAvailable with this strategy.")
    return _chunks("\n\n".join(lines))


class PlaybookView(discord.ui.View):
    """Browse maps, sides and paged strategies without mutating storage."""

    def __init__(self, service, maps, owner_id, initial_map=None, diagram_renderer=None):
        super().__init__(timeout=600)
        self.service = service
        self.owner_id = owner_id
        self.maps = list(dict.fromkeys(name for name in maps if name))
        self.map_name = initial_map
        self.side = None
        self.strategy_id = None
        self.page = 0
        self.list_page = 0
        self.map_page = 0
        self.message = None
        self.diagram_renderer = diagram_renderer
        self.refresh()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run /playbook to open your own menu.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        message = "The playbook could not be opened. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def refresh(self):
        self.clear_items()
        map_pages = max(1, math.ceil(len(self.maps) / 25))
        self.map_page = min(self.map_page, map_pages - 1)
        maps = self.maps[self.map_page * 25:(self.map_page + 1) * 25]
        if maps:
            select = discord.ui.Select(placeholder="Choose a map", row=0,
                options=[discord.SelectOption(label=name[:100], value=str(self.map_page * 25 + index), default=name == self.map_name)
                         for index, name in enumerate(maps)])

            async def choose_map(interaction, selector=select):
                self.map_name, self.side, self.strategy_id = self.maps[int(selector.values[0])], None, None
                self.list_page = self.page = 0
                await self.update_message(interaction)
            select.callback = choose_map
            self.add_item(select)
        if map_pages > 1:
            for label, delta in (("Previous maps", -1), ("Next maps", 1)):
                button = discord.ui.Button(label=label, row=1, disabled=not 0 <= self.map_page + delta < map_pages)
                async def change_map_page(interaction, change=delta):
                    self.map_page += change
                    await self.update_message(interaction)
                button.callback = change_map_page
                self.add_item(button)
        if not self.map_name:
            return
        strategies = self.service.list(self.map_name)
        side_options = [("Attack", "attack"), ("Defence", "defence")]
        if any(s["side"] == "legacy" for s in strategies):
            side_options.append(("Legacy", "legacy"))
        for label, side in side_options:
            count = sum(s["side"] == side for s in strategies)
            button = discord.ui.Button(label=f"{label} ({count})", row=2,
                style=discord.ButtonStyle.primary if self.side == side else discord.ButtonStyle.secondary)
            async def choose_side(interaction, selected=side):
                self.side, self.strategy_id = selected, None
                self.list_page = self.page = 0
                await self.update_message(interaction)
            button.callback = choose_side
            self.add_item(button)
        filtered = [s for s in strategies if s["side"] == self.side]
        pages = max(1, math.ceil(len(filtered) / 25))
        self.list_page = min(self.list_page, pages - 1)
        if filtered:
            select = discord.ui.Select(placeholder="Choose a strategy", row=3,
                options=[discord.SelectOption(label=s["name"][:100], value=s["id"], default=s["id"] == self.strategy_id)
                         for s in filtered[self.list_page * 25:(self.list_page + 1) * 25]])
            async def choose_strategy(interaction, selector=select):
                self.strategy_id, self.page = selector.values[0], 0
                await self.update_message(interaction)
            select.callback = choose_strategy
            self.add_item(select)
        if pages > 1:
            for label, delta in (("Previous strategies", -1), ("Next strategies", 1)):
                button = discord.ui.Button(label=label, row=4, disabled=not 0 <= self.list_page + delta < pages)
                async def change_list_page(interaction, change=delta):
                    self.list_page += change
                    self.strategy_id = None
                    await self.update_message(interaction)
                button.callback = change_list_page
                self.add_item(button)
        if self.strategy_id:
            strategy = self.service.get(self.strategy_id)
            page_count = len(strategy_pages(strategy)) if strategy else 1
            for label, delta in (("Previous page", -1), ("Next page", 1)):
                button = discord.ui.Button(label=label, row=1, disabled=not 0 <= self.page + delta < page_count)
                async def change_page(interaction, change=delta):
                    self.page += change
                    await self.update_message(interaction)
                button.callback = change_page
                self.add_item(button)
        if self.strategy_id:
            button = discord.ui.Button(label="Back to list", row=1)
            async def back(interaction):
                self.strategy_id = None
                self.page = 0
                await self.update_message(interaction)
            button.callback = back
            self.add_item(button)

    def embed(self):
        if not self.map_name:
            return discord.Embed(title="Playbook", description="Choose a map to browse strategies.", colour=0xF0636A)
        if self.strategy_id:
            strategy = self.service.get(self.strategy_id)
            if strategy:
                pages = strategy_pages(strategy)
                self.page = min(self.page, len(pages) - 1)
                embed = discord.Embed(title=_safe(strategy["name"])[:256], description=pages[self.page], colour=0xF0636A)
                embed.set_author(name=f"{strategy['map']} · {strategy['side'].title()}"[:256])
                embed.set_footer(text=f"Page {self.page + 1}/{len(pages)}")
                diagram = strategy.get("diagram") or {}
                if diagram.get("image_url"):
                    try:
                        embed.set_image(url=_url(diagram["image_url"]))
                    except PlaybookDataError:
                        pass
                return embed
        strategies = self.service.list(self.map_name)
        counts = {side: sum(s["side"] == side for s in strategies) for side in ("attack", "defence", "legacy")}
        lines = [f"Attack: **{counts['attack']}** · Defence: **{counts['defence']}**"]
        if counts["legacy"]:
            lines.append(f"Legacy: **{counts['legacy']}**")
        lines.append(f"Total: **{len(strategies)}**")
        lines.append("Choose a side, then select a strategy." if self.side else "Choose a side to see its strategies.")
        if self.side:
            names = [s["name"] for s in strategies if s["side"] == self.side]
            start = self.list_page * 25
            lines.append("\n".join(f"• {_safe(name)}" for name in names[start:start + 25]) or "No strategies on this side yet.")
        else:
            for side, label in (("attack", "Attack"), ("defence", "Defence"), ("legacy", "Legacy")):
                names = [s["name"] for s in strategies if s["side"] == side]
                if names:
                    extra = f" (+{len(names) - 10} more)" if len(names) > 10 else ""
                    lines.append(f"**{label}:** " + ", ".join(_safe(name) for name in names[:10]) + extra)
        return discord.Embed(title=f"Playbook · {_safe(self.map_name)}"[:256], description="\n".join(lines)[:4096], colour=0xF0636A)

    async def update_message(self, interaction):
        try:
            self.refresh()
            embed = self.embed()
            strategy = self.service.get(self.strategy_id) if self.strategy_id else None
            diagram = (strategy or {}).get("diagram") or {}
            if strategy and (diagram.get("markers") or diagram.get("image_asset")) and self.diagram_renderer:
                await interaction.response.defer()
                try:
                    image = await self.diagram_renderer(strategy)
                except Exception:
                    image = None
                attachments = [image] if image else []
                if image:
                    embed.set_image(url=f"attachment://{image.filename}")
                await interaction.edit_original_response(embed=embed, view=self, attachments=attachments,
                                                         allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.edit_message(embed=embed, view=self, attachments=[],
                                                        allowed_mentions=discord.AllowedMentions.none())
        except PlaybookDataError as exc:
            if interaction.response.is_done():
                await interaction.followup.send(f"Playbook unavailable: {_safe(exc)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"Playbook unavailable: {_safe(exc)}", ephemeral=True)
