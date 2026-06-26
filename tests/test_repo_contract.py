from support import *


class RepoContractTests(PluginTestBase):
    def test_no_python_cache_files(self):
        ignored_parts = {".git"}
        offenders = []
        for path in ROOT.rglob("*"):
            if ignored_parts.intersection(path.parts):
                continue
            if any(part == "venv" or part.startswith(".venv") for part in path.parts):
                continue
            if path.name == "__pycache__" or path.suffix == ".pyc":
                offenders.append(path.relative_to(ROOT))
        self.assertEqual([], offenders)

    def test_gitignore_blocks_generated_local_state(self):
        gitignore = (ROOT / ".gitignore").read_text().splitlines()
        required_patterns = {
            "*.swp",
            "*.swo",
            ".env",
            ".env.*",
            "!.env.example",
            "runtime-identity.json",
            "dashboard-state.json",
            "session.json",
            "public-runtime-url",
            "ripdock/",
            ".hermes/",
            "artifacts/",
            "transfers/",
            "codex-summary.log",
        }
        self.assertTrue(required_patterns.issubset(set(gitignore)))

    def test_generated_local_state_files_are_not_tracked(self):
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        tracked_files = set(result.stdout.splitlines())
        forbidden_names = {
            "runtime-identity.json",
            "dashboard-state.json",
            "session.json",
            "public-runtime-url",
        }
        offenders = [
            path
            for path in tracked_files
            if Path(path).name in forbidden_names
            or path.startswith(("ripdock/", ".hermes/", "artifacts/", "transfers/"))
        ]
        self.assertEqual([], offenders)

    def test_dashboard_default_state_uses_home_dot_hermes(self):
        api = load_dashboard_api()
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ.pop("HERMES_HOME", None)
                os.environ["HOME"] = directory
                for name in (
                    "RIPDOCK_DASHBOARD_STATE_FILE",
                    "RIPDOCK_RUNTIME_IDENTITY_FILE",
                    "RIPDOCK_PUBLIC_RUNTIME_URL_FILE",
                    "RIPDOCK_SESSION_FILE",
                ):
                    os.environ.pop(name, None)

                state_root = Path(directory) / ".hermes" / "ripdock"
                self.assertEqual(state_root / "dashboard-state.json", api.state_path())
                self.assertEqual(state_root / "runtime-identity.json", api.runtime_identity_path())
                self.assertEqual(state_root / "public-runtime-url", api.public_runtime_url_file())
                self.assertEqual(state_root / "session.json", api.session_file_path())

                os.environ["HERMES_HOME"] = str(Path(directory) / "custom-hermes")
                override_root = Path(directory) / "custom-hermes" / "ripdock"
                self.assertEqual(override_root / "dashboard-state.json", api.state_path())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

    def test_backend_default_state_uses_home_dot_hermes(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ.pop("HERMES_HOME", None)
                os.environ["HOME"] = directory
                for name in (
                    "RIPDOCK_PROFILE_SESSIONS_FILE",
                    "RIPDOCK_CONVERSATION_TITLES_FILE",
                    "RIPDOCK_CRON_STATE_FILE",
                    "RIPDOCK_RUNTIME_IDENTITY_FILE",
                    "RIPDOCK_DASHBOARD_STATE_FILE",
                    "RIPDOCK_SESSION_FILE",
                    "RIPDOCK_TRANSFER_DIR",
                ):
                    os.environ.pop(name, None)

                hermes_home = Path(directory) / ".hermes"
                state_root = hermes_home / "ripdock"
                self.assertEqual(state_root / "profile-sessions.json", adapter._profile_session_state_file_path())
                self.assertEqual(state_root / "conversation-titles.json", adapter._conversation_title_state_file_path())
                self.assertEqual(hermes_home / "sessions" / "sessions.json", adapter._gateway_session_state_file_path())
                self.assertEqual(state_root / "cron-targets.json", adapter._ripdock_cron_state_file_path())
                self.assertEqual(state_root / "runtime-identity.json", adapter._runtime_identity_file_path())
                self.assertEqual(state_root / "dashboard-state.json", adapter._runtime_metadata_state_file_path())
                self.assertEqual(state_root / "session.json", adapter._session_file_path())
                self.assertEqual(state_root / "transfers" / "transfer-1.bin", adapter._transfer_file_path("transfer-1"))

                os.environ["HERMES_HOME"] = str(Path(directory) / "custom-hermes")
                override_root = Path(directory) / "custom-hermes" / "ripdock"
                self.assertEqual(override_root / "runtime-identity.json", adapter._runtime_identity_file_path())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

    def test_root_init_is_entrypoint_not_monolith(self):
        root_init = ROOT / "__init__.py"
        self.assertTrue(root_init.exists())
        self.assertLess(root_init.stat().st_size, 10_000)
        self.assertLess(len(root_init.read_text().splitlines()), 100)

    def test_plugin_repo_has_standalone_layout(self):
        expected = (
            "__init__.py",
            "plugin.yaml",
            "Makefile",
            "README.md",
            "backend/adapter.py",
            "dashboard/api.py",
            "dashboard/app.js",
            "runtime/RIPDOCK.md",
        )
        for relative_path in expected:
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)
        self.assertFalse((ROOT / "hermes_plugin").exists())

    def test_dev_qa_routes_are_not_committed_to_plugin_source(self):
        route_prefix = "/" + "dev-qa"
        scanned_roots = ("dashboard", "runtime", "backend", "shared")
        offenders = []
        for root_name in scanned_roots:
            for path in (ROOT / root_name).rglob("*"):
                if path.is_file() and path.suffix in {".py", ".js", ".json", ".css", ".html", ".yaml", ".yml"}:
                    if route_prefix in path.read_text(errors="ignore"):
                        offenders.append(path.relative_to(ROOT))
        self.assertEqual([], offenders)

    def test_plugin_manifest_exists(self):
        self.assertTrue((ROOT / "plugin.yaml").is_file())

    def test_dashboard_manifest_registers_tab(self):
        manifest_path = ROOT / "dashboard" / "manifest.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text())

        self.assertEqual("ripdock", manifest["name"])
        self.assertEqual("RipDock", manifest["label"])
        self.assertEqual("Hermes plugin for RipDock", manifest["description"])
        self.assertEqual("Network", manifest["icon"])
        self.assertEqual("0.1.0", manifest["version"])
        self.assertEqual("/ripdock-protocol", manifest["tab"]["path"])
        self.assertIn("position", manifest["tab"])
        self.assertEqual("app.js", manifest["entry"])
        self.assertEqual("styles.css", manifest["css"])
        self.assertEqual("api.py", manifest["api"])

    def test_dashboard_assets_exist(self):
        self.assertTrue((ROOT / "dashboard" / "app.js").is_file())
        self.assertTrue((ROOT / "dashboard" / "styles.css").is_file())
        self.assertTrue((ROOT / "dashboard" / "api.py").is_file())

    def test_runtime_crypto_dependency_is_hard_requirement(self):
        adapter_module = load_backend_adapter()
        dashboard_api = load_dashboard_api()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)

        adapter_public_key, adapter_private_key = adapter._generate_runtime_keypair()
        dashboard_public_key, dashboard_private_key = dashboard_api._generate_runtime_keypair()

        self.assertTrue(adapter_module.check_requirements())
        for public_key, private_key in (
            (adapter_public_key, adapter_private_key),
            (dashboard_public_key, dashboard_private_key),
        ):
            self.assertEqual("EC", public_key["kty"])
            self.assertEqual("P-256", public_key["crv"])
            self.assertRegex(public_key["key_id"], r"^[0-9a-f]{64}$")
            self.assertIn("BEGIN PRIVATE KEY", private_key)

    def test_plugin_yaml_matches_dashboard_manifest(self):
        plugin_manifest = dict(
            line.split(": ", 1)
            for line in (ROOT / "plugin.yaml").read_text().splitlines()
            if ": " in line
        )
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())

        self.assertEqual(manifest["name"], plugin_manifest["name"])
        self.assertEqual(manifest["label"], plugin_manifest["label"])
        self.assertEqual(manifest["version"], plugin_manifest["version"])

    def test_runtime_public_url_file_defaults_to_hermes_state_path(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                os.environ.pop("RIPDOCK_PUBLIC_RUNTIME_URL_FILE", None)
                self.assertEqual(Path(directory) / "ripdock" / "public-runtime-url", adapter._public_runtime_url_file())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

    def test_adapter_declares_gateway_runner_for_hermes_injection(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter(types.SimpleNamespace(extra={}))

        self.assertTrue(hasattr(adapter, "gateway_runner"))
        self.assertIsNone(adapter.gateway_runner)

    def test_profile_chat_detail_redacts_session_id_banner(self):
        adapter, _message = self._signed_resume_fixture()

        detail = adapter._redact_profile_chat_detail(
            "Error: failed\n\nsession_id: 20260605_052918_e055b2\n"
        )

        self.assertIn("Error: failed", detail)
        self.assertIn("session_id: <redacted>", detail)
        self.assertNotIn("20260605_052918_e055b2", detail)

    def test_protocol_log_redacts_sensitive_fields(self):
        adapter, _message = self._signed_resume_fixture()
        raw = json.dumps({
            "type": "session.resume",
            "session_id": "session-secret",
            "resume_signature": {
                "nonce": "nonce-secret",
                "signature": "signature-secret",
            },
            "payload": {
                "download_url": "https://runtime.example/ripdock/transfer/secret",
                "transfer_url": "wss://runtime.example/ripdock/transfer/secret/app",
                "authorization": "Bearer secret",
                "safe": "visible",
            },
        })

        redacted = adapter._redact_protocol_log(raw)

        self.assertNotIn("session-secret", redacted)
        self.assertNotIn("token-secret", redacted)
        self.assertNotIn("nonce-secret", redacted)
        self.assertNotIn("signature-secret", redacted)
        self.assertNotIn("transfer/secret", redacted)
        self.assertNotIn("Bearer secret", redacted)
        self.assertIn("visible", redacted)

    def test_runtime_embedded_route_contract_separates_http_and_websocket(self):
        runtime_app = load_runtime_app()
        adapter = types.SimpleNamespace(
            _public_runtime_identity=lambda: {"runtime_id": "runtime-1"},
            _admin_state=lambda: {"ok": True},
            _admin_conversations_snapshot=lambda agent_id="", conversation_id="": [],
            _handle_embedded_artifact_download=lambda transfer_id: (200, [], b"artifact"),
            _handle_pairing_request=lambda payload: {"trustState": "pendingApproval"},
            _handle_pairing_status=lambda payload: {"trustState": "notFound"},
            _pairing_error_response=lambda message: {"error": {"message": message}},
            _create_pairing_code=lambda: "123456",
            _save_session_state=lambda: None,
            _direct_pairing_payload=lambda code, host, port: {"pairing_code": code},
            _log_pairing=lambda code, payload: None,
            _iso_after=lambda seconds: "2026-01-01T00:00:00Z",
            _pairing_ttl_seconds=lambda: 60,
            _approve_pending_device=lambda device_id: {"deviceId": device_id, "trustState": "trusted"},
            _reject_pending_device=lambda device_id: {"deviceId": device_id, "trustState": "rejected"},
            _revoke_trusted_device=lambda device_id: {"deviceId": device_id, "trustState": "revoked"},
            _ensure_device_maps=lambda: ({}, {}, {}, {}),
            _handle_embedded_app=lambda websocket, path: None,
            embedded_host="127.0.0.1",
            embedded_port=8788,
        )
        app = runtime_app.create_runtime_app(adapter)
        routes = {route.path: route for route in app.routes}

        for route in {
            "/.well-known/ripdock/runtime-identity",
            "/.well-known/ripdock/runtime-metadata",
            "/ripdock/admin/state",
            "/ripdock/admin/conversations",
            "/ripdock/admin/pairing-payloads",
            "/ripdock/pairing/request",
            "/ripdock/pairing/status",
            "/ripdock/admin/devices/{device_id}/{action}",
            "/ripdock/admin/devices/{action}",
            "/ripdock/transfer/{transfer_id}/artifact",
            "/ripdock/app",
            "/ripdock/app/pair/{pairing_code}",
            "/ripdock/transfer/{transfer_id}/{role}",
        }:
            self.assertIn(route, routes)

        self.assertIsNone(app.openapi_url)
        self.assertNotIn("GET", routes["/ripdock/pairing/status"].methods)
        self.assertIn("POST", routes["/ripdock/pairing/status"].methods)
