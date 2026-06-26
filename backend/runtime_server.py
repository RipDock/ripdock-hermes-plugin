import asyncio
import logging

import uvicorn

from backend.runtime_app import create_runtime_app


logger = logging.getLogger(__name__)


class RuntimeServer:
    def __init__(self, adapter, host, port):
        self.adapter = adapter
        self.host = host
        self.port = port
        self.app = create_runtime_app(adapter)
        self.server = None
        self.task = None

    async def start(self):
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            loop="asyncio",
        )
        self.server = uvicorn.Server(config)
        self.task = asyncio.create_task(self.server.serve())
        await self._wait_until_started()

    async def _wait_until_started(self):
        for _ in range(100):
            if self.server and self.server.started:
                return
            if self.task and self.task.done():
                self.task.result()
            await asyncio.sleep(0.05)
        raise RuntimeError(f"RipDock Runtime listener did not start on {self.host}:{self.port}")

    async def close(self):
        if not self.server:
            return
        self.server.should_exit = True
        if self.task:
            await self.task
        self.server = None
        self.task = None

    async def wait_closed(self):
        if self.task:
            await self.task
