"""Docker HEALTHCHECK: healthy while the poller has finished a poll recently."""

import sys
import time

from .config import Config


def main() -> int:
    config = Config.from_env(require_token=False)
    try:
        age = time.time() - config.heartbeat_path.stat().st_mtime
        content = config.heartbeat_path.read_text().strip()
    except OSError:
        return 1
    # The poller writes its current interval, which grows while Steam is failing.
    interval = int(content) if content.isdigit() else config.poll_interval_minutes
    max_age = interval * 60 * 3 + 120
    return 0 if age < max_age else 1


if __name__ == "__main__":
    sys.exit(main())
