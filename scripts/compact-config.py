from __future__ import annotations

import argparse
from pathlib import Path

from workbot.configuration import compact_config_file


def main() -> int:
    ap = argparse.ArgumentParser(description="Conservatively remove WorkBot config values that equal runtime defaults and rewrite compactly.")
    ap.add_argument("path", nargs="?", default="config/local.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    path = Path(args.path)
    if not path.exists():
        if not args.quiet:
            print(f"config not found: {path}")
        return 0
    removed = compact_config_file(path)
    if not args.quiet:
        print(f"Compacted {path}: removed {removed} redundant default entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
