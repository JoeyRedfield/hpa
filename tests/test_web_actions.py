"""手机端动作：暂停/恢复自动处理、待办勾选完成；daemon 必须尊重暂停开关。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from hpa import daemon, mail, store, web
from hpa.config import Config

TOKEN = "test-token"


class _Mailbox:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_since(self, folder: str, last_uid: int, limit: int | None = None):
        self.calls += 1
        return [], 1


class WebActionsTest(unittest.TestCase):
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
                "INSERT INTO todos(message_id, title, deadline, status, created_at) "
                "VALUES ('<m1@x>', '确认接口文档', '2026-11-13', 'open', ?)",
                (store.utcnow(),),
            )
            conn.commit()
            self.todo_id = conn.execute("SELECT id FROM todos").fetchone()[0]
        finally:
            conn.close()
        self.server = web.make_server(self.cfg, host="127.0.0.1", port=0, token=TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def _post(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json", "X-HPA-Token": TOKEN},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def _state(self) -> dict:
        req = urllib.request.Request(self.base + "/api/state", headers={"X-HPA-Token": TOKEN})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def test_pause_toggles_and_is_visible_in_state(self) -> None:
        self.assertFalse(self._state()["status"]["paused"])
        code, data = self._post("/api/pause", {"paused": True})
        self.assertEqual((code, data["paused"]), (200, True))
        self.assertTrue(self._state()["status"]["paused"])
        code, data = self._post("/api/pause", {"paused": False})
        self.assertEqual((code, data["paused"]), (200, False))
        self.assertFalse(self._state()["status"]["paused"])

    def test_todo_done_then_reopen(self) -> None:
        state = self._state()
        self.assertEqual(state["status"]["counts"]["todos_open"], 1)
        self.assertEqual(state["todos"][0]["id"], self.todo_id)

        code, data = self._post(f"/api/todos/{self.todo_id}/done")
        self.assertEqual((code, data["status"]), (200, "done"))
        state = self._state()
        self.assertEqual(state["status"]["counts"]["todos_open"], 0)
        self.assertEqual(state["todos"], [])

        code, data = self._post(f"/api/todos/{self.todo_id}/open")
        self.assertEqual((code, data["status"]), (200, "open"))
        self.assertEqual(self._state()["status"]["counts"]["todos_open"], 1)

    def test_todo_unknown_or_double_done(self) -> None:
        code, _ = self._post("/api/todos/9999/done")
        self.assertEqual(code, 400)
        self._post(f"/api/todos/{self.todo_id}/done")
        code, _ = self._post(f"/api/todos/{self.todo_id}/done")
        self.assertEqual(code, 400)

    def test_daemon_skips_tick_when_paused(self) -> None:
        conn = store.connect(self.cfg)
        try:
            store.set_paused(conn, True)
        finally:
            conn.close()
        box = _Mailbox()
        result = daemon.tick(self.cfg, mailbox=box, llm=None)
        self.assertTrue(result.paused)
        self.assertEqual(box.calls, 0)
        self.assertEqual(result.synced, 0)


if __name__ == "__main__":
    unittest.main()
