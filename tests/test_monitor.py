import json
import tempfile
import unittest
from pathlib import Path

import aihot_feishu_monitor as monitor


class MonitorTests(unittest.TestCase):
    def make_config(self, **overrides):
        defaults = dict(
            base_url="https://aihot.virxact.com",
            mode="selected",
            category=None,
            take=50,
            interval=60,
            state_file=Path("/tmp/aihot-test-state.json"),
            webhook_url=None,
            webhook_secret=None,
            user_agent="test-agent/1.0",
            timeout=5,
            max_notify=2,
            dry_run=True,
        )
        defaults.update(overrides)
        return monitor.Config(**defaults)

    def test_feishu_sign_known_shape(self):
        got = monitor.feishu_sign("1700000000", "secret")
        self.assertTrue(got)
        self.assertEqual(got, "fiWS2+gh28DOydAv7hzONH/mDn9+b1Y4Y5ivXWXy8vA=")

    def test_env_file_does_not_override_existing_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            path.write_text("AIHOT_MODE=all\nAIHOT_TAKE=7\n", encoding="utf-8")
            old = dict(monitor.os.environ)
            try:
                monitor.os.environ["AIHOT_MODE"] = "selected"
                monitor.load_env_file(path)
                self.assertEqual(monitor.os.environ["AIHOT_MODE"], "selected")
                self.assertEqual(monitor.os.environ["AIHOT_TAKE"], "7")
            finally:
                monitor.os.environ.clear()
                monitor.os.environ.update(old)

    def test_resolve_webhook_url_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "webhook"
            path.write_text("\nhttps://example.invalid/hook/test\n", encoding="utf-8")
            old = dict(monitor.os.environ)
            try:
                monitor.os.environ.pop("FEISHU_WEBHOOK_URL", None)
                monitor.os.environ["FEISHU_WEBHOOK_FILE"] = str(path)
                self.assertEqual(
                    monitor.resolve_webhook_url(None),
                    "https://example.invalid/hook/test",
                )
            finally:
                monitor.os.environ.clear()
                monitor.os.environ.update(old)

    def test_update_seen_is_per_stream(self):
        state = {"seen_ids_by_stream": {}}
        selected = self.make_config(mode="selected")
        all_mode = self.make_config(mode="all")

        monitor.update_seen(selected, state, ["a", "b"])
        monitor.update_seen(all_mode, state, ["c"])

        self.assertEqual(monitor.get_seen_ids(selected, state), ["a", "b"])
        self.assertEqual(monitor.get_seen_ids(all_mode, state), ["c"])

    def test_first_poll_seeds_without_alert(self):
        config = self.make_config()
        state = {"fingerprints": {}, "fingerprint_etags": {}, "seen_ids_by_stream": {}}
        calls = []

        def fake_get_fingerprint(_config, _state):
            return "f1-test", True

        def fake_fetch_items(_config):
            return [{"id": "1", "title": "one"}, {"id": "2", "title": "two"}]

        def fake_send(_config, _payload):
            calls.append(_payload)

        old_get = monitor.get_fingerprint
        old_fetch = monitor.fetch_items
        old_send = monitor.send_feishu_card
        try:
            monitor.get_fingerprint = fake_get_fingerprint
            monitor.fetch_items = fake_fetch_items
            monitor.send_feishu_card = fake_send
            count = monitor.poll_once(config, state)
        finally:
            monitor.get_fingerprint = old_get
            monitor.fetch_items = old_fetch
            monitor.send_feishu_card = old_send

        self.assertEqual(count, 0)
        self.assertEqual(calls, [])
        self.assertEqual(monitor.get_seen_ids(config, state), ["1", "2"])

    def test_next_poll_alerts_only_unseen_items(self):
        config = self.make_config(max_notify=1)
        state = {
            "fingerprints": {},
            "fingerprint_etags": {},
            "seen_ids_by_stream": {"selected:*": ["old"]},
        }
        calls = []

        def fake_get_fingerprint(_config, _state):
            return "f1-new", True

        def fake_fetch_items(_config):
            return [
                {"id": "new-2", "title": "two", "permalink": "https://x/2"},
                {"id": "new-1", "title": "one", "permalink": "https://x/1"},
                {"id": "old", "title": "old"},
            ]

        def fake_send(_config, payload):
            calls.append(payload)

        old_get = monitor.get_fingerprint
        old_fetch = monitor.fetch_items
        old_send = monitor.send_feishu_card
        try:
            monitor.get_fingerprint = fake_get_fingerprint
            monitor.fetch_items = fake_fetch_items
            monitor.send_feishu_card = fake_send
            count = monitor.poll_once(config, state)
        finally:
            monitor.get_fingerprint = old_get
            monitor.fetch_items = old_fetch
            monitor.send_feishu_card = old_send

        self.assertEqual(count, 1)
        rendered = json.dumps(calls[0], ensure_ascii=False)
        self.assertIn("AI HOT（数字卡兹克）新增 2 条", rendered)
        self.assertEqual(calls[0]["card"]["header"]["title"]["content"], "two 等 2 条")
        self.assertIn("还有 1 条新内容", rendered)
        self.assertIn("two", rendered)
        self.assertNotIn("[two](https://x/2)", rendered)
        self.assertIn("进入网站", rendered)
        self.assertNotIn("打开详情", rendered)

    def test_card_keeps_full_summary_and_uses_buttons_for_links(self):
        long_summary = "完整内容" * 120
        payload = monitor.build_card(
            [
                {
                    "id": "1",
                    "title": "标题",
                    "summary": long_summary,
                    "permalink": "https://aihot.virxact.com/items/1",
                    "url": "https://example.com/source",
                    "source": "来源",
                    "publishedAt": "2026-07-07T00:00:00.000Z",
                    "category": "tip",
                    "score": 88,
                }
            ],
            mode="selected",
        )
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["card"]["header"]["title"]["content"], "标题")
        self.assertIn(long_summary, rendered)
        self.assertIn("**标题**", rendered)
        self.assertNotIn("[标题](https://aihot.virxact.com/items/1)", rendered)
        self.assertIn("进入网站", rendered)
        self.assertNotIn("打开详情", rendered)
        self.assertIn("打开原文", rendered)

    def test_card_header_summarizes_multiple_items(self):
        payload = monitor.build_card(
            [
                {"id": "1", "title": "蚂蚁集团开源 LingBot-Vision", "summary": "a"},
                {"id": "2", "title": "OpenAI 发布新模型工具", "summary": "b"},
                {"id": "3", "title": "Claude Code 更新工作流", "summary": "c"},
            ],
            mode="selected",
            truncated_count=1,
        )
        header = payload["card"]["header"]["title"]["content"]
        self.assertIn("蚂蚁集团开源 LingBot-Vision", header)
        self.assertIn("OpenAI 发布新模型工具", header)
        self.assertIn("Claude Code 更新工作流", header)
        self.assertIn("等 4 条", header)
        self.assertNotIn("AI HOT（数字卡兹克）新增", header)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertIn("AI HOT（数字卡兹克）新增 4 条", rendered)


if __name__ == "__main__":
    unittest.main()
