from support import *


class DashboardApiTests(PluginTestBase):
    def test_runtime_app_serves_admin_state_with_fastapi(self):
        from fastapi.testclient import TestClient

        runtime_app = load_runtime_app()
        adapter = types.SimpleNamespace(_admin_state=lambda: {"ok": True})
        client = TestClient(runtime_app.create_runtime_app(adapter))

        response = client.get("/ripdock/admin/state")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"ok": True}, response.json())

    def test_device_actions_use_full_ids_and_proxy_expected_admin_shape(self):
        api = load_dashboard_api()
        calls = []
        old_proxy = api._proxy_json
        device_id = "3FAC838F-E652-40AD-A4CC-E00D60B51E7B"

        def fake_proxy(method, path, body=None, route_method=None):
            calls.append((method, path, body, route_method))
            if method == "GET" and path == "/ripdock/admin/state":
                return 200, {"pendingDevices": [], "trustedDevices": []}
            return 200, {
                "ok": True,
                "deviceId": body["deviceId"],
                "trustState": path.rsplit("/", 1)[-1],
                "state": {
                    "pendingDevices": [],
                    "trustedDevices": [{"deviceId": body["deviceId"]}] if path.endswith("/approve") else [],
                },
            }

        try:
            api._proxy_json = fake_proxy
            approve = asyncio.run(api.post_device_approve(device_id))
            reject = asyncio.run(api.post_device_reject(device_id))
            revoke = asyncio.run(api.post_device_revoke(device_id))
        finally:
            api._proxy_json = old_proxy

        self.assertEqual("3FAC838F-E652-40AD-A4CC-E00D60B51E7B", approve["deviceId"])
        self.assertEqual("3FAC838F-E652-40AD-A4CC-E00D60B51E7B", reject["deviceId"])
        self.assertEqual("3FAC838F-E652-40AD-A4CC-E00D60B51E7B", revoke["deviceId"])
        device_calls = [call for call in calls if call[1].startswith("/ripdock/admin/devices/")]
        self.assertEqual(
            [
                ("GET", "/ripdock/admin/devices/3FAC838F-E652-40AD-A4CC-E00D60B51E7B/approve", {"deviceId": device_id, "action": "approve"}, "POST"),
                ("GET", "/ripdock/admin/devices/3FAC838F-E652-40AD-A4CC-E00D60B51E7B/reject", {"deviceId": device_id, "action": "reject"}, "POST"),
                ("GET", "/ripdock/admin/devices/3FAC838F-E652-40AD-A4CC-E00D60B51E7B/revoke", {"deviceId": device_id, "action": "revoke"}, "POST"),
            ],
            device_calls,
        )

    def test_dashboard_body_action_falls_back_when_path_route_rejects(self):
        api = load_dashboard_api()
        calls = []
        old_proxy = api._proxy_json
        device_id = "device/with-special:id"

        def fake_proxy(method, path, body=None, route_method=None):
            calls.append((method, path, body, route_method))
            if path == "/ripdock/admin/devices/device%2Fwith-special%3Aid/approve":
                return 400, {"ok": False, "message": "path route rejected"}
            return 200, {
                "ok": True,
                "deviceId": body["deviceId"],
                "trustState": "trusted",
                "state": {"pendingDevices": [], "trustedDevices": [{"deviceId": body["deviceId"]}]},
            }

        try:
            api._proxy_json = fake_proxy
            result = asyncio.run(api.post_device_action("approve", api.DeviceActionBody(deviceId=device_id, action="approve")))
        finally:
            api._proxy_json = old_proxy

        self.assertEqual("trusted", result["trustState"])
        self.assertEqual(device_id, result["deviceId"])
        self.assertEqual(
            [
                ("GET", "/ripdock/admin/devices/device%2Fwith-special%3Aid/approve", {"deviceId": device_id, "action": "approve"}, "POST"),
                ("GET", "/ripdock/admin/devices/approve", {"deviceId": device_id, "action": "approve"}, "POST"),
            ],
            calls,
        )

    def test_runtime_reference_decodes_admin_device_ids(self):
        from fastapi.testclient import TestClient

        runtime_app = load_runtime_app()
        calls = []

        def approve(device_id):
            calls.append(("path", device_id))
            return {"deviceId": device_id, "trustState": "trusted"}

        def body_approve(device_id):
            calls.append(("body", device_id))
            return {"deviceId": device_id, "trustState": "trusted"}

        adapter = types.SimpleNamespace(
            _approve_pending_device=approve,
            _reject_pending_device=lambda device_id: {"deviceId": device_id, "trustState": "rejected"},
            _revoke_trusted_device=lambda device_id: {"deviceId": device_id, "trustState": "revoked"},
            _ensure_device_maps=lambda: ({}, {}, {}, {}),
            _admin_state=lambda: {},
        )
        adapter._approve_pending_device = body_approve
        client = TestClient(runtime_app.create_runtime_app(adapter))

        path_response = client.get("/ripdock/admin/devices/device%3Awith-special/approve")
        body_response = client.post("/ripdock/admin/devices/approve", json={"device_id": "body-device"})

        self.assertEqual(200, path_response.status_code)
        self.assertEqual("device:with-special", path_response.json()["deviceId"])
        self.assertEqual(200, body_response.status_code)
        self.assertEqual("body-device", body_response.json()["deviceId"])
        self.assertEqual([("body", "device:with-special"), ("body", "body-device")], calls)

    def test_dashboard_api_url_validation(self):
        api = load_dashboard_api()
        self.assertTrue(api.is_device_facing_runtime_url("https://runtime.example.com"))
        self.assertTrue(api.is_device_facing_runtime_url("https://ripdock-dev1.ripdock.com"))
        self.assertFalse(api.is_device_facing_runtime_url("http://runtime.example.com"))
        self.assertFalse(api.is_device_facing_runtime_url("https://localhost:8443"))
        self.assertFalse(api.is_device_facing_runtime_url("https://192.168.1.10"))

    def test_dashboard_api_public_url_save_accepts_https_and_rejects_private_urls(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({"runtimeId": "runtime-1"}))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "public-runtime-url")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                saved = asyncio.run(api.post_public_url(api.PublicURLBody(publicURL="https://runtime.example.com/")))
                with self.assertRaises(api.HTTPException):
                    asyncio.run(api.post_public_url(api.PublicURLBody(publicURL="http://runtime.example.com")))
                with self.assertRaises(api.HTTPException):
                    asyncio.run(api.post_public_url(api.PublicURLBody(publicURL="https://localhost:8443")))
                with self.assertRaises(api.HTTPException):
                    asyncio.run(api.post_public_url(api.PublicURLBody(publicURL="https://10.0.0.8")))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertTrue(saved["ok"])
        self.assertEqual("https://runtime.example.com", saved["publicURL"])

    def test_dashboard_api_connected_state_keeps_saved_public_url(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            public_url_file = Path(directory) / "public-runtime-url"
            identity_file.write_text(json.dumps({"runtimeId": "runtime-1"}))
            public_url_file.write_text("https://hermes.example.com\n")
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(public_url_file)
                os.environ.pop("RIPDOCK_PUBLIC_RUNTIME_URL", None)

                original_proxy_json = api._proxy_json
                api._proxy_json = lambda *args, **kwargs: (
                    200,
                    {
                        "runtimeIdentity": {"runtimeId": "runtime-1"},
                        "runtimeMetadata": {},
                        "runtimeAgents": [],
                        "publicURL": {
                            "configured": "",
                            "active": "",
                            "detectedTunnelURL": None,
                        },
                    },
                )
                try:
                    state = api.dashboard_state()
                finally:
                    api._proxy_json = original_proxy_json
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("https://hermes.example.com", state["publicURL"]["detectedTunnelURL"])
        self.assertEqual("https://hermes.example.com", state["publicURL"]["active"])

    def test_dashboard_api_generates_pairing_code_without_public_url(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({"runtimeId": "runtime-1"}))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                result = api.generate_pairing_payload()
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertRegex(result["pairingCode"], r"^\d{6}$")
        self.assertIsNone(result["pairingPayload"])
        self.assertIn("does not grant trust", result["securityNotice"])

    def test_dashboard_api_generates_pairing_payload_for_https_public_url(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(Path(directory) / "runtime-identity.json")
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL"] = "https://runtime.example.com"
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                result = api.generate_pairing_payload()
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertRegex(result["pairingCode"], r"^\d{6}$")
        self.assertEqual(result["pairingCode"], result["pairingPayload"]["pairing_code"])
        self.assertEqual(
            f"wss://runtime.example.com/ripdock/app/pair/{result['pairingCode']}",
            result["pairingPayload"]["runtime_url"],
        )
        self.assertIn("runtime_id", result["pairingPayload"])
        self.assertIn("runtime_identity", result["pairingPayload"])
        self.assertNotIn("metadata", result["pairingPayload"]["runtime_identity"])

    def test_dashboard_api_infers_runtime_admin_url_from_hermes_host_and_embedded_port(self):
        api = load_dashboard_api()
        old_env = os.environ.copy()
        try:
            os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
            os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
            os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
            os.environ.pop("RIPDOCK_EMBEDDED_HOST", None)
            os.environ.pop("RIPDOCK_EMBEDDED_PORT", None)
            os.environ.pop("HERMES_DASHBOARD_HOST", None)
            self.assertEqual("http://localhost:8788", api.admin_base_url())

            os.environ["HERMES_DASHBOARD_HOST"] = "0.0.0.0"
            os.environ["RIPDOCK_EMBEDDED_PORT"] = "9876"
            self.assertEqual("http://localhost:9876", api.admin_base_url())

            os.environ["HERMES_DASHBOARD_HOST"] = "192.0.2.10"
            self.assertEqual("http://192.0.2.10:9876", api.admin_base_url())

            os.environ["RIPDOCK_HOST_RELAY_HOST"] = "relay.local"
            os.environ["RIPDOCK_HOST_RELAY_PORT"] = "9999"
            self.assertEqual("http://relay.local:9999", api.admin_base_url())

            os.environ["RIPDOCK_RUNTIME_ADMIN_URL"] = "http://admin.example:7777/"
            self.assertEqual("http://admin.example:7777", api.admin_base_url())
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_dashboard_api_device_actions_are_explicit_when_runtime_admin_missing(self):
        api = load_dashboard_api()
        old_env = os.environ.copy()
        try:
            os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
            os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
            os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
            status, payload = api._proxy_json("POST", "/ripdock/admin/devices/device-1/approve")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertEqual(503, status)
        self.assertFalse(payload["implemented"])

    def test_dashboard_api_strips_non_emoji_runtime_icons(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            state_file = Path(directory) / "state.json"
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({
                "runtimeId": "runtime-1",
                "displayName": "Runtime",
                "publicKey": "public-key",
                "publicKeyFingerprint": "fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
            }))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(state_file)
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                result = asyncio.run(api.post_metadata(api.MetadataBody(displayName="Runtime", icon="server.rack", accentColor="#2563eb", backgroundColor="#ffffff")))
                state = asyncio.run(api.get_state())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("", result["runtimeMetadata"]["icon"])
        self.assertEqual("", state["runtimeMetadata"]["icon"])

    def test_runtime_agents_default_to_discovered_hermes_profiles(self):
        adapter, _message = self._signed_resume_fixture()
        old_env = os.environ.copy()
        try:
            adapter._hermes_profile_names = lambda: ["default"]
            advertised = adapter._agent_definitions()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertEqual(["default"], [agent["agent_id"] for agent in advertised])

    def test_device_label_is_dashboard_metadata_and_persists(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            state_file = Path(directory) / "state.json"
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
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(state_file)
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)

                payload = self._sample_device_identity("device-1", seed="pk")
                api.pairing_request(payload)
                asyncio.run(api.post_device_approve("device-1"))
                labeled = asyncio.run(api.post_device_label("device-1", api.DeviceLabelBody(label="  Dave's iPhone 16 Pro  ")))
                reloaded_api = load_dashboard_api()
                reloaded_state = asyncio.run(reloaded_api.get_state())
                cleared = asyncio.run(api.post_device_label("device-1", api.DeviceLabelBody(label=" ")))
                cleared_state = asyncio.run(api.get_state())
                persisted_identity = json.loads(identity_file.read_text())
                persisted_state = json.loads(state_file.read_text())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        labeled_device = labeled["state"]["trustedDevices"][0]
        reloaded_device = reloaded_state["trustedDevices"][0]
        cleared_device = cleared_state["trustedDevices"][0]
        self.assertEqual("Dave's iPhone 16 Pro", labeled["label"])
        self.assertEqual("Dave's iPhone 16 Pro", labeled_device["label"])
        self.assertEqual("Dave's iPhone 16 Pro", reloaded_device["label"])
        self.assertEqual("", cleared["label"])
        self.assertEqual("", cleared_device["label"])
        self.assertEqual("device-1", labeled_device["deviceId"])
        self.assertEqual(payload["publicKeyFingerprint"], labeled_device["deviceFingerprint"])
        self.assertEqual("trusted", labeled_device["status"])
        self.assertNotIn("label", persisted_identity["trustedDevices"]["device-1"])
        self.assertNotIn("deviceLabels", persisted_identity)
        self.assertEqual({}, persisted_state.get("deviceLabels", {}))

    def test_pending_device_label_carries_into_trusted_summary(self):
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

                api.pairing_request(self._sample_device_identity("device-1", seed="pk"))
                pending_label = asyncio.run(api.post_device_label("device-1", api.DeviceLabelBody(label="Dave's iPad")))
                approved = asyncio.run(api.post_device_approve("device-1"))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("Dave's iPad", pending_label["state"]["pendingDevices"][0]["label"])
        self.assertEqual("Dave's iPad", approved["state"]["trustedDevices"][0]["label"])

    def test_device_label_rejects_too_long_value(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(Path(directory) / "runtime-identity.json")
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                with self.assertRaises(api.HTTPException) as error:
                    asyncio.run(api.post_device_label("device-1", api.DeviceLabelBody(label="x" * 81)))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(400, error.exception.status_code)
        self.assertIn("80 characters", error.exception.detail)

    def test_pairing_reject_and_metadata_save_do_not_clear_device_trust_state(self):
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
                reject_payload = self._sample_device_identity("reject-me", seed="pk-r")
                keep_payload = self._sample_device_identity("keep-me", seed="pk-k")
                api.pairing_request(reject_payload)
                reject_result = asyncio.run(api.post_device_reject("reject-me"))
                api.pairing_request(keep_payload)
                asyncio.run(api.post_device_approve("keep-me"))
                asyncio.run(api.post_metadata(api.MetadataBody(displayName="Changed", icon="", accentColor="", backgroundColor="#ffffff")))
                state = asyncio.run(api.get_state())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("rejected", reject_result["trustState"])
        self.assertEqual([], [device["deviceId"] for device in state["pendingDevices"]])
        self.assertEqual(["keep-me"], [device["deviceId"] for device in state["trustedDevices"]])
        self.assertEqual("Changed", state["runtimeMetadata"]["displayName"])

    def test_dashboard_api_metadata_save_persists_and_preserves_identity(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            state_file = Path(directory) / "state.json"
            identity_file = Path(directory) / "runtime-identity.json"
            identity = {
                "runtimeId": "runtime-1",
                "displayName": "Immutable Runtime",
                "publicKey": "public-key",
                "publicKeyFingerprint": "SHA256:identity-fingerprint",
                "protocolVersion": "1",
                "createdAt": "2026-01-01T00:00:00Z",
            }
            identity_file.write_text(json.dumps(identity))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(state_file)
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                result = asyncio.run(
                    api.post_metadata(
                        api.MetadataBody(
                            displayName="Saved Runtime",
                            icon="🚀",
                            accentColor="#16a34a",
                            backgroundColor="#dcfce7",
                        )
                    )
                )
                state = asyncio.run(api.get_state())
                reloaded_metadata = api.runtime_metadata(api.load_runtime_identity())
                reloaded_api = load_dashboard_api()
                reload_state = asyncio.run(reloaded_api.get_state())
                persisted_identity = json.loads(identity_file.read_text())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertTrue(result["ok"])
        self.assertEqual(result["runtimeMetadata"], result["state"]["runtimeMetadata"])
        self.assertEqual("Saved Runtime", state["runtimeMetadata"]["displayName"])
        self.assertEqual("🚀", state["runtimeMetadata"]["icon"])
        self.assertEqual("#16a34a", state["runtimeMetadata"]["accentColor"])
        self.assertEqual("#dcfce7", state["runtimeMetadata"]["backgroundColor"])
        self.assertEqual("Saved Runtime", state["runtimeIdentity"]["displayName"])
        self.assertNotIn("metadata", state["runtimeIdentity"])
        self.assertEqual("Saved Runtime", reloaded_metadata["displayName"])
        self.assertEqual("🚀", reloaded_metadata["icon"])
        self.assertEqual("#16a34a", reloaded_metadata["accentColor"])
        self.assertEqual("#dcfce7", reloaded_metadata["backgroundColor"])
        self.assertEqual("Saved Runtime", reload_state["runtimeMetadata"]["displayName"])
        self.assertEqual("🚀", reload_state["runtimeMetadata"]["icon"])
        self.assertEqual("#16a34a", reload_state["runtimeMetadata"]["accentColor"])
        self.assertEqual("#dcfce7", reload_state["runtimeMetadata"]["backgroundColor"])
        self.assertEqual(identity["runtimeId"], persisted_identity["runtimeId"])
        self.assertRegex(persisted_identity["publicKeyFingerprint"], r"^[0-9a-f]{64}$")

    def test_dashboard_api_metadata_names_map_to_authoritative_store(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(Path(directory) / "runtime-identity.json")
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                result = asyncio.run(
                    api.post_metadata(
                        api.MetadataBody(
                            displayName="Runtime",
                            icon="🧪",
                            accentColor="#7c3aed",
                            backgroundColor="#dcfce7",
                        )
                    )
                )
                state = asyncio.run(api.get_state())
                stored = json.loads((Path(directory) / "state.json").read_text())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        expected = {
            "displayName": "Runtime",
            "icon": "🧪",
            "accentColor": "#7c3aed",
            "backgroundColor": "#dcfce7",
        }
        self.assertEqual(expected, result["runtimeMetadata"])
        self.assertEqual(expected, result["state"]["runtimeMetadata"])
        self.assertEqual(expected, state["runtimeMetadata"])
        self.assertEqual(expected, stored["runtimeMetadata"])
        self.assertEqual(expected, stored["metadata"])

    def test_dashboard_api_metadata_none_states_persist(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(Path(directory) / "runtime-identity.json")
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                asyncio.run(
                    api.post_metadata(
                        api.MetadataBody(
                            displayName="Plain Runtime",
                            icon="",
                            accentColor="",
                            backgroundColor="",
                        )
                    )
                )
                state = asyncio.run(api.get_state())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("Plain Runtime", state["runtimeMetadata"]["displayName"])
        self.assertEqual("", state["runtimeMetadata"]["icon"])
        self.assertEqual("", state["runtimeMetadata"]["accentColor"])
        self.assertEqual("#ffffff", state["runtimeMetadata"]["backgroundColor"])

    def test_dashboard_agent_enabled_defaults_true_and_persists_false(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            identity_file = Path(directory) / "runtime-identity.json"
            identity_file.write_text(json.dumps({"runtimeId": "runtime-1"}))
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(identity_file)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ["CODEX_HOME"] = str(Path(directory) / "empty-codex")
                api._hermes_profile_names = lambda: ["personal", "dev"]
                initial = api.runtime_agents({"runtimeId": "runtime-1"})
                result = asyncio.run(
                    api.post_agent_metadata(
                        "dev",
                        api.AgentMetadataBody(
                            displayName="Dev",
                            icon="🤖",
                            accentColor="#2563eb",
                            backgroundColor="#dbeafe",
                            sortOrder=1,
                            enabled=False,
                        ),
                    )
                )
                state = asyncio.run(api.get_state())
                stored = json.loads((Path(directory) / "state.json").read_text())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertTrue(all(agent["enabled"] is True for agent in initial))
        self.assertFalse(result["agentMetadata"]["enabled"])
        self.assertFalse(stored["agentMetadata"]["runtime-1"]["dev"]["enabled"])
        self.assertFalse(next(agent for agent in state["runtimeAgents"] if agent["agent_id"] == "dev")["enabled"])
        self.assertTrue(next(agent for agent in state["runtimeAgents"] if agent["agent_id"] == "personal")["enabled"])

    def test_dashboard_agents_default_to_discovered_hermes_profiles(self):
        api = load_dashboard_api()
        old_env = os.environ.copy()
        try:
            api._hermes_profile_names = lambda: ["default"]
            agents = api.runtime_agents({"runtimeId": "runtime-1"})
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertEqual(["default"], [agent["agent_id"] for agent in agents])

    def test_dashboard_api_saved_metadata_overrides_proxied_polling_defaults(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            old_proxy = api._proxy_json
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(Path(directory) / "state.json")
                os.environ["RIPDOCK_RUNTIME_IDENTITY_FILE"] = str(Path(directory) / "runtime-identity.json")
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(directory) / "missing")
                os.environ.pop("RIPDOCK_RUNTIME_ADMIN_URL", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_HOST", None)
                os.environ.pop("RIPDOCK_HOST_RELAY_PORT", None)
                asyncio.run(
                    api.post_metadata(
                        api.MetadataBody(
                            displayName="Saved Poll Runtime",
                            icon="🤖",
                            accentColor="#2563eb",
                            backgroundColor="#dbeafe",
                        )
                    )
                )

                def proxied_defaults(_method, _path, _body=None, _route_method=None):
                    return 200, {
                        "runtimeIdentity": {"runtimeId": "runtime-1", "publicKeyFingerprint": "SHA256:fp"},
                        "runtimeMetadata": {
                            "displayName": "Default Runtime",
                            "icon": "",
                            "accentColor": "",
                            "backgroundColor": "",
                        },
                    }

                api._proxy_json = proxied_defaults
                polled = asyncio.run(api.get_state())
            finally:
                api._proxy_json = old_proxy
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("Saved Poll Runtime", polled["runtimeMetadata"]["displayName"])
        self.assertEqual("🤖", polled["runtimeMetadata"]["icon"])
        self.assertEqual("#2563eb", polled["runtimeMetadata"]["accentColor"])
        self.assertEqual("#dbeafe", polled["runtimeMetadata"]["backgroundColor"])

    def test_public_runtime_metadata_route_returns_only_ui_metadata(self):
        from fastapi.testclient import TestClient

        runtime_app = load_runtime_app()
        adapter = types.SimpleNamespace()
        client = TestClient(runtime_app.create_runtime_app(adapter))

        response = client.get("/.well-known/ripdock/runtime-metadata")
        payload = response.json()

        self.assertEqual(403, response.status_code)
        self.assertEqual("authorization.denied", payload["code"])
        self.assertEqual(
            {"code", "message"},
            set(payload.keys()),
        )
