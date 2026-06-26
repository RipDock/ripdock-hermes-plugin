import asyncio
import logging
import os

from .HermesSession import HermesSession


logger = logging.getLogger(__name__)


class HermesRuntime:
    def __init__(self, adapter):
        self.adapter = adapter
        self.session = HermesSession(getattr(adapter, "session_id", None))
        self.timeout = self._timeout_seconds()
        self.base_url = os.getenv("HERMES_BASE_URL", "")
        self.api_key = os.getenv("HERMES_API_KEY", "")
        self.model = os.getenv("HERMES_MODEL", "")

    def _timeout_seconds(self):
        raw_value = os.getenv("HERMES_TIMEOUT", "1800")
        try:
            value = int(raw_value)
        except ValueError:
            return 1800
        return value if value > 0 else 1800

    async def sendMessage(self, websocket, message):
        conversation_id = message.get("conversation_id")
        content = message.get("content", "")
        if not isinstance(content, str):
            content = ""

        attachments = await self.attachFiles(message)

        logger.warning(
            "RipDock Hermes message sent session=%s conversation=%s model=%s base_url=%s attachments=%s",
            getattr(self.adapter, "session_id", None),
            conversation_id,
            self.model or "configured-default",
            self.base_url or "local-gateway",
            len(attachments),
        )
        logger.warning("RipDock Hermes stream started conversation=%s", conversation_id)

        try:
            await asyncio.wait_for(
                self.adapter._dispatch_hermes_message(websocket, message, content),
                timeout=self.timeout,
            )
            logger.warning("RipDock Hermes stream completed conversation=%s", conversation_id)
        except asyncio.CancelledError:
            logger.warning(
                "RipDock Hermes stream interrupted conversation=%s reason=cancelled_by_user",
                conversation_id,
            )
            return
        except asyncio.TimeoutError:
            logger.warning("RipDock Hermes stream failed conversation=%s reason=timeout", conversation_id)
            await self.adapter._send_runtime_failure(
                websocket,
                conversation_id,
                "runtime.unavailable",
                "Hermes timed out.",
            )
        except Exception as exc:
            logger.exception("RipDock Hermes stream failed conversation=%s", conversation_id)
            await self.adapter._send_runtime_failure(
                websocket,
                conversation_id,
                self._error_code(exc),
                self._error_message(exc),
            )

    async def streamResponse(self, websocket, message):
        await self.sendMessage(websocket, message)

    async def attachFiles(self, message):
        transfer_ids = message.get("transfer_ids") if isinstance(message, dict) else []
        if not isinstance(transfer_ids, list):
            return []

        attachments = []
        for transfer_id in transfer_ids:
            if not isinstance(transfer_id, str):
                continue
            transfer = self.adapter.transfers.get(transfer_id)
            if not transfer:
                attachments.append({"transfer_id": transfer_id, "completed": False})
                continue
            attachments.append(dict(transfer))
        return attachments

    async def resetConversation(self):
        self.session.reset_conversation()

    async def disconnect(self):
        await self.resetConversation()

    def _error_code(self, exc):
        return "runtime.unavailable"

    def _error_message(self, exc):
        return "Runtime is unavailable."
