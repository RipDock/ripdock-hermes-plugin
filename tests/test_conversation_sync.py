from support import *


class ConversationSyncTests(PluginTestBase):
    def test_conversation_sync_reads_injected_gateway_session_db_after_cursor(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                state = {
                    adapter._profile_session_key("personal", "conversation-1"): {
                        "session_id": "session-1",
                    }
                }
                profile_state_path = adapter._profile_session_state_file_path()
                profile_state_path.parent.mkdir(parents=True, exist_ok=True)
                profile_state_path.write_text(json.dumps(state))
                calls = []

                class FakeSessionDB:
                    def get_messages(self, session_id):
                        calls.append(session_id)
                        return [
                            {"id": 1, "session_id": "session-1", "role": "user", "content": "before", "timestamp": 1000.0},
                            {"id": 2, "session_id": "session-1", "role": "user", "content": "boundary", "timestamp": 1001.25},
                            {"id": 3, "session_id": "session-1", "role": "tool", "content": "tool output", "timestamp": 1001.26},
                            {"id": 4, "session_id": "session-1", "role": "assistant", "content": "", "timestamp": 1001.27},
                            {"id": 5, "session_id": "session-1", "role": "assistant", "content": "after", "timestamp": 1002.5},
                        ]

                adapter.gateway_runner = types.SimpleNamespace(_session_db=FakeSessionDB())

                messages = adapter._conversation_sync_messages("personal", "conversation-1", 1001.25)
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(
            [
                {
                    "message_id": "hermes:session-1:2",
                    "role": "user",
                    "content": "boundary",
                    "epoch": 1001.25,
                },
                {
                    "message_id": "hermes:session-1:5",
                    "role": "assistant",
                    "content": "after",
                    "epoch": 1002.5,
                },
            ],
            messages,
        )
        self.assertEqual(["session-1"], calls)

    def test_conversation_sync_filters_internal_system_notes_from_session_db(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                state = {
                    adapter._profile_session_key("personal", "conversation-1"): {
                        "session_id": "session-1",
                    }
                }
                profile_state_path = adapter._profile_session_state_file_path()
                profile_state_path.parent.mkdir(parents=True, exist_ok=True)
                profile_state_path.write_text(json.dumps(state))

                class FakeSessionDB:
                    def get_messages(self, _session_id):
                        return [
                            {
                                "id": 1,
                                "session_id": "session-1",
                                "role": "user",
                                "content": "[System note: hidden gateway recovery instruction]",
                                "timestamp": 1001.0,
                            },
                            {
                                "id": 2,
                                "session_id": "session-1",
                                "role": "assistant",
                                "content": "visible reply",
                                "timestamp": 1002.0,
                            },
                        ]

                adapter.gateway_runner = types.SimpleNamespace(_session_db=FakeSessionDB())

                messages = adapter._conversation_sync_messages("personal", "conversation-1", 0)
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(
            [
                {
                    "message_id": "hermes:session-1:2",
                    "role": "assistant",
                    "content": "visible reply",
                    "epoch": 1002.0,
                },
            ],
            messages,
        )

    def test_conversation_sync_does_not_create_session_db_without_gateway_runner(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"
        adapter.gateway_runner = None

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                state = {
                    adapter._profile_session_key("personal", "conversation-1"): {
                        "session_id": "session-1",
                    }
                }
                profile_state_path = adapter._profile_session_state_file_path()
                profile_state_path.parent.mkdir(parents=True, exist_ok=True)
                profile_state_path.write_text(json.dumps(state))

                messages = adapter._conversation_sync_messages("personal", "conversation-1", 0)
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual([], messages)

    def test_existing_conversation_without_profile_session_does_not_create_gateway_session(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_id = "runtime-1"
        adapter.config = types.SimpleNamespace(extra={})

        class Store:
            def __init__(self):
                self.created = []

            def get_or_create_session(self, source):
                self.created.append(source)
                return None

        store = Store()
        adapter._session_store = store

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                resumed, reason = adapter._force_gateway_session_resume("personal", "conversation-1", object())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertFalse(resumed)
        self.assertEqual("missing_profile_session", reason)
        self.assertEqual([], store.created)

    def test_new_conversation_remembers_gateway_session_from_state_file_when_store_misses(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_id = "runtime-1"
        adapter.config = types.SimpleNamespace(extra={})
        adapter._session_store = None

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                sessions_dir = Path(directory) / "sessions"
                sessions_dir.mkdir(parents=True)
                (sessions_dir / "sessions.json").write_text(json.dumps({
                    "session-key": {"session_id": "gateway-session-from-file"}
                }))

                session_id = adapter._remember_gateway_profile_session_id("personal", "conversation-1", "default", object())
                remembered = adapter._profile_session_id("personal", "conversation-1")
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("gateway-session-from-file", session_id)
        self.assertEqual("gateway-session-from-file", remembered)

    def test_conversation_list_reads_profile_sessions_and_injected_gateway_session_db_summaries(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_provider = "hermes"
        adapter.runtime_id = "runtime-1"

        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                state = {
                    adapter._profile_session_key("personal", "conversation-1"): {
                        "protocol_version": "1",
                        "runtime_id": "runtime-1",
                        "agent_id": "personal",
                        "conversation_id": "conversation-1",
                        "session_id": "session-1",
                        "updated_at": "1970-01-01T00:01:40Z",
                    },
                    adapter._profile_session_key("personal", "conversation-2"): {
                        "protocol_version": "1",
                        "runtime_id": "runtime-1",
                        "agent_id": "personal",
                        "conversation_id": "conversation-2",
                        "session_id": "session-2",
                        "updated_at": "1970-01-01T00:00:50Z",
                    },
                    adapter._profile_session_key("other", "conversation-3"): {
                        "protocol_version": "1",
                        "runtime_id": "runtime-1",
                        "agent_id": "other",
                        "conversation_id": "conversation-3",
                        "session_id": "session-3",
                    },
                }
                profile_state_path = adapter._profile_session_state_file_path()
                profile_state_path.parent.mkdir(parents=True, exist_ok=True)
                profile_state_path.write_text(json.dumps(state))
                adapter._conversation_title_state_file_path().write_text(json.dumps({
                    adapter._conversation_title_key("personal", "conversation-1"): {
                        "protocol_version": "1",
                        "runtime_id": "runtime-1",
                        "agent_id": "personal",
                        "conversation_id": "conversation-1",
                        "title": "Cached Runtime Title",
                        "updated_at": "1970-01-01T00:02:00Z",
                    }
                }))

                calls = []

                class FakeSessionDB:
                    def get_messages(self, session_id):
                        calls.append(session_id)
                        return {
                            "session-1": [
                                {"id": 0, "session_id": "session-1", "role": "user", "content": "[System note: hidden gateway recovery instruction]", "timestamp": 1000.0},
                                {"id": 1, "session_id": "session-1", "role": "user", "content": "hello from one", "timestamp": 1001.0},
                                {"id": 2, "session_id": "session-1", "role": "assistant", "content": "reply one", "timestamp": 1002.5},
                            ],
                            "session-2": [
                                {"id": 3, "session_id": "session-2", "role": "assistant", "content": "reply two", "timestamp": 1000.0},
                            ],
                            "session-3": [
                                {"id": 4, "session_id": "session-3", "role": "user", "content": "wrong agent", "timestamp": 1003.0},
                            ],
                        }.get(session_id, [])

                adapter.gateway_runner = types.SimpleNamespace(_session_db=FakeSessionDB())

                summaries = adapter._conversation_list_summaries("personal")
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(["conversation-1", "conversation-2"], [item["conversation_id"] for item in summaries])
        self.assertEqual("Cached Runtime Title", summaries[0]["title"])
        self.assertEqual("hello from one", summaries[0]["preview"])
        self.assertEqual("1970-01-01T00:16:41Z", summaries[0]["created_at"])
        self.assertEqual("1970-01-01T00:16:42.5Z", summaries[0]["updated_at"])
        self.assertEqual(2, summaries[0]["message_count"])
        self.assertEqual("reply two", summaries[1]["title"])
        self.assertEqual(["session-1", "session-2"], calls)

    def test_conversation_list_event_returns_summaries(self):
        adapter, _message = self._signed_resume_fixture()
        adapter.runtime_provider = "hermes"
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        adapter._conversation_list_summaries = lambda _agent_id: [
            {
                "conversation_id": "conversation-1",
                "title": "Seeded",
                "updated_at": "1970-01-01T00:16:42.5Z",
                "message_count": 2,
                "preview": "hello",
            }
        ]
        websocket = self._fake_embedded_websocket([
            json.dumps({
                "type": "conversation.list",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
            })
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"conversation:list"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(1, len(websocket.sent))
        event = websocket.sent[0]
        self.assertEqual("conversation.listed", event["type"])
        self.assertEqual("runtime-1", event["runtime_id"])
        self.assertEqual("personal", event["agent_id"])
        self.assertEqual(
            [
                {
                    "conversation_id": "conversation-1",
                    "title": "Seeded",
                    "updated_at": "1970-01-01T00:16:42.5Z",
                    "message_count": 2,
                    "preview": "hello",
                }
            ],
            event["conversations"],
        )

    def test_conversation_sync_event_returns_ordered_messages_and_cursor(self):
        adapter, _message = self._signed_resume_fixture()
        adapter.runtime_provider = "hermes"
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        adapter._conversation_sync_messages = lambda _agent_id, _conversation_id, _after_epoch: [
            {
                "message_id": "hermes:session-1:2",
                "role": "user",
                "content": "hello",
                "epoch": 1001.25,
            },
            {
                "message_id": "hermes:session-1:3",
                "role": "assistant",
                "content": "hi",
                "epoch": 1002.5,
            },
        ]
        websocket = self._fake_embedded_websocket([
            json.dumps({
                "type": "conversation.sync",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "conversation_id": "conversation-1",
                "after": "1970-01-01T00:16:41.25Z",
            })
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"conversation:sync"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(1, len(websocket.sent))
        event = websocket.sent[0]
        self.assertEqual("conversation.synced", event["type"])
        self.assertEqual("runtime-1", event["runtime_id"])
        self.assertEqual("personal", event["agent_id"])
        self.assertEqual("conversation-1", event["conversation_id"])
        self.assertEqual("1970-01-01T00:16:41.25Z", event["after"])
        self.assertEqual("1970-01-01T00:16:42.5Z", event["cursor"])
        self.assertEqual(
            [
                {
                    "message_id": "hermes:session-1:2",
                    "role": "user",
                    "content": "hello",
                    "created_at": "1970-01-01T00:16:41.25Z",
                },
                {
                    "message_id": "hermes:session-1:3",
                    "role": "assistant",
                    "content": "hi",
                    "created_at": "1970-01-01T00:16:42.5Z",
                },
            ],
            event["messages"],
        )

    def test_conversation_sync_requires_valid_timestamp(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        adapter._handle_conversation_sync = lambda _websocket, _message: self.fail("conversation.sync handler reached")
        websocket = self._fake_embedded_websocket([
            json.dumps({
                "type": "conversation.sync",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "conversation_id": "conversation-1",
                "after": "yesterday",
            })
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"conversation:sync"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual("protocol.invalid_payload", websocket.sent[0]["code"])

    def test_conversation_title_generate_persists_runtime_owned_title(self):
        adapter, _message = self._signed_resume_fixture()
        adapter.runtime_provider = "hermes"
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        adapter._schedule_message_create = lambda _websocket, _message: self.fail("message.create handler reached")
        adapter._remember_profile_session_id = lambda *_args, **_kwargs: self.fail("profile session was persisted")
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                websocket = self._fake_embedded_websocket([
                    json.dumps({
                        "type": "conversation.title.generate",
                        "protocol_version": "1",
                        "runtime_id": "runtime-1",
                        "agent_id": "personal",
                        "conversation_id": "conversation-1",
                        "messages": [
                            {"role": "user", "content": "[System note: hidden gateway recovery instruction]"},
                            {"role": "user", "content": "plan a weekend trip to Tokyo with museums and ramen"},
                            {"role": "assistant", "content": "Here is a concise itinerary."},
                        ],
                    })
                ])
                adapter.authenticated_app_websockets.add(websocket)
                adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
                adapter.authenticated_app_scopes_by_websocket[websocket] = {"conversation:title:generate"}

                asyncio.run(adapter._embedded_app_loop(websocket))
                title_state = adapter._load_conversation_title_state()
                profile_state = adapter._load_profile_session_state()
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(1, len(websocket.sent))
        event = websocket.sent[0]
        self.assertEqual("conversation.title.generated", event["type"])
        self.assertEqual("runtime-1", event["runtime_id"])
        self.assertEqual("personal", event["agent_id"])
        self.assertEqual("conversation-1", event["conversation_id"])
        self.assertEqual("plan a weekend trip to Tokyo with museums and ramen", event["title"])
        self.assertNotIn("conversation-1", profile_state)
        self.assertEqual(
            "plan a weekend trip to Tokyo with museums and ramen",
            title_state[adapter._conversation_title_key("personal", "conversation-1")]["title"],
        )

    def test_forgetting_profile_session_removes_cached_conversation_title(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.runtime_id = "runtime-1"
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            try:
                os.environ["HERMES_HOME"] = directory
                adapter._remember_conversation_title("personal", "conversation-1", "Cached")
                adapter._remember_profile_session_id("personal", "conversation-1", "default", "session-1")

                adapter._forget_profile_session_id("personal", "conversation-1")

                self.assertEqual({}, adapter._load_conversation_title_state())
                self.assertEqual({}, adapter._load_profile_session_state())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

    def test_conversation_title_generate_requires_valid_messages(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        adapter._handle_conversation_title_generate = lambda _websocket, _message: self.fail("conversation.title.generate handler reached")
        messages = [
            {
                "type": "conversation.title.generate",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "conversation_id": "conversation-1",
                "messages": [],
            },
            {
                "type": "conversation.title.generate",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "conversation_id": "conversation-1",
                "messages": [{"role": "system", "content": "hello"}],
            },
            {
                "type": "conversation.title.generate",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "conversation_id": "conversation-1",
                "messages": [{"role": "user", "content": "   "}],
            },
        ]
        websocket = self._fake_embedded_websocket(json.dumps(message) for message in messages)
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"conversation:title:generate"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(["protocol.invalid_payload"] * len(messages), [event["code"] for event in websocket.sent])

    def test_profile_session_id_parses_banner_session(self):
        adapter, _message = self._signed_resume_fixture()

        self.assertEqual(
            "20260602_091751_09aa41",
            adapter._session_id_from_profile_chat_output(
                "Available Tools\n│    Session: 20260602_091751_09aa41    kanban-worker │\n",
                "",
            ),
        )
