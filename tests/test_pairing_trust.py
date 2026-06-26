from support import *


class PairingTrustTests(PluginTestBase):
    def test_runtime_pairing_payload_uses_pairing_websocket_route(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_id = "runtime-1"
        adapter.runtime_identity = {"publicKeyFingerprint": "SHA256:fingerprint"}
        adapter._public_runtime_identity = types.MethodType(lambda _self: {"runtime_id": "runtime-1"}, adapter)

        old_env = os.environ.copy()
        try:
            os.environ["RIPDOCK_DIRECT_RUNTIME_URL"] = "https://runtime.example/base"
            payload = adapter._direct_pairing_payload("123456", "runtime.example", "443")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertEqual("wss://runtime.example/base/ripdock/app/pair/123456", payload["runtime_url"])
        self.assertEqual("123456", payload["pairing_code"])

    def test_pairing_request_persists_pending_and_approval_flow(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            state_file = Path(directory) / "state.json"
            identity_file = Path(directory) / "runtime-identity.json"
            identity = {
                "runtimeId": "runtime-1",
                "displayName": "Immutable Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {},
                "trustedDevices": {},
                "revokedDevices": {},
            }
            identity_file.write_text(json.dumps(identity))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(state_file)
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)

                payload = {
                    "deviceIdentity": self._sample_device_identity(
                        "device-1",
                        seed="device-public-key",
                        device_name="Dave's iPhone",
                        created_at="2026-01-01T01:00:00Z",
                    )
                }
                pending_result = api.pairing_request(payload)
                first_state = asyncio.run(api.get_state())
                reloaded_api = load_dashboard_api()
                reloaded_state = asyncio.run(reloaded_api.get_state())
                approve_result = asyncio.run(api.post_device_approve("device-1"))
                second_approve_result = asyncio.run(api.post_device_approve("device-1"))
                trusted_refresh = api.pairing_request(payload)
                mismatch = api.pairing_request(
                    {
                        "deviceIdentity": self._sample_device_identity(
                            "device-1",
                            seed="changed-public-key",
                            device_name="Dave's iPhone",
                            created_at="2026-01-01T01:00:00Z",
                        )
                    }
                )
                persisted_identity = json.loads(identity_file.read_text())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("pendingApproval", pending_result["trustState"])
        self.assertEqual("runtime-1", pending_result["runtimeId"])
        self.assertNotIn("ok", pending_result)
        self.assertNotIn("runtimeFingerprint", pending_result)
        self.assertNotIn("publicKeyFingerprint", pending_result)
        self.assertNotIn("deviceFingerprint", pending_result)
        self.assertNotIn("protocolVersion", pending_result)
        self.assertNotIn("state", pending_result)
        self.assertNotIn("runtimeIdentity", pending_result)
        self.assertNotIn("runtimeMetadata", pending_result)
        self.assertNotIn("runtimeAgents", pending_result)
        self.assertEqual(["device-1"], [device["deviceId"] for device in first_state["pendingDevices"]])
        self.assertEqual(["device-1"], [device["deviceId"] for device in reloaded_state["pendingDevices"]])
        self.assertEqual("trusted", approve_result["trustState"])
        self.assertIn("session_id", approve_result)
        self.assertEqual([], approve_result["state"]["pendingDevices"])
        self.assertEqual(["device-1"], [device["deviceId"] for device in approve_result["state"]["trustedDevices"]])
        self.assertEqual("trusted", second_approve_result["trustState"])
        self.assertTrue(second_approve_result["noop"])
        self.assertEqual(approve_result["session_id"], second_approve_result["session_id"])
        self.assertEqual([], second_approve_result["state"]["pendingDevices"])
        self.assertEqual(["device-1"], [device["deviceId"] for device in second_approve_result["state"]["trustedDevices"]])
        self.assertEqual("trusted", trusted_refresh["trustState"])
        self.assertIn("session_id", trusted_refresh)
        self.assertIn("runtimeAgents", trusted_refresh)
        self.assertIsInstance(trusted_refresh["runtimeAgents"], list)
        self.assertEqual(approve_result["session_id"], trusted_refresh["session_id"])
        self.assertEqual("identityMismatch", mismatch["trustState"])
        self.assertNotIn("session_id", pending_result)
        self.assertNotIn("session_id", mismatch)
        self.assertNotIn("runtimeAgents", mismatch)
        self.assertEqual(identity["runtimeId"], persisted_identity["runtimeId"])

    def test_signed_session_resume_accepts_p256_jwk_public_key(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric import utils
        from cryptography.hazmat.primitives.hashes import SHA256

        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key, _key_id = self._p256_jwk(adapter_module, private_key.public_key())
        signed_bytes = b'{"type":"session.resume"}'
        der_signature = private_key.sign(signed_bytes, ec.ECDSA(SHA256()))
        r, s = utils.decode_dss_signature(der_signature)
        raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")

        self.assertTrue(
            adapter._verify_es256_signature(
                public_key,
                adapter_module.base64.urlsafe_b64encode(raw_signature).decode("ascii").rstrip("="),
                signed_bytes,
            )
        )

    def test_signed_session_resume_uses_exact_app_session_route(self):
        adapter, message = self._signed_resume_fixture(route="/base/ripdock/app")

        self.assertEqual((True, "verified"), adapter._verify_signed_session_resume(message, expected_route="/base/ripdock/app"))

        adapter, message = self._signed_resume_fixture(route="/base/ripdock/app")
        self.assertEqual((False, "route"), adapter._verify_signed_session_resume(message, expected_route="/ripdock/app"))

    def test_embedded_app_route_parsing_supports_mounted_app_paths(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())

        self.assertEqual("/ripdock/app", adapter._app_session_route_from_path("/ripdock/app"))
        self.assertEqual("/base/ripdock/app", adapter._app_session_route_from_path("/base/ripdock/app"))
        self.assertEqual("123456", adapter._pairing_code_from_app_route("/ripdock/app/pair/123456"))
        self.assertEqual("123456", adapter._pairing_code_from_app_route("/base/ripdock/app/pair/123456"))

    def test_signed_session_resume_rejects_replay_without_consuming_nonce_on_bad_signature(self):
        adapter, message = self._signed_resume_fixture()
        bad_message = json.loads(json.dumps(message))
        bad_message["resume_signature"]["signature"] = "invalid"
        old_env = os.environ.get("RIPDOCK_ROTATE_SESSION_ON_RESUME")
        os.environ["RIPDOCK_ROTATE_SESSION_ON_RESUME"] = "false"
        try:
            self.assertEqual((False, "signature"), adapter._verify_signed_session_resume(bad_message))
            self.assertEqual((True, "verified"), adapter._verify_signed_session_resume(message))
            self.assertEqual((False, "nonce"), adapter._verify_signed_session_resume(message))
        finally:
            if old_env is None:
                os.environ.pop("RIPDOCK_ROTATE_SESSION_ON_RESUME", None)
            else:
                os.environ["RIPDOCK_ROTATE_SESSION_ON_RESUME"] = old_env

    def test_signed_session_resume_rejects_expired_sessions(self):
        adapter, message = self._signed_resume_fixture()
        adapter.session_created_at = "2026-01-01T00:00:00Z"
        adapter.session_last_seen_at = "2026-01-01T00:00:00Z"
        adapter.session_expires_at = "2026-01-01T00:00:00Z"
        adapter.session_idle_expires_at = adapter._iso_after(60)

        self.assertEqual((False, "session_expired"), adapter._verify_signed_session_resume(message))

        adapter, message = self._signed_resume_fixture()
        adapter.session_created_at = adapter._now_iso()
        adapter.session_last_seen_at = "2026-01-01T00:00:00Z"
        adapter.session_expires_at = adapter._iso_after(60)
        adapter.session_idle_expires_at = "2026-01-01T00:00:00Z"

        self.assertEqual((False, "session_idle_expired"), adapter._verify_signed_session_resume(message))

    def test_resume_security_failures_emit_connection_security_diagnostics(self):
        cases = []

        adapter, message = self._signed_resume_fixture()
        message["runtime_id"] = "changed-runtime"
        cases.append((adapter, message, "error", "session.invalid", "runtimeIdentityMismatch"))

        adapter, message = self._signed_resume_fixture()
        message["resume_signature"]["signature"] = "invalid"
        cases.append((adapter, message, "error", "session.signature_invalid", "invalidSignature"))

        adapter, message = self._signed_resume_fixture(timestamp="2026-01-01T00:00:00Z")
        cases.append((adapter, message, "error", "session.invalid", "staleResumeTimestamp"))

        adapter, message = self._signed_resume_fixture(route="/base/ripdock/app")
        cases.append((adapter, message, "error", "session.invalid", "routeMismatch"))

        adapter, message = self._signed_resume_fixture()
        adapter.runtime_identity["revokedDevices"]["device-1"] = adapter.runtime_identity["trustedDevices"]["device-1"]
        cases.append((adapter, message, "error", "session.invalid", "runtimeRevokedDevice"))

        adapter, message = self._signed_resume_fixture()
        adapter.session_created_at = "2026-01-01T00:00:00Z"
        adapter.session_last_seen_at = "2026-01-01T00:00:00Z"
        adapter.session_expires_at = "2026-01-01T00:00:00Z"
        adapter.session_idle_expires_at = adapter._iso_after(60)
        cases.append((adapter, message, "session.expired", "session.expired", "sessionExpired"))

        old_env = os.environ.get("RIPDOCK_ROTATE_SESSION_ON_RESUME")
        os.environ["RIPDOCK_ROTATE_SESSION_ON_RESUME"] = "false"
        try:
            adapter, message = self._signed_resume_fixture()
            self.assertEqual((True, "verified"), adapter._verify_signed_session_resume(message))
            cases.append((adapter, message, "error", "session.invalid", "reusedResumeNonce"))

            for adapter, message, expected_type, expected_code, expected_diagnostic in cases:
                websocket = self._fake_embedded_websocket([])
                adapter.app_session_route_by_websocket = {websocket: "/ripdock/app"}

                asyncio.run(adapter._handle_embedded_session_resume(websocket, message))

                self.assertEqual(expected_type, websocket.sent[0]["type"])
                self.assertEqual(expected_code, websocket.sent[0]["code"])
                self.assertEqual(expected_diagnostic, websocket.sent[0]["connection_security_error"])
                self.assertEqual(1000, websocket.close_code)
        finally:
            if old_env is None:
                os.environ.pop("RIPDOCK_ROTATE_SESSION_ON_RESUME", None)
            else:
                os.environ["RIPDOCK_ROTATE_SESSION_ON_RESUME"] = old_env

    def test_signed_session_resume_rotates_session_id(self):
        adapter, message = self._signed_resume_fixture()
        with tempfile.TemporaryDirectory() as directory:
            adapter._session_file_path = lambda: Path(directory) / "session.json"
            old_env = os.environ.get("RIPDOCK_ROTATE_SESSION_ON_RESUME")
            os.environ["RIPDOCK_ROTATE_SESSION_ON_RESUME"] = "true"
            try:
                self.assertEqual((True, "verified"), adapter._verify_signed_session_resume(message))
            finally:
                if old_env is None:
                    os.environ.pop("RIPDOCK_ROTATE_SESSION_ON_RESUME", None)
                else:
                    os.environ["RIPDOCK_ROTATE_SESSION_ON_RESUME"] = old_env

            self.assertNotEqual("session-1", adapter.session_id)
            self.assertEqual(adapter.session_id, adapter.runtime_identity["trustedDevices"]["device-1"]["session_id"])
            persisted = json.loads((Path(directory) / "session.json").read_text())
            self.assertEqual(adapter.session_id, persisted["session"]["id"])
            self.assertEqual((False, "session"), adapter._verify_signed_session_resume(message))

    def test_revoke_trusted_device_invalidates_current_session(self):
        adapter, _message = self._signed_resume_fixture()
        adapter.runtime_identity["trustedDevices"]["device-2"] = {
            "deviceIdentity": {"deviceId": "device-2", "publicKeyFingerprint": "SHA256:device-2"},
            "session_id": "session-1",
            "trustState": "trusted",
        }
        adapter._schedule_close_revoked_app_websockets = lambda _device_id: None
        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / "session.json"
            session_file.write_text(json.dumps({"session_id": "session-1"}))
            adapter._session_file_path = lambda: session_file

            result = adapter._revoke_trusted_device("device-1")

        self.assertEqual("revoked", result["trustState"])
        self.assertIsNone(adapter.session_id)
        self.assertNotIn("session_id", adapter.runtime_identity["trustedDevices"]["device-2"])
        self.assertFalse(session_file.exists())

    def test_trusted_device_gets_default_authorization_scopes(self):
        adapter, _message = self._signed_resume_fixture()
        entry = adapter.runtime_identity["trustedDevices"]["device-1"]

        scopes = adapter._ensure_trusted_authorization_scopes(entry)

        self.assertIn("message:create", scopes)
        self.assertIn("conversation:list", scopes)
        self.assertIn("conversation:sync", scopes)
        self.assertIn("conversation:title:generate", scopes)
        self.assertIn("agent:settings:update", scopes)
        self.assertIn("transfer:runtime_to_app:ack", scopes)
        self.assertEqual(sorted(scopes), entry["authorizationScopes"])
        self.assertEqual(sorted(scopes), entry["authorization"]["scopes"])

    def test_trusted_device_scope_migration_adds_new_default_scopes(self):
        adapter, _message = self._signed_resume_fixture()
        entry = adapter.runtime_identity["trustedDevices"]["device-1"]
        entry["authorizationScopes"] = ["message:create"]
        entry["authorization"] = {"scopes": ["message:create"]}

        scopes = adapter._ensure_trusted_authorization_scopes(entry)

        self.assertIn("message:create", scopes)
        self.assertIn("conversation:list", scopes)
        self.assertIn("conversation:sync", scopes)
        self.assertIn("conversation:title:generate", scopes)
        self.assertIn("agent:settings:update", scopes)
        self.assertEqual(sorted(scopes), entry["authorizationScopes"])
        self.assertEqual(sorted(scopes), entry["authorization"]["scopes"])

    def test_authorization_scopes_reject_privileged_messages(self):
        adapter, _message = self._signed_resume_fixture()
        adapter.authenticated_app_websockets = set()
        adapter.authenticated_app_device_by_websocket = {}
        adapter.authenticated_app_scopes_by_websocket = {}
        scheduled = []
        adapter._schedule_message_create = lambda _websocket, message: scheduled.append(message)

        class FakeWebSocket:
            def __init__(self, messages):
                self.messages = list(messages)
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.messages:
                    raise StopAsyncIteration
                return self.messages.pop(0)

            async def send(self, payload):
                self.sent.append(json.loads(payload))

        websocket = FakeWebSocket([
            json.dumps({
                "type": "message.create",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "conversation_id": "conversation-1",
                "client_message_id": "client-message-1",
                "content": "hello",
            })
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"agent:settings:update"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual([], scheduled)
        self.assertEqual("error", websocket.sent[0]["type"])
        self.assertEqual("authorization.denied", websocket.sent[0]["code"])

    def test_privileged_messages_before_resume_are_rejected(self):
        adapter, _message = self._signed_resume_fixture()
        privileged_messages = [
            {"type": "conversation.create", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "personal", "client_message_id": "client-message-0"},
            {"type": "message.create", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c"},
            {"type": "conversation.list", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "personal"},
            {"type": "conversation.sync", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c", "after": "1970-01-01T00:00:00Z"},
            {"type": "conversation.title.generate", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c", "messages": [{"role": "user", "content": "hello"}]},
            {"type": "runtime.settings.update", "runtime_id": "runtime-1", "settings": {}},
            {"type": "agent.settings.update", "runtime_id": "runtime-1", "agent_id": "personal", "settings": {}},
            {"type": "message.cancel", "conversation_id": "c", "message_id": "m"},
            {"type": "transfer.request", "conversation_id": "c", "payload": {"mime_type": "image/png", "size_bytes": 1}},
            {"type": "transfer.ready", "conversation_id": "c", "payload": {"transfer_id": "t", "transfer_url": "wss://runtime.example/ripdock/transfer/t/app"}},
            {"type": "transfer.completed", "conversation_id": "c", "payload": {"transfer_id": "t", "size_bytes": 1}},
            {"type": "transfer.failed", "conversation_id": "c", "payload": {"transfer_id": "t", "code": "x", "message": "x"}},
            {"type": "runtime.transfer.completed", "payload": {"transfer_id": "t", "artifact_id": "a", "size_bytes": 1, "sha256": "0000000000000000000000000000000000000000000000000000000000000000"}},
            {"type": "runtime.transfer.failed", "payload": {"transfer_id": "t", "artifact_id": "a", "code": "x", "message": "x"}},
        ]

        websocket = self._fake_embedded_websocket(json.dumps(message) for message in privileged_messages)

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(len(privileged_messages), len(websocket.sent))
        self.assertTrue(all(event["type"] == "error" for event in websocket.sent))
        self.assertEqual({"session.resume_required"}, {event["code"] for event in websocket.sent})

    def test_unknown_event_after_resume_is_rejected(self):
        adapter, _message = self._signed_resume_fixture()
        websocket = self._fake_embedded_websocket([json.dumps({"type": "unknown.event", "payload": {"ok": True}})])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = adapter._default_authorization_scopes()

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual("protocol.invalid_payload", websocket.sent[0]["code"])
        self.assertIsNone(websocket.close_code)

    def test_existing_conversation_forces_gateway_session_resume(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_id = "runtime-1"
        adapter.config = types.SimpleNamespace(extra={})

        class Entry:
            def __init__(self, session_id):
                self.session_id = session_id

        class Store:
            def __init__(self):
                self._entries = {}
                self.created = []
                self.switched = []

            def _ensure_loaded(self):
                pass

            def get_or_create_session(self, source):
                self.created.append(source)
                self._entries["session-key"] = Entry("fresh-session")
                return self._entries["session-key"]

            def switch_session(self, session_key, target_session_id):
                self.switched.append((session_key, target_session_id))
                self._entries[session_key] = Entry(target_session_id)
                return self._entries[session_key]

        store = Store()
        adapter._session_store = store

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                adapter._remember_profile_session_id("personal", "conversation-1", "default", "old-session")

                resumed, reason = adapter._force_gateway_session_resume("personal", "conversation-1", object())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertTrue(resumed)
        self.assertEqual("resumed", reason)
        self.assertEqual(1, len(store.created))
        self.assertEqual([("session-key", "old-session")], store.switched)
        self.assertEqual("old-session", store._entries["session-key"].session_id)

    def test_new_conversation_remembers_gateway_session_for_later_resume(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_id = "runtime-1"
        adapter.config = types.SimpleNamespace(extra={})

        class Entry:
            session_id = "gateway-session"

        class Store:
            def __init__(self):
                self._entries = {"session-key": Entry()}

            def _ensure_loaded(self):
                pass

        adapter._session_store = Store()

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                session_id = adapter._remember_gateway_profile_session_id("personal", "conversation-1", "default", object())
                remembered = adapter._profile_session_id("personal", "conversation-1")
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("gateway-session", session_id)
        self.assertEqual("gateway-session", remembered)

    def test_conversation_list_rejects_unknown_fields(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        adapter._handle_conversation_list = lambda _websocket, _message: self.fail("conversation.list handler reached")
        websocket = self._fake_embedded_websocket([
            json.dumps({
                "type": "conversation.list",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "after": "1970-01-01T00:00:00Z",
            })
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"conversation:list"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual("protocol.invalid_payload", websocket.sent[0]["code"])

    def test_conversation_title_generate_rejects_unknown_fields(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        adapter._handle_conversation_title_generate = lambda _websocket, _message: self.fail("conversation.title.generate handler reached")
        websocket = self._fake_embedded_websocket([
            json.dumps({
                "type": "conversation.title.generate",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "conversation_id": "conversation-1",
                "messages": [{"role": "user", "content": "hello", "message_id": "m"}],
            })
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"conversation:title:generate"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual("protocol.invalid_payload", websocket.sent[0]["code"])

    def test_malformed_privileged_payloads_are_rejected_after_resume(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        adapter._handle_runtime_settings_update = lambda _websocket, _message: self.fail("runtime settings handler reached")
        adapter._handle_agent_settings_update = lambda _websocket, _message: self.fail("Agent settings handler reached")
        adapter._handle_conversation_title_generate = lambda _websocket, _message: self.fail("conversation.title.generate handler reached")
        adapter._schedule_message_create = lambda _websocket, _message: self.fail("message.create handler reached")
        bad_messages = [
            {"type": "message.create", "runtime_id": "runtime-1", "agent_id": "personal", "client_message_id": "client-message-0", "content": "hi"},
            {"type": "conversation.create", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c", "client_message_id": "client-message-new"},
            {"type": "message.create", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c", "content": ""},
            {"type": "message.create", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c", "client_message_id": "client-message-1", "content": "hi", "transfer_ids": ["missing-transfer"]},
            {"type": "conversation.title.generate", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c", "messages": [{"role": "user", "content": ""}]},
            {"type": "runtime.settings.update", "runtime_id": "runtime-1", "settings": []},
            {"type": "runtime.settings.update", "runtime_id": "runtime-1", "settings": {}, "actions": [1]},
            {"type": "agent.settings.update", "runtime_id": "runtime-1", "agent_id": "", "settings": {}},
            {"type": "agent.settings.update", "runtime_id": "runtime-1", "agent_id": "personal", "settings": {}},
            {"type": "agent.settings.update", "runtime_id": "runtime-1", "agent_id": "personal", "actions": []},
            {"type": "message.cancel", "conversation_id": ""},
            {"type": "transfer.request", "protocol_version": "1", "conversation_id": "c", "payload": {"mime_type": "text/plain", "size_bytes": 1}},
            {"type": "transfer.request", "protocol_version": "1", "conversation_id": "c", "payload": {"mime_type": "image/png", "size_bytes": 0}},
            {"type": "transfer.ready", "protocol_version": "1", "conversation_id": "c", "payload": {"transfer_id": "t", "transfer_url": "not-a-url"}},
            {"type": "transfer.ready", "protocol_version": "1", "conversation_id": "c", "payload": {"transfer_id": "t", "transfer_url": "wss://runtime.example/ripdock/transfer/t/app", "max_file_bytes": 10485760, "max_chunk_bytes": 1048576, "expires_at": "soon"}},
            {"type": "transfer.completed", "protocol_version": "1", "conversation_id": "c", "payload": {"transfer_id": "t", "size_bytes": -1}},
            {"type": "transfer.completed", "protocol_version": "1", "conversation_id": "c", "payload": {"transfer_id": "t", "size_bytes": 1, "mime_type": "text/plain"}},
            {"type": "transfer.failed", "protocol_version": "1", "conversation_id": "c", "payload": {"transfer_id": "t", "code": "", "message": "x"}},
            {"type": "runtime.transfer.completed", "protocol_version": "1", "payload": {"transfer_id": "t", "artifact_id": "a", "size_bytes": 1}},
            {"type": "runtime.transfer.completed", "protocol_version": "1", "payload": {"transfer_id": "t", "artifact_id": "a", "size_bytes": 1, "sha256": "not-sha"}},
            {"type": "runtime.transfer.completed", "protocol_version": "1", "payload": {"transfer_id": "t", "artifact_id": "a", "size_bytes": 0, "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
            {"type": "runtime.transfer.failed", "protocol_version": "1", "payload": {"transfer_id": "t", "artifact_id": "a", "code": "", "message": "x"}},
            {"type": "transfer.request", "conversation_id": "c", "payload": {"mime_type": "image/png", "size_bytes": 1}},
        ]
        websocket = self._fake_embedded_websocket(json.dumps(message) for message in bad_messages)
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"*"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(len(bad_messages), len(websocket.sent))
        self.assertEqual({"protocol.invalid_payload"}, {event["code"] for event in websocket.sent})

    def test_wrong_runtime_and_agent_are_rejected_without_traceback(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._agent_by_id = lambda _agent_id: None
        messages = [
            {"type": "message.create", "protocol_version": "1", "runtime_id": "wrong-runtime", "agent_id": "personal", "conversation_id": "c", "client_message_id": "client-message-1", "content": "hi"},
            {"type": "message.create", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "missing-agent", "conversation_id": "c", "client_message_id": "client-message-2", "content": "hi"},
        ]
        websocket = self._fake_embedded_websocket(json.dumps(message) for message in messages)
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"message:create"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(["message.runtime_mismatch", "agent.unavailable"], [event["code"] for event in websocket.sent])

    def test_message_create_rejects_unknown_top_level_fields(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        adapter._schedule_message_create = lambda _websocket, _message: self.fail("message.create handler reached")
        websocket = self._fake_embedded_websocket([
            json.dumps({
                "type": "message.create",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "conversation_id": "c",
                "client_message_id": "client-message-1",
                "content": "hi",
                "unknown": {"runtime_id": "other-runtime"},
            })
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"message:create"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(["protocol.invalid_payload"], [event["code"] for event in websocket.sent])

    def test_app_capabilities_rejects_unknown_fields(self):
        adapter, _message = self._signed_resume_fixture()
        message = adapter._default_client_capabilities()
        message["payload"]["features"]["future_feature"] = True

        self.assertEqual(
            {"type": "error", "protocol_version": "1", "code": "protocol.invalid_payload", "message": "Protocol payload is invalid."},
            adapter._validate_app_capabilities_payload(message),
        )

    def test_app_capabilities_rejects_identity_metadata_fields(self):
        adapter, _message = self._signed_resume_fixture()
        message = adapter._default_client_capabilities()
        message["payload"]["app_metadata"] = {"app_device_id": "device-1"}

        self.assertEqual(
            {"type": "error", "protocol_version": "1", "code": "protocol.invalid_payload", "message": "Protocol payload is invalid."},
            adapter._validate_app_capabilities_payload(message),
        )

    def test_signed_session_resume_rejects_stale_timestamp(self):
        adapter, message = self._signed_resume_fixture(timestamp="2020-01-01T00:00:00Z")

        self.assertEqual((False, "timestamp"), adapter._verify_signed_session_resume(message))

    def test_signed_session_resume_accepts_fractional_timestamp(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())
        self.assertEqual(
            adapter._iso_epoch("2026-06-01T12:34:56Z"),
            adapter._iso_epoch("2026-06-01T12:34:56.123456Z"),
        )

    def test_signed_session_resume_verifies_fractional_timestamp(self):
        adapter = self._signed_resume_fixture()[0]
        fractional_timestamp = adapter._now_iso().replace("Z", ".123456Z")
        adapter, message = self._signed_resume_fixture(timestamp=fractional_timestamp)

        self.assertEqual((True, "verified"), adapter._verify_signed_session_resume(message))

    def test_signed_session_resume_rejects_wrong_session_runtime_device_key_and_revoked_device(self):
        cases = [
            ("wrong-session", {"session_id": "other-session"}, "session"),
            ("wrong-runtime", {"runtime_id": "other-runtime"}, "runtime"),
            ("wrong-device", {"app_device_id": "other-device"}, "device"),
            ("wrong-key", {"key_id": "1" * 64}, "key_id"),
        ]
        for _name, overrides, expected_reason in cases:
            adapter, message = self._signed_resume_fixture(
                session_id=overrides.get("session_id", "session-1"),
                runtime_id=overrides.get("runtime_id", "runtime-1"),
                app_device_id=overrides.get("app_device_id", "device-1"),
                key_id=overrides.get("key_id"),
            )
            self.assertEqual((False, expected_reason), adapter._verify_signed_session_resume(message))

        adapter, message = self._signed_resume_fixture()
        entry = adapter.runtime_identity["trustedDevices"].pop("device-1")
        adapter.runtime_identity["revokedDevices"]["device-1"] = entry
        self.assertEqual((False, "revoked"), adapter._verify_signed_session_resume(message))

    def test_invalid_signed_session_resume_sends_connection_security_error(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "current-session"

        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.close_code = None
                self.close_reason = None

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            async def close(self, code=None, reason=None):
                self.close_code = code
                self.close_reason = reason

        websocket = FakeWebSocket()
        asyncio.run(adapter._handle_embedded_session_resume(websocket, {
            "type": "session.resume",
            "protocol_version": "1",
            "session_id": "stale-session",
            "runtime_id": "runtime-1",
            "app_device_id": "device-1",
            "resume_signature": {},
        }))

        self.assertEqual("error", websocket.sent[0]["type"])
        self.assertEqual("session.invalid", websocket.sent[0]["code"])
        self.assertEqual("deviceNotTrusted", websocket.sent[0]["connection_security_error"])
        self.assertNotIn("session_id", websocket.sent[0])
        self.assertNotIn("runtime_id", websocket.sent[0])
        self.assertEqual(1000, websocket.close_code)
        self.assertEqual("Session is invalid.", websocket.close_reason)

    def test_signed_session_resume_route_failure_reports_connection_security_error(self):
        adapter, message = self._signed_resume_fixture(route="/base/ripdock/app")

        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.close_code = None
                self.close_reason = None

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            async def close(self, code=None, reason=None):
                self.close_code = code
                self.close_reason = reason

        websocket = FakeWebSocket()
        adapter.app_session_route_by_websocket = {websocket: "/ripdock/app"}
        asyncio.run(adapter._handle_embedded_session_resume(websocket, message))

        self.assertEqual("error", websocket.sent[0]["type"])
        self.assertEqual("session.invalid", websocket.sent[0]["code"])
        self.assertEqual("routeMismatch", websocket.sent[0]["connection_security_error"])
        self.assertNotIn("session_id", websocket.sent[0])
        self.assertEqual(1000, websocket.close_code)

    def test_trusted_device_without_public_key_must_pair_again(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter.runtime_identity = {
            "runtimeId": "runtime-1",
            "publicKeyFingerprint": "SHA256:runtime",
            "protocolVersion": "1",
            "trustedDevices": {
                "device-1": {
                    "deviceIdentity": {
                        "deviceId": "device-1",
                        "publicKeyFingerprint": "SHA256:device",
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
        device_public_key = self._sample_p256_jwk("1" * 64)
        payload = {
            "deviceIdentity": {
                "deviceId": "device-1",
                "publicKey": device_public_key,
                "publicKeyFingerprint": device_public_key["key_id"],
            }
        }

        status = adapter._handle_pairing_status(payload)
        request = adapter._handle_pairing_request(payload)

        self.assertEqual("identityMismatch", status["trustState"])
        self.assertNotIn("session_id", status)
        self.assertEqual("pendingApproval", request["trustState"])
        self.assertNotIn("session_id", request)
        self.assertNotIn("device-1", adapter.runtime_identity["trustedDevices"])
        self.assertIn("device-1", adapter.runtime_identity["pendingDevices"])

    def test_runtime_admin_approve_is_idempotent_for_trusted_device(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter.runtime_identity = {
            "runtimeId": "runtime-1",
            "displayName": "Runtime",
            "publicKey": "runtime-public-key",
            "publicKeyFingerprint": "SHA256:runtime",
            "protocolVersion": "1",
            "createdAt": "2026-01-01T00:00:00Z",
            "trustedDevices": {
                "device-1": {
                    "deviceIdentity": {
                        "deviceId": "device-1",
                        "publicKey": self._sample_p256_jwk("1" * 64),
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
        adapter._admin_state = lambda: {
            "pendingDevices": [],
            "trustedDevices": [{"deviceId": "device-1"}],
        }

        result = adapter._approve_pending_device("device-1")

        self.assertTrue(result["ok"])
        self.assertTrue(result["noop"])
        self.assertEqual("device-1", result["deviceId"])
        self.assertEqual("trusted", result["trustState"])
        self.assertEqual("session-1", result["session_id"])
        self.assertEqual([], result["state"]["pendingDevices"])
        self.assertEqual(["device-1"], [device["deviceId"] for device in result["state"]["trustedDevices"]])

    def test_pairing_request_sets_expiry_reopens_rejected_and_revoked_devices(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter._save_runtime_identity = lambda: True
        revoked_public_key = self._sample_p256_jwk("2" * 64)
        rejected_public_key = self._sample_p256_jwk("3" * 64)
        new_public_key = self._sample_p256_jwk("4" * 64)
        adapter.runtime_identity = {
            "runtimeId": "runtime-1",
            "publicKeyFingerprint": "SHA256:runtime",
            "protocolVersion": "1",
            "trustedDevices": {},
            "pendingDevices": {},
            "revokedDevices": {
                "revoked-device": {
                    "deviceIdentity": {
                        "deviceId": "revoked-device",
                        "publicKey": revoked_public_key,
                        "publicKeyFingerprint": revoked_public_key["key_id"],
                    },
                    "trustState": "revoked",
                }
            },
            "rejectedDevices": {
                "rejected-device": {
                    "deviceIdentity": {
                        "deviceId": "rejected-device",
                        "publicKey": rejected_public_key,
                        "publicKeyFingerprint": rejected_public_key["key_id"],
                    },
                    "rejectedAt": adapter._now_iso(),
                    "trustState": "rejected",
                }
            },
        }

        pending = adapter._handle_pairing_request({
            "deviceIdentity": {
                "deviceId": "new-device",
                "publicKey": new_public_key,
                "publicKeyFingerprint": new_public_key["key_id"],
            }
        })
        rejected = adapter._handle_pairing_request({
            "deviceIdentity": {
                "deviceId": "rejected-device",
                "publicKey": rejected_public_key,
                "publicKeyFingerprint": rejected_public_key["key_id"],
            }
        })
        revoked = adapter._handle_pairing_request({
            "deviceIdentity": {
                "deviceId": "revoked-device",
                "publicKey": revoked_public_key,
                "publicKeyFingerprint": revoked_public_key["key_id"],
            }
        })

        self.assertEqual("pendingApproval", pending["trustState"])
        self.assertRegex(adapter.runtime_identity["pendingDevices"]["new-device"]["expiresAt"], r"^20\d\d-\d\d-\d\dT")
        self.assertEqual("pendingApproval", rejected["trustState"])
        self.assertEqual("pendingApproval", revoked["trustState"])
        self.assertIn("rejected-device", adapter.runtime_identity["pendingDevices"])
        self.assertNotIn("rejected-device", adapter.runtime_identity["rejectedDevices"])
        self.assertIn("revoked-device", adapter.runtime_identity["pendingDevices"])
        self.assertNotIn("revoked-device", adapter.runtime_identity["revokedDevices"])

    def test_expired_pending_pairing_can_be_requested_again(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter._save_runtime_identity = lambda: True
        old_public_key = self._sample_p256_jwk("5" * 64)
        new_public_key = self._sample_p256_jwk("6" * 64)
        adapter.runtime_identity = {
            "runtimeId": "runtime-1",
            "publicKeyFingerprint": "SHA256:runtime",
            "protocolVersion": "1",
            "trustedDevices": {},
            "pendingDevices": {
                "device-1": {
                    "deviceIdentity": {
                        "deviceId": "device-1",
                        "publicKey": old_public_key,
                        "publicKeyFingerprint": old_public_key["key_id"],
                    },
                    "expiresAt": "2020-01-01T00:00:00Z",
                    "trustState": "pendingApproval",
                }
            },
            "revokedDevices": {},
            "rejectedDevices": {},
        }

        result = adapter._handle_pairing_request({
            "deviceIdentity": {
                "deviceId": "device-1",
                "publicKey": new_public_key,
                "publicKeyFingerprint": new_public_key["key_id"],
            }
        })

        self.assertEqual("pendingApproval", result["trustState"])
        self.assertEqual(new_public_key, adapter.runtime_identity["pendingDevices"]["device-1"]["deviceIdentity"]["publicKey"])
        self.assertNotEqual("2020-01-01T00:00:00Z", adapter.runtime_identity["pendingDevices"]["device-1"]["expiresAt"])

    def test_pairing_code_expires_and_rate_limits_bad_codes(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())
        adapter.pairing_code = "123456"
        adapter.pairing_bound = False
        adapter.pairing_code_created_at = 0
        adapter._rate_limit_events = {}

        old_env = os.environ.copy()
        try:
            os.environ["RIPDOCK_PAIRING_TTL_SECONDS"] = "30"
            os.environ["RIPDOCK_PAIRING_CODE_RATE_LIMIT"] = "2"
            os.environ["RIPDOCK_RATE_LIMIT_WINDOW_SECONDS"] = "60"
            self.assertFalse(adapter._pairing_code_matches("123456"))
            self.assertFalse(adapter._record_rate_limit_event("pairing_code", "000000"))
            self.assertFalse(adapter._record_rate_limit_event("pairing_code", "000000"))
            self.assertTrue(adapter._record_rate_limit_event("pairing_code", "000000"))
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_pairing_status_uses_separate_rate_limit_for_polling(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(config=types.SimpleNamespace())
        adapter.runtime_identity = {
            "runtimeId": "runtime-1",
            "displayName": "Runtime",
            "publicKey": {"kty": "EC"},
            "publicKeyFingerprint": "SHA256:runtime",
            "protocolVersion": "1",
            "createdAt": "2026-01-01T00:00:00Z",
            "pendingDevices": {},
            "trustedDevices": {},
            "revokedDevices": {},
            "rejectedDevices": {},
        }
        adapter._save_runtime_identity = lambda: True
        payload = self._sample_device_identity("device-1", seed="polling")

        old_env = os.environ.copy()
        try:
            os.environ["RIPDOCK_PAIRING_REQUEST_RATE_LIMIT"] = "1"
            os.environ["RIPDOCK_PAIRING_STATUS_RATE_LIMIT"] = "40"
            os.environ["RIPDOCK_RATE_LIMIT_WINDOW_SECONDS"] = "60"
            adapter._rate_limit_events = {}

            first_request = adapter._handle_pairing_request(payload)
            second_request = adapter._handle_pairing_request(payload)
            status_results = [adapter._handle_pairing_status(payload) for _ in range(30)]
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertEqual("pendingApproval", first_request["trustState"])
        self.assertEqual("notFound", second_request["trustState"])
        self.assertEqual(["pendingApproval"], sorted({result["trustState"] for result in status_results}))

    def test_resume_failure_rate_limit_records_repeated_bad_resumes(self):
        adapter, message = self._signed_resume_fixture()
        old_env = os.environ.copy()
        try:
            os.environ["RIPDOCK_RESUME_FAILURE_RATE_LIMIT"] = "1"
            os.environ["RIPDOCK_RATE_LIMIT_WINDOW_SECONDS"] = "60"
            self.assertFalse(adapter._record_rate_limit_event("resume_failure", message["app_device_id"]))
            self.assertTrue(adapter._record_rate_limit_event("resume_failure", message["app_device_id"]))
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_pairing_status_returns_protocol_result_shape(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {},
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                request_payload = self._sample_device_identity("device-1", seed="pk", created_at="2026-01-01T00:30:00Z")
                request = api.pairing_request(request_payload)
                status = api.pairing_status({"deviceId": "device-1", "deviceFingerprint": request_payload["publicKeyFingerprint"]})
                state = asyncio.run(api.get_state())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        for response in (request, status):
            self.assertEqual("pendingApproval", response["trustState"])
            self.assertEqual("runtime-1", response["runtimeId"])
            self.assertNotIn("ok", response)
            self.assertNotIn("runtimeFingerprint", response)
            self.assertNotIn("publicKeyFingerprint", response)
            self.assertNotIn("deviceFingerprint", response)
            self.assertNotIn("protocolVersion", response)
            self.assertNotIn("runtimeMetadata", response)
            self.assertNotIn("state", response)
            self.assertNotIn("pendingDevices", response)
            self.assertNotIn("runtimeIdentity", response)
        self.assertEqual(["device-1"], [device["deviceId"] for device in state["pendingDevices"]])

    def test_pairing_request_after_reject_recreates_pending_for_same_device(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            old_now = api._now_iso
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {},
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                api._now_iso = lambda: "2026-01-01T01:00:00Z"

                payload = self._sample_device_identity(
                    "9C645742-A606-4B0A-B115-034DCD328C47",
                    seed="device-public-key",
                    device_name="Dave's iPhone",
                )
                first = api.pairing_request(payload)
                first_state = asyncio.run(api.get_state())
                reject = asyncio.run(api.post_device_reject(payload["deviceId"]))
                rejected_state = asyncio.run(api.get_state())
                rejected_status = api.pairing_status({"deviceId": payload["deviceId"], "publicKeyFingerprint": payload["publicKeyFingerprint"]})
                after_status_state = asyncio.run(api.get_state())
                persisted_rejected_identity = json.loads(identity_file.read_text())
                second = api.pairing_request(payload)
                second_state = asyncio.run(api.get_state())
                dashboard_store = json.loads((Path(directory) / "state.json").read_text())
            finally:
                api._now_iso = old_now
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("pendingApproval", first["trustState"])
        self.assertEqual([payload["deviceId"]], [device["deviceId"] for device in first_state["pendingDevices"]])
        self.assertEqual("rejected", reject["trustState"])
        self.assertEqual([], rejected_state["pendingDevices"])
        self.assertEqual("rejected", rejected_status["trustState"])
        self.assertEqual("Pairing request rejected.", rejected_status["message"])
        self.assertEqual([], after_status_state["pendingDevices"])
        self.assertEqual("rejected", persisted_rejected_identity["rejectedDevices"][payload["deviceId"]]["trustState"])
        self.assertEqual("pendingApproval", second["trustState"])
        self.assertEqual([payload["deviceId"]], [device["deviceId"] for device in second_state["pendingDevices"]])
        self.assertEqual({}, dashboard_store.get("deletedPendingDevices", {}))

    def test_pairing_status_is_read_only_and_reports_terminal_states(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            old_now = api._now_iso
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {
                    "pending-device": {
                        "deviceIdentity": {"deviceId": "pending-device", "publicKeyFingerprint": "SHA256:p"},
                        "expiresAt": None,
                        "trustState": "pendingApproval",
                    },
                    "expired-device": {
                        "deviceIdentity": {"deviceId": "expired-device", "publicKeyFingerprint": "SHA256:e"},
                        "expiresAt": "2026-01-01T00:59:00Z",
                        "trustState": "pendingApproval",
                    }
                },
                "trustedDevices": {
                    "trusted-device": {
                        "deviceIdentity": {"deviceId": "trusted-device", "publicKey": "pk-t", "publicKeyFingerprint": "SHA256:t"},
                        "trustState": "trusted",
                    }
                },
                "revokedDevices": {
                    "revoked-device": {
                        "deviceIdentity": {"deviceId": "revoked-device", "publicKeyFingerprint": "SHA256:v"},
                        "trustState": "revoked",
                    }
                },
                "rejectedDevices": {
                    "fresh-reject": {
                        "deviceIdentity": {"deviceId": "fresh-reject", "publicKeyFingerprint": "SHA256:r"},
                        "rejectedAt": "2026-01-01T00:55:00Z",
                        "reason": "dashboardRejected",
                        "trustState": "rejected",
                    },
                    "old-reject": {
                        "deviceIdentity": {"deviceId": "old-reject", "publicKeyFingerprint": "SHA256:o"},
                        "rejectedAt": "2026-01-01T00:40:00Z",
                        "reason": "dashboardRejected",
                        "trustState": "rejected",
                    },
                },
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                api._now_iso = lambda: "2026-01-01T01:00:00Z"

                fresh = api.pairing_status({"deviceId": "fresh-reject", "publicKeyFingerprint": "SHA256:r"})
                old = api.pairing_status({"deviceId": "old-reject", "publicKeyFingerprint": "SHA256:o"})
                missing = api.pairing_status({"deviceId": "missing-device"})
                pending = api.pairing_status({"deviceId": "pending-device", "publicKeyFingerprint": "SHA256:p"})
                expired = api.pairing_status({"deviceId": "expired-device", "publicKeyFingerprint": "SHA256:e"})
                trusted = api.pairing_status({"deviceId": "trusted-device", "publicKeyFingerprint": "SHA256:t"})
                revoked = api.pairing_status({"deviceId": "revoked-device", "publicKeyFingerprint": "SHA256:v"})
                state_after_status = asyncio.run(api.get_state())
            finally:
                api._now_iso = old_now
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("rejected", fresh["trustState"])
        self.assertEqual("notFound", old["trustState"])
        self.assertEqual("notFound", missing["trustState"])
        self.assertEqual("pendingApproval", pending["trustState"])
        self.assertEqual("expired", expired["trustState"])
        self.assertEqual("trusted", trusted["trustState"])
        self.assertEqual("revoked", revoked["trustState"])
        self.assertIn("session_id", trusted)
        self.assertNotIn("session_id", fresh)
        self.assertNotIn("session_id", old)
        self.assertNotIn("session_id", missing)
        self.assertNotIn("session_id", pending)
        self.assertNotIn("session_id", expired)
        self.assertNotIn("session_id", revoked)
        for response in (fresh, old, missing, pending, expired, trusted, revoked):
            self.assertEqual("runtime-1", response["runtimeId"])
            self.assertIn(response["trustState"], {"pendingApproval", "trusted", "rejected", "expired", "revoked", "notFound"})
            self.assertIn("message", response)
            self.assertNotIn("Failed to open a WebSocket connection", json.dumps(response))
        self.assertEqual(["pending-device"], [device["deviceId"] for device in state_after_status["pendingDevices"]])

    def test_pairing_status_returns_runtime_scoped_session_id_for_each_trusted_runtime(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            base = Path(directory)

            def trusted_status(name: str) -> dict[str, object]:
                runtime_dir = base / name
                runtime_dir.mkdir()
                identity_file = runtime_dir / "runtime-identity.json"
                session_file = runtime_dir / "session.json"
                identity_file.write_text(json.dumps({
                    "runtimeId": f"{name}-runtime",
                    "displayName": name.title(),
                    "publicKey": f"{name}-runtime-public-key",
                    "publicKeyFingerprint": f"SHA256:{name}",
                    "protocolVersion": "1",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "pendingDevices": {},
                    "trustedDevices": {
                        "device-1": {
                            "deviceIdentity": {"deviceId": "device-1", "publicKey": "pk", "publicKeyFingerprint": "SHA256:device"},
                            "trustState": "trusted",
                        }
                    },
                    "revokedDevices": {},
                }))
                session_file.write_text(json.dumps({"session_id": f"{name}-session"}))
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(runtime_dir / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_SESSION_FILE"] = str(session_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(runtime_dir / "missing")
                return api.pairing_status({"deviceId": "device-1", "publicKeyFingerprint": "SHA256:device"})

            try:
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                personal = trusted_status("personal")
                dev = trusted_status("dev")
                production = trusted_status("production")
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("trusted", personal["trustState"])
        self.assertEqual("trusted", dev["trustState"])
        self.assertEqual("trusted", production["trustState"])
        self.assertEqual("personal-session", personal["session_id"])
        self.assertEqual("dev-session", dev["session_id"])
        self.assertEqual("production-session", production["session_id"])
        self.assertEqual(3, len({personal["session_id"], dev["session_id"], production["session_id"]}))

    def test_pairing_request_refreshes_existing_pending_timestamp_without_duplicates(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            old_now = api._now_iso
            times = iter([
                "2026-01-01T01:00:00Z",
                "2026-01-01T01:01:00Z",
                "2026-01-01T01:01:00Z",
                "2026-01-01T01:01:00Z",
            ])
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {},
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                api._now_iso = lambda: next(times)

                payload = self._sample_device_identity("device-1", seed="pk", created_at="2026-01-01T00:30:00Z")
                api.pairing_request(payload)
                refreshed = api.pairing_request(payload)
                state = asyncio.run(api.get_state())
            finally:
                api._now_iso = old_now
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("pendingApproval", refreshed["trustState"])
        self.assertEqual(["device-1"], [device["deviceId"] for device in state["pendingDevices"]])
        self.assertEqual(["2026-01-01T01:01:00Z"], [device["claimedTime"] for device in state["pendingDevices"]])

    def test_pairing_request_does_not_return_pending_when_persistence_fails(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            old_save = api.save_runtime_identity
            old_logger_error = api.logger.error
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(Path(directory) / "runtime-identity.json")
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                api.logger.error = lambda *_args, **_kwargs: None

                def fail_save(_identity):
                    raise OSError("disk full")

                api.save_runtime_identity = fail_save
                with self.assertRaises(api.HTTPException) as error:
                    api.pairing_request(self._sample_device_identity("device-1", seed="pk"))
            finally:
                api.save_runtime_identity = old_save
                api.logger.error = old_logger_error
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(500, error.exception.status_code)
        self.assertIn("not persisted", error.exception.detail)

    def test_approve_and_reject_accept_device_ids_from_dashboard_state(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {},
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)

                approve_id = "3FAC838F-E652-40AD-A4CC-E00D60B51E7B"
                reject_id = "delete-Device-42"
                api.pairing_request(self._sample_device_identity(approve_id, seed="pk-a"))
                api.pairing_request(self._sample_device_identity(reject_id, seed="pk-r"))
                state = asyncio.run(api.get_state())
                rendered_ids = [device["deviceId"] for device in state["pendingDevices"]]
                approve_result = asyncio.run(api.post_device_approve(rendered_ids[0]))
                reject_result = asyncio.run(api.post_device_reject(rendered_ids[1]))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual([approve_id, reject_id], rendered_ids)
        self.assertEqual("trusted", approve_result["trustState"])
        self.assertEqual("rejected", reject_result["trustState"])
        self.assertEqual([], [device["deviceId"] for device in reject_result["state"]["pendingDevices"]])
        self.assertEqual([approve_id], [device["deviceId"] for device in reject_result["state"]["trustedDevices"]])

    def test_approve_proxy_400_does_not_return_false_success(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            old_proxy = api._proxy_json
            identity_file = Path(directory) / "runtime-identity.json"
            device_id = "9C645742-A606-4B0A-B115-034DCD328C47"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {
                    device_id: {
                        "deviceIdentity": {
                            "deviceId": device_id,
                            "deviceName": "Dave's iPhone",
                            "publicKey": "pk-device",
                            "publicKeyFingerprint": "672d35aa",
                        },
                        "requestedAt": "2026-01-01T01:00:00Z",
                        "claimedAt": "2026-01-01T01:00:00Z",
                        "trustState": "pendingApproval",
                    }
                },
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            calls = []
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ["RIPDOCK_RUNTIME_ADMIN_URL"] = "https://runtime.example.com"

                def bad_request_proxy(method, path, body=None, route_method=None):
                    calls.append((method, path, body, route_method))
                    return 400, {"ok": False, "message": "HTTP Error 400: Bad Request"}

                api._proxy_json = bad_request_proxy
                with self.assertRaises(api.HTTPException) as error:
                    asyncio.run(api.post_device_approve(device_id))
                state = asyncio.run(api.get_state())
                persisted = json.loads(identity_file.read_text())
            finally:
                api._proxy_json = old_proxy
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(400, error.exception.status_code)
        self.assertIn("HTTP Error 400", error.exception.detail)
        self.assertEqual([device_id], [device["deviceId"] for device in state["pendingDevices"]])
        self.assertEqual([], [device["deviceId"] for device in state["trustedDevices"]])
        self.assertEqual([device_id], list(persisted["pendingDevices"].keys()))
        self.assertEqual({}, persisted["trustedDevices"])
        self.assertEqual(("GET", f"/ripdock/admin/devices/{device_id}/approve", {"deviceId": device_id, "action": "approve"}, "POST"), calls[0])

    def test_approve_proxy_success_must_include_mutated_admin_state(self):
        api = load_dashboard_api()
        old_proxy = api._proxy_json
        device_id = "9C645742-A606-4B0A-B115-034DCD328C47"
        calls = []

        def stale_success_proxy(method, path, body=None, route_method=None):
            calls.append((method, path, body, route_method))
            if method == "GET" and route_method == "POST" and path.endswith("/approve"):
                return 200, {
                    "ok": True,
                    "deviceId": device_id,
                    "trustState": "trusted",
                    "state": {
                        "pendingDevices": [{"deviceId": device_id, "deviceFingerprint": "672d35aa"}],
                        "trustedDevices": [],
                    },
                }
            return 200, {
                "pendingDevices": [{"deviceId": device_id, "deviceFingerprint": "672d35aa"}],
                "trustedDevices": [],
            }

        try:
            api._proxy_json = stale_success_proxy
            with self.assertRaises(api.HTTPException) as error:
                asyncio.run(api.post_device_approve(device_id))
        finally:
            api._proxy_json = old_proxy

        self.assertEqual(502, error.exception.status_code)
        self.assertIn("reported success", error.exception.detail)
        self.assertEqual(("GET", f"/ripdock/admin/devices/{device_id}/approve", {"deviceId": device_id, "action": "approve"}, "POST"), calls[0])

    def test_approve_fails_clearly_when_pending_identity_has_no_key_material(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {
                    "device-1": {
                        "deviceIdentity": {"deviceId": "device-1", "deviceName": "No Key"},
                        "trustState": "pendingApproval",
                    }
                },
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                with self.assertRaises(api.HTTPException) as error:
                    asyncio.run(api.post_device_approve("device-1"))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(400, error.exception.status_code)
        self.assertIn("publicKey", error.exception.detail)

    def test_reject_stale_pending_device_is_idempotent_and_does_not_revoke_trusted(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {},
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)

                api.pairing_request(self._sample_device_identity("trusted-device", seed="pk-t"))
                asyncio.run(api.post_device_approve("trusted-device"))
                stale_result = asyncio.run(api.post_device_reject("stale-device"))
                trusted_reject_result = asyncio.run(api.post_device_reject("trusted-device"))
                state = asyncio.run(api.get_state())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertTrue(stale_result["ok"])
        self.assertTrue(stale_result["noop"])
        self.assertEqual("notFound", stale_result["trustState"])
        self.assertEqual("Pending Device was already gone.", stale_result["message"])
        self.assertTrue(trusted_reject_result["noop"])
        self.assertEqual([], state["pendingDevices"])
        self.assertEqual(["trusted-device"], [device["deviceId"] for device in state["trustedDevices"]])

    def test_revoke_removes_trusted_and_allows_new_pairing_request(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {},
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)

                payload = self._sample_device_identity("device-1", seed="pk")
                api.pairing_request(payload)
                asyncio.run(api.post_device_approve("device-1"))
                revoke_result = asyncio.run(api.post_device_revoke("device-1"))
                after_revoke = asyncio.run(api.get_state())
                request_again = api.pairing_request(payload)
                after_request = asyncio.run(api.get_state())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("revoked", revoke_result["trustState"])
        self.assertEqual([], [device["deviceId"] for device in after_revoke["trustedDevices"]])
        self.assertEqual("pendingApproval", request_again["trustState"])
        self.assertEqual(["device-1"], [device["deviceId"] for device in after_request["pendingDevices"]])
        self.assertEqual([], [device["deviceId"] for device in after_request["trustedDevices"]])

    def test_revoke_trusted_device_matches_nested_device_id_and_persists(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            device_id = "9C645742-A606-4B0A-B115-034DCD328C47"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {},
                "trustedDevices": {
                    "stored-under-fingerprint": {
                        "deviceIdentity": {
                            "deviceId": device_id,
                            "deviceName": "Dave's iPhone",
                            "publicKey": "pk-device",
                            "publicKeyFingerprint": "672d35aa",
                        },
                        "approvedAt": "2026-01-01T01:00:00Z",
                        "lastSeen": "2026-01-01T01:00:00Z",
                        "trustState": "trusted",
                    }
                },
                "revokedDevices": {},
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)

                before = asyncio.run(api.get_state())
                result = asyncio.run(api.post_device_revoke(device_id))
                after = asyncio.run(api.get_state())
                persisted = json.loads(identity_file.read_text())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual([device_id], [device["deviceId"] for device in before["trustedDevices"]])
        self.assertTrue(result["ok"])
        self.assertEqual(device_id, result["deviceId"])
        self.assertEqual("revoked", result["trustState"])
        self.assertEqual([], result["state"]["trustedDevices"])
        self.assertEqual([], after["trustedDevices"])
        self.assertEqual({}, persisted["trustedDevices"])
        self.assertEqual([device_id], [entry["deviceIdentity"]["deviceId"] for entry in persisted["revokedDevices"].values()])

    def test_reject_removes_pending_row_when_store_key_differs_from_rendered_device_id(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {
                    "request-key": {
                        "deviceIdentity": {
                            "deviceId": "rendered-device-id",
                            "deviceName": "Stale iPhone",
                            "publicKey": "pk-stale",
                            "publicKeyFingerprint": "SHA256:stale",
                        },
                        "requestedAt": "2026-01-01T01:00:00Z",
                        "claimedAt": "2026-01-01T01:00:00Z",
                        "expiresAt": None,
                        "trustState": "pendingApproval",
                    }
                },
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)

                initial_state = asyncio.run(api.get_state())
                reject_result = asyncio.run(api.post_device_reject("rendered-device-id"))
                reloaded_state = asyncio.run(api.get_state())
                persisted_identity = json.loads(identity_file.read_text())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(["rendered-device-id"], [device["deviceId"] for device in initial_state["pendingDevices"]])
        self.assertEqual("rejected", reject_result["trustState"])
        self.assertEqual([], reject_result["state"]["pendingDevices"])
        self.assertEqual([], reloaded_state["pendingDevices"])
        self.assertEqual({}, persisted_identity["pendingDevices"])

    def test_runtime_admin_reject_removes_top_level_pending_device_shape(self):
        api = load_dashboard_api()
        device_id = "9C645742-A606-4B0A-B115-034DCD328C47"
        fingerprint = "672d35aa2b3e803e3a89bc03c09bdab688647a8e8a2bc62961334f3996b4cbec"
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {
                    "some-old-key": {
                        "deviceId": device_id,
                        "deviceName": "iPhone",
                        "deviceFingerprint": fingerprint,
                        "claimedTime": "2026-01-01T01:00:00Z",
                        "expiresAt": None,
                        "trustState": "pendingApproval",
                    }
                },
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)

                initial_state = asyncio.run(api.get_state())
                reject_result = asyncio.run(api.post_device_reject(device_id))
                reloaded_state = asyncio.run(api.get_state())
                persisted_identity = json.loads(identity_file.read_text())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual([device_id], [device["deviceId"] for device in initial_state["pendingDevices"]])
        self.assertEqual([fingerprint], [device["deviceFingerprint"] for device in initial_state["pendingDevices"]])
        self.assertEqual("rejected", reject_result["trustState"])
        self.assertEqual([], reject_result["state"]["pendingDevices"])
        self.assertEqual([], reloaded_state["pendingDevices"])
        self.assertEqual({}, persisted_identity["pendingDevices"])

    def test_reject_noop_prunes_proxied_stale_pending_row_from_dashboard_state(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            old_proxy = api._proxy_json
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(Path(directory) / "runtime-identity.json")
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")

                def fake_proxy(method, path, body=None, route_method=None):
                    if method == "GET" and path == "/ripdock/admin/state":
                        return 200, {
                            "runtimeIdentity": {
                                "runtimeId": "runtime-1",
                                "displayName": "Runtime",
                                "publicKey": "pk-runtime",
                                "publicKeyFingerprint": "SHA256:runtime",
                                "protocolVersion": "1",
                                "createdAt": "2026-01-01T00:00:00Z",
                            },
                            "runtimeMetadata": {
                                "displayName": "Runtime",
                                "icon": "",
                                "accentColor": "",
                                "backgroundColor": "#ffffff",
                            },
                            "pendingDevices": [
                                {
                                    "deviceName": "Stale iPhone",
                                    "deviceId": "stale-device",
                                    "deviceFingerprint": "SHA256:stale",
                                    "claimedTime": "2026-01-01T01:00:00Z",
                                    "expiresAt": None,
                                }
                            ],
                            "trustedDevices": [],
                        }
                    if method == "GET" and route_method == "POST" and path.endswith("/reject"):
                        return 400, {"ok": False, "message": "Pending Device not found."}
                    return 503, {"ok": False, "implemented": False}

                api._proxy_json = fake_proxy
                initial_state = asyncio.run(api.get_state())
                reject_result = asyncio.run(api.post_device_action("reject", api.DeviceActionBody(deviceId="stale-device", action="reject")))
                reloaded_state = asyncio.run(api.get_state())
            finally:
                api._proxy_json = old_proxy
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(["stale-device"], [device["deviceId"] for device in initial_state["pendingDevices"]])
        self.assertTrue(reject_result["noop"])
        self.assertEqual([], reject_result["state"]["pendingDevices"])
        self.assertEqual([], reloaded_state["pendingDevices"])

    def test_stale_pairing_payload_state_does_not_render_as_admin_pending_devices(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            state_file = Path(directory) / "state.json"
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {},
                "trustedDevices": {},
                "revokedDevices": {},
            }))
            state_file.write_text(json.dumps({
                "pairingPayloads": {
                    "pairing-1": {
                        "pairingId": "pairing-1",
                        "claimedDeviceId": "not-actionable-yet",
                        "status": "pending",
                    }
                }
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(state_file)
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)

                state = asyncio.run(api.get_state())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual([], state["pendingDevices"])
        self.assertNotIn("pairingPayloads", state)

    def test_device_action_errors_include_actionable_details(self):
        api = load_dashboard_api()
        with self.assertRaises(api.HTTPException) as missing:
            asyncio.run(api.post_device_action("reject", api.DeviceActionBody(deviceId="", action="reject")))
        with self.assertRaises(api.HTTPException) as unsupported:
            asyncio.run(api.post_device_action("erase", api.DeviceActionBody(deviceId="device-1", action="erase")))

        self.assertIn("deviceId is required", missing.exception.detail)
        self.assertIn("Unsupported Device action", unsupported.exception.detail)

    def test_pairing_status_reads_saved_runtime_metadata_without_changing_identity(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity = {
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "runtime-public-key",
                "publicKeyFingerprint": "SHA256:runtime-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
                "pendingDevices": {},
                "trustedDevices": {},
                "revokedDevices": {},
            }
            identity_file.write_text(json.dumps(identity))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)

                pending_payload = self._sample_device_identity("pending-device", seed="pk-p")
                trusted_payload = self._sample_device_identity("trusted-device", seed="pk-t")
                rejected_payload = self._sample_device_identity("rejected-device", seed="pk-r")
                revoked_payload = self._sample_device_identity("revoked-device", seed="pk-v")
                api.pairing_request(pending_payload)
                api.pairing_request(trusted_payload)
                asyncio.run(api.post_device_approve("trusted-device"))
                api.pairing_request(rejected_payload)
                asyncio.run(api.post_device_reject("rejected-device"))
                api.pairing_request(revoked_payload)
                asyncio.run(api.post_device_approve("revoked-device"))
                asyncio.run(api.post_device_revoke("revoked-device"))

                default_status = api.pairing_status({"deviceId": "missing-device"})
                asyncio.run(api.post_metadata(api.MetadataBody(displayName="Saved Runtime", icon="🤖", accentColor="#2563eb", backgroundColor="#dbeafe")))
                pending = api.pairing_status({"deviceId": "pending-device", "publicKeyFingerprint": pending_payload["publicKeyFingerprint"]})
                trusted = api.pairing_status({"deviceId": "trusted-device", "publicKeyFingerprint": trusted_payload["publicKeyFingerprint"]})
                rejected = api.pairing_status({"deviceId": "rejected-device", "publicKeyFingerprint": rejected_payload["publicKeyFingerprint"]})
                revoked = api.pairing_status({"deviceId": "revoked-device", "publicKeyFingerprint": revoked_payload["publicKeyFingerprint"]})
                missing = api.pairing_status({"deviceId": "missing-device"})
                persisted_identity = json.loads(identity_file.read_text())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertNotIn("runtimeMetadata", default_status)
        expected_metadata = {"displayName": "Saved Runtime", "icon": "🤖", "accentColor": "#2563eb", "backgroundColor": "#dbeafe"}
        self.assertEqual("pendingApproval", pending["trustState"])
        self.assertEqual("trusted", trusted["trustState"])
        self.assertEqual("rejected", rejected["trustState"])
        self.assertEqual("revoked", revoked["trustState"])
        self.assertEqual("notFound", missing["trustState"])
        self.assertEqual(expected_metadata, trusted["runtimeMetadata"])
        self.assertIn("runtimeAgents", trusted)
        self.assertIsInstance(trusted["runtimeAgents"], list)
        for response in (pending, rejected, revoked, missing):
            self.assertNotIn("runtimeMetadata", response)
            self.assertNotIn("runtimeAgents", response)
        self.assertEqual(identity["runtimeId"], persisted_identity["runtimeId"])
        self.assertIsInstance(persisted_identity["publicKey"], dict)
        self.assertRegex(persisted_identity["publicKeyFingerprint"], r"^[0-9a-f]{64}$")
