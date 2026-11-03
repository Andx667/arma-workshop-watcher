import logging
from pathlib import Path

import discord
from discord.ext import tasks

from . import steam
from .notifier import build_update_embed, missing_message
from .store import Store

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, client: discord.Client, store: Store, session, interval_minutes: int, heartbeat: Path):
        self.client = client
        self.store = store
        self.session = session
        self.heartbeat = heartbeat
        self._loop = tasks.loop(minutes=interval_minutes)(self._tick)

    def start(self) -> None:
        self._loop.start()

    def stop(self) -> None:
        self._loop.cancel()

    async def _tick(self) -> None:
        await self.client.wait_until_ready()
        try:
            await self.run_once()
        except steam.SteamError as exc:
            log.warning("Skipping this poll: %s", exc)
        except Exception:
            log.exception("Poll failed")

    async def run_once(self) -> None:
        ids = await self.store.all_mod_ids()
        if ids:
            details = await steam.fetch_details(self.session, ids)
            for mod_id in ids:
                await self._handle_mod(details[mod_id])
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat.touch()

    async def _handle_mod(self, details: steam.ModDetails) -> None:
        subs = await self.store.subscriptions(details.id)

        if not details.found:
            for sub in subs:
                if sub.missing:
                    continue
                try:
                    await self._send(sub.channel_id, sub.guild_id, content=missing_message(sub.title, details.id))
                except Exception:
                    log.exception("Could not warn guild %s about missing mod %s", sub.guild_id, details.id)
                    return
            await self.store.set_missing(details.id, True)
            return

        due = [s for s in subs if details.time_updated > s.last_updated]
        if not due:
            if any(s.missing for s in subs):
                await self.store.set_missing(details.id, False)
            return

        log.info("Mod %s (%s) updated on Steam, notifying %d guild(s)", details.id, details.title, len(due))
        changelog = await steam.fetch_changelog(self.session, details.id)
        for sub in due:
            presets = await self.store.sources(sub.guild_id, details.id)
            embed = build_update_embed(details, changelog, presets, sub.file_size)
            try:
                await self._send(sub.channel_id, sub.guild_id, embed=embed)
            except Exception:
                # Transient failure: leave last_updated alone so the next poll retries.
                log.exception("Could not notify guild %s about mod %s", sub.guild_id, details.id)
                continue
            await self.store.record_update(
                sub.guild_id, details.id, details.title, details.time_updated, details.file_size or None
            )

    async def _send(self, channel_id: int | None, guild_id: int, **kwargs) -> None:
        """Permanent problems (no channel set, channel gone, no permission) are logged and swallowed;
        anything else propagates so the caller can retry on the next poll."""
        if channel_id is None:
            log.info("No notification channel set for guild %s, skipping message", guild_id)
            return
        channel = self.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                log.warning("Channel %s is not accessible", channel_id)
                return
        try:
            await channel.send(**kwargs)
        except discord.Forbidden:
            log.warning("Missing permission to post in channel %s", channel_id)
