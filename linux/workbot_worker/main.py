from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .daemon import WorkerDaemon
from .relay import relay
from .lifecycle import ensure_daemon, stop_daemon


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["daemon", "relay", "ensure", "stop"])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = Path(os.environ.get("WORKBOT_HOME", str(Path.home() / ".workbot")))
    if args.mode == "daemon":
        asyncio.run(WorkerDaemon(root).serve())
    elif args.mode == "relay":
        asyncio.run(relay(root))
    elif args.mode == "ensure":
        import json
        print(json.dumps(ensure_daemon(root), ensure_ascii=False))
    else:
        import json
        print(json.dumps(stop_daemon(root), ensure_ascii=False))


if __name__ == "__main__":
    cli()
