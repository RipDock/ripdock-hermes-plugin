from support import *


class RuntimeIntentsTests(PluginTestBase):
    def _runtime_output_adapter(self, tool_names=None):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.session_id = "session-1"
        adapter.runtime_id = "runtime-1"
        adapter.app_capabilities_by_session = {
            "session-1": adapter._default_client_capabilities(),
        }
        adapter._outbound_conversation_by_message_id = {}
        adapter._outbound_websocket_by_message_id = {}
        adapter._outbound_content_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter._outbound_message_count_by_conversation = {}
        adapter._suppressed_home_channel_notice_conversations = set()
        adapter._conversation_context_by_id = {}
        adapter._raw_tool_details = {}
        adapter._activity_state_by_conversation = {}
        adapter._active_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._artifact_ids_by_message_id = {}
        adapter._hermes_tool_progress_names = frozenset(tool_names or {"skills_list", "skill_view", "web_search", "terminal"})
        adapter._websocket_for_ripdock_send = lambda *args, **kwargs: object()
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to
        return adapter, sent_messages

    def test_tool_progress_names_load_after_model_tools_registration(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        model_tools_module = types.ModuleType("model_tools")
        tools_module = types.ModuleType("tools")
        registry_module = types.ModuleType("tools.registry")

        class FakeRegistry:
            def get_all_tool_names(self):
                return ["skill_view", "web_search", "search_files", "terminal"]

        registry_module.registry = FakeRegistry()
        old_modules = {
            "model_tools": sys.modules.get("model_tools"),
            "tools": sys.modules.get("tools"),
            "tools.registry": sys.modules.get("tools.registry"),
        }
        try:
            sys.modules["model_tools"] = model_tools_module
            sys.modules["tools"] = tools_module
            sys.modules["tools.registry"] = registry_module

            names = adapter._load_hermes_tool_progress_names()

            self.assertEqual({"skill_view", "web_search", "search_files", "terminal"}, names)
        finally:
            for name, old_module in old_modules.items():
                if old_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old_module

    def test_ripdock_context_diagnostics_writes_model_request(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        run_agent_module = types.ModuleType("run_agent")
        agent_module = types.ModuleType("agent")
        model_metadata_module = types.ModuleType("agent.model_metadata")

        class FakeAIAgent:
            platform = "ripdock"
            session_id = "session/one"
            model = "test-model"
            provider = "test-provider"
            api_mode = "chat_completions"
            tools = [{"type": "function", "function": {"name": "test_tool"}}]

            def _build_api_kwargs(self, api_messages):
                return {"messages": api_messages, "api_key": "secret"}

        model_metadata_module.estimate_messages_tokens_rough = lambda messages: 42
        run_agent_module.AIAgent = FakeAIAgent

        old_modules = {
            "run_agent": sys.modules.get("run_agent"),
            "agent": sys.modules.get("agent"),
            "agent.model_metadata": sys.modules.get("agent.model_metadata"),
        }
        old_env = {
            "RIPDOCK_CONTEXT_DIAGNOSTICS": os.environ.get("RIPDOCK_CONTEXT_DIAGNOSTICS"),
            "RIPDOCK_CONTEXT_DIAGNOSTICS_DIR": os.environ.get("RIPDOCK_CONTEXT_DIAGNOSTICS_DIR"),
        }
        try:
            sys.modules["run_agent"] = run_agent_module
            sys.modules["agent"] = agent_module
            sys.modules["agent.model_metadata"] = model_metadata_module
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["RIPDOCK_CONTEXT_DIAGNOSTICS"] = "true"
                os.environ["RIPDOCK_CONTEXT_DIAGNOSTICS_DIR"] = tmpdir

                adapter._install_ripdock_context_diagnostics_override()
                api_messages = [{"role": "user", "content": "hello"}]
                api_kwargs = FakeAIAgent()._build_api_kwargs(api_messages)
                output_files = list(Path(tmpdir).glob("*.json"))

                self.assertEqual({"messages": api_messages, "api_key": "secret"}, api_kwargs)
                self.assertEqual(1, len(output_files))
                payload = json.loads(output_files[0].read_text())
                self.assertEqual("ripdock.context_diagnostics.v1", payload["schema"])
                self.assertEqual(api_messages, payload["messages"])
                self.assertEqual(FakeAIAgent.tools, payload["tools"])
                self.assertEqual(42, payload["approx_message_tokens"])
                self.assertNotIn("api_key", payload)
        finally:
            for name, old_module in old_modules.items():
                if old_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old_module
            for name, old_value in old_env.items():
                if old_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old_value

    def test_runtime_intent_parser_accepts_strict_json_object(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)

        intent = adapter._runtime_intent_from_content(json.dumps({
            "runtime_intent": "ripdock.artifact.deliver",
            "arguments": {"path": "/opt/data/report.pdf"},
            "visible_text": "Sending it now.",
        }))

        self.assertEqual("ripdock.artifact.deliver", intent["runtime_intent"])
        self.assertEqual({"path": "/opt/data/report.pdf"}, intent["arguments"])
        self.assertEqual("Sending it now.", intent["visible_text"])

    def test_runtime_intent_parser_accepts_single_json_fence(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)

        intent = adapter._runtime_intent_from_content(
            "```json\n"
            '{"runtime_intent": "ripdock.activity.report", "arguments": {"tool": "web_search"}}'
            "\n```"
        )

        self.assertEqual("ripdock.activity.report", intent["runtime_intent"])
        self.assertEqual({"tool": "web_search"}, intent["arguments"])

    def test_runtime_intent_json_bypasses_generic_local_file_extraction(self):
        adapter_module = load_backend_adapter()
        original = json.dumps({
            "runtime_intent": "ripdock.artifact.deliver",
            "arguments": {
                "path": "/opt/data/ripdock/smoke-artifacts/ripdock-smoke-report.txt",
                "description": "Runtime-local report file",
            },
            "visible_text": "Sending it now.",
        }, indent=2)
        calls = []

        def fake_extract_local_files(content):
            calls.append(content)
            return ["/opt/data/ripdock/smoke-artifacts/ripdock-smoke-report.txt"], content.replace(
                "/opt/data/ripdock/smoke-artifacts/ripdock-smoke-report.txt",
                "",
            )

        adapter_module.BasePlatformAdapter.extract_local_files = staticmethod(fake_extract_local_files)

        files, cleaned = adapter_module.RipDockAdapter.extract_local_files(original)

        self.assertEqual([], files)
        self.assertEqual(original, cleaned)
        self.assertEqual([], calls)

    def test_plain_text_file_extraction_still_uses_base_behavior(self):
        adapter_module = load_backend_adapter()
        original = "Here is the report: /opt/data/report.pdf"
        calls = []

        def fake_extract_local_files(content):
            calls.append(content)
            return ["/opt/data/report.pdf"], "Here is the report:"

        adapter_module.BasePlatformAdapter.extract_local_files = staticmethod(fake_extract_local_files)

        files, cleaned = adapter_module.RipDockAdapter.extract_local_files(original)

        self.assertEqual(["/opt/data/report.pdf"], files)
        self.assertEqual("Here is the report:", cleaned)
        self.assertEqual([original], calls)

    def test_hermes_tool_progress_send_emits_activity_not_visible_delta(self):
        adapter, sent_messages = self._runtime_output_adapter()

        result = asyncio.run(adapter.send(
            chat_id="conversation-1",
            content='📚 skill_view([\'name\'])\n{"name": "hermes-agent"}',
        ))

        self.assertTrue(result.success)
        self.assertEqual(["message.block"], [message["type"] for message in sent_messages])
        block = sent_messages[0]["block"]
        self.assertEqual("activity.tool.progress", block["kind"])
        payload = json.loads(block["content"])
        self.assertEqual("skill_view", payload["tool"])
        self.assertEqual("hermes-agent", payload["args"]["name"])
        self.assertEqual("Loading skill", payload["summary"])

    def test_hermes_tool_progress_send_keeps_answer_remainder(self):
        adapter, sent_messages = self._runtime_output_adapter()

        result = asyncio.run(adapter.send(
            chat_id="conversation-1",
            content='📚 skills_list...\nHere is the answer.',
        ))

        self.assertTrue(result.success)
        self.assertEqual(["message.block", "message.delta"], [message["type"] for message in sent_messages])
        self.assertEqual("Here is the answer.", sent_messages[1]["delta"])

    def test_hermes_tool_progress_accepts_skin_prefix_and_preview(self):
        adapter, sent_messages = self._runtime_output_adapter()

        result = asyncio.run(adapter.send(
            chat_id="conversation-1",
            content='TOOL web_search: "RipDock docs"',
        ))

        self.assertTrue(result.success)
        self.assertEqual(["message.block"], [message["type"] for message in sent_messages])
        payload = json.loads(sent_messages[0]["block"]["content"])
        self.assertEqual("web_search", payload["tool"])
        self.assertEqual("RipDock docs", payload["args"]["preview"])

    def test_hermes_terminal_progress_block_emits_activity(self):
        adapter, sent_messages = self._runtime_output_adapter()

        result = asyncio.run(adapter.send(
            chat_id="conversation-1",
            content="💻 terminal\n```\nmake smoke-runtime-chat\n```",
        ))

        self.assertTrue(result.success)
        self.assertEqual(["message.block"], [message["type"] for message in sent_messages])
        payload = json.loads(sent_messages[0]["block"]["content"])
        self.assertEqual("terminal", payload["tool"])
        self.assertEqual("make smoke-runtime-chat", payload["args"]["command"])

    def test_unloaded_tool_name_is_not_intercepted(self):
        adapter, sent_messages = self._runtime_output_adapter(tool_names={"skills_list"})

        result = asyncio.run(adapter.send(
            chat_id="conversation-1",
            content="📚 imaginary_tool...",
        ))

        self.assertTrue(result.success)
        self.assertEqual(["message.delta"], [message["type"] for message in sent_messages])
        self.assertEqual("📚 imaginary_tool...", sent_messages[0]["delta"])

    def test_artifact_deliver_intent_validates_any_regular_file_under_size_limit(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_id = "runtime-1"
        adapter._generated_artifacts_by_id = {}
        adapter._generated_artifacts_by_key = {}
        adapter._artifact_ids_by_message_id = {}
        adapter._conversation_context_by_id = {}
        adapter._outbound_content_by_message_id = {}
        adapter._outbound_conversation_by_message_id = {}
        adapter._outbound_websocket_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter.transfers = {}
        adapter.embedded_public_url = "https://runtime.example.com"
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to

        with tempfile.TemporaryDirectory() as hermes_home, tempfile.TemporaryDirectory() as artifact_dir:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = hermes_home
                path = Path(artifact_dir) / "report.ripdock-blob"
                path.write_bytes(b"runtime artifact bytes")
                content = json.dumps({
                    "runtime_intent": "ripdock.artifact.deliver",
                    "arguments": {"path": str(path), "description": "QA report"},
                    "visible_text": "Sending the report now.",
                })

                handled = asyncio.run(adapter._handle_runtime_intent_output(
                    object(),
                    "conversation-1",
                    "message-1",
                    content,
                    finalize=True,
                ))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertTrue(handled)
        self.assertEqual("message.delta", sent_messages[0]["type"])
        self.assertEqual("Sending the report now.", sent_messages[0]["delta"])
        created = next(message for message in sent_messages if message["type"] == "runtime.artifact.created")
        request = next(message for message in sent_messages if message["type"] == "runtime.transfer.request")
        self.assertEqual("report.ripdock-blob", created["filename"])
        self.assertEqual("application/octet-stream", created["mime_type"])
        self.assertEqual("report.ripdock-blob", request["payload"]["filename"])
        self.assertEqual("application/octet-stream", request["payload"]["mime_type"])
        self.assertEqual("message.completed", sent_messages[-1]["type"])

    def test_artifact_deliver_intent_rejects_file_over_size_limit(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_id = "runtime-1"
        adapter._generated_artifacts_by_id = {}
        adapter._generated_artifacts_by_key = {}
        adapter._artifact_ids_by_message_id = {}
        adapter._conversation_context_by_id = {}
        adapter._outbound_content_by_message_id = {}
        adapter._outbound_conversation_by_message_id = {}
        adapter._outbound_websocket_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter.transfers = {}
        adapter.embedded_public_url = "https://runtime.example.com"
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["RIPDOCK_MAX_ARTIFACT_BYTES"] = "3"
                path = Path(directory) / "oversized.anything"
                path.write_bytes(b"too large")
                content = json.dumps({
                    "runtime_intent": "ripdock.artifact.deliver",
                    "arguments": {"path": str(path), "description": "Too large"},
                    "visible_text": "Sending the file now.",
                })

                handled = asyncio.run(adapter._handle_runtime_intent_output(
                    object(),
                    "conversation-1",
                    "message-1",
                    content,
                    finalize=True,
                ))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertTrue(handled)
        self.assertEqual("message.delta", sent_messages[0]["type"])
        self.assertEqual("Sending the file now.", sent_messages[0]["delta"])
        self.assertEqual("message.delta", sent_messages[1]["type"])
        self.assertEqual("I couldn't validate that file for delivery.", sent_messages[1]["delta"])
        self.assertNotIn("runtime.artifact.created", [message["type"] for message in sent_messages])
        self.assertNotIn("runtime.transfer.request", [message["type"] for message in sent_messages])
        self.assertEqual("message.completed", sent_messages[-1]["type"])

    def test_invalid_artifact_deliver_intent_is_rejected_before_visible_text(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter._outbound_content_by_message_id = {}
        adapter._conversation_context_by_id = {}
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to

        content = json.dumps({
            "runtime_intent": "ripdock.artifact.deliver",
            "arguments": {},
            "visible_text": "Sending it now.",
        })
        handled = asyncio.run(adapter._handle_runtime_intent_output(
            object(),
            "conversation-1",
            "message-1",
            content,
            finalize=True,
        ))

        self.assertTrue(handled)
        self.assertEqual(1, len(sent_messages))
        self.assertEqual("error", sent_messages[0]["type"])
        self.assertEqual("runtime.intent_invalid", sent_messages[0]["code"])
        self.assertEqual("ripdock.artifact.deliver requires arguments.path.", sent_messages[0]["message"])
        self.assertNotIn("message-1", adapter._outbound_content_by_message_id)

    def test_resolve_and_deliver_intent_skips_fuzzy_query_search(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_id = "runtime-1"
        adapter._generated_artifacts_by_id = {}
        adapter._generated_artifacts_by_key = {}
        adapter._artifact_ids_by_message_id = {}
        adapter._conversation_context_by_id = {}
        adapter._outbound_content_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter.transfers = {}
        adapter.embedded_public_url = "https://runtime.example.com"
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                path = Path(directory) / "siamese_cat_generated.png"
                path.write_bytes(b"png")
                content = json.dumps({
                    "runtime_intent": "ripdock.artifact.resolve_and_deliver",
                    "arguments": {"query": "siamese cat image"},
                    "visible_text": "Sending it now.",
                })
                handled = asyncio.run(adapter._handle_runtime_intent_output(
                    object(),
                    "conversation-1",
                    "message-1",
                    content,
                    finalize=True,
                ))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertTrue(handled)
        self.assertNotIn("runtime.artifact.created", [message["type"] for message in sent_messages])
        self.assertNotIn("runtime.transfer.request", [message["type"] for message in sent_messages])
        self.assertEqual("message.delta", sent_messages[-2]["type"])
        self.assertIn("couldn't find", sent_messages[-2]["delta"])

    def test_plain_text_delivery_request_is_not_intercepted_before_model_dispatch(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"
        adapter._active_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._active_message_by_conversation = {}
        adapter._activity_state_by_conversation = {}
        adapter._active_user_text_by_conversation = {}
        adapter._suppressed_home_channel_notice_conversations = set()
        adapter.app_capabilities_by_session = {}
        dispatched = []

        async def fake_dispatch(_websocket, msg, agent_id):
            dispatched.append((msg["content"], agent_id))

        adapter._dispatch_ripdock_agent_message = fake_dispatch

        asyncio.run(adapter._handle_message_create(
            object(),
            {
                "type": "message.create",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "conversation_id": "conversation-1",
                "message_id": "request-1",
                "content": "send me the cat image",
            },
        ))

        self.assertEqual([("send me the cat image", "personal")], dispatched)

    def test_activity_report_intent_emits_activity_block(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.session_id = "session-1"
        adapter._raw_tool_details = {}
        adapter._activity_state_by_conversation = {}
        adapter._completed_message_ids = set()
        adapter.app_capabilities_by_session = {}
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to
        content = json.dumps({
            "runtime_intent": "ripdock.activity.report",
            "arguments": {
                "tool": "execute_code",
                "category": "command",
                "status": "completed",
                "summary": "Running Python script",
                "args": {"code": "print(123)"},
            },
        })

        asyncio.run(adapter._handle_runtime_intent_output(
            object(),
            "conversation-1",
            "message-1",
            content,
            finalize=True,
        ))

        self.assertEqual("message.block", sent_messages[0]["type"])
        self.assertEqual("activity.code.run", sent_messages[0]["block"]["kind"])
        block_content = json.loads(sent_messages[0]["block"]["content"])
        self.assertEqual("Running Python script", block_content["summary"])
        self.assertEqual("completed", block_content["status"])
        self.assertEqual("message.completed", sent_messages[1]["type"])
        self.assertEqual(2, len(sent_messages))

    def test_interleaved_duplicate_activity_reports_are_suppressed(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.session_id = "session-1"
        adapter._raw_tool_details = {}
        adapter._activity_state_by_conversation = {}
        adapter._completed_message_ids = set()
        adapter.app_capabilities_by_session = {}
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to
        first = {
            "tool_name": "skills_list",
            "category": "tool",
            "status": "running",
            "summary": "Loading skill",
            "detail_id": "detail-1",
            "raw_detail": "{}",
            "args": {},
        }
        second = {
            "tool_name": "terminal",
            "category": "command",
            "status": "running",
            "summary": "Running Python script",
            "detail_id": "detail-2",
            "raw_detail": "{}",
            "args": {},
        }

        for activity in (first, second, first, second):
            asyncio.run(adapter._emit_runtime_activity(
                object(),
                "conversation-1",
                "message-1",
                activity,
            ))

        self.assertEqual(["message.block", "message.block"], [message["type"] for message in sent_messages])
        self.assertEqual(
            ["activity.tool.progress", "activity.code.run"],
            [message["block"]["kind"] for message in sent_messages],
        )

    def test_malformed_runtime_intent_output_is_rejected_not_displayed(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to

        handled = asyncio.run(adapter._handle_runtime_intent_output(
            object(),
            "conversation-1",
            "message-1",
            '{"runtime_intent": "ripdock.artifact.deliver", "arguments": ',
            finalize=True,
        ))

        self.assertTrue(handled)
        self.assertEqual("error", sent_messages[0]["type"])
        self.assertEqual("runtime.intent_invalid", sent_messages[0]["code"])

    def test_partial_runtime_intent_output_is_suppressed_until_final(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to

        handled = asyncio.run(adapter._handle_runtime_intent_output(
            object(),
            "conversation-1",
            "message-1",
            '{"runtime_intent": "ripdock.artifact.deliver", "arguments": ',
            finalize=False,
        ))

        self.assertTrue(handled)
        self.assertEqual([], sent_messages)

    def test_pending_runtime_intent_json_fence_is_suppressed_for_artifact_request(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter._active_user_text_by_conversation = {
            "conversation-1": "Please send me the existing artifact named report.txt.",
        }
        adapter._active_generation_by_conversation = {}
        adapter._completed_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._outbound_content_by_message_id = {}
        adapter._outbound_conversation_by_message_id = {}
        adapter._outbound_websocket_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter._websocket_for_ripdock_send = lambda *args, **kwargs: object()
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to

        result = asyncio.run(adapter.send(chat_id="conversation-1", content="```json"))

        self.assertTrue(result.success)
        self.assertEqual([], sent_messages)

    def test_pending_runtime_intent_json_prefix_is_suppressed_for_artifact_request(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter._active_user_text_by_conversation = {
            "conversation-1": "yes, send it",
        }
        adapter._active_generation_by_conversation = {}
        adapter._completed_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._outbound_content_by_message_id = {}
        adapter._outbound_conversation_by_message_id = {}
        adapter._outbound_websocket_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter._websocket_for_ripdock_send = lambda *args, **kwargs: object()
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to

        result = asyncio.run(adapter.send(chat_id="conversation-1", content='{ "runtime'))

        self.assertTrue(result.success)
        self.assertEqual([], sent_messages)

    def test_split_runtime_intent_json_is_buffered_until_complete(self):
        adapter, sent_messages = self._runtime_output_adapter()
        adapter._active_user_text_by_conversation = {
            "conversation-1": "yes, send it",
        }
        first = asyncio.run(adapter.send(chat_id="conversation-1", content="{\n  "))
        self.assertTrue(first.success)
        self.assertEqual([], sent_messages)

        second = asyncio.run(adapter.edit_message(
            "conversation-1",
            first.message_id,
            '"runtime_intent": "ripdock.activity.report", "arguments": {"tool": "terminal"}}',
            finalize=True,
        ))

        self.assertTrue(second.success)
        self.assertEqual(["message.block", "message.block", "message.completed"], [message["type"] for message in sent_messages])
        self.assertEqual("activity.code.run", sent_messages[0]["block"]["kind"])
        block_contents = [
            json.loads(message["block"]["content"])
            for message in sent_messages
            if message["type"] == "message.block"
        ]
        self.assertEqual(["running", "completed"], [content["status"] for content in block_contents])
        self.assertEqual(block_contents[0]["detail_id"], block_contents[1]["detail_id"])

    def test_split_runtime_intent_buffer_releases_normal_json(self):
        adapter, sent_messages = self._runtime_output_adapter()
        adapter._active_user_text_by_conversation = {
            "conversation-1": "yes, send it",
        }
        first = asyncio.run(adapter.send(chat_id="conversation-1", content="{\n  "))
        self.assertTrue(first.success)
        self.assertEqual([], sent_messages)

        second = asyncio.run(adapter.edit_message(
            "conversation-1",
            first.message_id,
            '"status": "ok"}',
            finalize=False,
        ))

        self.assertTrue(second.success)
        self.assertEqual(["message.delta"], [message["type"] for message in sent_messages])
        self.assertEqual('{\n  "status": "ok"}', sent_messages[0]["delta"])

    def test_pending_runtime_intent_json_prefix_is_not_suppressed_for_normal_json_response(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter._active_user_text_by_conversation = {
            "conversation-1": "Show a minimal JSON example.",
        }

        self.assertFalse(adapter._is_pending_runtime_intent_fragment("conversation-1", '{ "runtime'))

    def test_pending_json_fence_is_not_suppressed_for_normal_json_response(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter._active_user_text_by_conversation = {
            "conversation-1": "Show a minimal JSON example.",
        }

        self.assertFalse(adapter._is_pending_runtime_intent_fence("conversation-1", "```json"))

    def test_runtime_intent_parser_rejects_prose_wrapped_json(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)

        self.assertIsNone(adapter._runtime_intent_from_content(
            'Here is the intent:\n{"runtime_intent": "ripdock.artifact.deliver", "arguments": {}}'
        ))


    def test_ripdock_channel_prompt_includes_runtime_model_contract(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.app_capabilities_by_session = {}
        adapter.session_id = "session-1"

        prompt = adapter._ripdock_channel_prompt("Personal", "personal")

        self.assertIn("RipDock Runtime Interface", prompt)
        self.assertIn("Active RipDock Agent: Personal (personal).", prompt)
        self.assertIn("Use only supported RipDock Rich Text v1 formatting", prompt)
        self.assertIn("Runtime Tool Intents", prompt)
        self.assertIn("ripdock.artifact.deliver", prompt)
        self.assertIn("ripdock.artifact.resolve_and_deliver", prompt)
        self.assertIn("Do not use this intent for fuzzy filename", prompt)
        self.assertIn("missing or empty `arguments.path`", prompt)
        self.assertIn("ripdock.activity.report", prompt)
        self.assertIn("Do not expose Runtime-local filesystem paths", prompt)
