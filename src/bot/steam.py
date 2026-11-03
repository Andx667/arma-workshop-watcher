import logging
import re
from dataclasses import dataclass

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

API_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
CHANGELOG_URL = "https://steamcommunity.com/sharedfiles/filedetails/changelog/{id}"
WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={id}"

BATCH_SIZE = 100
TIMEOUT = aiohttp.ClientTimeout(total=30)

_URL_ID_RE = re.compile(r"(?:[?&]id=|CommunityFilePage/)(\d+)")


class SteamError(Exception):
    pass


@dataclass(frozen=True)
class ModDetails:
    id: int
    title: str
    time_updated: int
    preview_url: str | None
    found: bool
    file_size: int = 0


@dataclass(frozen=True)
class Changelog:
    headline: str
    text: str


def workshop_url(mod_id: int) -> str:
    return WORKSHOP_URL.format(id=mod_id)


def parse_workshop_id(text: str) -> int | None:
    """Accepts a bare id, a Workshop URL or a steam://url/CommunityFilePage/ link."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    match = _URL_ID_RE.search(text)
    return int(match.group(1)) if match else None


def parse_preset_ids(html: str) -> list[int]:
    """Workshop ids of an Arma 3 Launcher preset export, in file order. Local mods have no link and are skipped."""
    ids: list[int] = []
    for link in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        mod_id = parse_workshop_id(link["href"])
        if mod_id is not None and mod_id not in ids:
            ids.append(mod_id)
    return ids


def parse_preset_name(html: str) -> str | None:
    meta = BeautifulSoup(html, "html.parser").find("meta", attrs={"name": "arma:PresetName"})
    name = meta.get("content", "").strip() if meta else ""
    return name or None


async def fetch_details(session: aiohttp.ClientSession, ids: list[int]) -> dict[int, ModDetails]:
    """Query the public Steam API (no key needed). Ids Steam does not return are reported as not found."""
    result: dict[int, ModDetails] = {}
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start : start + BATCH_SIZE]
        form = {"itemcount": str(len(chunk))}
        for i, mod_id in enumerate(chunk):
            form[f"publishedfileids[{i}]"] = str(mod_id)
        try:
            async with session.post(API_URL, data=form, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    raise SteamError(f"Steam API returned HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise SteamError(f"Steam API request failed: {exc!r}") from exc

        for item in payload.get("response", {}).get("publishedfiledetails", []):
            try:
                mod_id = int(item["publishedfileid"])
            except (KeyError, ValueError):
                continue
            found = item.get("result") == 1
            result[mod_id] = ModDetails(
                id=mod_id,
                title=item.get("title") or str(mod_id),
                time_updated=int(item.get("time_updated") or 0),
                preview_url=item.get("preview_url") or None,
                found=found,
                file_size=int(item.get("file_size") or 0),
            )
    for mod_id in ids:
        result.setdefault(mod_id, ModDetails(mod_id, str(mod_id), 0, None, False))
    return result


def parse_changelog(html: str) -> Changelog | None:
    """Newest entry of the Workshop changelog page, or None if the layout is not recognised."""
    entry = BeautifulSoup(html, "html.parser").select_one(".workshopAnnouncement")
    if entry is None:
        return None
    body = entry.find("p")
    text = body.get_text("\n", strip=True) if body else ""
    if not text:
        return None
    headline = entry.select_one(".headline")
    return Changelog(headline.get_text(" ", strip=True) if headline else "", text)


async def fetch_changelog(session: aiohttp.ClientSession, mod_id: int) -> Changelog | None:
    """Best effort: any failure just means the notification goes out without a change note."""
    try:
        async with session.get(
            CHANGELOG_URL.format(id=mod_id),
            timeout=TIMEOUT,
            headers={"Accept-Language": "en"},
            cookies={"Steam_Language": "english"},
        ) as resp:
            if resp.status != 200:
                log.warning("Changelog for %s returned HTTP %s", mod_id, resp.status)
                return None
            return parse_changelog(await resp.text())
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.warning("Changelog for %s failed: %r", mod_id, exc)
        return None
