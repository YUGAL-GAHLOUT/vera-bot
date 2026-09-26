#!/usr/bin/env python3
"""
Build submission.jsonl — one composed message per canonical test pair.

Usage:
    python generate_submission.py            # regenerates dataset/expanded if missing
    python generate_submission.py --out submission.jsonl
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from composer import compose

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
EXPANDED = DATASET / "expanded"


def _load(sub: str, key: str) -> dict:
    return {d[key]: d for d in (json.loads(f.read_text()) for f in sorted((EXPANDED / sub).glob("*.json")))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "submission.jsonl"))
    args = ap.parse_args()

    if not (EXPANDED / "test_pairs.json").exists():
        subprocess.run([sys.executable, "generate_dataset.py", "--out", "./expanded"], cwd=DATASET, check=True)

    categories = _load("categories", "slug")
    merchants = _load("merchants", "merchant_id")
    customers = _load("customers", "customer_id")
    triggers = _load("triggers", "id")
    pairs = json.loads((EXPANDED / "test_pairs.json").read_text())["pairs"]

    with open(args.out, "w", encoding="utf-8") as fh:
        for p in pairs:
            merchant = merchants[p["merchant_id"]]
            msg = compose(categories[merchant["category_slug"]], merchant, triggers[p["trigger_id"]],
                          customers.get(p["customer_id"]) if p.get("customer_id") else None)
            row = {"test_id": p["test_id"], "body": msg["body"], "cta": msg["cta"], "send_as": msg["send_as"],
                   "suppression_key": msg["suppression_key"], "rationale": msg["rationale"]}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Wrote {len(pairs)} lines to {args.out}")


if __name__ == "__main__":
    main()
