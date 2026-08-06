# netease-apple-daily

双向同步（可选）：

1. **正向**：网易云「每日推荐」→ Apple Music 云端资料库日期播放列表  
2. **回写**：Apple Music **喜爱歌曲** + **最近播放** → 网易云 **红心** / **听歌打卡**，帮助网易云更好推荐  

可选把日推列表放入资料库文件夹（如 `网易云每日推荐`）。

**不下载音频、不写本地 melib。** 只调用：

1. [NeteaseCloudMusicApi-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)（Docker：`moefurina/ncm-api`）
2. [Apple Music API](https://developer.apple.com/documentation/applemusicapi)（catalog / library / recent / 资料库歌单）

> 仅供个人学习 / 备份用途。请遵守网易云与 Apple 服务条款，勿滥用接口或传播付费内容。回写会改动网易云账号数据，请先 `--dry-run`。

---

## 架构

```text
NetEase /recommend/songs  ──►  match  ──►  Apple Music library playlist
        (cookie)              catalog           (media-user-token)

Apple 喜爱歌曲 / 最近播放  ──►  match  ──►  NetEase /like + /scrobble
        (media-user-token)      cloudsearch           (cookie)
```

| 组件 | 说明 |
|------|------|
| `docker-compose.yml` | 本机 `ncm-api`（默认 `127.0.0.1:3000`） |
| `login.sh` | 网易云扫码登录 → `data/cookie.txt` |
| `daily_recommend.py` | 日推 → Apple 资料库播放列表 |
| `apple_to_netease_feedback.py` | Apple 喜爱/最近播放 → 网易云红心/打卡 |
| `run.sh` / `run_feedback.sh` | 正向 / 回写入口 |
| `scripts/cron_summary.py` | 给 cron / 机器人用的短摘要输出 |

---

## 依赖

- Docker（跑 api-enhanced）
- Python 3.10+（仅标准库）
- `curl`、`jq`（登录脚本）
- 有效的 **Apple Music 订阅**
- 网易云账号（日推需登录）

---

## 快速开始

```bash
git clone https://github.com/FlyRenxing/netease-apple-daily.git
cd netease-apple-daily

cp config.env.example config.env
chmod 600 config.env
# 编辑 config.env：至少配置 MUSIC_USER_TOKEN 或 AM_CONFIG

docker compose up -d

# 网易云扫码登录
./login.sh qr
# 或分步（适合 bot）：./login.sh qr-init  → 扫码 → ./login.sh qr-poll

# 试跑：日推 → Apple
./run.sh

# 试跑：Apple → 网易云（务必先 dry-run）
./run_feedback.sh --dry-run --limit 5
# 确认匹配无误后再真实写入
./run_feedback.sh
```

成功后在 iPhone / Mac **音乐** App → 资料库 → 播放列表（或你配置的文件夹）中查看日推列表；回写结果见网易云「我喜欢的音乐」与听歌排行。

---

## Apple Music 凭证

| 凭证 | 获取方式 |
|------|----------|
| **Music User Token** | 浏览器打开 [music.apple.com](https://music.apple.com) 登录 → F12 → Application → Cookies → `media-user-token` |
| **Developer JWT** | 默认每次从 music.apple.com 前端 JS 提取（`iss=AMPWebPlay`）；也可自备正式 MusicKit developer token |

写入方式二选一：

```bash
# config.env
MUSIC_USER_TOKEN=你的media-user-token
```

或：

```yaml
# secrets/apple-music-config.yaml（示例，勿提交）
media-user-token: "..."
```

```bash
# config.env
AM_CONFIG=./secrets/apple-music-config.yaml
```

---

## 配置说明（`config.env`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `NCM_API_BASE` | `http://127.0.0.1:3000` | api-enhanced 地址 |
| `COOKIE_FILE` | `./data/cookie.txt` | 网易云 cookie |
| `MUSIC_USER_TOKEN` / `AM_CONFIG` | — | Apple 用户 token |
| `STOREFRONT` | `cn` | Apple 商店区域 |
| `PLAYLIST_PREFIX` | `网易云日推` | 列表名 = `{PREFIX}-{日期}` |
| `PLAYLIST_FOLDER_NAME` | `网易云每日推荐` | 目标文件夹（可空） |
| `PLAYLIST_FOLDER_ID` | — | 文件夹 ID（优先于名称） |
| `SKIP_IF_EXISTS` | `1` | 当日同名列表存在则跳过 |
| `SKIP_UNMATCHED` | `1` | 个别曲未匹配仍继续 |
| `FEEDBACK_SYNC_LIKES` | `1` | 喜爱 → 红心 |
| `FEEDBACK_SYNC_SCROBBLE` | `1` | 最近播放增量 → scrobble |
| `FEEDBACK_DRY_RUN` | `0` | `1` 则只匹配不写 |
| `FEEDBACK_FAVORITES_PLAYLIST_NAME` | `喜爱歌曲` | Apple 喜爱歌单名（或设 ID） |
| `FEEDBACK_RECENT_LIMIT` | `50` | 读取最近播放条数 |
| `FEEDBACK_MAX_LIKES_PER_RUN` | `40` | 每轮最多红心数 |
| `FEEDBACK_MAX_SCROBBLE_PER_RUN` | `25` | 每轮最多打卡数 |
| `FEEDBACK_MIN_SCORE` | `55` | 反查匹配最低分 |
| `FEEDBACK_SCROBBLE_SEED_ONLY_FIRST` | `1` | 首次只建最近播放快照、不打卡 |

相对路径均相对于**项目根目录**。

---

## 回写说明（Apple → 网易云）

| 源（Apple Music API） | 目标（网易云） | 说明 |
|----------------------|----------------|------|
| 资料库歌单「喜爱歌曲」 | `/like` | 与已有红心 / `feedback-state.json` 去重 |
| `/v1/me/recent/played/tracks` | `/scrobble` | **增量**：相对上次快照的新曲；**无播放时间戳** |

限制与策略：

- Apple 最近播放 **没有时间戳 / 次数**，不能做精确 scrobble；只能「窗口里新出现的曲」打卡。  
- **首次 scrobble 默认只写快照**，避免把历史窗口一次灌进网易云；需要可加 `--force-scrobble-seed`（不推荐）。  
- 曲目匹配靠 **歌名 + 艺人 + 时长**（ISRC 在网易云搜索不可靠），误匹配可能红心错歌 → 先 `--dry-run`。  
- 状态文件：`data/feedback-state.json`；清单：`data/feedback-manifest-YYYY-MM-DD.json`。

```bash
./run_feedback.sh --dry-run              # 全量预览（受 MAX_* 限制）
./run_feedback.sh --likes-only           # 只红心
./run_feedback.sh --scrobble-only        # 只打卡
./run_feedback.sh --limit 10             # 本轮每类最多 10 首
```

---

## 定时任务

### systemd / crontab

```cron
30 7 * * * /path/to/netease-apple-daily/run.sh >> /path/to/netease-apple-daily/logs/cron.log 2>&1
# 回写可更勤（示例每 6 小时），与日推错开
0 */6 * * * /path/to/netease-apple-daily/run_feedback.sh >> /path/to/netease-apple-daily/logs/cron.log 2>&1
```

或摘要模式（日推）：

```cron
30 7 * * * /usr/bin/python3 /path/to/netease-apple-daily/scripts/cron_summary.py
```

### Hermes Agent（可选）

见 [`extras/hermes/`](extras/hermes/README.md)。

---

## 登录脚本

```bash
./login.sh qr              # 终端一体化扫码
./login.sh qr-init         # 只生成二维码（data/login-qr.png）
./login.sh qr-poll [N]     # 轮询扫码结果
./login.sh status          # 检查登录态
./login.sh cookie 'MUSIC_U=...; __csrf=...'
```

Cookie 失效后重新扫码即可。

---

## 目录结构

```text
.
├── daily_recommend.py              # 日推 → Apple
├── apple_to_netease_feedback.py    # Apple → 网易云反馈
├── login.sh                        # 网易云登录
├── run.sh / run_feedback.sh        # 入口
├── docker-compose.yml              # ncm-api
├── config.env.example
├── scripts/cron_summary.py
├── extras/hermes/                  # 可选 Hermes skill / cron 说明
├── data/                           # cookie、manifest、feedback-state（gitignore）
└── logs/
```

- 日推：`data/manifest-YYYY-MM-DD.json`  
- 回写：`data/feedback-manifest-YYYY-MM-DD.json`、`data/feedback-state.json`  

---

## 故障排查

| 现象 | 处理 |
|------|------|
| 缺少 cookie | `./login.sh qr` |
| storefront / 401 / 403 | 更新 `media-user-token` |
| 连不上 ncm-api | `docker compose up -d`；本机有 SOCKS 代理时需直连 `127.0.0.1`（脚本已 `unset` 代理） |
| 列表不在文件夹内 | 确认 `PLAYLIST_FOLDER_NAME`；**已建列表 API 无法移动**，请 App 内手动拖一次 |
| 大量未匹配 | 版权差异 / 改名；见当日 manifest |
| 找不到喜爱歌单 | 设置 `FEEDBACK_FAVORITES_PLAYLIST_ID` 或改 `FEEDBACK_FAVORITES_PLAYLIST_NAME` |
| scrobble 一直 seed_only | 正常：首跑只建快照；再跑一次才会打新增曲 |

---

## 致谢

- [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)
- [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi)
- Apple Music API / MusicKit

## License

[MIT](LICENSE)
