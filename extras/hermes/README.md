# Hermes Agent 集成（可选）

若你使用 [Hermes](https://github.com/NousResearch/hermes-agent)（或同类 agent + cron）：

## 1. Skill

将本目录下的 `SKILL.md` 复制到 Hermes skills，例如：

```bash
mkdir -p ~/.hermes/skills/media/netease-apple-daily
cp extras/hermes/SKILL.md ~/.hermes/skills/media/netease-apple-daily/
```

## 2. Cron 脚本

```bash
# 方式 A：直接用仓库脚本（推荐）
export NETEASE_APPLE_DAILY_ROOT=/path/to/netease-apple-daily
# hermes cron create '30 7 * * *' --name '网易云日推→Apple' \
#   --script /path/to/netease-apple-daily/scripts/cron_summary.py \
#   --no-agent --deliver telegram:YOUR_CHAT_ID

# 方式 B：包装进 ~/.hermes/scripts/
ln -sf /path/to/netease-apple-daily/scripts/cron_summary.py \
  ~/.hermes/scripts/netease_apple_daily.py
```

`cron_summary.py` 为 `--no-agent` 模式：stdout 即投递正文。

## 3. 登录

对 Hermes 说「网易云日推登录」，按 skill：`qr-init` → 发二维码图 → `qr-poll`。
