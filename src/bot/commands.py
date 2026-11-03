from pathlib import Path

import aiohttp
import discord
from discord import app_commands

from . import steam
from .store import Store


MAX_PRESET_BYTES = 2_000_000


class WatchGroup(app_commands.Group):
    def __init__(self, store: Store, session: aiohttp.ClientSession):
        super().__init__(
            name="watch",
            description="Manage the watched Workshop mods",
            guild_only=True,
            default_permissions=discord.Permissions(manage_guild=True),
        )
        self.store = store
        self.session = session

    @app_commands.command(description="Start watching a Workshop mod (link or ID)")
    @app_commands.describe(mod="Workshop link or numeric ID")
    async def add(self, interaction: discord.Interaction, mod: str):
        await interaction.response.defer(ephemeral=True)
        mod_id = steam.parse_workshop_id(mod)
        if mod_id is None:
            await interaction.followup.send("That is not a Workshop link or ID.")
            return
        try:
            details = (await steam.fetch_details(self.session, [mod_id]))[mod_id]
        except steam.SteamError:
            await interaction.followup.send("Steam did not answer, try again in a minute.")
            return
        if not details.found:
            await interaction.followup.send(f"No Workshop item with ID {mod_id} found.")
            return
        # Baseline is the current state, so adding a mod never triggers a notification.
        added = await self.store.add_mod(
            interaction.guild_id, mod_id, details.title, details.time_updated, details.file_size or None
        )
        if not added:
            await interaction.followup.send(f"Already watching **{details.title}**.")
            return
        message = f"Now watching **{details.title}** ({steam.workshop_url(mod_id)})."
        if await self.store.get_channel(interaction.guild_id) is None:
            message += "\nNo notification channel is set yet, use `/watch channel`."
        await interaction.followup.send(message)

    @app_commands.command(name="import", description="Watch all Workshop mods of an Arma 3 Launcher preset (.html)")
    @app_commands.describe(preset="Preset HTML file exported from the Arma 3 Launcher")
    async def import_(self, interaction: discord.Interaction, preset: discord.Attachment):
        await interaction.response.defer(ephemeral=True)
        if preset.size > MAX_PRESET_BYTES:
            await interaction.followup.send("That file is too large for a preset.")
            return
        html = (await preset.read()).decode("utf-8", errors="replace")
        ids = steam.parse_preset_ids(html)
        preset_name = steam.parse_preset_name(html) or Path(preset.filename).stem
        if not ids:
            await interaction.followup.send("No Workshop links found in that file.")
            return
        try:
            details = await steam.fetch_details(self.session, ids)
        except steam.SteamError:
            await interaction.followup.send("Steam did not answer, try again in a minute.")
            return
        added, existing, missing = 0, 0, []
        for mod_id in ids:
            d = details[mod_id]
            if not d.found:
                missing.append(mod_id)
            else:
                if await self.store.add_mod(interaction.guild_id, mod_id, d.title, d.time_updated, d.file_size or None):
                    added += 1
                else:
                    existing += 1
                await self.store.add_source(interaction.guild_id, mod_id, preset_name)
        message = f"Imported {added} new mods, {existing} were already watched."
        if missing:
            message += f"\nNot found on Steam: {', '.join(map(str, missing[:20]))}" + (" …" if len(missing) > 20 else "")
        if await self.store.get_channel(interaction.guild_id) is None:
            message += "\nNo notification channel is set yet, use `/watch channel`."
        await interaction.followup.send(message)

    @app_commands.command(description="Stop watching a Workshop mod")
    @app_commands.describe(mod="Mod to remove")
    async def remove(self, interaction: discord.Interaction, mod: str):
        mod_id = steam.parse_workshop_id(mod)
        removed = mod_id is not None and await self.store.remove_mod(interaction.guild_id, mod_id)
        text = f"Stopped watching {mod_id}." if removed else "That mod is not on the watch list."
        await interaction.response.send_message(text, ephemeral=True)

    @remove.autocomplete("mod")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str):
        mods = await self.store.list_mods(interaction.guild_id)
        needle = current.lower()
        return [
            app_commands.Choice(name=f"{m.title} ({m.mod_id})"[:100], value=str(m.mod_id))
            for m in mods
            if needle in m.title.lower() or needle in str(m.mod_id)
        ][:25]

    @app_commands.command(name="list", description="Show the watched mods")
    async def list_(self, interaction: discord.Interaction):
        mods = await self.store.list_mods(interaction.guild_id)
        channel_id = await self.store.get_channel(interaction.guild_id)
        if not mods:
            await interaction.response.send_message("No mods watched yet. Use `/watch add`.", ephemeral=True)
            return
        lines = [f"- [{m.title}]({steam.workshop_url(m.mod_id)}) (`{m.mod_id}`)" for m in mods]
        description = "\n".join(lines)
        if len(description) > 3900:
            description = description[:3900].rsplit("\n", 1)[0] + "\n…"
        embed = discord.Embed(title=f"Watching {len(mods)} mods", description=description)
        embed.add_field(name="Notification channel", value="not set" if channel_id is None else f"<#{channel_id}>")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Set the channel for update notifications")
    @app_commands.describe(channel="Channel to post in")
    async def channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True
            )
            return
        await self.store.set_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(f"Updates will be posted in {channel.mention}.", ephemeral=True)
