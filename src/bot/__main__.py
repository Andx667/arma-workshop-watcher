import logging

import aiohttp
import discord
from discord import app_commands

from .commands import WatchGroup
from .config import Config
from .poller import Poller
from .store import Store

log = logging.getLogger("bot")


class WatchBot(discord.Client):
    def __init__(self, config: Config):
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.store: Store | None = None
        self.session: aiohttp.ClientSession | None = None
        self.poller: Poller | None = None

    async def setup_hook(self) -> None:
        self.store = await Store.open(self.config.db_path)
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "arma-workshop-watch (+https://github.com/Andx667/arma-workshop-watch)"}
        )
        self.poller = Poller(
            self, self.store, self.session, self.config.poll_interval_minutes, self.config.heartbeat_path
        )
        self.tree.add_command(WatchGroup(self.store, self.session, self.poller))
        synced = await self.tree.sync()
        for cmd in synced:
            subcommands = [o.name for o in cmd.options if isinstance(o, app_commands.AppCommandGroup)]
            log.info("Synced global command /%s %s", cmd.name, " ".join(subcommands))
        self.poller.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s, in %d guilds", self.user, len(self.guilds))

    async def close(self) -> None:
        if self.poller:
            self.poller.stop()
        if self.session:
            await self.session.close()
        if self.store:
            await self.store.close()
        await super().close()


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    WatchBot(config).run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
