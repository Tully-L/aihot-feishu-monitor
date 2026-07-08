#!/usr/bin/env python3
"""Monitor AI HOT public updates and send Feishu custom-bot alerts."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import hmac
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://aihot.virxact.com"
DEFAULT_USER_AGENT = "aihot-feishu-monitor/0.1 (+local)"
DEFAULT_STATE_FILE = "~/.aihot-feishu-monitor/state.json"
DEFAULT_FEISHU_WEBHOOK_FILE = "~/.claude/.feishu-webhook"
BRAND_NAME = "AI HOT（数字卡兹克）"
STATE_SEEN_LIMIT = 2000
CARD_MARKDOWN_LIMIT = 24000
CARD_HEADER_LIMIT = 90
CARD_HEADER_ITEM_LIMIT = 34
CATEGORY_LABELS = {
    "ai-models": "模型",
    "ai-products": "产品",
    "industry": "行业",
    "paper": "论文",
    "tip": "技巧",
}


class NotModified(Exception):
    pass


class ConfigError(Exception):
    pass


@dataclasses.dataclass
class JsonResponse:
    body: Any
    headers: dict[str, str]
    status: int


@dataclasses.dataclass
class Config:
    base_url: str
    mode: str
    category: str | None
    take: int
    interval: int
    state_file: Path
    webhook_url: str | None
    webhook_secret: str | None
    user_agent: str
    timeout: int
    max_notify: int
    dry_run: bool


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def read_first_line(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        value = line.strip()
        if value:
            return value
    return None


def resolve_webhook_url(cli_value: str | None) -> str | None:
    if cli_value:
        return cli_value
    env_value = os.environ.get("FEISHU_WEBHOOK_URL")
    if env_value:
        return env_value
    webhook_file = Path(
        os.environ.get("FEISHU_WEBHOOK_FILE", DEFAULT_FEISHU_WEBHOOK_FILE)
    ).expanduser()
    return read_first_line(webhook_file)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor AI HOT public API and send Feishu custom-bot alerts."
    )
    parser.add_argument("--env-file", type=Path, help="Optional .env file to load first.")
    parser.add_argument("--loop", action="store_true", help="Poll forever.")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit.")
    parser.add_argument("--test-send", action="store_true", help="Send one test alert and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts without sending Feishu messages.")
    parser.add_argument("--mode", choices=("selected", "all"), help="AI HOT stream to monitor.")
    parser.add_argument(
        "--category",
        choices=tuple(CATEGORY_LABELS),
        help="Optional category filter for items API.",
    )
    parser.add_argument("--take", type=int, help="Items to fetch when the stream changes, 1-100.")
    parser.add_argument("--interval", type=int, help="Polling interval in seconds. AI HOT recommends >= 60.")
    parser.add_argument("--state-file", type=Path, help="State JSON path.")
    parser.add_argument("--webhook-url", help="Feishu custom bot webhook URL.")
    parser.add_argument("--webhook-secret", help="Feishu custom bot signing secret, if enabled.")
    parser.add_argument("--user-agent", help="Non-browser User-Agent for AI HOT public API.")
    parser.add_argument("--timeout", type=int, help="HTTP timeout in seconds.")
    parser.add_argument("--max-notify", type=int, help="Maximum items to notify per cycle.")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    if args.env_file:
        load_env_file(args.env_file.expanduser())

    state_file = (
        args.state_file
        or Path(os.environ.get("AIHOT_STATE_FILE", DEFAULT_STATE_FILE))
    ).expanduser()
    mode = args.mode or os.environ.get("AIHOT_MODE", "selected")
    if mode not in {"selected", "all"}:
        raise ConfigError("AIHOT_MODE must be selected or all")

    category = args.category or os.environ.get("AIHOT_CATEGORY") or None
    if category and category not in CATEGORY_LABELS:
        raise ConfigError(f"AIHOT_CATEGORY must be one of: {', '.join(CATEGORY_LABELS)}")

    take = args.take if args.take is not None else env_int("AIHOT_TAKE", 50)
    if not 1 <= take <= 100:
        raise ConfigError("take must be between 1 and 100")

    interval = args.interval if args.interval is not None else env_int("AIHOT_POLL_INTERVAL_SECONDS", 60)
    if interval < 60:
        raise ConfigError("interval must be >= 60 seconds to respect AI HOT public API guidance")

    timeout = args.timeout if args.timeout is not None else env_int("HTTP_TIMEOUT_SECONDS", 15)
    if timeout <= 0:
        raise ConfigError("timeout must be positive")

    max_notify = args.max_notify if args.max_notify is not None else env_int("MAX_NOTIFY_PER_CYCLE", 8)
    if max_notify <= 0:
        raise ConfigError("max-notify must be positive")

    return Config(
        base_url=(os.environ.get("AIHOT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        mode=mode,
        category=category,
        take=take,
        interval=interval,
        state_file=state_file,
        webhook_url=resolve_webhook_url(args.webhook_url),
        webhook_secret=(
            args.webhook_secret
            or os.environ.get("FEISHU_WEBHOOK_SECRET")
            or os.environ.get("FEISHU_BOT_SECRET")
            or None
        ),
        user_agent=args.user_agent or os.environ.get("AIHOT_USER_AGENT") or DEFAULT_USER_AGENT,
        timeout=timeout,
        max_notify=max_notify,
        dry_run=args.dry_run,
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "fingerprints": {},
            "fingerprint_etags": {},
            "seen_ids_by_stream": {},
            "last_fingerprints_by_stream": {},
            "created_at": utc_now_iso(),
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"State file is invalid JSON: {path}") from exc


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def http_json(
    url: str,
    *,
    user_agent: str,
    timeout: int,
    headers: dict[str, str] | None = None,
) -> JsonResponse:
    req_headers = {
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return JsonResponse(
                body=json.loads(raw) if raw else None,
                headers={key.lower(): value for key, value in resp.headers.items()},
                status=resp.status,
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            raise NotModified
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed: HTTP {exc.code} {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: int,
    user_agent: str,
) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": user_agent,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST Feishu webhook failed: HTTP {exc.code} {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"POST Feishu webhook failed: {exc.reason}") from exc


def feishu_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def format_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%m-%d %H:%M")
    except ValueError:
        return value


def trim_text(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def item_line(item: dict[str, Any]) -> str:
    title = trim_text(item.get("title"), 120)
    source = trim_text(item.get("source"), 60)
    category = CATEGORY_LABELS.get(item.get("category"), item.get("category") or "未知")
    score = item.get("score")
    published = format_time(item.get("publishedAt"))
    summary = (item.get("summary") or "").strip()
    meta_parts = [part for part in [published, category, source, f"score {score}" if score is not None else ""] if part]
    meta = " · ".join(meta_parts)
    if summary:
        return f"**{title}**\n{meta}\n\n{summary}"
    return f"**{title}**\n{meta}"


def fit_markdown(content: str, limit: int = CARD_MARKDOWN_LIMIT) -> str:
    if len(content) <= limit:
        return content
    suffix = "\n\n内容过长，卡片已保留前段；可点下方按钮进入网站。"
    return content[: max(0, limit - len(suffix) - 1)].rstrip() + "…" + suffix


def total_item_count(items: list[dict[str, Any]], truncated_count: int = 0) -> int:
    return len(items) + max(0, truncated_count)


def item_title(item: dict[str, Any], limit: int) -> str:
    return trim_text(item.get("title") or "AI HOT 更新", limit)


def card_header_title(items: list[dict[str, Any]], truncated_count: int = 0) -> str:
    total = total_item_count(items, truncated_count)
    if not items:
        return f"{BRAND_NAME}更新提醒"
    if total == 1:
        return trim_text(item_title(items[0], CARD_HEADER_LIMIT), CARD_HEADER_LIMIT)

    title_parts = [item_title(item, CARD_HEADER_ITEM_LIMIT) for item in items[:3]]
    title = " + ".join(title_parts)
    if total > len(title_parts):
        title = f"{title} 等 {total} 条"
    return trim_text(title, CARD_HEADER_LIMIT)


def card_status_line(items: list[dict[str, Any]], truncated_count: int = 0) -> str:
    total = total_item_count(items, truncated_count)
    if total <= 0:
        return f"**{BRAND_NAME}更新提醒**"
    return f"**{BRAND_NAME}新增 {total} 条**"


def feed_url(mode: str) -> str:
    return f"{DEFAULT_BASE_URL}/" if mode == "selected" else f"{DEFAULT_BASE_URL}/all"


def button(text: str, url: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "url": url,
        "type": "default",
    }


def action_buttons(items: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    if len(items) == 1:
        item = items[0]
        actions = []
        permalink = item.get("permalink")
        source_url = item.get("url")
        if permalink:
            actions.append(button("进入网站", permalink))
        if source_url and source_url != permalink:
            actions.append(button("打开原文", source_url))
        if actions:
            return actions[:2]
    return [button("进入网站", feed_url(mode))]


def build_card(items: list[dict[str, Any]], *, mode: str, truncated_count: int = 0) -> dict[str, Any]:
    title = card_header_title(items, truncated_count)
    lines = [card_status_line(items, truncated_count), *[item_line(item) for item in items]]
    if truncated_count > 0:
        lines.append(f"本次还有 {truncated_count} 条新内容未展开，可点下方按钮进入网站查看。")
    if not items:
        lines.append("监控已连接，当前没有新增条目。")
    content = fit_markdown("\n\n---\n\n".join(lines))

    return {
        "msg_type": "interactive",
        "card": {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
            },
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": content,
                },
                {
                    "tag": "action",
                    "actions": action_buttons(items, mode),
                },
            ],
        },
    }


def send_feishu_card(config: Config, payload: dict[str, Any]) -> None:
    if config.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not config.webhook_url:
        raise ConfigError("FEISHU_WEBHOOK_URL is required unless --dry-run is used")

    if config.webhook_secret:
        timestamp = str(int(time.time()))
        payload = {
            "timestamp": timestamp,
            "sign": feishu_sign(timestamp, config.webhook_secret),
            **payload,
        }

    result = post_json(
        config.webhook_url,
        payload,
        timeout=config.timeout,
        user_agent="aihot-feishu-monitor/0.1",
    )
    code = result.get("code", result.get("StatusCode", 0))
    if code not in (0, None):
        raise RuntimeError(f"Feishu webhook returned code={code}, body={result}")


def fingerprint_url(config: Config) -> str:
    return f"{config.base_url}/api/public/fingerprint"


def items_url(config: Config) -> str:
    query: dict[str, str] = {
        "mode": config.mode,
        "take": str(config.take),
    }
    if config.category:
        query["category"] = config.category
    return f"{config.base_url}/api/public/items?{urllib.parse.urlencode(query)}"


def stream_key(config: Config) -> str:
    return f"{config.mode}:{config.category or '*'}"


def get_fingerprint(config: Config, state: dict[str, Any]) -> tuple[str | None, bool]:
    headers = {}
    etag = state.get("fingerprint_etags", {}).get("global")
    if etag:
        headers["If-None-Match"] = etag
    try:
        resp = http_json(
            fingerprint_url(config),
            user_agent=config.user_agent,
            timeout=config.timeout,
            headers=headers,
        )
    except NotModified:
        return state.get("fingerprints", {}).get(config.mode), False

    body = resp.body or {}
    fingerprints = state.setdefault("fingerprints", {})
    for key in ("selected", "all"):
        if key in body:
            fingerprints[key] = body[key]
    if resp.headers.get("etag"):
        state.setdefault("fingerprint_etags", {})["global"] = resp.headers["etag"]
    current = fingerprints.get(config.mode)
    key = stream_key(config)
    previous = state.setdefault("last_fingerprints_by_stream", {}).get(key)
    state["last_fingerprints_by_stream"][key] = current
    return current, current != previous


def fetch_items(config: Config) -> list[dict[str, Any]]:
    resp = http_json(
        items_url(config),
        user_agent=config.user_agent,
        timeout=config.timeout,
    )
    body = resp.body or {}
    items = body.get("items")
    if not isinstance(items, list):
        raise RuntimeError("AI HOT items response missing items[]")
    return [item for item in items if isinstance(item, dict) and item.get("id")]


def get_seen_ids(config: Config, state: dict[str, Any]) -> list[str]:
    seen_by_stream = state.setdefault("seen_ids_by_stream", {})
    key = stream_key(config)
    if key not in seen_by_stream and state.get("seen_ids"):
        seen_by_stream[key] = state.get("seen_ids", [])
    return list(seen_by_stream.get(key, []))


def update_seen(config: Config, state: dict[str, Any], item_ids: list[str]) -> None:
    seen_by_stream = state.setdefault("seen_ids_by_stream", {})
    key = stream_key(config)
    seen = list(dict.fromkeys([*item_ids, *seen_by_stream.get(key, [])]))
    seen_by_stream[key] = seen[:STATE_SEEN_LIMIT]


def poll_once(config: Config, state: dict[str, Any]) -> int:
    state.setdefault("last_checked_at", utc_now_iso())
    initial = not get_seen_ids(config, state)

    _, changed = get_fingerprint(config, state)
    if not changed and not initial:
        state["last_checked_at"] = utc_now_iso()
        print(f"[{state['last_checked_at']}] unchanged")
        return 0

    items = fetch_items(config)
    fetched_ids = [item["id"] for item in items]
    if initial:
        update_seen(config, state, fetched_ids)
        state["last_checked_at"] = utc_now_iso()
        print(f"[{state['last_checked_at']}] initialized baseline with {len(fetched_ids)} items")
        return 0

    seen_ids = set(get_seen_ids(config, state))
    new_items = [item for item in items if item["id"] not in seen_ids]
    if not new_items:
        update_seen(config, state, fetched_ids)
        state["last_checked_at"] = utc_now_iso()
        print(f"[{state['last_checked_at']}] fingerprint changed, no unseen items")
        return 0

    new_items = list(reversed(new_items))
    notify_items = new_items[-config.max_notify :]
    truncated_count = max(0, len(new_items) - len(notify_items))
    payload = build_card(notify_items, mode=config.mode, truncated_count=truncated_count)
    send_feishu_card(config, payload)
    update_seen(config, state, fetched_ids)
    state["last_checked_at"] = utc_now_iso()
    print(f"[{state['last_checked_at']}] notified {len(notify_items)} new items")
    return len(notify_items)


def test_send(config: Config) -> None:
    sample = {
        "id": "test",
        "title": f"{BRAND_NAME} 监控测试",
        "permalink": f"{config.base_url}/",
        "source": "local monitor",
        "publishedAt": utc_now_iso().replace("+00:00", "Z"),
        "summary": "看到这条消息，说明飞书机器人 webhook 已配置成功。后续卡片会直接在消息内展示完整摘要，标题区域不再承担跳转；需要跳转时再点击底部按钮。",
        "category": "tip",
        "score": 100,
    }
    send_feishu_card(config, build_card([sample], mode=config.mode))
    print("test message sent" if not config.dry_run else "test message rendered")


def run_loop(config: Config, state: dict[str, Any]) -> None:
    stopping = False

    def handle_stop(signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
        print(f"received signal {signum}, stopping after current cycle", file=sys.stderr)

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    while not stopping:
        try:
            poll_once(config, state)
            save_state(config.state_file, state)
        except Exception as exc:
            print(f"[{utc_now_iso()}] error: {exc}", file=sys.stderr)
        for _ in range(config.interval):
            if stopping:
                break
            time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        config = build_config(args)
        if args.test_send:
            test_send(config)
            return 0

        state = load_state(config.state_file)
        if args.loop and not args.once:
            run_loop(config, state)
        else:
            poll_once(config, state)
            save_state(config.state_file, state)
        return 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
