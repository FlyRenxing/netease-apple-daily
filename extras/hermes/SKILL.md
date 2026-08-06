---
name: netease-apple-daily
description: >
  Sync NetEase Cloud Music daily recommend songs into Apple Music library playlists
  (date-named, optional folder). Use for daily run, QR login, media-user-token refresh,
  and logs. Triggers: 网易云日推, 网易云推荐, Apple Music 播放列表, netease-apple-daily, 日推登录
metadata:
  short-description: "网易云日推 → Apple Music 资料库"
  hermes:
    tags: [music, netease, apple-music, cron]
---

# 网易云日推 → Apple Music 资料库

**项目根：** 环境变量 `NETEASE_APPLE_DAILY_ROOT`，或用户部署路径（clone 后的仓库目录）。

**做什么：** 拉网易云「每日推荐」→ 匹配 Apple Music 目录 → 在云端资料库创建  
`网易云日推-YYYY-MM-DD`（可进文件夹）。**不下载本地文件。**

| 依赖 | 说明 |
|------|------|
| api-enhanced | Docker `ncm-api` → `127.0.0.1:3000` |
| 网易云 cookie | `$ROOT/data/cookie.txt` |
| Apple Music | `MUSIC_USER_TOKEN` 或 `AM_CONFIG` 中的 `media-user-token` |
| 定时 | `scripts/cron_summary.py`（`--no-agent`） |

---

## 日常跑一次

```bash
export NETEASE_APPLE_DAILY_ROOT=/path/to/netease-apple-daily
python3 "$NETEASE_APPLE_DAILY_ROOT/scripts/cron_summary.py"
# 或
"$NETEASE_APPLE_DAILY_ROOT/run.sh"
```

---

## 网易云登录

分两步，**不要**长时间阻塞空等：

### 1. 生成二维码并发给用户

```bash
cd "$NETEASE_APPLE_DAILY_ROOT"
docker compose up -d
./login.sh qr-init
```

- 图片：`data/login-qr.png` → **真正作为图片消息发出**（不要只说路径）
- 备选：`data/login-qrurl.txt` 链接

### 2. 用户确认后轮询

```bash
./login.sh qr-poll 90
```

- `LOGIN_OK` → 再跑 `cron_summary.py`
- `QR_EXPIRED` / `QR_TIMEOUT` → 重新 `qr-init`

---

## Apple media-user-token

1. https://music.apple.com 登录  
2. F12 → Cookies → `media-user-token`  
3. 写入 `config.env` 的 `MUSIC_USER_TOKEN` 或 yaml 的 `media-user-token`  
4. **不要在对话中回显完整 token**

---

## 约束

1. 同步用仓库脚本，勿手写 API 调用。  
2. 登录：发 QR → 等用户「已扫」→ `qr-poll`。  
3. 密钥不进对话。  
4. 与「本地 ALAC 下载」类 skill 区分：本 skill 只写**云端资料库播放列表**。
