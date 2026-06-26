from support import *


class RuntimeProtocolTests(PluginTestBase):
    def _streaming_adapter_fixture(self, adapter_module):
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter._active_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._completed_generation_by_conversation = {}
        adapter._active_message_by_conversation = {}
        adapter._activity_state_by_conversation = {}
        adapter._outbound_conversation_by_message_id = {}
        adapter._outbound_websocket_by_message_id = {}
        adapter._outbound_content_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter._ripdock_message_streams_by_message_id = {}
        adapter._artifact_ids_by_message_id = {}
        adapter._conversation_context_by_id = {}
        adapter._outbound_message_count_by_conversation = {}
        adapter._suppressed_home_channel_notice_conversations = set()
        adapter._running_activities_by_message_id = {}
        adapter._hermes_tool_progress_names = frozenset()
        adapter._raw_tool_details = {}
        adapter.app_capabilities_by_session = {
            "session-1": {
                "payload": {
                    "content_types": ["application/vnd.ripdock.activity+json"],
                    "features": {"semantic_blocks": True},
                }
            }
        }
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to
        adapter._websocket_for_ripdock_send = lambda *args, **kwargs: object()
        return adapter, sent_messages

    def test_ripdock_display_override_defaults_streaming_on(self):
        adapter_module = load_backend_adapter()
        display_config = types.ModuleType("gateway.display_config")
        display_config.resolve_display_setting = lambda *_args, **_kwargs: None
        gateway_module = types.ModuleType("gateway")
        old_gateway = sys.modules.get("gateway")
        old_display = sys.modules.get("gateway.display_config")
        try:
            sys.modules["gateway"] = gateway_module
            sys.modules["gateway.display_config"] = display_config
            adapter_module.RipDockAdapter._install_ripdock_display_override(None)

            self.assertIs(
                True,
                display_config.resolve_display_setting({"display": {"platforms": {}}}, "ripdock", "streaming"),
            )
        finally:
            if old_gateway is None:
                sys.modules.pop("gateway", None)
            else:
                sys.modules["gateway"] = old_gateway
            if old_display is None:
                sys.modules.pop("gateway.display_config", None)
            else:
                sys.modules["gateway.display_config"] = old_display

    def test_ripdock_display_override_respects_explicit_streaming_false(self):
        adapter_module = load_backend_adapter()
        display_config = types.ModuleType("gateway.display_config")
        display_config.resolve_display_setting = lambda *_args, **_kwargs: False
        gateway_module = types.ModuleType("gateway")
        old_gateway = sys.modules.get("gateway")
        old_display = sys.modules.get("gateway.display_config")
        try:
            sys.modules["gateway"] = gateway_module
            sys.modules["gateway.display_config"] = display_config
            adapter_module.RipDockAdapter._install_ripdock_display_override(None)

            user_config = {"display": {"platforms": {"ripdock": {"streaming": False}}}}
            self.assertIs(False, display_config.resolve_display_setting(user_config, "ripdock", "streaming"))
        finally:
            if old_gateway is None:
                sys.modules.pop("gateway", None)
            else:
                sys.modules["gateway"] = old_gateway
            if old_display is None:
                sys.modules.pop("gateway.display_config", None)
            else:
                sys.modules["gateway.display_config"] = old_display

    def test_message_mapper_classifies_single_fenced_block_as_code(self):
        adapter_module = load_backend_adapter()
        classification = adapter_module.classify_runtime_content("```python\nprint('hello')\n```")

        self.assertEqual("fenced_code", classification["kind"])
        self.assertEqual("python", classification["language"])
        self.assertTrue(classification["complete"])

    def test_message_stream_delta_uses_current_strict_shape(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter._outbound_conversation_by_message_id = {}
        adapter._outbound_websocket_by_message_id = {}
        adapter._outbound_content_by_message_id = {}
        adapter._conversation_context_by_id = {}
        adapter._completed_message_ids = set()
        adapter._ripdock_message_streams_by_message_id = {}
        adapter._artifact_ids_by_message_id = {}
        adapter._active_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to
        adapter._websocket_for_ripdock_send = lambda *args, **kwargs: object()
        stream = adapter_module.RipDockMessageStream(adapter, "conversation-1", "message-1", websocket=object())
        asyncio.run(stream.delta("hello"))

        event = sent_messages[0]
        self.assertEqual("message.delta", event["type"])
        self.assertEqual("1", event["protocol_version"])
        self.assertEqual("hello", event["delta"])
        self.assertNotIn("payload", event)

    def test_message_mapper_classifies_mixed_fenced_response_as_markdown(self):
        adapter_module = load_backend_adapter()
        content = "\n".join([
            "Sure, here are examples:",
            "",
            "**Python**",
            "```python",
            "print('hello')",
            "```",
            "",
            "**JavaScript**",
            "```javascript",
            "console.log('hello')",
            "```",
        ])

        classification = adapter_module.classify_runtime_content(content)

        self.assertEqual({"kind": "markdown", "complete": True}, classification)

    def test_message_mapper_classifies_prose_around_single_fence_as_markdown(self):
        adapter_module = load_backend_adapter()
        classification = adapter_module.classify_runtime_content("Example:\n```php\necho 'hello';\n```")

        self.assertEqual({"kind": "markdown", "complete": True}, classification)

    def test_completed_activity_emit_sends_activity_block_only(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.session_id = "session-1"
        adapter._activity_state_by_conversation = {}
        adapter._running_activities_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter._raw_tool_details = {}
        adapter.app_capabilities_by_session = {}
        sent_messages = []

        async def fake_send_json_to(_websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = fake_send_json_to
        activity = {
            "tool_name": "self_improvement",
            "category": "memory",
            "summary": "User profile updated.",
            "detail_id": "detail-1",
            "raw_detail": "User profile updated.",
            "args": {"summary": "User profile updated."},
            "status": "completed",
        }

        asyncio.run(adapter._emit_runtime_activity(object(), "conversation-1", "message-1", activity))

        self.assertEqual("message.block", sent_messages[0]["type"])
        self.assertEqual("activity.status", sent_messages[0]["block"]["kind"])
        self.assertEqual("completed", json.loads(sent_messages[0]["block"]["content"])["status"])
        self.assertEqual(1, len(sent_messages))
        self.assertNotIn("activity.tool.start", [message["type"] for message in sent_messages])

    def test_completed_activity_is_not_completed_again_on_stream_completion(self):
        adapter_module = load_backend_adapter()
        adapter, sent_messages = self._streaming_adapter_fixture(adapter_module)
        adapter._active_message_by_conversation["conversation-1"] = "active-message"
        adapter._outbound_conversation_by_message_id["active-message"] = "conversation-1"
        activity = {
            "tool_name": "self_improvement",
            "category": "memory",
            "summary": "User profile updated.",
            "detail_id": "detail-1",
            "raw_detail": "User profile updated.",
            "args": {"summary": "User profile updated."},
            "status": "completed",
        }

        asyncio.run(adapter._emit_runtime_activity(object(), "conversation-1", "message-1", activity))
        asyncio.run(adapter._complete_ripdock_conversation(object(), "conversation-1"))

        self.assertEqual(["message.block", "message.completed"], [message["type"] for message in sent_messages])
        self.assertEqual("completed", json.loads(sent_messages[0]["block"]["content"])["status"])

    def test_hermes_send_and_edit_reuse_active_message_stream(self):
        adapter_module = load_backend_adapter()
        adapter, sent_messages = self._streaming_adapter_fixture(adapter_module)
        adapter._active_message_by_conversation["conversation-1"] = "active-message"
        adapter._outbound_conversation_by_message_id["active-message"] = "conversation-1"

        asyncio.run(adapter.send(chat_id="conversation-1", content="Hello"))
        asyncio.run(adapter.edit_message("conversation-1", "active-message", "Hello world"))
        asyncio.run(adapter._complete_ripdock_conversation(object(), "conversation-1"))

        self.assertEqual(
            ["message.delta", "message.delta", "message.completed"],
            [message["type"] for message in sent_messages],
        )
        self.assertEqual(["active-message"], sorted({message["message_id"] for message in sent_messages}))
        self.assertEqual(["Hello", " world"], [message["delta"] for message in sent_messages if message["type"] == "message.delta"])

    def test_hermes_edit_after_completion_is_suppressed(self):
        adapter_module = load_backend_adapter()
        adapter, sent_messages = self._streaming_adapter_fixture(adapter_module)
        adapter._active_generation_by_conversation["conversation-1"] = 1
        adapter._active_message_by_conversation["conversation-1"] = "active-message"
        adapter._outbound_conversation_by_message_id["active-message"] = "conversation-1"

        asyncio.run(adapter.send(chat_id="conversation-1", content="Hello"))
        asyncio.run(adapter._complete_ripdock_conversation(object(), "conversation-1"))
        asyncio.run(adapter.edit_message("conversation-1", "active-message", "Hello late", finalize=True))

        self.assertEqual(["message.delta", "message.completed"], [message["type"] for message in sent_messages])

    def test_conversation_completion_skips_completed_generation(self):
        adapter_module = load_backend_adapter()
        adapter, sent_messages = self._streaming_adapter_fixture(adapter_module)
        adapter._active_generation_by_conversation["conversation-1"] = 1
        adapter._active_message_by_conversation["conversation-1"] = "active-message"
        adapter._outbound_conversation_by_message_id["active-message"] = "conversation-1"
        adapter._outbound_conversation_by_message_id["platform-message"] = "conversation-1"

        stream = adapter._ripdock_message_stream("conversation-1", "active-message", websocket=object())
        asyncio.run(stream.delta("Done"))
        asyncio.run(stream.complete())
        asyncio.run(adapter._complete_ripdock_conversation(object(), "conversation-1"))

        self.assertEqual(["message.delta", "message.completed"], [message["type"] for message in sent_messages])
        self.assertEqual(["active-message"], sorted({message["message_id"] for message in sent_messages}))

    def test_hermes_platform_finalize_does_not_complete_runtime_turn(self):
        adapter_module = load_backend_adapter()
        adapter, sent_messages = self._streaming_adapter_fixture(adapter_module)
        adapter._active_generation_by_conversation["conversation-1"] = 1
        adapter._active_message_by_conversation["conversation-1"] = "active-message"
        adapter._outbound_conversation_by_message_id["active-message"] = "conversation-1"

        asyncio.run(adapter.send(chat_id="conversation-1", content="Hello"))
        asyncio.run(adapter.edit_message("conversation-1", "platform-message-1", "Hello world", finalize=True))
        asyncio.run(adapter.send(chat_id="conversation-1", content=" again"))
        asyncio.run(adapter._complete_ripdock_conversation(object(), "conversation-1"))

        self.assertEqual(
            ["message.delta", "message.delta", "message.delta", "message.completed"],
            [message["type"] for message in sent_messages],
        )
        self.assertEqual(["active-message"], sorted({message["message_id"] for message in sent_messages}))
        self.assertEqual(
            ["Hello", " world", "again"],
            [message["delta"] for message in sent_messages if message["type"] == "message.delta"],
        )

    def test_hermes_platform_edit_uses_runtime_message_snapshot_state(self):
        adapter_module = load_backend_adapter()
        adapter, sent_messages = self._streaming_adapter_fixture(adapter_module)
        adapter._active_generation_by_conversation["conversation-1"] = 1
        adapter._active_message_by_conversation["conversation-1"] = "active-message"
        adapter._outbound_conversation_by_message_id["active-message"] = "conversation-1"

        asyncio.run(adapter.send(chat_id="conversation-1", content="Hello"))
        asyncio.run(adapter.edit_message("conversation-1", "platform-message-1", "Hello world"))
        asyncio.run(adapter.edit_message("conversation-1", "platform-message-1", "Hello world again"))

        self.assertEqual(
            ["Hello", " world", " again"],
            [message["delta"] for message in sent_messages if message["type"] == "message.delta"],
        )
        self.assertEqual(["active-message"], sorted({message["message_id"] for message in sent_messages}))

    def test_hermes_platform_edit_suppresses_already_emitted_snapshot(self):
        adapter_module = load_backend_adapter()
        adapter, sent_messages = self._streaming_adapter_fixture(adapter_module)
        adapter._active_generation_by_conversation["conversation-1"] = 1
        adapter._active_message_by_conversation["conversation-1"] = "active-message"
        adapter._outbound_conversation_by_message_id["active-message"] = "conversation-1"

        asyncio.run(adapter.send(chat_id="conversation-1", content="Hello"))
        asyncio.run(adapter.edit_message("conversation-1", "platform-message-1", "Hello world"))
        adapter._outbound_content_by_message_id["platform-message-1"] = "world"
        asyncio.run(adapter.edit_message("conversation-1", "platform-message-1", "Hello world", finalize=True))

        self.assertEqual(
            ["Hello", " world"],
            [message["delta"] for message in sent_messages if message["type"] == "message.delta"],
        )
        self.assertNotIn("message.completed", [message["type"] for message in sent_messages])

    def test_runtime_activity_uses_active_message_stream(self):
        adapter_module = load_backend_adapter()
        adapter, sent_messages = self._streaming_adapter_fixture(adapter_module)
        adapter._active_message_by_conversation["conversation-1"] = "active-message"
        adapter._outbound_conversation_by_message_id["active-message"] = "conversation-1"
        activity = {
            "tool_name": "read_file",
            "category": "file",
            "summary": "Reading files",
            "detail_id": "detail-1",
            "raw_detail": "read_file",
            "args": {"path": "README.md"},
            "status": "running",
        }

        asyncio.run(adapter._emit_runtime_activity(object(), "conversation-1", "hermes-progress-message", activity))
        asyncio.run(adapter._complete_ripdock_conversation(object(), "conversation-1"))

        self.assertEqual(["message.block", "message.block", "message.completed"], [message["type"] for message in sent_messages])
        self.assertEqual(["active-message"], sorted({message["message_id"] for message in sent_messages}))
        block_contents = [
            json.loads(message["block"]["content"])
            for message in sent_messages
            if message["type"] == "message.block"
        ]
        self.assertEqual(["running", "completed"], [content["status"] for content in block_contents])
        self.assertEqual(block_contents[0]["detail_id"], block_contents[1]["detail_id"])

    def test_home_channel_notice_filter_only_matches_first_exact_notice(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter._outbound_message_count_by_conversation = {}
        notice = (
            "📬 No home channel is set for Some Client. A home channel is where Hermes delivers "
            "cron job results and cross-platform messages.\n\nType /sethome to make this chat your "
            "home channel, or ignore to skip."
        )

        self.assertTrue(adapter._should_suppress_home_channel_notice("conversation-1", notice))
        self.assertFalse(adapter._should_suppress_home_channel_notice("conversation-1", notice + " There are 8 planets."))
        self.assertFalse(adapter._should_suppress_home_channel_notice("conversation-1", "No home channel is configured."))

        adapter._record_outbound_message_attempt("conversation-1")
        self.assertFalse(adapter._should_suppress_home_channel_notice("conversation-1", notice))

    def test_conversation_create_emits_receipt_and_persists_profile_session(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._new_runtime_conversation_id = lambda: "conversation-new"
        remembered = []

        async def fake_bootstrap(agent_id, conversation_id, profile):
            self.assertEqual("default", agent_id)
            self.assertEqual("conversation-new", conversation_id)
            self.assertEqual("default", profile)
            remembered.append((agent_id, conversation_id, profile))
            return "session-1"

        adapter._ensure_profile_session_id = fake_bootstrap
        websocket = self._fake_embedded_websocket([])

        asyncio.run(adapter._handle_conversation_create(
            websocket,
            {
                "type": "conversation.create",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "default",
                "client_message_id": "client-message-1",
            },
        ))

        self.assertEqual([("default", "conversation-new", "default")], remembered)
        self.assertEqual(["conversation.created"], [message["type"] for message in websocket.sent])
        self.assertEqual("conversation-new", websocket.sent[0]["conversation_id"])
        self.assertEqual("client-message-1", websocket.sent[0]["client_message_id"])
        self.assertNotIn("assistant_content", websocket.sent[0])
        self.assertNotIn("assistant_message_id", websocket.sent[0])

    def test_conversation_create_replays_idempotent_receipt(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._new_runtime_conversation_id = lambda: "conversation-new"
        calls = []

        async def fake_bootstrap(_agent_id, _conversation_id, _profile):
            calls.append("bootstrap")
            return "session-1"

        adapter._ensure_profile_session_id = fake_bootstrap
        websocket = self._fake_embedded_websocket([])
        message = {
            "type": "conversation.create",
            "protocol_version": "1",
            "runtime_id": "runtime-1",
            "agent_id": "default",
            "client_message_id": "client-message-1",
        }

        asyncio.run(adapter._handle_conversation_create(websocket, dict(message)))
        asyncio.run(adapter._handle_conversation_create(websocket, dict(message)))

        self.assertEqual(["bootstrap"], calls)
        self.assertEqual(["conversation.created", "conversation.created"], [event["type"] for event in websocket.sent])
        self.assertEqual(websocket.sent[0], websocket.sent[1])

    def test_runtime_agent_message_dispatch_uses_clean_user_content(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter._active_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._active_message_by_conversation = {}
        adapter._activity_state_by_conversation = {}
        adapter._outbound_conversation_by_message_id = {}
        adapter._outbound_websocket_by_message_id = {}
        adapter._outbound_content_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter._outbound_message_count_by_conversation = {}
        adapter._suppressed_home_channel_notice_conversations = set()
        adapter._generated_artifacts_by_key = {}
        adapter._generated_artifacts_by_id = {}
        adapter._artifact_ids_by_message_id = {}
        adapter._conversation_context_by_id = {}
        adapter.app_capabilities_by_session = {}
        calls = []
        sent_messages = []

        class FakeHermesRuntime:
            async def sendMessage(self, websocket, message):
                calls.append((message["agent_id"], message["content"], message["conversation_id"]))
                await adapter.send(
                    chat_id=message["conversation_id"],
                    content=f"reply {len(calls)}",
                    metadata={"ripdock_websocket": websocket},
                )
                await adapter._complete_ripdock_conversation(websocket, message["conversation_id"])

        async def fake_send_json_to(_self, _websocket, message):
            sent_messages.append(message)

        adapter.hermes_runtime = FakeHermesRuntime()
        adapter._send_json_to = types.MethodType(fake_send_json_to, adapter)

        message = {
            "type": "message.create",
            "runtime_id": "runtime-1",
            "agent_id": "joker",
            "conversation_id": "conversation-1",
            "content": "what did I say earlier?",
        }

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                websocket = object()
                asyncio.run(adapter._handle_message_create(websocket, message))
                second = dict(message)
                second["content"] = "what was my previous message?"
                asyncio.run(adapter._handle_message_create(websocket, second))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(
            [
                ("joker", "what did I say earlier?", "conversation-1"),
                ("joker", "what was my previous message?", "conversation-1"),
            ],
            calls,
        )
        self.assertEqual(["reply 1", "reply 2"], [message["delta"] for message in sent_messages if message["type"] == "message.delta"])

    def test_runtime_agent_response_uses_current_device_websocket_after_reconnect(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter._active_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._active_message_by_conversation = {}
        adapter._activity_state_by_conversation = {}
        adapter._outbound_conversation_by_message_id = {}
        adapter._outbound_websocket_by_message_id = {}
        adapter._outbound_content_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter._outbound_message_count_by_conversation = {}
        adapter._suppressed_home_channel_notice_conversations = set()
        adapter._generated_artifacts_by_key = {}
        adapter._generated_artifacts_by_id = {}
        adapter._artifact_ids_by_message_id = {}
        adapter._conversation_context_by_id = {}
        adapter.app_capabilities_by_session = {}
        adapter.authenticated_app_device_by_websocket = {}

        class FakeWebSocket:
            def __init__(self, name):
                self.name = name
                self.sent = []
                self.closed = False

        first = FakeWebSocket("first")
        replacement = FakeWebSocket("replacement")
        adapter.authenticated_app_device_by_websocket[first] = "device-1"
        adapter._last_ripdock_websocket = replacement

        class FakeHermesRuntime:
            async def sendMessage(self, websocket, message):
                first.closed = True
                adapter.authenticated_app_device_by_websocket.pop(first, None)
                adapter.authenticated_app_device_by_websocket[replacement] = "device-1"
                adapter._last_ripdock_websocket = replacement
                await adapter.send(
                    chat_id=message["conversation_id"],
                    content="response for replacement websocket",
                    metadata={"ripdock_websocket": websocket},
                )
                await adapter._complete_ripdock_conversation(websocket, message["conversation_id"])

        async def fake_send_json_to(_self, websocket, message):
            websocket.sent.append(message)

        adapter.hermes_runtime = FakeHermesRuntime()
        adapter._send_json_to = types.MethodType(fake_send_json_to, adapter)

        asyncio.run(adapter._handle_message_create(
            first,
            {
                "type": "message.create",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "joker",
                "conversation_id": "conversation-1",
                "client_message_id": "client-message-1",
                "content": "hello",
            },
        ))

        self.assertEqual([], first.sent)
        self.assertEqual(["message.delta", "message.completed"], [message["type"] for message in replacement.sent])

    def test_runtime_agent_response_is_persisted_when_device_socket_is_missing(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter._active_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._active_message_by_conversation = {}
        adapter._activity_state_by_conversation = {}
        adapter._outbound_conversation_by_message_id = {}
        adapter._outbound_websocket_by_message_id = {}
        adapter._outbound_app_device_by_message_id = {}
        adapter._outbound_content_by_message_id = {}
        adapter._completed_message_ids = set()
        adapter._outbound_message_count_by_conversation = {}
        adapter._suppressed_home_channel_notice_conversations = set()
        adapter._generated_artifacts_by_key = {}
        adapter._generated_artifacts_by_id = {}
        adapter._artifact_ids_by_message_id = {}
        adapter._conversation_context_by_id = {}
        adapter.app_capabilities_by_session = {}
        adapter.authenticated_app_device_by_websocket = {}

        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.closed = False

        first = FakeWebSocket()
        adapter.authenticated_app_device_by_websocket[first] = "device-1"

        class FakeHermesRuntime:
            async def sendMessage(self, websocket, message):
                first.closed = True
                adapter.authenticated_app_device_by_websocket.pop(first, None)
                await adapter.send(
                    chat_id=message["conversation_id"],
                    content="response persisted for sync",
                    metadata={"ripdock_websocket": websocket},
                )

        async def fake_send_json_to(_self, websocket, message):
            websocket.sent.append(message)

        adapter.hermes_runtime = FakeHermesRuntime()
        adapter._send_json_to = types.MethodType(fake_send_json_to, adapter)

        asyncio.run(adapter._handle_message_create(
            first,
            {
                "type": "message.create",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "joker",
                "conversation_id": "conversation-1",
                "client_message_id": "client-message-1",
                "content": "hello",
            },
        ))

        self.assertEqual([], first.sent)
        self.assertIn("response persisted for sync", adapter._outbound_content_by_message_id.values())

    def test_runtime_agent_hidden_instructions_are_not_user_content(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter.app_capabilities_by_session = {}

        user_content = "send me that file"
        channel_prompt = adapter._ripdock_channel_prompt("Joker", "joker")

        self.assertEqual("send me that file", user_content)
        self.assertIn("RipDock Runtime Interface", channel_prompt)
        self.assertIn("Active RipDock Agent: Joker (joker).", channel_prompt)
        self.assertIn("Runtime Tool Intents", channel_prompt)
        self.assertNotIn("send me that file", channel_prompt)

    def test_qa_content_command_emits_all_supported_content_blocks_without_model_dispatch(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.session_id = "session-1"
        adapter._active_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._active_message_by_conversation = {}
        adapter._activity_state_by_conversation = {}
        adapter.app_capabilities_by_session = {
            "session-1": {
                "payload": {
                    "content_types": [
                        "text/plain",
                        "text/markdown",
                        "text/code",
                        "text/log",
                        "application/json",
                        "application/yaml",
                        "application/vnd.ripdock.activity+json",
                        "application/vnd.ripdock.artifact+json",
                    ],
                    "features": {"semantic_blocks": True},
                }
            }
        }
        sent_messages = []
        profile_called = False

        async def fake_profile_dispatch(_websocket, _msg, _agent_id):
            nonlocal profile_called
            profile_called = True

        async def fake_send_json_to(_self, _websocket, message):
            sent_messages.append(message)

        adapter._dispatch_ripdock_agent_message = fake_profile_dispatch
        adapter._send_json_to = types.MethodType(fake_send_json_to, adapter)

        old_env = os.environ.copy()
        try:
            os.environ["RIPDOCK_DEV_COMMANDS"] = "true"
            asyncio.run(adapter._handle_message_create(
                object(),
                {
                    "type": "message.create",
                    "conversation_id": "conversation-1",
                    "message_id": "request-1",
                    "content": "/qa_content",
                },
            ))
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertFalse(profile_called)
        self.assertEqual("message.delta", sent_messages[0]["type"])
        self.assertIn("Rich Text v1", sent_messages[0]["delta"])
        block_messages = [message for message in sent_messages if message["type"] == "message.block"]
        self.assertEqual(
            [
                ("text", "text/plain"),
                ("markdown", "text/markdown"),
                ("code", "text/code"),
                ("log", "text/log"),
                ("data", "application/json"),
                ("data", "application/yaml"),
                ("activity.status", "application/vnd.ripdock.activity+json"),
                ("activity.tool.progress", "application/vnd.ripdock.activity+json"),
                ("artifact.reference", "application/vnd.ripdock.artifact+json"),
            ],
            [(message["block"]["kind"], message["block"]["mime_type"]) for message in block_messages],
        )
        self.assertTrue(all("block" in message and "payload" not in message for message in block_messages))
        self.assertEqual("message.completed", sent_messages[-1]["type"])

    def test_qa_content_command_is_dev_only(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)

        old_env = os.environ.copy()
        try:
            os.environ["RIPDOCK_DEV_COMMANDS"] = "false"
            os.environ["RIPDOCK_LOG_LEVEL"] = "debug"
            self.assertFalse(adapter._is_ripdock_qa_content_command("/qa_content"))
            os.environ["RIPDOCK_DEV_COMMANDS"] = "true"
            self.assertTrue(adapter._is_ripdock_qa_content_command("/qa_content"))
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_qa_transfer_failures_command_emits_runtime_transfer_failures(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter._active_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._active_message_by_conversation = {}
        adapter._activity_state_by_conversation = {}
        adapter.app_capabilities_by_session = {}
        adapter.transfers = {}
        sent_messages = []
        profile_called = False

        async def fake_profile_dispatch(_websocket, _msg, _agent_id):
            nonlocal profile_called
            profile_called = True

        async def fake_send_json_to(_self, _websocket, message):
            sent_messages.append(message)

        adapter._dispatch_ripdock_agent_message = fake_profile_dispatch
        adapter._send_json_to = types.MethodType(fake_send_json_to, adapter)

        old_env = os.environ.copy()
        with tempfile.TemporaryDirectory() as transfer_dir:
            try:
                os.environ["RIPDOCK_DEV_COMMANDS"] = "true"
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL"] = "https://runtime.example.com"
                os.environ["RIPDOCK_TRANSFER_DIR"] = transfer_dir
                asyncio.run(adapter._handle_message_create(
                    object(),
                    {
                        "type": "message.create",
                        "conversation_id": "conversation-1",
                        "message_id": "request-1",
                        "content": "/qa_transfer_failures",
                    },
                ))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertFalse(profile_called)
        self.assertEqual("message.delta", sent_messages[0]["type"])
        created = [message for message in sent_messages if message["type"] == "runtime.artifact.created"]
        requests = [message for message in sent_messages if message["type"] == "runtime.transfer.request"]
        failed = [message for message in sent_messages if message["type"] == "runtime.transfer.failed"]
        self.assertEqual(5, len(created))
        self.assertEqual(4, len(requests))
        self.assertEqual(1, len(failed))
        self.assertEqual("runtime-reject.txt", created[1]["filename"])
        self.assertNotIn("transfer_id", created[1])
        self.assertNotIn("download_url", created[1])
        self.assertEqual("qa-transfer-runtime-reject", failed[0]["payload"]["artifact_id"])
        self.assertIn("/invalid-transfer/", created[0]["download_url"])
        hash_request = next(message for message in requests if message["payload"]["filename"] == "hash-mismatch.txt")
        self.assertEqual("0" * 64, hash_request["payload"]["sha256"])
        self.assertEqual("message.completed", sent_messages[-1]["type"])

    def test_qa_transfer_failure_command_emits_single_variant(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"
        adapter.session_id = "session-1"
        adapter._active_generation_by_conversation = {}
        adapter._interrupted_generation_by_conversation = {}
        adapter._active_message_by_conversation = {}
        adapter._activity_state_by_conversation = {}
        adapter.app_capabilities_by_session = {}
        adapter.transfers = {}
        sent_messages = []

        async def fake_send_json_to(_self, _websocket, message):
            sent_messages.append(message)

        adapter._dispatch_ripdock_agent_message = lambda *_args: None
        adapter._send_json_to = types.MethodType(fake_send_json_to, adapter)

        old_env = os.environ.copy()
        with tempfile.TemporaryDirectory() as transfer_dir:
            try:
                os.environ["RIPDOCK_DEV_COMMANDS"] = "true"
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL"] = "https://runtime.example.com"
                os.environ["RIPDOCK_TRANSFER_DIR"] = transfer_dir
                asyncio.run(adapter._handle_message_create(
                    object(),
                    {
                        "type": "message.create",
                        "conversation_id": "conversation-1",
                        "message_id": "request-1",
                        "content": "/qa_transfer_failure hash-mismatch",
                    },
                ))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        created = [message for message in sent_messages if message["type"] == "runtime.artifact.created"]
        requests = [message for message in sent_messages if message["type"] == "runtime.transfer.request"]
        self.assertEqual(["hash-mismatch.txt"], [message["filename"] for message in created])
        self.assertEqual(["hash-mismatch.txt"], [message["payload"]["filename"] for message in requests])
        self.assertEqual(["qa-transfer-hash-mismatch"], sent_messages[-1]["artifact_ids"])

    def test_help_text_uses_advertised_runtime_slash_command_catalog(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)

        help_text = adapter._ripdock_help_text()

        self.assertTrue(help_text.startswith("# RipDock Help\n\nSupported commands:"))
        for command in adapter_module.RIPDOCK_ADVERTISED_SLASH_COMMANDS:
            self.assertIn(command["display"], help_text)
            self.assertIn(command["description"], help_text)

    def test_runtime_slash_commands_are_curated_for_app_surface(self):
        adapter_module = load_backend_adapter()
        adapter, _message = self._signed_resume_fixture()
        slash_commands = adapter._runtime_slash_commands()

        self.assertEqual("runtime.slash_commands", slash_commands["type"])
        self.assertEqual("runtime-1", slash_commands["runtime_id"])
        names = [command["name"] for command in slash_commands["commands"]]
        self.assertIn("cron", names)
        self.assertIn("status", names)
        self.assertEqual(
            {command["name"] for command in adapter_module.RIPDOCK_ADVERTISED_SLASH_COMMANDS},
            set(names),
        )

    def test_runtime_metadata_emits_slash_commands_after_agents(self):
        adapter, _message = self._signed_resume_fixture()
        websocket = self._fake_embedded_websocket([])

        asyncio.run(adapter._emit_runtime_metadata_to(websocket))

        self.assertEqual(
            [
                "runtime.identity",
                "runtime.capabilities",
                "runtime.agents",
                "runtime.slash_commands",
                "runtime.settings",
            ],
            [message["type"] for message in websocket.sent],
        )

    def test_advertised_runtime_slash_command_dispatches_without_model_prompt(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._profile_session_id = lambda _agent_id, _conversation_id: "session-1"
        dispatched = []

        async def fake_slash_dispatch(_websocket, msg, agent_id):
            dispatched.append((msg["content"], agent_id))

        adapter._dispatch_hermes_profile_slash_command = fake_slash_dispatch
        websocket = self._fake_embedded_websocket([])

        asyncio.run(adapter._handle_message_create(
            websocket,
            {
                "type": "message.create",
                "runtime_id": "runtime-1",
                "agent_id": "default",
                "conversation_id": "conversation-1",
                "content": "/status",
            },
        ))

        self.assertEqual([("/status", "default")], dispatched)

    def test_slash_command_bootstraps_missing_profile_session(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._profile_session_id = lambda _agent_id, _conversation_id: ""
        remembered = []

        async def fake_chat(_profile, content, session_id=None):
            self.assertEqual("", content)
            self.assertIsNone(session_id)
            return {
                "returncode": 0,
                "stdout": "Session: bootstrapped-session\n",
                "stderr": "",
            }

        async def fake_slash(_profile, content, session_id):
            self.assertEqual("/status", content)
            self.assertEqual("bootstrapped-session", session_id)
            return {
                "returncode": 0,
                "stdout": "\x1b[32mHermes CLI Status\x1b[0m",
                "stderr": "",
            }

        adapter._run_hermes_profile_chat = fake_chat
        adapter._run_hermes_profile_slash_command = fake_slash
        adapter._remember_profile_session_id = lambda *args: remembered.append(args)
        websocket = self._fake_embedded_websocket([])

        asyncio.run(adapter._dispatch_hermes_profile_slash_command(
            websocket,
            {
                "type": "message.create",
                "runtime_id": "runtime-1",
                "agent_id": "default",
                "conversation_id": "conversation-1",
                "content": "/status",
            },
            "default",
        ))

        self.assertEqual("bootstrapped-session", remembered[0][3])
        self.assertEqual("message.delta", websocket.sent[0]["type"])
        self.assertEqual("Hermes CLI Status", websocket.sent[0]["delta"])
        self.assertEqual("message.completed", websocket.sent[1]["type"])

    def test_slash_command_unknown_worker_output_fails(self):
        adapter, _message = self._signed_resume_fixture()

        async def fake_ensure(_agent_id, _conversation_id, _profile):
            return "session-1"

        async def fake_slash(_profile, _content, _session_id):
            return {
                "returncode": 0,
                "stdout": "\x1b[1;31mUnknown command: /approve\x1b[0m\nType /help for available commands",
                "stderr": "",
            }

        adapter._ensure_profile_session_id = fake_ensure
        adapter._run_hermes_profile_slash_command = fake_slash
        websocket = self._fake_embedded_websocket([])

        asyncio.run(adapter._dispatch_hermes_profile_slash_command(
            websocket,
            {
                "type": "message.create",
                "runtime_id": "runtime-1",
                "agent_id": "default",
                "conversation_id": "conversation-1",
                "content": "/approve",
            },
            "default",
        ))

        self.assertEqual("error", websocket.sent[0]["type"])
        self.assertEqual("runtime.unavailable", websocket.sent[0]["code"])

    def test_slash_command_empty_output_sends_fallback_text(self):
        adapter, _message = self._signed_resume_fixture()

        async def fake_ensure(_agent_id, _conversation_id, _profile):
            return "session-1"

        async def fake_slash(_profile, _content, _session_id):
            return {"returncode": 0, "stdout": "", "stderr": ""}

        adapter._ensure_profile_session_id = fake_ensure
        adapter._run_hermes_profile_slash_command = fake_slash
        websocket = self._fake_embedded_websocket([])

        asyncio.run(adapter._dispatch_hermes_profile_slash_command(
            websocket,
            {
                "type": "message.create",
                "runtime_id": "runtime-1",
                "agent_id": "default",
                "conversation_id": "conversation-1",
                "content": "/model",
            },
            "default",
        ))

        self.assertEqual("message.delta", websocket.sent[0]["type"])
        self.assertIn("No model change requested", websocket.sent[0]["delta"])
