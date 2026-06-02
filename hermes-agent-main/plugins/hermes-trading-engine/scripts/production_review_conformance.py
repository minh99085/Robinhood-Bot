#!/usr/bin/env python3
"""Run the MOCK-ONLY production conformance suite (Phase 11). Zero real network
calls; proves production execution is impossible."""

from __future__ import annotations

import argparse
import json

from _production_review_common import default_db  # noqa: F401 (sys.path bootstrap)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mock-only production conformance")
    ap.add_argument("--fail-on-warning", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from engine.production_review import ProductionReviewConfig
    from engine.production_review import production_conformance as pc
    traps = {"fail_on_warning": True} if args.fail_on_warning else {}
    run = pc.run(ProductionReviewConfig.from_env(), traps=traps)
    out = {"status": run.status, "mock_only": run.mock_only,
           "real_network_calls": run.real_network_calls,
           "production_order_calls": run.production_order_calls,
           "production_cancel_calls": run.production_cancel_calls,
           "production_signer_calls": run.production_signer_calls}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"production conformance: {run.status} (mock_only={run.mock_only}, "
              f"real_network_calls={run.real_network_calls})")
        for c in run.checks:
            if c.status != "PASS":
                print(f"  [{c.status}] {c.check_name} - {c.reason}")
        print("No real production network calls. Production execution remains UNIMPLEMENTED.")
    return 0 if run.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
