import os
import time

from bot import healthcheck


def test_healthcheck_follows_backoff_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLL_INTERVAL_MINUTES", "10")
    heartbeat = tmp_path / "heartbeat"
    hour_ago = time.time() - 3600

    assert healthcheck.main() == 1  # no heartbeat yet

    heartbeat.write_text("10")
    os.utime(heartbeat, (hour_ago, hour_ago))
    assert healthcheck.main() == 1  # three missed 10 minute polls

    heartbeat.write_text("60")
    os.utime(heartbeat, (hour_ago, hour_ago))
    assert healthcheck.main() == 0  # backing off, the next poll is not overdue

    heartbeat.write_text("")  # written by an older version
    assert healthcheck.main() == 0
