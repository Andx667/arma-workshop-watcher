import logging
import time
from pathlib import Path

import discord
from discord.ext import tasks

from . import steam
from .notifier import build_update_embed, missing_message, steam_back_message, steam_down_message
from .store import Store

log = logging.getLogger(__name__)

# While Steam keeps failing, the poll interval doubles up to this cap.
MAX_BACKOFF_MINUTES = 60
# Servers are told once Steam has been failing this long, and again when it recovers.
OUTAGE_NOTICE_SECONDS = 3600


def backoff_minutes(base: int, failures: int) -> int:
    if failures == 0:
        return base
    return min(base * 2 ** min(failures - 1, 10), max(base, MAX_BACKOFF_MINUTES))


class Poller:
    def __init__(self, client: discord.Client, store: Store, session, interval_minutes: int, heartbeat: Path):
        self.client = client
        self.store = store
        self.session = session
        self.heartbeat = heartbeat
        self.base_interval = interval_minutes
        self.interval_minutes = interval_minutes
        self.failures = 0
        self.failing_since: float | None = None
        self.outage_notified = False
        self.last_success: float | None = None
        self.last_error: str | None = None
        self._loop = tasks.loop(minutes=interval_minutes)(self._tick)

    @property
    def next_poll(self) -> float | None:
        when = self._loop.next_iteration
        return when.timestamp() if when else None

    def start(self) -> None:
        self._loop.start()

    def stop(self) -> None:
        self._loop.cancel()

    async def _tick(self) -> None:
        await self.client.wait_until_ready()
        try:
            await self.run_once()
        except Exception:
            log.exception("Poll failed")

    async def run_once(self) -> None:
        ids = await self.store.all_mod_ids()
        try:
            details = await steam.fetch_details(self.session, ids) if ids else {}
        except steam.SteamError as exc:
            await self._steam_failed(exc)
        else:
            await self._steam_recovered()
            for mod_id in ids:
                await self._handle_mod(details[mod_id])
            self.last_success = time.time()
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        # The healthcheck reads the current interval, so a long backoff does not look like a hang.
        self.heartbeat.write_text(str(self.interval_minutes))

    async def _steam_failed(self, exc: steam.SteamError) -> None:
        now = time.time()
        self.failures += 1
        self.last_error = str(exc)
        if self.failing_since is None:
            self.failing_since = now
        self._set_interval(backoff_minutes(self.base_interval, self.failures))
        log.warning(
            "Skipping this poll (%d failure(s) in a row, next try in %d min): %s",
            self.failures,
            self.interval_minutes,
            exc,
        )
        if not self.outage_notified and now - self.failing_since >= OUTAGE_NOTICE_SECONDS:
            self.outage_notified = True
            await self._broadcast(steam_down_message(int(self.failing_since)))

    async def _steam_recovered(self) -> None:
        if not self.failures:
            return
        log.info("Steam answered again after %d failed poll(s)", self.failures)
        if self.outage_notified:
            await self._broadcast(steam_back_message())
        self.failures = 0
        self.failing_since = None
        self.outage_notified = False
        self._set_interval(self.base_interval)

    def _set_interval(self, minutes: int) -> None:
        if minutes != self.interval_minutes:
            self.interval_minutes = minutes
            self._loop.change_interval(minutes=minutes)

    async def _broadcast(self, content: str) -> None:
        for guild_id, channel_id in await self.store.notification_channels():
            try:
                await self._send(channel_id, guild_id, content=content)
            except Exception:
                log.exception("Could not post Steam status to guild %s", guild_id)

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
