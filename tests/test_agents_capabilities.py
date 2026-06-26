from support import *


class AgentsCapabilitiesTests(PluginTestBase):
    def test_ripdock_toolsets_inherit_cli_when_unconfigured(self):
        adapter_module = load_backend_adapter()
        yaml_cfg = {
            "platform_toolsets": {
                "cli": ["web", "vision"],
            },
        }

        result = adapter_module._apply_yaml_config(yaml_cfg, {"enabled": True})

        self.assertEqual({"toolset_inheritance": "cli"}, result)
        self.assertEqual(["web", "vision"], yaml_cfg["platform_toolsets"]["ripdock"])
        self.assertIsNot(yaml_cfg["platform_toolsets"]["cli"], yaml_cfg["platform_toolsets"]["ripdock"])

    def test_ripdock_toolsets_do_not_override_explicit_config(self):
        adapter_module = load_backend_adapter()
        yaml_cfg = {
            "platform_toolsets": {
                "cli": ["web", "vision"],
                "ripdock": ["web"],
            },
        }

        result = adapter_module._apply_yaml_config(yaml_cfg, {"enabled": True})

        self.assertIsNone(result)
        self.assertEqual(["web"], yaml_cfg["platform_toolsets"]["ripdock"])

    def test_ripdock_toolset_resolver_inherits_cli_at_runtime(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        hermes_cli_module = types.ModuleType("hermes_cli")
        tools_config_module = types.ModuleType("hermes_cli.tools_config")

        def fake_get_platform_tools(config, platform, **_kwargs):
            return set((config.get("platform_toolsets") or {}).get(platform) or [f"hermes-{platform}"])

        tools_config_module._get_platform_tools = fake_get_platform_tools
        old_modules = {
            "hermes_cli": sys.modules.get("hermes_cli"),
            "hermes_cli.tools_config": sys.modules.get("hermes_cli.tools_config"),
        }
        try:
            hermes_cli_module.tools_config = tools_config_module
            sys.modules["hermes_cli"] = hermes_cli_module
            sys.modules["hermes_cli.tools_config"] = tools_config_module

            adapter._install_ripdock_toolset_inheritance_override()
            inherited = tools_config_module._get_platform_tools(
                {"platform_toolsets": {"cli": ["web", "vision"]}},
                "ripdock",
            )
            explicit = tools_config_module._get_platform_tools(
                {"platform_toolsets": {"cli": ["web", "vision"], "ripdock": ["web"]}},
                "ripdock",
            )

            self.assertEqual({"web", "vision"}, inherited)
            self.assertEqual({"web"}, explicit)
        finally:
            for name, old_module in old_modules.items():
                if old_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old_module

    def test_agent_settings_update_requires_advertised_setting_keys(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._agent_by_id = lambda agent_id: {
            "agent_id": agent_id,
            "settings": [
                {"key": "show_activity", "label": "Show Activity", "type": "boolean"},
                {"key": "reset_context", "label": "Reset Context", "type": "action"},
            ],
        } if agent_id == "personal" else None
        websocket = self._fake_embedded_websocket([
            json.dumps({
                "type": "agent.settings.update",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "settings": {"unknown_setting": True},
            }),
            json.dumps({
                "type": "agent.settings.update",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "actions": ["show_activity"],
            }),
            json.dumps({
                "type": "agent.settings.update",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "settings": {"show_activity": False},
            }),
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"agent:settings:update"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual("protocol.invalid_payload", websocket.sent[0]["code"])
        self.assertEqual("protocol.invalid_payload", websocket.sent[1]["code"])
        self.assertEqual("runtime.agents", websocket.sent[2]["type"])

    def test_app_capabilities_requires_strict_payload_shape(self):
        adapter, _message = self._signed_resume_fixture()
        websocket = self._fake_embedded_websocket([
            json.dumps({"type": "app.capabilities", "protocol_version": "1", "payload": {"features": {}}})
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = adapter._default_authorization_scopes()

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(["protocol.invalid_payload"], [event["code"] for event in websocket.sent])

    def test_default_app_capabilities_match_strict_payload_shape(self):
        adapter, _message = self._signed_resume_fixture()

        self.assertIsNone(adapter._validate_app_capabilities_payload(adapter._default_client_capabilities()))

    def test_runtime_capabilities_use_strict_flat_shape(self):
        adapter, _message = self._signed_resume_fixture()
        capabilities = adapter._runtime_capabilities()

        self.assertEqual("runtime.capabilities", capabilities["type"])
        self.assertEqual("1", capabilities["protocol_version"])
        self.assertTrue(capabilities["slash_commands"])
        self.assertNotIn("payload", capabilities)
        self.assertNotIn("artifact_request_resolution", capabilities)

    def test_runtime_does_not_advertise_or_accept_disabled_agents(self):
        adapter, _message = self._signed_resume_fixture()
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "dashboard-state.json"
            state_file.write_text(json.dumps({
                "agentMetadata": {
                    "runtime-1": {
                        "personal": {"display_name": "Personal", "enabled": True},
                        "dev": {"display_name": "Dev", "enabled": False},
                    }
                }
            }))
            old_env = os.environ.copy()
            try:
                os.environ["RIPDOCK_DASHBOARD_STATE_FILE"] = str(state_file)
                os.environ["CODEX_HOME"] = str(Path(directory) / "empty-codex")
                adapter._hermes_profile_names = lambda: ["personal", "dev"]
                advertised = adapter._agent_definitions()
                personal = adapter._agent_by_id("personal")
                disabled = adapter._agent_by_id("dev")
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(["personal"], [agent["agent_id"] for agent in advertised])
        self.assertTrue(all("values" not in agent for agent in advertised))
        self.assertIsNotNone(personal)
        self.assertIsNone(disabled)

    def test_runtime_metadata_uses_null_for_missing_icon_without_identity_icon(self):
        adapter, _message = self._signed_resume_fixture()
        old_env = os.environ.copy()
        try:
            os.environ.pop("RIPDOCK_RUNTIME_ICON", None)
            metadata = adapter._runtime_metadata()
            identity = adapter._runtime_identity()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertIsNone(metadata["icon"])
        self.assertNotIn("icon", identity)
        self.assertNotIn("accent_color", identity)
        self.assertIsNone(identity["runtime_metadata"]["icon"])
