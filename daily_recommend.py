#!/usr/bin/env python3
"""
每日：网易云日推 → 匹配 Apple Music 曲库 → 在 Apple Music 资料库新建「日期」播放列表并加歌

不写本地文件；操作对象是账号云端资料库（api.music.apple.com）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


ROOT = Path(__file__).resolve().parent
load_env_file(ROOT / "config.env")

# 本机 ncm-api / Apple 公网均可直连；socks5h 代理会让 urllib 访问 127.0.0.1 失败
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)


def _resolve_path(value: str, default: Path) -> Path:
    raw = (value or "").strip()
    if not raw:
        return default
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    return p


NCM_API_BASE = os.environ.get("NCM_API_BASE", "http://127.0.0.1:3000").rstrip("/")

COOKIE_FILE = _resolve_path(
    os.environ.get("COOKIE_FILE", ""), ROOT / "data" / "cookie.txt"
)
_am_cfg = os.environ.get("AM_CONFIG", "").strip()
AM_CONFIG = (
    _resolve_path(_am_cfg, ROOT / "secrets" / "apple-music-config.yaml")
    if _am_cfg
    else ROOT / "secrets" / "apple-music-config.yaml"
)
STOREFRONT = os.environ.get("STOREFRONT", "cn")
PLAYLIST_PREFIX = os.environ.get("PLAYLIST_PREFIX", "网易云日推")
PLAYLIST_DESCRIPTION = os.environ.get(
    "PLAYLIST_DESCRIPTION", "Auto-synced from NetEase Cloud Music daily recommend"
)
# 资料库播放列表文件夹：优先 ID，否则按名称查找/创建
PLAYLIST_FOLDER_ID = os.environ.get("PLAYLIST_FOLDER_ID", "").strip()
PLAYLIST_FOLDER_NAME = os.environ.get("PLAYLIST_FOLDER_NAME", "网易云每日推荐").strip()
SKIP_IF_EXISTS = os.environ.get("SKIP_IF_EXISTS", "1") == "1"
SKIP_UNMATCHED = os.environ.get("SKIP_UNMATCHED", "1") == "1"
LOG_DIR = _resolve_path(os.environ.get("LOG_DIR", ""), ROOT / "logs")
DATA_DIR = _resolve_path(os.environ.get("DATA_DIR", ""), ROOT / "data")
# 可选：config.env 直接写 token；否则从 AM_CONFIG yaml 读 media-user-token
MUSIC_USER_TOKEN = os.environ.get("MUSIC_USER_TOKEN", "").strip()
DEVELOPER_TOKEN = os.environ.get("DEVELOPER_TOKEN", "").strip()  # 一般留空，自动抓取
# Apple Music API 限速 / 429 重试
try:
    AM_MIN_INTERVAL = float(os.environ.get("AM_MIN_INTERVAL", "0.75") or "0.75")
except ValueError:
    AM_MIN_INTERVAL = 0.75
try:
    AM_429_RETRIES = int(os.environ.get("AM_429_RETRIES", "6") or "6")
except ValueError:
    AM_429_RETRIES = 6
try:
    AM_429_BASE_WAIT = float(os.environ.get("AM_429_BASE_WAIT", "3") or "3")
except ValueError:
    AM_429_BASE_WAIT = 3.0
# 匹配时若首条搜索已高分则不再搜其它 term
MATCH_EARLY_SCORE = float(os.environ.get("MATCH_EARLY_SCORE", "90") or "90")

TODAY = date.today().isoformat()
RUN_TS = datetime.now().strftime("%Y%m%d-%H%M%S")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def http_json(
    url: str,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    body: Any = None,
    timeout: int = 30,
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            j = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            j = {"raw": raw[:800]}
        if not isinstance(j, dict):
            j = {"data": j}
        # 供 429 退避：Retry-After 秒数或 HTTP-date（我们只解析整数秒）
        try:
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra is not None:
                j["_retry_after"] = str(ra).strip()
        except Exception:
            pass
        return e.code, j


def parse_yaml_simple_value(path: Path, key: str) -> str:
    """极简 YAML 单行 key: value 读取（够用，不引 PyYAML）。"""
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"^{re.escape(key)}:\s*(.*)$", line)
        if not m:
            continue
        v = m.group(1).strip()
        if v.startswith(("'", '"')) and v.endswith(("'", '"')) and len(v) >= 2:
            v = v[1:-1]
        return v
    return ""


def get_music_user_token() -> str:
    tok = MUSIC_USER_TOKEN or parse_yaml_simple_value(AM_CONFIG, "media-user-token")
    if not tok or tok.startswith("your-"):
        raise SystemExit(
            f"缺少有效的 Music User Token。\n"
            f"请在浏览器打开 https://music.apple.com 登录后，\n"
            f"F12 → Application → Cookies → media-user-token，\n"
            f"写入 {AM_CONFIG} 的 media-user-token，或 config.env 的 MUSIC_USER_TOKEN。"
        )
    return tok


def fetch_web_developer_token() -> str:
    """从 music.apple.com 前端 JS 提取 AMPWebPlay developer JWT（会过期，每次任务刷新）。"""
    if DEVELOPER_TOKEN:
        return DEVELOPER_TOKEN
    req = urllib.request.Request(
        f"https://music.apple.com/{STOREFRONT}",
        headers={"User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    paths = list(dict.fromkeys(re.findall(r"(/assets/[^\"']+\.js)", html)))
    for p in paths:
        url = "https://music.apple.com" + p
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30
            ) as resp:
                js = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            log(f"拉取 JS 失败 {p}: {e}")
            continue
        for h in re.findall(
            r"eyJ[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}", js
        ):
            if h.count(".") != 2:
                continue
            try:
                payload = h.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                pl = json.loads(base64.urlsafe_b64decode(payload))
            except Exception:
                continue
            if pl.get("iss") == "AMPWebPlay":
                exp = pl.get("exp")
                if exp and int(exp) < time.time():
                    continue
                log(f"已获取 Apple developer token (iss=AMPWebPlay, exp={exp})")
                return h
    raise SystemExit("无法从 music.apple.com 提取 developer token，请检查网络或稍后重试")


class AppleMusicClient:
    def __init__(self, developer_token: str, user_token: str, storefront: str = "cn"):
        self.dev = developer_token
        self.user = user_token
        self.storefront = storefront
        self.base = "https://api.music.apple.com"
        self.min_interval = max(0.1, AM_MIN_INTERVAL)
        self.max_429_retries = max(0, AM_429_RETRIES)
        self._last_request_at = 0.0
        self._consecutive_429 = 0

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.dev}",
            "Music-User-Token": self.user,
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "User-Agent": UA,
        }

    def _pace(self) -> None:
        """请求间最小间隔，减轻 429。"""
        gap = self.min_interval
        # 连续撞限后临时拉大间隔
        if self._consecutive_429 > 0:
            gap = max(gap, min(5.0, self.min_interval * (1.0 + 0.5 * self._consecutive_429)))
        elapsed = time.time() - self._last_request_at
        if elapsed < gap:
            time.sleep(gap - elapsed)

    @staticmethod
    def _retry_after_seconds(data: Any, attempt: int) -> float:
        ra = None
        if isinstance(data, dict):
            ra = data.get("_retry_after")
        if ra is not None:
            try:
                return max(1.0, float(str(ra).strip()))
            except ValueError:
                pass
        # 指数退避 + 轻微抖动：3, 6, 12, 24... 封顶 60s
        base = AM_429_BASE_WAIT * (2**attempt)
        jitter = 0.25 * (attempt + 1)
        return min(60.0, base + jitter)

    def request(
        self, path: str, method: str = "GET", body: Any = None, timeout: int = 45
    ) -> tuple[int, Any]:
        last_code = 0
        last_data: Any = {}
        for attempt in range(self.max_429_retries + 1):
            self._pace()
            code, data = http_json(
                self.base + path,
                method=method,
                headers=self._headers(),
                body=body,
                timeout=timeout,
            )
            self._last_request_at = time.time()
            last_code, last_data = code, data
            if code != 429:
                if code == 200:
                    self._consecutive_429 = 0
                return code, data
            self._consecutive_429 += 1
            if attempt >= self.max_429_retries:
                break
            wait = self._retry_after_seconds(data, attempt)
            log(
                f"  Apple API 429，{wait:.1f}s 后重试 "
                f"({attempt + 1}/{self.max_429_retries}) {path[:80]}"
            )
            time.sleep(wait)
            # 撞限后略提高全局间隔，后续请求更慢
            self.min_interval = min(3.0, self.min_interval * 1.25)
        return last_code, last_data

    def storefront_info(self) -> dict[str, Any]:
        code, data = self.request("/v1/me/storefront")
        if code != 200:
            raise RuntimeError(f"storefront 失败 {code}: {data}")
        return data

    def search_songs(self, term: str, limit: int = 10) -> list[dict[str, Any]]:
        qs = urllib.parse.urlencode(
            {"term": term, "types": "songs", "limit": str(limit)}
        )
        code, data = self.request(
            f"/v1/catalog/{self.storefront}/search?{qs}"
        )
        if code == 429:
            log(f"  搜索仍 429（已重试）: {term[:60]}")
            return []
        if code != 200:
            log(f"  搜索失败 {code}: {json.dumps(data, ensure_ascii=False)[:200]}")
            return []
        return list(data.get("results", {}).get("songs", {}).get("data") or [])

    def list_library_playlists(self, limit: int = 100) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        path: Optional[str] = f"/v1/me/library/playlists?limit={min(limit, 100)}"
        while path and len(out) < limit:
            code, data = self.request(path)
            if code != 200:
                raise RuntimeError(f"列出播放列表失败 {code}: {data}")
            out.extend(data.get("data") or [])
            nxt = (data.get("next") or "").strip()
            path = nxt if nxt else None
            if path and path.startswith("http"):
                # next 可能是绝对 URL
                path = path.replace(self.base, "")
        return out

    def find_playlist_by_name(self, name: str) -> Optional[dict[str, Any]]:
        for pl in self.list_library_playlists(limit=200):
            if (pl.get("attributes") or {}).get("name") == name:
                return pl
        return None

    def list_playlist_folders(self, limit: int = 100) -> list[dict[str, Any]]:
        code, data = self.request(
            f"/v1/me/library/playlist-folders?limit={min(limit, 100)}"
        )
        if code != 200:
            raise RuntimeError(f"列出播放列表文件夹失败 {code}: {data}")
        return list(data.get("data") or [])

    def find_folder_by_name(self, name: str) -> Optional[dict[str, Any]]:
        for folder in self.list_playlist_folders(limit=100):
            if (folder.get("attributes") or {}).get("name") == name:
                return folder
        return None

    def create_playlist_folder(self, name: str) -> dict[str, Any]:
        body = {"attributes": {"name": name}}
        code, data = self.request(
            "/v1/me/library/playlist-folders", method="POST", body=body
        )
        if code not in (200, 201):
            raise RuntimeError(
                f"创建播放列表文件夹失败 {code}: {json.dumps(data, ensure_ascii=False)[:500]}"
            )
        items = data.get("data") or []
        if not items:
            raise RuntimeError(f"创建文件夹无返回 data: {data}")
        return items[0]

    def resolve_playlist_folder(self) -> Optional[dict[str, Any]]:
        """
        解析目标文件夹：PLAYLIST_FOLDER_ID > 按名称查找 > 按名称创建。
        返回 None 表示不放进文件夹（根目录）。
        """
        if PLAYLIST_FOLDER_ID:
            code, data = self.request(
                f"/v1/me/library/playlist-folders/{PLAYLIST_FOLDER_ID}"
            )
            if code == 200 and (data.get("data") or []):
                return (data.get("data") or [None])[0]
            log(f"警告: 配置的文件夹 ID 无效: {PLAYLIST_FOLDER_ID}")
        if not PLAYLIST_FOLDER_NAME:
            return None
        found = self.find_folder_by_name(PLAYLIST_FOLDER_NAME)
        if found:
            return found
        log(f"资料库无文件夹「{PLAYLIST_FOLDER_NAME}」，正在创建…")
        return self.create_playlist_folder(PLAYLIST_FOLDER_NAME)

    def folder_children(self, folder_id: str) -> list[dict[str, Any]]:
        code, data = self.request(
            f"/v1/me/library/playlist-folders/{folder_id}/children?limit=100"
        )
        if code != 200:
            return []
        return list(data.get("data") or [])

    def find_playlist_in_folder(
        self, folder_id: str, name: str
    ) -> Optional[dict[str, Any]]:
        for item in self.folder_children(folder_id):
            if item.get("type") != "library-playlists":
                continue
            if (item.get("attributes") or {}).get("name") == name:
                return item
        return None

    def create_playlist(
        self,
        name: str,
        description: str,
        track_ids: list[str],
        parent_folder_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        track_ids: catalog song ids（纯数字字符串）。
        parent_folder_id: 资料库播放列表文件夹 id，写入 relationships.parent。
        注意：parent 与 tracks 可同时传；若 parent 被忽略则先建空列表再补歌。
        """
        body: dict[str, Any] = {
            "attributes": {
                "name": name,
                "description": description,
            }
        }
        rel: dict[str, Any] = {}
        if parent_folder_id:
            rel["parent"] = {
                "data": [
                    {
                        "id": parent_folder_id,
                        "type": "library-playlist-folders",
                    }
                ]
            }
        if track_ids:
            rel["tracks"] = {
                "data": [{"id": tid, "type": "songs"} for tid in track_ids]
            }
        if rel:
            body["relationships"] = rel

        code, data = self.request("/v1/me/library/playlists", method="POST", body=body)
        if code not in (200, 201):
            # 回退：仅 parent 建空列表，再 add_tracks（部分环境同时带 tracks 时 parent 不生效）
            if parent_folder_id and track_ids:
                log("创建时 parent+tracks 失败，改为先建空列表再加歌…")
                body2: dict[str, Any] = {
                    "attributes": {"name": name, "description": description},
                    "relationships": {
                        "parent": {
                            "data": [
                                {
                                    "id": parent_folder_id,
                                    "type": "library-playlist-folders",
                                }
                            ]
                        }
                    },
                }
                code, data = self.request(
                    "/v1/me/library/playlists", method="POST", body=body2
                )
                if code not in (200, 201):
                    raise RuntimeError(
                        f"创建播放列表失败 {code}: {json.dumps(data, ensure_ascii=False)[:800]}"
                    )
                items = data.get("data") or []
                if not items:
                    raise RuntimeError(f"创建播放列表无返回 data: {data}")
                pl = items[0]
                self.add_tracks(str(pl.get("id")), track_ids)
                return pl
            raise RuntimeError(
                f"创建播放列表失败 {code}: {json.dumps(data, ensure_ascii=False)[:800]}"
            )
        items = data.get("data") or []
        if not items:
            raise RuntimeError(f"创建播放列表无返回 data: {data}")
        pl = items[0]
        # 若创建时带了 tracks 但实际为空，补加
        if track_ids and parent_folder_id:
            time.sleep(0.5)
            pid = str(pl.get("id"))
            code_t, detail = self.request(
                f"/v1/me/library/playlists/{pid}/tracks?limit=5"
            )
            have = len((detail.get("data") or [])) if code_t == 200 else -1
            if have == 0:
                log("创建后曲目为空，正在补加…")
                self.add_tracks(pid, track_ids)
        return pl

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        if not track_ids:
            return
        # 分批，避免请求过大
        for i in range(0, len(track_ids), 25):
            batch = track_ids[i : i + 25]
            body = {"data": [{"id": tid, "type": "songs"} for tid in batch]}
            code, data = self.request(
                f"/v1/me/library/playlists/{playlist_id}/tracks",
                method="POST",
                body=body,
            )
            if code not in (200, 201, 204):
                raise RuntimeError(
                    f"添加曲目失败 {code}: {json.dumps(data, ensure_ascii=False)[:500]}"
                )
            time.sleep(0.3)


def ncm_get(path: str, cookie: str, extra: Optional[dict[str, str]] = None) -> Any:
    params: dict[str, str] = {
        "cookie": cookie,
        "timestamp": str(int(time.time() * 1000)),
    }
    if extra:
        params.update(extra)
    url = f"{NCM_API_BASE}{path}?{urllib.parse.urlencode(params)}"
    code, data = http_json(url)
    if code != 200 and not isinstance(data, dict):
        raise RuntimeError(f"NCM {path} HTTP {code}")
    return data


def load_cookie() -> str:
    if not COOKIE_FILE.is_file():
        raise SystemExit(f"缺少网易云 cookie: {COOKIE_FILE}\n请运行: {ROOT}/login.sh qr")
    cookie = COOKIE_FILE.read_text(encoding="utf-8").strip()
    if not cookie:
        raise SystemExit(f"cookie 为空: {COOKIE_FILE}")
    return cookie


def ncm_alive() -> bool:
    """首页是 HTML，不走 JSON；用 banner 探测。"""
    try:
        code, data = http_json(f"{NCM_API_BASE}/banner?type=0", timeout=5)
        return code == 200 and isinstance(data, dict)
    except Exception:
        try:
            req = urllib.request.Request(
                f"{NCM_API_BASE}/", headers={"User-Agent": UA}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False


def ensure_ncm_api() -> None:
    if ncm_alive():
        return
    log("NCM API 未就绪，尝试 docker compose up -d ...")
    env = os.environ.copy()
    subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=str(ROOT),
        check=False,
        env=env,
    )
    for _ in range(40):
        if ncm_alive():
            log("NCM API 已就绪")
            return
        time.sleep(1)
    raise SystemExit(f"无法连接 NCM API: {NCM_API_BASE}")


def artists_str(ar: list[Any]) -> str:
    names = []
    for a in ar or []:
        if isinstance(a, dict) and a.get("name"):
            names.append(str(a["name"]))
        elif isinstance(a, str):
            names.append(a)
    return " / ".join(names)


@dataclass
class NeteaseSong:
    id: int
    name: str
    artists: str
    album: str
    duration_ms: int = 0

    @property
    def search_term(self) -> str:
        primary = self.artists.split(" / ")[0] if self.artists else ""
        return f"{primary} {self.name}".strip()


def fetch_daily_songs(cookie: str) -> list[NeteaseSong]:
    body = ncm_get("/recommend/songs", cookie)
    if isinstance(body, dict) and body.get("code") not in (200, None):
        raise RuntimeError(
            f"/recommend/songs 失败: {json.dumps(body, ensure_ascii=False)[:500]}"
        )
    data = body.get("data") or body
    daily = data.get("dailySongs") or data.get("recommend") or []
    songs: list[NeteaseSong] = []
    for item in daily:
        song = item.get("song") if isinstance(item.get("song"), dict) else item
        name = str(song.get("name") or "").strip()
        if not name:
            continue
        ar = song.get("ar") or song.get("artists") or []
        al = song.get("al") or song.get("album") or {}
        album = al.get("name", "") if isinstance(al, dict) else str(al or "")
        songs.append(
            NeteaseSong(
                id=int(song.get("id") or 0),
                name=name,
                artists=artists_str(ar),
                album=str(album or ""),
                duration_ms=int(song.get("dt") or song.get("duration") or 0),
            )
        )
    return songs


def normalize_title(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\(（\[【].*?[\)）\]】]", "", s)
    s = re.sub(r"[·・\.\,\!\?\:\;\'\"“”‘’]", "", s)
    return s.strip()


def score_match(ns: NeteaseSong, song: dict[str, Any]) -> float:
    attrs = song.get("attributes") or {}
    t = normalize_title(str(attrs.get("name") or ""))
    a = normalize_title(str(attrs.get("artistName") or ""))
    al = normalize_title(str(attrs.get("albumName") or ""))
    nt = normalize_title(ns.name)
    na = normalize_title(ns.artists.split(" / ")[0] if ns.artists else "")
    nal = normalize_title(ns.album)
    if not t or not nt:
        return 0.0
    score = 0.0
    if t == nt:
        score += 50
    elif nt in t or t in nt:
        score += 30
    else:
        score += min(20, len(set(t) & set(nt)) * 2)
    if na:
        if a == na:
            score += 40
        elif na in a or a in na:
            score += 25
        else:
            for part in re.split(r"[,/&]| featuring | feat\.| ft\.", a):
                part = part.strip()
                if part and (part == na or na in part or part in na):
                    score += 20
                    break
    if nal and al:
        if al == nal:
            score += 10
        elif nal in al or al in nal:
            score += 5
    dur = int(attrs.get("durationInMillis") or 0)
    if ns.duration_ms and dur:
        diff = abs(ns.duration_ms - dur)
        if diff <= 3000:
            score += 8
        elif diff <= 8000:
            score += 4
    return score


def match_catalog(am: AppleMusicClient, ns: NeteaseSong) -> Optional[dict[str, Any]]:
    terms = [
        ns.search_term,
        f"{ns.name} {ns.artists.split(' / ')[0]}" if ns.artists else ns.name,
        ns.name,
    ]
    # 去重并去掉与 search_term 相同的重复查询
    uniq_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term = term.strip()
        if not term or term in seen:
            continue
        seen.add(term)
        uniq_terms.append(term)

    candidates: list[dict[str, Any]] = []
    best: Optional[tuple[float, dict[str, Any]]] = None

    def consider(songs: list[dict[str, Any]]) -> None:
        nonlocal best
        for song in songs:
            sc = score_match(ns, song)
            if best is None or sc > best[0]:
                best = (sc, song)

    for i, term in enumerate(uniq_terms):
        batch = am.search_songs(term, limit=10)
        candidates.extend(batch)
        consider(batch)
        # 高分命中：停止后续关键词
        if best and best[0] >= MATCH_EARLY_SCORE:
            break
        # 已有不错匹配（>=70）且已搜过 ≥2 个 term：跳过更模糊的纯歌名
        if i >= 1 and best and best[0] >= 70:
            break
        # 连续 429 耗尽重试后：多歇一会再继续
        if not batch and am._consecutive_429 > 0:
            time.sleep(min(8.0, 1.5 * am._consecutive_429))

    if not best or best[0] < 55:
        return None
    song = best[1]
    attrs = song.get("attributes") or {}
    return {
        "id": str(song.get("id")),
        "name": attrs.get("name"),
        "artistName": attrs.get("artistName"),
        "albumName": attrs.get("albumName"),
        "url": attrs.get("url"),
        "score": best[0],
    }


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"daily-{TODAY}.log"

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

    log(f"=== 网易云日推 → Apple Music 资料库 {TODAY} ({RUN_TS}) ===")
    ensure_ncm_api()
    cookie = load_cookie()

    try:
        st = ncm_get("/login/status", cookie)
        profile = (st.get("data") or {}).get("profile") or {}
        log(f"网易云账号: {profile.get('nickname') or '?'}")
    except Exception as e:
        log(f"警告: 登录状态检查失败: {e}")

    songs = fetch_daily_songs(cookie)
    log(f"日推曲目数: {len(songs)}")
    if not songs:
        log("今日无推荐，结束")
        return 1

    user_token = get_music_user_token()
    dev_token = fetch_web_developer_token()
    am = AppleMusicClient(dev_token, user_token, STOREFRONT)

    sf = am.storefront_info()
    sf_id = (sf.get("data") or [{}])[0].get("id")
    log(f"Apple Music storefront: {sf_id}")

    folder = am.resolve_playlist_folder()
    folder_id = str(folder.get("id")) if folder else None
    folder_name = (
        (folder.get("attributes") or {}).get("name") if folder else None
    ) or PLAYLIST_FOLDER_NAME or ""
    if folder_id:
        log(f"目标文件夹: 「{folder_name}」(id={folder_id})")
    else:
        log("未配置文件夹，播放列表将建在资料库根目录")

    playlist_name = f"{PLAYLIST_PREFIX}-{TODAY}"
    existing = None
    if folder_id:
        existing = am.find_playlist_in_folder(folder_id, playlist_name)
    if not existing:
        existing = am.find_playlist_by_name(playlist_name)
    if existing and SKIP_IF_EXISTS:
        log(f"资料库已存在播放列表「{playlist_name}」(id={existing.get('id')})，跳过")
        man = {
            "date": TODAY,
            "skipped": True,
            "folder": {"id": folder_id, "name": folder_name} if folder_id else None,
            "playlist": {
                "id": existing.get("id"),
                "name": playlist_name,
                "href": (existing.get("attributes") or {}).get("url"),
            },
        }
        (DATA_DIR / f"manifest-{TODAY}.json").write_text(
            json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0

    results: list[dict[str, Any]] = []
    track_ids: list[str] = []
    for i, ns in enumerate(songs, 1):
        log(f"[{i}/{len(songs)}] {ns.artists} - {ns.name}")
        m = match_catalog(am, ns)
        row: dict[str, Any] = {
            "netease": asdict(ns),
            "match": m,
        }
        if not m:
            log("  未匹配")
            row["status"] = "unmatched"
            results.append(row)
            if not SKIP_UNMATCHED:
                return 2
            continue
        log(
            f"  → {m['artistName']} - {m['name']} [{m['albumName']}] "
            f"id={m['id']} score={m['score']:.0f}"
        )
        row["status"] = "matched"
        track_ids.append(m["id"])
        results.append(row)

    # 去重保序
    seen_ids: set[str] = set()
    unique_ids: list[str] = []
    for tid in track_ids:
        if tid not in seen_ids:
            seen_ids.add(tid)
            unique_ids.append(tid)

    where = f"文件夹「{folder_name}」" if folder_id else "资料库根目录"
    log(
        f"匹配成功 {len(unique_ids)}/{len(songs)}，"
        f"在{where}创建播放列表「{playlist_name}」…"
    )
    desc = f"{PLAYLIST_DESCRIPTION} | {TODAY} | {len(unique_ids)} 首"
    pl = am.create_playlist(
        playlist_name, desc, unique_ids, parent_folder_id=folder_id
    )
    pl_id = pl.get("id")
    log(f"已创建: id={pl_id} name={(pl.get('attributes') or {}).get('name')}")
    if folder_id:
        in_folder = None
        for wait in (0.5, 1.5, 3.0):
            time.sleep(wait)
            in_folder = am.find_playlist_in_folder(folder_id, playlist_name)
            if in_folder:
                break
        if in_folder:
            log(f"已确认位于文件夹「{folder_name}」内")
        else:
            log(
                f"警告: 创建后暂未在文件夹「{folder_name}」中索引到该列表 "
                "（偶发延迟）；请稍后在 App 中刷新确认。"
                "若仍在根目录，可手动拖入（API 无法移动已有列表）。"
            )
    # 若创建时未带上全部 tracks（部分环境忽略 relationships），再 POST 一次
    if unique_ids:
        try:
            # 仅当创建 body 可能被忽略时补加；重复添加可能导致重复曲目，故先读 tracks 数
            code, detail = am.request(
                f"/v1/me/library/playlists/{pl_id}/tracks?limit=100"
            )
            have = len((detail.get("data") or [])) if code == 200 else -1
            log(f"播放列表当前曲目数: {have}")
            if have == 0 and unique_ids:
                log("创建时未写入曲目，正在追加…")
                am.add_tracks(str(pl_id), unique_ids)
                code, detail = am.request(
                    f"/v1/me/library/playlists/{pl_id}/tracks?limit=100"
                )
                have = len((detail.get("data") or [])) if code == 200 else -1
                log(f"追加后曲目数: {have}")
        except Exception as e:
            log(f"校验/补加曲目时出错（播放列表可能已含歌）: {e}")

    man = {
        "date": TODAY,
        "run_ts": RUN_TS,
        "folder": {"id": folder_id, "name": folder_name} if folder_id else None,
        "playlist": {
            "id": pl_id,
            "name": playlist_name,
            "attributes": pl.get("attributes"),
        },
        "stats": {
            "total": len(songs),
            "matched": len(unique_ids),
            "unmatched": sum(1 for r in results if r.get("status") == "unmatched"),
        },
        "tracks": results,
    }
    man_path = DATA_DIR / f"manifest-{TODAY}.json"
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"清单: {man_path}")
    log(f"统计: {json.dumps(man['stats'], ensure_ascii=False)}")
    if folder_name:
        log(
            f"请在「音乐」App → 资料库 → 播放列表 → 文件夹「{folder_name}」中查看"
        )
    else:
        log("请在 iPhone / Mac「音乐」App → 资料库 → 播放列表 中查看")
    log("=== 完成 ===")
    return 0 if unique_ids else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("中断", file=sys.stderr)
        raise SystemExit(130)
