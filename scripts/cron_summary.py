#!/usr/bin/env python3
"""
Cron / agent wrapper: run the daily job and print a short human summary on stdout.

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
RUN = ROOT / "run.sh"
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
        timeout=1800,
    )
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    tail = "\n".join(out.strip().splitlines()[-40:])

    man_path = DATA / f"manifest-{TODAY}.json"
    man: dict = {}
    if man_path.is_file():
        try:
            man = json.loads(man_path.read_text(encoding="utf-8"))
        except Exception:
            man = {}

    stats = man.get("stats") or {}
    pl = man.get("playlist") or {}
    name = pl.get("name") or f"网易云日推-{TODAY}"
    pl_id = pl.get("id") or ""
    folder = man.get("folder") or {}
    folder_label = folder.get("name") or ""

    if man.get("skipped"):
        # 已存在/跳过 → 正常无操作，静默（watchdog：成功不通知）
        return 0

    if proc.returncode == 0 and (stats.get("matched") or pl_id):
        # 同步成功 → 静默（watchdog：成功不通知）
        return 0

    print("ERROR: sync failed")
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
        print("ERROR: timeout (>30min)")
        raise SystemExit(124)
