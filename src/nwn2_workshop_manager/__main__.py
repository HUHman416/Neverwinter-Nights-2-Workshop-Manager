from __future__ import annotations

import argparse
import json
import sys

from .core import ConfigStore, ManagerError, apply_best_detection, build_sync_plan, sync
from .ui import run_gui


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely manage NWN2 EE Steam Workshop mods")
    parser.add_argument("--dry-run", action="store_true", help="print the planned changes as JSON")
    parser.add_argument("--sync", action="store_true", help="synchronize without opening the GUI")
    parser.add_argument("--version", action="store_true", help="print the version")
    parser.add_argument("--check-updates", action="store_true", help="check GitHub over verified HTTPS without opening the GUI")
    args = parser.parse_args()
    if args.check_updates:
        from .core import VERSION
        from .updater import check_release

        print(json.dumps(check_release(VERSION)))
        return 0
    if args.version:
        from .core import VERSION

        print(VERSION)
        return 0
    if not args.dry_run and not args.sync:
        run_gui()
        return 0

    store = ConfigStore()
    settings = store.load()
    if apply_best_detection(settings):
        store.save(settings)
    try:
        if args.dry_run:
            plan = build_sync_plan(settings, store)
            print(
                json.dumps(
                    {
                        "actions": [action.__dict__ for action in plan.actions],
                        "unchanged": plan.unchanged,
                        "conflicts": [conflict.__dict__ for conflict in plan.conflicts],
                        "warnings": plan.warnings,
                    },
                    indent=2,
                )
            )
        else:
            result = sync(settings, store)
            print(json.dumps(result.__dict__, indent=2))
        return 0
    except ManagerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
