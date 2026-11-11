"""手机端一页（M3b）：待办 / 草稿确认 / 纠正 / 状态条。

只用标准库 `http.server`，单进程；默认绑 `127.0.0.1`，手机经 Tailscale 访问时
在 config.toml 里改 `[web] host = "0.0.0.0"`（或 `tailscale serve` 反代）。

安全：
- 首次访问必须带 `?token=`；校验通过后写 HttpOnly Cookie，后续请求不再暴露 token。
- token 来源优先级：`HPA_WEB_TOKEN` 环境变量 → secrets.env → `state/web-token`（自动生成，0600）。
- 所有对外动作（确认发送）都必须 POST 到这里，且只由人点击触发。
"""

from __future__ import annotations

import hmac
import json
import secrets
import tomllib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import kb, send, store
from .config import Config

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 64 * 1024
TOKEN_COOKIE = "hpa_token"


class WebAuthError(RuntimeError):
    pass


def load_web_settings(cfg: Config) -> dict:
    path = cfg.project_root / "config.toml"
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f).get("web", {})


def ensure_token(cfg: Config) -> str:
    import os

    token = os.environ.get("HPA_WEB_TOKEN") or ""
    if token:
        return token
    try:
        from .mail import load_secrets

        token = load_secrets().get("HPA_WEB_TOKEN", "")
    except Exception:
        token = ""
    if token:
        return token
    path = cfg.state_dir / "web-token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(24)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return token


def state_payload(cfg: Config) -> dict:
    conn = store.connect(cfg)
    try:
        drafts = [
            _draft_dict(row)
            for row in conn.execute(
                """SELECT d.*, m.subject AS mail_subject, m.from_addr AS mail_from,
                          m.received_at AS mail_received_at
                   FROM drafts d LEFT JOIN messages m ON m.message_id = d.message_id
                   WHERE d.status IN ('proposed', 'sending')
                   ORDER BY d.created_at DESC, d.id DESC"""
            )
        ]
        todos = [
            dict(row)
            for row in conn.execute(
                "SELECT id, title, deadline, status, message_id, created_at FROM todos "
                "WHERE status = 'open' ORDER BY deadline IS NULL, deadline, id DESC LIMIT 50"
            )
        ]
        corrections = [
            dict(row)
            for row in conn.execute(
                "SELECT id, created_at, message_id, raw FROM corrections ORDER BY id DESC LIMIT 20"
            )
        ]
        counts = {
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "todos_open": conn.execute("SELECT COUNT(*) FROM todos WHERE status = 'open'").fetchone()[0],
            "drafts_pending": conn.execute(
                "SELECT COUNT(*) FROM drafts WHERE status IN ('proposed', 'sending')"
            ).fetchone()[0],
            "sent": conn.execute("SELECT COUNT(*) FROM drafts WHERE status = 'sent'").fetchone()[0],
        }
        last_sync = store.get_meta(conn, "last_sync_at")
    finally:
        conn.close()
    try:
        last_index = kb.stats(cfg)["last_index_at"]
    except Exception:
        last_index = None
    return {
        "status": {
            "counts": counts,
            "last_sync": last_sync,
            "last_index": last_index,
        },
        "drafts": drafts,
        "todos": todos,
        "corrections": corrections,
    }


def _draft_dict(row) -> dict:
    try:
        citations = json.loads(row["citations"] or "[]")
    except json.JSONDecodeError:
        citations = []
    return {
        "id": row["id"],
        "version": row["version"],
        "message_id": row["message_id"],
        "from_addr": row["from_addr"],
        "to_addr": row["to_addr"],
        "subject": row["subject"],
        "body": row["body"],
        "citations": citations,
        "status": row["status"],
        "created_at": row["created_at"],
        "last_error": row["last_error"],
        "mail_subject": row["mail_subject"],
        "mail_from": row["mail_from"],
        "mail_received_at": row["mail_received_at"],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "hpa/0.1"
    protocol_version = "HTTP/1.1"

    # ---- 基础响应 ----
    def log_message(self, fmt: str, *args) -> None:  # 静默：别把邮件内容打进日志
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _html(self, code: int, text: str, extra: dict[str, str] | None = None) -> None:
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8", extra)

    def _static(self, name: str) -> None:
        path = (WEB_DIR / name).resolve()
        if WEB_DIR not in path.parents or not path.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".webmanifest": "application/manifest+json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(path.suffix, "application/octet-stream")
        self._send(HTTPStatus.OK, path.read_bytes(), ctype)

    # ---- 鉴权 ----
    def _token_from_request(self) -> str:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == TOKEN_COOKIE:
                return unquote(value)
        header = self.headers.get("X-HPA-Token", "")
        if header:
            return header
        query = parse_qs(urlsplit(self.path).query)
        return (query.get("token") or [""])[0]

    def _authorized(self) -> bool:
        expected = self.server.token  # type: ignore[attr-defined]
        got = self._token_from_request()
        return bool(got) and hmac.compare_digest(got, expected)

    def _secret_ok_or_401(self) -> bool:
        if self._authorized():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "需要访问令牌：打开控制台打印的带 ?token= 的地址"})
        return False

    # ---- GET ----
    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if path in ("/", "/index.html"):
            if not self._authorized():
                self._html(HTTPStatus.UNAUTHORIZED, _login_hint())
                return
            extra = {}
            if (parse_qs(urlsplit(self.path).query).get("token") or [""])[0]:
                extra["Set-Cookie"] = f"{TOKEN_COOKIE}={self.server.token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"  # type: ignore[attr-defined]
            self._html(HTTPStatus.OK, (WEB_DIR / "index.html").read_text(encoding="utf-8"), extra)
            return
        if path == "/manifest.webmanifest":
            self._static("manifest.webmanifest")
            return
        if path.startswith("/static/"):
            self._static(path.removeprefix("/static/"))
            return
        if path == "/api/state":
            if not self._secret_ok_or_401():
                return
            self._json(HTTPStatus.OK, state_payload(self.server.cfg))  # type: ignore[attr-defined]
            return
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    # ---- POST ----
    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if not self._secret_ok_or_401():
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        try:
            result = self._dispatch_post(path, payload)
        except send.SendError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"status": "no_credentials", "error": str(exc)})
            return
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"内部错误：{exc}"})
            return
        self._json(HTTPStatus.OK, result)

    def _dispatch_post(self, path: str, payload: dict) -> dict:
        cfg: Config = self.server.cfg  # type: ignore[attr-defined]
        parts = [p for p in path.split("/") if p]
        # /api/drafts/<id>/<action>
        if len(parts) == 4 and parts[:2] == ["api", "drafts"]:
            draft_id = int(parts[2])
            action = parts[3]
            if action == "confirm":
                sender = self.server.sender_factory()  # type: ignore[attr-defined]
                return send.confirm_and_send(cfg, draft_id, sender)
            if action == "edit":
                return send.edit_draft(
                    cfg, draft_id, str(payload.get("subject", "")), str(payload.get("body", ""))
                )
            if action == "reject":
                return send.reject_draft(cfg, draft_id, str(payload.get("reason", "")))
            if action == "resolve":
                return send.resolve_sending(cfg, draft_id, bool(payload.get("sent")))
            if action == "correct":
                note = str(payload.get("note", "")).strip()
                if not note:
                    raise ValueError("纠正内容不能为空")
                conn = store.connect(cfg)
                try:
                    row = conn.execute(
                        "SELECT message_id FROM drafts WHERE id = ?", (draft_id,)
                    ).fetchone()
                    if row is None:
                        raise ValueError(f"草稿 {draft_id} 不存在")
                    cur = conn.execute(
                        "INSERT INTO corrections(created_at, message_id, raw) VALUES (?, ?, ?)",
                        (store.utcnow(), row["message_id"], note),
                    )
                    conn.commit()
                finally:
                    conn.close()
                return {"status": "recorded", "id": cur.lastrowid}
        if path == "/api/sync":
            from . import mail

            box = mail.mailbox_from_env()
            res = mail.sync(cfg, box)
            return {"status": "ok", "inserted": res.inserted, "fetched": res.fetched}
        raise ValueError(f"未知接口：{path}")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data


def _login_hint() -> str:
    return (
        "<!doctype html><meta charset='utf-8'><title>需要令牌</title>"
        "<body style='font-family:system-ui;padding:2rem;max-width:32rem;margin:auto'>"
        "<h1>需要访问令牌</h1><p>请打开启动 <code>hpa web</code> 时打印的带 <code>?token=</code> 的完整地址。</p>"
        "<p>token 也可以写在 <code>~/.config/hpa/secrets.env</code> 的 "
        "<code>HPA_WEB_TOKEN=...</code>。</p></body>"
    )


def make_server(
    cfg: Config, host: str | None = None, port: int | None = None, token: str | None = None
) -> ThreadingHTTPServer:
    settings = load_web_settings(cfg)
    bind_host = host or settings.get("host", "127.0.0.1")
    bind_port = int(port or settings.get("port", 8787))
    server = ThreadingHTTPServer((bind_host, bind_port), Handler)
    server.cfg = cfg  # type: ignore[attr-defined]
    server.token = token or ensure_token(cfg)  # type: ignore[attr-defined]
    server.sender_factory = send.sender_from_env  # type: ignore[attr-defined]
    return server


def serve(cfg: Config, host: str | None = None, port: int | None = None) -> None:
    server = make_server(cfg, host=host, port=port)
    shown_host = "127.0.0.1" if server.server_address[0] in ("0.0.0.0", "::") else server.server_address[0]
    print(f"手机端控制台：http://{shown_host}:{server.server_address[1]}/?token={server.token}")  # type: ignore[attr-defined]
    print("（手机经 Tailscale 访问时，把 host 设为 Tailscale IP 或 0.0.0.0）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
