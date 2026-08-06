# netease-apple-daily

把**网易云音乐「每日推荐」**同步到 **Apple Music 云端资料库**：  
每天新建（或跳过已存在的）播放列表 `网易云日推-YYYY-MM-DD`，并写入匹配到的曲目。

可选放入资料库文件夹（如 `网易云每日推荐`）。

**不下载音频、不写本地 melib。** 只调用：

1. [NeteaseCloudMusicApi-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)（Docker：`moefurina/ncm-api`）
2. [Apple Music API](https://developer.apple.com/documentation/applemusicapi)（catalog 搜索 + library 建列表）

> 仅供个人学习 / 备份用途。请遵守网易云与 Apple 服务条款，勿滥用接口或传播付费内容。

---

## 架构

```text
NetEase /recommend/songs  ──►  match  ──►  Apple Music library playlist
        (cookie)              catalog           (media-user-token)
```

| 组件 | 说明 |
|------|------|
| `docker-compose.yml` | 本机 `ncm-api`（默认 `127.0.0.1:3000`） |
| `login.sh` | 网易云扫码登录 → `data/cookie.txt` |
| `daily_recommend.py` | 匹配 + 创建资料库播放列表 |
| `run.sh` | 日常 / cron 入口 |
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

# 试跑
./run.sh
```

成功后在 iPhone / Mac **音乐** App → 资料库 → 播放列表（或你配置的文件夹）中查看。

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

相对路径均相对于**项目根目录**。

---

## 定时任务

### systemd / crontab

```cron
30 7 * * * /path/to/netease-apple-daily/run.sh >> /path/to/netease-apple-daily/logs/cron.log 2>&1
```

或摘要模式：

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
├── daily_recommend.py      # 主逻辑
├── login.sh                # 网易云登录
├── run.sh                  # 入口
├── docker-compose.yml      # ncm-api
├── config.env.example
├── scripts/cron_summary.py
├── extras/hermes/          # 可选 Hermes skill / cron 说明
├── data/                   # cookie、manifest（gitignore）
└── logs/                   # 运行日志（gitignore）
```

每次运行会写 `data/manifest-YYYY-MM-DD.json`（匹配结果与统计，不含密码）。

---

## 故障排查

| 现象 | 处理 |
|------|------|
| 缺少 cookie | `./login.sh qr` |
| storefront / 401 / 403 | 更新 `media-user-token` |
| 连不上 ncm-api | `docker compose up -d`；本机有 SOCKS 代理时需直连 `127.0.0.1`（脚本已 `unset` 代理） |
| 列表不在文件夹内 | 确认 `PLAYLIST_FOLDER_NAME`；**已建列表 API 无法移动**，请 App 内手动拖一次 |
| 大量未匹配 | 版权差异 / 改名；见当日 manifest |

---

## 致谢

- [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)
- [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi)
- Apple Music API / MusicKit

## License

[MIT](LICENSE)
