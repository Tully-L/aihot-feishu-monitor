# AI HOT（数字卡兹克）飞书实时提醒

监控 `https://aihot.virxact.com/` 的公开 API，有新条目时以 `AI HOT（数字卡兹克）` 名称发送到飞书群自定义机器人。

## 方案

- 数据源：优先轮询 `https://aihot.virxact.com/api/public/fingerprint`。
- 拉取：只有 fingerprint 变化时才请求 `/api/public/items`。
- 去重：本地 `state.json` 记录已处理 item id，避免重复提醒。
- 频率：默认 60 秒一次。AI HOT OpenAPI 明确建议轮询间隔 >= 60 秒。
- 飞书：使用群自定义机器人 webhook，可选加签 secret。不需要飞书账号密码。

## 需要你提供

必须提供：

- `FEISHU_WEBHOOK_URL`：目标飞书群的自定义机器人 webhook。

仅当机器人开启“签名校验”时提供：

- `FEISHU_WEBHOOK_SECRET`：该机器人的加签密钥。

不需要提供：

- 飞书账号密码
- AI HOT 账号密码
- GitHub 或其他账号

如果你希望发个人私聊而不是群机器人，再改成飞书开放平台应用发送消息，那时才需要 `app_id`、`app_secret`、目标 `open_id/chat_id` 和对应 IM 权限。

## 本地运行

```bash
git clone <repo-url>
cd aihot-feishu-monitor
cp .env.example .env
```

编辑 `.env`，填入飞书 webhook 和可选 secret。
如果本机已有 `~/.claude/.feishu-webhook`，可以不填 `FEISHU_WEBHOOK_URL`；脚本会自动读取这个文件作为兜底。

先测试飞书通道：

```bash
python3 aihot_feishu_monitor.py --env-file .env --test-send
```

初始化监控基线：

```bash
python3 aihot_feishu_monitor.py --env-file .env --once
```

持续运行：

```bash
python3 aihot_feishu_monitor.py --env-file .env --loop
```

未配置 webhook 时可 dry-run：

```bash
python3 aihot_feishu_monitor.py --env-file .env --once --dry-run
```

## launchd 常驻

模板在 `launchd/com.tully.aihot-feishu-monitor.plist.example`。
模板里当前 Python 路径是 `/usr/bin/python3`，使用系统证书链访问 HTTPS，并通过 `-u` 让日志实时写入。

使用前先把 plist 里的项目路径改成你自己的 clone 路径：

- `/Users/tully/01work/aihot-feishu-monitor/aihot_feishu_monitor.py`
- `/Users/tully/01work/aihot-feishu-monitor/.env`
- `/Users/tully/01work/aihot-feishu-monitor`

```bash
mkdir -p ~/Library/LaunchAgents ~/.aihot-feishu-monitor
cp launchd/com.tully.aihot-feishu-monitor.plist.example ~/Library/LaunchAgents/com.tully.aihot-feishu-monitor.plist
# 先编辑 ~/Library/LaunchAgents/com.tully.aihot-feishu-monitor.plist 里的路径
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tully.aihot-feishu-monitor.plist
launchctl kickstart -k gui/$(id -u)/com.tully.aihot-feishu-monitor
```

查看日志：

```bash
tail -f ~/.aihot-feishu-monitor/monitor.log ~/.aihot-feishu-monitor/monitor.err.log
```

停止：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.tully.aihot-feishu-monitor.plist
```

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile aihot_feishu_monitor.py tests/test_monitor.py
```

## 安全说明

- 不要提交 `.env`、webhook、secret、运行日志或 state 文件。
- 项目默认 `.gitignore` 已排除这些本地运行文件。
- AI HOT 公开 API 要求脚本使用可识别的非浏览器 User-Agent，生产环境请把 `.env` 里的 `AIHOT_USER_AGENT` 改成自己的标识。
