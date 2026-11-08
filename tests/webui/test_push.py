import base64
import json
from contextlib import closing
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from websockets.datastructures import Headers
from websockets.http11 import Request
from websockets.protocol import State

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent, TurnEndEvent
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.metadata import (
    WEBUI_MESSAGE_SOURCE_METADATA_KEY,
    WEBUI_REPLY_CONNECTION_METADATA_KEY,
    WEBUI_TURN_METADATA_KEY,
)
from nanobot.webui.push import (
    WebPushService,
    notice_identity,
    notification_preview,
    validate_subscription,
)

ORIGIN = "https://axis.example.test:8443"


@pytest.fixture
def registration():
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )
    def encode(data):
        return base64.urlsafe_b64encode(data).decode().rstrip("=")
    return {
        "subscription": {"endpoint": "https://fcm.googleapis.com/fcm/send/private-capability",
                         "keys": {"p256dh": encode(public), "auth": encode(b"a" * 16)}},
        "origin": ORIGIN,
        "bodies": {"completed": "Finished", "failed": "Failed", "attention": "Needs you", "test": "Test"},
    }


def rows(service, sql):
    with closing(service._connect()) as db:
        return db.execute(sql).fetchall()


@pytest.mark.parametrize(("text", "expected"), [
    ("# Hola **Rodrigo**\n\nTu [recordatorio](https://example.test/?token=secret) está listo.",
     "Hola Rodrigo Tu recordatorio está listo."),
    ("uno\ndos  \ntres\n\n```python\nmi_archivo = 1\n```", "uno dos tres mi_archivo = 1"),
    ("a" * 300, "a" * 300),
    ("😀" * 160, "😀" * 149 + "…"),
    (" \n ", ""),
])
def test_notification_preview_is_plain_text_and_fits_service_worker(text, expected):
    preview = notification_preview(text)
    assert preview == expected
    assert len(preview.encode("utf-16-le")) // 2 <= 300


@pytest.mark.asyncio
async def test_subscription_and_retry_survive_restart_without_private_content(tmp_path, registration, monkeypatch):
    service = WebPushService(tmp_path)
    public = service.public_key()
    identifier = service.subscribe(registration, ORIGIN)
    event = {"event": "turn_end", "chat_id": "my-chat", "turn_id": "turn-1", "content": "SECRET MAIL"}
    service.enqueue_notice(event)
    service.enqueue_notice(event)
    jobs = rows(service, "SELECT payload FROM outbox")
    assert len(jobs) == 1
    payload = json.loads(jobs[0][0])
    assert payload["body"] == "Finished"
    assert payload["url"] == ORIGIN + "/#/chat/websocket%3Amy-chat"
    assert "SECRET MAIL" not in jobs[0][0]
    assert (tmp_path / "vapid.pem").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    monkeypatch.setattr(service, "_send", lambda *args: 503)
    await service._drain()
    assert rows(service, "SELECT done,attempts FROM outbox") == [(0, 1)]
    resumed = WebPushService(tmp_path)
    assert resumed.public_key() == public
    assert resumed.has_subscription(identifier)
    with closing(resumed._connect()) as db, db:
        db.execute("UPDATE outbox SET retry=0")
    sent = []
    monkeypatch.setattr(resumed, "_send", lambda *args: sent.append(args) or 201)
    await resumed._drain()
    assert len(sent) == 1
    assert rows(resumed, "SELECT done,attempts FROM outbox") == [(1, 2)]
    resumed.enqueue_notice(event)
    assert len(rows(resumed, "SELECT id FROM outbox")) == 1


@pytest.mark.asyncio
async def test_expired_subscription_and_disabled_device_stop_delivery(tmp_path, registration, monkeypatch):
    service = WebPushService(tmp_path)
    identifier = service.subscribe(registration, ORIGIN)
    service.test(identifier)
    monkeypatch.setattr(service, "_send", lambda *args: 410)
    await service._drain()
    assert not service.has_subscription(identifier)
    assert rows(service, "SELECT id FROM outbox") == []
    identifier = service.subscribe(registration, ORIGIN)
    service.test(identifier)
    service.unsubscribe(identifier)
    assert rows(service, "SELECT id FROM outbox") == []


@pytest.mark.asyncio
async def test_visible_app_silences_only_its_device_and_consumes_alerts(tmp_path, registration, monkeypatch):
    service = WebPushService(tmp_path)
    first = service.subscribe(registration, ORIGIN)
    second_registration = {**registration, "subscription": {**registration["subscription"],
                           "endpoint": "https://fcm.googleapis.com/fcm/send/other-device"}}
    second = service.subscribe(second_registration, ORIGIN)
    connection = MagicMock(state=State.OPEN)
    service.set_presence(connection, first, True, ORIGIN)
    event = {"event": "turn_end", "chat_id": "chat", "turn_id": "one"}
    service.enqueue_notice(event)
    service.test(first)  # The explicit test button still works while visible.
    sent = []
    monkeypatch.setattr(service, "_send", lambda *args: sent.append(args) or 201)
    await service._drain()
    assert sorted(json.loads(args[1])["test"] for args in sent) == [False, True]
    assert {row[0]: row[1] for row in rows(service, "SELECT subscription,done FROM outbox WHERE attempts=0")} == {first: 1}
    assert service.has_subscription(second)
    service.set_presence(connection, first, False, ORIGIN)
    service.enqueue_notice(event)
    await service._drain()
    assert len(sent) == 2  # No delayed alert for the response seen in the app.
    service.enqueue_notice({**event, "turn_id": "two"})
    await service._drain()
    assert len(sent) == 4


def test_presence_survives_other_tabs_hiding_but_expires_after_lost_contact(tmp_path, registration, monkeypatch):
    service = WebPushService(tmp_path)
    identifier = service.subscribe(registration, ORIGIN)
    first, second = MagicMock(state=State.OPEN), MagicMock(state=State.OPEN)
    now = [100.0]
    monkeypatch.setattr("nanobot.webui.push.time.monotonic", lambda: now[0])
    service.set_presence(first, identifier, True, ORIGIN)
    service.set_presence(second, identifier, True, ORIGIN)
    service.set_presence(first, identifier, False, ORIGIN)
    assert service._device_visible(identifier)
    second.state = State.CLOSED
    assert not service._device_visible(identifier)
    first.state = State.OPEN
    service.set_presence(first, identifier, True, ORIGIN)
    now[0] += 46
    assert not service._device_visible(identifier)


@pytest.mark.asyncio
async def test_returning_to_app_consumes_pending_retries(tmp_path, registration, monkeypatch):
    service = WebPushService(tmp_path)
    identifier = service.subscribe(registration, ORIGIN)
    service.enqueue_notice({"event": "turn_end", "chat_id": "chat", "turn_id": "one"})
    monkeypatch.setattr(service, "_send", lambda *args: 503)
    await service._drain()
    service.set_presence(MagicMock(state=State.OPEN), identifier, True, ORIGIN)
    assert rows(service, "SELECT done,attempts FROM outbox") == [(1, 1)]


@pytest.mark.asyncio
async def test_pc_without_push_silences_its_replies_but_not_other_alerts(tmp_path, registration, monkeypatch):
    service = WebPushService(tmp_path)
    service.subscribe(registration, ORIGIN)
    pc = MagicMock(state=State.OPEN, id="pc-connection")
    service.set_presence(pc, None, True, ORIGIN)
    event = {"event": "turn_end", "chat_id": "chat", "turn_id": "pc-reply"}
    service.enqueue_notice(event, reply_connection=pc.id)
    service.enqueue_notice({**event, "turn_id": "cron"})
    service.enqueue_notice({**event, "turn_id": "other-reply"}, reply_connection="other-connection")
    sent = []
    monkeypatch.setattr(service, "_send", lambda *args: sent.append(json.loads(args[1])) or 201)
    await service._drain()
    assert len(sent) == 2
    assert all("reply_connection" not in notice for notice in sent)
    service.set_presence(pc, None, False, ORIGIN)
    service.enqueue_notice({**event, "turn_id": "pc-hidden"}, reply_connection=pc.id)
    await service._drain()
    assert len(sent) == 3


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/send/x", "https://127.0.0.1/private",
    "https://fcm.googleapis.com.evil.test/send/x", "https://user@fcm.googleapis.com/send/x",
])
def test_rejects_arbitrary_or_private_push_targets(registration, endpoint):
    registration["subscription"]["endpoint"] = endpoint
    with pytest.raises(ValueError):
        validate_subscription(registration["subscription"])


def test_subscription_cannot_point_notification_clicks_at_another_origin(tmp_path, registration):
    with pytest.raises(ValueError, match="browser origin"):
        WebPushService(tmp_path).subscribe(registration, "https://another.test")


def test_cancelled_turns_and_progress_do_not_notify():
    assert notice_identity({"event": "turn_end", "chat_id": "a", "turn_id": "b", "outcome": "cancelled"}) is None
    assert notice_identity({"event": "model_request", "chat_id": "a", "request_id": "b"}) is None
    assert notice_identity({"event": "recovery_state", "chat_id": "a", "recovery_id": "b", "status": "awaiting_user"}) == ('["a","b","awaiting_user"]', "attention")


@pytest.mark.asyncio
async def test_push_http_requires_auth_and_mutations_use_websocket(tmp_path, registration):
    gateway = build_gateway_services(
        config=WebSocketConfig(token_issue_secret="owner"), bus=MessageBus(), session_manager=None,
        static_dist_path=None, workspace_path=tmp_path, default_restrict_to_workspace=False,
        config_path=tmp_path / "config.json", runtime_model_name=None,
        runtime_surface="browser", runtime_capabilities_overrides=None,
    )
    connection = MagicMock(state=State.OPEN, remote_address=("192.0.2.1", 1), request=Request("/", Headers({"Origin": ORIGIN})))
    response = await gateway.http.dispatch(connection, Request("/api/push", Headers()))
    assert response.status_code == 401
    token = gateway.tokens.issue_api_token(300)
    response = await gateway.http.dispatch(connection, Request("/api/push", Headers({"Authorization": f"Bearer {token}"})))
    assert response.status_code == 200
    assert json.loads(response.body)["registered"] is False
    response = await gateway.http.dispatch(connection, Request("/api/push/subscribe", Headers()))
    assert response.status_code == 405
    response = await gateway.http.dispatch_webui_mutation(connection, "settings.push.subscribe", registration)
    assert response.status_code == 200
    identifier = json.loads(response.body)["id"]
    assert gateway.http.push.has_subscription(identifier)
    response = await gateway.http.dispatch_webui_mutation(connection, "settings.push.test", {"id": identifier})
    assert response.status_code == 200
    assert len(rows(gateway.http.push, "SELECT id FROM outbox")) == 1
    response = await gateway.http.dispatch_webui_mutation(connection, "settings.push.presence", {"id": identifier, "visible": True})
    assert response.status_code == 200
    assert gateway.http.push._device_visible(identifier)
    response = await gateway.http.dispatch_webui_mutation(connection, "settings.push.presence", {"id": None, "visible": True})
    assert response.status_code == 200
    assert gateway.http.push._device_visible(identifier, str(connection.id))
    response = await gateway.http.dispatch_webui_mutation(connection, "settings.push.presence", {"id": identifier, "visible": "false"})
    assert response.status_code == 400
    connection.request = Request("/", Headers({"Origin": "https://another.test"}))
    response = await gateway.http.dispatch_webui_mutation(connection, "settings.push.presence", {"id": identifier, "visible": False})
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_completed_turn_enqueues_push_without_websocket_subscribers(tmp_path, registration, streamed):
    bus = MessageBus()
    sessions = SessionManager(tmp_path / "workspace")
    sessions.save(sessions.get_or_create("websocket:my-chat"))
    config = WebSocketConfig()
    gateway = build_gateway_services(
        config=config, bus=bus, session_manager=sessions, static_dist_path=None,
        workspace_path=tmp_path / "workspace", default_restrict_to_workspace=False,
        config_path=tmp_path / "config.json", runtime_model_name=None,
        runtime_surface="browser", runtime_capabilities_overrides=None,
    )
    gateway.http.push.subscribe(registration, ORIGIN)
    channel = WebSocketChannel(config, bus, gateway=gateway)
    identifier = gateway.http.push.subscribe(registration, ORIGIN)
    connection = MagicMock(state=State.OPEN)
    gateway.http.push.set_presence(connection, identifier, True, ORIGIN)
    await channel._cleanup_connection(connection)
    metadata = {WEBUI_TURN_METADATA_KEY: "turn-1"}
    await channel.send(OutboundMessage(channel="websocket", chat_id="my-chat", content="Earlier step", metadata=metadata))
    if streamed:
        await channel.send_delta("my-chat", "**Tu correo** ", metadata, stream_id="answer")
        await channel.send_delta("my-chat", "está listo.", metadata, stream_id="answer", stream_end=True)
    else:
        await channel.send(OutboundMessage(channel="websocket", chat_id="my-chat", content="**Tu correo** está listo.", metadata=metadata))
    await channel.send_reasoning_delta("my-chat", "PRIVATE REASONING", metadata, stream_id="thinking")
    await channel.send_reasoning_end("my-chat", metadata, stream_id="thinking")
    await channel.send(OutboundMessage(channel="websocket", chat_id="my-chat", content="PRIVATE TOOL OUTPUT",
        metadata=metadata, event=ProgressEvent(content="PRIVATE TOOL OUTPUT")))
    await channel.send(OutboundMessage(channel="websocket", chat_id="another-chat", content="OTHER CHAT", metadata=metadata))
    await channel.send(OutboundMessage(channel="websocket", chat_id="my-chat", content="",
        event=TurnEndEvent(), metadata=metadata))
    assert len(rows(gateway.http.push, "SELECT id FROM outbox")) == 1
    assert rows(gateway.http.push, "SELECT done FROM outbox") == [(0,)]
    assert json.loads(rows(gateway.http.push, "SELECT payload FROM outbox")[0][0])["body"] == "Tu correo está listo."
    await channel.send(OutboundMessage(channel="websocket", chat_id="my-chat", content="",
        event=TurnEndEvent(), metadata={WEBUI_TURN_METADATA_KEY: "turn-2"}))
    assert [json.loads(row[0])["body"] for row in rows(gateway.http.push, "SELECT payload FROM outbox ORDER BY created")] == [
        "Tu correo está listo.", "Finished",
    ]
    failed_metadata = {WEBUI_TURN_METADATA_KEY: "turn-3"}
    await channel.send(OutboundMessage(channel="websocket", chat_id="my-chat", content="PARTIAL RESPONSE", metadata=failed_metadata))
    await channel.send(OutboundMessage(channel="websocket", chat_id="my-chat", content="",
        event=TurnEndEvent(outcome="failed", failure_message="PRIVATE ERROR"), metadata=failed_metadata))
    assert json.loads(rows(gateway.http.push, "SELECT payload FROM outbox ORDER BY created DESC")[0][0])["body"] == "Failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "cron", "local_trigger"])
async def test_automation_delivery_does_not_inherit_pc_reply_suppression(tmp_path, registration, source):
    bus = MessageBus()
    sessions = SessionManager(tmp_path / "workspace")
    sessions.save(sessions.get_or_create("websocket:my-chat"))
    config = WebSocketConfig()
    gateway = build_gateway_services(
        config=config, bus=bus, session_manager=sessions, static_dist_path=None,
        workspace_path=tmp_path / "workspace", default_restrict_to_workspace=False,
        config_path=tmp_path / "config.json", runtime_model_name=None,
        runtime_surface="browser", runtime_capabilities_overrides=None,
    )
    gateway.http.push.subscribe(registration, ORIGIN)
    channel = WebSocketChannel(config, bus, gateway=gateway)
    pc = MagicMock(state=State.OPEN, id="pc-connection")
    gateway.http.push.set_presence(pc, None, True, ORIGIN)
    metadata = {WEBUI_TURN_METADATA_KEY: "turn-1", WEBUI_REPLY_CONNECTION_METADATA_KEY: pc.id}
    if source:
        metadata[WEBUI_MESSAGE_SOURCE_METADATA_KEY] = {"kind": source}
    await channel.send(OutboundMessage(channel="websocket", chat_id="my-chat", content="**Hora de cenar.**", metadata=metadata))
    await channel.send(OutboundMessage(channel="websocket", chat_id="my-chat", content="",
        event=TurnEndEvent(), metadata=metadata))
    assert rows(gateway.http.push, "SELECT done FROM outbox") == [(int(source is None),)]
    assert json.loads(rows(gateway.http.push, "SELECT payload FROM outbox")[0][0])["body"] == "Hora de cenar."


def test_send_encrypts_payload_signs_request_and_disallows_redirects(tmp_path, registration, monkeypatch):
    import requests

    service = WebPushService(tmp_path)
    service.public_key()
    captured = []

    def send(session, request, **kwargs):
        captured.append((request, session.max_redirects))
        response = requests.Response()
        response.status_code = 201
        return response

    monkeypatch.setattr("nanobot.webui.push.validate_url_target", lambda url: (True, ""))
    monkeypatch.setattr(requests.Session, "send", send)
    status = service._send(registration["subscription"], '{"body":"Private notification"}',
                           tmp_path / "vapid.pem", ORIGIN)
    assert status == 201
    request, max_redirects = captured[0]
    assert request.headers["authorization"].startswith("vapid ")
    assert request.headers["content-encoding"] == "aes128gcm"
    assert b"Private notification" not in request.body
    assert max_redirects == 0


def test_push_does_not_follow_a_redirect_to_private_network(tmp_path, registration, monkeypatch):
    import requests

    service = WebPushService(tmp_path)
    service.public_key()
    urls = []

    def send(adapter, request, **kwargs):
        urls.append(request.url)
        response = requests.Response()
        response.status_code = 302
        response.headers["Location"] = "http://127.0.0.1/private"
        response._content = b""
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setattr("nanobot.webui.push.validate_url_target", lambda url: (True, ""))
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with pytest.raises(requests.TooManyRedirects):
        service._send(registration["subscription"], "test", tmp_path / "vapid.pem", ORIGIN)
    assert urls == [registration["subscription"]["endpoint"]]
