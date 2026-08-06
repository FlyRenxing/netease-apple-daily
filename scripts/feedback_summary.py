#!/usr/bin/env python3
"""
Cron / agent wrapper: run Apple → NetEase feedback and print a short summary.

Environment:
  NETEASE_APPLE_DAILY_ROOT  Project root (default: parent of scripts/)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(
    os.environ.get(
        "NETEASE_APPLE_DAILY_ROOT",
        Path(__file__).resolve().parent.parent,
    )
).resolve()
RUN = ROOT / "run_feedback.sh"
DATA = ROOT / "data"
TODAY = date.today().isoformat()


def main() -> int:
    for k in list(os.environ):
        if "proxy" in k.lower():
            os.environ.pop(k, None)

    if not RUN.is_file():
        print(f"ERROR: project not found: {ROOT}")
        return 2

    cookie = DATA / "cookie.txt"
    if not cookie.is_file() or not cookie.read_text(encoding="utf-8").strip():
        print("ERROR: NetEase cookie missing")
        print(f"Run: {ROOT}/login.sh qr-init  # then qr-poll after scanning")
        return 3

    proc = subprocess.run(
        ["/bin/bash", str(RUN)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=3600,
    )
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    tail = "\n".join(out.strip().splitlines()[-50:])

    man_path = DATA / f"feedback-manifest-{TODAY}.json"
    man: dict = {}
    if man_path.is_file():
        try:
            man = json.loads(man_path.read_text(encoding="utf-8"))
        except Exception:
            man = {}

    likes = man.get("likes") or {}
    scrob = man.get("scrobble") or {}
    dry = man.get("dry_run")

    if proc.returncode == 0:
        print("OK: Apple Music → NetEase feedback")
        if dry:
            print("mode: dry-run")
        print(
            f"likes: +{likes.get('liked', 0)} "
            f"(matched {likes.get('matched', '?')}, "
            f"unmatched {likes.get('unmatched', 0)}, "
            f"already {likes.get('already_liked', 0)}, "
            f"src {likes.get('source_total', '?')})"
        )
        if scrob.get("seed_only"):
            print(
                f"scrobble: seeded recent snapshot "
                f"({scrob.get('source_total', '?')} tracks, no write)"
            )
        else:
            print(
                f"scrobble: +{scrob.get('scrobbled', 0)} "
                f"(new {scrob.get('new_plays', '?')}, "
                f"unmatched {scrob.get('unmatched', 0)}, "
                f"window {scrob.get('source_total', '?')})"
            )
        return 0

    print("ERROR: feedback sync failed")
    print(f"exit={proc.returncode}")
    if "cookie" in out.lower():
        print("hint: re-login with ./login.sh qr")
    if "media-user-token" in out or "Music User Token" in out:
        print("hint: set MUSIC_USER_TOKEN or AM_CONFIG media-user-token")
    print("--- log tail ---")
    print(tail[-2500:] if len(tail) > 2500 else tail)
    return proc.returncode or 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print("ERROR: timeout (>60min)")
        raise SystemExit(124)
