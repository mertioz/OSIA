from __future__ import annotations

import asyncio
import importlib.util

from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute

from osia import conversations, turn_terminality
from osia.durable_transport import DurableTransportStore
from osia.server import server


class _NoDurableTransport:
    def client_cursors(self, _client_id):
        return {}

    def replay_for_client(self, _client_id, _cursors, *, stream_ids=None):
        return []


def _receive_type(websocket, expected_type: str) -> dict:
    for _ in range(30):
        frame = websocket.receive_json()
        if frame.get("type") == expected_type:
            return frame
    raise AssertionError(f"did not receive {expected_type!r}")


def _new_store_instance(store_path):
    spec = importlib.util.spec_from_file_location(
        "osia._wor312_persistence_reload",
        conversations.__file__,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CONVERSATIONS_DIR = store_path
    return module


async def _internal_conversation_changed(request):
    return JSONResponse(await server.internal_conversation_changed(request))


def test_special_mode_round_trips_through_mobile_and_reconnects(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    monkeypatch.setattr(server, "MOBILE_TOKEN", "test-token")
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", _NoDurableTransport())
    monkeypatch.setattr(server, "_WS_CLIENTS", set())
    monkeypatch.setattr(server, "_CONVERSATION_BROADCAST_LOCK", None)
    monkeypatch.setattr(server, "_LAST_BROADCAST_REVISIONS", {})
    first = conversations.create_conversation()["id"]
    second = conversations.create_conversation()["id"]

    def authenticate(websocket, device):
        websocket.send_json({
            "type": "auth", "token": "test-token",
            "device_id": device, "event_cursors": {},
        })
        return _receive_type(websocket, "conversations")

    def change(websocket, operation_id, conversation_id, enabled):
        websocket.send_json({
            "type": "set_conversation_special", "operation_id": operation_id,
            "conversation_id": conversation_id, "enabled": enabled,
        })
        ack = _receive_type(websocket, "conversations")
        assert ack["operation_id"] == operation_id
        return ack

    app = Starlette(routes=[WebSocketRoute("/ws", server.websocket_endpoint)])
    with TestClient(app, client=("127.0.0.1", 54321)) as client:
        with (
            client.websocket_connect("/ws") as mobile,
            client.websocket_connect("/ws") as peer,
        ):
            authenticate(mobile, "slash-mobile")
            authenticate(peer, "slash-peer")
            ack = change(mobile, "special_enable", first, True)
            assert ack["special_conversation_id"] == first
            assert ack["special_error"] == ""
            broadcast = _receive_type(peer, "conversations")
            assert {item["id"]: item["special_mode"] for item in broadcast["items"]} == {
                first: True, second: False,
            }

        with client.websocket_connect("/ws") as reconnected:
            snapshot = authenticate(reconnected, "slash-mobile")
            assert {item["id"]: item["special_mode"] for item in snapshot["items"]} == {
                first: True, second: False,
            }
            ack = change(reconnected, "special_restore", first, False)
            assert {item["id"]: item["special_mode"] for item in ack["items"]} == {
                first: False, second: False,
            }
            ack = change(reconnected, "special_new", "", True)
            new = ack["special_conversation_id"]
            assert new and new not in (first, second)
            assert conversations.conversation_special_mode(new)
            replay = change(reconnected, "special_new", "", True)
            assert replay["special_conversation_id"] == new
            assert len(replay["items"]) == 3


def test_real_store_single_bulk_and_desktop_server_deletion_paths(
    tmp_path, monkeypatch,
):
    store_path = tmp_path / "conversations"
    store_path.mkdir()
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", store_path)
    monkeypatch.setattr(server, "MOBILE_TOKEN", "test-token")
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", _NoDurableTransport())
    monkeypatch.setattr(server, "_WS_CLIENTS", set())
    monkeypatch.setattr(server, "_CONVERSATION_BROADCAST_LOCK", None)
    monkeypatch.setattr(server, "_LAST_BROADCAST_REVISIONS", {})

    mobile_single = conversations.create_conversation("Mobile single")
    mobile_busy = conversations.create_conversation("Mobile busy")
    desktop_single = conversations.create_conversation("Desktop single")
    mobile_bulk = [
        conversations.create_conversation("Bulk A"),
        conversations.create_conversation("Bulk B"),
    ]
    desktop_bulk = []
    folder = conversations.create_folder("Preserved", operation_id="folder-1")
    foldered = conversations.create_conversation("Keep folder member")
    assert conversations.move_conversation_to_folder(
        foldered["id"],
        folder["folder"]["id"],
        operation_id="move-1",
    )["success"]

    app = Starlette(routes=[
        Route(
            "/internal/conversation_changed",
            _internal_conversation_changed,
            methods=["POST"],
        ),
        WebSocketRoute("/ws", server.websocket_endpoint),
    ])
    with TestClient(app, client=("127.0.0.1", 54321)) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({
                "type": "auth",
                "token": "test-token",
                "device_id": "wor312-test-device",
                "event_cursors": {},
            })
            initial = _receive_type(websocket, "conversations")
            assert mobile_single["id"] in {
                item["id"] for item in initial["items"]
            }
            assert mobile_busy["id"] in {
                item["id"] for item in initial["items"]
            }

            assert conversations.claim_background_execution(
                mobile_busy["id"], "protected-background-lease",
            )
            websocket.send_json({
                "type": "delete_conversation",
                "id": mobile_busy["id"],
                "operation_id": "mobile_delete_busy",
            })
            busy_ack = _receive_type(websocket, "conversation_deleted")
            assert busy_ack["success"] is False
            assert busy_ack["error"] == "conversation_busy"
            busy_snapshot = _receive_type(websocket, "conversations")
            assert mobile_busy["id"] in {
                item["id"] for item in busy_snapshot["items"]
            }
            assert conversations.get_conversation(mobile_busy["id"]) is not None
            assert conversations.release_background_execution(
                mobile_busy["id"], "protected-background-lease",
            )

            websocket.send_json({
                "type": "delete_conversation",
                "id": mobile_busy["id"],
                "operation_id": "mobile_delete_busy_retried",
            })
            busy_retry_ack = _receive_type(websocket, "conversation_deleted")
            assert busy_retry_ack["success"] is True
            busy_retry_snapshot = _receive_type(websocket, "conversations")
            assert mobile_busy["id"] not in {
                item["id"] for item in busy_retry_snapshot["items"]
            }

            websocket.send_json({
                "type": "delete_conversation",
                "id": mobile_single["id"],
                "operation_id": "mobile_delete_single",
            })
            single_ack = _receive_type(websocket, "conversation_deleted")
            assert single_ack["id"] == mobile_single["id"]
            assert single_ack["success"] is True
            assert single_ack["operation_id"] == "mobile_delete_single"
            assert single_ack["snapshot_revision"] > initial["snapshot_revision"]
            single_snapshot = _receive_type(websocket, "conversations")
            assert mobile_single["id"] not in {
                item["id"] for item in single_snapshot["items"]
            }

            # Desktop's actual store receipt followed by the production local
            # invalidation endpoint must report success only after the ID is
            # absent from a fresh canonical snapshot.
            premature_notify = client.post(
                "/internal/conversation_changed",
                json={
                    "conversation_id": desktop_single["id"],
                    "operation_id": "desktop_delete_single",
                    "action": "delete",
                },
            ).json()
            assert premature_notify["broadcast"] is False
            desktop_receipt = conversations.delete_conversation_with_receipt(
                desktop_single["id"],
                operation_id="desktop_delete_single",
            )
            assert desktop_receipt["success"] is True
            desktop_notify = client.post(
                "/internal/conversation_changed",
                json={
                    "conversation_id": desktop_single["id"],
                    "operation_id": "desktop_delete_single",
                    "revision": desktop_receipt["snapshot_revision"],
                    "action": "delete",
                },
            ).json()
            assert desktop_notify["broadcast"] is True
            desktop_ack = _receive_type(websocket, "conversation_deleted")
            assert desktop_ack["id"] == desktop_single["id"]
            assert desktop_ack["success"] is True
            assert desktop_ack["snapshot_revision"] == (
                desktop_receipt["snapshot_revision"]
            )
            desktop_snapshot = _receive_type(websocket, "conversations")
            assert desktop_single["id"] not in {
                item["id"] for item in desktop_snapshot["items"]
            }

            websocket.send_json({
                "type": "bulk_delete_ungrouped_conversations",
                "expected_count": 999,
                "operation_id": "mobile_bulk_delete_rejected",
            })
            failed_bulk_ack = _receive_type(
                websocket,
                "conversations_bulk_deleted",
            )
            assert failed_bulk_ack["success"] is False
            assert failed_bulk_ack["error"] == "ungrouped_count_changed"
            failed_bulk_snapshot = _receive_type(websocket, "conversations")
            assert {
                item["id"] for item in mobile_bulk
            }.issubset({item["id"] for item in failed_bulk_snapshot["items"]})

            websocket.send_json({
                "type": "bulk_delete_ungrouped_conversations",
                "expected_count": 2,
                "operation_id": "mobile_bulk_delete",
            })
            mobile_bulk_ack = _receive_type(
                websocket,
                "conversations_bulk_deleted",
            )
            assert mobile_bulk_ack["success"] is True
            assert mobile_bulk_ack["operation_id"] == "mobile_bulk_delete"
            assert set(mobile_bulk_ack["deleted_ids"]) == {
                item["id"] for item in mobile_bulk
            }
            assert mobile_bulk_ack["snapshot_revision"] > (
                desktop_snapshot["snapshot_revision"]
            )
            mobile_bulk_snapshot = _receive_type(websocket, "conversations")
            mobile_bulk_ids = {item["id"] for item in mobile_bulk_snapshot["items"]}
            assert not mobile_bulk_ids.intersection(
                item["id"] for item in mobile_bulk
            )
            assert foldered["id"] in mobile_bulk_ids

            desktop_bulk = [
                conversations.create_conversation("Desktop bulk A"),
                conversations.create_conversation("Desktop bulk B"),
            ]
            desktop_receipt = conversations.delete_confirmed_ungrouped_conversations(
                expected_count=2,
                operation_id="desktop_bulk_delete",
            )
            assert desktop_receipt["success"] is True
            desktop_bulk_notify = client.post(
                "/internal/conversation_changed",
                json={
                    "conversation_id": "",
                    "operation_id": "desktop_bulk_delete",
                    "revision": desktop_receipt["snapshot_revision"],
                    "action": "bulk_delete",
                    "deleted_ids": [item["id"] for item in desktop_bulk],
                },
            ).json()
            assert desktop_bulk_notify["ok"] is True
            desktop_bulk_ack = _receive_type(
                websocket,
                "conversations_bulk_deleted",
            )
            assert desktop_bulk_ack["success"] is True
            assert set(desktop_bulk_ack["deleted_ids"]) == {
                item["id"] for item in desktop_bulk
            }
            desktop_bulk_snapshot = _receive_type(websocket, "conversations")
            assert foldered["id"] in {
                item["id"] for item in desktop_bulk_snapshot["items"]
            }

    # A new store module reads the same real files after the live server path.
    reloaded_store = _new_store_instance(store_path)
    final_snapshot = reloaded_store.conversation_snapshot()
    final_ids = {item["id"] for item in final_snapshot["items"]}
    deleted_ids = {
        mobile_single["id"],
        mobile_busy["id"],
        desktop_single["id"],
        *(item["id"] for item in mobile_bulk),
        *(item["id"] for item in desktop_bulk),
    }
    assert final_ids.isdisjoint(deleted_ids)
    assert foldered["id"] in final_ids
    for conv_id in deleted_ids:
        assert reloaded_store.get_conversation(conv_id) is None


def test_mobile_single_delete_stops_exact_canonical_chat_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(turn_terminality, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(server, "MOBILE_TOKEN", "test-token")
    monkeypatch.setattr(
        server, "_DURABLE_TRANSPORT", DurableTransportStore(tmp_path / "transport"),
    )
    for name in ("_ACTIVE_TURNS_BY_CONV", "_ACTIVE_TURN_META_BY_CONV",
                 "_ACTIVE_CONV_BY_REQUEST", "_LAST_BROADCAST_REVISIONS"):
        monkeypatch.setattr(server, name, {})
    monkeypatch.setattr(server, "_WS_CLIENTS", set())
    monkeypatch.setattr(server, "_CONVERSATION_BROADCAST_LOCK", None)

    conv_id = conversations.create_conversation("Active chat")["id"]
    keep_id = conversations.create_conversation("Another chat")["id"]
    desktop_id = conversations.create_conversation("Active desktop chat")["id"]
    stopped = []
    binding = {
        "conversation_id": conv_id,
        "request_id": "request-to-delete",
        "turn_id": "turn-to-delete",
        "run_id": "run-to-delete",
        "provider_id": "deepseek",
        "model_id": "deepseek-flash",
        "effective_reasoning": "off",
    }
    meta = {
        **binding,
        "client_request_id": binding["request_id"],
        "user_message_id": "user-to-delete",
        "assistant_message_id": "assistant-to-delete",
        "_terminality_objective": "an unfinished task",
        "_execution_generation_cancel": stopped.append,
    }
    desktop_meta = {
        **meta,
        "conversation_id": desktop_id,
        "request_id": "desktop-request",
        "client_request_id": "desktop-request",
        "turn_id": "desktop-turn",
        "run_id": "desktop-run",
        "user_message_id": "desktop-user",
        "assistant_message_id": "desktop-assistant",
        "_execution_owner": "desktop",
    }
    assert server._persist_accepted_user_message(
        desktop_id, "Desktop-owned task", desktop_meta,
    )

    async def claim_turns():
        assert server._persist_accepted_user_message(conv_id, "an unfinished task", meta)
        active = asyncio.create_task(asyncio.Event().wait())
        other = asyncio.create_task(asyncio.Event().wait())
        assert server._claim_conversation_turn(conv_id, active, meta)
        assert server._claim_conversation_turn(keep_id, other)
        return active, other

    app = Starlette(routes=[WebSocketRoute("/ws", server.websocket_endpoint)])
    with TestClient(app, client=("127.0.0.1", 54321)) as client:
        with client.websocket_connect("/ws") as mobile:
            mobile.send_json({"type": "auth", "token": "test-token"})
            initial = _receive_type(mobile, "conversations")
            active, other = client.portal.call(claim_turns)
            try:
                mobile.send_json({
                    "type": "delete_conversation", "id": desktop_id,
                    "operation_id": "mobile_delete_desktop_active",
                })
                protected = _receive_type(mobile, "conversation_deleted")
                assert protected["success"] is False
                assert protected["error"] == "conversation_busy"
                protected_snapshot = _receive_type(mobile, "conversations")
                assert desktop_id in {item["id"] for item in protected_snapshot["items"]}
                assert not conversations.was_cancelled_for_conversation_delete(desktop_meta)

                mobile.send_json({
                    "type": "delete_conversation", "id": conv_id,
                    "operation_id": "mobile_delete_active",
                })
                status = _receive_type(mobile, "turn_status")
                assert status["status"] == "cancelled"
                assert {key: status[key] for key in binding} == binding
                ack = _receive_type(mobile, "conversation_deleted")
                assert ack["id"] == conv_id
                assert ack["success"] is True
                assert ack["operation_id"] == "mobile_delete_active"
                assert ack["snapshot_revision"] > initial["snapshot_revision"]
                snapshot = _receive_type(mobile, "conversations")
                assert {item["id"] for item in snapshot["items"]} == {keep_id, desktop_id}
                assert conversations.was_cancelled_for_conversation_delete(binding)
                assert active.cancelling()
                assert not other.done()
                assert stopped == [binding["turn_id"]]
            finally:
                client.portal.call(active.cancel)
                client.portal.call(other.cancel)
