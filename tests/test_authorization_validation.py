from support import *


class AuthorizationValidationTests(PluginTestBase):
    def test_authorization_scope_allows_matching_privileged_message(self):
        adapter, _message = self._signed_resume_fixture()
        scheduled = []
        adapter._schedule_message_create = lambda _websocket, message: scheduled.append(message)
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None

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
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"message:create"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(1, len(scheduled))
        self.assertEqual([], websocket.sent)

    def test_malformed_main_socket_frames_fail_closed(self):
        adapter, _message = self._signed_resume_fixture()

        websocket = self._fake_embedded_websocket([
            "{not-json",
            json.dumps(["not", "object"]),
            json.dumps({"content": "missing type"}),
            b"\x00\x01",
        ])

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual("transport.invalid_json", websocket.sent[0]["code"])
        self.assertEqual("transport.invalid_message", websocket.sent[1]["code"])
        self.assertEqual("transport.missing_type", websocket.sent[2]["code"])
        self.assertEqual("transport.invalid_message", websocket.sent[3]["code"])
        self.assertEqual(1003, websocket.close_code)

    def test_oversized_main_socket_frame_closes_connection(self):
        adapter, _message = self._signed_resume_fixture()
        old_env = os.environ.get("RIPDOCK_MAX_MESSAGE_BYTES")
        os.environ["RIPDOCK_MAX_MESSAGE_BYTES"] = "4096"
        try:
            websocket = self._fake_embedded_websocket([json.dumps({"type": "ping", "padding": "x" * 4096})])

            asyncio.run(adapter._embedded_app_loop(websocket))
        finally:
            if old_env is None:
                os.environ.pop("RIPDOCK_MAX_MESSAGE_BYTES", None)
            else:
                os.environ["RIPDOCK_MAX_MESSAGE_BYTES"] = old_env

        self.assertEqual("message.too_large", websocket.sent[0]["code"])
        self.assertEqual(1009, websocket.close_code)

    def test_authorization_denied_matrix_for_privileged_messages(self):
        adapter, _message = self._signed_resume_fixture()
        messages = [
            {"type": "message.create", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c"},
            {"type": "conversation.list", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "personal"},
            {"type": "conversation.sync", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c", "after": "1970-01-01T00:00:00Z"},
            {"type": "conversation.title.generate", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c", "messages": [{"role": "user", "content": "hello"}]},
            {"type": "runtime.settings.update", "runtime_id": "runtime-1", "settings": {}},
            {"type": "agent.settings.update", "runtime_id": "runtime-1", "agent_id": "personal", "settings": {}},
            {"type": "message.cancel", "conversation_id": "c", "message_id": "m"},
            {"type": "transfer.request", "conversation_id": "c", "payload": {"mime_type": "image/png", "size_bytes": 1}},
            {"type": "runtime.transfer.completed", "payload": {"transfer_id": "t", "artifact_id": "a", "size_bytes": 1, "sha256": "0000000000000000000000000000000000000000000000000000000000000000"}},
        ]
        websocket = self._fake_embedded_websocket(json.dumps(message) for message in messages)
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = set()

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(len(messages), len(websocket.sent))
        self.assertEqual({"authorization.denied"}, {event["code"] for event in websocket.sent})

    def test_wildcard_authorization_scope_allows_privileged_message(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        scheduled = []
        adapter._schedule_message_create = lambda _websocket, message: scheduled.append(message)
        websocket = self._fake_embedded_websocket([
            json.dumps({"type": "message.create", "protocol_version": "1", "runtime_id": "runtime-1", "agent_id": "personal", "conversation_id": "c", "client_message_id": "client-message-1", "content": "hi"})
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"*"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(1, len(scheduled))
        self.assertEqual([], websocket.sent)

    def test_endpoint_policy_message_limit_is_clamped_to_v1_bounds(self):
        adapter, _message = self._signed_resume_fixture()
        old_env = os.environ.get("RIPDOCK_MAX_MESSAGE_BYTES")
        try:
            os.environ["RIPDOCK_MAX_MESSAGE_BYTES"] = "1"
            self.assertEqual(4096, adapter._max_message_bytes())
            os.environ["RIPDOCK_MAX_MESSAGE_BYTES"] = str(2 * 1024 * 1024)
            self.assertEqual(1024 * 1024, adapter._max_message_bytes())
        finally:
            if old_env is None:
                os.environ.pop("RIPDOCK_MAX_MESSAGE_BYTES", None)
            else:
                os.environ["RIPDOCK_MAX_MESSAGE_BYTES"] = old_env
