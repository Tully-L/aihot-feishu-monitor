# AGENTS.md

## Project

This repository contains a small Python monitor for AI HOT public updates. It polls `https://aihot.virxact.com/api/public/fingerprint`, fetches `/api/public/items` only when the stream changes, and sends Feishu custom-bot card alerts.

## Commands

Run tests before changing behavior:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile aihot_feishu_monitor.py tests/test_monitor.py
```

Dry-run a card without sending to Feishu:

```bash
python3 aihot_feishu_monitor.py --test-send --dry-run
```

## Safety

- Never commit `.env`, Feishu webhook URLs, signing secrets, local logs, or state files.
- Keep `.env.example` free of real credentials.
- The script must keep a non-browser `AIHOT_USER_AGENT`; AI HOT public API may reject generic browser-like agents.
- First real monitor run should seed state and avoid pushing historical items.

## Style

- Use only the Python standard library unless there is a strong reason to add a dependency.
- Keep the script runnable with `/usr/bin/python3` on macOS for launchd usage.
- Preserve the card behavior: item titles in the message body are not links; website navigation belongs in the bottom button labeled `进入网站`.
