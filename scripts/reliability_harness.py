#!/usr/bin/env python
"""Reliability harness — measure build go-rate over N identical-brief runs.

The audit found go/no_go is luck-driven (~14% over 7 identical builds). Progress was
claimed by feel, never measured. This makes it measurable: run the SAME brief N times
and report the real go-rate, so any "reliable" claim can be gated on data (e.g. >=80%).

Usage:
  reliability_harness.py run --brief-file BRIEF.txt --n 3 --stack nextjs --prefix rel
  reliability_harness.py report --prefix choateshvac          # go-rate from existing DB rows
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data" / "skyn3t.db"


def skyn3t_command(*args: str) -> list[str]:
    """Build a cross-platform CLI command using the active interpreter."""
    return [sys.executable, "-m", "skyn3t.cli.main", *args]


def _verdict_for(slug: str):
    c = sqlite3.connect(str(DB))
    try:
        return c.execute(
            "select verdict, score, status from builds where slug=? "
            "order by created_at desc limit 1", (slug,)).fetchone()
    finally:
        c.close()


def _summary(results: list[tuple[str, tuple | None]]) -> None:
    n = len(results)
    gos = sum(1 for _, v in results if v and v[0] == "go")
    print(f"\n=== GO-RATE: {gos}/{n} = {round(100 * gos / max(1, n))}% ===")
    for slug, v in results:
        if v:
            print(f"  {slug:24} {v[0] or 'running':7} score={v[1]} status={v[2]}")
        else:
            print(f"  {slug:24} no-row")


def run(args) -> None:
    brief = Path(args.brief_file).read_text(encoding="utf-8").strip()
    results: list[tuple[str, tuple | None]] = []
    for i in range(1, args.n + 1):
        slug = f"{args.prefix}-{i}"
        print(f"[{i}/{args.n}] building {slug} (timeout {args.timeout}s) ...", flush=True)
        t0 = time.time()
        try:
            subprocess.run(
                skyn3t_command(
                    "studio", "build", brief, "--stack", args.stack, "--slug", slug
                ),
                cwd=str(REPO), timeout=args.timeout, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            print(f"  {slug}: TIMEOUT after {args.timeout}s", flush=True)
        v = _verdict_for(slug)
        print(f"  {slug}: {v} ({int(time.time() - t0)}s)", flush=True)
        results.append((slug, v))
    _summary(results)


def report(args) -> None:
    c = sqlite3.connect(str(DB))
    try:
        rows = c.execute(
            "select slug, verdict, score from builds where slug like ? "
            "order by created_at desc", (args.prefix + "%",)).fetchall()
    finally:
        c.close()
    completed = [r for r in rows if r[1] in ("go", "no_go")]
    gos = sum(1 for r in completed if r[1] == "go")
    print(f"=== {args.prefix}*: {gos}/{len(completed)} completed = "
          f"{round(100 * gos / max(1, len(completed)))}% go ===")
    for r in rows[:25]:
        print(f"  {r[0]:26} {r[1] or 'running':7} {r[2]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run N identical builds + report go-rate")
    r.add_argument("--brief-file", required=True)
    r.add_argument("--n", type=int, default=3)
    r.add_argument("--stack", default="nextjs")
    r.add_argument("--prefix", default="rel")
    r.add_argument("--timeout", type=int, default=2400)
    r.set_defaults(fn=run)
    rep = sub.add_parser("report", help="go-rate from existing DB rows by slug prefix")
    rep.add_argument("--prefix", required=True)
    rep.set_defaults(fn=report)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
