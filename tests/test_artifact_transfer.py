from support import *


class ArtifactTransferTests(PluginTestBase):
    def test_runtime_max_artifact_bytes_cannot_exceed_protocol_max(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)

        old_env = os.environ.copy()
        try:
            os.environ["RIPDOCK_MAX_ARTIFACT_BYTES"] = "99999999"
            self.assertEqual(adapter_module.MAX_FILE_BYTES, adapter._max_artifact_bytes())
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_runtime_transfer_url_uses_public_tunnel_for_runtime_side(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.embedded_public_url = "https://localhost"
        adapter.embedded_host = "0.0.0.0"
        adapter.embedded_port = 8788

        old_env = os.environ.copy()
        try:
            os.environ["RIPDOCK_PUBLIC_RUNTIME_URL"] = "https://runtime.example.com"
            os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(tempfile.gettempdir()) / "missing-ripdock-url")
            os.environ["RIPDOCK_DIRECT_RUNTIME_URL"] = "https://localhost"
            transfer_url = adapter._embedded_transfer_url("transfer-1", "runtime")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertEqual("wss://runtime.example.com/ripdock/transfer/transfer-1/runtime", transfer_url)

    def test_runtime_transfer_url_uses_public_tunnel_file_for_runtime_side(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.embedded_public_url = "https://localhost"
        adapter.embedded_host = "0.0.0.0"
        adapter.embedded_port = 8788

        with tempfile.TemporaryDirectory() as directory:
            public_url_file = Path(directory) / "public-runtime-url"
            public_url_file.write_text("https://runtime-file.example.com\n")
            old_env = os.environ.copy()
            try:
                os.environ.pop("RIPDOCK_PUBLIC_RUNTIME_URL", None)
                os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(public_url_file)
                os.environ["RIPDOCK_DIRECT_RUNTIME_URL"] = "https://localhost"
                transfer_url = adapter._embedded_transfer_url("transfer-1", "runtime")
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual("wss://runtime-file.example.com/ripdock/transfer/transfer-1/runtime", transfer_url)

    def test_runtime_transfer_url_maps_bare_localhost_direct_url_to_embedded_port(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.embedded_public_url = "https://localhost"
        adapter.embedded_host = "0.0.0.0"
        adapter.embedded_port = 8788

        old_env = os.environ.copy()
        try:
            os.environ.pop("RIPDOCK_PUBLIC_RUNTIME_URL", None)
            os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(tempfile.gettempdir()) / "missing-ripdock-url")
            os.environ["RIPDOCK_DIRECT_RUNTIME_URL"] = "https://localhost"
            os.environ["RIPDOCK_EMBEDDED_HOST"] = "0.0.0.0"
            os.environ["RIPDOCK_EMBEDDED_PORT"] = "8788"
            transfer_url = adapter._embedded_transfer_url("transfer-1", "runtime")
            adapter.embedded_public_url = "https://localhost:443"
            os.environ["RIPDOCK_DIRECT_RUNTIME_URL"] = "https://localhost:443"
            transfer_url_with_default_port = adapter._embedded_transfer_url("transfer-1", "runtime")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertEqual("wss://127.0.0.1:8788/ripdock/transfer/transfer-1/runtime", transfer_url)
        self.assertEqual("wss://127.0.0.1:8788/ripdock/transfer/transfer-1/runtime", transfer_url_with_default_port)
        self.assertNotEqual("wss://localhost/ripdock/transfer/transfer-1/runtime", transfer_url)

    def test_runtime_artifact_transfer_request_uses_http_download_url(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.embedded_public_url = "https://localhost"
        adapter.embedded_host = "0.0.0.0"
        adapter.embedded_port = 8788
        adapter.session_id = "session-1"
        adapter.app_capabilities_by_session = {
            "session-1": {
                "type": "app.capabilities",
                "protocol_version": "1",
                "payload": {
                    "artifact_limits": {
                        "max_chunk_bytes": 65536,
                    },
                },
            },
        }
        adapter.transfers = {}
        sent_messages = []

        async def fake_send_json_to(_self, _websocket, message):
            sent_messages.append(message)

        adapter._send_json_to = types.MethodType(fake_send_json_to, adapter)

        artifact = {
            "artifact_id": "artifact-1",
            "conversation_id": "conversation-1",
            "message_id": "message-1",
            "filename": "image.png",
            "mime_type": "image/png",
            "size_bytes": 12,
            "created_at": "2026-01-01T00:00:00Z",
            "description": "",
            "source_runtime_id": "runtime-1",
            "source_message_id": "message-1",
            "path": "/tmp/image.png",
            "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        }

        old_env = os.environ.copy()
        try:
            os.environ["RIPDOCK_PUBLIC_RUNTIME_URL"] = "https://runtime.example.com"
            os.environ["RIPDOCK_PUBLIC_RUNTIME_URL_FILE"] = str(Path(tempfile.gettempdir()) / "missing-ripdock-url")
            os.environ["RIPDOCK_DIRECT_RUNTIME_URL"] = "https://localhost"
            asyncio.run(adapter._start_embedded_artifact_transfer(object(), artifact))
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        request = next(message for message in sent_messages if message["type"] == "runtime.transfer.request")
        created = next(message for message in sent_messages if message["type"] == "runtime.artifact.created")
        transfer_id = request["payload"]["transfer_id"]
        self.assertEqual(transfer_id, created["transfer_id"])
        self.assertEqual(f"https://runtime.example.com/ripdock/transfer/{transfer_id}/artifact", created["download_url"])
        self.assertEqual(f"https://runtime.example.com/ripdock/transfer/{transfer_id}/artifact", request["payload"]["download_url"])
        self.assertEqual("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", request["payload"]["sha256"])
        self.assertNotIn("max_file_bytes", request["payload"])
        self.assertEqual(f"https://runtime.example.com/ripdock/transfer/{transfer_id}/artifact", adapter.transfers[transfer_id]["download_url"])

    def test_runtime_artifact_sender_honors_app_advertised_chunk_limit(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.session_id = "session-1"
        adapter.app_capabilities_by_session = {
            "session-1": {
                "type": "app.capabilities",
                "protocol_version": "1",
                "payload": {
                    "artifact_limits": {
                        "max_chunk_bytes": 65536,
                    },
                },
            },
        }
        sent_chunks = []

        class FakeTransferWebSocket:
            async def send(self, chunk):
                sent_chunks.append(chunk)

        class FakeConnect:
            async def __aenter__(self):
                return FakeTransferWebSocket()

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        adapter_module.websockets.connect = lambda _url: FakeConnect()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.bin"
            path.write_bytes(b"x" * (65536 * 2 + 7))
            transfer = {
                "transfer_id": "transfer-1",
                "artifact_id": "artifact-1",
                "transfer_url": "wss://runtime.example.com/ripdock/transfer/transfer-1/runtime",
                "path": str(path),
                "mime_type": "image/png",
                "size_bytes": path.stat().st_size,
                "sent_bytes": 0,
                "chunks": 0,
            }
            asyncio.run(adapter._send_artifact_transfer_chunks(transfer))

        self.assertEqual([65536, 65536, 7], [len(chunk) for chunk in sent_chunks])
        self.assertEqual(65536 * 2 + 7, transfer["sent_bytes"])
        self.assertEqual(3, transfer["chunks"])

    def test_valid_completed_transfer_reference_allows_message_create(self):
        adapter, _message = self._signed_resume_fixture()
        adapter._agent_by_id = lambda agent_id: {"agent_id": agent_id} if agent_id == "personal" else None
        adapter.transfers["transfer-1"] = {"transfer_id": "transfer-1", "completed": True}
        scheduled = []
        adapter._schedule_message_create = lambda _websocket, message: scheduled.append(message)
        websocket = self._fake_embedded_websocket([
            json.dumps({
                "type": "message.create",
                "protocol_version": "1",
                "runtime_id": "runtime-1",
                "agent_id": "personal",
                "conversation_id": "c",
                "client_message_id": "client-message-1",
                "content": "use the file",
                "transfer_ids": ["transfer-1"],
            })
        ])
        adapter.authenticated_app_websockets.add(websocket)
        adapter.authenticated_app_device_by_websocket[websocket] = "device-1"
        adapter.authenticated_app_scopes_by_websocket[websocket] = {"message:create"}

        asyncio.run(adapter._embedded_app_loop(websocket))

        self.assertEqual(1, len(scheduled))
        self.assertEqual([], websocket.sent)

    def test_hermes_runtime_preserves_uploaded_attachment_metadata_without_preview(self):
        sys.path.insert(0, str(ROOT))
        from runtime.hermes.HermesRuntime import HermesRuntime

        class FakeAdapter:
            session_id = "session-1"

            def __init__(self):
                self.transfers = {}

        adapter = FakeAdapter()
        runtime = HermesRuntime(adapter)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload.pdf"
            path.write_text("PDF bytes are handed to Hermes, not parsed by RipDock.", encoding="utf-8")
            adapter.transfers["transfer-1"] = {
                "transfer_id": "transfer-1",
                "completed": True,
                "filename": "upload.pdf",
                "mime_type": "application/pdf",
                "size_bytes": path.stat().st_size,
                "path": str(path),
            }

            attachments = asyncio.run(runtime.attachFiles({"transfer_ids": ["transfer-1"]}))

        self.assertEqual(1, len(attachments))
        self.assertEqual("upload.pdf", attachments[0]["filename"])
        self.assertEqual("application/pdf", attachments[0]["mime_type"])
        self.assertEqual(str(path), attachments[0]["path"])
        self.assertNotIn("text_preview", attachments[0])

    def test_hermes_runtime_does_not_inject_attachment_metadata_into_prompt(self):
        sys.path.insert(0, str(ROOT))
        from runtime.hermes.HermesRuntime import HermesRuntime

        class FakeAdapter:
            session_id = "session-1"

            def __init__(self):
                self.transfers = {
                    "transfer-1": {
                        "transfer_id": "transfer-1",
                        "completed": True,
                        "filename": "upload.pdf",
                        "mime_type": "application/pdf",
                        "size_bytes": 123,
                        "path": "/opt/data/ripdock/transfers/transfer-1.bin",
                    }
                }
                self.dispatched_prompt = None

            async def _dispatch_hermes_message(self, _websocket, _message, prompt):
                self.dispatched_prompt = prompt

        adapter = FakeAdapter()
        runtime = HermesRuntime(adapter)
        asyncio.run(runtime.sendMessage(object(), {
            "conversation_id": "conversation-1",
            "content": "Inspect the attachment.",
            "transfer_ids": ["transfer-1"],
        }))

        self.assertEqual("Inspect the attachment.", adapter.dispatched_prompt)

    def test_pdf_transfer_selects_document_message_type(self):
        adapter_module = load_backend_adapter()
        adapter = adapter_module.RipDockAdapter.__new__(adapter_module.RipDockAdapter)
        adapter.transfers = {
            "transfer-1": {
                "transfer_id": "transfer-1",
                "completed": True,
                "filename": "upload.pdf",
                "mime_type": "application/pdf",
                "path": "/opt/data/ripdock/transfers/transfer-1.bin",
            }
        }

        class FakeMessageType:
            TEXT = "text"
            DOCUMENT = "document"

        old_message_type = adapter_module.MessageType
        try:
            adapter_module.MessageType = FakeMessageType
            message_type = adapter._message_type_for_message({"transfer_ids": ["transfer-1"]})
        finally:
            adapter_module.MessageType = old_message_type

        self.assertEqual("document", message_type)

    def test_artifact_download_route_is_http_not_websocket_only(self):
        from fastapi.testclient import TestClient

        runtime_app = load_runtime_app()
        calls = []

        def artifact_download(transfer_id):
            calls.append(transfer_id)
            return 200, [("content-type", "text/plain")], b"artifact bytes"

        adapter = types.SimpleNamespace(_handle_embedded_artifact_download=artifact_download)
        client = TestClient(runtime_app.create_runtime_app(adapter))

        response = client.get("/ripdock/transfer/transfer%3Aone/artifact")

        self.assertEqual(200, response.status_code)
        self.assertEqual("artifact bytes", response.text)
        self.assertEqual("text/plain", response.headers["content-type"])
        self.assertEqual(["transfer:one"], calls)
