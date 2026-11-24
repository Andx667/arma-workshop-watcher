import pytest

from bot import steam


@pytest.fixture(autouse=True)
def no_workshop_pages(monkeypatch):
    """Tests never read real Workshop pages; mods have no required items unless a test says otherwise."""

    async def fake_required_items(session, mod_id):
        return []

    monkeypatch.setattr(steam, "fetch_required_items", fake_required_items)
