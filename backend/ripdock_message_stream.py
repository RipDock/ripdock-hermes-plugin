import logging


PROTOCOL_VERSION = "1"

logger = logging.getLogger(__name__)


class RipDockMessageStream:
    def __init__(self, adapter, conversation_id, message_id, websocket=None):
        self.adapter = adapter
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.websocket = websocket
        self.app_device_id = None
        self.completed = False
        self.artifact_ids = []
        self.content = ""
        for name, default in (
            ("_outbound_conversation_by_message_id", {}),
            ("_outbound_websocket_by_message_id", {}),
            ("_outbound_app_device_by_message_id", {}),
            ("_outbound_content_by_message_id", {}),
            ("_conversation_context_by_id", {}),
            ("_completed_message_ids", set()),
            ("_ripdock_message_streams_by_message_id", {}),
            ("_artifact_ids_by_message_id", {}),
            ("_active_message_by_conversation", {}),
            ("_active_generation_by_conversation", {}),
            ("_interrupted_generation_by_conversation", {}),
            ("_completed_generation_by_conversation", {}),
            ("_running_activities_by_message_id", {}),
        ):
            if not hasattr(self.adapter, name):
                setattr(self.adapter, name, default.copy() if isinstance(default, dict) else set(default))
        self.adapter._outbound_conversation_by_message_id[message_id] = conversation_id
        if websocket:
            self.adapter._remember_outbound_websocket(message_id, websocket)
            self.app_device_id = self.adapter._app_device_id_for_websocket(websocket)

    def attach_websocket(self, websocket):
        if not websocket:
            return
        if not self.websocket or getattr(self.websocket, "closed", False):
            self.websocket = websocket
        self.adapter._remember_outbound_websocket(self.message_id, websocket)
        if not self.app_device_id:
            self.app_device_id = self.adapter._app_device_id_for_websocket(websocket)

    def _websocket(self):
        if self.app_device_id:
            websocket = self.adapter._authenticated_app_websocket_for_device(self.app_device_id)
            if websocket:
                return websocket
        if self.websocket and not getattr(self.websocket, "closed", False):
            return self.websocket
        return self.adapter._websocket_for_ripdock_send(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
        )

    def _is_interrupted(self):
        return self.adapter._is_generation_interrupted(self.conversation_id)

    async def _send(self, event):
        if self.completed and event.get("type") != "error":
            logger.warning(
                "RipDock stream frame suppressed reason=already_completed type=%s conversation=%s message=%s",
                event.get("type"),
                self.conversation_id,
                self.message_id,
            )
            return False
        if self._is_interrupted():
            logger.warning(
                "RipDock stream frame suppressed reason=interrupted type=%s conversation=%s message=%s generation=%s",
                event.get("type"),
                self.conversation_id,
                self.message_id,
                self.adapter._current_generation(self.conversation_id),
            )
            return False
        websocket = self._websocket()
        if not websocket:
            logger.warning(
                "RipDock stream frame dropped reason=missing_authenticated_device_socket type=%s conversation=%s message=%s device_id=%s",
                event.get("type"),
                self.conversation_id,
                self.message_id,
                self.app_device_id or "<unknown>",
            )
            return False
        await self.adapter._send_json_to(websocket, event)
        return True

    async def delta(self, delta, *, source="direct"):
        if not isinstance(delta, str) or not delta:
            return False
        self.content += delta
        self.adapter._outbound_content_by_message_id[self.message_id] = self.content
        self.adapter._remember_conversation_context(self.conversation_id, self.message_id, self.content)
        logger.warning(
            "RipDock stream delta source=%s conversation=%s message=%s bytes=%s",
            source,
            self.conversation_id,
            self.message_id,
            len(delta.encode("utf-8")),
        )
        return await self._send(
            {
                "type": "message.delta",
                "protocol_version": PROTOCOL_VERSION,
                "conversation_id": self.conversation_id,
                "message_id": self.message_id,
                "delta": delta,
            }
        )

    async def snapshot(self, content, *, source="edit_snapshot"):
        if not isinstance(content, str) or not content:
            return False
        previous = self.adapter._outbound_content_by_message_id.get(self.message_id, self.content)
        if content.startswith(previous):
            delta = content[len(previous):]
        else:
            delta = content
        self.content = content
        self.adapter._outbound_content_by_message_id[self.message_id] = content
        self.adapter._remember_conversation_context(self.conversation_id, self.message_id, content)
        if not delta:
            return False
        logger.warning(
            "RipDock stream snapshot source=%s conversation=%s message=%s bytes=%s",
            source,
            self.conversation_id,
            self.message_id,
            len(delta.encode("utf-8")),
        )
        return await self._send(
            {
                "type": "message.delta",
                "protocol_version": PROTOCOL_VERSION,
                "conversation_id": self.conversation_id,
                "message_id": self.message_id,
                "delta": delta,
            }
        )

    async def block(self, block, *, source="runtime_activity"):
        if not isinstance(block, dict):
            return False
        logger.warning(
            "RipDock stream block source=%s conversation=%s message=%s kind=%s",
            source,
            self.conversation_id,
            self.message_id,
            block.get("kind"),
        )
        return await self._send(
            {
                "type": "message.block",
                "protocol_version": PROTOCOL_VERSION,
                "conversation_id": self.conversation_id,
                "message_id": self.message_id,
                "block": block,
            }
        )

    async def artifact_created(self, artifact, *, transfer_id=None, download_url=None, source="artifact"):
        event = self._artifact_created_event(artifact, transfer_id=transfer_id, download_url=download_url)
        artifact_id = event.get("artifact_id")
        if artifact_id and artifact_id not in self.artifact_ids:
            self.artifact_ids.append(artifact_id)
        logger.warning(
            "RipDock stream artifact created source=%s conversation=%s message=%s artifact_id=%s",
            source,
            self.conversation_id,
            self.message_id,
            artifact_id,
        )
        return await self._send(event)

    async def transfer_requested(self, artifact, transfer, *, source="artifact"):
        logger.warning(
            "RipDock stream transfer requested source=%s conversation=%s message=%s artifact_id=%s transfer_id=%s",
            source,
            self.conversation_id,
            self.message_id,
            artifact.get("artifact_id"),
            transfer.get("transfer_id"),
        )
        return await self._send(
            {
                "type": "runtime.transfer.request",
                "protocol_version": PROTOCOL_VERSION,
                "conversation_id": artifact["conversation_id"],
                "message_id": artifact["message_id"],
                "payload": {
                    "transfer_id": transfer["transfer_id"],
                    "artifact_id": artifact["artifact_id"],
                    "download_url": transfer["download_url"],
                    "filename": artifact["filename"],
                    "mime_type": artifact["mime_type"],
                    "size_bytes": artifact["size_bytes"],
                    "direction": "runtime_to_app",
                    "sha256": artifact["sha256"],
                },
            }
        )

    async def transfer_failed(self, artifact, transfer_id, code, message, *, source="artifact"):
        logger.warning(
            "RipDock stream transfer failed source=%s conversation=%s message=%s artifact_id=%s transfer_id=%s code=%s",
            source,
            self.conversation_id,
            self.message_id,
            artifact.get("artifact_id"),
            transfer_id,
            code,
        )
        return await self._send(
            {
                "type": "runtime.transfer.failed",
                "protocol_version": PROTOCOL_VERSION,
                "conversation_id": artifact.get("conversation_id") or self.conversation_id or "",
                "message_id": artifact.get("message_id") or self.message_id,
                "payload": {
                    "transfer_id": transfer_id,
                    "artifact_id": artifact.get("artifact_id"),
                    "code": code,
                    "message": message,
                },
            }
        )

    async def complete(self, *, artifact_ids=None, source="direct"):
        if self.completed:
            logger.warning(
                "RipDock stream completion suppressed reason=already_completed source=%s conversation=%s message=%s",
                source,
                self.conversation_id,
                self.message_id,
            )
            return False
        if artifact_ids is None:
            artifact_ids = list(dict.fromkeys([*self.artifact_ids, *self.adapter._artifact_ids_for_message(self.message_id)]))
        logger.warning(
            "RipDock stream complete source=%s conversation=%s message=%s artifact_count=%s",
            source,
            self.conversation_id,
            self.message_id,
            len(artifact_ids or []),
        )
        await self.adapter._complete_running_activities_for_message(
            self._websocket(),
            self.conversation_id,
            self.message_id,
        )
        event = {
            "type": "message.completed",
            "protocol_version": PROTOCOL_VERSION,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
        }
        if artifact_ids:
            event["artifact_ids"] = artifact_ids
        sent = await self._send(event)
        if sent:
            self.completed = True
            self.adapter._completed_message_ids.add(self.message_id)
            self.adapter._clear_running_activities_for_message(self.message_id)
            self.adapter._ripdock_message_streams_by_message_id.pop(self.message_id, None)
            active_messages = getattr(self.adapter, "_active_message_by_conversation", {})
            if active_messages.get(self.conversation_id) == self.message_id:
                active_messages.pop(self.conversation_id, None)
            self.adapter._mark_generation_completed(self.conversation_id)
        return sent

    async def fail(self, code, message, *, visible_text=None, source="runtime_failure"):
        if visible_text:
            await self.delta(visible_text, source=source)
        event = {
            "type": "error",
            "protocol_version": PROTOCOL_VERSION,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "code": code,
            "message": message,
        }
        sent = await self._send(event)
        if sent:
            self.completed = True
            self.adapter._completed_message_ids.add(self.message_id)
            self.adapter._clear_running_activities_for_message(self.message_id)
            self.adapter._ripdock_message_streams_by_message_id.pop(self.message_id, None)
            active_messages = getattr(self.adapter, "_active_message_by_conversation", {})
            if active_messages.get(self.conversation_id) == self.message_id:
                active_messages.pop(self.conversation_id, None)
            self.adapter._mark_generation_completed(self.conversation_id)
        return sent

    def _artifact_created_event(self, artifact, transfer_id=None, download_url=None):
        payload_artifact = {
            "artifact_id": artifact["artifact_id"],
            "filename": artifact["filename"],
            "mime_type": artifact["mime_type"],
            "size_bytes": artifact["size_bytes"],
            "created_at": artifact["created_at"],
            "description": artifact["description"],
            "source_runtime_id": artifact["source_runtime_id"],
            "source_message_id": artifact["source_message_id"],
            "sha256": artifact["sha256"],
        }
        if transfer_id:
            payload_artifact["transfer_id"] = transfer_id
            payload_artifact["download_url"] = download_url or self.adapter._embedded_artifact_download_url(transfer_id)
        return {
            "type": "runtime.artifact.created",
            "protocol_version": PROTOCOL_VERSION,
            "conversation_id": artifact["conversation_id"],
            "message_id": artifact["message_id"],
            **payload_artifact,
        }
