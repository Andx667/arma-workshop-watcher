from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER
);
CREATE TABLE IF NOT EXISTS mods (
    guild_id     INTEGER NOT NULL,
    mod_id       INTEGER NOT NULL,
    title        TEXT    NOT NULL,
    last_updated INTEGER NOT NULL,
    missing      INTEGER NOT NULL DEFAULT 0,
    file_size    INTEGER,
    PRIMARY KEY (guild_id, mod_id)
);
CREATE TABLE IF NOT EXISTS mod_sources (
    guild_id INTEGER NOT NULL,
    mod_id   INTEGER NOT NULL,
    preset   TEXT    NOT NULL,
    PRIMARY KEY (guild_id, mod_id, preset)
);
"""


@dataclass(frozen=True)
class Subscription:
    guild_id: int
    channel_id: int | None
    title: str
    last_updated: int
    missing: bool
    file_size: int | None


@dataclass(frozen=True)
class WatchedMod:
    mod_id: int
    title: str


class Store:
    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    @classmethod
    async def open(cls, path: Path) -> "Store":
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        async with db.execute("PRAGMA table_info(mods)") as cur:
            columns = {r["name"] async for r in cur}
        # Databases created before file sizes were tracked.
        if "file_size" not in columns:
            await db.execute("ALTER TABLE mods ADD COLUMN file_size INTEGER")
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    async def set_channel(self, guild_id: int, channel_id: int) -> None:
        await self._db.execute(
            "INSERT INTO guilds (guild_id, channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id",
            (guild_id, channel_id),
        )
        await self._db.commit()

    async def get_channel(self, guild_id: int) -> int | None:
        async with self._db.execute("SELECT channel_id FROM guilds WHERE guild_id = ?", (guild_id,)) as cur:
            row = await cur.fetchone()
        return row["channel_id"] if row else None

    async def add_mod(
        self, guild_id: int, mod_id: int, title: str, last_updated: int, file_size: int | None = None
    ) -> bool:
        cur = await self._db.execute(
            "INSERT OR IGNORE INTO mods (guild_id, mod_id, title, last_updated, file_size) VALUES (?, ?, ?, ?, ?)",
            (guild_id, mod_id, title, last_updated, file_size),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def add_source(self, guild_id: int, mod_id: int, preset: str) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO mod_sources (guild_id, mod_id, preset) VALUES (?, ?, ?)",
            (guild_id, mod_id, preset),
        )
        await self._db.commit()

    async def sources(self, guild_id: int, mod_id: int) -> list[str]:
        async with self._db.execute(
            "SELECT preset FROM mod_sources WHERE guild_id = ? AND mod_id = ? ORDER BY preset COLLATE NOCASE",
            (guild_id, mod_id),
        ) as cur:
            return [r["preset"] async for r in cur]

    async def remove_mod(self, guild_id: int, mod_id: int) -> bool:
        await self._db.execute("DELETE FROM mod_sources WHERE guild_id = ? AND mod_id = ?", (guild_id, mod_id))
        cur = await self._db.execute("DELETE FROM mods WHERE guild_id = ? AND mod_id = ?", (guild_id, mod_id))
        await self._db.commit()
        return cur.rowcount > 0

    async def list_mods(self, guild_id: int) -> list[WatchedMod]:
        async with self._db.execute(
            "SELECT mod_id, title FROM mods WHERE guild_id = ? ORDER BY title COLLATE NOCASE", (guild_id,)
        ) as cur:
            return [WatchedMod(r["mod_id"], r["title"]) async for r in cur]

    async def all_mod_ids(self) -> list[int]:
        async with self._db.execute("SELECT DISTINCT mod_id FROM mods") as cur:
            return [r["mod_id"] async for r in cur]

    async def notification_channels(self) -> list[tuple[int, int]]:
        """(guild_id, channel_id) of every server that has a channel set and watches at least one mod."""
        async with self._db.execute(
            "SELECT g.guild_id, g.channel_id FROM guilds g WHERE g.channel_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM mods m WHERE m.guild_id = g.guild_id)"
        ) as cur:
            return [(r["guild_id"], r["channel_id"]) async for r in cur]

    async def subscriptions(self, mod_id: int) -> list[Subscription]:
        async with self._db.execute(
            "SELECT m.guild_id, g.channel_id, m.title, m.last_updated, m.missing, m.file_size "
            "FROM mods m LEFT JOIN guilds g ON g.guild_id = m.guild_id WHERE m.mod_id = ?",
            (mod_id,),
        ) as cur:
            return [
                Subscription(
                    r["guild_id"], r["channel_id"], r["title"], r["last_updated"], bool(r["missing"]), r["file_size"]
                )
                async for r in cur
            ]

    async def record_update(
        self, guild_id: int, mod_id: int, title: str, last_updated: int, file_size: int | None
    ) -> None:
        await self._db.execute(
            "UPDATE mods SET title = ?, last_updated = ?, file_size = ?, missing = 0 WHERE guild_id = ? AND mod_id = ?",
            (title, last_updated, file_size, guild_id, mod_id),
        )
        await self._db.commit()

    async def set_missing(self, mod_id: int, missing: bool) -> None:
        await self._db.execute("UPDATE mods SET missing = ? WHERE mod_id = ?", (int(missing), mod_id))
        await self._db.commit()
