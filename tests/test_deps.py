import asyncio

from bot import steam
from bot.deps import describe_result, watch_dependencies
from bot.steam import DependencyGraph, parse_required_items, resolve_dependencies
from bot.store import Store

WORKSHOP_HTML = """
<div class="requiredItemsContainer" id="RequiredItems">
  <a href="https://steamcommunity.com/workshop/filedetails/?id=450814997" target="_blank">
    <div class="requiredItem">CBA_A3</div></a>
  <a href="https://steamcommunity.com/workshop/filedetails/?id=450814997"><div class="requiredItem">dup</div></a>
</div>
<div class="panel"><a href="https://www.facebook.com/ACE3Mod">Facebook</a>
  <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=123">not a dependency</a></div>
"""


def test_parse_required_items_only_reads_the_required_block():
    assert parse_required_items(WORKSHOP_HTML) == [450814997]
    assert parse_required_items("<html><body>no deps</body></html>") == []


def fake_pages(monkeypatch, pages, calls=None):
    async def fake_required_items(session, mod_id):
        if calls is not None:
            calls.append(mod_id)
        return pages.get(mod_id, [])

    monkeypatch.setattr(steam, "fetch_required_items", fake_required_items)


def test_resolve_follows_transitive_and_cycles(monkeypatch):
    calls = []
    # 1 -> 2 -> 3 -> 1 (cycle), 1 -> 4, page of 4 unreadable
    fake_pages(monkeypatch, {1: [2, 4], 2: [3], 3: [1], 4: None}, calls)
    graph = asyncio.run(resolve_dependencies(None, [1]))
    assert graph.requires == {1: [2, 4], 2: [3], 3: [1]}
    assert graph.unreadable == [4]
    assert sorted(calls) == [1, 2, 3, 4]  # each page read once


def test_resolve_respects_lookup_limit(monkeypatch):
    fake_pages(monkeypatch, {1: [2, 3], 2: [4]})
    graph = asyncio.run(resolve_dependencies(None, [1], max_lookups=2))
    assert graph.requires == {1: [2, 3], 2: [4]}
    assert graph.unreadable == [3, 4]  # 3 hit the limit, 4 was found but never read


def fake_details(monkeypatch, missing=()):
    async def details(session, ids):
        return {i: steam.ModDetails(i, f"Mod {i}", 100, None, i not in missing) for i in ids}

    monkeypatch.setattr(steam, "fetch_details", details)


def test_watch_dependencies_adds_and_links(tmp_path, monkeypatch):
    async def scenario():
        fake_details(monkeypatch, missing={5})
        store = await Store.open(tmp_path / "db.sqlite")
        await store.add_mod(10, 1, "Mod 1", 100)
        await store.add_mod(10, 3, "Mod 3", 100)  # already watched dependency
        graph = DependencyGraph({1: [2, 3, 5], 2: [3]}, [7])
        result = await watch_dependencies(store, None, 10, [1], graph)

        assert [d.id for d in result.added] == [2]
        assert result.not_found == [5]
        assert result.unreadable == [7]
        assert await store.guild_mod_ids(10) == {1, 2, 3}
        assert await store.required_by(10, 3) == ["Mod 1", "Mod 2"]
        assert await store.guild_mod_ids(20) == set()  # other servers untouched

        text = describe_result(result)
        assert "**Mod 2**" in text and "5" in text and "1 mod(s)" in text

        await store.remove_mod(10, 1)
        assert await store.required_by(10, 2) == []
        assert await store.required_by(10, 3) == ["Mod 2"]
        await store.close()

    asyncio.run(scenario())


def test_nothing_to_add_is_silent(tmp_path, monkeypatch):
    async def scenario():
        fake_details(monkeypatch)
        store = await Store.open(tmp_path / "db.sqlite")
        await store.add_mod(10, 1, "Mod 1", 100)
        result = await watch_dependencies(store, None, 10, [1], DependencyGraph({1: []}, []))
        assert describe_result(result) == ""
        await store.close()

    asyncio.run(scenario())
