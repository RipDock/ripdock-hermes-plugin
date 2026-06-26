import asyncio

try:
    from starlette.websockets import WebSocketDisconnect
except Exception:
    WebSocketDisconnect = Exception


class RuntimeWebSocket:
    def __init__(self, websocket, path):
        self._websocket = websocket
        self.path = path
        self.request = type("RuntimeWebSocketRequest", (), {"path": path})()
        self.closed = False
        self._closed_event = asyncio.Event()

    async def accept(self):
        await self._websocket.accept()

    async def send(self, message):
        if isinstance(message, bytes):
            await self._websocket.send_bytes(message)
        else:
            await self._websocket.send_text(str(message))

    async def close(self, code=1000, reason=""):
        if self.closed:
            return
        self.closed = True
        self._closed_event.set()
        await self._websocket.close(code=code, reason=reason)

    async def wait_closed(self):
        await self._closed_event.wait()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            message = await self._websocket.receive()
        except WebSocketDisconnect:
            self.closed = True
            self._closed_event.set()
            raise StopAsyncIteration

        message_type = message.get("type")
        if message_type == "websocket.disconnect":
            self.closed = True
            self._closed_event.set()
            raise StopAsyncIteration
        if "text" in message and message["text"] is not None:
            return message["text"]
        if "bytes" in message and message["bytes"] is not None:
            return message["bytes"]
        return ""


async def handle_runtime_websocket(adapter, websocket, path):
    runtime_websocket = RuntimeWebSocket(websocket, path)
    await runtime_websocket.accept()
    try:
        await adapter._handle_embedded_app(runtime_websocket, path)
    finally:
        runtime_websocket.closed = True
        runtime_websocket._closed_event.set()
