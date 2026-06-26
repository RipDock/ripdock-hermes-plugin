import json
import logging
import time
import uuid
from urllib.parse import unquote

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from backend.runtime_ws import handle_runtime_websocket


logger = logging.getLogger(__name__)

RIPDOCK_NOT_FOUND = {
    "ok": False,
    "error": {
        "code": "runtime.notFound",
        "message": "RipDock route not found.",
    },
    "message": "RipDock route not found.",
}


def create_runtime_app(adapter):
    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/.well-known/ripdock/runtime-identity")
    async def runtime_identity():
        return _json(adapter._public_runtime_identity())

    @app.get("/.well-known/ripdock/runtime-metadata")
    async def runtime_metadata():
        return _json(
            {
                "code": "authorization.denied",
                "message": "Pairing is required before Runtime metadata is available.",
            },
            status_code=403,
        )

    @app.get("/ripdock/admin/state")
    async def admin_state():
        return _json(adapter._admin_state())

    @app.get("/ripdock/admin/conversations")
    async def admin_conversations(agent_id: str = "", conversation_id: str = ""):
        return _json(adapter._admin_conversations_snapshot(agent_id=agent_id, conversation_id=conversation_id))

    @app.post("/ripdock/admin/pairing-payloads")
    async def admin_pairing_payloads():
        adapter.pairing_code = adapter._create_pairing_code()
        adapter.pairing_code_created_at = time.time()
        adapter.pairing_bound = False
        adapter._save_session_state()
        public_host = getattr(adapter, "embedded_host", None) or "127.0.0.1"
        public_port = str(getattr(adapter, "embedded_port", None) or "")
        payload = adapter._direct_pairing_payload(adapter.pairing_code, public_host, public_port)
        adapter._log_pairing(adapter.pairing_code, payload)
        return _json(
            {
                "pairingCode": adapter.pairing_code,
                "sessionId": str(uuid.uuid4()),
                "pairingPayload": payload,
                "expiresAt": adapter._iso_after(adapter._pairing_ttl_seconds()),
                "securityNotice": "QR/code does not grant trust by itself. Hermes must approve each DeviceIdentity.",
                "backend": {
                    "source": "runtime",
                    "runtime_identity": adapter._public_runtime_identity(),
                    "pairing_code": adapter.pairing_code,
                },
            }
        )

    @app.post("/ripdock/pairing/request")
    async def pairing_request(request: Request):
        try:
            payload = await _json_body(request)
            return _json(adapter._handle_pairing_request(payload))
        except ValueError as exc:
            return _json(adapter._pairing_error_response(str(exc)), status_code=400)

    @app.post("/ripdock/pairing/status")
    async def pairing_status(request: Request):
        try:
            payload = await _json_body(request)
            return _json(adapter._handle_pairing_status(payload))
        except ValueError as exc:
            return _json(adapter._pairing_error_response(str(exc)), status_code=400)

    @app.get("/ripdock/admin/devices/{device_id}/{action}")
    @app.post("/ripdock/admin/devices/{device_id}/{action}")
    async def admin_device_action(device_id: str, action: str):
        return _admin_device_response(adapter, unquote(device_id), action)

    @app.post("/ripdock/admin/devices/{action}")
    async def admin_device_body_action(action: str, request: Request):
        try:
            body = await _json_body(request)
        except ValueError:
            body = {}
        device_id = str(body.get("deviceId") or body.get("device_id") or "").strip()
        return _admin_device_response(adapter, device_id, action)

    @app.get("/ripdock/transfer/{transfer_id}/artifact")
    async def artifact_download(transfer_id: str):
        return _response_from_embedded(adapter._handle_embedded_artifact_download(unquote(transfer_id)))

    @app.websocket("/ripdock/app")
    async def app_websocket(websocket: WebSocket):
        await handle_runtime_websocket(adapter, websocket, "/ripdock/app")

    @app.websocket("/ripdock/app/pair/{pairing_code}")
    async def pairing_websocket(websocket: WebSocket, pairing_code: str):
        await handle_runtime_websocket(adapter, websocket, f"/ripdock/app/pair/{pairing_code}")

    @app.websocket("/ripdock/transfer/{transfer_id}/{role}")
    async def transfer_websocket(websocket: WebSocket, transfer_id: str, role: str):
        await handle_runtime_websocket(adapter, websocket, f"/ripdock/transfer/{transfer_id}/{role}")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def not_found(path: str):
        return _json(RIPDOCK_NOT_FOUND, status_code=404)

    return app


async def _json_body(request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise ValueError("Request body must be a JSON object.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


def _json(payload, status_code=200, headers=None):
    return JSONResponse(
        content=payload,
        status_code=status_code,
        headers=headers or {},
    )


def _admin_device_response(adapter, device_id, action):
    if action not in {"approve", "reject", "revoke"}:
        return _json(RIPDOCK_NOT_FOUND, status_code=404)
    if action == "revoke":
        pending_map, trusted_map, _revoked_map, _rejected_map = adapter._ensure_device_maps()
        logger.info(
            "RipDock revoke route entry request_path=/ripdock/admin/devices/%s/%s method=ASGI device_id=%s trusted_keys=%s pending_keys=%s",
            device_id,
            action,
            device_id,
            sorted(str(key) for key in trusted_map.keys()),
            sorted(str(key) for key in pending_map.keys()),
        )
    try:
        if not device_id:
            raise ValueError("deviceId is required.")
        if action == "approve":
            response = adapter._approve_pending_device(device_id)
        elif action == "reject":
            response = adapter._reject_pending_device(device_id)
        else:
            response = adapter._revoke_trusted_device(device_id)
        return _json(response)
    except ValueError as exc:
        status = 200 if action == "reject" and "not found" in str(exc).lower() else 404
        return _json(
            {
                "ok": action == "reject" and status == 200,
                "deviceId": device_id,
                "trustState": "notFound" if action == "reject" and status == 200 else "error",
                "noop": action == "reject" and status == 200,
                "message": "Pending Device was already gone." if action == "reject" and status == 200 else str(exc),
                "state": adapter._admin_state() if action == "reject" and status == 200 else None,
            },
            status_code=status,
        )


def _response_from_embedded(value):
    if hasattr(value, "status_code") and hasattr(value, "body"):
        return Response(
            content=value.body,
            status_code=value.status_code,
            headers=dict(getattr(value, "headers", {}) or {}),
            media_type=None,
        )
    if isinstance(value, tuple) and len(value) == 3:
        status_code, headers, body = value
        return Response(
            content=body,
            status_code=status_code,
            headers={str(key): str(item) for key, item in headers},
            media_type=None,
        )
    if isinstance(value, (dict, list)):
        return _json(value)
    return Response(content=json.dumps(RIPDOCK_NOT_FOUND) + "\n", status_code=404, media_type="application/json")
