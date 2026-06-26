from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel
except Exception:
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def get(self, *_args, **_kwargs):
            return lambda fn: fn

        def post(self, *_args, **_kwargs):
            return lambda fn: fn

    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

router = APIRouter()
logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "1"
DEFAULT_PAIRING_TTL_SECONDS = 15 * 60
MIN_PAIRING_TTL_SECONDS = 30
MAX_PAIRING_TTL_SECONDS = 15 * 60
PRODUCTION_PAIRING_TTL_SECONDS = 15 * 60
DEFAULT_REJECTED_PAIRING_TTL_SECONDS = 10 * 60


class PublicURLBody(BaseModel):
    publicURL: str = ""


class MetadataBody(BaseModel):
    displayName: str = ""
    icon: str = ""
    accentColor: str = ""
    backgroundColor: str = ""
    productionWarning: bool = False


class AgentMetadataBody(BaseModel):
    displayName: str = ""
    icon: str = ""
    accentColor: str = ""
    backgroundColor: str = ""
    sortOrder: int | None = None
    enabled: bool = True


class PairingRequestBody(BaseModel):
    deviceIdentity: dict[str, Any] = {}
    deviceId: str = ""
    deviceName: str = ""
    publicKey: Any = None
    publicKeyFingerprint: str = ""
    deviceFingerprint: str = ""


class DeviceActionBody(BaseModel):
    deviceId: str = ""
    action: str = ""


class DeviceLabelBody(BaseModel):
    label: str = ""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hermes_home() -> Path:
    value = os.getenv("HERMES_HOME", "").strip()
    if value:
        return Path(value)
    home = os.getenv("HOME", "").strip()
    return Path(home) / ".hermes" if home else Path.home() / ".hermes"


def state_path() -> Path:
    return Path(
        os.getenv(
            "RIPDOCK_DASHBOARD_STATE_FILE",
            str(_hermes_home() / "ripdock" / "dashboard-state.json"),
        )
    )


def runtime_identity_path() -> Path:
    return Path(
        os.getenv(
            "RIPDOCK_RUNTIME_IDENTITY_FILE",
            str(_hermes_home() / "ripdock" / "runtime-identity.json"),
        )
    )


def public_runtime_url_file() -> Path:
    return Path(
        os.getenv(
            "RIPDOCK_PUBLIC_RUNTIME_URL_FILE",
            str(_hermes_home() / "ripdock" / "public-runtime-url"),
        )
    )


def session_file_path() -> Path:
    return Path(
        os.getenv(
            "RIPDOCK_SESSION_FILE",
            str(_hermes_home() / "ripdock" / "session.json"),
        )
    )


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def loadState() -> dict[str, Any]:
    return load_state()


def saveState(state: dict[str, Any]) -> None:
    save_state(state)


def save_public_runtime_url_file(value: str) -> None:
    path = public_runtime_url_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((value.strip().rstrip("/") if value else "") + "\n")


def _string_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _emoji_icon_or_empty(value: Any) -> str:
    text = _string_value(value).strip()
    if not text:
        return ""
    has_emoji_symbol = any(unicodedata.category(char) == "So" for char in text)
    has_alnum = any(char.isalnum() for char in text)
    return text if has_emoji_symbol and not has_alnum else ""


def _summary_identifiers(summary: dict[str, Any]) -> set[str]:
    identifiers = set()
    for key in ("deviceId", "device_id", "deviceFingerprint", "publicKeyFingerprint", "public_key_fingerprint", "requestId"):
        value = _string_value(summary.get(key)).strip()
        if value:
            identifiers.add(value)
    return identifiers


def _normalize_device_label(value: Any) -> str:
    label = _string_value(value).strip()
    if len(label) > 80:
        raise HTTPException(status_code=400, detail="Device label must be 80 characters or fewer.")
    return label


def _device_label_records() -> dict[str, dict[str, Any]]:
    state = load_state()
    records = state.get("deviceLabels")
    return records if isinstance(records, dict) else {}


def _label_for_summary(summary: dict[str, Any], records: dict[str, dict[str, Any]]) -> str:
    for identifier in _summary_identifiers(summary):
        record = records.get(identifier)
        if isinstance(record, dict) and isinstance(record.get("label"), str):
            return record["label"]
    return ""


def _apply_device_labels(payload: dict[str, Any]) -> dict[str, Any]:
    records = _device_label_records()
    payload = dict(payload)
    for list_key in ("pendingDevices", "trustedDevices", "revokedDevices"):
        devices = payload.get(list_key)
        if not isinstance(devices, list):
            continue
        labeled = []
        for device in devices:
            if not isinstance(device, dict):
                labeled.append(device)
                continue
            summary = dict(device)
            summary["label"] = _label_for_summary(summary, records)
            labeled.append(summary)
        payload[list_key] = labeled
    return payload


def update_device_label(device_id: str, label: str) -> dict[str, Any]:
    device_id = _string_value(device_id).strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId is required.")
    normalized = _normalize_device_label(label)
    state = load_state()
    records = state.setdefault("deviceLabels", {})
    if not isinstance(records, dict):
        records = {}
        state["deviceLabels"] = records
    if normalized:
        records[device_id] = {"label": normalized, "updatedAt": _now_iso()}
    else:
        records.pop(device_id, None)
    save_state(state)
    return {"ok": True, "deviceId": device_id, "label": normalized, "state": dashboard_state()}


def _pending_summary_time(summary: dict[str, Any]) -> str:
    for key in ("claimedTime", "requestedAt", "claimedAt", "createdAt"):
        value = _string_value(summary.get(key)).strip()
        if value:
            return value
    return ""


def _deleted_pending_records() -> dict[str, dict[str, Any]]:
    state = load_state()
    records = state.get("deletedPendingDevices")
    return records if isinstance(records, dict) else {}


def _remember_deleted_pending_device(device_id: str, state_payload: dict[str, Any] | None = None) -> None:
    device_id = _string_value(device_id).strip()
    if not device_id:
        return
    identifiers = {device_id}
    if isinstance(state_payload, dict):
        for summary in state_payload.get("pendingDevices", []):
            if not isinstance(summary, dict):
                continue
            summary_ids = _summary_identifiers(summary)
            if device_id in summary_ids:
                identifiers.update(summary_ids)
    state = load_state()
    records = state.setdefault("deletedPendingDevices", {})
    if not isinstance(records, dict):
        records = {}
        state["deletedPendingDevices"] = records
    deleted_at = _now_iso()
    for identifier in identifiers:
        records[identifier] = {"deletedAt": deleted_at}
    save_state(state)
    logger.info("RIPDOCK dashboard pruned pending Device identifiers=%s", sorted(identifiers))


def _clear_deleted_pending_device(device_identity: dict[str, Any]) -> None:
    identifiers = _summary_identifiers({
        "deviceId": device_identity.get("deviceId"),
        "device_id": device_identity.get("device_id"),
        "publicKeyFingerprint": device_identity.get("publicKeyFingerprint"),
        "public_key_fingerprint": device_identity.get("public_key_fingerprint"),
    })
    if not identifiers:
        return
    state = load_state()
    records = state.get("deletedPendingDevices")
    if not isinstance(records, dict):
        return
    removed = sorted(identifier for identifier in identifiers if identifier in records)
    if not removed:
        return
    for identifier in removed:
        records.pop(identifier, None)
    save_state(state)
    logger.info("RIPDOCK dashboard cleared deleted pending Device markers identifiers=%s", removed)


def _pending_summary_deleted(summary: dict[str, Any], deleted_records: dict[str, dict[str, Any]]) -> bool:
    summary_ids = _summary_identifiers(summary)
    if not summary_ids:
        return False
    summary_time = _pending_summary_time(summary)
    for identifier in summary_ids:
        record = deleted_records.get(identifier)
        if not isinstance(record, dict):
            continue
        deleted_at = _string_value(record.get("deletedAt")).strip()
        if not summary_time or not deleted_at or summary_time < deleted_at:
            return True
    return False


def _state_contains_device(state_payload: dict[str, Any], list_key: str, device_id: str) -> bool:
    devices = state_payload.get(list_key)
    if not isinstance(devices, list):
        return False
    return any(isinstance(device, dict) and device_id in _summary_identifiers(device) for device in devices)


def _filter_deleted_pending_devices(payload: dict[str, Any]) -> dict[str, Any]:
    pending = payload.get("pendingDevices")
    if not isinstance(pending, list):
        return payload
    deleted_records = _deleted_pending_records()
    if not deleted_records:
        logger.info("RIPDOCK dashboard state pending Devices count=%s pruned=0", len(pending))
        return payload
    filtered = [device for device in pending if not (isinstance(device, dict) and _pending_summary_deleted(device, deleted_records))]
    pruned = len(pending) - len(filtered)
    if pruned:
        payload = dict(payload)
        payload["pendingDevices"] = filtered
    logger.info("RIPDOCK dashboard state pending Devices count=%s pruned=%s", len(filtered), pruned)
    return payload


def _filter_expired_pending_devices(payload: dict[str, Any]) -> dict[str, Any]:
    pending = payload.get("pendingDevices")
    if not isinstance(pending, list):
        return payload
    filtered = [device for device in pending if not (isinstance(device, dict) and _is_expired_entry(device))]
    pruned = len(pending) - len(filtered)
    if pruned:
        payload = dict(payload)
        payload["pendingDevices"] = filtered
    logger.info("RIPDOCK dashboard state pending Devices count=%s expired_pruned=%s", len(filtered), pruned)
    return payload


def _filter_pending_devices(payload: dict[str, Any]) -> dict[str, Any]:
    return _filter_deleted_pending_devices(_filter_expired_pending_devices(payload))


def _metadata_from_mapping(source: dict[str, Any] | None, identity: dict[str, Any] | None = None) -> dict[str, str]:
    source = source if isinstance(source, dict) else {}
    identity = identity if isinstance(identity, dict) else {}
    display_name = (
        _string_value(source.get("displayName")).strip()
        or _string_value(identity.get("displayName")).strip()
        or os.getenv("RIPDOCK_RUNTIME_NAME", "").strip()
        or "Hermes"
    )
    icon = _emoji_icon_or_empty(source.get("icon"))
    if "icon" not in source:
        icon = _emoji_icon_or_empty(os.getenv("RIPDOCK_RUNTIME_ICON", ""))
    accent_color = _string_value(source.get("accentColor"))
    if "accentColor" not in source:
        accent_color = os.getenv("RIPDOCK_ACCENT_COLOR", "")
    background_color = _string_value(source.get("backgroundColor"))
    if not background_color.strip():
        background_color = os.getenv("RIPDOCK_BACKGROUND_COLOR", "#ffffff")
    return {
        "displayName": display_name,
        "icon": icon,
        "accentColor": accent_color.strip(),
        "backgroundColor": background_color.strip() or "#ffffff",
    }


def _stored_runtime_metadata(identity: dict[str, Any] | None = None) -> dict[str, str]:
    state = load_state()
    stored = state.get("runtimeMetadata")
    if not isinstance(stored, dict):
        stored = state.get("metadata")
    return _metadata_from_mapping(stored if isinstance(stored, dict) else {}, identity)


def save_runtime_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    normalized = _metadata_from_mapping(metadata)
    state = load_state()
    state["runtimeMetadata"] = normalized
    state["metadata"] = normalized
    save_state(state)
    return normalized


def _normalize_agent_id(value: Any) -> str:
    import re

    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.:-]+", "-", text)
    text = text.strip("-")
    return text[:80]


def _agent_display_name(agent_id: str) -> str:
    import re

    parts = [part for part in re.split(r"[-_.:]+", agent_id) if part]
    return " ".join(part.capitalize() for part in parts) or agent_id or "Agent"


def _configured_agent_ids() -> list[str]:
    values: list[str] = _hermes_profile_names()

    if not values:
        values.append("default")

    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        agent_id = _normalize_agent_id(value)
        if agent_id and agent_id not in seen:
            seen.add(agent_id)
            normalized.append(agent_id)
    return normalized or ["default"]


def _hermes_profile_names() -> list[str]:
    try:
        result = subprocess.run(
            [_hermes_command(), "profile", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception as exc:
        logger.warning("RIPDOCK dashboard failed to discover Hermes profiles error=%s", repr(exc))
        return []
    if result.returncode != 0:
        logger.warning("RIPDOCK dashboard Hermes profile discovery failed status=%s stderr=%s", result.returncode, result.stderr.strip())
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split()
        if not fields:
            continue
        name = fields[0].lstrip("◆*")
        if name and name.lower() != "profile" and not set(name) <= {"─"}:
            names.append(name)
    return names


def _hermes_command() -> str:
    discovered = shutil.which("hermes")
    if discovered:
        return discovered
    candidate = Path(sys.executable).with_name("hermes")
    if candidate.exists():
        return str(candidate)
    return "hermes"


def _default_agent_metadata(agent_id: str, index: int) -> dict[str, Any]:
    accents = ["#2563eb", "#0f766e", "#7c3aed", "#dc2626", "#ea580c", "#16a34a", "#0891b2", "#4f46e5"]
    backgrounds = ["#dbeafe", "#ccfbf1", "#ede9fe", "#fee2e2", "#ffedd5", "#dcfce7", "#cffafe", "#e0e7ff"]
    return {
        "agent_id": agent_id,
        "display_name": _agent_display_name(agent_id),
        "icon": "🤖",
        "accent_color": accents[index % len(accents)],
        "background_color": backgrounds[index % len(backgrounds)],
        "sort_order": index,
        "settings": [
            {
                "key": "show_activity",
                "label": "Show Activity",
                "type": "boolean",
                "default": True,
            }
        ],
    }


def runtime_agents(identity: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    identity = identity or load_runtime_identity()
    runtime_id = _string_value(identity.get("runtimeId")).strip()
    state = load_state()
    all_metadata = state.get("agentMetadata")
    metadata = all_metadata.get(runtime_id) if isinstance(all_metadata, dict) and isinstance(all_metadata.get(runtime_id), dict) else {}
    all_settings = state.get("agentSettings")
    settings_by_agent = all_settings.get(runtime_id) if isinstance(all_settings, dict) and isinstance(all_settings.get(runtime_id), dict) else {}
    agents: list[dict[str, Any]] = []
    for index, agent_id in enumerate(_configured_agent_ids()):
        defaults = _default_agent_metadata(agent_id, index)
        stored = metadata.get(agent_id) if isinstance(metadata, dict) else {}
        if not isinstance(stored, dict):
            stored = {}
        agent = dict(defaults)
        for stored_key, agent_key in {
            "displayName": "display_name",
            "icon": "icon",
            "accentColor": "accent_color",
            "backgroundColor": "background_color",
            "sortOrder": "sort_order",
        }.items():
            value = stored.get(agent_key, stored.get(stored_key))
            if value is not None and value != "":
                if agent_key == "icon":
                    emoji_icon = _emoji_icon_or_empty(value)
                    if emoji_icon:
                        agent[agent_key] = emoji_icon
                else:
                    agent[agent_key] = value
        agent["enabled"] = stored.get("enabled") if isinstance(stored.get("enabled"), bool) else True
        values = settings_by_agent.get(agent_id) if isinstance(settings_by_agent, dict) else {}
        agent["values"] = values if isinstance(values, dict) else {}
        agents.append(agent)
    agents.sort(key=lambda item: (item.get("sort_order") if isinstance(item.get("sort_order"), int) else 9999, item.get("display_name") or item.get("agent_id")))
    return agents


def save_agent_metadata(agent_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    agent_id = _normalize_agent_id(agent_id)
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required.")
    identity = load_runtime_identity()
    runtime_id = _string_value(identity.get("runtimeId")).strip()
    state = load_state()
    records = state.setdefault("agentMetadata", {})
    if not isinstance(records, dict):
        records = {}
        state["agentMetadata"] = records
    runtime_records = records.setdefault(runtime_id, {})
    if not isinstance(runtime_records, dict):
        runtime_records = {}
        records[runtime_id] = runtime_records
    existing = runtime_records.get(agent_id) if isinstance(runtime_records.get(agent_id), dict) else {}
    normalized = {
        "display_name": _string_value(metadata.get("displayName") or metadata.get("display_name") or existing.get("display_name") or _agent_display_name(agent_id)).strip(),
        "icon": _emoji_icon_or_empty(metadata.get("icon") or existing.get("icon")) or "🤖",
        "accent_color": _string_value(metadata.get("accentColor") or metadata.get("accent_color") or existing.get("accent_color") or "#2563eb").strip(),
        "background_color": _string_value(metadata.get("backgroundColor") or metadata.get("background_color") or existing.get("background_color") or "#dbeafe").strip(),
        "sort_order": metadata.get("sortOrder") if isinstance(metadata.get("sortOrder"), int) else existing.get("sort_order"),
        "enabled": metadata.get("enabled") if isinstance(metadata.get("enabled"), bool) else (existing.get("enabled") if isinstance(existing.get("enabled"), bool) else True),
    }
    runtime_records[agent_id] = normalized
    save_state(state)
    return normalized


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes | None:
    try:
        padding = "=" * ((4 - len(value) % 4) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (binascii.Error, UnicodeEncodeError):
        return None


def _p256_public_key_bytes_from_jwk(public_key: Any) -> bytes | None:
    if not isinstance(public_key, dict):
        return None
    if set(public_key.keys()) != {"crv", "key_id", "kty", "x", "y"}:
        return None
    if public_key.get("kty") != "EC" or public_key.get("crv") != "P-256":
        return None
    key_id = public_key.get("key_id")
    if not isinstance(key_id, str) or re.fullmatch(r"[0-9a-f]{64}", key_id) is None:
        return None
    x_value = public_key.get("x")
    y_value = public_key.get("y")
    if not isinstance(x_value, str) or not isinstance(y_value, str):
        return None
    x = _base64url_decode(x_value)
    y = _base64url_decode(y_value)
    if x is None or y is None or len(x) != 32 or len(y) != 32:
        return None
    public_bytes = x + y
    if hashlib.sha256(public_bytes).hexdigest() != key_id:
        return None
    return public_bytes


def _p256_jwk_key_id(public_key: Any) -> str:
    if not isinstance(public_key, dict):
        return ""
    return _string_value(public_key.get("key_id")).strip()


def _valid_p256_jwk_public_key(public_key: Any) -> bool:
    return _p256_public_key_bytes_from_jwk(public_key) is not None


def _public_key_fingerprint(public_key: Any) -> str:
    return _p256_jwk_key_id(public_key) if isinstance(public_key, dict) else ""


def _generate_runtime_keypair() -> tuple[dict[str, str], str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_bytes = private_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)[1:]
    public_key = {
        "crv": "P-256",
        "key_id": hashlib.sha256(public_bytes).hexdigest(),
        "kty": "EC",
        "x": _base64url(public_bytes[:32]),
        "y": _base64url(public_bytes[32:]),
    }
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return public_key, private_pem


def _public_identity(identity: dict[str, Any]) -> dict[str, Any]:
    metadata = runtime_metadata(identity)
    return {
        "runtimeId": identity.get("runtimeId") or "",
        "displayName": metadata["displayName"],
        "publicKey": identity.get("publicKey") or "",
        "publicKeyFingerprint": identity.get("publicKeyFingerprint") or "",
        "protocolVersion": identity.get("protocolVersion") or PROTOCOL_VERSION,
        "createdAt": identity.get("createdAt") or "",
    }


def load_runtime_identity() -> dict[str, Any]:
    saved_identity: dict[str, Any] = {}
    try:
        data = json.loads(runtime_identity_path().read_text())
        if (
            isinstance(data, dict)
            and isinstance(data.get("runtimeId"), str)
            and _valid_p256_jwk_public_key(data.get("publicKey"))
            and data.get("publicKeyFingerprint") == _p256_jwk_key_id(data.get("publicKey"))
            and isinstance(data.get("createdAt"), str)
        ):
            if not isinstance(data.get("pendingDevices"), dict):
                data["pendingDevices"] = {}
            if not isinstance(data.get("trustedDevices"), dict):
                data["trustedDevices"] = {}
            if not isinstance(data.get("revokedDevices"), dict):
                data["revokedDevices"] = {}
            if not isinstance(data.get("rejectedDevices"), dict):
                data["rejectedDevices"] = {}
            return data
        if isinstance(data, dict):
            saved_identity = data
    except Exception:
        pass
    state = load_state()
    public_key = state.get("localPublicKey")
    private_key = state.get("localPrivateKey")
    if not _valid_p256_jwk_public_key(public_key) or not isinstance(private_key, str) or not private_key:
        public_key, private_key = _generate_runtime_keypair()
        state["localPublicKey"] = public_key
        state["localPrivateKey"] = private_key
        save_state(state)
    return {
        "runtimeId": _string_value(saved_identity.get("runtimeId")).strip() or state.get("localRuntimeId") or str(uuid.uuid4()),
        "displayName": _string_value(saved_identity.get("displayName")).strip() or _stored_runtime_metadata().get("displayName") or "Hermes",
        "publicKey": public_key,
        "publicKeyFingerprint": _public_key_fingerprint(public_key),
        "protocolVersion": PROTOCOL_VERSION,
        "createdAt": _string_value(saved_identity.get("createdAt")).strip() or state.get("localRuntimeCreatedAt") or _now_iso(),
        "privateKey": private_key,
        "pendingDevices": saved_identity.get("pendingDevices") if isinstance(saved_identity.get("pendingDevices"), dict) else {},
        "trustedDevices": saved_identity.get("trustedDevices") if isinstance(saved_identity.get("trustedDevices"), dict) else {},
        "revokedDevices": saved_identity.get("revokedDevices") if isinstance(saved_identity.get("revokedDevices"), dict) else {},
        "rejectedDevices": saved_identity.get("rejectedDevices") if isinstance(saved_identity.get("rejectedDevices"), dict) else {},
    }


def save_runtime_identity(identity: dict[str, Any]) -> None:
    path = runtime_identity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n")
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _device_state(pending: dict[str, Any], trusted: dict[str, Any], revoked: dict[str, Any], device_id: str) -> str:
    if device_id in trusted:
        return "trusted"
    if device_id in pending:
        return "pendingApproval"
    if device_id in revoked:
        return "revoked"
    return "unknown"


def _log_transition(action: str, device_id: str, previous_state: str, next_state: str, pending: dict[str, Any], trusted: dict[str, Any]) -> None:
    logger.info(
        "RIPDOCK pairing state transition action=%s device_id=%s previous_state=%s next_state=%s pending_count=%s trusted_count=%s",
        action,
        device_id,
        previous_state,
        next_state,
        len(pending),
        len(trusted),
    )


def _device_identity_from_request(body: dict[str, Any]) -> dict[str, Any]:
    source = body.get("deviceIdentity") if isinstance(body.get("deviceIdentity"), dict) else body
    public_key = source.get("publicKey") if "publicKey" in source else source.get("public_key")
    public_key = public_key if _valid_p256_jwk_public_key(public_key) else None
    public_key_fingerprint = _string_value(source.get("publicKeyFingerprint") or source.get("public_key_fingerprint") or source.get("deviceFingerprint")).strip()
    if public_key is not None and not public_key_fingerprint:
        public_key_fingerprint = _p256_jwk_key_id(public_key)
    return {
        "deviceId": _string_value(source.get("deviceId") or source.get("device_id")).strip(),
        "deviceName": _string_value(source.get("deviceName") or source.get("device_name") or source.get("name")).strip() or "Unnamed Device",
        "publicKey": public_key,
        "publicKeyFingerprint": public_key_fingerprint,
        "createdAt": _string_value(source.get("createdAt") or source.get("created_at")).strip() or _now_iso(),
    }


def _rejected_pairing_ttl_seconds() -> int:
    try:
        return max(0, int(os.getenv("RIPDOCK_REJECTED_PAIRING_TTL_SECONDS", str(DEFAULT_REJECTED_PAIRING_TTL_SECONDS))))
    except ValueError:
        return DEFAULT_REJECTED_PAIRING_TTL_SECONDS


def _iso_epoch(value: str) -> float | None:
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


def _is_rejected_entry_queryable(entry: dict[str, Any]) -> bool:
    rejected_at = _iso_epoch(_string_value(entry.get("rejectedAt")).strip())
    now = _iso_epoch(_now_iso())
    if rejected_at is None or now is None:
        return True
    return now - rejected_at <= _rejected_pairing_ttl_seconds()


def _is_expired_entry(entry: dict[str, Any]) -> bool:
    expires_at = _iso_epoch(_string_value(entry.get("expiresAt")).strip())
    now = _iso_epoch(_now_iso())
    return expires_at is not None and now is not None and expires_at <= now


def _ensure_device_maps(identity: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    pending = identity.setdefault("pendingDevices", {})
    trusted = identity.setdefault("trustedDevices", {})
    revoked = identity.setdefault("revokedDevices", {})
    rejected = identity.setdefault("rejectedDevices", {})
    if not isinstance(pending, dict):
        pending = {}
        identity["pendingDevices"] = pending
    if not isinstance(trusted, dict):
        trusted = {}
        identity["trustedDevices"] = trusted
    if not isinstance(revoked, dict):
        revoked = {}
        identity["revokedDevices"] = revoked
    if not isinstance(rejected, dict):
        rejected = {}
        identity["rejectedDevices"] = rejected
    return pending, trusted, revoked, rejected


def _device_fingerprint_matches(stored: dict[str, Any], device_identity: dict[str, Any]) -> bool:
    stored_identity = stored.get("deviceIdentity") if isinstance(stored.get("deviceIdentity"), dict) else {}
    stored_public_key = stored_identity.get("publicKey")
    stored_fingerprint = _string_value(stored_identity.get("publicKeyFingerprint"))
    requested_public_key = device_identity.get("publicKey")
    requested_fingerprint = _string_value(device_identity.get("publicKeyFingerprint"))
    if stored_fingerprint and requested_fingerprint and stored_fingerprint != requested_fingerprint:
        return False
    if isinstance(stored_public_key, dict) and isinstance(requested_public_key, dict) and stored_public_key != requested_public_key:
        return False
    return True


def _device_fingerprint_from_entry(entry: dict[str, Any] | None, device_identity: dict[str, Any] | None = None) -> str:
    entry = entry if isinstance(entry, dict) else {}
    device_identity = device_identity if isinstance(device_identity, dict) else {}
    stored_identity = entry.get("deviceIdentity") if isinstance(entry.get("deviceIdentity"), dict) else {}
    return (
        _string_value(device_identity.get("publicKeyFingerprint")).strip()
        or _string_value(stored_identity.get("publicKeyFingerprint")).strip()
        or _string_value(stored_identity.get("public_key_fingerprint")).strip()
        or _string_value(entry.get("deviceFingerprint")).strip()
        or _string_value(entry.get("publicKeyFingerprint")).strip()
        or _string_value(entry.get("public_key_fingerprint")).strip()
    )


def _pairing_result(
    identity: dict[str, Any],
    device_id: str,
    device_fingerprint: str,
    trust_state: str,
    message: str,
    ok: bool = True,
    trusted_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = {
        "runtimeId": _string_value(identity.get("runtimeId")),
        "deviceId": _string_value(device_id),
        "trustState": trust_state,
        "message": message,
    }
    if trust_state == "trusted" and isinstance(trusted_entry, dict):
        response["runtimeMetadata"] = _pairing_runtime_metadata(identity)
        response["runtimeAgents"] = runtime_agents(identity)
        session_id = _ensure_trusted_session_id(trusted_entry)
        if session_id:
            response["session_id"] = session_id
    logger.info(
        "RIPDOCK dashboard pairing response trust_state=%s ok=%s fields=%s",
        trust_state,
        ok,
        sorted(response.keys()),
    )
    return response


def _read_saved_session_id() -> str:
    try:
        saved = json.loads(session_file_path().read_text())
    except Exception:
        return ""
    if not isinstance(saved, dict):
        return ""
    session_id = saved.get("session_id")
    return session_id if isinstance(session_id, str) and session_id.strip() else ""


def _ensure_trusted_session_id(entry: dict[str, Any]) -> str:
    existing = _string_value(entry.get("session_id") or entry.get("sessionId")).strip()
    if existing:
        entry["session_id"] = existing
        return existing
    session_id = _read_saved_session_id() or str(uuid.uuid4())
    entry["session_id"] = session_id
    return session_id


def _pairing_runtime_metadata(identity: dict[str, Any]) -> dict[str, Any]:
    metadata = runtime_metadata(identity)
    return {
        "displayName": metadata.get("displayName") or _string_value(identity.get("displayName")) or "Hermes",
        "icon": metadata.get("icon") or None,
        "accentColor": metadata.get("accentColor") or None,
        "backgroundColor": metadata.get("backgroundColor") or "#ffffff",
    }


def pairing_status(payload: dict[str, Any]) -> dict[str, Any]:
    identity = load_runtime_identity()
    pending, trusted, revoked, rejected = _ensure_device_maps(identity)
    device_identity = _device_identity_from_request(payload)
    device_id = device_identity.get("deviceId")
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId is required.")
    public_key = device_identity.get("publicKey")
    public_key_fingerprint = _string_value(device_identity.get("publicKeyFingerprint")).strip()
    if public_key is not None and public_key_fingerprint != _p256_jwk_key_id(public_key):
        raise HTTPException(status_code=400, detail="publicKeyFingerprint must equal publicKey.key_id.")
    trusted_entry = trusted.get(device_id)
    if isinstance(trusted_entry, dict):
        if not _device_fingerprint_matches(trusted_entry, device_identity):
            return _pairing_result(identity, device_id, _device_fingerprint_from_entry(trusted_entry, device_identity), "identityMismatch", "Device identity does not match the trusted key.")
        result = _pairing_result(identity, device_id, _device_fingerprint_from_entry(trusted_entry, device_identity), "trusted", "Device is trusted.", trusted_entry=trusted_entry)
        save_runtime_identity(identity)
        return result
    pending_entry = pending.get(device_id)
    if isinstance(pending_entry, dict):
        if _is_expired_entry(pending_entry):
            return _pairing_result(identity, device_id, _device_fingerprint_from_entry(pending_entry, device_identity), "expired", "Pairing request expired.")
        return _pairing_result(identity, device_id, _device_fingerprint_from_entry(pending_entry, device_identity), "pendingApproval", "Device is pending approval.")
    revoked_entry = revoked.get(device_id)
    if isinstance(revoked_entry, dict):
        return _pairing_result(identity, device_id, _device_fingerprint_from_entry(revoked_entry, device_identity), "revoked", "Device trust was revoked.")
    rejected_entry = rejected.get(device_id)
    if (
        isinstance(rejected_entry, dict)
        and _device_fingerprint_matches(rejected_entry, device_identity)
        and _is_rejected_entry_queryable(rejected_entry)
    ):
        return _pairing_result(identity, device_id, _device_fingerprint_from_entry(rejected_entry, device_identity), "rejected", "Pairing request rejected.")
    return _pairing_result(identity, device_id, _device_fingerprint_from_entry(None, device_identity), "notFound", "Device pairing state was not found.")


def _pending_device_matches(entry_key: str, entry: dict[str, Any], device_id: str) -> bool:
    if entry_key == device_id:
        return True
    device_identity = entry.get("deviceIdentity") if isinstance(entry, dict) else {}
    if not isinstance(device_identity, dict):
        device_identity = {}
    candidates = {
        _string_value(entry.get("deviceId")).strip(),
        _string_value(entry.get("device_id")).strip(),
        _string_value(entry.get("deviceFingerprint")).strip(),
        _string_value(entry.get("publicKeyFingerprint")).strip(),
        _string_value(entry.get("public_key_fingerprint")).strip(),
        _string_value(entry.get("requestId")).strip(),
        _string_value(device_identity.get("deviceId")).strip(),
        _string_value(device_identity.get("device_id")).strip(),
        _string_value(device_identity.get("publicKeyFingerprint")).strip(),
        _string_value(device_identity.get("public_key_fingerprint")).strip(),
    }
    return device_id in candidates


def _pop_pending_device(pending: dict[str, Any], device_id: str) -> tuple[str | None, dict[str, Any] | None]:
    entry = pending.pop(device_id, None)
    if isinstance(entry, dict):
        return device_id, entry
    for key, value in list(pending.items()):
        if isinstance(value, dict) and _pending_device_matches(str(key), value, device_id):
            return str(key), pending.pop(key)
    return None, None


def _pop_trusted_device(trusted: dict[str, Any], device_id: str) -> tuple[str | None, dict[str, Any] | None]:
    entry = trusted.pop(device_id, None)
    if isinstance(entry, dict):
        return device_id, entry
    for key, value in list(trusted.items()):
        if isinstance(value, dict) and _pending_device_matches(str(key), value, device_id):
            return str(key), trusted.pop(key)
    return None, None


def pairing_request(payload: dict[str, Any]) -> dict[str, Any]:
    identity = load_runtime_identity()
    pending, trusted, revoked, rejected = _ensure_device_maps(identity)
    device_identity = _device_identity_from_request(payload)
    device_id = device_identity.get("deviceId")
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId is required.")
    public_key = device_identity.get("publicKey")
    public_key_fingerprint = _string_value(device_identity.get("publicKeyFingerprint")).strip()
    if not isinstance(public_key, dict):
        raise HTTPException(status_code=400, detail="publicKey is required.")
    if public_key_fingerprint != _p256_jwk_key_id(public_key):
        raise HTTPException(status_code=400, detail="publicKeyFingerprint must equal publicKey.key_id.")

    pending_entry = pending.get(device_id)
    trusted_entry = trusted.get(device_id)
    revoked_entry = revoked.get(device_id)
    previous_state = _device_state(pending, trusted, revoked, device_id)
    deleted_records = _deleted_pending_records()
    deleted_match = bool(_summary_identifiers({
        "deviceId": device_identity.get("deviceId"),
        "publicKeyFingerprint": device_identity.get("publicKeyFingerprint"),
    }).intersection(deleted_records.keys()))
    logger.info(
        "RIPDOCK dashboard pairing request device_id=%s fingerprint=%s public_key_present=%s pending_match=%s trusted_match=%s revoked_match=%s deleted_marker_match=%s",
        device_id,
        device_identity.get("publicKeyFingerprint"),
        bool(device_identity.get("publicKey")),
        isinstance(pending_entry, dict),
        isinstance(trusted_entry, dict),
        isinstance(revoked_entry, dict),
        deleted_match,
    )

    now = _now_iso()
    if isinstance(trusted_entry, dict):
        if not _device_fingerprint_matches(trusted_entry, device_identity):
            return _pairing_result(identity, device_id, _device_fingerprint_from_entry(trusted_entry, device_identity), "identityMismatch", "Device identity does not match the trusted key.")
        trusted_entry["lastSeen"] = now
        trusted_entry["trustState"] = "trusted"
        _ensure_trusted_session_id(trusted_entry)
        save_runtime_identity(identity)
        _log_transition("refreshStatus", device_id, previous_state, "trusted", pending, trusted)
        return _pairing_result(identity, device_id, _device_fingerprint_from_entry(trusted_entry, device_identity), "trusted", "Device is trusted.", trusted_entry=trusted_entry)

    _clear_deleted_pending_device(device_identity)
    revoked.pop(device_id, None)
    rejected.pop(device_id, None)
    pending[device_id] = {
        "deviceIdentity": device_identity,
        "requestedAt": now,
        "claimedAt": now,
        "expiresAt": None,
        "trustState": "pendingApproval",
    }
    try:
        save_runtime_identity(identity)
    except Exception as exc:
        logger.error("RIPDOCK dashboard pairing request failed to persist pending Device device_id=%s error=%s", device_id, repr(exc))
        raise HTTPException(status_code=500, detail="Pending Device was not persisted.") from exc
    persisted_identity = load_runtime_identity()
    persisted_pending = persisted_identity.get("pendingDevices") if isinstance(persisted_identity.get("pendingDevices"), dict) else {}
    if not isinstance(persisted_pending.get(device_id), dict):
        logger.error("RIPDOCK dashboard pairing request did not persist pending Device device_id=%s", device_id)
        raise HTTPException(status_code=500, detail="Pending Device was not persisted.")
    pending_count = len(persisted_pending)
    logger.info("RIPDOCK dashboard pairing request wrote pending Device device_id=%s pending_count=%s", device_id, pending_count)
    _log_transition("requestPairing", device_id, previous_state, "pendingApproval", persisted_pending, persisted_identity.get("trustedDevices") if isinstance(persisted_identity.get("trustedDevices"), dict) else {})
    return _pairing_result(identity, device_id, _device_fingerprint_from_entry(persisted_pending.get(device_id), device_identity), "pendingApproval", "Device is pending approval.")


def approve_device(device_id: str) -> dict[str, Any]:
    identity = load_runtime_identity()
    pending, trusted, _revoked, _rejected = _ensure_device_maps(identity)
    previous_state = _device_state(pending, trusted, _revoked, device_id)
    _entry_key, entry = _pop_pending_device(pending, device_id)
    if not isinstance(entry, dict):
        trusted_entry = trusted.get(device_id)
        if isinstance(trusted_entry, dict):
            _ensure_trusted_session_id(trusted_entry)
            save_runtime_identity(identity)
            persisted = load_runtime_identity()
            persisted_pending = persisted.get("pendingDevices") if isinstance(persisted.get("pendingDevices"), dict) else {}
            persisted_trusted = persisted.get("trustedDevices") if isinstance(persisted.get("trustedDevices"), dict) else {}
            _log_transition("approve", device_id, previous_state, "trusted", persisted_pending, persisted_trusted)
            return {
                "ok": True,
                "deviceId": device_id,
                "trustState": "trusted",
                "session_id": _string_value(trusted_entry.get("session_id")),
                "noop": True,
                "state": dashboard_state(),
            }
        raise HTTPException(status_code=404, detail="Pending Device not found.")
    device_identity = entry.get("deviceIdentity") if isinstance(entry.get("deviceIdentity"), dict) else {}
    public_key = device_identity.get("publicKey") or entry.get("publicKey") or entry.get("devicePublicKey")
    if not isinstance(public_key, dict) or not _valid_p256_jwk_public_key(public_key):
        raise HTTPException(status_code=400, detail="Pending Device is missing publicKey and cannot be approved.")
    fingerprint = _device_fingerprint_from_entry(entry, device_identity)
    if fingerprint != _p256_jwk_key_id(public_key):
        raise HTTPException(status_code=400, detail="Pending Device publicKeyFingerprint must equal publicKey.key_id.")
    now = _now_iso()
    entry["approvedAt"] = now
    entry["lastSeen"] = now
    entry["trustState"] = "trusted"
    _ensure_trusted_session_id(entry)
    trusted[device_id] = entry
    save_runtime_identity(identity)
    persisted = load_runtime_identity()
    persisted_pending = persisted.get("pendingDevices") if isinstance(persisted.get("pendingDevices"), dict) else {}
    persisted_trusted = persisted.get("trustedDevices") if isinstance(persisted.get("trustedDevices"), dict) else {}
    if any(isinstance(value, dict) and _pending_device_matches(str(key), value, device_id) for key, value in persisted_pending.items()):
        raise HTTPException(status_code=500, detail="Approved Device remained pending after persistence.")
    if not any(isinstance(value, dict) and _pending_device_matches(str(key), value, device_id) for key, value in persisted_trusted.items()):
        raise HTTPException(status_code=500, detail="Approved Device was not persisted as trusted.")
    _log_transition("approve", device_id, previous_state, "trusted", persisted_pending, persisted_trusted)
    return {
        "ok": True,
        "deviceId": device_id,
        "trustState": "trusted",
        "session_id": _string_value(entry.get("session_id")),
        "state": dashboard_state(),
    }


def reject_device(device_id: str) -> dict[str, Any]:
    identity = load_runtime_identity()
    pending, _trusted, _revoked, rejected = _ensure_device_maps(identity)
    previous_state = _device_state(pending, _trusted, _revoked, device_id)
    _entry_key, entry = _pop_pending_device(pending, device_id)
    if not isinstance(entry, dict):
        _remember_deleted_pending_device(device_id, dashboard_state())
        return {
            "ok": True,
            "deviceId": device_id,
            "trustState": "notFound",
            "noop": True,
            "message": "Pending Device was already gone.",
            "state": dashboard_state(),
        }
    entry["rejectedAt"] = _now_iso()
    entry["reason"] = "dashboardRejected"
    entry["trustState"] = "rejected"
    rejected[device_id] = entry
    save_runtime_identity(identity)
    _remember_deleted_pending_device(device_id)
    persisted = load_runtime_identity()
    persisted_pending = persisted.get("pendingDevices") if isinstance(persisted.get("pendingDevices"), dict) else {}
    persisted_trusted = persisted.get("trustedDevices") if isinstance(persisted.get("trustedDevices"), dict) else {}
    _log_transition("reject", device_id, previous_state, "rejected", persisted_pending, persisted_trusted)
    return {
        "ok": True,
        "deviceId": device_id,
        "trustState": "rejected",
        "noop": False,
        "message": "Pending Device removed.",
        "state": dashboard_state(),
    }


def revoke_device(device_id: str) -> dict[str, Any]:
    identity = load_runtime_identity()
    pending, trusted, revoked, rejected = _ensure_device_maps(identity)
    previous_state = _device_state(pending, trusted, revoked, device_id)
    logger.info(
        "RIPDOCK dashboard revoke route entry device_id=%s trusted_keys=%s pending_keys=%s",
        device_id,
        sorted(str(key) for key in trusted.keys()),
        sorted(str(key) for key in pending.keys()),
    )
    entry_key, entry = _pop_trusted_device(trusted, device_id)
    if not isinstance(entry, dict):
        entry_key, entry = _pop_pending_device(pending, device_id)
    logger.info(
        "RIPDOCK dashboard revoke route match device_id=%s matched_key=%s matched=%s",
        device_id,
        entry_key,
        isinstance(entry, dict),
    )
    if not isinstance(entry, dict):
        raise HTTPException(status_code=404, detail="Device not found.")
    entry["revokedAt"] = _now_iso()
    entry["trustState"] = "revoked"
    revoked[device_id] = entry
    rejected.pop(device_id, None)
    save_runtime_identity(identity)
    persisted = load_runtime_identity()
    persisted_pending = persisted.get("pendingDevices") if isinstance(persisted.get("pendingDevices"), dict) else {}
    persisted_trusted = persisted.get("trustedDevices") if isinstance(persisted.get("trustedDevices"), dict) else {}
    if any(isinstance(value, dict) and _pending_device_matches(str(key), value, device_id) for key, value in persisted_pending.items()):
        raise HTTPException(status_code=500, detail="Revoked Device remained pending after persistence.")
    if any(isinstance(value, dict) and _pending_device_matches(str(key), value, device_id) for key, value in persisted_trusted.items()):
        raise HTTPException(status_code=500, detail="Revoked Device remained trusted after persistence.")
    _log_transition("revoke", device_id, previous_state, "revoked", persisted_pending, persisted_trusted)
    return {"ok": True, "deviceId": device_id, "trustState": "revoked", "state": dashboard_state()}


def _model_payload(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        payload = model.model_dump()
        return payload if isinstance(payload, dict) else {}
    payload = getattr(model, "__dict__", {})
    return payload if isinstance(payload, dict) else {}


def runtime_metadata(identity: dict[str, Any] | None = None) -> dict[str, Any]:
    return _stored_runtime_metadata(identity)


def pairing_runtime_metadata(identity: dict[str, Any] | None = None) -> dict[str, Any]:
    identity = identity if isinstance(identity, dict) else load_runtime_identity()
    return _pairing_runtime_metadata(identity)


def is_device_facing_runtime_url(value: str | None) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parts = urlsplit(value.strip())
    except Exception:
        return False
    if parts.scheme != "https":
        return False
    hostname = (parts.hostname or "").lower()
    if hostname in {"localhost", "host.docker.internal", "runtime", "caddy", "0.0.0.0", "127.0.0.1", "::1"}:
        return False
    if hostname.endswith(".local") or hostname.endswith(".internal"):
        return False
    if hostname.startswith("127.") or hostname.startswith("10.") or hostname.startswith("192.168."):
        return False
    parts_172 = hostname.split(".")
    if len(parts_172) >= 2 and parts_172[0] == "172":
        try:
            if 16 <= int(parts_172[1]) <= 31:
                return False
        except ValueError:
            pass
    return True


def detected_public_runtime_url() -> str | None:
    try:
        value = public_runtime_url_file().read_text().strip()
        return value or None
    except Exception:
        return None


def configured_public_runtime_url() -> str:
    state_value = load_state().get("publicURLOverride")
    if isinstance(state_value, str) and state_value.strip():
        return state_value.strip()
    return os.getenv("RIPDOCK_PUBLIC_RUNTIME_URL", "").strip()


def public_ripdock_url() -> str | None:
    configured = configured_public_runtime_url()
    if is_device_facing_runtime_url(configured):
        return configured.rstrip("/")
    detected = detected_public_runtime_url()
    if is_device_facing_runtime_url(detected):
        return detected.rstrip("/")
    return None


def pairing_ttl_seconds() -> int:
    raw_value = os.getenv("RIPDOCK_PAIRING_TTL_SECONDS", "").strip()
    if not raw_value:
        return DEFAULT_PAIRING_TTL_SECONDS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_PAIRING_TTL_SECONDS
    return min(MAX_PAIRING_TTL_SECONDS, max(MIN_PAIRING_TTL_SECONDS, value))


def _device_summary(entry: dict[str, Any]) -> dict[str, Any]:
    device_identity = entry.get("deviceIdentity") if isinstance(entry, dict) else {}
    if not isinstance(device_identity, dict):
        device_identity = {}
    return {
        "deviceName": device_identity.get("deviceName") or device_identity.get("name") or entry.get("deviceName") or entry.get("name") or "",
        "deviceId": device_identity.get("deviceId") or device_identity.get("device_id") or entry.get("deviceId") or entry.get("device_id") or "",
        "deviceFingerprint": device_identity.get("publicKeyFingerprint") or device_identity.get("public_key_fingerprint") or entry.get("deviceFingerprint") or entry.get("publicKeyFingerprint") or entry.get("public_key_fingerprint") or "",
        "claimedTime": entry.get("requestedAt") or entry.get("claimedAt") or entry.get("claimedTime"),
        "approvedTime": entry.get("approvedAt"),
        "lastSeen": entry.get("lastSeen"),
        "expiresAt": entry.get("expiresAt"),
        "revokedAt": entry.get("revokedAt"),
        "status": entry.get("trustState") or entry.get("status", "unknown"),
    }


def admin_base_url() -> str | None:
    configured = os.getenv("RIPDOCK_RUNTIME_ADMIN_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    host = os.getenv("RIPDOCK_HOST_RELAY_HOST", "").strip()
    port = os.getenv("RIPDOCK_HOST_RELAY_PORT", "").strip()
    if host and port:
        return f"http://{host}:{port}"
    host = os.getenv("HERMES_DASHBOARD_HOST", "").strip() or os.getenv("RIPDOCK_EMBEDDED_HOST", "").strip() or "localhost"
    if host in {"0.0.0.0", "::", ""}:
        host = "localhost"
    if host == "::1":
        host = "[::1]"
    port = os.getenv("RIPDOCK_EMBEDDED_PORT", "").strip() or "8788"
    return f"http://{host}:{port}"


def _proxy_json(method: str, path: str, body: dict[str, Any] | None = None, route_method: str | None = None) -> tuple[int, dict[str, Any]]:
    base = admin_base_url()
    if not base:
        return 503, {"ok": False, "implemented": False, "message": "Runtime admin API is not configured."}
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"accept": "application/json"}
    if route_method:
        headers["method"] = route_method
        if body is not None:
            headers["x-ripdock-body"] = json.dumps(body)
    elif body is not None:
        headers["content-type"] = "application/json"
    request = Request(
        base + path,
        data=None if route_method else data,
        method=method,
        headers=headers,
    )
    logger.info("RIPDOCK dashboard Runtime proxy request raw_method=%s route_method=%s url=%s", method, route_method or "", base + path)
    try:
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            logger.info("RIPDOCK dashboard Runtime proxy response raw_method=%s route_method=%s url=%s status=%s", method, route_method or "", base + path, response.status)
            return response.status, payload if isinstance(payload, dict) else {"value": payload}
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {"ok": False, "message": str(exc)}
        logger.info("RIPDOCK dashboard Runtime proxy response raw_method=%s route_method=%s url=%s status=%s", method, route_method or "", base + path, exc.code)
        return exc.code, payload if isinstance(payload, dict) else {"value": payload}
    except (URLError, TimeoutError, OSError) as exc:
        return 503, {"ok": False, "implemented": False, "message": f"Runtime admin API unavailable: {exc}"}


def _local_device_action(device_id: str, action: str) -> dict[str, Any]:
    if action == "approve":
        return approve_device(device_id)
    if action == "reject":
        return reject_device(device_id)
    if action == "revoke":
        return revoke_device(device_id)
    raise HTTPException(status_code=400, detail="Unsupported Device action.")


def _admin_device_action(device_id: str, action: str) -> dict[str, Any]:
    device_id = _string_value(device_id).strip()
    action = _string_value(action).strip()
    if not device_id:
        if action == "revoke":
            logger.error("RIPDOCK dashboard revoke 400 device_id_missing action=%s", action)
        raise HTTPException(status_code=400, detail="deviceId is required for Device admin action.")
    if action not in {"approve", "reject", "revoke"}:
        if action == "revoke":
            logger.error("RIPDOCK dashboard revoke 400 unsupported_action action=%s device_id=%s", action, device_id)
        raise HTTPException(status_code=400, detail=f"Unsupported Device action: {action or '(empty)'}.")

    body = {"deviceId": device_id, "action": action}
    prior_state = dashboard_state() if action == "reject" else None
    logger.info("RIPDOCK dashboard Device action action=%s device_id=%s", action, device_id)
    runtime_path = f"/ripdock/admin/devices/{quote(device_id, safe='')}/{action}"
    route_method = "POST"
    raw_method = "GET"
    status, payload = _proxy_json(raw_method, runtime_path, body, route_method)
    if action == "revoke":
        logger.info("RIPDOCK dashboard revoke Runtime response status=%s payload=%s", status, payload)
    if 200 <= status < 300:
        if action == "reject":
            _remember_deleted_pending_device(device_id, prior_state)
            if isinstance(payload.get("state"), dict):
                payload = dict(payload)
                payload["state"] = _filter_pending_devices(payload["state"])
        if action == "approve":
            state_payload = payload.get("state") if isinstance(payload.get("state"), dict) else dashboard_state()
            if _state_contains_device(state_payload, "pendingDevices", device_id) or not _state_contains_device(state_payload, "trustedDevices", device_id):
                raise HTTPException(status_code=502, detail=f"Approve reported success but Runtime admin state did not trust deviceId {device_id}.")
            payload = dict(payload)
            payload["state"] = state_payload
        return payload

    if status in {400, 404, 405}:
        if action == "revoke" and status == 400:
            logger.error("RIPDOCK dashboard revoke Runtime path returned 400 device_id=%s payload=%s", device_id, payload)
        fallback_path = f"/ripdock/admin/devices/{action}"
        if action == "revoke":
            logger.info("RIPDOCK dashboard revoke Runtime fallback request raw_method=%s route_method=%s url=%s", raw_method, route_method, (admin_base_url() or "") + fallback_path)
        fallback_status, fallback_payload = _proxy_json(raw_method, fallback_path, body, route_method)
        if action == "revoke":
            logger.info("RIPDOCK dashboard revoke Runtime fallback response status=%s payload=%s", fallback_status, fallback_payload)
        if 200 <= fallback_status < 300:
            if action == "reject":
                _remember_deleted_pending_device(device_id, prior_state)
                if isinstance(fallback_payload.get("state"), dict):
                    fallback_payload = dict(fallback_payload)
                    fallback_payload["state"] = _filter_pending_devices(fallback_payload["state"])
            return fallback_payload
        if fallback_status != 503:
            status, payload = fallback_status, fallback_payload

    if status == 503 and payload.get("implemented") is False:
        return _local_device_action(device_id, action)

    if action == "revoke" and status == 400:
        logger.error("RIPDOCK dashboard revoke final 400 device_id=%s payload=%s", device_id, payload)

    if action == "reject" and status in {400, 404}:
        message = _string_value(payload.get("message") or payload.get("detail"))
        if "not found" in message.lower() or "pending" in message.lower() or "bad request" in message.lower():
            _remember_deleted_pending_device(device_id, prior_state)
            state_payload = dashboard_state()
            return {
                "ok": True,
                "deviceId": device_id,
                "trustState": "notFound",
                "noop": True,
                "message": "Pending Device was already gone.",
                "state": state_payload,
            }

    detail = payload.get("message") or payload.get("detail") or f"{action.capitalize()} failed for deviceId {device_id}."
    raise HTTPException(status_code=status, detail=detail)


def _pairing_runtime_url(value: str | None, pairing_code: str) -> str | None:
    if not is_device_facing_runtime_url(value):
        return None
    parts = urlsplit(str(value).strip().rstrip("/"))
    base_path = parts.path.rstrip("/")
    pairing_path = f"{base_path}/ripdock/app/pair/{quote(str(pairing_code), safe='')}"
    return urlunsplit(("wss", parts.netloc, pairing_path, "", ""))


def _local_admin_state() -> dict[str, Any]:
    identity = load_runtime_identity()
    configured = configured_public_runtime_url()
    detected = detected_public_runtime_url()
    active = public_ripdock_url()
    pending = identity.get("pendingDevices") if isinstance(identity.get("pendingDevices"), dict) else {}
    trusted = identity.get("trustedDevices") if isinstance(identity.get("trustedDevices"), dict) else {}
    revoked = identity.get("revokedDevices") if isinstance(identity.get("revokedDevices"), dict) else {}
    state = load_state()
    return _apply_device_labels(_filter_pending_devices({
        "runtimeIdentity": _public_identity(identity),
        "runtimeMetadata": runtime_metadata(identity),
        "runtimeAgents": runtime_agents(identity),
        "publicURL": {
            "configured": configured,
            "detectedTunnelURL": detected,
            "active": active,
            "pairingQRAvailable": False,
            "detectedTunnelURLUsable": is_device_facing_runtime_url(detected),
            "message": "Public RIPDOCK URL is used for Device-facing Runtime connections.",
        },
        "pairingSettings": {
            "pairingTTLSeconds": pairing_ttl_seconds(),
            "minPairingTTLSeconds": MIN_PAIRING_TTL_SECONDS,
            "maxPairingTTLSeconds": MAX_PAIRING_TTL_SECONDS,
            "productionPairingTTLSeconds": PRODUCTION_PAIRING_TTL_SECONDS,
            "pairingQRAvailable": False,
            "pairingCodeOnlyAvailable": True,
        },
        "pendingDevices": [_device_summary(value) for value in pending.values() if isinstance(value, dict) and not _is_expired_entry(value)],
        "trustedDevices": [_device_summary(value) for value in trusted.values() if isinstance(value, dict)],
        "revokedDevices": [_device_summary(value) for value in revoked.values() if isinstance(value, dict)],
        "backend": {
            "source": "local",
            "runtimeAdminAvailable": False,
            "actionsImplemented": False,
            "message": "Runtime admin API is unavailable; device actions are shown as not implemented.",
        },
        "security": {
            "runtimeFingerprint": identity.get("publicKeyFingerprint"),
            "trustAnchorWarning": "RuntimeIdentity public key fingerprint is the trust anchor.",
            "publicURLWarning": "A public URL change does not change RuntimeIdentity or its fingerprint.",
        },
    }))


def _with_saved_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    identity = load_runtime_identity()
    metadata = runtime_metadata(identity)
    agents = runtime_agents(identity)
    configured = configured_public_runtime_url()
    detected = detected_public_runtime_url()
    active = public_ripdock_url()
    merged = dict(payload)
    merged["runtimeMetadata"] = metadata
    merged["runtimeAgents"] = agents
    proxied_public_url = merged.get("publicURL") if isinstance(merged.get("publicURL"), dict) else {}
    merged["publicURL"] = {
        **proxied_public_url,
        "configured": configured,
        "detectedTunnelURL": detected,
        "active": active,
        "detectedTunnelURLUsable": is_device_facing_runtime_url(detected),
    }
    runtime_identity = merged.get("runtimeIdentity")
    if isinstance(runtime_identity, dict):
        merged_identity = dict(runtime_identity)
        merged_identity["displayName"] = metadata["displayName"]
        merged_identity["metadata"] = metadata
        merged["runtimeIdentity"] = merged_identity
    return _apply_device_labels(_filter_pending_devices(merged))


def dashboard_state() -> dict[str, Any]:
    status, proxied = _proxy_json("GET", "/ripdock/admin/state")
    if 200 <= status < 300:
        proxied["backend"] = {
            "source": "runtime-admin",
            "runtimeAdminAvailable": True,
            "actionsImplemented": True,
            "message": "Dashboard is connected to the Runtime admin API.",
        }
        return _with_saved_metadata(proxied)
    state = _local_admin_state()
    state["backend"]["lastProxyStatus"] = status
    state["backend"]["lastProxyMessage"] = proxied.get("message")
    return state


requestPairing = pairing_request
refreshStatus = pairing_status
approveDevice = approve_device
rejectDevice = reject_device
revokeDevice = revoke_device
labelDevice = update_device_label
getDeviceTrustState = pairing_status
buildAdminState = dashboard_state
buildPublicPairingResponse = _pairing_result


def generate_pairing_payload() -> dict[str, Any]:
    status, proxied = _proxy_json("POST", "/ripdock/admin/pairing-payloads")
    if 200 <= status < 300 and isinstance(proxied.get("pairingCode"), str):
        pairing_code = proxied["pairingCode"]
        runtime_url = _pairing_runtime_url(public_ripdock_url(), pairing_code)
        if runtime_url:
            identity = load_runtime_identity()
            payload = {
                "runtime_url": runtime_url,
                "runtime_id": identity.get("runtimeId"),
                "runtime_public_key_fingerprint": identity.get("publicKeyFingerprint"),
                "runtime_identity": _public_identity(identity),
                "pairing_code": pairing_code,
            }
            proxied["pairingPayload"] = payload
        proxied.setdefault("securityNotice", "QR/code does not grant trust by itself. Hermes must approve each DeviceIdentity.")
        return proxied

    identity = load_runtime_identity()
    pairing_code = f"{secrets.randbelow(1_000_000):06d}"
    session_id = str(uuid.uuid4())
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + pairing_ttl_seconds()))
    runtime_url = _pairing_runtime_url(public_ripdock_url(), pairing_code)
    pairing_payload = None
    if runtime_url:
        pairing_payload = {
            "runtime_url": runtime_url,
            "runtime_id": identity.get("runtimeId"),
            "runtime_public_key_fingerprint": identity.get("publicKeyFingerprint"),
            "runtime_identity": _public_identity(identity),
            "pairing_code": pairing_code,
        }
    return {
        "pairingCode": pairing_code,
        "sessionId": session_id,
        "pairingPayload": pairing_payload,
        "expiresAt": expires_at,
        "securityNotice": "QR/code does not grant trust by itself. Hermes must approve each DeviceIdentity.",
        "backend": {
            "source": "local",
            "runtimeAdminAvailable": False,
            "actionsImplemented": False,
            "message": proxied.get("message", "Runtime admin API unavailable; generated local dashboard Pairing payload."),
        },
    }


@router.get("/state")
async def get_state():
    return dashboard_state()


@router.post("/public-url")
async def post_public_url(body: PublicURLBody):
    value = (body.publicURL or "").strip().rstrip("/")
    if value and not is_device_facing_runtime_url(value):
        raise HTTPException(status_code=400, detail="Public RIPDOCK URL must be a device-facing https URL; localhost and private/internal hosts are not allowed.")
    state = load_state()
    state["publicURLOverride"] = value
    save_state(state)
    save_public_runtime_url_file(value)
    return {"ok": True, "publicURL": value, "state": dashboard_state()}


@router.post("/metadata")
async def post_metadata(body: MetadataBody):
    metadata = save_runtime_metadata(
        {
            "displayName": getattr(body, "displayName", ""),
            "icon": getattr(body, "icon", ""),
            "accentColor": getattr(body, "accentColor", ""),
            "backgroundColor": getattr(body, "backgroundColor", ""),
        }
    )
    save_runtime_identity(load_runtime_identity())
    return {"ok": True, "runtimeMetadata": metadata, "state": dashboard_state()}


@router.post("/agents/{agent_id}/metadata")
async def post_agent_metadata(agent_id: str, body: AgentMetadataBody):
    metadata = save_agent_metadata(
        agent_id,
        {
            "displayName": getattr(body, "displayName", ""),
            "icon": getattr(body, "icon", ""),
            "accentColor": getattr(body, "accentColor", ""),
            "backgroundColor": getattr(body, "backgroundColor", ""),
            "sortOrder": getattr(body, "sortOrder", None),
            "enabled": getattr(body, "enabled", True),
        },
    )
    return {"ok": True, "agentMetadata": metadata, "runtimeAgents": runtime_agents(load_runtime_identity()), "state": dashboard_state()}


@router.post("/pairing-payloads")
async def post_pairing_payload():
    return generate_pairing_payload()


@router.post("/pairing/request")
async def post_pairing_request(body: PairingRequestBody):
    return pairing_request(_model_payload(body))


@router.get("/pairing/status")
async def get_pairing_status(deviceId: str = "", device_id: str = "", deviceFingerprint: str = "", publicKeyFingerprint: str = ""):
    return pairing_status({
        "deviceId": deviceId or device_id,
        "publicKeyFingerprint": publicKeyFingerprint or deviceFingerprint,
    })


@router.post("/pairing/status")
async def post_pairing_status(body: PairingRequestBody):
    return pairing_status(_model_payload(body))


@router.post("/devices/{device_id}/approve")
async def post_device_approve(device_id: str):
    return _admin_device_action(device_id, "approve")


@router.post("/devices/{device_id}/reject")
async def post_device_reject(device_id: str):
    return _admin_device_action(device_id, "reject")


@router.post("/devices/{deviceId}/revoke")
async def post_device_revoke(deviceId: str):
    logger.info("revoke handler entered deviceId=%s", deviceId)
    return _admin_device_action(deviceId, "revoke")


@router.post("/devices/{device_id}/label")
async def post_device_label(device_id: str, body: DeviceLabelBody):
    return update_device_label(device_id, getattr(body, "label", ""))


@router.post("/devices/actions/{action}")
async def post_device_action(action: str, body: DeviceActionBody):
    payload = _model_payload(body)
    return _admin_device_action(payload.get("deviceId", ""), action)
