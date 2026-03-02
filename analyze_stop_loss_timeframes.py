#!/usr/bin/env python3
"""
Analyze stop-loss timing windows from trades.jsonl.

Focuses on EXIT_STOP_LOSS rows and answers:
- At what T-minus windows (seconds remaining to resolution) do SL hits happen most?
- How long after entry are SL hits happening?
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _fmt_secs(seconds: float) -> str:
    total = int(max(0.0, seconds))
    mins, secs = divmod(total, 60)
    return f"{mins:02d}:{secs:02d}"


def _bucket_label(idx: int, bucket_size: int) -> str:
    lo = idx * bucket_size
    hi = (idx + 1) * bucket_size
    return f"T-{_fmt_secs(hi)} to T-{_fmt_secs(lo)}"


def _print_header(title: str) -> None:
    print(title)
    print("-" * len(title))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find most common stop-loss timing windows."
    )
    parser.add_argument(
        "--file",
        default="trades.jsonl",
        help="Path to trades.jsonl (default: trades.jsonl)",
    )
    parser.add_argument(
        "--bucket-secs",
        type=int,
        default=60,
        help="Bucket size in seconds for T-minus grouping (default: 60)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Top N timeframe buckets to print (default: 10)",
    )
    parser.add_argument(
        "--last-hours",
        type=float,
        default=None,
        help="Optional lookback window in hours (e.g. 24). Default: all data.",
    )
    args = parser.parse_args()

    if args.bucket_secs <= 0:
        print("Error: --bucket-secs must be > 0")
        return 2

    if not os.path.isfile(args.file):
        print(
            f"No trade file found at '{args.file}'. "
            "Run the bot first to generate trades.jsonl."
        )
        return 1

    since: Optional[datetime] = None
    if args.last_hours is not None:
        if args.last_hours <= 0:
            print("Error: --last-hours must be > 0")
            return 2
        since = datetime.now(timezone.utc) - timedelta(hours=args.last_hours)

    total_exits = 0
    total_stop_losses = 0
    missing_tminus = 0
    tminus_buckets: Counter[int] = Counter()
    hold_buckets: Counter[str] = Counter()
    sl_hours_utc: Counter[int] = Counter()

    with open(args.file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = _parse_iso_utc(row.get("timestamp"))
            if since is not None and (ts is None or ts < since):
                continue

            action = row.get("action")
            if action in ("EXIT_STOP_LOSS", "EXIT_RESOLUTION"):
                total_exits += 1

            if action != "EXIT_STOP_LOSS":
                continue

            total_stop_losses += 1
            if ts is not None:
                sl_hours_utc[ts.hour] += 1

            exit_secs_raw = row.get("seconds_remaining")
            exit_secs: Optional[float] = None
            try:
                if exit_secs_raw is not None:
                    exit_secs = max(0.0, float(exit_secs_raw))
            except (TypeError, ValueError):
                exit_secs = None

            if exit_secs is None:
                missing_tminus += 1
            else:
                idx = int(exit_secs // args.bucket_secs)
                tminus_buckets[idx] += 1

            entry_secs_raw = row.get("entry_seconds_remaining")
            entry_secs: Optional[float] = None
            try:
                if entry_secs_raw is not None:
                    entry_secs = float(entry_secs_raw)
            except (TypeError, ValueError):
                entry_secs = None

            if entry_secs is None or exit_secs is None:
                hold_buckets["unknown"] += 1
            else:
                held = max(0.0, entry_secs - exit_secs)
                if held < 30:
                    hold_buckets["0-30s"] += 1
                elif held < 60:
                    hold_buckets["30-60s"] += 1
                elif held < 120:
                    hold_buckets["1-2m"] += 1
                elif held < 180:
                    hold_buckets["2-3m"] += 1
                elif held < 300:
                    hold_buckets["3-5m"] += 1
                else:
                    hold_buckets["5m+"] += 1

    lookback_label = (
        f"last {args.last_hours:g}h" if args.last_hours is not None else "all data"
    )
    _print_header(f"Stop-loss timing report ({lookback_label})")
    print(f"Trade file: {args.file}")
    print(f"Total exits: {total_exits}")
    print(f"Stop-loss exits: {total_stop_losses}")
    if total_exits > 0:
        print(f"Stop-loss rate: {total_stop_losses / total_exits:.2%}")
    if total_stop_losses == 0:
        print("No EXIT_STOP_LOSS records found in the selected window.")
        return 0

    _print_header("Top T-minus windows for stop-loss hits")
    for idx, count in tminus_buckets.most_common(args.top):
        label = _bucket_label(idx, args.bucket_secs)
        pct = (count / total_stop_losses) * 100.0
        print(f"{label:>24} : {count:4d} ({pct:5.1f}%)")
    if missing_tminus > 0:
        pct = (missing_tminus / total_stop_losses) * 100.0
        print(f"{'missing seconds_remaining':>24} : {missing_tminus:4d} ({pct:5.1f}%)")

    _print_header("Hold duration to stop-loss (entry -> SL)")
    for label, count in hold_buckets.most_common():
        pct = (count / total_stop_losses) * 100.0
        print(f"{label:>24} : {count:4d} ({pct:5.1f}%)")

    if sl_hours_utc:
        _print_header("Top UTC hours with stop-loss hits")
        for hour, count in sl_hours_utc.most_common(8):
            pct = (count / total_stop_losses) * 100.0
            print(f"{hour:02d}:00-{hour:02d}:59 UTC : {count:4d} ({pct:5.1f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
