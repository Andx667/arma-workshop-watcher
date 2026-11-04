import asyncio

import discord

from bot import steam
from bot.poller import OUTAGE_NOTICE_SECONDS, Poller
from bot.store import Store


class FakeChannel:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    async def send(self, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append(kwargs)


class FakeClient:
    def __init__(self, channels):
        self.channels = channels

    async def wait_until_ready(self):
        pass

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


def details(mod_id=1, updated=100, found=True):
    return steam.ModDetails(mod_id, "Cool Mod", updated, None, found)


def test_notifies_once_per_update(tmp_path, monkeypatch):
    async def scenario():
        channel = FakeChannel()
        state = {"details": details(updated=100)}

        async def fake_details(session, ids):
            return {i: state["details"] for i in ids}

        async def fake_changelog(session, mod_id):
            return steam.Changelog("today", "notes")

        monkeypatch.setattr(steam, "fetch_details", fake_details)
        monkeypatch.setattr(steam, "fetch_changelog", fake_changelog)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.set_channel(10, 99)
        await store.add_mod(10, 1, "Cool Mod", 100)
        poller = Poller(FakeClient({99: channel}), store, None, 10, tmp_path / "hb")

        await poller.run_once()
        assert channel.sent == []  # baseline, nothing new

        state["details"] = details(updated=200)
        await poller.run_once()
        await poller.run_once()
        assert len(channel.sent) == 1
        assert channel.sent[0]["embed"].description == "notes"
        assert (tmp_path / "hb").exists()
        await store.close()

    asyncio.run(scenario())


def test_update_shows_size_change(tmp_path, monkeypatch):
    async def scenario():
        channel = FakeChannel()
        state = {"details": steam.ModDetails(1, "Cool Mod", 200, None, True, 300 * 1024**2)}

        async def fake_details(session, ids):
            return {i: state["details"] for i in ids}

        async def fake_changelog(session, mod_id):
            return None

        monkeypatch.setattr(steam, "fetch_details", fake_details)
        monkeypatch.setattr(steam, "fetch_changelog", fake_changelog)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.set_channel(10, 99)
        await store.add_mod(10, 1, "Cool Mod", 100)  # size unknown, e.g. watched before sizes were stored
        poller = Poller(FakeClient({99: channel}), store, None, 10, tmp_path / "hb")

        await poller.run_once()
        assert channel.sent[0]["embed"].fields[0].value == "300.0 MB"

        state["details"] = steam.ModDetails(1, "Cool Mod", 300, None, True, int(1.5 * 1024**3))
        await poller.run_once()
        assert channel.sent[1]["embed"].fields[0].name == "Size"
        assert channel.sent[1]["embed"].fields[0].value == "1.5 GB (+1.2 GB)"
        await store.close()

    asyncio.run(scenario())


def test_unknown_size_is_backfilled_without_update(tmp_path, monkeypatch):
    async def scenario():
        channel = FakeChannel()
        state = {"details": steam.ModDetails(1, "Cool Mod", 100, None, True, 300 * 1024**2)}

        async def fake_details(session, ids):
            return {i: state["details"] for i in ids}

        async def fake_changelog(session, mod_id):
            return None

        monkeypatch.setattr(steam, "fetch_details", fake_details)
        monkeypatch.setattr(steam, "fetch_changelog", fake_changelog)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.set_channel(10, 99)
        await store.add_mod(10, 1, "Cool Mod", 100)  # watched before sizes were stored
        poller = Poller(FakeClient({99: channel}), store, None, 10, tmp_path / "hb")

        await poller.run_once()
        assert channel.sent == []
        assert (await store.subscriptions(1))[0].file_size == 300 * 1024**2

        state["details"] = steam.ModDetails(1, "Cool Mod", 200, None, True, 400 * 1024**2)
        await poller.run_once()
        assert channel.sent[0]["embed"].fields[0].value == "400.0 MB (+100.0 MB)"
        await store.close()

    asyncio.run(scenario())


def test_old_database_gets_size_column(tmp_path):
    async def scenario():
        import aiosqlite

        path = tmp_path / "db.sqlite"
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "CREATE TABLE mods (guild_id INTEGER NOT NULL, mod_id INTEGER NOT NULL, title TEXT NOT NULL, "
                "last_updated INTEGER NOT NULL, missing INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, mod_id))"
            )
            await db.execute("INSERT INTO mods (guild_id, mod_id, title, last_updated) VALUES (10, 1, 'Old', 100)")
            await db.commit()

        store = await Store.open(path)
        assert (await store.subscriptions(1))[0].file_size is None
        await store.record_update(10, 1, "Old", 200, 42)
        assert (await store.subscriptions(1))[0].file_size == 42
        await store.close()

    asyncio.run(scenario())


def test_transient_failure_retries(tmp_path, monkeypatch):
    async def scenario():
        channel = FakeChannel(fail=RuntimeError("boom"))

        async def fake_details(session, ids):
            return {i: details(updated=200) for i in ids}

        async def fake_changelog(session, mod_id):
            return None

        monkeypatch.setattr(steam, "fetch_details", fake_details)
        monkeypatch.setattr(steam, "fetch_changelog", fake_changelog)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.set_channel(10, 99)
        await store.add_mod(10, 1, "Cool Mod", 100)
        poller = Poller(FakeClient({99: channel}), store, None, 10, tmp_path / "hb")

        await poller.run_once()
        assert (await store.subscriptions(1))[0].last_updated == 100  # not advanced

        channel.fail = None
        await poller.run_once()
        assert len(channel.sent) == 1
        assert (await store.subscriptions(1))[0].last_updated == 200
        await store.close()

    asyncio.run(scenario())


def test_missing_mod_warns_once(tmp_path, monkeypatch):
    async def scenario():
        channel = FakeChannel()

        async def fake_details(session, ids):
            return {i: details(found=False) for i in ids}

        monkeypatch.setattr(steam, "fetch_details", fake_details)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.set_channel(10, 99)
        await store.add_mod(10, 1, "Cool Mod", 100)
        poller = Poller(FakeClient({99: channel}), store, None, 10, tmp_path / "hb")

        await poller.run_once()
        await poller.run_once()
        assert len(channel.sent) == 1
        assert "can no longer be found" in channel.sent[0]["content"]
        await store.close()

    asyncio.run(scenario())


def test_forbidden_is_swallowed_and_advances(tmp_path, monkeypatch):
    async def scenario():
        class Resp:
            status = 403
            reason = "Forbidden"

        channel = FakeChannel(fail=discord.Forbidden(Resp(), "no"))

        async def fake_details(session, ids):
            return {i: details(updated=200) for i in ids}

        async def fake_changelog(session, mod_id):
            return None

        monkeypatch.setattr(steam, "fetch_details", fake_details)
        monkeypatch.setattr(steam, "fetch_changelog", fake_changelog)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.set_channel(10, 99)
        await store.add_mod(10, 1, "Cool Mod", 100)
        poller = Poller(FakeClient({99: channel}), store, None, 10, tmp_path / "hb")

        await poller.run_once()
        assert (await store.subscriptions(1))[0].last_updated == 200
        await store.close()

    asyncio.run(scenario())


def test_update_names_source_presets(tmp_path, monkeypatch):
    async def scenario():
        channel = FakeChannel()

        async def fake_details(session, ids):
            return {i: details(updated=200) for i in ids}

        async def fake_changelog(session, mod_id):
            return None

        monkeypatch.setattr(steam, "fetch_details", fake_details)
        monkeypatch.setattr(steam, "fetch_changelog", fake_changelog)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.set_channel(10, 99)
        await store.add_mod(10, 1, "Cool Mod", 100)
        await store.add_source(10, 1, "Preset B")
        await store.add_source(10, 1, "Preset A")
        await store.add_source(10, 1, "Preset A")
        poller = Poller(FakeClient({99: channel}), store, None, 10, tmp_path / "hb")

        await poller.run_once()
        embed = channel.sent[0]["embed"]
        assert embed.fields[0].name == "Presets"
        assert embed.fields[0].value == "Preset A, Preset B"
        await store.remove_mod(10, 1)
        assert await store.sources(10, 1) == []
        await store.close()

    asyncio.run(scenario())


def test_steam_outage_backs_off_and_notifies_once(tmp_path, monkeypatch):
    async def scenario():
        watching, idle = FakeChannel(), FakeChannel()
        state = {"down": True}

        async def fake_details(session, ids):
            if state["down"]:
                raise steam.SteamError("Steam API returned HTTP 503")
            return {i: details(updated=100) for i in ids}

        monkeypatch.setattr(steam, "fetch_details", fake_details)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.set_channel(10, 99)
        await store.add_mod(10, 1, "Cool Mod", 100)
        await store.set_channel(20, 98)  # watches nothing, so gets no notices
        poller = Poller(FakeClient({99: watching, 98: idle}), store, None, 10, tmp_path / "hb")

        intervals = []
        for _ in range(5):
            await poller.run_once()
            intervals.append(poller.interval_minutes)
        assert intervals == [10, 20, 40, 60, 60]
        assert (tmp_path / "hb").read_text() == "60"
        assert watching.sent == []  # not down long enough yet

        poller.failing_since -= OUTAGE_NOTICE_SECONDS
        await poller.run_once()
        await poller.run_once()
        assert len(watching.sent) == 1
        assert "Steam has not answered" in watching.sent[0]["content"]

        state["down"] = False
        await poller.run_once()
        assert len(watching.sent) == 2
        assert "answering again" in watching.sent[1]["content"]
        assert poller.interval_minutes == 10
        assert (tmp_path / "hb").read_text() == "10"
        assert idle.sent == []
        await store.close()

    asyncio.run(scenario())


def test_short_steam_hiccup_is_silent(tmp_path, monkeypatch):
    async def scenario():
        channel = FakeChannel()
        state = {"down": True}

        async def fake_details(session, ids):
            if state["down"]:
                raise steam.SteamError("timeout")
            return {i: details(updated=100) for i in ids}

        monkeypatch.setattr(steam, "fetch_details", fake_details)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.set_channel(10, 99)
        await store.add_mod(10, 1, "Cool Mod", 100)
        poller = Poller(FakeClient({99: channel}), store, None, 10, tmp_path / "hb")

        await poller.run_once()
        state["down"] = False
        await poller.run_once()
        assert channel.sent == []
        assert poller.failures == 0 and poller.interval_minutes == 10
        await store.close()

    asyncio.run(scenario())
