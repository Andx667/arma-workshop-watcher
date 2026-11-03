import asyncio

import discord

from bot import steam
from bot.commands import build_status_embed, channel_health
from bot.poller import Poller
from bot.store import Store, WatchedMod


class FakeChannel:
    def __init__(self, channel_id, perms):
        self.mention = f"<#{channel_id}>"
        self.perms = perms

    def permissions_for(self, member):
        return self.perms


class FakeGuild:
    me = object()

    def __init__(self, channels):
        self.channels = channels

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


ALL_PERMS = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)


def test_channel_health():
    guild = FakeGuild({1: FakeChannel(1, ALL_PERMS), 2: FakeChannel(2, discord.Permissions(view_channel=True))})
    assert channel_health(guild, None)[0] is False
    assert channel_health(guild, 1) == (True, "✅ <#1>")
    assert channel_health(guild, 2) == (False, "⚠️ <#2>: I am missing Send Messages, Embed Links")
    ok, text = channel_health(guild, 3)
    assert not ok and "no longer exists" in text


def test_status_embed_reports_problems(tmp_path, monkeypatch):
    async def scenario():
        state = {"down": True}

        async def fake_details(session, ids):
            if state["down"]:
                raise steam.SteamError("Steam API returned HTTP 503")
            return {i: steam.ModDetails(i, "Cool Mod", 100, None, True) for i in ids}

        monkeypatch.setattr(steam, "fetch_details", fake_details)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.add_mod(10, 1, "Cool Mod", 100)
        poller = Poller(None, store, None, 10, tmp_path / "hb")
        guild = FakeGuild({99: FakeChannel(99, ALL_PERMS)})

        await poller.run_once()
        embed = build_status_embed(poller, guild, 99, 1, [WatchedMod(2, "Gone Mod")])
        fields = {f.name: f.value for f in embed.fields}
        assert embed.colour == discord.Colour.orange()
        assert fields["Last successful check"] == "None since the bot started"
        assert "HTTP 503" in fields["Steam"]
        assert fields["Notification channel"] == "✅ <#99>"
        assert "Gone Mod" in fields["Removed or hidden on Steam (1)"]

        state["down"] = False
        await poller.run_once()
        embed = build_status_embed(poller, guild, 99, 1, [])
        fields = {f.name: f.value for f in embed.fields}
        assert embed.colour == discord.Colour.green()
        assert fields["Last successful check"].startswith("<t:")
        assert fields["Steam"] == "✅ Answering"
        assert fields["Next check"] == "Not scheduled"  # loop is not running in tests
        await store.close()

    asyncio.run(scenario())


def test_missing_mods_lists_only_flagged(tmp_path):
    async def scenario():
        store = await Store.open(tmp_path / "db.sqlite")
        await store.add_mod(10, 1, "Here", 100)
        await store.add_mod(10, 2, "Gone", 100)
        await store.add_mod(20, 2, "Gone", 100)
        await store.set_missing(2, True)
        assert await store.missing_mods(10) == [WatchedMod(2, "Gone")]
        await store.close()

    asyncio.run(scenario())
