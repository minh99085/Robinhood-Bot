#!/usr/bin/env python3
"""Record a manual production-readiness attestation (Phase 11).

Human-authored only (bot reviewers rejected). Not legal/tax advice. Does not
enable production. Unknown flags (e.g. --enable-production / --submit) fail.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _production_review_common import default_db


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record a manual production-readiness attestation")
    ap.add_argument("--type", required=True,
                    choices=["jurisdiction", "account-readiness", "venue-terms"])
    ap.add_argument("--reviewer-id", required=True)
    ap.add_argument("--venue", default="kalshi")
    ap.add_argument("--confirm", required=True)
    ap.add_argument("--account-identifier", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.production_review import ProductionReviewConfig
    from engine.production_review.jurisdiction import create_attestation
    from engine.storage import Store
    store = Store(Path(args.db or default_db()))
    cfg = ProductionReviewConfig.from_env()
    att, errs = create_attestation(
        kind=args.type, reviewer_id=args.reviewer_id, venue=args.venue,
        confirmation_text=args.confirm, expiry_hours=cfg.approval_expiry_hours,
        account_identifier=args.account_identifier)
    if not errs:
        store.add_production_jurisdiction_attestation(att.record())
    out = {"attestation_id": att.attestation_id, "type": args.type, "status": att.status,
           "errors": errs, "no_execution": True}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"attestation {att.attestation_id} type={args.type} status={att.status}")
        for e in errs:
            print(f"  error: {e}")
        print("Manual attestation recorded. This is not legal/tax advice. No production enabled.")
    return 0 if not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())
