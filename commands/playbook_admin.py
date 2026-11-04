"""Private administrator workflows for creating and editing strategies."""

import copy
import asyncio
import io
import math

import discord

from commands.playbook_views import _safe, strategy_pages
from services.playbook_service import PlaybookDataError
from services.playbook_images import MAX_IMAGE_BYTES, validate_image


def _can_manage(interaction, owner_id):
    return bool(interaction.guild and interaction.user.id == owner_id and interaction.permissions.administrator)


class DraftModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Playbook details", timeout=600)
        self.owner_view = view
        self.draft_token = view.draft_token
        draft = view.draft
        objective = draft.get("objective", "")
        fields = [("name", "Title", draft.get("name", ""), 80, True, False),
                  ("objective", "Short description", objective, max(500, len(objective)), True, True)]
        self.inputs = {}
        for key, label, default, maximum, required, multiline in fields:
            if len(default) > min(maximum, 4000):
                raise PlaybookDataError(f"Existing {label} exceeds the edit form limit; data was not changed")
            field = discord.ui.TextInput(label=label[:45], default=default, required=required, max_length=min(maximum, 4000),
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
            title, description = self.inputs["name"].value.strip(), self.inputs["objective"].value
            if not title or not description.strip():
                raise PlaybookDataError("Enter a title and short description")
            if len(description) > 500 and (not view.original or description != view.original.get("objective")):
                raise PlaybookDataError("The short description must be 500 characters or fewer")
            candidate.update(name=title, objective=description)
            view.draft = candidate
        except PlaybookDataError as exc:
            return await interaction.response.send_message(f"Could not update draft: {_safe(exc)}", ephemeral=True)
        view.draft_token += 1
        view.preview_page = 0
        view.refresh()
        embed, attachments = view.preview_content()
        await interaction.response.edit_message(embed=embed, view=view, attachments=attachments,
                                                allowed_mentions=discord.AllowedMentions.none())


class ImageUploadModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Upload tactical image", timeout=600)
        self.owner_view, self.draft_token = view, view.draft_token
        self.upload = discord.ui.FileUpload(required=True, min_values=1, max_values=1)
        self.add_item(discord.ui.Label(text="Image file", description="PNG, JPEG or WebP, up to 8 MiB and 8 megapixels.",
                                       component=self.upload))

    async def on_submit(self, interaction):
        view = self.owner_view
        if (not _can_manage(interaction, view.owner_id) or view.is_finished()
                or view.draft is None or view.draft_token != self.draft_token):
            return await interaction.response.send_message("This editing session has expired or changed.", ephemeral=True)
        if len(self.upload.values) != 1:
            return await interaction.response.send_message("Choose one image file.", ephemeral=True)
        attachment = self.upload.values[0]
        if attachment.size > MAX_IMAGE_BYTES:
            return await interaction.response.send_message("Image must be at most 8 MiB.", ephemeral=True)
        await interaction.response.defer()
        try:
            data = await attachment.read()
            asset_name = await asyncio.to_thread(validate_image, data)
        except PlaybookDataError:
            return await interaction.followup.send("Invalid image. Use a PNG, JPEG or WebP up to 8 MiB and 8 megapixels.", ephemeral=True)
        except Exception:
            return await interaction.followup.send("Could not download the attachment. Please try again.", ephemeral=True)
        if view.is_finished() or view.draft is None or view.draft_token != self.draft_token:
            return await interaction.followup.send("The draft changed while the image was uploading; nothing was replaced.", ephemeral=True)
        view.pending_image = data
        view.pending_extension = asset_name.rsplit(".", 1)[-1]
        view.draft_token += 1
        view.refresh()
        await view.edit_deferred(interaction)


class PlaybookAdminView(discord.ui.View):
    def __init__(self, service, maps, owner_id, initial_map=None):
        super().__init__(timeout=900)
        self.service, self.maps, self.owner_id = service, maps, owner_id
        self.map_name, self.side = initial_map, None
        self.map_page = self.list_page = self.preview_page = 0
        self.draft = self.original = self.selected = None
        self.draft_token = 0
        self.pending_image = None
        self.pending_extension = "png"
        self.remove_existing_image = False
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
        embed, attachments = self.preview_content()
        await interaction.response.edit_message(embed=embed, view=self, attachments=attachments,
                                                allowed_mentions=discord.AllowedMentions.none())

    async def edit_deferred(self, interaction):
        embed, attachments = self.preview_content()
        await interaction.edit_original_response(embed=embed, view=self, attachments=attachments,
                                                 allowed_mentions=discord.AllowedMentions.none())

    def preview_content(self):
        embed = self.embed()
        image = None
        if self.draft is not None:
            if self.pending_image:
                image = discord.File(io.BytesIO(self.pending_image), filename=f"playbook-preview.{self.pending_extension}")
            else:
                name = (self.draft.get("diagram") or {}).get("image_asset")
                try:
                    path = self.service.image_path(name) if name else None
                    if path and path.is_file():
                        image = discord.File(path, filename="playbook-preview" + path.suffix)
                except (OSError, PlaybookDataError):
                    pass
        if image:
            embed.set_image(url=f"attachment://{image.filename}")
        return embed, [image] if image else []

    def add_action(self, label, row, callback, style=discord.ButtonStyle.secondary, disabled=False):
        button = discord.ui.Button(label=label, row=row, style=style, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    def refresh(self):
        self.clear_items()
        if self.draft is not None:
            async def open_modal(interaction):
                try:
                    await interaction.response.send_modal(DraftModal(self))
                except PlaybookDataError as exc:
                    await interaction.response.send_message(_safe(exc), ephemeral=True)
            self.add_action("Title & description", 0, open_modal)
            async def save(interaction):
                try:
                    if not self.draft.get("objective", "").strip():
                        raise PlaybookDataError("Enter a short description before saving")
                    if self.original:
                        saved = self.service.update_details(self.original["id"], self.original["revision"],
                            self.draft.get("name", ""), self.draft.get("objective", ""),
                            self.pending_image, self.remove_existing_image)
                    else:
                        saved = self.service.create(self.draft, self.pending_image)
                except PlaybookDataError as exc:
                    return await interaction.response.send_message(_safe(exc), ephemeral=True)
                self.selected, self.draft, self.original = saved["id"], None, None
                self.pending_image = None
                self.remove_existing_image = False
                self.draft_token += 1
                await self.update(interaction)
            async def cancel(interaction):
                self.draft = self.original = None
                self.pending_image = None
                self.remove_existing_image = False
                self.draft_token += 1
                await self.update(interaction)
            self.add_action("Save strategy", 1, save, discord.ButtonStyle.success)
            self.add_action("Cancel draft", 1, cancel, discord.ButtonStyle.danger)
            async def upload_image(interaction):
                await interaction.response.send_modal(ImageUploadModal(self))
            async def remove_image(interaction):
                self.pending_image = None
                self.remove_existing_image = True
                diagram = self.draft.get("diagram") or {}
                markers = diagram.get("markers") or []
                self.draft["diagram"] = {"markers": markers, "image_url": "", "image_asset": ""} if markers else None
                self.draft_token += 1
                await self.update(interaction)
            self.add_action("Upload image", 2, upload_image)
            self.add_action("Remove image", 2, remove_image)
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
            self.pending_image = None
            self.remove_existing_image = False
            self.draft_token += 1
            self.draft = {"name": "", "map": self.map_name, "side": self.side, "objective": "",
                          "steps": [], "assignments": [], "utility": "", "links": [], "diagram": None}
            self.preview_page = 0
            await self.update(interaction)
        async def edit(interaction):
            original = self.service.get(self.selected)
            if not original:
                return await interaction.response.send_message("Strategy was deleted; reopen the list.", ephemeral=True)
            self.original, self.draft = original, copy.deepcopy(original)
            self.pending_image = None
            self.remove_existing_image = False
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
