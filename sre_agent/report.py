"""Print the health report from the post-mortem archive (M6).

    python -m sre_agent.report
"""
from __future__ import annotations

import sys
from pathlib import Path

from sre_agent.config import Config
from sre_agent.integrations.postmortems import SqlitePostMortemStore, health_report


def main(argv: list[str] | None = None) -> int:
    cfg = Config.load(argv[0] if argv else None)
    store = SqlitePostMortemStore(Path(cfg.data_dir) / "postmortems.db")
    sys.stdout.reconfigure(encoding="utf-8")
    print(health_report(store.all()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
