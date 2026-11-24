from dataclasses import dataclass, field

import aiohttp

from . import steam
from .store import Store


@dataclass
class DependencyResult:
    added: list[steam.ModDetails] = field(default_factory=list)
    """Dependencies that were not watched before and are now."""
    not_found: list[int] = field(default_factory=list)
    """Required items Steam does not know (removed or hidden)."""
    unreadable: list[int] = field(default_factory=list)
    """Mods whose Workshop page could not be read, so their dependencies are unknown."""


async def watch_dependencies(
    store: Store,
    session: aiohttp.ClientSession,
    guild_id: int,
    roots: list[int],
    graph: steam.DependencyGraph | None = None,
) -> DependencyResult:
    """Watch every (transitive) required item of the roots, with the current state as baseline.

    Raises steam.SteamError if the details of new dependencies cannot be fetched."""
    if graph is None:
        graph = await steam.resolve_dependencies(session, roots)
    result = DependencyResult(unreadable=list(graph.unreadable))
    edges = graph.edges()
    watched = await store.guild_mod_ids(guild_id)
    new_ids = list(dict.fromkeys(dep for dep, _ in edges if dep not in watched))
    if new_ids:
        details = await steam.fetch_details(session, new_ids)
        for mod_id in new_ids:
            d = details[mod_id]
            if not d.found:
                result.not_found.append(mod_id)
                continue
            await store.add_mod(guild_id, mod_id, d.title, d.time_updated, d.file_size or None)
            watched.add(mod_id)
            result.added.append(d)
    for dep, parent in edges:
        if dep in watched and parent in watched:
            await store.add_dependency(guild_id, dep, parent)
    return result


def describe_result(result: DependencyResult, limit: int = 1500) -> str:
    """Lines for a command reply; empty when there is nothing to say."""
    lines = []
    if result.added:
        names = ", ".join(f"**{d.title}**" for d in result.added)
        if len(names) > limit:
            names = names[:limit].rsplit(", ", 1)[0] + ", …"
        lines.append(f"Also watching {len(result.added)} required mod(s): {names}")
    if result.not_found:
        lines.append(f"Required but not found on Steam: {', '.join(map(str, result.not_found[:20]))}")
    if result.unreadable:
        lines.append(f"Could not read the Workshop page of {len(result.unreadable)} mod(s), their dependencies may be missing.")
    return "\n".join(lines)
