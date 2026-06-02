#!/usr/bin/env python3
"""Run the micro-live conformance harness (Phase 9). Proves the safe-disabled
defaults and that forbidden behaviors are impossible. No order is submitted."""

from __future__ import annotations

import argparse
import json

from _micro_live_common import default_db  # noqa: F401  (sys.path bootstrap)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Micro-live conformance (safe-disabled)")
    ap.add_argument("--with-network-trap", action="store_true")
    ap.add_argument("--fail-on-warning", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from engine.micro_live import MicroLiveConfig
    from engine.micro_live.conformance import MicroLiveConformanceHarness
    traps = {}
    if args.with_network_trap:
        traps["with_network_trap"] = True
        traps["network_used"] = False
    if args.fail_on_warning:
        traps["fail_on_warning"] = True
    res = MicroLiveConformanceHarness(MicroLiveConfig.from_env()).run(traps)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"conformance: {res['status']} ({res['pass_count']}/{res['test_count']})")
        for c in res["checks"]:
            if c["status"] != "PASS":
                print(f"  [{c['status']}] {c['check_name']} - {c['reason']}")
        print("No order was submitted. Real execution remains DISABLED by default.")
    return 0 if res["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
