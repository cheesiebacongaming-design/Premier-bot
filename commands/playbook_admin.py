"""Private administrator workflows for creating and editing strategies."""

import copy
import json
import math

import discord

from commands.playbook_views import _safe, strategy_pages
from services.playbook_service import PlaybookDataError, validate_draft


def _can_manage(interaction, owner_id):
    return bool(interaction.guild and interaction.user.id == owner_id and interaction.permissions.administrator)


class DraftModal(discord.ui.Modal):
    def __init__(self, view, kind):
        super().__init__(title={"basic": "Strategy details", "roles": "Player assignments",
                                "links": "Utility and links", "diagram": "Tactical diagram"}[kind], timeout=600)
        self.owner_view, self.kind = view, kind
        self.draft_token = view.draft_token
        draft = view.draft
        if kind == "basic":
            fields = [("name", "Strategy name", draft.get("name", ""), 80, True, False),
                      ("objective", "Objective", draft.get("objective", ""), 4000, False, True),
                      ("steps", "Steps: one per line", "\n".join(draft.get("steps", [])), 4000, False, True)]
        elif kind == "roles":
            fields = [("assignments", "role | player | agent | position | task | utility",
                       "\n".join(" | ".join(a.get(k, "") for k in ("role", "player", "agent", "starting_position", "responsibilities", "utility"))
                                 for a in draft.get("assignments", [])), 4000, False, True)]
        elif kind == "links":
            fields = [("utility", "Utility instructions", draft.get("utility", ""), 2000, False, True),
                      ("links", "Links: one URL per line", "\n".join(draft.get("links", [])), 4000, False, True)]
        else:
            diagram = draft.get("diagram") or {}
            fields = [("image_url", "Optional tactical image URL", diagram.get("image_url", ""), 500, False, False),
                      ("markers", "Markers JSON (empty removes markers)",
                       json.dumps(diagram.get("markers", []), ensure_ascii=False, separators=(",", ":")) if diagram.get("markers") else "",
                       4000, False, True)]
        self.inputs = {}
        for key, label, default, maximum, required, multiline in fields:
            if len(default) > maximum:
                raise PlaybookDataError(f"Existing {label} exceeds the edit form limit; data was not changed")
            placeholder = None
            if key == "markers":
                placeholder = '[{"kind":"player","x":0.25,"y":0.4,"label":"1"}]'
            elif key == "assignments":
                placeholder = "Entry | Player | Jett | A main | Enter site | Dash"
            field = discord.ui.TextInput(label=label[:45], default=default, placeholder=placeholder, required=required, max_length=maximum,
                                         style=discord.TextStyle.paragraph if multiline else discord.TextStyle.short)
            self.inputs[key] = field
            self.add_item(field)

    async def on_submit(self, interaction):
        view = self.owner_view
        if (not _can_manage(interaction, view.owner_id) or view.is_finished()
                or view.draft is None or view.draft_token != self.draft_token):
            return await interaction.response.send_message("This editing session has expired or you lack administrator permission.", ephemeral=True)
        candidate = copy.deepcopy(view.draft)
        try:
            if self.kind == "basic":
                candidate.update(name=self.inputs["name"].value, objective=self.inputs["objective"].value,
                                 steps=[line.strip() for line in self.inputs["steps"].value.splitlines() if line.strip()])
            elif self.kind == "roles":
                assignments = []
                for line in self.inputs["assignments"].value.splitlines():
                    if not line.strip():
                        continue
                    parts = [part.strip() for part in line.split("|")]
                    if len(parts) > 6:
                        raise PlaybookDataError("Each assignment needs at most six fields separated by |")
                    keys = ("role", "player", "agent", "starting_position", "responsibilities", "utility")
                    assignments.append(dict(zip(keys, parts)))
                candidate["assignments"] = assignments
            elif self.kind == "links":
                candidate.update(utility=self.inputs["utility"].value,
                                 links=[line.strip() for line in self.inputs["links"].value.splitlines() if line.strip()])
            else:
                raw = self.inputs["markers"].value.strip()
                markers = json.loads(raw) if raw else []
                url = self.inputs["image_url"].value.strip()
                candidate["diagram"] = {"markers": markers, "image_url": url} if markers or url else None
            view.draft = validate_draft(candidate, allow_legacy=view.original and view.original["side"] == "legacy")
        except (PlaybookDataError, ValueError) as exc:
            return await interaction.response.send_message(f"Could not update draft: {_safe(exc)}", ephemeral=True)
        view.draft_token += 1
        view.preview_page = 0
        view.refresh()
        await interaction.response.edit_message(embed=view.embed(), view=view, allowed_mentions=discord.AllowedMentions.none())


class PlaybookAdminView(discord.ui.View):
    def __init__(self, service, maps, owner_id, initial_map=None, initial_notes="", initial_links="", lineup_loader=None):
        super().__init__(timeout=900)
        self.service, self.maps, self.owner_id = service, maps, owner_id
        self.map_name, self.side = initial_map, None
        self.map_page = self.list_page = self.preview_page = 0
        self.draft = self.original = self.selected = None
        self.draft_token = 0
        self.initial_notes, self.initial_links = initial_notes, initial_links
        self.lineup_loader = lineup_loader
        self.confirm_delete = False
        self.message = None
        self.refresh()

    async def interaction_check(self, interaction):
        if not _can_manage(interaction, self.owner_id):
            await interaction.response.send_message("Only the administrator who opened this menu can manage strategies.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        text = f"Playbook action failed: {_safe(error)}" if isinstance(error, PlaybookDataError) else "Playbook action failed. Try again."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def update(self, interaction):
        self.refresh()
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    def add_action(self, label, row, callback, style=discord.ButtonStyle.secondary, disabled=False):
        button = discord.ui.Button(label=label, row=row, style=style, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    def refresh(self):
        self.clear_items()
        if self.draft is not None:
            for label, kind in (("Details", "basic"), ("Assignments", "roles"), ("Utility & links", "links"), ("Diagram", "diagram")):
                async def open_modal(interaction, modal_kind=kind):
                    try:
                        await interaction.response.send_modal(DraftModal(self, modal_kind))
                    except PlaybookDataError as exc:
                        await interaction.response.send_message(_safe(exc), ephemeral=True)
                self.add_action(label, 0, open_modal)
            if self.lineup_loader:
                async def import_lineup(interaction):
                    if self.draft.get("assignments"):
                        return await interaction.response.send_message("Clear draft assignments before importing a lineup.", ephemeral=True)
                    token = self.draft_token
                    await interaction.response.defer()
                    try:
                        assignments = await self.lineup_loader(self.map_name, interaction.guild)
                    except PlaybookDataError as exc:
                        return await interaction.followup.send(_safe(exc), ephemeral=True)
                    except Exception:
                        return await interaction.followup.send("Could not read this map's lineup.", ephemeral=True)
                    if not assignments:
                        return await interaction.followup.send("No agent lineup is set for this map.", ephemeral=True)
                    if len(assignments) > 5:
                        return await interaction.followup.send("The lineup has more than five players; edit it before importing.", ephemeral=True)
                    if self.draft is None or self.draft_token != token or self.draft.get("assignments"):
                        return await interaction.followup.send("The draft changed while importing; no assignments were replaced.", ephemeral=True)
                    self.draft["assignments"] = assignments
                    self.draft_token += 1
                    self.refresh()
                    await interaction.edit_original_response(embed=self.embed(), view=self,
                                                             allowed_mentions=discord.AllowedMentions.none())
                self.add_action("Import lineup", 0, import_lineup)
            async def save(interaction):
                try:
                    if self.original:
                        saved = self.service.update(self.original["id"], self.original["revision"], self.draft)
                    else:
                        saved = self.service.create(self.draft)
                except PlaybookDataError as exc:
                    return await interaction.response.send_message(_safe(exc), ephemeral=True)
                self.selected, self.draft, self.original = saved["id"], None, None
                self.draft_token += 1
                await self.update(interaction)
            async def cancel(interaction):
                self.draft = self.original = None
                self.draft_token += 1
                await self.update(interaction)
            self.add_action("Save strategy", 1, save, discord.ButtonStyle.success)
            self.add_action("Cancel draft", 1, cancel, discord.ButtonStyle.danger)
            page_count = len(strategy_pages(self.draft))
            for label, delta in (("Previous page", -1), ("Next page", 1)):
                async def change_preview(interaction, change=delta):
                    self.preview_page += change
                    await self.update(interaction)
                self.add_action(label, 1, change_preview, disabled=not 0 <= self.preview_page + delta < page_count)
            return
        if self.confirm_delete:
            async def delete(interaction):
                try:
                    self.service.delete(self.original["id"], self.original["revision"])
                except PlaybookDataError as exc:
                    return await interaction.response.send_message(_safe(exc), ephemeral=True)
                self.confirm_delete, self.selected, self.original = False, None, None
                await self.update(interaction)
            async def cancel_delete(interaction):
                self.confirm_delete, self.original = False, None
                await self.update(interaction)
            self.add_action("Confirm delete", 0, delete, discord.ButtonStyle.danger)
            self.add_action("Cancel", 0, cancel_delete)
            return
        map_pages = max(1, math.ceil(len(self.maps) / 25))
        self.map_page = min(self.map_page, map_pages - 1)
        page = self.maps[self.map_page * 25:(self.map_page + 1) * 25]
        if page:
            select = discord.ui.Select(placeholder="Choose a map", row=0,
                options=[discord.SelectOption(label=name[:100], value=str(self.map_page * 25 + index), default=name == self.map_name)
                         for index, name in enumerate(page)])
            async def select_map(interaction, selector=select):
                self.map_name, self.side, self.selected = self.maps[int(selector.values[0])], None, None
                self.list_page = 0
                await self.update(interaction)
            select.callback = select_map
            self.add_item(select)
        if map_pages > 1:
            for label, delta in (("Previous maps", -1), ("Next maps", 1)):
                async def change_maps(interaction, change=delta):
                    self.map_page += change
                    await self.update(interaction)
                self.add_action(label, 4, change_maps, disabled=not 0 <= self.map_page + delta < map_pages)
        if not self.map_name:
            return
        strategies = self.service.list(self.map_name)
        sides = [("Attack", "attack"), ("Defence", "defence")]
        if any(item["side"] == "legacy" for item in strategies):
            sides.append(("Legacy", "legacy"))
        for label, side in sides:
            async def select_side(interaction, selected_side=side):
                self.side, self.selected, self.list_page = selected_side, None, 0
                await self.update(interaction)
            self.add_action(label, 1, select_side,
                            discord.ButtonStyle.primary if self.side == side else discord.ButtonStyle.secondary)
        if not self.side:
            return
        filtered = [item for item in strategies if item["side"] == self.side]
        pages = max(1, math.ceil(len(filtered) / 25))
        self.list_page = min(self.list_page, pages - 1)
        if filtered:
            select = discord.ui.Select(placeholder="Choose an existing strategy", row=2,
                options=[discord.SelectOption(label=s["name"][:100], value=s["id"], default=s["id"] == self.selected)
                         for s in filtered[self.list_page * 25:(self.list_page + 1) * 25]])
            async def choose(interaction, selector=select):
                self.selected = selector.values[0]
                await self.update(interaction)
            select.callback = choose
            self.add_item(select)
        async def new(interaction):
            self.original = None
            self.draft_token += 1
            self.draft = {"name": "", "map": self.map_name, "side": self.side, "objective": self.initial_notes,
                          "steps": [], "assignments": [], "utility": "", "links": self.initial_links.replace(',', ' ').split(), "diagram": None}
            self.preview_page = 0
            await self.update(interaction)
        async def edit(interaction):
            original = self.service.get(self.selected)
            if not original:
                return await interaction.response.send_message("Strategy was deleted; reopen the list.", ephemeral=True)
            self.original, self.draft = original, copy.deepcopy(original)
            self.draft_token += 1
            self.preview_page = 0
            await self.update(interaction)
        async def delete_prompt(interaction):
            self.original = self.service.get(self.selected)
            if not self.original:
                return await interaction.response.send_message("Strategy was deleted; reopen the list.", ephemeral=True)
            self.confirm_delete = True
            await self.update(interaction)
        self.add_action("New strategy", 3, new, discord.ButtonStyle.success, disabled=self.side == "legacy")
        self.add_action("Edit", 3, edit, disabled=not self.selected)
        self.add_action("Delete", 3, delete_prompt, discord.ButtonStyle.danger, disabled=not self.selected)
        if pages > 1:
            for label, delta in (("Previous", -1), ("Next", 1)):
                async def change_list(interaction, change=delta):
                    self.list_page += change
                    self.selected = None
                    await self.update(interaction)
                self.add_action(label, 3, change_list, disabled=not 0 <= self.list_page + delta < pages)

    def embed(self):
        if self.confirm_delete:
            return discord.Embed(title="Delete strategy?", description=f"Permanently delete **{_safe(self.original['name'])}**?", colour=0xF0636A)
        if self.draft is not None:
            pages = strategy_pages(self.draft)
            self.preview_page = min(self.preview_page, len(pages) - 1)
            embed = discord.Embed(title=f"Draft · {_safe(self.draft.get('name') or 'Untitled')}"[:256],
                                  description=pages[self.preview_page], colour=0xF0636A)
            embed.set_footer(text=f"{self.map_name} · {self.side.title()} · Preview {self.preview_page + 1}/{len(pages)} · Save to confirm")
            image_url = (self.draft.get("diagram") or {}).get("image_url")
            if image_url:
                embed.set_image(url=image_url)
            return embed
        title = f"Manage playbooks · {self.map_name}" if self.map_name else "Manage playbooks"
        text = "Choose a map and side, then create or select a strategy."
        if self.selected:
            current = self.service.get(self.selected)
            if current:
                text = f"Selected: **{_safe(current['name'])}**\nChoose Edit or Delete."
        return discord.Embed(title=title[:256], description=text, colour=0xF0636A)
