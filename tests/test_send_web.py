"""M3b 测试：发送闸门（确认才发、失败可退回、改稿新版本）+ 手机端 API 鉴权。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from hpa import send, store, web
from hpa.config import Config


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str, str]] = []

    def send(self, from_addr: str, to_addr: str, subject: str, body: str) -> None:
        if self.fail:
            raise send.SendError("模拟 SMTP 故障")
        self.calls.append((from_addr, to_addr, subject, body))


class SendGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "vault").mkdir()
        self.cfg = Config(vault=self.root / "vault", project_root=self.root)
        conn = store.connect(self.cfg)
        try:
            conn.execute(
                "INSERT INTO messages(message_id, folder, uid, uidvalidity, from_addr, to_addr, "
                "subject, body, seen_at) VALUES ('<m1@x>', 'INBOX', 1, 1, 'boss@corp.com', "
                "'me@qq.com', '需要回复', '正文', ?)",
                (store.utcnow(),),
            )
            conn.execute(
                """INSERT INTO drafts(message_id, from_addr, version, to_addr, subject, body,
                                      citations, status, created_at)
                   VALUES ('<m1@x>', 'me@qq.com', 1, 'boss@corp.com', 'Re: 需要回复',
                           '草稿正文 [1]', '["学习/a.md:3"]', 'proposed', ?)""",
                (store.utcnow(),),
            )
            conn.commit()
            self.draft_id = conn.execute("SELECT id FROM drafts").fetchone()[0]
        finally:
            conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _draft(self):
        conn = store.connect(self.cfg)
        try:
            return conn.execute("SELECT * FROM drafts WHERE id = ?", (self.draft_id,)).fetchone()
        finally:
            conn.close()

    def test_confirm_sends_exactly_once(self) -> None:
        sender = FakeSender()
        first = send.confirm_and_send(self.cfg, self.draft_id, sender)
        self.assertEqual(first["status"], "sent")
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(sender.calls[0][0], "me@qq.com")
        self.assertEqual(sender.calls[0][1], "boss@corp.com")
        second = send.confirm_and_send(self.cfg, self.draft_id, sender)
        self.assertEqual(second["status"], "sent")
        self.assertTrue(second.get("already"))
        self.assertEqual(len(sender.calls), 1)
        self.assertIsNotNone(self._draft()["sent_at"])

    def test_failure_reverts_to_proposed_and_keeps_error(self) -> None:
        res = send.confirm_and_send(self.cfg, self.draft_id, FakeSender(fail=True))
        self.assertEqual(res["status"], "failed")
        row = self._draft()
        self.assertEqual(row["status"], "proposed")
        self.assertIn("模拟 SMTP 故障", row["last_error"])
        # 修好后可以重发
        ok = send.confirm_and_send(self.cfg, self.draft_id, FakeSender())
        self.assertEqual(ok["status"], "sent")
        self.assertIsNone(self._draft()["last_error"])

    def test_edit_creates_new_version_and_requires_reconfirm(self) -> None:
        res = send.edit_draft(self.cfg, self.draft_id, "Re: 改过的主题", "改过的正文 [1]")
        self.assertEqual(res["status"], "edited")
        self.assertEqual(res["version"], 2)
        old = self._draft()
        self.assertEqual(old["status"], "superseded")
        new = self._draft_row(res["id"])
        self.assertEqual((new["status"], new["subject"], new["body"]), ("proposed", "Re: 改过的主题", "改过的正文 [1]"))
        self.assertEqual(json.loads(new["citations"]), ["学习/a.md:3"])
        # 被 supersede 的旧版本不能再发
        self.assertTrue(send.confirm_and_send(self.cfg, self.draft_id, FakeSender())["already"])
        self.assertEqual(send.confirm_and_send(self.cfg, res["id"], FakeSender())["status"], "sent")

    def _draft_row(self, draft_id: int):
        conn = store.connect(self.cfg)
        try:
            return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        finally:
            conn.close()

    def test_resolve_sending_is_manual(self) -> None:
        conn = store.connect(self.cfg)
        try:
            conn.execute("UPDATE drafts SET status = 'sending' WHERE id = ?", (self.draft_id,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(send.resolve_sending(self.cfg, self.draft_id, sent=False)["status"], "proposed")
        self.assertEqual(self._draft()["status"], "proposed")

    def test_reject(self) -> None:
        self.assertEqual(send.reject_draft(self.cfg, self.draft_id, "不需要回")["status"], "rejected")
        self.assertEqual(self._draft()["status"], "rejected")
        self.assertTrue(send.confirm_and_send(self.cfg, self.draft_id, FakeSender())["already"])

    def test_missing_draft(self) -> None:
        self.assertEqual(send.confirm_and_send(self.cfg, 9999, FakeSender())["status"], "missing")


class WebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "vault").mkdir()
        self.cfg = Config(vault=self.root / "vault", project_root=self.root)
        conn = store.connect(self.cfg)
        try:
            conn.execute(
                "INSERT INTO messages(message_id, folder, uid, uidvalidity, from_addr, to_addr, "
                "subject, body, seen_at) VALUES ('<m2@x>', 'INBOX', 2, 1, 'boss@corp.com', "
                "'me@qq.com', '需要回复', '正文', ?)",
                (store.utcnow(),),
            )
            conn.execute(
                """INSERT INTO drafts(message_id, from_addr, version, to_addr, subject, body,
                                      citations, status, created_at)
                   VALUES ('<m2@x>', 'me@qq.com', 1, 'boss@corp.com', 'Re: 需要回复',
                           '草稿正文', '[]', 'proposed', ?)""",
                (store.utcnow(),),
            )
            conn.commit()
            self.draft_id = conn.execute("SELECT id FROM drafts").fetchone()[0]
        finally:
            conn.close()
        self.sender = FakeSender()
        self.server = web.make_server(self.cfg, host="127.0.0.1", port=0, token="test-token")
        self.server.sender_factory = lambda: self.sender
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def _get(self, path: str, token: str | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("X-HPA-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_state_requires_token(self) -> None:
        code, data = self._get("/api/state")
        self.assertEqual(code, 401)
        code, data = self._get("/api/state", token="wrong")
        self.assertEqual(code, 401)
        code, data = self._get("/api/state", token="test-token")
        self.assertEqual(code, 200)
        self.assertEqual(len(data["drafts"]), 1)
        self.assertEqual(data["drafts"][0]["body"], "草稿正文")

    def test_confirm_endpoint_sends_once(self) -> None:
        req = urllib.request.Request(
            self.base + f"/api/drafts/{self.draft_id}/confirm",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-HPA-Token": "test-token"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        self.assertEqual(data["status"], "sent")
        self.assertEqual(len(self.sender.calls), 1)
        # 再点一次不会重复发
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        self.assertTrue(data.get("already"))
        self.assertEqual(len(self.sender.calls), 1)

    def test_index_page_requires_token(self) -> None:
        try:
            urllib.request.urlopen(self.base + "/", timeout=5)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
            exc.close()
        else:
            self.fail("未带 token 的首页应当 401")
        with urllib.request.urlopen(self.base + "/?token=test-token", timeout=5) as resp:
            html = resp.read().decode("utf-8")
            cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("个人助手", html)
        self.assertIn("hpa_token", cookie)


if __name__ == "__main__":
    unittest.main()
