from bot.steam import parse_changelog, parse_preset_ids, parse_preset_name, parse_workshop_id

CHANGELOG_HTML = """
<div class="workshopAnnouncement">
  <div class="headline"><a href="#">12 Mar @ 4:05pm</a></div>
  <p id="123">Fixed the thing.<br>Added another thing.</p>
</div>
<div class="workshopAnnouncement">
  <div class="headline">1 Jan @ 1:00am</div>
  <p>Old entry</p>
</div>
"""


def test_parse_id_variants():
    assert parse_workshop_id("463939057") == 463939057
    assert parse_workshop_id(" 463939057 ") == 463939057
    assert parse_workshop_id("https://steamcommunity.com/sharedfiles/filedetails/?id=463939057") == 463939057
    assert parse_workshop_id("https://steamcommunity.com/workshop/filedetails/?l=german&id=463939057&x=1") == 463939057
    assert parse_workshop_id("steam://url/CommunityFilePage/463939057") == 463939057
    assert parse_workshop_id("hello") is None


def test_parse_preset_ids():
    html = """<?xml version="1.0" encoding="utf-8"?><html><body><table>
    <tr data-type="ModContainer"><td data-type="DisplayName">A</td>
      <td><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=893328083" data-type="Link">x</a></td></tr>
    <tr data-type="ModContainer"><td data-type="DisplayName">Local</td><td>Local</td></tr>
    <tr data-type="ModContainer"><td data-type="DisplayName">A again</td>
      <td><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=893328083" data-type="Link">x</a></td></tr>
    <tr data-type="ModContainer"><td data-type="DisplayName">B</td>
      <td><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=1135534951" data-type="Link">x</a></td></tr>
    </table></body></html>"""
    assert parse_preset_ids(html) == [893328083, 1135534951]


def test_parse_changelog_takes_newest_entry():
    result = parse_changelog(CHANGELOG_HTML)
    assert result is not None
    assert result.headline == "12 Mar @ 4:05pm"
    assert result.text == "Fixed the thing.\nAdded another thing."


def test_parse_changelog_unknown_layout():
    assert parse_changelog("<html><body>nothing</body></html>") is None


def test_parse_preset_name():
    assert parse_preset_name('<head><meta name="arma:PresetName" content="[TTT] A" /></head>') == "[TTT] A"
    assert parse_preset_name("<html></html>") is None


def test_describe_size():
    from bot.notifier import describe_size, format_size

    assert format_size(512) == "512 B"
    assert format_size(1536) == "1.5 KB"
    assert format_size(3 * 1024**3) == "3.0 GB"
    assert describe_size(100 * 1024**2, None) == "100.0 MB"
    assert describe_size(100 * 1024**2, 100 * 1024**2) == "100.0 MB"
    assert describe_size(90 * 1024**2, 100 * 1024**2) == "90.0 MB (-10.0 MB)"
