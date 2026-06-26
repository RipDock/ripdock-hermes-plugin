import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import types
import unittest
from importlib import util
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit


ROOT = Path(__file__).resolve().parents[1]


def load_dashboard_api():
    spec = util.spec_from_file_location("ripdock_dashboard_api_test", ROOT / "dashboard" / "api.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_backend_adapter():
    gateway_module = types.ModuleType("gateway")
    gateway_platforms_module = types.ModuleType("gateway.platforms")
    gateway_base_module = types.ModuleType("gateway.platforms.base")
    gateway_config_module = types.ModuleType("gateway.config")
    gateway_session_module = types.ModuleType("gateway.session")
    websockets_module = types.ModuleType("websockets")

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform

    class PlatformConfig:
        pass

    class Platform(str):
        pass

    gateway_base_module.BasePlatformAdapter = BasePlatformAdapter
    gateway_base_module.MessageEvent = object
    gateway_base_module.MessageType = object
    gateway_base_module.SendResult = object
    gateway_config_module.PlatformConfig = PlatformConfig
    gateway_config_module.Platform = Platform
    gateway_session_module.SessionSource = object
    gateway_session_module.build_session_key = lambda *args, **kwargs: "session-key"
    websockets_module.serve = None
    websockets_module.connect = None

    old_modules = {name: sys.modules.get(name) for name in (
        "gateway",
        "gateway.platforms",
        "gateway.platforms.base",
        "gateway.config",
        "gateway.session",
        "websockets",
    )}
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.modules.update({
        "gateway": gateway_module,
        "gateway.platforms": gateway_platforms_module,
        "gateway.platforms.base": gateway_base_module,
        "gateway.config": gateway_config_module,
        "gateway.session": gateway_session_module,
        "websockets": websockets_module,
    })
    try:
        sys.dont_write_bytecode = True
        spec = util.spec_from_file_location("ripdock_backend_adapter_test", ROOT / "backend" / "adapter.py")
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        for name, old_module in old_modules.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


def load_runtime_app():
    spec = util.spec_from_file_location("ripdock_runtime_app_test", ROOT / "backend" / "runtime_app.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module




class PluginTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._previous_logging_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.disable(cls._previous_logging_disable)

    def _p256_jwk(self, adapter_module, public_key):
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        raw_public_key = public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)[1:]
        x = raw_public_key[:32]
        y = raw_public_key[32:]
        key_id = hashlib.sha256(raw_public_key).hexdigest()
        jwk = {
            "crv": "P-256",
            "key_id": key_id,
            "kty": "EC",
            "x": adapter_module.base64.urlsafe_b64encode(x).decode("ascii").rstrip("="),
            "y": adapter_module.base64.urlsafe_b64encode(y).decode("ascii").rstrip("="),
        }
        return jwk, key_id

    def _sample_p256_jwk(self, key_id=None):
        seed = (key_id or "sample").encode("utf-8")
        x = hashlib.sha256(seed + b":x").digest()
        y = hashlib.sha256(seed + b":y").digest()
        raw_public_key = x + y
        key_id = hashlib.sha256(raw_public_key).hexdigest()
        return {
            "crv": "P-256",
            "key_id": key_id,
            "kty": "EC",
            "x": base64.urlsafe_b64encode(x).decode("ascii").rstrip("="),
            "y": base64.urlsafe_b64encode(y).decode("ascii").rstrip("="),
        }

    def _sample_device_identity(self, device_id="device-1", seed=None, device_name=None, created_at=None):
        public_key = self._sample_p256_jwk(seed or device_id)
        identity = {
            "deviceId": device_id,
            "publicKey": public_key,
            "publicKeyFingerprint": public_key["key_id"],
        }
        if device_name is not None:
            identity["deviceName"] = device_name
        if created_at is not None:
            identity["createdAt"] = created_at
        return identity

    def _signed_resume_fixture(self, nonce="nonce-1", timestamp=None, session_id="session-1", runtime_id="runtime-1", app_device_id="device-1", key_id=None, route="/ripdock/app"):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric import utils
        from cryptography.hazmat.primitives.hashes import SHA256

        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key, public_key_id = self._p256_jwk(adapter_module, private_key.public_key())
        runtime_public_key = self._sample_p256_jwk("runtime")
        message_key_id = key_id or public_key_id
        adapter.runtime_identity = {
            "runtimeId": "runtime-1",
            "displayName": "Hermes",
            "publicKey": runtime_public_key,
            "publicKeyFingerprint": runtime_public_key["key_id"],
            "protocolVersion": "1",
            "createdAt": "2026-01-01T00:00:00Z",
            "trustedDevices": {
                "device-1": {
                    "deviceIdentity": {
                        "deviceId": "device-1",
                        "publicKey": public_key,
                        "publicKeyFingerprint": public_key_id,
                    },
                    "session_id": "session-1",
                    "trustState": "trusted",
                }
            },
            "pendingDevices": {},
            "revokedDevices": {},
            "rejectedDevices": {},
        }
        adapter._save_runtime_identity = lambda: True
        timestamp = timestamp or adapter._now_iso()
        signed = {
            "app_device_id": app_device_id,
            "key_id": message_key_id,
            "nonce": nonce,
            "protocol_version": "1",
            "route": route,
            "runtime_id": runtime_id,
            "session_id": session_id,
            "timestamp": timestamp,
            "type": "session.resume",
        }
        signed_bytes = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        der_signature = private_key.sign(signed_bytes, ec.ECDSA(SHA256()))
        r, s = utils.decode_dss_signature(der_signature)
        raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        message = {
            "type": "session.resume",
            "protocol_version": "1",
            "session_id": session_id,
            "runtime_id": runtime_id,
            "app_device_id": app_device_id,
            "resume_signature": {
                "alg": "ES256",
                "key_id": message_key_id,
                "nonce": nonce,
                "timestamp": timestamp,
                "route": route,
                "signature": adapter_module.base64.urlsafe_b64encode(raw_signature).decode("ascii").rstrip("="),
            },
        }
        return adapter, message

    def _fake_embedded_websocket(self, messages):
        class FakeWebSocket:
            def __init__(self, source_messages):
                self.messages = list(source_messages)
                self.sent = []
                self.close_code = None
                self.close_reason = None

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.messages:
                    raise StopAsyncIteration
                return self.messages.pop(0)

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            async def close(self, code=None, reason=None):
                self.close_code = code
                self.close_reason = reason

        return FakeWebSocket(messages)
