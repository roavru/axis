"""Opt-in Web Push delivery, independent of open browser connections."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing, suppress
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from loguru import logger
from websockets.asyncio.server import ServerConnection
from websockets.protocol import State

from nanobot.security.network import validate_url_target

_PUSH_HOSTS = {"fcm.googleapis.com", "android.googleapis.com", "web.push.apple.com"}


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_subscription(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Invalid push subscription")
    value = cast(dict[str, Any], value)
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValueError("Invalid push endpoint")
    url = urlsplit(endpoint)
    host = url.hostname or ""
    if (url.scheme != "https" or url.port not in (None, 443) or url.username or url.password
            or url.fragment or not (host in _PUSH_HOSTS
                or host.endswith(".push.services.mozilla.com")
                or host.endswith(".notify.windows.com"))):
        raise ValueError("Unsupported push service")
    keys = value.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("Invalid push keys")
    keys = cast(dict[str, Any], keys)
    try:
        public_text, auth_text = keys["p256dh"], keys["auth"]
        if not isinstance(public_text, str) or not isinstance(auth_text, str):
            raise ValueError
        public = _decode(public_text)
        auth = _decode(auth_text)
        if len(public) != 65:
            raise ValueError
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
        if len(auth) != 16:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid push keys") from exc
    return {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}


def notice_identity(payload: dict[str, Any]) -> tuple[str, str] | None:
    chat = payload.get("chat_id")
    if not isinstance(chat, str) or not chat:
        return None
    if payload.get("event") == "turn_end" and payload.get("outcome") != "cancelled":
        turn = payload.get("turn_id")
        if isinstance(turn, str) and turn:
            kind = ("failed" if payload.get("outcome") == "failed" else
                    "attention" if payload.get("outcome") == "interrupted" else "completed")
            return json.dumps([chat, turn, "end"], separators=(",", ":")), kind
    if payload.get("event") == "recovery_state" and payload.get("status") in {"awaiting_user", "failed"}:
        recovery = payload.get("recovery_id")
        if isinstance(recovery, str) and recovery:
            return json.dumps([chat, recovery, payload["status"]], separators=(",", ":")), "attention"
    return None


class WebPushService:
    """Private subscriptions and a durable, bounded retry queue for one gateway."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._visible: dict[ServerConnection, tuple[str, float]] = {}

    def _connect(self) -> sqlite3.Connection:
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        db = sqlite3.connect(self.directory / "push.sqlite3")
        (self.directory / "push.sqlite3").chmod(0o600)
        db.execute("CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, subscription TEXT NOT NULL, "
                   "payload TEXT NOT NULL, created REAL NOT NULL, retry REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0)")
        db.execute("DELETE FROM outbox WHERE created < ?", (time.time() - 86400,))
        db.commit()
        return db

    def public_key(self) -> str:
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        path = self.directory / "vapid.pem"
        if not path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                f.write(pem)
                f.flush()
                os.fsync(f.fileno())
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("Invalid push signing key")
        public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(public).decode().rstrip("=")

    def subscribe(self, payload: dict[str, Any], request_origin: str) -> str:
        self.public_key()
        subscription = validate_subscription(payload.get("subscription"))
        origin = payload.get("origin")
        if not isinstance(origin, str) or origin != request_origin:
            raise ValueError("Push subscription must belong to this browser origin")
        url = urlsplit(origin)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment):
            raise ValueError("Push notifications require HTTPS")
        raw_bodies = payload.get("bodies")
        if not isinstance(raw_bodies, dict):
            raise ValueError("Invalid notification text")
        raw_bodies = cast(dict[str, object], raw_bodies)
        bodies: dict[str, str] = {}
        for kind in ("completed", "attention", "failed", "test"):
            text = raw_bodies.get(kind)
            if not isinstance(text, str) or not 1 <= len(text) <= 300:
                raise ValueError("Invalid notification text")
            bodies[kind] = text
        identifier = hashlib.sha256(subscription["endpoint"].encode()).hexdigest()
        data = json.dumps({"subscription": subscription, "origin": origin, "bodies": bodies})
        with closing(self._connect()) as db, db:
            count = db.execute("SELECT count(*) FROM subscriptions").fetchone()[0]
            exists = db.execute("SELECT 1 FROM subscriptions WHERE id=?", (identifier,)).fetchone()
            if count >= 32 and not exists:
                raise ValueError("Push device limit reached")
            db.execute("INSERT OR REPLACE INTO subscriptions VALUES (?, ?)", (identifier, data))
        return identifier

    def unsubscribe(self, identifier: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM subscriptions WHERE id=?", (identifier,))
            db.execute("DELETE FROM outbox WHERE subscription=?", (identifier,))

    def has_subscription(self, identifier: str) -> bool:
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM subscriptions WHERE id=?", (identifier,)).fetchone() is not None

    def set_presence(self, connection: ServerConnection, identifier: str, visible: bool, origin: str) -> None:
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT data FROM subscriptions WHERE id=?", (identifier,)).fetchone()
            if row is None or json.loads(row[0])["origin"] != origin:
                raise ValueError("Push device does not belong to this browser origin")
            if visible and connection.state is State.OPEN:
                self._visible[connection] = (identifier, time.monotonic() + 45)
                # Returning to the app consumes queued alerts too, including
                # retries whose next delivery attempt is still in the future.
                pending = db.execute("SELECT id,payload FROM outbox WHERE subscription=? AND done=0",
                                     (identifier,)).fetchall()
                db.executemany("UPDATE outbox SET done=1 WHERE id=?",
                               [(job,) for job, payload in pending if not json.loads(payload).get("test")])
            else:
                self.clear_presence(connection)

    def clear_presence(self, connection: ServerConnection) -> None:
        self._visible.pop(connection, None)

    def _device_visible(self, identifier: str) -> bool:
        now = time.monotonic()
        for connection, (_, expires) in tuple(self._visible.items()):
            if expires <= now or connection.state is not State.OPEN:
                self.clear_presence(connection)
        return any(device == identifier for device, _ in self._visible.values())

    def _enqueue(self, identifier: str, settings: dict[str, Any], key: str, url: str, kind: str) -> None:
        payload = json.dumps({"key": key, "url": url, "body": settings["bodies"][kind], "test": kind == "test"})
        job = hashlib.sha256((identifier + key).encode()).hexdigest()
        now = time.time()
        with closing(self._connect()) as db, db:
            if db.execute("SELECT count(*) FROM outbox WHERE done=0").fetchone()[0] >= 256:
                logger.warning("Web Push queue is full")
                return
            done = int(kind != "test" and self._device_visible(identifier))
            db.execute("INSERT OR IGNORE INTO outbox (id,subscription,payload,created,retry,done) VALUES (?,?,?,?,?,?)",
                       (job, identifier, payload, now, now, done))
        self._wake.set()

    def enqueue_notice(self, payload: dict[str, Any]) -> None:
        notice = notice_identity(payload)
        if notice is None or not (self.directory / "push.sqlite3").exists():
            return
        key, kind = notice
        with closing(self._connect()) as db:
            subscriptions = db.execute("SELECT id,data FROM subscriptions").fetchall()
        for identifier, raw in subscriptions:
            settings = json.loads(raw)
            url = settings["origin"] + "/#/chat/" + quote("websocket:" + payload["chat_id"], safe="")
            self._enqueue(identifier, settings, key, url, kind)

    def test(self, identifier: str) -> None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT data FROM subscriptions WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Enable push notifications on this device first")
        settings = json.loads(row[0])
        self._enqueue(identifier, settings, "test:" + uuid4().hex,
                      settings["origin"] + "/#/settings?section=notifications", "test")

    @staticmethod
    def _send(subscription: dict[str, Any], payload: str, key: Path, origin: str) -> int:
        import requests
        from pywebpush import WebPushException, webpush  # pyright: ignore[reportMissingTypeStubs, reportUnknownVariableType]

        allowed, _ = validate_url_target(subscription["endpoint"])
        if not allowed:
            raise ValueError("Push endpoint blocked")
        with requests.Session() as session:
            session.trust_env = False
            # Requests raises before following the first redirect when this is zero.
            session.max_redirects = 0
            try:
                response = webpush(subscription_info=subscription, data=payload,
                                   vapid_private_key=str(key),
                                   vapid_claims={"sub": f"https://{urlsplit(origin).hostname}"},
                                   ttl=86400, timeout=10, requests_session=session)
                if not isinstance(response, requests.Response):
                    raise ValueError("Unexpected Web Push response")
                return response.status_code
            except WebPushException as exc:
                failed_response: object = getattr(exc, "response", None)
                return failed_response.status_code if isinstance(failed_response, requests.Response) else 503

    async def _drain(self) -> None:
        if not (self.directory / "push.sqlite3").exists():
            return
        with closing(self._connect()) as db:
            jobs = db.execute("SELECT o.id,o.subscription,o.payload,o.attempts,s.data FROM outbox o "
                              "JOIN subscriptions s ON s.id=o.subscription WHERE o.done=0 AND o.retry<=? "
                              "ORDER BY o.created LIMIT 32", (time.time(),)).fetchall()
        for job, identifier, payload, attempts, raw in jobs:
            with closing(self._connect()) as db:
                pending = db.execute("SELECT 1 FROM outbox WHERE id=? AND done=0", (job,)).fetchone()
            if pending is None:
                continue
            if self._device_visible(identifier) and not json.loads(payload).get("test"):
                with closing(self._connect()) as db, db:
                    db.execute("UPDATE outbox SET done=1 WHERE id=?", (job,))
                continue
            settings = json.loads(raw)
            try:
                status = await asyncio.to_thread(self._send, settings["subscription"], payload,
                                                self.directory / "vapid.pem", settings["origin"])
            except Exception:
                # Never log endpoints, keys or HTTP exception strings (capability URLs).
                status = 503
            if status in (404, 410):
                self.unsubscribe(identifier)
            else:
                done = int(200 <= status < 300 or (400 <= status < 500 and status != 429))
                with closing(self._connect()) as db, db:
                    db.execute("UPDATE outbox SET done=?,attempts=attempts+1,retry=? WHERE id=? AND done=0",
                               (done, time.time() + min(30 * 2 ** min(attempts, 7), 3600), job))
                if not 200 <= status < 300:
                    logger.warning("Web Push delivery failed (HTTP {}); {}", status, "stopped" if done else "will retry")

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self._drain()
            except Exception:
                logger.warning("Web Push queue could not be processed; will retry")
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 30)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._visible.clear()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
