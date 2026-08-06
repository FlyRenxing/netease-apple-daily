#!/usr/bin/env python3
"""
Apple Music → 网易云 反馈回写

1. 喜爱歌曲（Favorite Songs / 喜爱歌曲）→ 网易云红心 /like
2. 最近播放 recent/played/tracks 增量 → 网易云听歌打卡 /scrobble

默认会真实写入；请先用 --dry-run 看匹配结果。
状态与去重：data/feedback-state.json（gitignore）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 复用日推脚本的配置加载、HTTP、Apple/NCM 客户端
import daily_recommend as dr


# ---------------------------------------------------------------------------
# 配置（config.env 可覆盖）
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = (os.environ.get(name) or "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


FEEDBACK_SYNC_LIKES = _env_bool("FEEDBACK_SYNC_LIKES", True)
FEEDBACK_SYNC_SCROBBLE = _env_bool("FEEDBACK_SYNC_SCROBBLE", True)
FEEDBACK_DRY_RUN = _env_bool("FEEDBACK_DRY_RUN", False)

FEEDBACK_FAVORITES_PLAYLIST_ID = os.environ.get(
    "FEEDBACK_FAVORITES_PLAYLIST_ID", ""
).strip()
FEEDBACK_FAVORITES_PLAYLIST_NAME = os.environ.get(
    "FEEDBACK_FAVORITES_PLAYLIST_NAME", "喜爱歌曲"
).strip()

FEEDBACK_RECENT_LIMIT = _env_int("FEEDBACK_RECENT_LIMIT", 50)
FEEDBACK_MAX_LIKES_PER_RUN = _env_int("FEEDBACK_MAX_LIKES_PER_RUN", 40)
FEEDBACK_MAX_SCROBBLE_PER_RUN = _env_int("FEEDBACK_MAX_SCROBBLE_PER_RUN", 25)
# 本轮最多对多少首喜爱做反查（含已红心/未匹配），防止全库 400+ 首拖很久
FEEDBACK_MAX_LIKE_SCAN_PER_RUN = _env_int("FEEDBACK_MAX_LIKE_SCAN_PER_RUN", 80)
FEEDBACK_MIN_SCORE = float(os.environ.get("FEEDBACK_MIN_SCORE", "55") or "55")
FEEDBACK_SCROBBLE_TIME = _env_int("FEEDBACK_SCROBBLE_TIME", 240)
FEEDBACK_REQUEST_SLEEP = float(os.environ.get("FEEDBACK_REQUEST_SLEEP", "0.35") or "0.35")
# 首次运行只做 recent 快照、不 scrobble（避免把整窗历史一次打卡）
FEEDBACK_SCROBBLE_SEED_ONLY_FIRST = _env_bool("FEEDBACK_SCROBBLE_SEED_ONLY_FIRST", True)

STATE_FILE = dr._resolve_path(
    os.environ.get("FEEDBACK_STATE_FILE", ""),
    dr.DATA_DIR / "feedback-state.json",
)
MANIFEST_PREFIX = "feedback-manifest"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class AppleSong:
    apple_id: str  # catalog id（数字字符串）
    name: str
    artists: str
    album: str
    duration_ms: int = 0
    isrc: str = ""
    library_id: str = ""  # i.xxx 若来自资料库

    @property
    def search_term(self) -> str:
        primary = self.artists.split(",")[0].split("&")[0].strip()
        primary = re.split(r"\s+feat\.?\s+", primary, flags=re.I)[0].strip()
        return f"{primary} {self.name}".strip()


def log(msg: str) -> None:
    dr.log(msg)


# ---------------------------------------------------------------------------
# Apple Music 读取
# ---------------------------------------------------------------------------

def _strip_base(path: str, base: str) -> str:
    path = (path or "").strip()
    if path.startswith("http"):
        path = path.replace(base, "")
    return path


def am_paginate(
    am: dr.AppleMusicClient, first_path: str, limit: int = 500
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    path: Optional[str] = first_path
    while path and len(out) < limit:
        code, data = am.request(path)
        if code != 200:
            raise RuntimeError(
                f"Apple GET {path} 失败 {code}: {json.dumps(data, ensure_ascii=False)[:400]}"
            )
        batch = data.get("data") or []
        out.extend(batch)
        nxt = _strip_base(data.get("next") or "", am.base)
        path = nxt or None
        if path:
            time.sleep(0.15)
    return out[:limit]


def apple_resource_to_song(item: dict[str, Any]) -> Optional[AppleSong]:
    attrs = item.get("attributes") or {}
    name = str(attrs.get("name") or "").strip()
    if not name:
        return None
    pp = attrs.get("playParams") or {}
    catalog_id = str(
        pp.get("catalogId") or pp.get("reportingId") or item.get("id") or ""
    ).strip()
    # library id 形如 i.xxx，catalog 应为纯数字
    library_id = ""
    raw_id = str(item.get("id") or "")
    if raw_id.startswith("i.") or item.get("type") == "library-songs":
        library_id = raw_id
        if not catalog_id.isdigit():
            catalog_id = str(pp.get("catalogId") or pp.get("reportingId") or "")
    if not catalog_id or not catalog_id.replace("-", "").isalnum():
        # 仍可用 library 信息搜，apple_id 用 library_id 兜底
        catalog_id = catalog_id or library_id or raw_id
    return AppleSong(
        apple_id=catalog_id,
        name=name,
        artists=str(attrs.get("artistName") or "").strip(),
        album=str(attrs.get("albumName") or "").strip(),
        duration_ms=int(attrs.get("durationInMillis") or 0),
        isrc=str(attrs.get("isrc") or "").strip(),
        library_id=library_id,
    )


def find_favorites_playlist(am: dr.AppleMusicClient) -> dict[str, Any]:
    if FEEDBACK_FAVORITES_PLAYLIST_ID:
        code, data = am.request(
            f"/v1/me/library/playlists/{FEEDBACK_FAVORITES_PLAYLIST_ID}"
        )
        if code == 200 and (data.get("data") or []):
            return (data.get("data") or [None])[0]
        raise SystemExit(
            f"FEEDBACK_FAVORITES_PLAYLIST_ID 无效: {FEEDBACK_FAVORITES_PLAYLIST_ID}"
        )

    candidates_names = []
    if FEEDBACK_FAVORITES_PLAYLIST_NAME:
        candidates_names.append(FEEDBACK_FAVORITES_PLAYLIST_NAME)
    candidates_names.extend(
        [
            "喜爱歌曲",
            "Favorite Songs",
            "Favourites",
            "Favorites",
            "喜欢的音乐",
        ]
    )
    # 去重保序
    seen: set[str] = set()
    names: list[str] = []
    for n in candidates_names:
        if n and n not in seen:
            seen.add(n)
            names.append(n)

    playlists = am_paginate(am, "/v1/me/library/playlists?limit=100", limit=300)
    # 优先精确名 + canEdit=false（系统喜爱列表）
    exact: list[dict[str, Any]] = []
    fuzzy: list[dict[str, Any]] = []
    for pl in playlists:
        attrs = pl.get("attributes") or {}
        name = str(attrs.get("name") or "")
        if name in names:
            exact.append(pl)
        else:
            low = name.lower()
            if any(
                k in low
                for k in ("favorit", "favourite", "喜爱", "喜欢的")
            ):
                fuzzy.append(pl)

    def rank(pl: dict[str, Any]) -> tuple:
        attrs = pl.get("attributes") or {}
        can_edit = bool(attrs.get("canEdit"))
        name = str(attrs.get("name") or "")
        # canEdit False 优先（系统歌单）
        return (0 if not can_edit else 1, 0 if name in names else 1, name)

    pool = exact or fuzzy
    if not pool:
        raise SystemExit(
            "未找到 Apple Music「喜爱歌曲」播放列表。"
            "请在 config.env 设置 FEEDBACK_FAVORITES_PLAYLIST_ID 或 "
            "FEEDBACK_FAVORITES_PLAYLIST_NAME。"
        )
    pool.sort(key=rank)
    return pool[0]


def fetch_favorite_songs(am: dr.AppleMusicClient) -> tuple[dict[str, Any], list[AppleSong]]:
    pl = find_favorites_playlist(am)
    pid = str(pl.get("id"))
    name = (pl.get("attributes") or {}).get("name")
    log(f"喜爱歌单: 「{name}」(id={pid})")
    items = am_paginate(
        am, f"/v1/me/library/playlists/{pid}/tracks?limit=100", limit=2000
    )
    songs: list[AppleSong] = []
    for it in items:
        s = apple_resource_to_song(it)
        if s:
            songs.append(s)
    log(f"喜爱曲目: {len(songs)} 首")
    return pl, songs


def fetch_recent_tracks(am: dr.AppleMusicClient, limit: int) -> list[AppleSong]:
    items = am_paginate(
        am, f"/v1/me/recent/played/tracks?limit={min(limit, 30)}", limit=limit
    )
    songs: list[AppleSong] = []
    for it in items:
        s = apple_resource_to_song(it)
        if s:
            songs.append(s)
    log(f"最近播放: 拉取 {len(songs)} 首（上限 {limit}）")
    return songs


# ---------------------------------------------------------------------------
# 网易云：搜索 / 红心 / 打卡
# ---------------------------------------------------------------------------

def ncm_cloudsearch(cookie: str, keywords: str, limit: int = 8) -> list[dict[str, Any]]:
    data = dr.ncm_get(
        "/cloudsearch",
        cookie,
        {"keywords": keywords, "limit": str(limit), "type": "1"},
    )
    if not isinstance(data, dict):
        return []
    # 部分部署 code 在外层
    if data.get("code") not in (200, None) and not (data.get("result") or {}).get("songs"):
        # 回退 search
        data = dr.ncm_get(
            "/search",
            cookie,
            {"keywords": keywords, "limit": str(limit), "type": "1"},
        )
    result = data.get("result") or {}
    return list(result.get("songs") or [])


def _version_tags(title: str) -> set[str]:
    s = title.lower()
    tags: set[str] = set()
    checks = (
        ("taylor's version", "tv"),
        ("taylor’s version", "tv"),
        ("live", "live"),
        ("remix", "remix"),
        ("acoustic", "acoustic"),
        ("deluxe", "deluxe"),
        ("karaoke", "karaoke"),
        ("instrumental", "instrumental"),
    )
    for needle, tag in checks:
        if needle in s:
            tags.add(tag)
    return tags


def score_apple_vs_ncm(apple: AppleSong, ncm: dict[str, Any]) -> float:
    """与 daily_recommend.score_match 对称：Apple 为「真」，NCM 为候选。"""
    ncm_name = str(ncm.get("name") or "")
    nt = dr.normalize_title(ncm_name)
    ar = ncm.get("ar") or ncm.get("artists") or []
    # 主艺人 + 全体艺人串，便于 feat.
    ar_names: list[str] = []
    for a in ar:
        if isinstance(a, dict) and a.get("name"):
            ar_names.append(str(a["name"]))
        elif isinstance(a, str):
            ar_names.append(a)
    na = dr.normalize_title(ar_names[0]) if ar_names else ""
    na_all = dr.normalize_title(" ".join(ar_names))
    al_obj = ncm.get("al") or ncm.get("album") or {}
    nal = dr.normalize_title(
        str(al_obj.get("name") if isinstance(al_obj, dict) else al_obj or "")
    )
    t = dr.normalize_title(apple.name)
    a_raw = apple.artists.split(",")[0].split("&")[0]
    a_raw = re.split(r"\s+feat\.?\s+", a_raw, flags=re.I)[0].strip()
    a = dr.normalize_title(a_raw)
    al = dr.normalize_title(apple.album)
    if not t or not nt:
        return 0.0
    score = 0.0
    if t == nt:
        score += 50
    elif nt in t or t in nt:
        score += 30
    else:
        score += min(20, len(set(t) & set(nt)) * 2)

    # 未去括号的完整标题更一致时加分（Taylor's Version 等）
    if apple.name.strip().lower() == ncm_name.strip().lower():
        score += 12

    artist_pts = 0.0
    if a and ar_names:
        norms = [dr.normalize_title(x) for x in ar_names if x]
        if a in norms:
            artist_pts = 40
        else:
            for n in norms:
                if not n:
                    continue
                if a == n or a in n or n in a:
                    artist_pts = max(artist_pts, 28)
                    break
            if artist_pts < 28:
                # 分词：防止 "iu" 误命中 "iu piano" 以外的拼接串
                a_parts = [p for p in re.split(r"[\s,./&]+", a) if len(p) >= 2]
                for n in norms:
                    n_parts = [p for p in re.split(r"[\s,./&]+", n) if len(p) >= 2]
                    if a_parts and n_parts and (set(a_parts) & set(n_parts)):
                        artist_pts = max(artist_pts, 22)
    score += artist_pts

    if nal and al:
        if al == nal:
            score += 10
        elif nal in al or al in nal:
            score += 5
    dt = int(ncm.get("dt") or ncm.get("duration") or 0)
    if apple.duration_ms and dt:
        diff = abs(apple.duration_ms - dt)
        if diff <= 3000:
            score += 8
        elif diff <= 8000:
            score += 4
        elif diff > 45000:
            score -= 10

    # 版本标签（live / TV / remix）不一致则惩罚
    va = _version_tags(apple.name)
    vn = _version_tags(ncm_name)
    if va or vn:
        if va == vn:
            score += 12
        else:
            score -= 18

    # 艺人完全对不上时大幅降权（避免同名异曲）
    if a and na and artist_pts < 20:
        score *= 0.55

    return score


def match_netease(cookie: str, apple: AppleSong) -> Optional[dict[str, Any]]:
    terms = [
        apple.search_term,
        f"{apple.name} {apple.artists.split(',')[0].strip()}" if apple.artists else apple.name,
        apple.name,
    ]
    candidates: list[dict[str, Any]] = []
    seen_terms: set[str] = set()
    seen_ids: set[int] = set()
    for term in terms:
        term = term.strip()
        if not term or term in seen_terms:
            continue
        seen_terms.add(term)
        for s in ncm_cloudsearch(cookie, term, limit=8):
            sid = int(s.get("id") or 0)
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                candidates.append(s)
        time.sleep(FEEDBACK_REQUEST_SLEEP * 0.5)

    best: Optional[tuple[float, dict[str, Any]]] = None
    for s in candidates:
        sc = score_apple_vs_ncm(apple, s)
        if best is None or sc > best[0]:
            best = (sc, s)
    if not best or best[0] < FEEDBACK_MIN_SCORE:
        return None
    # 艺人无任何重合则拒绝（避免同名异曲，如 Autumn Morning 翻奏）
    ar = best[1].get("ar") or []
    ar_names = [
        str(x.get("name") if isinstance(x, dict) else x)
        for x in ar
        if x
    ]
    a = dr.normalize_title(
        re.split(r"\s+feat\.?\s+", apple.artists.split(",")[0], flags=re.I)[0]
    )
    norms = [dr.normalize_title(x) for x in ar_names]
    artist_ok = False
    if a and norms:
        if a in norms:
            artist_ok = True
        else:
            for n in norms:
                if a in n or n in a:
                    artist_ok = True
                    break
                if set(p for p in re.split(r"[\s,./&]+", a) if len(p) >= 2) & set(
                    p for p in re.split(r"[\s,./&]+", n) if len(p) >= 2
                ):
                    artist_ok = True
                    break
    if a and norms and not artist_ok:
        return None
    s = best[1]
    ar = s.get("ar") or []
    artists = " / ".join(
        str(a.get("name") if isinstance(a, dict) else a) for a in ar if a
    )
    al = s.get("al") or {}
    return {
        "id": int(s.get("id")),
        "name": s.get("name"),
        "artists": artists,
        "album": al.get("name") if isinstance(al, dict) else "",
        "duration_ms": int(s.get("dt") or 0),
        "album_id": int(al.get("id") or 0) if isinstance(al, dict) else 0,
        "score": best[0],
    }


def ncm_likelist_ids(cookie: str, uid: int) -> set[int]:
    data = dr.ncm_get("/likelist", cookie, {"uid": str(uid)})
    ids = data.get("ids") or []
    return {int(x) for x in ids if x is not None}


def ncm_like(cookie: str, song_id: int, like: bool = True) -> dict[str, Any]:
    return dr.ncm_get(
        "/like",
        cookie,
        {"id": str(song_id), "like": "true" if like else "false"},
    )


def ncm_scrobble(
    cookie: str, song_id: int, source_id: int, play_seconds: int
) -> dict[str, Any]:
    return dr.ncm_get(
        "/scrobble",
        cookie,
        {
            "id": str(song_id),
            "sourceid": str(source_id or 0),
            "time": str(max(1, play_seconds)),
        },
    )


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {
            "liked_netease_ids": [],
            "liked_apple_ids": [],
            "scrobbled_apple_ids": [],
            "seen_recent_apple_ids": [],
            "unmatched_apple_ids": {},
            "favorites_playlist_id": "",
            "updated_at": None,
        }
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {
            "liked_netease_ids": [],
            "liked_apple_ids": [],
            "scrobbled_apple_ids": [],
            "seen_recent_apple_ids": [],
            "unmatched_apple_ids": {},
            "favorites_playlist_id": "",
            "updated_at": None,
        }


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _as_id_set(vals: Any) -> set[str]:
    out: set[str] = set()
    for v in vals or []:
        out.add(str(v))
    return out


def _as_int_set(vals: Any) -> set[int]:
    out: set[int] = set()
    for v in vals or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def sync_likes(
    cookie: str,
    am: dr.AppleMusicClient,
    state: dict[str, Any],
    already_liked: set[int],
    dry_run: bool,
    limit: int,
) -> dict[str, Any]:
    stats = {
        "source_total": 0,
        "already_liked": 0,
        "matched": 0,
        "unmatched": 0,
        "liked": 0,
        "skipped_state": 0,
        "errors": 0,
        "items": [],
    }
    pl, songs = fetch_favorite_songs(am)
    state["favorites_playlist_id"] = str(pl.get("id") or "")
    stats["source_total"] = len(songs)

    liked_apple = _as_id_set(state.get("liked_apple_ids"))
    liked_ncm = _as_int_set(state.get("liked_netease_ids")) | set(already_liked)
    unmatched_map: dict[str, Any] = dict(state.get("unmatched_apple_ids") or {})

    actions = 0
    scanned = 0
    scan_limit = max(limit, FEEDBACK_MAX_LIKE_SCAN_PER_RUN)
    for i, apple in enumerate(songs, 1):
        if actions >= limit:
            log(f"喜爱同步达到本轮写入上限 {limit}，其余下轮继续")
            break
        key = apple.apple_id
        row: dict[str, Any] = {"apple": asdict(apple), "action": None}

        if key in liked_apple:
            stats["skipped_state"] += 1
            row["action"] = "skip_state"
            # 不计入 scanned（无 API），快速跳过
            continue

        if scanned >= scan_limit:
            log(f"喜爱反查达到本轮扫描上限 {scan_limit}，其余下轮继续")
            break
        scanned += 1

        log(f"[like {i}/{len(songs)}] {apple.artists} - {apple.name}")
        m = match_netease(cookie, apple)
        if not m:
            log("  未匹配网易云")
            stats["unmatched"] += 1
            row["action"] = "unmatched"
            unmatched_map[key] = {
                "name": apple.name,
                "artists": apple.artists,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            stats["items"].append(row)
            continue

        stats["matched"] += 1
        row["netease"] = m
        nid = int(m["id"])
        log(
            f"  → {m['artists']} - {m['name']} id={nid} score={m['score']:.0f}"
        )

        if nid in liked_ncm:
            log("  已在网易云红心列表，记入 state")
            stats["already_liked"] += 1
            row["action"] = "already_liked"
            liked_apple.add(key)
            liked_ncm.add(nid)
            stats["items"].append(row)
            continue

        if dry_run:
            log("  [dry-run] 将红心")
            row["action"] = "dry_run_like"
            actions += 1
            stats["liked"] += 1
            stats["items"].append(row)
            continue

        try:
            resp = ncm_like(cookie, nid, True)
            code = resp.get("code")
            if code not in (200, None):
                log(f"  红心失败: {json.dumps(resp, ensure_ascii=False)[:200]}")
                row["action"] = "error"
                row["error"] = resp
                stats["errors"] += 1
            else:
                log("  已红心")
                row["action"] = "liked"
                stats["liked"] += 1
                liked_apple.add(key)
                liked_ncm.add(nid)
                actions += 1
                unmatched_map.pop(key, None)
            time.sleep(FEEDBACK_REQUEST_SLEEP)
        except Exception as e:
            log(f"  红心异常: {e}")
            row["action"] = "error"
            row["error"] = str(e)
            stats["errors"] += 1
        stats["items"].append(row)

    state["liked_apple_ids"] = sorted(liked_apple)
    state["liked_netease_ids"] = sorted(liked_ncm)
    # 控制 unmatched 体积
    if len(unmatched_map) > 500:
        # 保留最近写入的 500 个（按 at 粗排做不到，直接截断）
        unmatched_map = dict(list(unmatched_map.items())[-500:])
    state["unmatched_apple_ids"] = unmatched_map
    return stats


def sync_scrobble(
    cookie: str,
    am: dr.AppleMusicClient,
    state: dict[str, Any],
    dry_run: bool,
    limit: int,
    seed_only_first: bool = True,
) -> dict[str, Any]:
    stats = {
        "source_total": 0,
        "new_plays": 0,
        "seed_only": False,
        "matched": 0,
        "unmatched": 0,
        "scrobbled": 0,
        "skipped_seen": 0,
        "errors": 0,
        "items": [],
    }
    songs = fetch_recent_tracks(am, FEEDBACK_RECENT_LIMIT)
    stats["source_total"] = len(songs)
    current_ids = [s.apple_id for s in songs]
    seen = _as_id_set(state.get("seen_recent_apple_ids"))
    scrobbled = _as_id_set(state.get("scrobbled_apple_ids"))

    first_run = len(seen) == 0
    if first_run and seed_only_first:
        log(
            "首次运行：仅写入最近播放快照，不 scrobble "
            "（避免把历史窗口一次打卡）。下轮起同步新增。"
        )
        stats["seed_only"] = True
        # 仍匹配打印若干预览
        for apple in songs[: min(5, len(songs))]:
            m = match_netease(cookie, apple)
            log(
                f"  seed preview: {apple.artists} - {apple.name} → "
                + (
                    f"{m['artists']} - {m['name']} ({m['id']})"
                    if m
                    else "未匹配"
                )
            )
        state["seen_recent_apple_ids"] = current_ids
        # 有界
        state["scrobbled_apple_ids"] = sorted(scrobbled)[-800:]
        return stats

    # 新出现在窗口中、且未曾 scrobble 的曲（按 recent 顺序，越靠前越新）
    new_songs = [
        s
        for s in songs
        if s.apple_id not in seen and s.apple_id not in scrobbled
    ]
    stats["new_plays"] = len(new_songs)
    log(f"增量新播放（相对快照）: {len(new_songs)} 首")

    actions = 0
    for i, apple in enumerate(new_songs, 1):
        if actions >= limit:
            log(f"scrobble 达到本轮上限 {limit}")
            break
        log(f"[scrobble {i}/{len(new_songs)}] {apple.artists} - {apple.name}")
        row: dict[str, Any] = {"apple": asdict(apple), "action": None}
        m = match_netease(cookie, apple)
        if not m:
            log("  未匹配")
            stats["unmatched"] += 1
            row["action"] = "unmatched"
            stats["items"].append(row)
            # 仍记入 seen，避免每轮重复搜失败曲
            seen.add(apple.apple_id)
            continue

        stats["matched"] += 1
        row["netease"] = m
        nid = int(m["id"])
        source_id = int(m.get("album_id") or 0)
        play_sec = FEEDBACK_SCROBBLE_TIME
        if apple.duration_ms:
            play_sec = max(30, min(FEEDBACK_SCROBBLE_TIME, apple.duration_ms // 1000 - 1))
        log(
            f"  → {m['artists']} - {m['name']} id={nid} "
            f"score={m['score']:.0f} time={play_sec}s"
        )

        if dry_run:
            log("  [dry-run] 将 scrobble")
            row["action"] = "dry_run_scrobble"
            stats["scrobbled"] += 1
            actions += 1
            stats["items"].append(row)
            # dry-run 不更新 scrobbled，但更新 seen 会让下轮不重复；
            # dry-run 下不改 seen/scrobbled 在 main 里统一：此处仍模拟
            continue

        try:
            resp = ncm_scrobble(cookie, nid, source_id, play_sec)
            code = resp.get("code")
            if code not in (200, None):
                log(f"  scrobble 失败: {json.dumps(resp, ensure_ascii=False)[:200]}")
                row["action"] = "error"
                row["error"] = resp
                stats["errors"] += 1
            else:
                log("  已 scrobble")
                row["action"] = "scrobbled"
                stats["scrobbled"] += 1
                scrobbled.add(apple.apple_id)
                seen.add(apple.apple_id)
                actions += 1
            time.sleep(FEEDBACK_REQUEST_SLEEP)
        except Exception as e:
            log(f"  scrobble 异常: {e}")
            row["action"] = "error"
            row["error"] = str(e)
            stats["errors"] += 1
        stats["items"].append(row)

    # 更新 seen：合并当前窗口（保持较新顺序，截断）
    merged: list[str] = []
    have: set[str] = set()
    for aid in current_ids + list(seen):
        if aid not in have:
            have.add(aid)
            merged.append(aid)
    state["seen_recent_apple_ids"] = merged[:500]
    state["scrobbled_apple_ids"] = sorted(scrobbled)[-800:]
    # 窗口内已见但未进 new 的
    stats["skipped_seen"] = max(0, len(songs) - len(new_songs))
    return stats


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Apple Music 喜爱/最近播放 → 网易云红心/听歌打卡"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只匹配不写网易云（也可用 FEEDBACK_DRY_RUN=1）",
    )
    p.add_argument(
        "--likes-only",
        action="store_true",
        help="只同步喜爱→红心",
    )
    p.add_argument(
        "--scrobble-only",
        action="store_true",
        help="只同步最近播放→scrobble",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="覆盖每类本轮最大写入条数（0=用配置）",
    )
    p.add_argument(
        "--force-scrobble-seed",
        action="store_true",
        help="首次也 scrobble（不推荐；默认首次只建快照）",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    dry_run = bool(args.dry_run or FEEDBACK_DRY_RUN)
    do_likes = FEEDBACK_SYNC_LIKES and not args.scrobble_only
    do_scrobble = FEEDBACK_SYNC_SCROBBLE and not args.likes_only
    seed_only_first = FEEDBACK_SCROBBLE_SEED_ONLY_FIRST and not args.force_scrobble_seed

    like_limit = args.limit or FEEDBACK_MAX_LIKES_PER_RUN
    scrobble_limit = args.limit or FEEDBACK_MAX_SCROBBLE_PER_RUN

    dr.LOG_DIR.mkdir(parents=True, exist_ok=True)
    dr.DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = dr.LOG_DIR / f"feedback-{dr.TODAY}.log"

    class Tee:
        def __init__(self, *files):
            self.files = files

        def write(self, data):
            for f in self.files:
                f.write(data)
                f.flush()

        def flush(self):
            for f in self.files:
                f.flush()

    log_fp = open(log_file, "a", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log_fp)  # type: ignore
    sys.stderr = Tee(sys.__stderr__, log_fp)  # type: ignore

    log(
        f"=== Apple → 网易云反馈 {dr.TODAY} ({dr.RUN_TS}) "
        f"dry_run={dry_run} likes={do_likes} scrobble={do_scrobble} ==="
    )
    dr.ensure_ncm_api()
    cookie = dr.load_cookie()

    uid = 0
    nickname = "?"
    try:
        st = dr.ncm_get("/login/status", cookie)
        profile = (st.get("data") or {}).get("profile") or {}
        nickname = str(profile.get("nickname") or "?")
        uid = int(profile.get("userId") or 0)
        log(f"网易云账号: {nickname} (uid={uid})")
    except Exception as e:
        log(f"警告: 登录状态检查失败: {e}")

    already_liked: set[int] = set()
    if uid and do_likes:
        try:
            already_liked = ncm_likelist_ids(cookie, uid)
            log(f"网易云现有红心: {len(already_liked)} 首")
        except Exception as e:
            log(f"警告: 拉取 likelist 失败: {e}")

    user_token = dr.get_music_user_token()
    dev_token = dr.fetch_web_developer_token()
    am = dr.AppleMusicClient(dev_token, user_token, dr.STOREFRONT)
    sf = am.storefront_info()
    sf_id = (sf.get("data") or [{}])[0].get("id")
    log(f"Apple Music storefront: {sf_id}")

    state = load_state()
    # dry-run 用内存副本，结束不写 state（scrobble 首次 seed 除外？）
    # 策略：dry-run 完全不持久化；真实运行才 save
    like_stats: Optional[dict[str, Any]] = None
    scrobble_stats: Optional[dict[str, Any]] = None

    if do_likes:
        log("--- 同步喜爱 → 红心 ---")
        like_stats = sync_likes(
            cookie, am, state, already_liked, dry_run, like_limit
        )
        log(
            "喜爱统计: "
            + json.dumps(
                {k: v for k, v in like_stats.items() if k != "items"},
                ensure_ascii=False,
            )
        )

    if do_scrobble:
        log("--- 同步最近播放 → scrobble ---")
        scrobble_stats = sync_scrobble(
            cookie,
            am,
            state,
            dry_run,
            scrobble_limit,
            seed_only_first=seed_only_first,
        )
        log(
            "scrobble 统计: "
            + json.dumps(
                {k: v for k, v in scrobble_stats.items() if k != "items"},
                ensure_ascii=False,
            )
        )

    man = {
        "date": dr.TODAY,
        "run_ts": dr.RUN_TS,
        "dry_run": dry_run,
        "account": {"nickname": nickname, "uid": uid},
        "likes": (
            {k: v for k, v in (like_stats or {}).items() if k != "items"}
            if like_stats
            else None
        ),
        "scrobble": (
            {k: v for k, v in (scrobble_stats or {}).items() if k != "items"}
            if scrobble_stats
            else None
        ),
        # 明细可能很长，只保留本轮 action 条目
        "like_items": (like_stats or {}).get("items") if like_stats else [],
        "scrobble_items": (scrobble_stats or {}).get("items")
        if scrobble_stats
        else [],
    }
    man_path = dr.DATA_DIR / f"{MANIFEST_PREFIX}-{dr.TODAY}.json"
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"清单: {man_path}")

    if dry_run:
        log("dry-run：未写入 feedback-state.json，也未调用写接口（或未真正红心/打卡）")
    else:
        save_state(state)
        log(f"状态: {STATE_FILE}")

    log("=== 完成 ===")
    # 有硬错误则非 0
    err = 0
    if like_stats:
        err += int(like_stats.get("errors") or 0)
    if scrobble_stats:
        err += int(scrobble_stats.get("errors") or 0)
    return 1 if err else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
