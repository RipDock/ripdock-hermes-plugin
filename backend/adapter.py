import asyncio
import ast
import base64
import binascii
import calendar
import hashlib
import importlib.util
import json
import logging
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import websockets
try:
    from websockets.datastructures import Headers as WebSocketsHeaders
    from websockets.http11 import Response as WebSocketsResponse
except Exception:
    WebSocketsHeaders = None
    WebSocketsResponse = None

from gateway.platforms.base import BasePlatformAdapter
from gateway.config import PlatformConfig

try:
    from gateway.platforms.base import MessageEvent, MessageType, SendResult
    from gateway.session import SessionSource, build_session_key
    from gateway.config import Platform
except Exception:
    MessageEvent = None
    MessageType = None
    SessionSource = None
    build_session_key = None
    Platform = None

    class SendResult:
        def __init__(self, success, message_id=None, error=None, raw_response=None, retryable=False):
            self.success = success
            self.message_id = message_id
            self.error = error
            self.raw_response = raw_response
            self.retryable = retryable

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
RUNTIME_DIR = PLUGIN_DIR / "runtime"
RIPDOCK_INSTRUCTIONS_FILE = RUNTIME_DIR / "RIPDOCK.md"

from runtime.hermes import (
    RIPDOCK_RICH_TEXT_V1_CAPABILITIES,
    classify_runtime_content,
    HermesRuntime,
    HermesSession,
    formatting_capability_summary,
)
from backend.ripdock_message_stream import RipDockMessageStream
from backend.runtime_server import RuntimeServer

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "1"


def _hermes_home():
    value = os.getenv("HERMES_HOME", "").strip()
    if value:
        return Path(value)
    home = os.getenv("HOME", "").strip()
    return Path(home) / ".hermes" if home else Path.home() / ".hermes"


def _emoji_icon_or_empty(value):
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return ""
    has_emoji_symbol = any(unicodedata.category(char) == "So" for char in text)
    has_alnum = any(char.isalnum() for char in text)
    return text if has_emoji_symbol and not has_alnum else ""


def _embedded_http_response(status, headers, body):
    if WebSocketsResponse is None or WebSocketsHeaders is None:
        return (status, headers, body)
    response_headers = WebSocketsHeaders()
    for key, value in headers:
        response_headers[key] = value
    response_headers.setdefault("content-length", str(len(body)))
    reason = {
        200: "OK",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
    }.get(status, "OK")
    return WebSocketsResponse(status, reason, response_headers, body)


MIN_MAIN_MESSAGE_BYTES = 4096
MAX_MAIN_MESSAGE_BYTES = 1024 * 1024
DEFAULT_MAX_MESSAGE_BYTES = MAX_MAIN_MESSAGE_BYTES
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
DEFAULT_PAIRING_TTL_SECONDS = 15 * 60
MIN_PAIRING_TTL_SECONDS = 30
MAX_PAIRING_TTL_SECONDS = 15 * 60
PRODUCTION_PAIRING_TTL_SECONDS = 15 * 60
DEFAULT_REJECTED_PAIRING_TTL_SECONDS = 10 * 60
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60
DEFAULT_PAIRING_REQUEST_RATE_LIMIT = 20
DEFAULT_PAIRING_STATUS_RATE_LIMIT = 120
DEFAULT_PAIRING_CODE_RATE_LIMIT = 10
DEFAULT_RESUME_FAILURE_RATE_LIMIT = 20
DEFAULT_MESSAGE_BURST_RATE_LIMIT = 120
DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS = 24 * 60 * 60
DEFAULT_SESSION_ABSOLUTE_LIFETIME_SECONDS = 30 * 24 * 60 * 60
MIN_SESSION_LIFETIME_SECONDS = 60
MAX_DELAY_COMMAND_MS = 30_000
DEFAULT_AUTHORIZATION_SCOPES = (
    "message:create",
    "message:cancel",
    "conversation:list",
    "conversation:sync",
    "conversation:delete",
    "conversation:title:generate",
    "agent:settings:update",
    "runtime:settings:update",
    "transfer:app_to_runtime",
    "transfer:runtime_to_app:ack",
)
SUPPORTED_TRANSFER_MIME_TYPES = {"image/jpeg", "image/png", "application/pdf"}
RIPDOCK_ADVERTISED_SLASH_COMMANDS = [
    {"name": "help", "display": "/help", "description": "Show available Runtime commands.", "category": "Info"},
    {"name": "status", "display": "/status", "description": "Show session info.", "category": "Info"},
    {"name": "usage", "display": "/usage", "description": "Show token usage and rate limits.", "category": "Info"},
    {"name": "insights", "display": "/insights", "description": "Show usage insights and analytics.", "argument_hint": "[days]", "category": "Info"},
    {"name": "retry", "display": "/retry", "description": "Retry the last message.", "category": "Session"},
    {"name": "undo", "display": "/undo", "description": "Back up user turns and re-prompt.", "argument_hint": "[N]", "category": "Session"},
    {"name": "compress", "display": "/compress", "description": "Compress conversation context.", "argument_hint": "[focus topic]", "category": "Session"},
    {"name": "goal", "display": "/goal", "description": "Set or manage a standing goal.", "argument_hint": "[text | pause | resume | clear | status]", "category": "Session"},
    {"name": "subgoal", "display": "/subgoal", "description": "Add or manage active goal criteria.", "argument_hint": "[text | remove N | clear]", "category": "Session"},
    {"name": "queue", "display": "/queue", "description": "Queue a prompt for the next turn.", "argument_hint": "<prompt>", "category": "Session", "aliases": ["q"]},
    {"name": "steer", "display": "/steer", "description": "Inject guidance after the next tool call.", "argument_hint": "<prompt>", "category": "Session"},
    {"name": "model", "display": "/model", "description": "Switch or show the model for this session.", "argument_hint": "[model] [--provider name]", "category": "Configuration"},
    {"name": "reasoning", "display": "/reasoning", "description": "Manage reasoning effort and display.", "argument_hint": "[level|show|hide]", "category": "Configuration"},
    {"name": "fast", "display": "/fast", "description": "Toggle fast mode.", "argument_hint": "[normal|fast|status]", "category": "Configuration"},
    {"name": "tools", "display": "/tools", "description": "List, enable, or disable tools.", "argument_hint": "[list|enable|disable] [name...]", "category": "Tools"},
    {"name": "skills", "display": "/skills", "description": "Search, install, inspect, or manage skills.", "argument_hint": "[list|search|browse|inspect|install]", "category": "Tools"},
    {"name": "bundles", "display": "/bundles", "description": "List skill bundles.", "category": "Tools"},
    {"name": "reload-skills", "display": "/reload-skills", "description": "Rescan installed skills.", "category": "Tools", "aliases": ["reload_skills"]},
    {"name": "cron", "display": "/cron", "description": "Manage scheduled Runtime tasks.", "argument_hint": "[list|add|edit|pause|resume|run|remove]", "category": "Tools"},
    {"name": "curator", "display": "/curator", "description": "Manage background skill maintenance.", "argument_hint": "[status|run|pause|resume|pin]", "category": "Tools"},
    {"name": "kanban", "display": "/kanban", "description": "Manage the Runtime kanban board.", "argument_hint": "[subcommand]", "category": "Tools"},
    {"name": "background", "display": "/background", "description": "Run a prompt in the background.", "argument_hint": "<prompt>", "category": "Tools", "aliases": ["bg", "btw"]},
    {"name": "delay", "display": "/delay", "description": "Reply after a delay.", "argument_hint": "<milliseconds> <text>", "category": "Debug"},
    {"name": "agents", "display": "/agents", "description": "Show active agents and running tasks.", "category": "Tools", "aliases": ["tasks"]},
    {"name": "stop", "display": "/stop", "description": "Stop background processes.", "category": "Tools"},
]
RIPDOCK_ADVERTISED_SLASH_COMMAND_NAMES = {
    command["name"]
    for command in RIPDOCK_ADVERTISED_SLASH_COMMANDS
} | {
    alias
    for command in RIPDOCK_ADVERTISED_SLASH_COMMANDS
    for alias in command.get("aliases", [])
}
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
SEMANTIC_BLOCK_DEMOS = [
    {
        "kind": "text",
        "mime_type": "text/plain",
        "content": "Hermes semantic text block.",
        "copyable": False,
        "wrap": True,
        "collapsed": False,
    },
    {
        "kind": "markdown",
        "mime_type": "text/markdown",
        "content": "## Markdown Block\n\nThis block includes **bold**, *italic*, __underline__, `inline code`, a URL https://example.com, a list:\n\n- first item\n- second item\n\n> quoted text",
        "copyable": False,
        "wrap": True,
        "collapsed": False,
    },
    {
        "kind": "code",
        "mime_type": "text/code",
        "language": "python",
        "title": "hello.py",
        "content": "print(\"hello from RipDock\")\n",
        "copyable": True,
        "wrap": False,
        "collapsed": False,
    },
    {
        "kind": "log",
        "mime_type": "text/log",
        "title": "hermes.log",
        "content": "[info] received message.create\n[info] emitted semantic block demos\n",
        "copyable": True,
        "wrap": False,
        "collapsed": False,
    },
    {
        "kind": "data",
        "mime_type": "application/json",
        "language": "json",
        "title": "result.json",
        "content": json.dumps({"status": "ok", "runtime": "hermes", "count": 3}, indent=2),
        "copyable": True,
        "wrap": True,
        "collapsed": False,
    },
    {
        "kind": "data",
        "mime_type": "application/yaml",
        "language": "yaml",
        "title": "demo.yml",
        "content": "runtime: hermes\nsemantic_blocks: true\n",
        "copyable": True,
        "wrap": False,
        "collapsed": False,
    },
    {
        "kind": "activity.status",
        "mime_type": "application/vnd.ripdock.activity+json",
        "title": "Runtime Activity",
        "content": json.dumps(
            {
                "category": "runtime",
                "status": "running",
                "summary": "Preparing a response",
            },
            indent=2,
        ),
        "copyable": True,
        "wrap": True,
        "collapsed": True,
    },
    {
        "kind": "activity.tool.progress",
        "mime_type": "application/vnd.ripdock.activity+json",
        "title": "Tool Progress",
        "content": json.dumps(
            {
                "category": "search",
                "tool": "web_search",
                "status": "running",
                "summary": "Searching web",
                "args": {"query": "RipDock Protocol"},
            },
            indent=2,
        ),
        "copyable": True,
        "wrap": True,
        "collapsed": True,
    },
    {
        "kind": "artifact.reference",
        "mime_type": "application/vnd.ripdock.artifact+json",
        "title": "Artifact Reference",
        "content": json.dumps(
            {
                "artifact_id": "artifact-demo",
                "filename": "example.pdf",
                "mime_type": "application/pdf",
                "status": "available",
            },
            indent=2,
        ),
        "copyable": True,
        "wrap": True,
        "collapsed": True,
    },
]
QA_TRANSFER_FAILURE_VARIANTS = (
    {
        "tag": "invalid-url",
        "filename": "invalid-url.txt",
        "message": "Invalid transfer URL",
        "code": "runtime.transfer.failed",
    },
    {
        "tag": "runtime-reject",
        "filename": "runtime-reject.txt",
        "message": "Runtime rejected transfer",
        "code": "runtime.transfer.failed",
    },
    {
        "tag": "network-drop",
        "filename": "network-drop.txt",
        "message": "Network dropped during transfer",
        "code": "runtime.transfer.failed",
    },
    {
        "tag": "download",
        "filename": "download-failure.txt",
        "message": "Download failed",
        "code": "runtime.transfer.failed",
    },
    {
        "tag": "hash-mismatch",
        "filename": "hash-mismatch.txt",
        "message": "Transfer checksum mismatch",
        "code": "runtime.transfer.failed",
    },
)
QA_TRANSFER_FAILURE_VARIANTS_BY_TAG = {variant["tag"]: variant for variant in QA_TRANSFER_FAILURE_VARIANTS}

def check_requirements():
    try:
        import cryptography  # noqa: F401
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except Exception:
        logger.error("RipDock Hermes plugin requires cryptography, fastapi, and uvicorn.")
        return False
    return True


def validate_config(config):
    return True


def _apply_yaml_config(yaml_cfg, platform_cfg):
    platform_toolsets = yaml_cfg.get("platform_toolsets") if isinstance(yaml_cfg, dict) else None
    if not isinstance(platform_toolsets, dict):
        return None
    if "ripdock" in platform_toolsets:
        return None
    cli_toolsets = platform_toolsets.get("cli")
    if not isinstance(cli_toolsets, list):
        return None
    platform_toolsets["ripdock"] = list(cli_toolsets)
    return {"toolset_inheritance": "cli"}


class RipDockAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        platform = Platform("ripdock") if Platform is not None else "ripdock"
        super().__init__(config, platform)
        self.ws = None
        self.embedded_server = None
        self.app_ws = None
        self.app_websockets = set()
        self.authenticated_app_websockets = set()
        self.authenticated_app_device_by_websocket = {}
        self.authenticated_app_scopes_by_websocket = {}
        self.app_session_route_by_websocket = {}
        self._app_websocket_by_client_message_id = {}
        self._app_websocket_by_conversation_id = {}
        self._outbound_websocket_by_message_id = {}
        self._resume_nonce_seen_at = {}
        self._rate_limit_events = {}
        self.gateway_runner = None
        self._running = False
        self.session_id = None
        self.session_created_at = None
        self.session_last_seen_at = None
        self.session_expires_at = None
        self.session_idle_expires_at = None
        self.pairing_code = None
        self.pairing_code_created_at = None
        self.pairing_bound = False
        self.embedded_public_url = None
        self.embedded_host = None
        self.embedded_port = None
        self._last_session_resume_failed = False
        self.app_capabilities_by_session = {}
        self.transfers = {}
        self.runtime_provider = os.getenv("RUNTIME_PROVIDER", "hermes").strip().lower()
        if self.runtime_provider not in {"stub", "hermes"}:
            logger.warning("Unknown RUNTIME_PROVIDER=%s; using hermes", self.runtime_provider)
            self.runtime_provider = "hermes"
        self.hermes_session = HermesSession()
        self.hermes_runtime = HermesRuntime(self)
        self._outbound_conversation_by_message_id = {}
        self._outbound_websocket_by_message_id = {}
        self._outbound_content_by_message_id = {}
        self._completed_message_ids = set()
        self._pending_runtime_intent_by_message_id = {}
        self._ripdock_message_streams_by_message_id = {}
        self._outbound_message_count_by_conversation = {}
        self._suppressed_home_channel_notice_conversations = set()
        self._raw_tool_details = {}
        self._activity_state_by_conversation = {}
        self._running_activities_by_message_id = {}
        self._active_generation_by_conversation = {}
        self._interrupted_generation_by_conversation = {}
        self._completed_generation_by_conversation = {}
        self._active_message_by_conversation = {}
        self._active_user_text_by_conversation = {}
        self._generated_artifacts_by_key = {}
        self._generated_artifacts_by_id = {}
        self._artifact_ids_by_message_id = {}
        self._conversation_context_by_id = {}
        self._conversation_create_receipts = {}
        self._pending_artifact_transfers = {}
        self._hermes_tool_progress_names = self._load_hermes_tool_progress_names()
        self._hermes_tool_progress_names_finalized = False
        self._ripdock_cron_delivery_task = None
        self._dev_command_module = self._load_dev_command_module()
        self.app_capabilities_by_session[self._metadata_session_key()] = self._default_client_capabilities()
        self.runtime_type = self._configured_runtime_type()
        self.runtime_identity = self._load_or_create_runtime_identity()
        self.runtime_id = self.runtime_identity["runtimeId"]
        self.runtime_settings_by_runtime_id = {
            self.runtime_id: {},
        }
        self._install_ripdock_toolset_inheritance_override()
        self._install_ripdock_context_diagnostics_override()
        self._install_ripdock_display_override()

    async def connect(self):
        try:
            self._running = True

            await self._start_embedded_endpoint()

            logger.warning("RipDock connect() success")
            logger.warning(
                "RipDock Runtime provider: %s",
                self.runtime_provider,
            )
            self._log_advertised_client_capabilities()
            self._ensure_ripdock_cron_delivery_watcher()

            return True

        except Exception:
            logger.exception("RipDock connect() failed")
            raise

    async def disconnect(self):
        self._running = False
        if self._ripdock_cron_delivery_task:
            self._ripdock_cron_delivery_task.cancel()
            self._ripdock_cron_delivery_task = None

        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            logger.exception("Error closing websocket")

        try:
            for app_ws in list(self.app_websockets):
                await app_ws.close(code=1000, reason="Connector shutdown requested.")
        except Exception:
            logger.exception("Error closing embedded app websocket")

        try:
            if self.embedded_server:
                self.embedded_server.close()
                await self.embedded_server.wait_closed()
        except Exception:
            logger.exception("Error closing embedded runtime endpoint")

        logger.info("RipDock platform disconnected")

    async def _start_embedded_endpoint(self):
        saved_state = self._read_saved_session_state()
        if saved_state:
            self._restore_persisted_runtime_state(saved_state)
        saved_session_id = saved_state.get("session_id")
        if saved_session_id:
            self.session_id = saved_session_id
            self._restore_session_lifecycle(saved_state)
            expired, expiry_reason = self._session_expiry_reason()
            if expired:
                logger.warning("RipDock saved Session expired reason=%s sessionID=%s", expiry_reason, self._redacted_session_id(saved_session_id))
                self._clear_session_id_from_trusted_devices(saved_session_id)
                self._invalidate_saved_session(expiry_reason)
                self.session_id = self._create_session_id()
                self._reset_session_lifecycle()
                self._save_session_id(self.session_id)
            else:
                logger.warning("Reusing RipDock sessionID=%s", self._redacted_session_id(saved_session_id))
        else:
            self.session_id = self._create_session_id()
            self._reset_session_lifecycle()
            self._save_session_id(self.session_id)

        self.pairing_code = self._create_pairing_code()
        self.pairing_code_created_at = time.time()
        self.pairing_bound = False

        host = os.getenv("RIPDOCK_EMBEDDED_HOST", "0.0.0.0")
        port = int(os.getenv("RIPDOCK_EMBEDDED_PORT", "8788"))
        self.embedded_host = host
        self.embedded_port = port
        self.embedded_public_url = os.getenv(
            "RIPDOCK_DIRECT_RUNTIME_URL",
            "https://localhost:8443",
        ).rstrip("/")
        self._require_secure_transport_url(
            self.embedded_public_url,
            "RIPDOCK_DIRECT_RUNTIME_URL",
        )
        if not self._embedded_runtime_endpoint_enabled():
            logger.warning(
                "RipDock Embedded Runtime Endpoint disabled by RIPDOCK_EMBEDDED_RUNTIME_ENDPOINT=false"
            )
            return
        self.embedded_server = RuntimeServer(self, host, port)
        await self.embedded_server.start()

        public_host = os.getenv("RIPDOCK_EMBEDDED_PUBLIC_HOST", "127.0.0.1")
        public_port = os.getenv("RIPDOCK_EMBEDDED_PUBLIC_PORT", str(port))
        logger.warning(
            "RipDock Embedded Runtime Endpoint: %s",
            self.embedded_public_url,
        )
        self._log_advertised_client_capabilities()
        self._log_pairing(
            self.pairing_code,
            self._direct_pairing_payload(self.pairing_code, public_host, public_port),
        )
        logger.warning("RipDock connect() success")

    def _embedded_runtime_endpoint_enabled(self):
        value = os.getenv("RIPDOCK_EMBEDDED_RUNTIME_ENDPOINT", "true").strip().lower()
        return value not in {"0", "false", "no", "off"}

    async def _handle_embedded_app(self, websocket, path=None):
        request_path = urlsplit(self._embedded_request_path(websocket, path)).path

        try:
            pairing_code = self._pairing_code_from_app_route(request_path)
            app_session_route = self._app_session_route_from_path(request_path)
            if request_path.startswith("/ripdock/transfer/"):
                await self._handle_embedded_transfer_socket(websocket, request_path)
                return
            if pairing_code:
                await self._handle_embedded_pairing(websocket, request_path)
            elif app_session_route:
                await self._handle_embedded_session(websocket, request_path)
            else:
                await websocket.close(code=1008, reason="Not Found")
                return

            await self._embedded_app_loop(websocket)
        except Exception:
            logger.exception("Embedded Runtime Endpoint connection failed")
            try:
                await websocket.close(code=1011, reason="Embedded endpoint error.")
            except Exception:
                pass
        finally:
            self.app_websockets.discard(websocket)
            self.authenticated_app_websockets.discard(websocket)
            self.authenticated_app_device_by_websocket.pop(websocket, None)
            self.authenticated_app_scopes_by_websocket.pop(websocket, None)
            getattr(self, "app_session_route_by_websocket", {}).pop(websocket, None)
            if self.app_ws is websocket:
                self.app_ws = self._latest_open_embedded_app_websocket()
            if getattr(self, "_last_ripdock_websocket", None) is websocket:
                self._last_ripdock_websocket = self._latest_open_embedded_app_websocket()
            for message_id, message_websocket in list(self._outbound_websocket_by_message_id.items()):
                if message_websocket is websocket:
                    self._outbound_websocket_by_message_id.pop(message_id, None)

    async def _handle_embedded_pairing(self, websocket, request_path):
        await self._emit_endpoint_policy_to(websocket)
        pairing_code = self._pairing_code_from_app_route(request_path)
        if not self._pairing_code_matches(pairing_code):
            error = self._pairing_error()
            if self._record_rate_limit_event("pairing_code", pairing_code or "missing"):
                error = self._runtime_error("Pairing is temporarily unavailable. Try again shortly.", code="pairing.rate_limited")
            await self._send_json_to(websocket, error)
            await websocket.close(code=1000, reason="Pairing failed.")
            return

        self.pairing_bound = True
        self._save_session_state()
        await self._replace_embedded_app(websocket)
        await self._send_json_to(
            websocket,
            {
                "type": "pairing.connected",
                "protocol_version": PROTOCOL_VERSION,
                "session_id": self.session_id,
            },
        )
        await self._emit_runtime_metadata_to(websocket)

    async def _handle_embedded_session(self, websocket, request_path):
        if not hasattr(self, "app_session_route_by_websocket"):
            self.app_session_route_by_websocket = {}
        self.app_session_route_by_websocket[websocket] = self._app_session_route_from_path(request_path) or "/ripdock/app"
        await self._emit_endpoint_policy_to(websocket)
        logger.warning("RipDock embedded App session socket opened; awaiting session.resume")

    def _pairing_code_from_app_route(self, request_path):
        parts = str(request_path or "").strip("/").split("/")
        if len(parts) >= 4 and parts[-4] == "ripdock" and parts[-3] == "app" and parts[-2] == "pair" and parts[-1]:
            return parts[-1]
        return ""

    def _app_session_route_from_path(self, request_path):
        parts = str(request_path or "").strip("/").split("/")
        if len(parts) >= 2 and parts[-2] == "ripdock" and parts[-1] == "app":
            return "/" + "/".join(parts)
        return ""

    async def _replace_embedded_app(self, websocket):
        self.app_websockets.add(websocket)
        self.app_ws = websocket

    def _latest_open_embedded_app_websocket(self):
        for websocket in reversed(list(getattr(self, "app_websockets", set()) or [])):
            if not getattr(websocket, "closed", False):
                return websocket
        return None

    def _authenticated_app_websocket_for_device(self, app_device_id):
        if not isinstance(app_device_id, str) or not app_device_id.strip():
            return None
        for websocket, device_id in reversed(list(getattr(self, "authenticated_app_device_by_websocket", {}).items())):
            if device_id == app_device_id and not getattr(websocket, "closed", False):
                return websocket
        return None

    async def _embedded_app_loop(self, websocket):
        async for raw in websocket:
            logger.warning("Embedded app recv: %s", self._redact_protocol_log(raw))
            if self._message_size(raw) > self._max_message_bytes():
                await self._send_json_to(
                    websocket,
                    self._runtime_error(
                        "Message exceeds endpoint policy max_message_bytes.",
                        code="message.too_large",
                    ),
                )
                await websocket.close(code=1009, reason="Message exceeds endpoint policy.")
                return
            if not isinstance(raw, str):
                await self._send_json_to(
                    websocket,
                    self._runtime_error(
                        "Main protocol socket expects JSON text messages.",
                        code="transport.invalid_message",
                    ),
                )
                await websocket.close(code=1003, reason="Main protocol socket expects JSON text.")
                return

            try:
                msg = json.loads(raw)
            except Exception:
                await self._send_json_to(
                    websocket,
                    self._runtime_error("Invalid JSON message.", code="transport.invalid_json"),
                )
                continue

            if not isinstance(msg, dict):
                await self._send_json_to(
                    websocket,
                    self._runtime_error("Protocol message must be a JSON object.", code="transport.invalid_message"),
                )
                continue

            msg_type = msg.get("type")
            if not isinstance(msg_type, str) or not msg_type:
                await self._send_json_to(
                    websocket,
                    self._runtime_error("Protocol message type is required.", code="transport.missing_type"),
                )
                continue

            if websocket in self.authenticated_app_websockets and self._record_rate_limit_event("message_burst", self.authenticated_app_device_by_websocket.get(websocket) or "authenticated"):
                await self._send_json_to(
                    websocket,
                    self._runtime_error("Runtime is busy. Try again shortly.", code="runtime.rate_limited"),
                )
                continue

            if msg_type == "ping":
                await self._send_json_to(
                    websocket,
                    {"type": "pong", "protocol_version": PROTOCOL_VERSION},
                )
                continue

            if msg_type != "session.resume" and websocket not in self.authenticated_app_websockets:
                await self._send_json_to(
                    websocket,
                    self._runtime_error("Session resume is required.", code="session.resume_required"),
                )
                continue

            if websocket in self.authenticated_app_websockets and self._websocket_device_is_revoked(websocket):
                await self._send_json_to(
                    websocket,
                    {
                        "type": "session.expired",
                        "protocol_version": PROTOCOL_VERSION,
                        "session_id": self.session_id or "",
                        "reason": "Device trust was revoked.",
                        "code": "session.expired",
                        "connection_security_error": "runtimeRevokedDevice",
                    },
                )
                await websocket.close(code=1000, reason="Device trust was revoked.")
                return

            if msg_type == "app.capabilities":
                capabilities_error = self._validate_app_capabilities_payload(msg)
                if capabilities_error:
                    await self._send_json_to(websocket, capabilities_error)
                    continue
                self._store_app_capabilities(msg)
                await self._emit_runtime_metadata_to(websocket)
                continue

            if msg_type == "session.resume":
                await self._handle_embedded_session_resume(websocket, msg)
                continue

            required_scope = self._authorization_scope_for_message(msg)
            if required_scope and not self._websocket_has_authorization_scope(websocket, required_scope):
                await self._reject_unauthorized_message(websocket, msg, required_scope)
                continue

            payload_error = self._validate_protocol_message_payload(msg)
            if payload_error:
                await self._send_json_to(websocket, payload_error)
                continue

            if msg_type == "runtime.settings.update":
                await self._handle_runtime_settings_update(websocket, msg)
                continue

            if msg_type == "agent.settings.update":
                await self._handle_agent_settings_update(websocket, msg)
                continue

            if self._is_interrupt_event(msg):
                await self._handle_runtime_interrupt(websocket, msg)
                continue

            if msg_type == "transfer.request":
                await self._handle_transfer_request(websocket, msg, embedded=True)
                continue

            if msg_type == "transfer.ready":
                await self._handle_transfer_ready(msg)
                continue

            if msg_type == "transfer.completed":
                self._complete_transfer(msg)
                continue

            if msg_type == "transfer.failed":
                self._fail_transfer(msg)
                continue

            if msg_type == "runtime.transfer.completed":
                self._complete_runtime_artifact_transfer_ack(msg)
                continue

            if msg_type == "runtime.transfer.failed":
                self._fail_transfer(msg)
                continue

            if msg_type == "conversation.list":
                await self._handle_conversation_list(websocket, msg)
                continue

            if msg_type == "conversation.sync":
                await self._handle_conversation_sync(websocket, msg)
                continue

            if msg_type == "conversation.title.generate":
                await self._handle_conversation_title_generate(websocket, msg)
                continue

            if msg_type == "conversation.delete":
                await self._handle_conversation_delete(websocket, msg)
                continue

            if msg_type == "conversation.create":
                if not await self._validate_agent_routed_message_create(websocket, msg):
                    continue
                await self._handle_conversation_create(websocket, msg)
                continue

            if msg_type == "message.create":
                if not await self._validate_agent_routed_message_create(websocket, msg):
                    continue
                self._schedule_message_create(websocket, msg)
                continue

            await self._send_json_to(
                websocket,
                self._runtime_error("Protocol message is not supported.", code="protocol.invalid_payload"),
            )

    async def _handle_embedded_session_resume(self, websocket, msg):
        route_by_websocket = getattr(self, "app_session_route_by_websocket", {})
        ok, reason = self._verify_signed_session_resume(
            msg,
            expected_route=route_by_websocket.get(websocket) or "/ripdock/app",
        )
        if not ok:
            logger.warning("RipDock signed session.resume rejected reason=%s", reason)
            app_device_id = str(msg.get("app_device_id") or "") if isinstance(msg, dict) else ""
            if self._record_rate_limit_event("resume_failure", app_device_id or "unknown"):
                logger.warning("RipDock signed session.resume rate limited device_id=%s", app_device_id or "<unknown>")
            if reason in {"session_expired", "session_idle_expired"}:
                await self._send_json_to(
                    websocket,
                    {
                        "type": "session.expired",
                        "protocol_version": PROTOCOL_VERSION,
                        "session_id": str(msg.get("session_id") or "") if isinstance(msg, dict) else "",
                        "reason": "Session has expired.",
                        "code": "session.expired",
                        "connection_security_error": "sessionExpired",
                    },
                )
                await websocket.close(code=1000, reason="Session has expired.")
                return
            await self._send_json_to(
                websocket,
                self._resume_failure_error(reason),
            )
            await websocket.close(code=1000, reason="Session is invalid.")
            return

        await self._replace_embedded_app(websocket)
        self.authenticated_app_websockets.add(websocket)
        app_device_id = str(msg.get("app_device_id") or "").strip()
        self.authenticated_app_device_by_websocket[websocket] = app_device_id
        self.authenticated_app_scopes_by_websocket[websocket] = self._authorization_scopes_for_device(app_device_id)
        await self._send_json_to(
            websocket,
            {
                "type": "session.resumed",
                "protocol_version": PROTOCOL_VERSION,
                "session_id": self.session_id,
                "runtime_id": self.runtime_id,
                "payload": {
                    "authorization_scopes": sorted(self.authenticated_app_scopes_by_websocket.get(websocket, set())),
                },
            },
        )
        await self._emit_runtime_metadata_to(websocket)

    def _verify_signed_session_resume(self, msg, expected_route="/ripdock/app"):
        if not isinstance(msg, dict):
            return False, "malformed"
        if self._reject_unknown_fields(msg, {"type", "protocol_version", "session_id", "runtime_id", "app_device_id", "resume_signature", "last_event_id", "conversation_id"}):
            return False, "malformed"
        if msg.get("type") != "session.resume" or msg.get("protocol_version") != PROTOCOL_VERSION:
            return False, "malformed"
        session_id = str(msg.get("session_id") or "").strip()
        runtime_id = str(msg.get("runtime_id") or "").strip()
        app_device_id = str(msg.get("app_device_id") or "").strip()
        signature = msg.get("resume_signature")
        if isinstance(signature, dict) and self._reject_unknown_fields(signature, {"alg", "key_id", "nonce", "timestamp", "route", "signature"}):
            return False, "signature"
        if not session_id or not runtime_id or not app_device_id or not isinstance(signature, dict):
            return False, "missing_fields"
        if session_id != self.session_id:
            return False, "session"
        if runtime_id != self.runtime_id:
            return False, "runtime"
        expired, expiry_reason = self._session_expiry_reason()
        if expired:
            return False, expiry_reason

        pending, trusted, revoked, _rejected = self._ensure_device_maps()
        if app_device_id in revoked:
            return False, "revoked"
        if app_device_id in pending:
            return False, "pending"
        trusted_entry = trusted.get(app_device_id)
        if not isinstance(trusted_entry, dict):
            return False, "device"
        entry_session_id = str(trusted_entry.get("session_id") or "").strip()
        if entry_session_id and entry_session_id != session_id:
            return False, "session_owner"

        alg = str(signature.get("alg") or "").strip()
        key_id = str(signature.get("key_id") or "").strip()
        nonce = str(signature.get("nonce") or "").strip()
        timestamp = str(signature.get("timestamp") or "").strip()
        route = str(signature.get("route") or "").strip()
        signature_value = str(signature.get("signature") or "").strip()
        if alg != "ES256" or not key_id or not nonce or not timestamp or not route or not signature_value:
            return False, "signature_fields"
        if route != expected_route:
            return False, "route"
        if not self._resume_timestamp_is_fresh(timestamp):
            return False, "timestamp"
        if self._resume_nonce_was_seen(app_device_id, nonce, timestamp):
            return False, "nonce"

        device_identity = trusted_entry.get("deviceIdentity") if isinstance(trusted_entry.get("deviceIdentity"), dict) else {}
        public_key = device_identity.get("publicKey") or trusted_entry.get("publicKey")
        stored_key_id = (
            str(device_identity.get("publicKeyFingerprint") or "").strip()
            or str(trusted_entry.get("publicKeyFingerprint") or "").strip()
            or str(trusted_entry.get("deviceFingerprint") or "").strip()
        )
        if stored_key_id and key_id != stored_key_id:
            return False, "key_id"
        if not isinstance(public_key, dict):
            return False, "public_key"
        if self._p256_jwk_key_id(public_key) != key_id:
            return False, "key_id"
        self._ensure_trusted_authorization_scopes(trusted_entry)

        signed = {
            "app_device_id": app_device_id,
            "key_id": key_id,
            "nonce": nonce,
            "protocol_version": "1",
            "route": route,
            "runtime_id": runtime_id,
            "session_id": session_id,
            "timestamp": timestamp,
            "type": "session.resume",
        }
        signed_bytes = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if not self._verify_es256_signature(public_key, signature_value, signed_bytes):
            return False, "signature"
        self._remember_resume_nonce(app_device_id, nonce, timestamp)
        trusted_entry["lastSeen"] = self._now_iso()
        if self._rotate_session_on_resume():
            self._rotate_session_id()
        else:
            self._touch_session()
        self._save_runtime_identity()
        return True, "verified"

    def _resume_failure_error(self, reason):
        connection_security_error = self._connection_security_error_for_resume_reason(reason)
        code = "session.signature_invalid" if connection_security_error == "invalidSignature" else "session.invalid"
        payload = self._runtime_error("Session is invalid.", code=code)
        if connection_security_error:
            payload["connection_security_error"] = connection_security_error
        return payload

    def _connection_security_error_for_resume_reason(self, reason):
        return {
            "runtime": "runtimeIdentityMismatch",
            "revoked": "runtimeRevokedDevice",
            "pending": "deviceNotTrusted",
            "device": "deviceNotTrusted",
            "session_owner": "deviceNotTrusted",
            "session": "deviceNotTrusted",
            "signature": "invalidSignature",
            "signature_fields": "invalidSignature",
            "key_id": "invalidSignature",
            "public_key": "invalidSignature",
            "timestamp": "staleResumeTimestamp",
            "nonce": "reusedResumeNonce",
            "route": "routeMismatch",
        }.get(str(reason or ""))

    def _resume_timestamp_is_fresh(self, timestamp):
        parsed = self._iso_epoch(timestamp)
        now = self._iso_epoch(self._now_iso())
        if parsed is None or now is None:
            return False
        try:
            window = max(1, int(os.getenv("RIPDOCK_RESUME_TIMESTAMP_WINDOW_SECONDS", "300")))
        except ValueError:
            window = 300
        return abs(now - parsed) <= window

    def _resume_nonce_was_seen(self, app_device_id, nonce, timestamp):
        parsed = self._iso_epoch(timestamp)
        if parsed is None:
            return True
        self._prune_resume_nonces(parsed)
        return (app_device_id, nonce) in self._resume_nonce_seen_at

    def _remember_resume_nonce(self, app_device_id, nonce, timestamp):
        parsed = self._iso_epoch(timestamp)
        if parsed is None:
            return False
        self._prune_resume_nonces(parsed)
        self._resume_nonce_seen_at[(app_device_id, nonce)] = parsed
        return True

    def _prune_resume_nonces(self, now_epoch):
        try:
            window = max(1, int(os.getenv("RIPDOCK_RESUME_TIMESTAMP_WINDOW_SECONDS", "300")))
        except ValueError:
            window = 300
        now = self._iso_epoch(self._now_iso()) or now_epoch
        cutoff = now - window
        for key, seen_at in list(self._resume_nonce_seen_at.items()):
            if seen_at < cutoff:
                self._resume_nonce_seen_at.pop(key, None)

    def _verify_es256_signature(self, public_key, signature_value, signed_bytes):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        from cryptography.hazmat.primitives.hashes import SHA256

        try:
            public_key_bytes = self._p256_public_key_bytes_from_jwk(public_key)
            if public_key_bytes is None:
                return False
            signature_bytes = self._base64_any_decode(signature_value)
            if len(signature_bytes) != 64:
                return False
            verifier_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key_bytes)
            r = int.from_bytes(signature_bytes[:32], "big")
            s = int.from_bytes(signature_bytes[32:], "big")
            verifier_key.verify(utils.encode_dss_signature(r, s), signed_bytes, ec.ECDSA(SHA256()))
            return True
        except InvalidSignature:
            return False
        except Exception as exc:
            logger.warning("RipDock signed session.resume verification failed error=%s", repr(exc))
            return False

    def _p256_public_key_bytes_from_jwk(self, public_key):
        value = public_key if isinstance(public_key, dict) else None
        if value is None:
            return None
        if set(value.keys()) != {"kty", "crv", "x", "y", "key_id"}:
            return None
        if value.get("kty") != "EC" or value.get("crv") != "P-256":
            return None
        try:
            x = self._base64_any_decode(value.get("x"))
            y = self._base64_any_decode(value.get("y"))
        except Exception:
            return None
        key_id = str(value.get("key_id") or "").strip()
        if len(x) != 32 or len(y) != 32 or not re.fullmatch(r"[0-9a-f]{64}", key_id):
            return None
        if hashlib.sha256(x + y).hexdigest() != key_id:
            return None
        return b"\x04" + x + y

    def _p256_jwk_key_id(self, public_key):
        if not isinstance(public_key, dict):
            return ""
        return str(public_key.get("key_id") or "").strip()

    def _valid_p256_jwk_public_key(self, public_key):
        return self._p256_public_key_bytes_from_jwk(public_key) is not None

    def _redacted_session_id(self, session_id):
        return "<redacted>" if isinstance(session_id, str) and session_id else "<none>"

    def _websocket_device_is_revoked(self, websocket):
        app_device_id = getattr(self, "authenticated_app_device_by_websocket", {}).get(websocket)
        if not app_device_id:
            return False
        _pending, _trusted, revoked, _rejected = self._ensure_device_maps()
        return app_device_id in revoked

    def _app_device_id_for_websocket(self, websocket):
        app_device_id = getattr(self, "authenticated_app_device_by_websocket", {}).get(websocket)
        return app_device_id if isinstance(app_device_id, str) and app_device_id.strip() else ""

    def _default_authorization_scopes(self):
        return set(DEFAULT_AUTHORIZATION_SCOPES)

    def _normalize_authorization_scopes(self, scopes):
        if scopes is None:
            return self._default_authorization_scopes()
        if not isinstance(scopes, list):
            return set()
        normalized = set()
        for scope in scopes:
            if isinstance(scope, str):
                value = scope.strip()
                if value:
                    normalized.add(value)
        return normalized

    def _authorization_scopes_from_entry(self, entry):
        if not isinstance(entry, dict):
            return set()
        authorization = entry.get("authorization") if isinstance(entry.get("authorization"), dict) else {}
        scopes = entry.get("authorizationScopes")
        if scopes is None:
            scopes = entry.get("scopes")
        if scopes is None:
            scopes = authorization.get("scopes")
        return self._normalize_authorization_scopes(scopes)

    def _ensure_trusted_authorization_scopes(self, entry):
        if not isinstance(entry, dict):
            return set()
        scopes = self._authorization_scopes_from_entry(entry)
        scopes.update(self._default_authorization_scopes())
        entry["authorizationScopes"] = sorted(scopes)
        authorization = entry.get("authorization") if isinstance(entry.get("authorization"), dict) else {}
        authorization["scopes"] = sorted(scopes)
        entry["authorization"] = authorization
        return scopes

    def _authorization_scopes_for_device(self, app_device_id):
        _pending, trusted, _revoked, _rejected = self._ensure_device_maps()
        entry = trusted.get(app_device_id)
        return self._ensure_trusted_authorization_scopes(entry)

    def _websocket_has_authorization_scope(self, websocket, required_scope):
        scopes = self.authenticated_app_scopes_by_websocket.get(websocket)
        if not isinstance(scopes, set):
            scopes = set(scopes or [])
        return "*" in scopes or required_scope in scopes

    def _authorization_scope_for_message(self, message):
        msg_type = message.get("type") if isinstance(message, dict) else ""
        if msg_type in {"conversation.create", "message.create"}:
            return "message:create"
        if msg_type == "message.cancel":
            return "message:cancel"
        if msg_type == "conversation.list":
            return "conversation:list"
        if msg_type == "conversation.sync":
            return "conversation:sync"
        if msg_type == "conversation.delete":
            return "conversation:delete"
        if msg_type == "conversation.title.generate":
            return "conversation:title:generate"
        if msg_type == "agent.settings.update":
            return "agent:settings:update"
        if msg_type == "runtime.settings.update":
            return "runtime:settings:update"
        if msg_type in {"transfer.request", "transfer.ready", "transfer.completed", "transfer.failed"}:
            return "transfer:app_to_runtime"
        if msg_type in {"runtime.transfer.completed", "runtime.transfer.failed"}:
            return "transfer:runtime_to_app:ack"
        return ""

    def _validate_protocol_message_payload(self, message):
        msg_type = message.get("type") if isinstance(message, dict) else None
        validators = {
            "conversation.create": self._validate_conversation_create_payload,
            "message.create": self._validate_message_create_payload,
            "message.cancel": self._validate_message_cancel_payload,
            "conversation.list": self._validate_conversation_list_payload,
            "conversation.sync": self._validate_conversation_sync_payload,
            "conversation.delete": self._validate_conversation_delete_payload,
            "conversation.title.generate": self._validate_conversation_title_generate_payload,
            "runtime.settings.update": self._validate_runtime_settings_update_payload,
            "agent.settings.update": self._validate_agent_settings_update_payload,
            "transfer.request": self._validate_transfer_request_payload,
            "transfer.ready": self._validate_transfer_ready_payload,
            "transfer.completed": self._validate_transfer_completed_payload,
            "transfer.failed": self._validate_transfer_failed_payload,
            "runtime.transfer.completed": self._validate_runtime_transfer_completed_payload,
            "runtime.transfer.failed": self._validate_runtime_transfer_failed_payload,
        }
        validator = validators.get(msg_type)
        if not validator:
            return None
        reason = validator(message)
        if not reason:
            return None
        conversation_id = message.get("conversation_id") if isinstance(message.get("conversation_id"), str) else None
        logger.warning("RipDock protocol payload rejected type=%s reason=%s", msg_type, reason)
        return self._runtime_error(
            "Protocol payload is invalid.",
            conversation_id=conversation_id,
            code="protocol.invalid_payload",
        )

    def _is_non_empty_string(self, value):
        return isinstance(value, str) and bool(value.strip())

    def _is_non_negative_int(self, value):
        return isinstance(value, int) and value >= 0 and not isinstance(value, bool)

    def _is_positive_int(self, value):
        return isinstance(value, int) and value > 0 and not isinstance(value, bool)

    def _has_protocol_version_v1(self, message):
        return isinstance(message, dict) and message.get("protocol_version") == PROTOCOL_VERSION

    def _payload_dict(self, message):
        payload = message.get("payload") if isinstance(message, dict) else None
        return payload if isinstance(payload, dict) else None

    def _reject_unknown_fields(self, value, allowed):
        if not isinstance(value, dict):
            return "message"
        unknown = set(value.keys()) - set(allowed)
        return "field:" + sorted(unknown)[0] if unknown else None

    def _reject_unknown_payload_fields(self, message, allowed):
        payload = self._payload_dict(message)
        if payload is None:
            return "payload"
        return self._reject_unknown_fields(payload, allowed)

    def _valid_transfer_url(self, value):
        if not self._is_non_empty_string(value):
            return False
        parts = urlsplit(value)
        return parts.scheme == "wss" and bool(parts.netloc)

    def _validate_conversation_create_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "runtime_id", "agent_id", "client_message_id"})
        if reason:
            return reason
        if "payload" in message:
            return "payload"
        if not self._is_non_empty_string(self._message_runtime_id(message)):
            return "runtime_id"
        if not self._is_non_empty_string(self._message_agent_id(message)):
            return "agent_id"
        if not self._is_non_empty_string(message.get("client_message_id")):
            return "client_message_id"
        return None

    def _validate_message_create_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "runtime_id", "agent_id", "client_message_id", "conversation_id", "content", "transfer_ids"})
        if reason:
            return reason
        if "payload" in message:
            return "payload"
        if not self._is_non_empty_string(self._message_runtime_id(message)):
            return "runtime_id"
        if not self._is_non_empty_string(self._message_agent_id(message)):
            return "agent_id"
        if not self._is_non_empty_string(message.get("client_message_id")):
            return "client_message_id"
        if not self._is_non_empty_string(message.get("conversation_id")):
            return "conversation_id"
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return "content"
        transfer_ids = message.get("transfer_ids")
        if transfer_ids is None:
            return None
        if not isinstance(transfer_ids, list) or len(transfer_ids) > 1:
            return "transfer_ids"
        for transfer_id in transfer_ids:
            if not self._is_non_empty_string(transfer_id):
                return "transfer_ids"
            transfer = self.transfers.get(transfer_id)
            if not transfer or not transfer.get("completed"):
                return "transfer_ids"
        return None

    def _validate_message_cancel_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "conversation_id", "message_id"})
        if reason:
            return reason
        if "payload" in message:
            return "payload"
        if not self._is_non_empty_string(message.get("conversation_id")):
            return "conversation_id"
        message_id = message.get("message_id")
        if not self._is_non_empty_string(message_id):
            return "message_id"
        return None

    def _validate_conversation_list_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "runtime_id", "agent_id"})
        if reason:
            return reason
        if "payload" in message:
            return "payload"
        if not self._is_non_empty_string(self._message_runtime_id(message)):
            return "runtime_id"
        if not self._is_non_empty_string(self._message_agent_id(message)):
            return "agent_id"
        return None

    def _validate_conversation_sync_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "runtime_id", "agent_id", "conversation_id", "after"})
        if reason:
            return reason
        if "payload" in message:
            return "payload"
        if not self._is_non_empty_string(self._message_runtime_id(message)):
            return "runtime_id"
        if not self._is_non_empty_string(self._message_agent_id(message)):
            return "agent_id"
        if not self._is_non_empty_string(message.get("conversation_id")):
            return "conversation_id"
        if self._protocol_timestamp_epoch(message.get("after")) is None:
            return "after"
        return None

    def _validate_conversation_title_generate_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "runtime_id", "agent_id", "conversation_id", "messages"})
        if reason:
            return reason
        if "payload" in message:
            return "payload"
        if not self._is_non_empty_string(self._message_runtime_id(message)):
            return "runtime_id"
        if not self._is_non_empty_string(self._message_agent_id(message)):
            return "agent_id"
        if not self._is_non_empty_string(message.get("conversation_id")):
            return "conversation_id"
        messages = message.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 12:
            return "messages"
        for entry in messages:
            if not isinstance(entry, dict):
                return "messages"
            if self._reject_unknown_fields(entry, {"role", "content"}):
                return "messages"
            if entry.get("role") not in {"user", "assistant"}:
                return "messages"
            content = entry.get("content")
            if not isinstance(content, str) or not content.strip() or len(content) > 4000:
                return "messages"
        return None

    def _validate_conversation_delete_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "runtime_id", "agent_id", "conversation_id", "runtime_options"})
        if reason:
            return reason
        if "payload" in message:
            return "payload"
        if not self._is_non_empty_string(self._message_runtime_id(message)):
            return "runtime_id"
        if not self._is_non_empty_string(self._message_agent_id(message)):
            return "agent_id"
        if not self._is_non_empty_string(message.get("conversation_id")):
            return "conversation_id"
        options = message.get("runtime_options")
        if options is None:
            return None
        if not isinstance(options, dict):
            return "runtime_options"
        for namespace, value in options.items():
            if not isinstance(namespace, str) or not re.match(r"^[a-z][a-z0-9_]{0,63}$", namespace):
                return "runtime_options"
            if not isinstance(value, dict):
                return "runtime_options"
        return None

    def _validate_runtime_settings_update_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "runtime_id", "settings", "actions"})
        if reason:
            return reason
        if "payload" in message:
            return "payload"
        if not self._is_non_empty_string(self._runtime_settings_update_runtime_id(message)):
            return "runtime_id"
        settings = message.get("settings")
        actions = message.get("actions")
        if settings is None and actions is None:
            return "settings"
        setting_keys = self._advertised_runtime_setting_keys()
        action_keys = self._advertised_runtime_action_keys()
        if settings is not None:
            reason = self._validate_settings_update_map(settings, setting_keys)
            if reason:
                return reason
        if actions is not None and self._validate_settings_actions(actions, action_keys):
            return "actions"
        return None

    def _validate_agent_settings_update_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "runtime_id", "agent_id", "settings", "actions"})
        if reason:
            return reason
        if "payload" in message:
            return "payload"
        if not self._is_non_empty_string(self._runtime_settings_update_runtime_id(message)):
            return "runtime_id"
        settings = message.get("settings")
        actions = message.get("actions")
        if settings is None and actions is None:
            return "settings"
        if not self._is_non_empty_string(self._agent_settings_update_agent_id(message)):
            return "agent_id"
        agent = self._agent_by_id(self._agent_settings_update_agent_id(message))
        if not agent:
            return "agent_id"
        setting_keys = self._advertised_agent_setting_keys(agent)
        action_keys = self._advertised_agent_action_keys(agent)
        if settings is not None:
            reason = self._validate_settings_update_map(settings, setting_keys)
            if reason:
                return reason
        if actions is not None and self._validate_settings_actions(actions, action_keys):
            return "actions"
        return None

    def _valid_setting_key(self, value):
        return isinstance(value, str) and bool(re.match(r"^[A-Za-z0-9_.:-]+$", value))

    def _setting_keys_from_definitions(self, definitions, action_only=False):
        keys = set()
        if not isinstance(definitions, list):
            return keys
        for definition in definitions:
            if isinstance(definition, dict):
                key = definition.get("key")
                if action_only and definition.get("type") != "action":
                    continue
                if not action_only and definition.get("type") == "action":
                    continue
                if self._valid_setting_key(key):
                    keys.add(key)
        return keys

    def _advertised_runtime_setting_keys(self):
        return self._setting_keys_from_definitions(self._runtime_settings_definitions(self.runtime_id, self.runtime_type))

    def _advertised_runtime_action_keys(self):
        return self._setting_keys_from_definitions(self._runtime_settings_definitions(self.runtime_id, self.runtime_type), action_only=True)

    def _advertised_agent_setting_keys(self, agent):
        return self._setting_keys_from_definitions(agent.get("settings") if isinstance(agent, dict) else None)

    def _advertised_agent_action_keys(self, agent):
        return self._setting_keys_from_definitions(agent.get("settings") if isinstance(agent, dict) else None, action_only=True)

    def _validate_settings_update_map(self, settings, advertised_keys):
        if not isinstance(settings, dict) or not settings:
            return "settings"
        for key in settings.keys():
            if not self._valid_setting_key(key) or key not in advertised_keys:
                return "settings"
        return None

    def _validate_settings_actions(self, actions, advertised_keys):
        if not isinstance(actions, list) or not actions:
            return "actions"
        for action in actions:
            if not self._valid_setting_key(action) or action not in advertised_keys:
                return "actions"
        return None

    def _validate_transfer_request_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "conversation_id", "payload"})
        if reason:
            return reason
        payload = self._payload_dict(message)
        if payload is None:
            return "payload"
        reason = self._reject_unknown_payload_fields(message, {"mime_type", "size_bytes", "filename", "direction"})
        if reason:
            return reason
        if not self._is_non_empty_string(message.get("conversation_id")):
            return "conversation_id"
        if payload.get("mime_type") not in SUPPORTED_TRANSFER_MIME_TYPES:
            return "mime_type"
        if not self._is_positive_int(payload.get("size_bytes")) or payload.get("size_bytes") > MAX_FILE_BYTES:
            return "size_bytes"
        filename = payload.get("filename")
        if filename is not None and not isinstance(filename, str):
            return "filename"
        direction = payload.get("direction")
        if direction is not None and direction not in {"app_to_runtime", "runtime_to_app"}:
            return "direction"
        return None

    def _validate_transfer_ready_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "conversation_id", "payload"})
        if reason:
            return reason
        payload = self._payload_dict(message)
        if payload is None:
            return "payload"
        reason = self._reject_unknown_payload_fields(message, {"transfer_id", "transfer_url", "max_file_bytes", "max_chunk_bytes", "expires_at"})
        if reason:
            return reason
        if not self._is_non_empty_string(message.get("conversation_id")):
            return "conversation_id"
        if not self._is_non_empty_string(payload.get("transfer_id")):
            return "transfer_id"
        if not self._valid_transfer_url(payload.get("transfer_url")):
            return "transfer_url"
        if payload.get("max_file_bytes") != MAX_FILE_BYTES:
            return "max_file_bytes"
        if payload.get("max_chunk_bytes") != MAX_CHUNK_BYTES:
            return "max_chunk_bytes"
        expires_at = payload.get("expires_at")
        if expires_at is not None and not self._valid_protocol_timestamp(expires_at):
            return "expires_at"
        return None

    def _validate_transfer_completed_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "conversation_id", "payload"})
        if reason:
            return reason
        payload = self._payload_dict(message)
        if payload is None:
            return "payload"
        reason = self._reject_unknown_payload_fields(message, {"transfer_id", "size_bytes", "mime_type"})
        if reason:
            return reason
        if not self._is_non_empty_string(message.get("conversation_id")):
            return "conversation_id"
        if not self._is_non_empty_string(payload.get("transfer_id")):
            return "transfer_id"
        if not self._is_positive_int(payload.get("size_bytes")) or payload.get("size_bytes") > MAX_FILE_BYTES:
            return "size_bytes"
        mime_type = payload.get("mime_type")
        if mime_type is not None and mime_type not in SUPPORTED_TRANSFER_MIME_TYPES:
            return "mime_type"
        return None

    def _validate_transfer_failed_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "conversation_id", "payload"})
        if reason:
            return reason
        payload = self._payload_dict(message)
        if payload is None:
            return "payload"
        reason = self._reject_unknown_payload_fields(message, {"message", "code", "transfer_id"})
        if reason:
            return reason
        if not self._is_non_empty_string(message.get("conversation_id")):
            return "conversation_id"
        if not self._is_non_empty_string(payload.get("code")):
            return "code"
        if payload.get("code") not in {
            "transfer.invalid_request",
            "transfer.unsupported_mime_type",
            "transfer.file_too_large",
            "transfer.invalid_chunk",
            "transfer.chunk_too_large",
            "transfer.invalid_completion",
            "transfer.byte_count_mismatch",
            "transfer.missing_completion",
            "transfer.failed",
        }:
            return "code"
        if not self._is_non_empty_string(payload.get("message")):
            return "message"
        transfer_id = payload.get("transfer_id")
        if transfer_id is not None and not self._is_non_empty_string(transfer_id):
            return "transfer_id"
        return None

    def _validate_runtime_transfer_completed_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "conversation_id", "message_id", "payload"})
        if reason:
            return reason
        payload = self._payload_dict(message)
        if payload is None:
            return "payload"
        reason = self._reject_unknown_payload_fields(message, {"transfer_id", "artifact_id", "filename", "mime_type", "size_bytes", "sha256", "source_runtime_id", "source_message_id"})
        if reason:
            return reason
        conversation_id = message.get("conversation_id")
        if conversation_id is not None and not self._is_non_empty_string(conversation_id):
            return "conversation_id"
        message_id = message.get("message_id")
        if message_id is not None and not self._is_non_empty_string(message_id):
            return "message_id"
        if not self._is_non_empty_string(payload.get("transfer_id")):
            return "transfer_id"
        if not self._is_non_empty_string(payload.get("artifact_id")):
            return "artifact_id"
        filename = payload.get("filename")
        if filename is not None and not self._is_non_empty_string(filename):
            return "filename"
        mime_type = payload.get("mime_type")
        if mime_type is not None and not self._is_non_empty_string(mime_type):
            return "mime_type"
        if not self._is_positive_int(payload.get("size_bytes")) or payload.get("size_bytes") > MAX_FILE_BYTES:
            return "size_bytes"
        if not self._valid_sha256(payload.get("sha256")):
            return "sha256"
        source_runtime_id = payload.get("source_runtime_id")
        if source_runtime_id is not None and not self._is_non_empty_string(source_runtime_id):
            return "source_runtime_id"
        source_message_id = payload.get("source_message_id")
        if source_message_id is not None and not self._is_non_empty_string(source_message_id):
            return "source_message_id"
        return None

    def _validate_runtime_transfer_failed_payload(self, message):
        if not self._has_protocol_version_v1(message):
            return "protocol_version"
        reason = self._reject_unknown_fields(message, {"type", "protocol_version", "conversation_id", "message_id", "payload"})
        if reason:
            return reason
        payload = self._payload_dict(message)
        if payload is None:
            return "payload"
        reason = self._reject_unknown_payload_fields(message, {"transfer_id", "artifact_id", "message", "code"})
        if reason:
            return reason
        conversation_id = message.get("conversation_id")
        if conversation_id is not None and not self._is_non_empty_string(conversation_id):
            return "conversation_id"
        message_id = message.get("message_id")
        if message_id is not None and not self._is_non_empty_string(message_id):
            return "message_id"
        if not self._is_non_empty_string(payload.get("transfer_id")):
            return "transfer_id"
        if not self._is_non_empty_string(payload.get("artifact_id")):
            return "artifact_id"
        if not self._is_non_empty_string(payload.get("code")):
            return "code"
        if payload.get("code") not in {
            "runtime.transfer.unknown",
            "runtime.transfer.wrong_mode",
            "runtime.transfer.missing_file",
            "runtime.transfer.invalid_chunk",
            "runtime.transfer.file_too_large",
            "runtime.transfer.size_mismatch",
            "runtime.transfer.timeout",
            "runtime.transfer.failed",
        }:
            return "code"
        if not self._is_non_empty_string(payload.get("message")):
            return "message"
        return None

    def _validate_app_capabilities_payload(self, message):
        if not isinstance(message, dict):
            reason = "message"
        elif set(message.keys()) != {"type", "protocol_version", "payload"}:
            reason = "envelope"
        elif message.get("type") != "app.capabilities" or message.get("protocol_version") != "1":
            reason = "envelope"
        else:
            reason = ""
        payload = self._payload_dict(message)
        if reason:
            pass
        elif payload is None:
            reason = "payload"
        else:
            content_types = payload.get("content_types")
            features = payload.get("features")
            client_capabilities = payload.get("client_capabilities")
            artifact_limits = payload.get("artifact_limits")
            content_rendering = client_capabilities.get("content_rendering") if isinstance(client_capabilities, dict) else None
            rich_text = client_capabilities.get("rich_text_v1") if isinstance(client_capabilities, dict) else None
            required_features = ("streaming", "semantic_blocks", "attachments", "inline_images", "tool_cards", "html")
            allowed_features = set(required_features + ("generated_artifacts", "runtime_transfers", "artifact_http_downloads", "artifact_ack"))
            required_rendering = ("plain_text", "basic_markdown", "rich_text_v1", "json", "yaml", "code_blocks", "external_links")
            required_rich_text = ("bold", "italic", "underline", "inline_code", "code_blocks", "lists", "quotes")
            allowed_payload = {"content_types", "features", "client_capabilities", "app_metadata", "artifact_limits"}
            allowed_artifact_limits = {"max_artifact_bytes", "max_chunk_bytes", "transfer_timeout_seconds"}
            app_metadata = payload.get("app_metadata")
            if set(payload.keys()) - allowed_payload:
                reason = "payload"
            elif not isinstance(content_types, list) or any(not self._is_non_empty_string(item) for item in content_types) or len(content_types) != len(set(content_types)):
                reason = "content_types"
            elif not isinstance(features, dict) or set(features.keys()) - allowed_features or any(not isinstance(features.get(key), bool) for key in required_features):
                reason = "features"
            elif "app_metadata" in payload and (
                not isinstance(app_metadata, dict)
                or set(app_metadata.keys()) - {"selected_runtime_type"}
                or any(not self._is_non_empty_string(value) for value in app_metadata.values())
            ):
                reason = "app_metadata"
            elif not isinstance(content_rendering, dict) or any(not isinstance(content_rendering.get(key), bool) for key in required_rendering):
                reason = "client_capabilities.content_rendering"
            elif set(content_rendering.keys()) != set(required_rendering):
                reason = "client_capabilities.content_rendering"
            elif not isinstance(rich_text, dict) or any(not isinstance(rich_text.get(key), bool) for key in required_rich_text):
                reason = "client_capabilities.rich_text_v1"
            elif set(rich_text.keys()) != set(required_rich_text):
                reason = "client_capabilities.rich_text_v1"
            elif set(client_capabilities.keys()) != {"content_rendering", "rich_text_v1"}:
                reason = "client_capabilities"
            elif "artifact_limits" in payload and (
                not isinstance(artifact_limits, dict)
                or set(artifact_limits.keys()) - allowed_artifact_limits
                or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in artifact_limits.values())
            ):
                reason = "artifact_limits"
        if not reason:
            return None
        logger.warning("RipDock app.capabilities rejected reason=%s", reason)
        return self._runtime_error("Protocol payload is invalid.", code="protocol.invalid_payload")

    async def _reject_unauthorized_message(self, websocket, message, required_scope):
        conversation_id = message.get("conversation_id") if isinstance(message, dict) else None
        msg_type = message.get("type") if isinstance(message, dict) else "<unknown>"
        app_device_id = self.authenticated_app_device_by_websocket.get(websocket) or "<unknown>"
        logger.warning(
            "RipDock authorization rejected type=%s required_scope=%s device_id=%s",
            msg_type,
            required_scope,
            app_device_id,
        )
        await self._send_json_to(
            websocket,
            self._runtime_error(
                "This Device is not allowed to perform that action.",
                code="authorization.denied",
                conversation_id=conversation_id,
            ),
        )

    def _schedule_close_revoked_app_websockets(self, app_device_id):
        targets = [
            websocket
            for websocket, device_id in list(self.authenticated_app_device_by_websocket.items())
            if device_id == app_device_id
        ]
        if not targets:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._close_revoked_app_websockets(targets))

    async def _close_revoked_app_websockets(self, websockets_to_close):
        for websocket in websockets_to_close:
            await self._send_json_to(
                websocket,
                {
                    "type": "session.expired",
                    "protocol_version": PROTOCOL_VERSION,
                        "session_id": self.session_id or "",
                        "reason": "Device trust was revoked.",
                        "code": "session.expired",
                        "connection_security_error": "runtimeRevokedDevice",
                    },
                )
            try:
                await websocket.close(code=1000, reason="Device trust was revoked.")
            except Exception:
                pass

    def _base64_any_decode(self, value):
        raw = str(value or "").strip()
        padded = raw + ("=" * (-len(raw) % 4))
        try:
            return base64.urlsafe_b64decode(padded.encode("ascii"))
        except (binascii.Error, ValueError):
            return base64.b64decode(padded.encode("ascii"))

    def _redact_protocol_log(self, raw):
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            redacted = self._redact_sensitive_json(parsed)
            return json.dumps(redacted, sort_keys=True)
        except Exception:
            return "<unreadable>"

    def _redact_sensitive_json(self, value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]+", "", str(key).lower())
                if any(fragment in normalized for fragment in ("sessionid", "token", "secret", "privatekey", "authorization", "signature", "nonce", "pairingcode", "transferurl", "downloadurl")):
                    result[key] = "<redacted>"
                else:
                    result[key] = self._redact_sensitive_json(item)
            return result
        if isinstance(value, list):
            return [self._redact_sensitive_json(item) for item in value]
        return value

    async def _handle_embedded_transfer_socket(self, websocket, request_path):
        parts = request_path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "ripdock" or parts[1] != "transfer":
            await websocket.close(code=1008, reason="Invalid transfer route.")
            return

        transfer_id = parts[2]
        role = parts[3]
        transfer = self.transfers.get(transfer_id)
        if not transfer:
            await websocket.close(code=1008, reason="Unknown transfer.")
            return

        if transfer.get("direction") == "runtime_to_app":
            await self._handle_embedded_runtime_to_app_transfer_socket(websocket, transfer, role)
            return

        transfer["active"] = True
        transfer_path = self._transfer_file_path(transfer_id)
        transfer_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            completed = False
            with transfer_path.open("wb") as handle:
                async for chunk in websocket:
                    if isinstance(chunk, str):
                        transfer["path"] = str(transfer_path)
                        completion_status = await self._handle_transfer_complete_frame(websocket, transfer, chunk)
                        if completion_status == "complete":
                            completed = True
                            break
                        if completion_status == "failed":
                            return
                        continue

                    result = self._validate_transfer_chunk(transfer, chunk)
                    if result:
                        transfer["failed"] = True
                        transfer["failure"] = result["message"]
                        await self._send_json_to(self._transfer_app_websocket(transfer), result["event"])
                        await websocket.close(code=1009, reason=result["message"])
                        return
                    handle.write(chunk)
                    await self._send_json_to(websocket, self._transfer_chunk_ack(transfer))

            if completed:
                transfer["path"] = str(transfer_path)
                await self._complete_embedded_transfer(transfer, transfer_socket=websocket)
                await websocket.close(code=1000, reason="Transfer complete.")
                return

            transfer["path"] = str(transfer_path)
            transfer["failed"] = True
            transfer["failure"] = "Transfer socket closed without transfer.complete."
            await self._send_json_to(
                self._transfer_app_websocket(transfer),
                self._transfer_failed(
                    transfer.get("conversation_id", ""),
                    "transfer.missing_completion",
                    "Transfer socket closed without transfer.complete.",
                    transfer_id,
                ),
            )
            self._log_transfer_summary(transfer)
        except Exception:
            logger.exception("Embedded transfer socket failed")
            transfer["failed"] = True
            await self._send_json_to(
                self._transfer_app_websocket(transfer),
                self._transfer_failed(
                    transfer.get("conversation_id", ""),
                    "transfer.failed",
                    "Transfer failed.",
                    transfer_id,
                ),
            )

    def _transfer_chunk_ack(self, transfer):
        return {
            "type": "transfer.chunk.ack",
            "protocol_version": PROTOCOL_VERSION,
            "conversation_id": transfer.get("conversation_id", ""),
            "payload": {
                "transfer_id": transfer.get("transfer_id"),
                "received_bytes": transfer.get("received_bytes", 0),
            },
        }

    def _transfer_app_websocket(self, transfer):
        websocket = transfer.get("app_websocket") if isinstance(transfer, dict) else None
        if websocket and not getattr(websocket, "closed", False):
            return websocket
        conversation_id = transfer.get("conversation_id") if isinstance(transfer, dict) else None
        message_id = transfer.get("message_id") if isinstance(transfer, dict) else None
        return self._websocket_for_ripdock_send(conversation_id=conversation_id, message_id=message_id)

    async def _handle_transfer_complete_frame(self, websocket, transfer, chunk):
        try:
            message = json.loads(chunk)
        except Exception:
            await self._fail_embedded_transfer_socket(
                websocket,
                transfer,
                "transfer.invalid_chunk",
                "Transfer text frames must be transfer.complete.",
            )
            return "failed"

        if not isinstance(message, dict) or message.get("type") != "transfer.complete":
            await self._fail_embedded_transfer_socket(
                websocket,
                transfer,
                "transfer.invalid_chunk",
                "Transfer text frames must be transfer.complete.",
            )
            return "failed"

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        transfer_id = payload.get("transfer_id")
        size_bytes = payload.get("size_bytes")
        if transfer_id != transfer.get("transfer_id") or not isinstance(size_bytes, int):
            await self._fail_embedded_transfer_socket(
                websocket,
                transfer,
                "transfer.invalid_completion",
                "Transfer completion frame is invalid.",
            )
            return "failed"

        transfer["size_bytes"] = size_bytes
        return "complete"

    async def _fail_embedded_transfer_socket(self, websocket, transfer, code, message):
        transfer["failed"] = True
        transfer["failure"] = message
        event = self._transfer_failed(
            transfer.get("conversation_id", ""),
            code,
            message,
            transfer.get("transfer_id"),
        )
        await self._send_json_to(self._transfer_app_websocket(transfer), event)
        await self._send_json_to(websocket, event)
        await websocket.close(code=1008, reason=message)

    async def _handle_embedded_runtime_to_app_transfer_socket(self, websocket, transfer, role):
        if role not in {"app", "runtime"}:
            await websocket.close(code=1008, reason="Invalid transfer role.")
            return

        transfer[role] = websocket
        logger.warning(
            "RipDock embedded artifact transfer socket connected transfer_id=%s role=%s",
            transfer.get("transfer_id"),
            role,
        )

        if role == "app":
            for chunk in transfer.get("pending_chunks", []):
                await websocket.send(chunk)
            transfer["pending_chunks"] = []
            try:
                await websocket.wait_closed()
            finally:
                if transfer.get("app") is websocket:
                    transfer["app"] = None
            return

        try:
            async for chunk in websocket:
                result = self._validate_artifact_transfer_chunk(transfer, chunk)
                if result:
                    transfer["failed"] = True
                    transfer["failure"] = result["message"]
                    await websocket.close(code=1009, reason=result["message"])
                    await self._fail_artifact_transfer(transfer, result["code"], result["message"])
                    return

                target = transfer.get("app")
                if target:
                    await target.send(chunk)
                else:
                    transfer.setdefault("pending_chunks", []).append(chunk)
        finally:
            if transfer.get("runtime") is websocket:
                transfer["runtime"] = None

    def _validate_artifact_transfer_chunk(self, transfer, chunk):
        if isinstance(chunk, str):
            return {
                "code": "runtime.transfer.invalid_chunk",
                "message": "Artifact transfer chunks must be binary.",
            }
        chunk_size = len(chunk)
        if chunk_size > MAX_CHUNK_BYTES:
            return {
                "code": "runtime.transfer.file_too_large",
                "message": "Artifact transfer chunk exceeds endpoint maximum size.",
            }
        sent_bytes = transfer.get("received_bytes", 0) + chunk_size
        if sent_bytes > transfer.get("size_bytes", self._max_artifact_bytes()) or sent_bytes > self._max_artifact_bytes():
            return {
                "code": "runtime.transfer.file_too_large",
                "message": "Artifact transfer exceeds endpoint maximum size.",
            }
        transfer["received_bytes"] = sent_bytes
        return None

    async def _handle_message_create(self, websocket, msg):
        user_text = msg.get("content", "")
        conversation_id = msg.get("conversation_id")
        agent_id = self._message_agent_id(msg)
        self._remember_request_websocket(websocket, msg)
        if isinstance(conversation_id, str) and conversation_id:
            if not hasattr(self, "_active_user_text_by_conversation"):
                self._active_user_text_by_conversation = {}
            self._active_user_text_by_conversation[conversation_id] = user_text
        message_id = msg.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            message_id = self._new_message_id()
        self._begin_generation(conversation_id, message_id)
        self._ripdock_message_stream(conversation_id, message_id, websocket=websocket)
        if self._is_ripdock_help_command(user_text):
            logger.warning(
                "RipDock command intercepted platform_profile=ripdock command=/help help_intercepted=true conversation=%s",
                conversation_id,
            )
            await self._send_ripdock_help(websocket, conversation_id)
            return
        if self._is_ripdock_qa_content_command(user_text):
            logger.warning(
                "RipDock command intercepted platform_profile=ripdock command=/qa_content dev_only=true conversation=%s",
                conversation_id,
            )
            await self._send_ripdock_qa_content(websocket, conversation_id)
            return
        qa_transfer_failure_variant = self._ripdock_qa_transfer_failure_variant(user_text)
        if qa_transfer_failure_variant:
            logger.warning(
                "RipDock command intercepted platform_profile=ripdock command=/qa_transfer_failure dev_only=true conversation=%s variant=%s",
                conversation_id,
                qa_transfer_failure_variant["tag"],
            )
            await self._send_ripdock_qa_transfer_failure(websocket, conversation_id, qa_transfer_failure_variant)
            return
        if self._is_ripdock_qa_transfer_failures_command(user_text):
            logger.warning(
                "RipDock command intercepted platform_profile=ripdock command=/qa_transfer_failures dev_only=true conversation=%s",
                conversation_id,
            )
            await self._send_ripdock_qa_transfer_failures(websocket, conversation_id)
            return
        if await self._handle_configured_dev_command(websocket, msg, conversation_id, agent_id, user_text):
            return
        delay_command = self._delay_command(user_text)
        if delay_command:
            delay_ms, delayed_text = delay_command
            logger.warning(
                "RipDock delay command scheduled runtime_id=%s agent_id=%s conversation=%s delay_ms=%s",
                self.runtime_id,
                agent_id,
                conversation_id,
                delay_ms,
            )
            await asyncio.sleep(delay_ms / 1000)
            logger.warning(
                "RipDock delay command dispatching runtime_id=%s agent_id=%s conversation=%s delay_ms=%s",
                self.runtime_id,
                agent_id,
                conversation_id,
                delay_ms,
            )
            delayed_msg = dict(msg)
            delayed_msg["conversation_id"] = conversation_id
            delayed_msg["content"] = f"Reply with exactly this text and no other text: {delayed_text}"
            await self._dispatch_ripdock_agent_message(websocket, delayed_msg, agent_id)
            return
        if self._is_advertised_runtime_slash_command(user_text):
            logger.warning(
                "RipDock slash command dispatch platform_profile=ripdock command=%s conversation=%s",
                self._slash_command_name(user_text),
                conversation_id,
            )
            await self._dispatch_hermes_profile_slash_command(websocket, msg, agent_id)
            return

        logger.warning(
            "RipDock message dispatch platform_profile=ripdock help_intercepted=false runtime_id=%s agent_id=%s conversation=%s",
            self.runtime_id,
            agent_id,
            conversation_id,
        )

        if self.runtime_provider == "stub":
            logger.warning("RipDock stub Runtime received message runtime_id=%s agent_id=%s conversation=%s", self.runtime_id, agent_id, conversation_id)
            await self._send_stub_response(websocket, msg)
            return

        logger.warning("RipDock Hermes Runtime message received runtime_id=%s agent_id=%s conversation=%s", self.runtime_id, agent_id, conversation_id)
        await self._dispatch_ripdock_agent_message(websocket, msg, agent_id)

    async def _handle_conversation_create(self, websocket, msg):
        runtime_id = msg.get("runtime_id") if isinstance(msg.get("runtime_id"), str) else self.runtime_id
        agent_id = self._message_agent_id(msg)
        client_message_id = msg.get("client_message_id")
        receipt_key = (runtime_id, agent_id, client_message_id)
        receipt = getattr(self, "_conversation_create_receipts", {}).get(receipt_key)
        if isinstance(receipt, dict):
            await self._send_json_to(websocket, dict(receipt))
            return

        conversation_id = self._new_runtime_conversation_id()
        profile = self._hermes_profile_for_agent(agent_id)
        session_id = await self._ensure_profile_session_id(agent_id, conversation_id, profile)
        if not session_id:
            await self._send_runtime_failure(
                websocket,
                conversation_id,
                "runtime.unavailable",
                "Runtime could not create this conversation.",
            )
            return

        event = self._conversation_created_event(
            runtime_id,
            agent_id,
            conversation_id,
            client_message_id,
        )
        self._conversation_create_receipts[receipt_key] = dict(event)
        logger.warning(
            "RipDock conversation.create completed runtime_id=%s agent_id=%s conversation=%s",
            runtime_id,
            agent_id,
            conversation_id,
        )
        await self._send_json_to(websocket, event)

    def _new_runtime_conversation_id(self):
        return time.strftime("%Y%m%d_%H%M%S_", time.gmtime()) + uuid.uuid4().hex

    def _schedule_message_create(self, websocket, msg):
        task = asyncio.create_task(self._run_message_create_task(websocket, msg))
        try:
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except Exception:
            pass

    async def _run_message_create_task(self, websocket, msg):
        try:
            await self._handle_message_create(websocket, msg)
        except asyncio.CancelledError:
            logger.warning(
                "RipDock message task cancelled conversation=%s",
                msg.get("conversation_id") if isinstance(msg, dict) else None,
            )
        except Exception:
            logger.exception(
                "RipDock message task failed conversation=%s",
                msg.get("conversation_id") if isinstance(msg, dict) else None,
            )

    def _hermes_profile_for_agent(self, agent_id):
        if not isinstance(agent_id, str) or not agent_id:
            return "default"
        normalized = agent_id.strip().lower()
        if normalized in {"personal", "default"}:
            return "default"
        return normalized

    def _profile_session_state_file_path(self):
        return Path(
            os.getenv(
                "RIPDOCK_PROFILE_SESSIONS_FILE",
                os.path.join(
                    str(_hermes_home()),
                    "ripdock",
                    "profile-sessions.json",
                ),
            )
        )

    def _profile_session_key(self, agent_id, conversation_id):
        return f"{self.runtime_id}:{agent_id or ''}:{conversation_id or ''}"

    def _conversation_title_state_file_path(self):
        return Path(
            os.getenv(
                "RIPDOCK_CONVERSATION_TITLES_FILE",
                os.path.join(
                    str(_hermes_home()),
                    "ripdock",
                    "conversation-titles.json",
                ),
            )
        )

    def _conversation_title_key(self, agent_id, conversation_id):
        return f"{self.runtime_id}:{agent_id or ''}:{conversation_id or ''}"

    def _load_conversation_title_state(self):
        try:
            with self._conversation_title_state_file_path().open() as handle:
                state = json.load(handle)
        except Exception:
            return {}
        return state if isinstance(state, dict) else {}

    def _save_conversation_title_state(self, state):
        state_file = self._conversation_title_state_file_path()
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with state_file.open("w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            try:
                state_file.chmod(0o600)
            except Exception:
                pass
            return True
        except Exception as exc:
            logger.warning("RipDock failed to persist conversation titles path=%s error=%s", state_file, repr(exc))
            return False

    def _cached_conversation_title(self, agent_id, conversation_id, state=None):
        title_state = state if isinstance(state, dict) else self._load_conversation_title_state()
        entry = title_state.get(self._conversation_title_key(agent_id, conversation_id))
        if not isinstance(entry, dict):
            return ""
        title = entry.get("title")
        return title.strip() if isinstance(title, str) and title.strip() else ""

    def _remember_conversation_title(self, agent_id, conversation_id, title):
        if not self._is_non_empty_string(agent_id) or not self._is_non_empty_string(conversation_id):
            return False
        if not isinstance(title, str) or not title.strip():
            return False
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state = self._load_conversation_title_state()
        state[self._conversation_title_key(agent_id, conversation_id)] = {
            "protocol_version": PROTOCOL_VERSION,
            "runtime_id": self.runtime_id,
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "title": title.strip(),
            "updated_at": now,
        }
        return self._save_conversation_title_state(state)

    def _forget_conversation_title(self, agent_id, conversation_id):
        state = self._load_conversation_title_state()
        key = self._conversation_title_key(agent_id, conversation_id)
        if key in state:
            del state[key]
            self._save_conversation_title_state(state)

    def _load_profile_session_state(self):
        try:
            with self._profile_session_state_file_path().open() as handle:
                state = json.load(handle)
        except Exception:
            return {}
        return state if isinstance(state, dict) else {}

    def _save_profile_session_state(self, state):
        state_file = self._profile_session_state_file_path()
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with state_file.open("w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            try:
                state_file.chmod(0o600)
            except Exception:
                pass
            return True
        except Exception as exc:
            logger.warning("RipDock failed to persist Agent profile sessions path=%s error=%s", state_file, repr(exc))
            return False

    def _profile_session_id(self, agent_id, conversation_id):
        entry = self._load_profile_session_state().get(self._profile_session_key(agent_id, conversation_id))
        if not isinstance(entry, dict):
            return ""
        session_id = entry.get("session_id")
        return session_id if isinstance(session_id, str) and session_id.strip() else ""

    def _gateway_session_store(self):
        return getattr(self, "_session_store", None)

    def _gateway_session_key_for_source(self, source):
        if source is None or build_session_key is None:
            return ""
        config_extra = getattr(getattr(self, "config", None), "extra", {})
        if not isinstance(config_extra, dict):
            config_extra = {}
        try:
            return build_session_key(
                source,
                group_sessions_per_user=config_extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=config_extra.get("thread_sessions_per_user", False),
            )
        except Exception as exc:
            logger.warning("RipDock failed to build Hermes session key error=%s", repr(exc))
            return ""

    def _gateway_session_entry_for_key(self, session_key):
        store = self._gateway_session_store()
        if store is None or not self._is_non_empty_string(session_key):
            return None
        try:
            ensure_loaded = getattr(store, "_ensure_loaded", None)
            if callable(ensure_loaded):
                ensure_loaded()
            entries = getattr(store, "_entries", None)
            if isinstance(entries, dict):
                return entries.get(session_key)
        except Exception as exc:
            logger.warning("RipDock failed to inspect Hermes session store key=%s error=%s", session_key, repr(exc))
        return None

    def _gateway_session_state_file_path(self):
        return _hermes_home() / "sessions" / "sessions.json"

    def _gateway_session_id_from_state_file(self, session_key):
        if not self._is_non_empty_string(session_key):
            return ""
        try:
            with self._gateway_session_state_file_path().open() as handle:
                state = json.load(handle)
        except Exception:
            return ""
        if not isinstance(state, dict):
            return ""
        entry = state.get(session_key)
        if not isinstance(entry, dict):
            return ""
        session_id = entry.get("session_id")
        return session_id if isinstance(session_id, str) and session_id.strip() else ""

    def _gateway_session_id_for_source(self, source):
        session_key = self._gateway_session_key_for_source(source)
        entry = self._gateway_session_entry_for_key(session_key)
        session_id = getattr(entry, "session_id", "")
        if isinstance(session_id, str) and session_id.strip():
            return session_id
        return self._gateway_session_id_from_state_file(session_key)

    def _remember_gateway_profile_session_id(self, agent_id, conversation_id, profile, source):
        session_id = self._gateway_session_id_for_source(source)
        if session_id:
            self._remember_profile_session_id(agent_id, conversation_id, profile, session_id)
        return session_id

    def _force_gateway_session_resume(self, agent_id, conversation_id, source):
        target_session_id = self._profile_session_id(agent_id, conversation_id)
        if not target_session_id:
            return False, "missing_profile_session"

        store = self._gateway_session_store()
        if store is None:
            return False, "missing_gateway_session_store"

        session_key = self._gateway_session_key_for_source(source)
        if not session_key:
            return False, "missing_gateway_session_key"

        entry = self._gateway_session_entry_for_key(session_key)
        current_session_id = getattr(entry, "session_id", "") if entry is not None else ""
        if current_session_id == target_session_id:
            return True, "already_resumed"

        if entry is None:
            get_or_create = getattr(store, "get_or_create_session", None)
            if callable(get_or_create):
                try:
                    get_or_create(source)
                except Exception as exc:
                    logger.warning(
                        "RipDock failed to create Hermes session key before resume key=%s target_session=%s error=%s",
                        session_key,
                        target_session_id,
                        repr(exc),
                    )
                    return False, "gateway_session_create_failed"

        switch_session = getattr(store, "switch_session", None)
        if not callable(switch_session):
            return False, "missing_gateway_session_switch"

        try:
            switched = switch_session(session_key, target_session_id)
        except Exception as exc:
            logger.warning(
                "RipDock failed to switch Hermes session key=%s target_session=%s error=%s",
                session_key,
                target_session_id,
                repr(exc),
            )
            return False, "gateway_session_switch_failed"
        switched_session_id = getattr(switched, "session_id", "") if switched is not None else ""
        if switched_session_id != target_session_id:
            return False, "gateway_session_switch_rejected"

        logger.warning(
            "RipDock forced Hermes session resume runtime_id=%s agent_id=%s conversation=%s session_key=%s from_session=%s target_session=%s",
            self.runtime_id,
            agent_id,
            conversation_id,
            session_key,
            current_session_id or "<missing>",
            target_session_id,
        )
        return True, "resumed"

    def _remember_profile_session_id(self, agent_id, conversation_id, profile, session_id):
        if not isinstance(session_id, str) or not session_id.strip():
            return
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            conversation_id = session_id.strip()
        state = self._load_profile_session_state()
        state[self._profile_session_key(agent_id, conversation_id)] = {
            "protocol_version": PROTOCOL_VERSION,
            "runtime_id": self.runtime_id,
            "agent_id": agent_id or "",
            "conversation_id": conversation_id or "",
            "profile": profile or "default",
            "session_id": session_id.strip(),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._save_profile_session_state(state)

    def _conversation_created_event(
        self,
        runtime_id,
        agent_id,
        conversation_id,
        client_message_id=None,
    ):
        return {
            "type": "conversation.created",
            "protocol_version": PROTOCOL_VERSION,
            "runtime_id": runtime_id or self.runtime_id,
            "agent_id": agent_id or "",
            "conversation_id": conversation_id,
            "client_message_id": client_message_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def _forget_profile_session_id(self, agent_id, conversation_id):
        state = self._load_profile_session_state()
        key = self._profile_session_key(agent_id, conversation_id)
        if key in state:
            del state[key]
            self._save_profile_session_state(state)
        self._forget_conversation_title(agent_id, conversation_id)

    def _profile_chat_command(self, profile, content, session_id=None):
        hermes_bin = self._hermes_command()
        if not hermes_bin:
            return None
        command = [
            hermes_bin,
            "-p",
            profile or "default",
            "chat",
            "--quiet",
        ]
        if session_id:
            command.extend(["--resume", session_id])
        command.extend(["-q", content])
        return command

    def _session_id_from_profile_chat_stderr(self, stderr_text):
        if not isinstance(stderr_text, str):
            return ""
        matches = re.findall(r"(?m)^session_id:\s*(\S+)\s*$", stderr_text)
        return matches[-1].strip() if matches else ""

    def _session_id_from_profile_chat_output(self, stdout_text, stderr_text):
        session_id = self._session_id_from_profile_chat_stderr(stderr_text)
        if session_id:
            return session_id
        if not isinstance(stdout_text, str):
            return ""
        matches = re.findall(r"\bSession:\s*([A-Za-z0-9_.:-]+)", stdout_text)
        return matches[-1].strip() if matches else ""

    def _redact_profile_chat_detail(self, detail_text):
        detail = str(detail_text or "")
        detail = re.sub(r"(?im)^(\s*session_id\s*:\s*)\S+", r"\1<redacted>", detail)
        detail = re.sub(
            r"(?i)(\bsession[_-]?id[\"'\s:=]+)(?!<redacted>|redacted|null|none)[A-Za-z0-9._:-]{16,}",
            r"\1<redacted>",
            detail,
        )
        return detail.strip()

    def _profile_chat_session_missing(self, stderr_text, stdout_text=""):
        text = f"{stderr_text or ''}\n{stdout_text or ''}"
        return "Session not found:" in text

    def _plain_text_command_output(self, output):
        if not isinstance(output, str):
            return ""
        return ANSI_ESCAPE_RE.sub("", output).strip()

    def _slash_command_output_is_failure(self, output):
        normalized = self._plain_text_command_output(output).lower()
        return normalized.startswith("unknown command:") or "type /help for available commands" in normalized

    def _empty_slash_command_output(self, content):
        name = self._slash_command_name(content)
        if name == "model":
            return "No model change requested. Use /status to see the active model, or /model <model> [--provider name] to switch models."
        return "Command completed with no output."

    async def _ensure_profile_session_id(self, agent_id, conversation_id, profile):
        session_id = self._profile_session_id(agent_id, conversation_id)
        if session_id:
            return session_id

        result = await self._run_hermes_profile_chat(profile, "", session_id=None)
        if result["returncode"] != 0:
            detail = self._redact_profile_chat_detail(result["stderr"] or result["stdout"] or "")
            logger.error(
                "RipDock Hermes profile session bootstrap failed profile=%s returncode=%s detail=%s",
                profile,
                result["returncode"],
                detail[:1000],
            )
            return ""

        session_id = self._session_id_from_profile_chat_output(result["stdout"], result["stderr"])
        if session_id:
            self._remember_profile_session_id(agent_id, conversation_id, profile, session_id)
        return session_id

    async def _dispatch_ripdock_agent_message(self, websocket, msg, agent_id):
        conversation_id = msg.get("conversation_id")
        message_id = msg.get("message_id") or str(uuid.uuid4())
        msg["message_id"] = message_id
        self._remember_request_websocket(websocket, msg)
        self._remember_outbound_websocket(message_id, websocket)

        hermes_runtime = getattr(self, "hermes_runtime", None)
        if hermes_runtime is None:
            hermes_runtime = HermesRuntime(self)
            self.hermes_runtime = hermes_runtime
        await hermes_runtime.sendMessage(websocket, msg)

    def _remember_request_websocket(self, websocket, msg):
        if not websocket or not isinstance(msg, dict):
            return
        if not hasattr(self, "_app_websocket_by_client_message_id"):
            self._app_websocket_by_client_message_id = {}
        if not hasattr(self, "_app_websocket_by_conversation_id"):
            self._app_websocket_by_conversation_id = {}
        client_message_id = msg.get("client_message_id")
        if isinstance(client_message_id, str) and client_message_id:
            self._app_websocket_by_client_message_id[client_message_id] = websocket
        conversation_id = msg.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            self._app_websocket_by_conversation_id[conversation_id] = websocket

    def _remember_outbound_websocket(self, message_id, websocket):
        if not hasattr(self, "_outbound_websocket_by_message_id"):
            self._outbound_websocket_by_message_id = {}
        if not hasattr(self, "_outbound_app_device_by_message_id"):
            self._outbound_app_device_by_message_id = {}
        if isinstance(message_id, str) and message_id and websocket:
            self._outbound_websocket_by_message_id[message_id] = websocket
            app_device_id = self._app_device_id_for_websocket(websocket)
            if app_device_id:
                self._outbound_app_device_by_message_id[message_id] = app_device_id

    def _ripdock_message_stream(self, conversation_id, message_id, websocket=None):
        if not hasattr(self, "_ripdock_message_streams_by_message_id"):
            self._ripdock_message_streams_by_message_id = {}
        stream = self._ripdock_message_streams_by_message_id.get(message_id)
        if stream:
            stream.attach_websocket(websocket)
            return stream
        stream = RipDockMessageStream(self, conversation_id, message_id, websocket=websocket)
        self._ripdock_message_streams_by_message_id[message_id] = stream
        return stream

    def _stream_for(self, conversation_id, message_id, *, websocket=None, metadata=None):
        message_id = self._message_id_for_stream(conversation_id, message_id)
        websocket = websocket or self._request_websocket_for_metadata(
            metadata=metadata,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        return self._ripdock_message_stream(conversation_id, message_id, websocket=websocket)

    def _message_id_for_stream(self, conversation_id, message_id=None):
        active_message_id = getattr(self, "_active_message_by_conversation", {}).get(conversation_id)
        if (
            isinstance(active_message_id, str)
            and active_message_id
            and active_message_id not in getattr(self, "_completed_message_ids", set())
        ):
            return active_message_id
        if isinstance(message_id, str) and message_id:
            return message_id
        return self._new_message_id()

    def _request_websocket_for_metadata(self, metadata=None, conversation_id=None, message_id=None):
        if isinstance(metadata, dict):
            websocket = metadata.get("ripdock_websocket")
            if websocket:
                return websocket
            client_message_id = metadata.get("ripdock_client_message_id")
            if isinstance(client_message_id, str) and client_message_id:
                websocket = getattr(self, "_app_websocket_by_client_message_id", {}).get(client_message_id)
                if websocket:
                    return websocket
        if isinstance(message_id, str) and message_id:
            app_device_id = getattr(self, "_outbound_app_device_by_message_id", {}).get(message_id)
            websocket = self._authenticated_app_websocket_for_device(app_device_id)
            if websocket:
                return websocket
            websocket = getattr(self, "_outbound_websocket_by_message_id", {}).get(message_id)
            if websocket and not getattr(websocket, "closed", False):
                return websocket
        if isinstance(conversation_id, str) and conversation_id:
            websocket = getattr(self, "_app_websocket_by_conversation_id", {}).get(conversation_id)
            if websocket and not getattr(websocket, "closed", False):
                return websocket
        return None

    async def _run_hermes_profile_chat(self, profile, content, session_id=None):
        command = self._profile_chat_command(profile, content, session_id=session_id)
        if not command:
            return {"returncode": 127, "stdout": "", "stderr": "Hermes CLI is unavailable. Set RIPDOCK_HERMES_BIN or run the plugin inside the Hermes environment."}
        safe_command = [part if index != len(command) - 1 else "<message>" for index, part in enumerate(command)]
        logger.warning(
            "RipDock Hermes profile chat dispatch profile=%s session=%s command=%s",
            profile,
            session_id or "<new>",
            safe_command,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=int(os.getenv("RIPDOCK_HERMES_PROFILE_TIMEOUT", "1800")),
            )
        except asyncio.TimeoutError:
            return {"returncode": 124, "stdout": "", "stderr": "Hermes profile chat timed out."}
        stdout_text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else str(stdout or "")
        stderr_text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr or "")
        return {
            "returncode": proc.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }

    async def _dispatch_hermes_profile_slash_command(self, websocket, msg, agent_id):
        conversation_id = msg.get("conversation_id")
        runtime_id = msg.get("runtime_id") if isinstance(msg.get("runtime_id"), str) else self.runtime_id
        profile = self._hermes_profile_for_agent(agent_id)
        message_id = msg.get("message_id") or str(uuid.uuid4())
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = ""

        session_id = await self._ensure_profile_session_id(agent_id, conversation_id, profile)
        if not session_id:
            await self._send_runtime_failure(
                websocket,
                conversation_id,
                "runtime.unavailable",
                "Runtime command requires an active session.",
            )
            return

        result = await self._run_hermes_profile_slash_command(profile, content, session_id)
        if result["returncode"] != 0 or self._slash_command_output_is_failure(result["stdout"]):
            detail = (result["stderr"] or result["stdout"] or "").strip()
            logger.error(
                "RipDock Hermes profile slash command failed profile=%s command=%s returncode=%s detail=%s",
                profile,
                self._slash_command_name(content),
                result["returncode"],
                detail[:1000],
            )
            await self._send_runtime_failure(websocket, conversation_id, "runtime.unavailable", "Runtime command failed.")
            return

        output = self._plain_text_command_output(result["stdout"]) or self._empty_slash_command_output(content)
        if result["returncode"] == 0 and self._is_ripdock_app_cron_create_command(content):
            job_id = self._cron_job_id_from_command_output(output)
            if job_id:
                self._remember_ripdock_cron_target(
                    job_id,
                    runtime_id=runtime_id,
                    agent_id=agent_id,
                    conversation_id=conversation_id,
                    profile=profile,
                )
        if output:
            await self._stream_for(conversation_id, message_id, websocket=websocket).delta(output, source="slash_command")
        await self._stream_for(conversation_id, message_id, websocket=websocket).complete(source="slash_command")

    async def _run_hermes_profile_slash_command(self, profile, content, session_id):
        worker_path = self._hermes_slash_worker()
        if not worker_path:
            return {"returncode": 127, "stdout": "", "stderr": "Hermes slash worker is unavailable. Set RIPDOCK_HERMES_SLASH_WORKER to enable slash commands."}
        python_bin = self._hermes_python()
        if not python_bin:
            return {"returncode": 127, "stdout": "", "stderr": "Hermes Python is unavailable. Set RIPDOCK_HERMES_PYTHON or run the plugin inside the Hermes environment."}
        request_id = str(uuid.uuid4())
        request = json.dumps({"id": request_id, "command": content})
        env = dict(os.environ)
        if profile:
            env["HERMES_PROFILE"] = profile
        command = [
            python_bin,
            worker_path,
            "--session-key",
            session_id,
        ]
        logger.warning(
            "RipDock Hermes profile slash dispatch profile=%s session=%s command=%s",
            profile,
            self._redacted_session_id(session_id),
            self._slash_command_name(content),
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate((request + "\n").encode("utf-8")),
                timeout=int(os.getenv("RIPDOCK_HERMES_SLASH_TIMEOUT", "120")),
            )
        except asyncio.TimeoutError:
            return {"returncode": 124, "stdout": "", "stderr": "Hermes slash command timed out."}
        stdout_text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else str(stdout or "")
        stderr_text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr or "")
        output = ""
        error = ""
        for line in stdout_text.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") != request_id:
                continue
            if payload.get("ok") is True:
                output = str(payload.get("output") or "")
            else:
                error = str(payload.get("error") or "")
        if error:
            return {"returncode": 1, "stdout": output, "stderr": error or stderr_text}
        return {
            "returncode": proc.returncode,
            "stdout": output or stdout_text,
            "stderr": stderr_text,
        }

    def _is_ripdock_app_cron_create_command(self, content):
        try:
            parts = shlex.split(content or "")
        except ValueError:
            parts = str(content or "").split()
        if len(parts) < 2:
            return False
        return parts[0].strip().lower() == "/cron" and parts[1].strip().lower() in {"add", "create"}

    def _cron_job_id_from_command_output(self, output):
        if not isinstance(output, str):
            return ""
        patterns = [
            r"(?i)\bcreated\s+job:\s*([A-Za-z0-9_-]+)",
            r"(?i)\bjob_id[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_-]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, output)
            if match:
                return match.group(1)
        return ""

    def _ripdock_cron_state_file_path(self):
        return Path(
            os.getenv(
                "RIPDOCK_CRON_STATE_FILE",
                os.path.join(str(_hermes_home()), "ripdock", "cron-targets.json"),
            )
        )

    def _load_ripdock_cron_state(self):
        try:
            with self._ripdock_cron_state_file_path().open() as handle:
                state = json.load(handle)
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        targets = state.get("targets")
        messages = state.get("messages")
        state["targets"] = targets if isinstance(targets, dict) else {}
        state["messages"] = messages if isinstance(messages, list) else []
        return state

    def _save_ripdock_cron_state(self, state):
        state_file = self._ripdock_cron_state_file_path()
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with state_file.open("w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            try:
                state_file.chmod(0o600)
            except Exception:
                pass
            return True
        except Exception as exc:
            logger.warning("RipDock failed to persist cron delivery state path=%s error=%s", state_file, repr(exc))
            return False

    def _remember_ripdock_cron_target(self, job_id, runtime_id, agent_id, conversation_id, profile):
        if not all(isinstance(value, str) and value.strip() for value in [job_id, runtime_id, agent_id, conversation_id]):
            return
        state = self._load_ripdock_cron_state()
        state["targets"][job_id] = {
            "protocol_version": PROTOCOL_VERSION,
            "runtime_id": runtime_id,
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "profile": profile or "default",
            "created_epoch": time.time(),
            "last_delivered_path": "",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._save_ripdock_cron_state(state)
        logger.warning(
            "RipDock cron target registered job_id=%s runtime_id=%s agent_id=%s conversation=%s profile=%s",
            job_id,
            runtime_id,
            agent_id,
            conversation_id,
            profile or "default",
        )

    def _ripdock_cron_targets_for_conversation(self, runtime_id, agent_id, conversation_id):
        state = self._load_ripdock_cron_state()
        matches = {}
        for job_id, target in state.get("targets", {}).items():
            if not isinstance(target, dict):
                continue
            if (
                target.get("runtime_id") == runtime_id
                and target.get("agent_id") == agent_id
                and target.get("conversation_id") == conversation_id
            ):
                matches[job_id] = target
        return matches

    def _profile_home_for_cron_target(self, profile):
        hermes_home = _hermes_home()
        normalized = (profile or "default").strip()
        if not normalized or normalized == "default":
            return hermes_home
        return hermes_home / "profiles" / normalized

    def _cron_output_dir_for_target(self, target, job_id):
        return self._profile_home_for_cron_target(target.get("profile")).joinpath("cron", "output", job_id)

    def _extract_cron_response_from_output(self, text):
        if not isinstance(text, str):
            return ""
        marker = "\n## Response\n"
        if marker in text:
            text = text.split(marker, 1)[1]
        text = text.strip()
        if text.startswith("[SILENT]"):
            return ""
        return text

    def _ripdock_cron_messages_for_sync(self, runtime_id, agent_id, conversation_id, after_epoch):
        state = self._load_ripdock_cron_state()
        messages = []
        for message in state.get("messages", []):
            if not isinstance(message, dict):
                continue
            if (
                message.get("runtime_id") != runtime_id
                or message.get("agent_id") != agent_id
                or message.get("conversation_id") != conversation_id
            ):
                continue
            try:
                epoch = float(message.get("epoch"))
            except Exception:
                continue
            if epoch < after_epoch:
                continue
            content = message.get("content")
            message_id = message.get("message_id")
            if not isinstance(content, str) or not content.strip() or not isinstance(message_id, str) or not message_id:
                continue
            messages.append(
                {
                    "message_id": message_id,
                    "role": "assistant",
                    "content": content,
                    "epoch": epoch,
                }
            )
        return messages

    def _persist_ripdock_cron_message(self, target, job_id, output_path, content):
        state = self._load_ripdock_cron_state()
        message_id = f"hermes-cron:{job_id}:{Path(output_path).stem}"
        if any(isinstance(existing, dict) and existing.get("message_id") == message_id for existing in state.get("messages", [])):
            return message_id
        try:
            epoch = Path(output_path).stat().st_mtime
        except Exception:
            epoch = time.time()
        state["messages"].append(
            {
                "protocol_version": PROTOCOL_VERSION,
                "message_id": message_id,
                "runtime_id": target.get("runtime_id"),
                "agent_id": target.get("agent_id"),
                "conversation_id": target.get("conversation_id"),
                "job_id": job_id,
                "profile": target.get("profile") or "default",
                "content": content,
                "epoch": epoch,
                "created_at": self._protocol_timestamp_from_epoch(epoch),
            }
        )
        state["messages"] = state["messages"][-1000:]
        if job_id in state.get("targets", {}):
            state["targets"][job_id]["last_delivered_path"] = str(output_path)
            state["targets"][job_id]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save_ripdock_cron_state(state)
        return message_id

    def _ensure_ripdock_cron_delivery_watcher(self):
        if self.runtime_provider != "hermes":
            return
        task = self._ripdock_cron_delivery_task
        if task and not task.done():
            return
        task = asyncio.create_task(self._ripdock_cron_delivery_loop())
        self._ripdock_cron_delivery_task = task
        try:
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except Exception:
            pass

    async def _ripdock_cron_delivery_loop(self):
        interval = max(1, int(os.getenv("RIPDOCK_CRON_DELIVERY_POLL_SECONDS", "5")))
        while self._running:
            try:
                await self._deliver_ready_ripdock_cron_outputs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("RipDock cron delivery poll failed")
            await asyncio.sleep(interval)

    async def _deliver_ready_ripdock_cron_outputs(self):
        state = self._load_ripdock_cron_state()
        targets = state.get("targets", {})
        if not targets:
            return
        for job_id, target in list(targets.items()):
            if not isinstance(target, dict):
                continue
            output_dir = self._cron_output_dir_for_target(target, job_id)
            if not output_dir.exists():
                continue
            created_epoch = float(target.get("created_epoch") or 0)
            last_delivered_path = str(target.get("last_delivered_path") or "")
            output_files = sorted(output_dir.glob("*.md"), key=lambda path: str(path))
            if last_delivered_path:
                output_files = [path for path in output_files if str(path) > last_delivered_path]
            ready_files = []
            for output_path in output_files:
                try:
                    if output_path.stat().st_mtime + 0.25 < created_epoch:
                        continue
                except Exception:
                    continue
                ready_files.append(output_path)
            for output_path in ready_files:
                try:
                    content = self._extract_cron_response_from_output(output_path.read_text(errors="replace"))
                except Exception as exc:
                    logger.warning("RipDock cron output read failed path=%s error=%s", output_path, repr(exc))
                    continue
                if not content:
                    state = self._load_ripdock_cron_state()
                    if job_id in state.get("targets", {}):
                        state["targets"][job_id]["last_delivered_path"] = str(output_path)
                        self._save_ripdock_cron_state(state)
                    continue
                message_id = self._persist_ripdock_cron_message(target, job_id, output_path, content)
                await self._broadcast_ripdock_cron_message(target.get("conversation_id"), message_id, content)

    async def _broadcast_ripdock_cron_message(self, conversation_id, message_id, content):
        if not isinstance(conversation_id, str) or not conversation_id or not isinstance(content, str) or not content:
            return
        sockets = list(getattr(self, "authenticated_app_websockets", set()) or [])
        for websocket in sockets:
            stream = self._stream_for(conversation_id, message_id, websocket=websocket)
            await stream.delta(content, source="cron_broadcast")
            await stream.complete(source="cron_broadcast")

    def _remove_ripdock_cron_jobs(self, job_targets):
        removed = 0
        errors = []
        for job_id, target in job_targets.items():
            if self._remove_hermes_cron_job(job_id, target.get("profile") or "default"):
                removed += 1
            else:
                errors.append(job_id)
        state = self._load_ripdock_cron_state()
        for job_id in job_targets.keys():
            state.get("targets", {}).pop(job_id, None)
        self._save_ripdock_cron_state(state)
        if errors:
            logger.warning("RipDock conversation delete could not remove some cron jobs job_ids=%s", errors)
        return removed

    def _remove_hermes_cron_job(self, job_id, profile):
        python_bin = self._hermes_python()
        if not python_bin:
            logger.warning("RipDock cron remove skipped job_id=%s profile=%s reason=missing_hermes_python", job_id, profile)
            return False
        script = (
            "from tools.cronjob_tools import cronjob\n"
            "import sys\n"
            "print(cronjob(action='remove', job_id=sys.argv[1]))\n"
        )
        env = dict(os.environ)
        if profile:
            env["HERMES_PROFILE"] = profile
        try:
            result = subprocess.run(
                [python_bin, "-c", script, job_id],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
        except Exception as exc:
            logger.warning("RipDock cron remove failed job_id=%s profile=%s error=%s", job_id, profile, repr(exc))
            return False
        if result.returncode != 0:
            logger.warning("RipDock cron remove failed job_id=%s profile=%s stderr=%s", job_id, profile, result.stderr.strip())
            return False
        try:
            payload = json.loads(result.stdout)
        except Exception:
            payload = {}
        return bool(payload.get("success", True))

    async def _handle_conversation_list(self, websocket, msg):
        if not await self._validate_agent_routed_conversation_list(websocket, msg):
            return
        runtime_id = self._message_runtime_id(msg)
        agent_id = self._message_agent_id(msg)
        conversations = await asyncio.to_thread(self._conversation_list_summaries, agent_id)
        logger.warning(
            "RipDock conversation.list completed runtime_id=%s agent_id=%s count=%s",
            runtime_id,
            agent_id,
            len(conversations),
        )
        await self._send_json_to(
            websocket,
            {
                "type": "conversation.listed",
                "protocol_version": PROTOCOL_VERSION,
                "runtime_id": runtime_id,
                "agent_id": agent_id,
                "conversations": conversations,
            },
        )

    async def _handle_conversation_delete(self, websocket, msg):
        if not await self._validate_agent_routed_conversation_delete(websocket, msg):
            return
        runtime_id = self._message_runtime_id(msg)
        agent_id = self._message_agent_id(msg)
        conversation_id = msg.get("conversation_id", "")
        targets = self._ripdock_cron_targets_for_conversation(runtime_id, agent_id, conversation_id)
        options = msg.get("runtime_options") if isinstance(msg.get("runtime_options"), dict) else {}
        hermes_options = options.get("hermes") if isinstance(options.get("hermes"), dict) else {}
        confirm_delete_crons = hermes_options.get("confirm_delete_crons") is True

        if targets and not confirm_delete_crons:
            await self._send_json_to(
                websocket,
                {
                    "type": "conversation.delete_blocked",
                    "protocol_version": PROTOCOL_VERSION,
                    "runtime_id": runtime_id,
                    "agent_id": agent_id,
                    "conversation_id": conversation_id,
                    "runtime_namespace": "hermes",
                    "reason": "cron_jobs_exist",
                    "message": "Delete this conversation and its scheduled tasks?",
                },
            )
            return

        deleted_cron_count = 0
        if targets:
            deleted_cron_count = await asyncio.to_thread(self._remove_ripdock_cron_jobs, targets)
        self._forget_profile_session_id(agent_id, conversation_id)
        await self._send_json_to(
            websocket,
            {
                "type": "conversation.deleted",
                "protocol_version": PROTOCOL_VERSION,
                "runtime_id": runtime_id,
                "agent_id": agent_id,
                "conversation_id": conversation_id,
                "runtime_result": {
                    "hermes": {
                        "deleted_cron_count": deleted_cron_count,
                    }
                },
            },
        )

    async def _handle_conversation_sync(self, websocket, msg):
        if not await self._validate_agent_routed_conversation_sync(websocket, msg):
            return

        runtime_id = self._message_runtime_id(msg)
        agent_id = self._message_agent_id(msg)
        conversation_id = msg.get("conversation_id", "")
        after = msg.get("after", "")
        after_epoch = self._protocol_timestamp_epoch(after)
        if after_epoch is None:
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "Protocol payload is invalid.",
                    conversation_id=conversation_id,
                    code="protocol.invalid_payload",
                ),
            )
            return

        messages = await asyncio.to_thread(
            self._conversation_sync_messages,
            agent_id,
            conversation_id,
            after_epoch,
        )
        cron_messages = await asyncio.to_thread(
            self._ripdock_cron_messages_for_sync,
            runtime_id,
            agent_id,
            conversation_id,
            after_epoch,
        )
        messages = sorted(messages + cron_messages, key=lambda message: (message["epoch"], message["message_id"]))
        if messages:
            latest_epoch = max(message["epoch"] for message in messages)
        else:
            latest_epoch = max(after_epoch, time.time())
        event_messages = [
            {
                "message_id": message["message_id"],
                "role": message["role"],
                "content": message["content"],
                "created_at": self._protocol_timestamp_from_epoch(message["epoch"]),
            }
            for message in messages
        ]
        logger.warning(
            "RipDock conversation.sync completed runtime_id=%s agent_id=%s conversation=%s after=%s count=%s",
            runtime_id,
            agent_id,
            conversation_id,
            after,
            len(event_messages),
        )
        await self._send_json_to(
            websocket,
            {
                "type": "conversation.synced",
                "protocol_version": PROTOCOL_VERSION,
                "runtime_id": runtime_id,
                "agent_id": agent_id,
                "conversation_id": conversation_id,
                "after": after,
                "cursor": self._protocol_timestamp_from_epoch(latest_epoch),
                "messages": event_messages,
            },
        )

    async def _handle_conversation_title_generate(self, websocket, msg):
        if not await self._validate_agent_routed_conversation_title_generate(websocket, msg):
            return

        runtime_id = self._message_runtime_id(msg)
        agent_id = self._message_agent_id(msg)
        conversation_id = msg.get("conversation_id", "")
        title = self._generated_conversation_title(msg.get("messages"))
        if not title:
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "Runtime could not generate a title.",
                    conversation_id=conversation_id,
                    code="runtime.unavailable",
                ),
            )
            return
        await asyncio.to_thread(self._remember_conversation_title, agent_id, conversation_id, title)

        logger.warning(
            "RipDock conversation.title.generate completed runtime_id=%s agent_id=%s conversation=%s",
            runtime_id,
            agent_id,
            conversation_id,
        )
        await self._send_json_to(
            websocket,
            {
                "type": "conversation.title.generated",
                "protocol_version": PROTOCOL_VERSION,
                "runtime_id": runtime_id,
                "agent_id": agent_id,
                "conversation_id": conversation_id,
                "title": title,
            },
        )

    def _generated_conversation_title(self, messages):
        if not isinstance(messages, list):
            return ""
        source = ""
        for entry in messages:
            if not isinstance(entry, dict) or entry.get("role") != "user":
                continue
            content = entry.get("content")
            content = self._apply_runtime_visibility_filter(content)
            if isinstance(content, str) and content.strip():
                source = content
                break
        if not source:
            for entry in messages:
                if not isinstance(entry, dict):
                    continue
                content = entry.get("content")
                content = self._apply_runtime_visibility_filter(content)
                if isinstance(content, str) and content.strip():
                    source = content
                    break
        normalized = re.sub(r"\s+", " ", source).strip()
        normalized = re.sub(r"^[`'\"“”‘’\s]+|[`'\"“”‘’\s]+$", "", normalized)
        if not normalized:
            return ""
        sentence = re.split(r"(?<=[.!?])\s+", normalized, maxsplit=1)[0].strip()
        title = sentence[:80].strip()
        title = re.sub(r"[\s,;:.\-!?]+$", "", title).strip()
        if not title:
            return ""
        if len(title) <= 24:
            return title[:1].upper() + title[1:]
        return title

    def _conversation_list_summaries(self, agent_id):
        if self.runtime_provider != "hermes":
            return []
        state = self._load_profile_session_state()
        title_state = self._load_conversation_title_state()
        summaries = []
        seen = set()
        for entry in state.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("runtime_id") != self.runtime_id or entry.get("agent_id") != agent_id:
                continue
            conversation_id = entry.get("conversation_id")
            session_id = entry.get("session_id")
            if not self._is_non_empty_string(conversation_id) or not self._is_non_empty_string(session_id):
                continue
            if conversation_id in seen:
                continue
            seen.add(conversation_id)
            summaries.append(self._conversation_summary_for_session(agent_id, conversation_id, session_id, entry, title_state))
        return sorted(
            summaries,
            key=lambda summary: (
                0 if self._is_non_empty_string(summary.get("updated_at")) else 1,
                -(self._protocol_timestamp_epoch(summary.get("updated_at")) or 0),
                summary["conversation_id"],
            ),
        )

    def _conversation_summary_for_session(self, agent_id, conversation_id, session_id, entry, title_state=None):
        summary = {"conversation_id": conversation_id}
        updated_at = entry.get("updated_at")
        if self._valid_protocol_timestamp(updated_at):
            summary["updated_at"] = updated_at
        cached_title = self._cached_conversation_title(agent_id, conversation_id, title_state)
        if cached_title:
            summary["title"] = cached_title
        messages = self._conversation_summary_messages(session_id)
        if not messages:
            return summary

        first_epoch = min(message["epoch"] for message in messages)
        last_epoch = max(message["epoch"] for message in messages)
        summary["created_at"] = self._protocol_timestamp_from_epoch(first_epoch)
        summary["updated_at"] = self._protocol_timestamp_from_epoch(last_epoch)
        summary["message_count"] = len(messages)
        first_user = next((message for message in messages if message["role"] == "user"), None)
        preview_source = first_user or messages[-1]
        preview = self._conversation_summary_preview(preview_source.get("content", ""))
        if preview:
            summary["preview"] = preview
            if not cached_title:
                summary["title"] = preview[:64]
        return summary

    def _conversation_summary_messages(self, session_id):
        session_db = self._gateway_session_db()
        if session_db is None:
            logger.warning("RipDock conversation.list summary skipped missing Hermes gateway SessionDB session=%s", session_id)
            return []
        try:
            rows = session_db.get_messages(session_id)
        except Exception as exc:
            logger.warning("RipDock conversation.list summary failed session=%s error=%s", session_id, repr(exc))
            return []

        messages = []
        for row in rows:
            role = row.get("role") if isinstance(row, dict) else None
            content = row.get("content") if isinstance(row, dict) else None
            timestamp_value = row.get("timestamp") if isinstance(row, dict) else None
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            content = self._apply_runtime_visibility_filter(content)
            if not content:
                continue
            try:
                epoch = float(timestamp_value)
            except Exception:
                continue
            messages.append({"role": role, "content": content, "epoch": epoch})
        return messages

    def _conversation_summary_preview(self, content):
        if not isinstance(content, str):
            return ""
        return re.sub(r"\s+", " ", content).strip()[:160]

    def _conversation_sync_messages(self, agent_id, conversation_id, after_epoch):
        if self.runtime_provider != "hermes":
            return []
        session_id = self._profile_session_id(agent_id, conversation_id)
        if not session_id and self._is_non_empty_string(conversation_id):
            session_id = conversation_id
        if not session_id:
            return []
        session_db = self._gateway_session_db()
        if session_db is None:
            logger.warning("RipDock conversation.sync skipped missing Hermes gateway SessionDB session=%s", session_id)
            return []
        try:
            rows = session_db.get_messages(session_id)
        except Exception as exc:
            logger.warning("RipDock conversation.sync failed session=%s error=%s", session_id, repr(exc))
            return []

        messages = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = row.get("id")
            role = row.get("role")
            content = row.get("content")
            timestamp_value = row.get("timestamp")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            content = self._apply_runtime_visibility_filter(content)
            if not content:
                continue
            try:
                epoch = float(timestamp_value)
            except Exception:
                continue
            if epoch < float(after_epoch):
                continue
            messages.append(
                {
                    "message_id": f"hermes:{session_id}:{row_id}",
                    "role": role,
                    "content": content,
                    "epoch": epoch,
                }
            )
        return messages

    def _gateway_session_db(self):
        runner = getattr(self, "gateway_runner", None)
        session_db = getattr(runner, "_session_db", None)
        return session_db if session_db is not None and hasattr(session_db, "get_messages") else None

    def _begin_generation(self, conversation_id, message_id=None):
        if not isinstance(conversation_id, str) or not conversation_id:
            return 0
        if not hasattr(self, "_completed_generation_by_conversation"):
            self._completed_generation_by_conversation = {}
        generation = self._active_generation_by_conversation.get(conversation_id, 0) + 1
        self._active_generation_by_conversation[conversation_id] = generation
        self._interrupted_generation_by_conversation.pop(conversation_id, None)
        self._completed_generation_by_conversation.pop(conversation_id, None)
        if isinstance(message_id, str) and message_id:
            self._active_message_by_conversation[conversation_id] = message_id
        self._activity_state_by_conversation.pop(conversation_id, None)
        self._clear_running_activities_for_conversation(conversation_id)
        logger.warning(
            "RipDock generation started conversation=%s generation=%s message_id=%s",
            conversation_id,
            generation,
            message_id,
        )
        return generation

    def _current_generation(self, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id:
            return 0
        return getattr(self, "_active_generation_by_conversation", {}).get(conversation_id, 0)

    def _mark_generation_interrupted(self, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id:
            return 0
        generation = self._current_generation(conversation_id)
        self._interrupted_generation_by_conversation[conversation_id] = generation
        return generation

    def _is_generation_interrupted(self, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id:
            return False
        return getattr(self, "_interrupted_generation_by_conversation", {}).get(conversation_id) == self._current_generation(conversation_id)

    def _mark_generation_completed(self, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id:
            return 0
        generation = self._current_generation(conversation_id)
        if generation:
            self._completed_generation_by_conversation[conversation_id] = generation
        return generation

    def _is_generation_completed(self, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id:
            return False
        return getattr(self, "_completed_generation_by_conversation", {}).get(conversation_id) == self._current_generation(conversation_id)

    async def _send_stub_response(self, websocket, msg):
        user_text = msg.get("content", "")
        conversation_id = msg.get("conversation_id")
        message_id = self._new_message_id()
        stream = self._stream_for(conversation_id, message_id, websocket=websocket)
        await stream.delta(f"Stub Runtime received: {user_text}", source="stub")
        await stream.complete(source="stub")

    async def _send_ripdock_help(self, websocket, conversation_id):
        message_id = self._new_message_id()
        stream = self._stream_for(conversation_id, message_id, websocket=websocket)
        await stream.delta(self._ripdock_help_text(), source="help")
        await stream.complete(source="help")

    def _ripdock_help_text(self):
        lines = ["# RipDock Help", "", "Supported commands:"]
        for command in RIPDOCK_ADVERTISED_SLASH_COMMANDS:
            hint = command.get("argument_hint")
            display = command["display"] if not hint else f"{command['display']} {hint}"
            lines.append(f"- {display} - {command['description']}")
        return "\n".join(lines)

    async def _send_ripdock_qa_content(self, websocket, conversation_id):
        message_id = self._new_message_id()
        capabilities = self._app_capabilities_for_current_session()
        supported_blocks = self._supported_semantic_blocks(capabilities)
        intro = (
            "QA content rendering sample.\n\n"
            "Rich Text v1: **bold**, *italic*, __underline__, `inline code`, https://example.com\n\n"
            "- bullet item\n"
            "- second bullet\n\n"
            "1. numbered item\n"
            "2. second number\n\n"
            "> blockquote"
        )
        stream = self._stream_for(conversation_id, message_id, websocket=websocket)
        await stream.delta(intro, source="qa_content")

        if supported_blocks:
            for block in supported_blocks:
                await stream.block(block, source="qa_content")
        else:
            await stream.delta(
                "\n\nSemantic blocks are not advertised by this App. Plain-text fallback:\n\n"
                + self._plain_text_for_blocks(SEMANTIC_BLOCK_DEMOS),
                source="qa_content",
            )

        await stream.complete(source="qa_content")

    async def _send_ripdock_qa_transfer_failures(self, websocket, conversation_id):
        message_id = self._new_message_id()
        stream = self._stream_for(conversation_id, message_id, websocket=websocket)
        await stream.delta("QA transfer failure variants.", source="qa_transfer_failures")

        for variant in QA_TRANSFER_FAILURE_VARIANTS:
            artifact = self._qa_transfer_failure_artifact(variant, conversation_id, message_id)
            await self._start_qa_transfer_failure(websocket, artifact, variant)

        await stream.complete(artifact_ids=[f"qa-transfer-{variant['tag']}" for variant in QA_TRANSFER_FAILURE_VARIANTS], source="qa_transfer_failures")

    async def _send_ripdock_qa_transfer_failure(self, websocket, conversation_id, variant):
        message_id = self._new_message_id()
        stream = self._stream_for(conversation_id, message_id, websocket=websocket)
        await stream.delta(f"QA transfer failure variant: {variant['tag']}.", source="qa_transfer_failure")
        artifact = self._qa_transfer_failure_artifact(variant, conversation_id, message_id)
        await self._start_qa_transfer_failure(websocket, artifact, variant)
        await stream.complete(artifact_ids=[artifact["artifact_id"]], source="qa_transfer_failure")

    def _is_ripdock_help_command(self, content):
        if not isinstance(content, str):
            return False
        return content.strip().lower() == "/help"

    def _is_ripdock_qa_content_command(self, content):
        if not isinstance(content, str):
            return False
        return content.strip().lower() == "/qa_content" and self._dev_commands_enabled()

    def _is_ripdock_qa_transfer_failures_command(self, content):
        if not isinstance(content, str):
            return False
        return content.strip().lower() == "/qa_transfer_failures" and self._dev_commands_enabled()

    def _ripdock_qa_transfer_failure_variant(self, content):
        if not isinstance(content, str) or not self._dev_commands_enabled():
            return None
        parts = content.strip().lower().split()
        if len(parts) != 2 or parts[0] != "/qa_transfer_failure":
            return None
        return QA_TRANSFER_FAILURE_VARIANTS_BY_TAG.get(parts[1])

    def _is_advertised_runtime_slash_command(self, content):
        name = self._slash_command_name(content)
        return bool(name and name in RIPDOCK_ADVERTISED_SLASH_COMMAND_NAMES)

    def _delay_command(self, content):
        if not isinstance(content, str):
            return None
        parts = content.strip().split(None, 2)
        if len(parts) != 3 or parts[0].lower() != "/delay":
            return None
        try:
            delay_ms = int(parts[1], 10)
        except ValueError:
            return None
        text = parts[2].strip()
        if not text:
            return None
        return min(max(delay_ms, 0), MAX_DELAY_COMMAND_MS), text

    def _slash_command_name(self, content):
        if not isinstance(content, str):
            return ""
        text = content.strip()
        if not text.startswith("/"):
            return ""
        return text.split(None, 1)[0].lstrip("/").strip().lower()

    def _dev_commands_enabled(self):
        explicit = os.getenv("RIPDOCK_DEV_COMMANDS", "").strip().lower()
        if explicit:
            return explicit in {"1", "true", "yes", "on"}
        return os.getenv("RIPDOCK_LOG_LEVEL", "").strip().lower() == "debug"

    def _load_dev_command_module(self):
        module_path = os.getenv("RIPDOCK_DEV_COMMAND_MODULE", "").strip()
        if not module_path:
            return None
        if not self._dev_commands_enabled():
            logger.warning("RipDock dev command module configured but dev commands are disabled path=%s", module_path)
            return None
        path = Path(module_path)
        if not path.is_file():
            logger.warning("RipDock dev command module not found path=%s", module_path)
            return None
        try:
            spec = importlib.util.spec_from_file_location("_ripdock_dev_commands", path)
            if spec is None or spec.loader is None:
                logger.warning("RipDock dev command module failed to create import spec path=%s", module_path)
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            logger.exception("RipDock dev command module failed to load path=%s", module_path)
            return None
        if not callable(getattr(module, "handle_message_create", None)):
            logger.warning("RipDock dev command module missing handle_message_create path=%s", module_path)
            return None
        logger.warning("RipDock dev command module loaded path=%s", module_path)
        return module

    async def _handle_configured_dev_command(self, websocket, msg, conversation_id, agent_id, user_text):
        module = getattr(self, "_dev_command_module", None)
        if module is None:
            return False
        handler = getattr(module, "handle_message_create", None)
        if not callable(handler):
            return False
        result = handler(
            adapter=self,
            websocket=websocket,
            msg=msg,
            conversation_id=conversation_id,
            agent_id=agent_id,
            user_text=user_text,
        )
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)

    def _configured_dev_slash_commands(self):
        module = getattr(self, "_dev_command_module", None)
        if module is None:
            return []
        provider = getattr(module, "runtime_slash_commands", None)
        if not callable(provider):
            return []
        try:
            commands = provider(self)
        except Exception:
            logger.exception("RipDock dev command module slash command provider failed")
            return []
        if not isinstance(commands, list):
            return []
        return [command for command in commands if isinstance(command, dict)]


    def _is_interrupt_event(self, message):
        if not isinstance(message, dict):
            return False
        return message.get("type") == "message.cancel"

    async def _handle_runtime_interrupt(self, websocket, message):
        conversation_id = self._interrupt_conversation_id(message)
        message_id = self._interrupt_message_id(message)
        generation = self._mark_generation_interrupted(conversation_id)
        session_key = self._session_key_for_conversation(conversation_id)
        logger.warning(
            "RipDock interrupt requested conversation=%s message_id=%s generation=%s session_key=%s",
            conversation_id,
            message_id,
            generation,
            self._redacted_session_id(session_key),
        )

        provider_cancel_attempted = False
        provider_cancel_succeeded = False
        provider_cancel_error = None
        if session_key:
            try:
                provider_cancel_attempted = True
                await self.interrupt_session_activity(session_key, conversation_id or "ripdock")
                logger.warning(
                    "RipDock provider cancel attempted conversation=%s session_key=%s",
                    conversation_id,
                    self._redacted_session_id(session_key),
                )
                if hasattr(self, "cancel_session_processing"):
                    await self.cancel_session_processing(
                        session_key,
                        release_guard=True,
                        discard_pending=True,
                    )
                provider_cancel_succeeded = True
                logger.warning(
                    "RipDock provider cancel succeeded conversation=%s session_key=%s",
                    conversation_id,
                    self._redacted_session_id(session_key),
                )
            except Exception as exc:
                provider_cancel_error = repr(exc)
                logger.warning(
                    "RipDock provider cancel failed conversation=%s session_key=%s error=%s",
                    conversation_id,
                    self._redacted_session_id(session_key),
                    provider_cancel_error,
                )

        self._outbound_content_by_message_id.clear()
        self._activity_state_by_conversation.pop(conversation_id or "unknown", None)
        self._clear_running_activities_for_conversation(conversation_id)
        logger.warning(
            "RipDock retries aborted task marked interrupted conversation=%s generation=%s",
            conversation_id,
            generation,
        )
        await self._send_json_to(
            websocket,
            self._runtime_status(
                "interrupted",
                "Generation interrupted. Cancelled by user.",
                conversation_id=conversation_id,
                message_id=message_id,
                payload={
                    "cancelled_by": "user",
                    "generation": generation,
                    "provider_cancel_attempted": provider_cancel_attempted,
                    "provider_cancel_succeeded": provider_cancel_succeeded,
                    "provider_cancel_error": provider_cancel_error,
                },
            ),
        )

    def _interrupt_conversation_id(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        conversation_id = message.get("conversation_id") or payload.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            return conversation_id
        if len(self._active_generation_by_conversation) == 1:
            return next(iter(self._active_generation_by_conversation))
        return self.session_id or "ripdock"

    def _interrupt_message_id(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        message_id = message.get("message_id") or payload.get("message_id")
        return message_id if isinstance(message_id, str) and message_id else ""

    def _session_key_for_conversation(self, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id:
            return ""
        if build_session_key is None or SessionSource is None or Platform is None:
            return conversation_id
        config_extra = getattr(getattr(self, "config", None), "extra", {})
        if not isinstance(config_extra, dict):
            config_extra = {}
        source = SessionSource(
            platform=Platform("ripdock"),
            chat_id=conversation_id or "ripdock",
            chat_name="RipDock",
            chat_type="dm",
            user_id="ripdock-protocol-client",
            user_name="RipDock Client",
            message_id=self._active_message_by_conversation.get(conversation_id, ""),
        )
        return build_session_key(
            source,
            group_sessions_per_user=config_extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=config_extra.get("thread_sessions_per_user", False),
        )

    def _runtime_status(self, status, message, conversation_id=None, message_id=None, payload=None):
        return {
            "type": "runtime.status",
            "protocol_version": PROTOCOL_VERSION,
            "status": status,
            "message": message,
        }

    async def _dispatch_hermes_message(self, websocket, msg, prompt):
        if not getattr(self, "_message_handler", None) or MessageEvent is None or SessionSource is None or Platform is None:
            await self._send_stub_response(websocket, msg)
            return

        conversation_id = msg.get("conversation_id")
        agent_id = self._message_agent_id(msg)
        agent = self._agent_by_id(agent_id) or {}
        agent_name = agent.get("display_name") or self._agent_display_name(agent_id)
        message_id = msg.get("message_id") or str(uuid.uuid4())
        source = SessionSource(
            platform=Platform("ripdock"),
            chat_id=conversation_id or "ripdock",
            chat_name=agent_name,
            chat_type="dm",
            user_id=f"ripdock:{self.runtime_id}:{agent_id}",
            user_name=agent_name,
            message_id=message_id,
        )
        if msg.get("type") == "message.create":
            resumed, reason = self._force_gateway_session_resume(agent_id, conversation_id, source)
            if not resumed:
                logger.error(
                    "RipDock message.create rejected reason=session_resume_failed detail=%s runtime_id=%s agent_id=%s conversation=%s",
                    reason,
                    self.runtime_id,
                    agent_id,
                    conversation_id,
                )
                await self._send_runtime_failure(
                    websocket,
                    conversation_id,
                    "runtime.unavailable",
                    "Runtime could not resume this conversation session.",
                )
                return

        channel_prompt = self._ripdock_channel_prompt(agent_name, agent_id)
        event = MessageEvent(
            text=prompt,
            message_type=self._message_type_for_message(msg),
            source=source,
            raw_message=msg,
            message_id=message_id,
            media_urls=self._media_paths_for_message(msg),
            media_types=self._media_types_for_message(msg),
            channel_prompt=channel_prompt,
        )
        setattr(event, "ripdock_websocket", websocket)
        setattr(event, "ripdock_conversation_id", conversation_id)
        setattr(event, "ripdock_runtime_id", self.runtime_id)
        setattr(event, "ripdock_agent_id", agent_id)
        self._last_ripdock_websocket = websocket

        self._sync_hermes_display_settings()
        await BasePlatformAdapter.handle_message(self, event)
        await self._wait_for_hermes_event_completion(event)
        self._remember_gateway_profile_session_id(
            agent_id,
            conversation_id,
            self._hermes_profile_for_agent(agent_id),
            source,
        )
        await self._complete_ripdock_conversation(websocket, conversation_id)

    def _ripdock_channel_prompt(self, agent_name, agent_id):
        instructions = self._ripdock_runtime_instructions()
        parts = []
        if instructions:
            parts.append(instructions)
        parts.append(f"Active RipDock Agent: {agent_name} ({agent_id}).")
        parts.append(formatting_capability_summary(self.formatting_capabilities_for_current_session()))
        return "\n\n".join(part for part in parts if part)

    def _ripdock_runtime_instructions(self):
        try:
            return RIPDOCK_INSTRUCTIONS_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("RipDock Runtime instructions file unavailable path=%s", RIPDOCK_INSTRUCTIONS_FILE)
            return ""

    async def _wait_for_hermes_event_completion(self, event):
        if build_session_key is None:
            return
        config_extra = getattr(getattr(self, "config", None), "extra", {})
        if not isinstance(config_extra, dict):
            config_extra = {}
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=config_extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=config_extra.get("thread_sessions_per_user", False),
        )
        task = self._session_tasks.get(session_key)
        if task:
            await task

    async def _complete_ripdock_conversation(self, websocket, conversation_id):
        websocket = self._request_websocket_for_metadata(conversation_id=conversation_id) or websocket
        if self._is_generation_completed(conversation_id):
            logger.warning(
                "RipDock completion skipped reason=generation_completed conversation=%s generation=%s",
                conversation_id,
                self._current_generation(conversation_id),
            )
            return
        if self._is_generation_interrupted(conversation_id):
            logger.warning(
                "RipDock completion suppressed reason=interrupted conversation=%s generation=%s",
                conversation_id,
                self._current_generation(conversation_id),
            )
            return
        message_ids = list(
            dict.fromkeys(
                self._message_id_for_stream(conversation_id, message_id)
                for message_id, mapped_conversation_id in self._outbound_conversation_by_message_id.items()
                if mapped_conversation_id == conversation_id
            )
        )
        message_ids = [message_id for message_id in message_ids if message_id not in self._completed_message_ids]
        if not message_ids:
            logger.warning("RipDock completion skipped reason=no_active_stream conversation=%s", conversation_id)
            return

        for message_id in message_ids:
            stream = self._stream_for(conversation_id, message_id, websocket=websocket)
            model_info = self._runtime_model_info_delta()
            if model_info:
                await stream.delta(model_info, source="runtime_model_info")
            await stream.complete(artifact_ids=self._artifact_ids_for_message(message_id), source="conversation_completion")

    async def _send_runtime_failure(self, websocket, conversation_id, code, message):
        message_id = self._active_message_by_conversation.get(conversation_id)
        if isinstance(message_id, str) and message_id:
            stream = self._ripdock_message_stream(conversation_id, message_id, websocket=websocket)
            await stream.fail(code, message)
            return
        await self._send_json_to(websocket, self._runtime_error(message, conversation_id=conversation_id, code=code))

    def _media_paths_for_message(self, msg):
        paths = []
        for transfer in self._transfers_for_message(msg):
            path = transfer.get("path")
            if isinstance(path, str) and path:
                paths.append(path)
        return paths

    def _media_types_for_message(self, msg):
        media_types = []
        for transfer in self._transfers_for_message(msg):
            mime_type = transfer.get("mime_type")
            if isinstance(mime_type, str) and mime_type:
                media_types.append(mime_type)
        return media_types

    def _message_type_for_message(self, msg):
        for transfer in self._transfers_for_message(msg):
            mime_type = str(transfer.get("mime_type") or "").lower()
            filename = str(transfer.get("filename") or "").lower()
            if self._is_document_transfer(mime_type, filename):
                return getattr(MessageType, "DOCUMENT", getattr(MessageType, "TEXT", None))
        return getattr(MessageType, "TEXT", None)

    def _is_document_transfer(self, mime_type, filename):
        if mime_type.startswith(("image/", "audio/", "video/")):
            return False
        if mime_type.startswith(("application/", "text/")):
            return True
        return filename.endswith((
            ".pdf",
            ".txt",
            ".md",
            ".csv",
            ".json",
            ".xml",
            ".yaml",
            ".yml",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".ppt",
            ".pptx",
            ".rtf",
        ))

    def _transfers_for_message(self, msg):
        transfer_ids = msg.get("transfer_ids") if isinstance(msg, dict) else []
        if not isinstance(transfer_ids, list):
            return []
        return [
            self.transfers[transfer_id]
            for transfer_id in transfer_ids
            if isinstance(transfer_id, str) and transfer_id in self.transfers
        ]

    def _message_has_transfer_ids(self, msg):
        transfer_ids = msg.get("transfer_ids") if isinstance(msg, dict) else []
        return isinstance(transfer_ids, list) and any(isinstance(transfer_id, str) and transfer_id for transfer_id in transfer_ids)

    def _embedded_request_path(self, websocket, path):
        if path is not None:
            return path

        request = getattr(websocket, "request", None)
        if request is not None:
            return getattr(request, "path", "/")

        return getattr(websocket, "path", "/")

    async def _send_json_to(self, websocket, message):
        if not websocket:
            return
        payload = json.dumps(message)
        if self._message_size(payload) > self._max_message_bytes():
            logger.error(
                "RipDock outbound message exceeds endpoint policy max_message_bytes=%s type=%s",
                self._max_message_bytes(),
                message.get("type") if isinstance(message, dict) else None,
            )
            return
        try:
            await websocket.send(payload)
        except Exception as exc:
            logger.warning(
                "RipDock outbound send skipped closed websocket type=%s error=%s",
                message.get("type") if isinstance(message, dict) else None,
                exc,
            )

    async def _handle_runtime_settings_update(self, websocket, message):
        runtime_id = self._runtime_settings_update_runtime_id(message)
        settings = self._runtime_settings_update_settings(message)
        actions = self._runtime_settings_update_actions(message)

        logger.warning(
            "RipDock runtime settings received runtime_id=%s settings=%s actions=%s",
            runtime_id,
            settings,
            actions,
        )

        if runtime_id != self.runtime_id:
            logger.warning(
                "RipDock runtime settings ignored runtime_id=%s active_runtime_id=%s",
                runtime_id,
                self.runtime_id,
            )
            return

        self._sync_hermes_display_settings()
        self._save_session_state()

        logger.warning(
            "RipDock runtime settings updated runtime_id=%s values=%s actions=%s",
            runtime_id,
            {},
            actions,
        )
        await self._send_json_to(websocket, self._runtime_settings())

    async def _handle_agent_settings_update(self, websocket, message):
        runtime_id = self._runtime_settings_update_runtime_id(message)
        agent_id = self._agent_settings_update_agent_id(message)
        settings = self._runtime_settings_update_settings(message)
        actions = self._runtime_settings_update_actions(message)

        logger.warning(
            "RipDock Agent settings received runtime_id=%s agent_id=%s settings=%s actions=%s",
            runtime_id,
            agent_id,
            settings,
            actions,
        )

        if runtime_id != self.runtime_id or not self._agent_by_id(agent_id):
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "Unknown Agent.",
                    code="agent.unavailable",
                    conversation_id=None,
                ),
            )
            logger.warning(
                "RipDock Agent settings rejected runtime_id=%s active_runtime_id=%s agent_id=%s",
                runtime_id,
                self.runtime_id,
                agent_id,
            )
            return

        state = self._read_dashboard_state()
        agent_settings = state.setdefault("agentSettings", {})
        if not isinstance(agent_settings, dict):
            agent_settings = {}
            state["agentSettings"] = agent_settings
        runtime_settings = agent_settings.setdefault(self.runtime_id, {})
        if not isinstance(runtime_settings, dict):
            runtime_settings = {}
            agent_settings[self.runtime_id] = runtime_settings
        current = runtime_settings.setdefault(agent_id, {})
        if not isinstance(current, dict):
            current = {}
        for key, value in settings.items():
            if isinstance(key, str):
                current[key] = value
        runtime_settings[agent_id] = current
        self._write_dashboard_state(state)

        logger.warning(
            "RipDock Agent settings updated runtime_id=%s agent_id=%s values=%s actions=%s",
            self.runtime_id,
            agent_id,
            current,
            actions,
        )
        await self._send_json_to(websocket, self._runtime_agents())

    def _runtime_settings_update_runtime_id(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        runtime_id = message.get("runtime_id") if isinstance(message, dict) else None
        if not isinstance(runtime_id, str) or not runtime_id:
            runtime_id = payload.get("runtime_id")
        return runtime_id if isinstance(runtime_id, str) and runtime_id else ""

    def _runtime_settings_update_settings(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        settings = message.get("settings") if isinstance(message, dict) else None
        if not isinstance(settings, dict):
            settings = payload.get("settings")
        return settings if isinstance(settings, dict) else {}

    def _runtime_settings_update_actions(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        actions = message.get("actions") if isinstance(message, dict) else None
        if not isinstance(actions, list):
            actions = payload.get("actions")
        if not isinstance(actions, list):
            return []
        return [action for action in actions if isinstance(action, str)]

    def _agent_settings_update_agent_id(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        agent_id = message.get("agent_id") if isinstance(message, dict) else None
        if not isinstance(agent_id, str) or not agent_id:
            agent_id = payload.get("agent_id")
        return agent_id if isinstance(agent_id, str) and agent_id else ""

    def _message_runtime_id(self, message):
        return self._runtime_settings_update_runtime_id(message)

    def _message_agent_id(self, message):
        return self._agent_settings_update_agent_id(message)

    async def _validate_agent_routed_message_create(self, websocket, message):
        runtime_id = self._message_runtime_id(message)
        agent_id = self._message_agent_id(message)
        conversation_id = message.get("conversation_id") if isinstance(message, dict) else None
        if runtime_id != self.runtime_id:
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "message.create runtime_id is required and must match the active Runtime.",
                    code="message.runtime_mismatch",
                    conversation_id=conversation_id,
                ),
            )
            logger.warning(
                "RipDock message.create rejected reason=runtime_id runtime_id=%s active_runtime_id=%s agent_id=%s conversation=%s",
                runtime_id,
                self.runtime_id,
                agent_id,
                conversation_id,
            )
            return False
        if not self._agent_by_id(agent_id):
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "message.create agent_id is required and must identify an advertised Agent.",
                    code="agent.unavailable",
                    conversation_id=conversation_id,
                ),
            )
            logger.warning(
                "RipDock message.create rejected reason=agent_id runtime_id=%s agent_id=%s conversation=%s",
                runtime_id,
                agent_id,
                conversation_id,
            )
            return False
        return True

    async def _validate_agent_routed_conversation_list(self, websocket, message):
        runtime_id = self._message_runtime_id(message)
        agent_id = self._message_agent_id(message)
        if runtime_id != self.runtime_id:
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "conversation.list runtime_id is required and must match the active Runtime.",
                    code="message.runtime_mismatch",
                ),
            )
            logger.warning(
                "RipDock conversation.list rejected reason=runtime_id runtime_id=%s active_runtime_id=%s agent_id=%s",
                runtime_id,
                self.runtime_id,
                agent_id,
            )
            return False
        if not self._agent_by_id(agent_id):
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "conversation.list agent_id is required and must identify an advertised Agent.",
                    code="agent.unavailable",
                ),
            )
            logger.warning(
                "RipDock conversation.list rejected reason=agent_id runtime_id=%s agent_id=%s",
                runtime_id,
                agent_id,
            )
            return False
        return True

    async def _validate_agent_routed_conversation_sync(self, websocket, message):
        runtime_id = self._message_runtime_id(message)
        agent_id = self._message_agent_id(message)
        conversation_id = message.get("conversation_id") if isinstance(message, dict) else None
        if runtime_id != self.runtime_id:
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "conversation.sync runtime_id is required and must match the active Runtime.",
                    code="message.runtime_mismatch",
                    conversation_id=conversation_id,
                ),
            )
            logger.warning(
                "RipDock conversation.sync rejected reason=runtime_id runtime_id=%s active_runtime_id=%s agent_id=%s conversation=%s",
                runtime_id,
                self.runtime_id,
                agent_id,
                conversation_id,
            )
            return False
        if not self._agent_by_id(agent_id):
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "conversation.sync agent_id is required and must identify an advertised Agent.",
                    code="agent.unavailable",
                    conversation_id=conversation_id,
                ),
            )
            logger.warning(
                "RipDock conversation.sync rejected reason=agent_id runtime_id=%s agent_id=%s conversation=%s",
                runtime_id,
                agent_id,
                conversation_id,
            )
            return False
        return True

    async def _validate_agent_routed_conversation_title_generate(self, websocket, message):
        runtime_id = self._message_runtime_id(message)
        agent_id = self._message_agent_id(message)
        conversation_id = message.get("conversation_id") if isinstance(message, dict) else None
        if runtime_id != self.runtime_id:
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "conversation.title.generate runtime_id is required and must match the active Runtime.",
                    code="message.runtime_mismatch",
                    conversation_id=conversation_id,
                ),
            )
            logger.warning(
                "RipDock conversation.title.generate rejected reason=runtime_id runtime_id=%s active_runtime_id=%s agent_id=%s conversation=%s",
                runtime_id,
                self.runtime_id,
                agent_id,
                conversation_id,
            )
            return False
        if not self._agent_by_id(agent_id):
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "conversation.title.generate agent_id is required and must identify an advertised Agent.",
                    code="agent.unavailable",
                    conversation_id=conversation_id,
                ),
            )
            logger.warning(
                "RipDock conversation.title.generate rejected reason=agent_id runtime_id=%s agent_id=%s conversation=%s",
                runtime_id,
                agent_id,
                conversation_id,
            )
            return False
        return True

    async def _validate_agent_routed_conversation_delete(self, websocket, message):
        runtime_id = self._message_runtime_id(message)
        agent_id = self._message_agent_id(message)
        conversation_id = message.get("conversation_id") if isinstance(message, dict) else None
        if runtime_id != self.runtime_id:
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "conversation.delete runtime_id is required and must match the active Runtime.",
                    code="message.runtime_mismatch",
                    conversation_id=conversation_id,
                ),
            )
            logger.warning(
                "RipDock conversation.delete rejected reason=runtime_id runtime_id=%s active_runtime_id=%s agent_id=%s conversation=%s",
                runtime_id,
                self.runtime_id,
                agent_id,
                conversation_id,
            )
            return False
        if not self._agent_by_id(agent_id):
            await self._send_json_to(
                websocket,
                self._runtime_error(
                    "conversation.delete agent_id is required and must identify an advertised Agent.",
                    code="agent.unavailable",
                    conversation_id=conversation_id,
                ),
            )
            logger.warning(
                "RipDock conversation.delete rejected reason=agent_id runtime_id=%s agent_id=%s conversation=%s",
                runtime_id,
                agent_id,
                conversation_id,
            )
            return False
        return True

    async def _emit_endpoint_policy_to(self, websocket):
        await self._send_json_to(websocket, self._endpoint_policy())

    async def _emit_runtime_metadata_to(self, websocket):
        await self._send_json_to(websocket, self._runtime_identity())
        await self._send_json_to(websocket, self._runtime_capabilities())
        await self._send_json_to(websocket, self._runtime_agents())
        await self._send_json_to(websocket, self._runtime_slash_commands())
        await self._send_json_to(websocket, self._runtime_settings())

    async def _send_semantic_block_demos(self, websocket, conversation_id):
        supported_blocks = self._supported_semantic_blocks()
        message_id = str(uuid.uuid4())
        stream = self._ripdock_message_stream(conversation_id, message_id, websocket=websocket)
        if not supported_blocks:
            await stream.delta(f"\n\n{self._plain_text_for_blocks(SEMANTIC_BLOCK_DEMOS)}", source="semantic_block_demo")
            await stream.complete(source="semantic_block_demo")
            return

        for block in supported_blocks:
            await stream.block(block, source="semantic_block_demo")
        await stream.complete(source="semantic_block_demo")

    def _runtime_identity(self):
        identity = self._public_runtime_identity()
        metadata = self._runtime_metadata()
        event = {
            "type": "runtime.identity",
            "protocol_version": PROTOCOL_VERSION,
            "runtime_id": identity["runtimeId"],
            "runtime_public_key": identity["publicKey"],
            "runtime_public_key_fingerprint": identity["publicKeyFingerprint"],
            "runtime_identity_created_at": identity["createdAt"],
            "runtime_identity": identity,
            "runtime_type": self.runtime_type,
            "display_name": identity["displayName"],
            "background_color": metadata["backgroundColor"],
            "runtime_metadata": metadata,
            "runtime_version": os.getenv("RIPDOCK_RUNTIME_VERSION", "0.1.0"),
        }
        if metadata.get("icon"):
            event["icon"] = metadata["icon"]
        if metadata.get("accentColor"):
            event["accent_color"] = metadata["accentColor"]
        return event

    def _runtime_capabilities(self):
        return {
            "type": "runtime.capabilities",
            "protocol_version": PROTOCOL_VERSION,
            "streaming": True,
            "tools": True,
            "interrupt": True,
            "multimodal": False,
            "attachments": True,
            "background_tasks": False,
            "settings": False,
            "agents": True,
            "agent_settings": True,
            "slash_commands": True,
            "generated_artifacts": True,
            "runtime_transfers": True,
            "artifact_http_downloads": True,
            "artifact_ack": True,
        }

    def _runtime_agents(self):
        agents = self._agent_definitions()
        return {
            "type": "runtime.agents",
            "protocol_version": PROTOCOL_VERSION,
            "runtime_id": self.runtime_id,
            "agents": agents,
        }

    def _runtime_slash_commands(self):
        return {
            "type": "runtime.slash_commands",
            "protocol_version": PROTOCOL_VERSION,
            "runtime_id": self.runtime_id,
            "commands": RIPDOCK_ADVERTISED_SLASH_COMMANDS + self._configured_dev_slash_commands(),
        }

    def _runtime_settings(self):
        settings = self._runtime_settings_definitions(self.runtime_id, self.runtime_type)
        return {
            "type": "runtime.settings",
            "protocol_version": PROTOCOL_VERSION,
            "runtime_id": self.runtime_id,
            "runtime_type": self.runtime_type,
            "display_name": self._runtime_display_name(),
            "settings": settings,
        }

    def _configured_agent_names(self):
        values = self._hermes_profile_names()

        if not values:
            values.append("default")

        seen = set()
        normalized = []
        for value in values:
            agent_id = self._normalize_agent_id(value)
            if agent_id and agent_id not in seen:
                seen.add(agent_id)
                normalized.append(agent_id)
        return normalized or ["default"]

    def _hermes_profile_names(self):
        try:
            result = subprocess.run(
                [self._hermes_command(), "profile", "list"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception as exc:
            logger.warning("RipDock failed to discover Hermes profiles error=%s", repr(exc))
            return []
        if result.returncode != 0:
            logger.warning("RipDock Hermes profile discovery failed status=%s stderr=%s", result.returncode, result.stderr.strip())
            return []
        names = []
        for line in result.stdout.splitlines():
            fields = line.strip().split()
            if not fields:
                continue
            name = fields[0].lstrip("◆*")
            if name and name.lower() not in {"profile", "───────────────"} and not set(name) <= {"─"}:
                names.append(name)
        return names

    def _hermes_command(self):
        configured = os.getenv("RIPDOCK_HERMES_BIN", "").strip()
        if configured:
            return configured
        discovered = shutil.which("hermes")
        if discovered:
            return discovered
        candidate = Path(sys.executable).with_name("hermes")
        if candidate.exists():
            return str(candidate)
        return ""

    def _hermes_python(self):
        configured = os.getenv("RIPDOCK_HERMES_PYTHON", "").strip()
        if configured:
            return configured
        hermes_bin = self._hermes_command()
        if hermes_bin:
            candidate = Path(hermes_bin).with_name("python")
            if candidate.exists():
                return str(candidate)
            candidate = Path(hermes_bin).with_name("python3")
            if candidate.exists():
                return str(candidate)
        executable = Path(sys.executable)
        if executable.exists():
            return str(executable)
        return ""

    def _hermes_slash_worker(self):
        configured = os.getenv("RIPDOCK_HERMES_SLASH_WORKER", "").strip()
        if configured:
            path = Path(configured)
            return str(path) if path.is_file() else ""
        candidates = []
        hermes_python = self._hermes_python()
        if hermes_python:
            venv_bin = Path(hermes_python).resolve().parent
            venv_root = venv_bin.parent
            install_root = venv_root.parent
            candidates.extend(
                [
                    install_root / "tui_gateway" / "slash_worker.py",
                    venv_root / "tui_gateway" / "slash_worker.py",
                ]
            )
        candidates.append(Path(sys.executable).resolve().parents[1] / "tui_gateway" / "slash_worker.py")
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return ""

    def _normalize_agent_id(self, value):
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9_.:-]+", "-", text)
        text = text.strip("-")
        return text[:80]

    def _agent_display_name(self, agent_id):
        if not isinstance(agent_id, str) or not agent_id:
            return "Agent"
        return " ".join(part.capitalize() for part in re.split(r"[-_.:]+", agent_id) if part) or agent_id

    def _default_agent_metadata(self, agent_id, index):
        accents = ["#2563eb", "#0f766e", "#7c3aed", "#dc2626", "#ea580c", "#16a34a", "#0891b2", "#4f46e5"]
        backgrounds = ["#dbeafe", "#ccfbf1", "#ede9fe", "#fee2e2", "#ffedd5", "#dcfce7", "#cffafe", "#e0e7ff"]
        return {
            "agent_id": agent_id,
            "display_name": self._agent_display_name(agent_id),
            "icon": "🤖",
            "accent_color": accents[index % len(accents)],
            "background_color": backgrounds[index % len(backgrounds)],
            "sort_order": index,
            "settings": [
                {
                    "key": "show_activity",
                    "label": "Show Activity",
                    "type": "boolean",
                    "default": True,
                }
            ],
        }

    def _read_dashboard_state(self):
        try:
            with self._runtime_metadata_state_file_path().open() as handle:
                state = json.load(handle)
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def _write_dashboard_state(self, state):
        try:
            path = self._runtime_metadata_state_file_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except Exception as exc:
            logger.warning("RipDock failed to persist dashboard state path=%s error=%s", self._runtime_metadata_state_file_path(), repr(exc))

    def _agent_metadata_records(self):
        state = self._read_dashboard_state()
        records = state.get("agentMetadata")
        if isinstance(records, dict):
            runtime_records = records.get(self.runtime_id)
            if isinstance(runtime_records, dict):
                return runtime_records
        return {}

    def _agent_setting_values(self, agent_id):
        state = self._read_dashboard_state()
        records = state.get("agentSettings")
        if not isinstance(records, dict):
            return {}
        runtime_records = records.get(self.runtime_id)
        if not isinstance(runtime_records, dict):
            return {}
        values = runtime_records.get(agent_id)
        return values if isinstance(values, dict) else {}

    def _agent_definitions(self):
        records = self._agent_metadata_records()
        agents = []
        for index, agent_id in enumerate(self._configured_agent_names()):
            defaults = self._default_agent_metadata(agent_id, index)
            stored = records.get(agent_id)
            if not isinstance(stored, dict):
                stored = {}
            if stored.get("enabled") is False:
                continue
            agent = dict(defaults)
            for key in ("display_name", "icon", "accent_color", "background_color", "sort_order"):
                value = stored.get(key)
                if value is not None and value != "":
                    if key == "icon":
                        emoji_icon = _emoji_icon_or_empty(value)
                        if emoji_icon:
                            agent[key] = emoji_icon
                    else:
                        agent[key] = value
            agent["settings"] = stored.get("settings") if isinstance(stored.get("settings"), list) else defaults["settings"]
            agents.append(agent)
        agents.sort(key=lambda item: (item.get("sort_order") if isinstance(item.get("sort_order"), int) else 9999, item.get("display_name") or item.get("agent_id")))
        return agents

    def _agent_by_id(self, agent_id):
        if not isinstance(agent_id, str) or not agent_id:
            return None
        for agent in self._agent_definitions():
            if agent.get("agent_id") == agent_id:
                return agent
        return None

    def _configured_runtime_type(self):
        raw_value = os.getenv("RIPDOCK_RUNTIME_TYPE", "hermes")
        runtime_type = str(raw_value or "hermes").strip().lower()
        return runtime_type if runtime_type in {"hermes", "openclaw", "custom"} else "custom"

    def _configured_runtime_id(self):
        configured = os.getenv("RIPDOCK_RUNTIME_ID", "").strip()
        if configured:
            return configured
        return str(uuid.uuid4())

    def _runtime_identity_file_path(self):
        return Path(
            os.getenv(
                "RIPDOCK_RUNTIME_IDENTITY_FILE",
                os.path.join(
                    str(_hermes_home()),
                    "ripdock",
                    "runtime-identity.json",
                ),
            )
        )

    def _runtime_metadata_state_file_path(self):
        return Path(
            os.getenv(
                "RIPDOCK_DASHBOARD_STATE_FILE",
                os.path.join(
                    str(_hermes_home()),
                    "ripdock",
                    "dashboard-state.json",
                ),
            )
        )

    def _save_runtime_identity(self):
        identity_file = self._runtime_identity_file_path()
        try:
            identity_file.parent.mkdir(parents=True, exist_ok=True)
            with identity_file.open("w") as handle:
                json.dump(self.runtime_identity, handle, indent=2, sort_keys=True)
                handle.write("\n")
            try:
                identity_file.chmod(0o600)
            except Exception:
                pass
            return True
        except Exception as exc:
            logger.warning("RipDock failed to persist Runtime identity path=%s error=%s", identity_file, repr(exc))
            return False

    def _http_request_json_body(self, path, request_headers):
        candidates = []
        for obj in (path, request_headers):
            for attr in ("body", "data", "content"):
                value = getattr(obj, attr, None)
                if value:
                    candidates.append(value)
        for header_name in ("x-ripdock-body", "x-ripdock-body"):
            try:
                value = request_headers.get(header_name)
            except Exception:
                value = None
            if value:
                candidates.append(value)
        for value in candidates:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            if isinstance(value, str):
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
        return {}

    def _ensure_device_maps(self):
        pending = self.runtime_identity.setdefault("pendingDevices", {})
        trusted = self.runtime_identity.setdefault("trustedDevices", {})
        revoked = self.runtime_identity.setdefault("revokedDevices", {})
        rejected = self.runtime_identity.setdefault("rejectedDevices", {})
        if not isinstance(pending, dict):
            pending = {}
            self.runtime_identity["pendingDevices"] = pending
        if not isinstance(trusted, dict):
            trusted = {}
            self.runtime_identity["trustedDevices"] = trusted
        if not isinstance(revoked, dict):
            revoked = {}
            self.runtime_identity["revokedDevices"] = revoked
        if not isinstance(rejected, dict):
            rejected = {}
            self.runtime_identity["rejectedDevices"] = rejected
        return pending, trusted, revoked, rejected

    def _now_iso(self):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _rejected_pairing_ttl_seconds(self):
        try:
            return max(0, int(os.getenv("RIPDOCK_REJECTED_PAIRING_TTL_SECONDS", str(DEFAULT_REJECTED_PAIRING_TTL_SECONDS))))
        except ValueError:
            return DEFAULT_REJECTED_PAIRING_TTL_SECONDS

    def _iso_epoch(self, value):
        try:
            text = str(value or "")
            if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+Z$", text):
                text = text.split(".", 1)[0] + "Z"
            return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            return None

    def _protocol_timestamp_epoch(self, value):
        if not self._valid_protocol_timestamp(value):
            return None
        match = re.match(
            r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?Z$",
            str(value),
        )
        if not match:
            return None
        try:
            epoch = calendar.timegm(time.strptime(match.group(1) + "Z", "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            return None
        fraction = match.group(2) or ""
        if fraction:
            epoch += int(fraction.ljust(9, "0")) / 1_000_000_000
        return epoch

    def _valid_protocol_timestamp(self, value):
        return isinstance(value, str) and re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$", value) is not None

    def _valid_sha256(self, value):
        return isinstance(value, str) and re.match(r"^[a-f0-9]{64}$", value) is not None

    def _is_rejected_entry_queryable(self, entry):
        rejected_at = self._iso_epoch(entry.get("rejectedAt") if isinstance(entry, dict) else "")
        now = self._iso_epoch(self._now_iso())
        if rejected_at is None or now is None:
            return True
        return now - rejected_at <= self._rejected_pairing_ttl_seconds()

    def _iso_after(self, seconds):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + max(0, int(seconds))))

    def _iso_from_epoch(self, epoch):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(0, int(epoch))))

    def _protocol_timestamp_from_epoch(self, epoch):
        try:
            value = max(0.0, float(epoch))
        except Exception:
            value = 0.0
        seconds = int(value)
        nanos = int(round((value - seconds) * 1_000_000_000))
        if nanos >= 1_000_000_000:
            seconds += 1
            nanos -= 1_000_000_000
        base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
        if nanos <= 0:
            return base + "Z"
        fraction = f"{nanos:09d}".rstrip("0")
        return f"{base}.{fraction}Z"

    def _session_policy_seconds(self, env_key, default):
        try:
            value = int(os.getenv(env_key, str(default)))
        except ValueError:
            return default
        return max(MIN_SESSION_LIFETIME_SECONDS, value)

    def _session_idle_timeout_seconds(self):
        return self._session_policy_seconds("RIPDOCK_SESSION_IDLE_TIMEOUT_SECONDS", DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS)

    def _session_absolute_lifetime_seconds(self):
        return self._session_policy_seconds("RIPDOCK_SESSION_ABSOLUTE_LIFETIME_SECONDS", DEFAULT_SESSION_ABSOLUTE_LIFETIME_SECONDS)

    def _rotate_session_on_resume(self):
        value = os.getenv("RIPDOCK_ROTATE_SESSION_ON_RESUME", "true").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _reset_session_lifecycle(self):
        now = self._now_iso()
        now_epoch = self._iso_epoch(now) or time.time()
        self.session_created_at = now
        self.session_last_seen_at = now
        self.session_expires_at = self._iso_from_epoch(now_epoch + self._session_absolute_lifetime_seconds())
        self.session_idle_expires_at = self._iso_from_epoch(now_epoch + self._session_idle_timeout_seconds())

    def _restore_session_lifecycle(self, saved):
        session_state = saved.get("session") if isinstance(saved, dict) else {}
        if not isinstance(session_state, dict):
            session_state = {}
        now = self._now_iso()
        now_epoch = self._iso_epoch(now) or time.time()
        created_at = str(session_state.get("createdAt") or saved.get("session_created_at") or "").strip()
        last_seen_at = str(session_state.get("lastSeenAt") or saved.get("session_last_seen_at") or "").strip()
        expires_at = str(session_state.get("expiresAt") or saved.get("session_expires_at") or "").strip()
        idle_expires_at = str(session_state.get("idleExpiresAt") or saved.get("session_idle_expires_at") or "").strip()
        if self._iso_epoch(created_at) is None:
            created_at = now
        if self._iso_epoch(last_seen_at) is None:
            last_seen_at = now
        created_epoch = self._iso_epoch(created_at) or now_epoch
        last_seen_epoch = self._iso_epoch(last_seen_at) or now_epoch
        if self._iso_epoch(expires_at) is None:
            expires_at = self._iso_from_epoch(created_epoch + self._session_absolute_lifetime_seconds())
        if self._iso_epoch(idle_expires_at) is None:
            idle_expires_at = self._iso_from_epoch(last_seen_epoch + self._session_idle_timeout_seconds())
        self.session_created_at = created_at
        self.session_last_seen_at = last_seen_at
        self.session_expires_at = expires_at
        self.session_idle_expires_at = idle_expires_at

    def _session_expiry_reason(self):
        if not isinstance(self.session_id, str) or not self.session_id:
            return True, "session"
        if not self.session_created_at or not self.session_expires_at or not self.session_idle_expires_at:
            self._reset_session_lifecycle()
            self._save_session_state()
        now = self._iso_epoch(self._now_iso())
        absolute_expiry = self._iso_epoch(self.session_expires_at)
        idle_expiry = self._iso_epoch(self.session_idle_expires_at)
        if now is not None and absolute_expiry is not None and absolute_expiry <= now:
            return True, "session_expired"
        if now is not None and idle_expiry is not None and idle_expiry <= now:
            return True, "session_idle_expired"
        return False, "active"

    def _touch_session(self):
        now = self._now_iso()
        now_epoch = self._iso_epoch(now) or time.time()
        self.session_last_seen_at = now
        self.session_idle_expires_at = self._iso_from_epoch(now_epoch + self._session_idle_timeout_seconds())
        self._save_session_state()

    def _rotate_session_id(self):
        old_session_id = self.session_id
        self.session_id = self._create_session_id()
        self._reset_session_lifecycle()
        self._replace_session_id_in_trusted_devices(old_session_id, self.session_id)
        self._save_session_state()

    def _replace_session_id_in_trusted_devices(self, old_session_id, new_session_id):
        if not old_session_id or not new_session_id:
            return
        _pending, trusted, _revoked, _rejected = self._ensure_device_maps()
        for entry in trusted.values():
            if isinstance(entry, dict) and str(entry.get("session_id") or "").strip() == old_session_id:
                entry["session_id"] = new_session_id

    def _clear_session_id_from_trusted_devices(self, session_id):
        if not session_id:
            return
        _pending, trusted, _revoked, _rejected = self._ensure_device_maps()
        changed = False
        for entry in trusted.values():
            if isinstance(entry, dict) and str(entry.get("session_id") or "").strip() == session_id:
                entry.pop("session_id", None)
                changed = True
        if changed:
            self._save_runtime_identity()

    def _is_expired_entry(self, entry):
        expires_at = self._iso_epoch(entry.get("expiresAt") if isinstance(entry, dict) else "")
        now = self._iso_epoch(self._now_iso())
        return expires_at is not None and now is not None and expires_at <= now

    def _rate_limit_window_seconds(self):
        try:
            return max(1, int(os.getenv("RIPDOCK_RATE_LIMIT_WINDOW_SECONDS", str(DEFAULT_RATE_LIMIT_WINDOW_SECONDS))))
        except ValueError:
            return DEFAULT_RATE_LIMIT_WINDOW_SECONDS

    def _rate_limit_limit(self, category):
        env_names = {
            "pairing_request": ("RIPDOCK_PAIRING_REQUEST_RATE_LIMIT", DEFAULT_PAIRING_REQUEST_RATE_LIMIT),
            "pairing_status": ("RIPDOCK_PAIRING_STATUS_RATE_LIMIT", DEFAULT_PAIRING_STATUS_RATE_LIMIT),
            "pairing_code": ("RIPDOCK_PAIRING_CODE_RATE_LIMIT", DEFAULT_PAIRING_CODE_RATE_LIMIT),
            "resume_failure": ("RIPDOCK_RESUME_FAILURE_RATE_LIMIT", DEFAULT_RESUME_FAILURE_RATE_LIMIT),
            "message_burst": ("RIPDOCK_MESSAGE_BURST_RATE_LIMIT", DEFAULT_MESSAGE_BURST_RATE_LIMIT),
        }
        env_name, default = env_names.get(category, ("RIPDOCK_RATE_LIMIT", 60))
        try:
            return max(1, int(os.getenv(env_name, str(default))))
        except ValueError:
            return default

    def _record_rate_limit_event(self, category, identifier):
        state = getattr(self, "_rate_limit_events", None)
        if not isinstance(state, dict):
            state = {}
            self._rate_limit_events = state
        now = time.time()
        window = self._rate_limit_window_seconds()
        limit = self._rate_limit_limit(category)
        key = (str(category or "unknown"), str(identifier or "unknown"))
        events = [event_at for event_at in state.get(key, []) if now - event_at <= window]
        limited = len(events) >= limit
        events.append(now)
        state[key] = events[-max(limit, 1):]
        if limited:
            logger.warning(
                "RipDock rate limit exceeded category=%s identifier=%s window_seconds=%s limit=%s",
                key[0],
                key[1],
                window,
                limit,
            )
        return limited

    def _device_state(self, pending, trusted, revoked, device_id):
        if device_id in trusted:
            return "trusted"
        if device_id in pending:
            return "pendingApproval"
        if device_id in revoked:
            return "revoked"
        return "unknown"

    def _log_pairing_transition(self, action, device_id, previous_state, next_state, pending, trusted):
        logger.info(
            "RipDock pairing state transition action=%s device_id=%s previous_state=%s next_state=%s pending_count=%s trusted_count=%s",
            action,
            device_id,
            previous_state,
            next_state,
            len(pending),
            len(trusted),
        )

    def _device_identity_from_pairing_request(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        source = payload.get("deviceIdentity") if isinstance(payload.get("deviceIdentity"), dict) else payload
        public_key = source.get("publicKey") if "publicKey" in source else source.get("public_key")
        public_key = public_key if isinstance(public_key, dict) and self._valid_p256_jwk_public_key(public_key) else None
        public_key_fingerprint = str(source.get("publicKeyFingerprint") or source.get("public_key_fingerprint") or source.get("deviceFingerprint") or "").strip()
        if public_key and not public_key_fingerprint:
            public_key_fingerprint = self._p256_jwk_key_id(public_key)
        return {
            "deviceId": str(source.get("deviceId") or source.get("device_id") or "").strip(),
            "deviceName": str(source.get("deviceName") or source.get("device_name") or source.get("name") or "Unnamed Device").strip(),
            "publicKey": public_key,
            "publicKeyFingerprint": public_key_fingerprint,
            "createdAt": str(source.get("createdAt") or source.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())).strip(),
        }

    def _device_identity_matches(self, entry, device_identity):
        stored_identity = entry.get("deviceIdentity") if isinstance(entry, dict) and isinstance(entry.get("deviceIdentity"), dict) else {}
        stored_public_key = stored_identity.get("publicKey")
        stored_fingerprint = str(stored_identity.get("publicKeyFingerprint") or "")
        requested_public_key = device_identity.get("publicKey")
        requested_fingerprint = str(device_identity.get("publicKeyFingerprint") or "")
        if stored_fingerprint and requested_fingerprint and stored_fingerprint != requested_fingerprint:
            return False
        if isinstance(stored_public_key, dict) and isinstance(requested_public_key, dict) and stored_public_key != requested_public_key:
            return False
        return True

    def _trusted_device_has_public_key(self, entry):
        if not isinstance(entry, dict):
            return False
        device_identity = entry.get("deviceIdentity") if isinstance(entry.get("deviceIdentity"), dict) else {}
        public_key = device_identity.get("publicKey") or entry.get("publicKey") or entry.get("devicePublicKey")
        return isinstance(public_key, dict) and self._valid_p256_jwk_public_key(public_key)

    def _device_fingerprint_from_entry(self, entry, device_identity=None):
        entry = entry if isinstance(entry, dict) else {}
        device_identity = device_identity if isinstance(device_identity, dict) else {}
        stored_identity = entry.get("deviceIdentity") if isinstance(entry.get("deviceIdentity"), dict) else {}
        return (
            str(device_identity.get("publicKeyFingerprint") or "").strip()
            or str(stored_identity.get("publicKeyFingerprint") or "").strip()
            or str(stored_identity.get("public_key_fingerprint") or "").strip()
            or str(entry.get("deviceFingerprint") or "").strip()
            or str(entry.get("publicKeyFingerprint") or "").strip()
            or str(entry.get("public_key_fingerprint") or "").strip()
        )

    def _pairing_result_response(self, device_id, device_fingerprint, trust_state, message, ok=True, trusted_entry=None):
        response = {
            "runtimeId": str(self.runtime_identity.get("runtimeId") or ""),
            "deviceId": str(device_id or ""),
            "trustState": trust_state,
            "message": message,
        }
        if trust_state == "trusted" and isinstance(trusted_entry, dict):
            response["runtimeMetadata"] = self._pairing_runtime_metadata()
            response["runtimeAgents"] = self._agent_definitions()
            session_id = self._ensure_trusted_session_id(trusted_entry)
            if session_id:
                response["session_id"] = session_id
        logger.info(
            "RipDock pairing response trust_state=%s ok=%s fields=%s",
            trust_state,
            ok,
            sorted(response.keys()),
        )
        return response

    def _runtime_chat_session_id(self):
        if isinstance(self.session_id, str) and self.session_id:
            expired, expiry_reason = self._session_expiry_reason()
            if expired:
                logger.warning("RipDock current Session expired reason=%s sessionID=%s", expiry_reason, self._redacted_session_id(self.session_id))
                self._clear_session_id_from_trusted_devices(self.session_id)
                self._invalidate_saved_session(expiry_reason)
            else:
                return self.session_id
        saved_state = self._read_saved_session_state()
        saved_session_id = saved_state.get("session_id")
        if saved_session_id:
            self.session_id = saved_session_id
            self._restore_session_lifecycle(saved_state)
            expired, expiry_reason = self._session_expiry_reason()
            if not expired:
                return saved_session_id
            logger.warning("RipDock saved Session expired reason=%s sessionID=%s", expiry_reason, self._redacted_session_id(saved_session_id))
            self._clear_session_id_from_trusted_devices(saved_session_id)
            self._invalidate_saved_session(expiry_reason)
        self.session_id = self._create_session_id()
        self._reset_session_lifecycle()
        self._save_session_id(self.session_id)
        return self.session_id

    def _ensure_trusted_session_id(self, entry):
        if not isinstance(entry, dict):
            return ""
        self._ensure_trusted_authorization_scopes(entry)
        session_id = self._runtime_chat_session_id()
        if session_id:
            entry["session_id"] = session_id
            return session_id
        existing = str(entry.get("session_id") or entry.get("sessionId") or "").strip()
        if existing:
            entry["session_id"] = existing
            return existing
        return ""

    def _pairing_runtime_metadata(self):
        metadata = self._runtime_metadata()
        return {
            "displayName": metadata.get("displayName") or self.runtime_identity.get("displayName") or self._runtime_display_name(),
            "icon": metadata.get("icon") or None,
            "accentColor": metadata.get("accentColor") or None,
            "backgroundColor": metadata.get("backgroundColor") or "#ffffff",
        }

    def _pairing_error_response(self, message):
        return {
            "runtimeId": str(self.runtime_identity.get("runtimeId") or ""),
            "deviceId": "unknown",
            "trustState": "unpaired",
            "message": message,
        }

    def _handle_pairing_status(self, payload):
        pending, trusted, revoked, rejected = self._ensure_device_maps()
        device_identity = self._device_identity_from_pairing_request(payload)
        device_id = device_identity.get("deviceId")
        if not device_id:
            raise ValueError("deviceId is required.")
        public_key = device_identity.get("publicKey")
        public_key_fingerprint = str(device_identity.get("publicKeyFingerprint") or "").strip()
        if public_key is not None and public_key_fingerprint != self._p256_jwk_key_id(public_key):
            raise ValueError("publicKeyFingerprint must equal publicKey.key_id.")
        if self._record_rate_limit_event("pairing_status", device_id):
            return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(None, device_identity), "notFound", "Pairing is temporarily unavailable. Try again shortly.", ok=False)
        trusted_entry = trusted.get(device_id)
        if isinstance(trusted_entry, dict):
            if not self._trusted_device_has_public_key(trusted_entry):
                return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(trusted_entry, device_identity), "identityMismatch", "Device must be paired again.")
            if not self._device_identity_matches(trusted_entry, device_identity):
                return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(trusted_entry, device_identity), "identityMismatch", "Device identity does not match the trusted key.")
            response = self._pairing_result_response(device_id, self._device_fingerprint_from_entry(trusted_entry, device_identity), "trusted", "Device is trusted.", trusted_entry=trusted_entry)
            self._save_runtime_identity()
            return response
        pending_entry = pending.get(device_id)
        if isinstance(pending_entry, dict):
            if self._is_expired_entry(pending_entry):
                return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(pending_entry, device_identity), "expired", "Pairing request expired.")
            return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(pending_entry, device_identity), "pendingApproval", "Device is pending approval.")
        revoked_entry = revoked.get(device_id)
        if isinstance(revoked_entry, dict):
            return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(revoked_entry, device_identity), "revoked", "Device trust was revoked.")
        rejected_entry = rejected.get(device_id)
        if (
            isinstance(rejected_entry, dict)
            and self._device_identity_matches(rejected_entry, device_identity)
            and self._is_rejected_entry_queryable(rejected_entry)
        ):
            return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(rejected_entry, device_identity), "rejected", "Pairing request rejected.")
        return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(None, device_identity), "notFound", "Device pairing state was not found.")

    def _pending_device_matches(self, entry_key, entry, device_id):
        if str(entry_key) == device_id:
            return True
        device_identity = entry.get("deviceIdentity") if isinstance(entry, dict) and isinstance(entry.get("deviceIdentity"), dict) else {}
        candidates = {
            str(entry.get("deviceId") or "").strip() if isinstance(entry, dict) else "",
            str(entry.get("device_id") or "").strip() if isinstance(entry, dict) else "",
            str(entry.get("deviceFingerprint") or "").strip() if isinstance(entry, dict) else "",
            str(entry.get("publicKeyFingerprint") or "").strip() if isinstance(entry, dict) else "",
            str(entry.get("public_key_fingerprint") or "").strip() if isinstance(entry, dict) else "",
            str(entry.get("requestId") or "").strip() if isinstance(entry, dict) else "",
            str(device_identity.get("deviceId") or "").strip(),
            str(device_identity.get("device_id") or "").strip(),
            str(device_identity.get("publicKeyFingerprint") or "").strip(),
            str(device_identity.get("public_key_fingerprint") or "").strip(),
        }
        return device_id in candidates

    def _pop_pending_device(self, pending, device_id):
        entry = pending.pop(device_id, None)
        if isinstance(entry, dict):
            return device_id, entry
        for key, value in list(pending.items()):
            if isinstance(value, dict) and self._pending_device_matches(key, value, device_id):
                return key, pending.pop(key)
        return None, None

    def _pop_trusted_device(self, trusted, device_id):
        entry = trusted.pop(device_id, None)
        if isinstance(entry, dict):
            return device_id, entry
        for key, value in list(trusted.items()):
            if isinstance(value, dict) and self._pending_device_matches(key, value, device_id):
                return key, trusted.pop(key)
        return None, None

    def _handle_pairing_request(self, payload):
        pending, trusted, revoked, rejected = self._ensure_device_maps()
        device_identity = self._device_identity_from_pairing_request(payload)
        device_id = device_identity.get("deviceId")
        if not device_id:
            raise ValueError("deviceId is required.")
        public_key = device_identity.get("publicKey")
        public_key_fingerprint = str(device_identity.get("publicKeyFingerprint") or "").strip()
        if not isinstance(public_key, dict):
            raise ValueError("publicKey is required.")
        if public_key_fingerprint != self._p256_jwk_key_id(public_key):
            raise ValueError("publicKeyFingerprint must equal publicKey.key_id.")
        if self._record_rate_limit_event("pairing_request", f"request:{device_id}"):
            return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(None, device_identity), "notFound", "Pairing is temporarily unavailable. Try again shortly.", ok=False)
        pending_entry = pending.get(device_id)
        trusted_entry = trusted.get(device_id)
        revoked_entry = revoked.get(device_id)
        rejected_entry = rejected.get(device_id)
        previous_state = self._device_state(pending, trusted, revoked, device_id)
        logger.info(
            "RipDock pairing request device_id=%s fingerprint=%s public_key_present=%s pending_match=%s trusted_match=%s revoked_match=%s rejected_deleted_match=%s",
            device_id,
            device_identity.get("publicKeyFingerprint"),
            bool(device_identity.get("publicKey")),
            isinstance(pending_entry, dict),
            isinstance(trusted_entry, dict),
            isinstance(revoked_entry, dict),
            False,
        )
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if isinstance(trusted_entry, dict):
            if not self._trusted_device_has_public_key(trusted_entry):
                trusted.pop(device_id, None)
                trusted_entry = None
                previous_state = "trusted"
                logger.warning("RipDock trusted Device missing public key; requiring re-pair device_id=%s", device_id)
            else:
                if not self._device_identity_matches(trusted_entry, device_identity):
                    return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(trusted_entry, device_identity), "identityMismatch", "Device identity does not match the trusted key.")
                trusted_entry["lastSeen"] = now
                trusted_entry["trustState"] = "trusted"
                self._ensure_trusted_session_id(trusted_entry)
                self._save_runtime_identity()
                self._log_pairing_transition("refreshStatus", device_id, previous_state, "trusted", pending, trusted)
                return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(trusted_entry, device_identity), "trusted", "Device is trusted.", trusted_entry=trusted_entry)
        if isinstance(revoked_entry, dict):
            revoked.pop(device_id, None)
        if isinstance(pending_entry, dict) and self._is_expired_entry(pending_entry):
            pending.pop(device_id, None)
            previous_state = "expired"
        if isinstance(rejected_entry, dict):
            rejected.pop(device_id, None)
        pending[device_id] = {
            "deviceIdentity": device_identity,
            "requestedAt": now,
            "claimedAt": now,
            "expiresAt": self._iso_after(self._pairing_ttl_seconds()),
            "trustState": "pendingApproval",
        }
        if not self._save_runtime_identity():
            logger.error("RipDock pairing request did not persist pending Device device_id=%s", device_id)
            raise ValueError("Pending Device was not persisted.")
        pending_count = len(pending)
        logger.info("RipDock pairing request wrote pending Device device_id=%s pending_count=%s", device_id, pending_count)
        self._log_pairing_transition("requestPairing", device_id, previous_state, "pendingApproval", pending, trusted)
        return self._pairing_result_response(device_id, self._device_fingerprint_from_entry(pending.get(device_id), device_identity), "pendingApproval", "Device is pending approval.")

    def _approve_pending_device(self, device_id):
        pending, trusted, _revoked, _rejected = self._ensure_device_maps()
        previous_state = self._device_state(pending, trusted, _revoked, device_id)
        _entry_key, entry = self._pop_pending_device(pending, device_id)
        if not isinstance(entry, dict):
            trusted_entry = trusted.get(device_id)
            if isinstance(trusted_entry, dict):
                self._ensure_trusted_authorization_scopes(trusted_entry)
                self._ensure_trusted_session_id(trusted_entry)
                if not self._save_runtime_identity():
                    raise ValueError("Trusted Device was not persisted.")
                self._log_pairing_transition("approve", device_id, previous_state, "trusted", pending, trusted)
                return {
                    "ok": True,
                    "deviceId": device_id,
                    "trustState": "trusted",
                    "session_id": str(trusted_entry.get("session_id") or ""),
                    "noop": True,
                    "state": self._admin_state(),
                }
            raise ValueError("Pending Device not found.")
        device_identity = entry.get("deviceIdentity") if isinstance(entry.get("deviceIdentity"), dict) else {}
        public_key = device_identity.get("publicKey") or entry.get("publicKey") or entry.get("devicePublicKey")
        fingerprint = self._device_fingerprint_from_entry(entry, device_identity)
        if not isinstance(public_key, dict) or not self._valid_p256_jwk_public_key(public_key):
            raise ValueError("Pending Device is missing publicKey and cannot be approved.")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry["approvedAt"] = now
        entry["lastSeen"] = now
        entry["trustState"] = "trusted"
        self._ensure_trusted_authorization_scopes(entry)
        self._ensure_trusted_session_id(entry)
        trusted[device_id] = entry
        if not self._save_runtime_identity():
            raise ValueError("Approved Device was not persisted.")
        self._log_pairing_transition("approve", device_id, previous_state, "trusted", pending, trusted)
        return {
            "ok": True,
            "deviceId": device_id,
            "trustState": "trusted",
            "session_id": str(entry.get("session_id") or ""),
            "state": self._admin_state(),
        }

    def _reject_pending_device(self, device_id):
        pending, _trusted, _revoked, rejected = self._ensure_device_maps()
        previous_state = self._device_state(pending, _trusted, _revoked, device_id)
        _entry_key, entry = self._pop_pending_device(pending, device_id)
        if not isinstance(entry, dict):
            return {
                "ok": True,
                "deviceId": device_id,
                "trustState": "notFound",
                "noop": True,
                "message": "Pending Device was already gone.",
                "state": self._admin_state(),
            }
        entry["rejectedAt"] = self._now_iso()
        entry["reason"] = "dashboardRejected"
        entry["trustState"] = "rejected"
        rejected[device_id] = entry
        self._save_runtime_identity()
        self._log_pairing_transition("reject", device_id, previous_state, "rejected", pending, _trusted)
        return {
            "ok": True,
            "deviceId": device_id,
            "trustState": "rejected",
            "noop": False,
            "message": "Pending Device removed.",
            "state": self._admin_state(),
        }

    def _revoke_trusted_device(self, device_id):
        logger.info("RipDock revoke action entered action=revoke device_id=%s", device_id)
        pending, trusted, revoked, rejected = self._ensure_device_maps()
        previous_state = self._device_state(pending, trusted, revoked, device_id)
        logger.info(
            "RipDock revoke action entry action=revoke device_id=%s trusted_keys=%s trusted_shape=%s",
            device_id,
            sorted(str(key) for key in trusted.keys()),
            {str(key): sorted(value.keys()) if isinstance(value, dict) else type(value).__name__ for key, value in trusted.items()},
        )
        entry_key, entry = self._pop_trusted_device(trusted, device_id)
        if not isinstance(entry, dict):
            entry_key, entry = self._pop_pending_device(pending, device_id)
        logger.info(
            "RipDock revoke route match device_id=%s matched_key=%s matched=%s",
            device_id,
            entry_key,
            isinstance(entry, dict),
        )
        if not isinstance(entry, dict):
            logger.error(
                "RipDock revoke route condition failed condition=trusted_device_not_found device_id=%s trusted_keys=%s",
                device_id,
                sorted(str(key) for key in trusted.keys()),
            )
            return {"ok": True, "deviceId": device_id, "trustState": "revoked", "noop": True, "message": "Trusted Device was already gone.", "state": self._admin_state()}
        entry["revokedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry["trustState"] = "revoked"
        revoked[device_id] = entry
        rejected.pop(device_id, None)
        revoked_session_id = str(entry.get("session_id") or "").strip()
        if revoked_session_id and revoked_session_id == self.session_id:
            self._clear_session_id_from_trusted_devices(revoked_session_id)
            self._invalidate_saved_session("device_revoked")
        if not self._save_runtime_identity():
            raise ValueError("Revoked Device was not persisted.")
        self._schedule_close_revoked_app_websockets(device_id)
        self._log_pairing_transition("revoke", device_id, previous_state, "revoked", pending, trusted)
        return {"ok": True, "deviceId": device_id, "trustState": "revoked", "state": self._admin_state()}

    def _stored_runtime_metadata(self):
        try:
            with self._runtime_metadata_state_file_path().open() as handle:
                state = json.load(handle)
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        metadata = state.get("runtimeMetadata")
        if not isinstance(metadata, dict):
            metadata = state.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return metadata

    def _base64url(self, value):
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    def _public_key_fingerprint(self, public_key):
        if isinstance(public_key, dict):
            return self._p256_jwk_key_id(public_key)
        return ""

    def _generate_runtime_keypair(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        private_key = ec.generate_private_key(ec.SECP256R1())
        public_bytes = private_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)[1:]
        x = public_bytes[:32]
        y = public_bytes[32:]
        key_id = hashlib.sha256(public_bytes).hexdigest()
        public_key = {
            "crv": "P-256",
            "key_id": key_id,
            "kty": "EC",
            "x": self._base64url(x),
            "y": self._base64url(y),
        }
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        return public_key, private_pem

    def _load_or_create_runtime_identity(self):
        display_name = self._runtime_display_name()
        identity_file = self._runtime_identity_file_path()
        try:
            with identity_file.open() as handle:
                saved = json.load(handle)
            if (
                isinstance(saved, dict)
                and isinstance(saved.get("runtimeId"), str)
                and self._valid_p256_jwk_public_key(saved.get("publicKey"))
                and isinstance(saved.get("publicKeyFingerprint"), str)
                and saved.get("publicKeyFingerprint") == self._p256_jwk_key_id(saved.get("publicKey"))
                and isinstance(saved.get("createdAt"), str)
            ):
                saved["displayName"] = saved.get("displayName") if isinstance(saved.get("displayName"), str) and saved.get("displayName") else display_name
                saved["protocolVersion"] = PROTOCOL_VERSION
                if not isinstance(saved.get("trustedDevices"), dict):
                    saved["trustedDevices"] = {}
                if not isinstance(saved.get("pendingDevices"), dict):
                    saved["pendingDevices"] = {}
                if not isinstance(saved.get("revokedDevices"), dict):
                    saved["revokedDevices"] = {}
                if not isinstance(saved.get("rejectedDevices"), dict):
                    saved["rejectedDevices"] = {}
                return saved
        except Exception:
            pass

        public_key, private_key = self._generate_runtime_keypair()
        identity = {
            "runtimeId": self._configured_runtime_id(),
            "displayName": display_name,
            "publicKey": public_key,
            "publicKeyFingerprint": self._public_key_fingerprint(public_key),
            "protocolVersion": PROTOCOL_VERSION,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "privateKey": private_key,
            "trustedDevices": {},
            "pendingDevices": {},
            "revokedDevices": {},
            "rejectedDevices": {},
        }
        try:
            identity_file.parent.mkdir(parents=True, exist_ok=True)
            with identity_file.open("w") as handle:
                json.dump(identity, handle, indent=2, sort_keys=True)
                handle.write("\n")
            try:
                identity_file.chmod(0o600)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("RipDock failed to persist Runtime identity path=%s error=%s", identity_file, repr(exc))
        return identity

    def _public_runtime_identity(self):
        identity = self.runtime_identity
        return {
            "runtimeId": identity["runtimeId"],
            "displayName": identity["displayName"],
            "publicKey": identity["publicKey"],
            "publicKeyFingerprint": identity["publicKeyFingerprint"],
            "protocolVersion": identity.get("protocolVersion", PROTOCOL_VERSION),
            "createdAt": identity["createdAt"],
        }

    def _environment_label(self):
        configured = os.getenv("RIPDOCK_ENVIRONMENT_LABEL", "Dev").strip()
        normalized = configured.lower()
        if normalized == "dev":
            return "Dev"
        if normalized == "staging":
            return "Staging"
        if normalized == "production":
            return "Production"
        return configured or "Custom"

    def _runtime_metadata(self):
        stored = self._stored_runtime_metadata()
        display_name = stored.get("displayName") if isinstance(stored.get("displayName"), str) and stored.get("displayName") else None
        icon = _emoji_icon_or_empty(stored.get("icon"))
        accent_color = stored.get("accentColor") if isinstance(stored.get("accentColor"), str) else ""
        background_color = stored.get("backgroundColor") if isinstance(stored.get("backgroundColor"), str) else ""
        resolved_icon = icon or _emoji_icon_or_empty(os.getenv("RIPDOCK_RUNTIME_ICON", ""))
        return {
            "displayName": display_name or self.runtime_identity.get("displayName") or self._runtime_display_name(),
            "icon": resolved_icon or None,
            "accentColor": (accent_color if isinstance(accent_color, str) else os.getenv("RIPDOCK_ACCENT_COLOR", "")).strip(),
            "backgroundColor": (background_color if isinstance(background_color, str) else os.getenv("RIPDOCK_BACKGROUND_COLOR", "#ffffff")).strip() or "#ffffff",
        }

    def _public_runtime_url_file(self):
        return Path(
            os.getenv(
                "RIPDOCK_PUBLIC_RUNTIME_URL_FILE",
                str(_hermes_home() / "ripdock" / "public-runtime-url"),
            )
        )

    def _detected_public_runtime_url(self):
        try:
            return self._public_runtime_url_file().read_text().strip() or None
        except Exception:
            return None

    def _public_runtime_url_with_source(self):
        configured = os.getenv("RIPDOCK_PUBLIC_RUNTIME_URL", "").strip()
        if self._is_device_facing_runtime_url(configured):
            return configured, "public_url"
        detected = self._detected_public_runtime_url()
        if self._is_device_facing_runtime_url(detected):
            return detected, "public_url_file"
        return None, None

    def _is_device_facing_runtime_url(self, value):
        if not isinstance(value, str) or not value:
            return False
        try:
            parts = urlsplit(value)
        except Exception:
            return False
        if parts.scheme != "https":
            return False
        hostname = (parts.hostname or "").lower()
        if hostname in {"localhost", "host.docker.internal", "runtime", "caddy", "0.0.0.0", "127.0.0.1", "::1"}:
            return False
        if hostname.endswith(".local") or hostname.endswith(".internal"):
            return False
        if hostname.startswith("127.") or hostname.startswith("10.") or hostname.startswith("192.168."):
            return False
        match = re.match(r"^172\.(\d+)\.", hostname)
        if match and 16 <= int(match.group(1)) <= 31:
            return False
        return True

    def _public_ripdock_url(self):
        public_url, _source = self._public_runtime_url_with_source()
        return public_url

    def _pairing_ttl_seconds(self):
        raw_value = os.getenv("RIPDOCK_PAIRING_TTL_SECONDS", "").strip()
        if not raw_value:
            return DEFAULT_PAIRING_TTL_SECONDS
        try:
            value = int(raw_value)
        except ValueError:
            return DEFAULT_PAIRING_TTL_SECONDS
        return min(MAX_PAIRING_TTL_SECONDS, max(MIN_PAIRING_TTL_SECONDS, value))

    def _device_summary(self, entry):
        device_identity = entry.get("deviceIdentity") if isinstance(entry, dict) else {}
        if not isinstance(device_identity, dict):
            device_identity = {}
        return {
            "deviceName": device_identity.get("deviceName") or device_identity.get("name") or entry.get("deviceName") or entry.get("name") or "",
            "deviceId": device_identity.get("deviceId") or device_identity.get("device_id") or entry.get("deviceId") or entry.get("device_id") or "",
            "deviceFingerprint": device_identity.get("publicKeyFingerprint") or device_identity.get("public_key_fingerprint") or entry.get("deviceFingerprint") or entry.get("publicKeyFingerprint") or entry.get("public_key_fingerprint") or "",
            "claimedTime": entry.get("requestedAt") or entry.get("claimedAt") or entry.get("claimedTime") if isinstance(entry, dict) else None,
            "approvedTime": entry.get("approvedAt") if isinstance(entry, dict) else None,
            "lastSeen": entry.get("lastSeen") if isinstance(entry, dict) else None,
            "expiresAt": entry.get("expiresAt") if isinstance(entry, dict) else None,
            "status": entry.get("trustState") or entry.get("status", "unknown") if isinstance(entry, dict) else "unknown",
        }

    def _admin_state(self):
        configured_public_url = os.getenv("RIPDOCK_PUBLIC_RUNTIME_URL", "").strip()
        detected_tunnel_url = self._detected_public_runtime_url()
        active_public_url = self._public_ripdock_url()
        trusted_devices = self.runtime_identity.get("trustedDevices")
        pending_devices = self.runtime_identity.get("pendingDevices")
        if not isinstance(trusted_devices, dict):
            trusted_devices = {}
        if not isinstance(pending_devices, dict):
            pending_devices = {}
        return {
            "runtimeIdentity": self._public_runtime_identity(),
            "runtimeMetadata": self._runtime_metadata(),
            "publicURL": {
                "configured": configured_public_url,
                "detectedTunnelURL": detected_tunnel_url,
                "active": active_public_url,
                "pairingQRAvailable": False,
                "detectedTunnelURLUsable": self._is_device_facing_runtime_url(detected_tunnel_url),
                "message": "Public RIPDOCK URL is used for Device-facing Runtime connections.",
            },
            "pairingSettings": {
                "pairingTTLSeconds": self._pairing_ttl_seconds(),
                "minPairingTTLSeconds": MIN_PAIRING_TTL_SECONDS,
                "maxPairingTTLSeconds": MAX_PAIRING_TTL_SECONDS,
                "productionPairingTTLSeconds": PRODUCTION_PAIRING_TTL_SECONDS,
                "pairingQRAvailable": False,
                "pairingCodeOnlyAvailable": True,
            },
            "pendingDevices": [self._device_summary(value) for value in pending_devices.values() if isinstance(value, dict) and not self._is_expired_entry(value)],
            "trustedDevices": [self._device_summary(value) for value in trusted_devices.values()],
            "security": {
                "runtimeFingerprint": self.runtime_identity.get("publicKeyFingerprint"),
                "trustAnchorWarning": "RuntimeIdentity public key fingerprint is the trust anchor.",
                "publicURLWarning": "A public URL change does not change RuntimeIdentity or its fingerprint.",
            },
        }

    def _admin_conversations_snapshot(self, agent_id="", conversation_id=""):
        requested_agent_id = agent_id if self._is_non_empty_string(agent_id) else ""
        requested_conversation_id = conversation_id if self._is_non_empty_string(conversation_id) else ""
        agents = self._agent_definitions()
        conversations = []
        for agent in agents:
            current_agent_id = agent.get("agent_id")
            if not self._is_non_empty_string(current_agent_id):
                continue
            if requested_agent_id and current_agent_id != requested_agent_id:
                continue
            for summary in self._conversation_list_summaries(current_agent_id):
                current_conversation_id = summary.get("conversation_id")
                if requested_conversation_id and current_conversation_id != requested_conversation_id:
                    continue
                messages = self._conversation_sync_messages(current_agent_id, current_conversation_id, 0)
                conversations.append(
                    {
                        **summary,
                        "runtime_id": self.runtime_id,
                        "agent_id": current_agent_id,
                        "agent_display_name": agent.get("display_name") or self._agent_display_name(current_agent_id),
                        "messages": [
                            {
                                "message_id": message.get("message_id"),
                                "role": message.get("role"),
                                "content": message.get("content"),
                                "created_at": self._protocol_timestamp_from_epoch(message["epoch"]),
                            }
                            for message in messages
                        ],
                    }
                )
        return {
            "ok": True,
            "runtime_id": self.runtime_id,
            "agent_id": requested_agent_id,
            "conversation_id": requested_conversation_id,
            "conversations": conversations,
        }

    def _runtime_display_name(self):
        configured = os.getenv("RIPDOCK_RUNTIME_NAME", "").strip()
        if configured:
            return configured
        if self.runtime_type == "openclaw":
            return "OpenClaw"
        if self.runtime_type == "custom":
            return "Custom Runtime"
        return "Hermes"

    def _runtime_settings_definitions(self, runtime_id, runtime_type):
        return []

    def _default_runtime_settings_values(self, runtime_id):
        return {}

    def _runtime_settings_values(self):
        self.runtime_settings_by_runtime_id[self.runtime_id] = {}
        return self.runtime_settings_by_runtime_id[self.runtime_id]

    def _runtime_model_info_delta(self):
        return ""

    def _endpoint_policy(self):
        return {
            "type": "endpoint.policy",
            "protocol_version": PROTOCOL_VERSION,
            "payload": {
                "max_message_bytes": self._max_message_bytes(),
            },
        }

    def _max_message_bytes(self):
        raw_value = os.getenv("RIPDOCK_MAX_MESSAGE_BYTES", "")
        if not raw_value:
            return DEFAULT_MAX_MESSAGE_BYTES
        try:
            value = int(raw_value)
        except ValueError:
            logger.warning(
                "Invalid RIPDOCK_MAX_MESSAGE_BYTES=%s; using default %s",
                raw_value,
                DEFAULT_MAX_MESSAGE_BYTES,
            )
            return DEFAULT_MAX_MESSAGE_BYTES
        if value < MIN_MAIN_MESSAGE_BYTES:
            logger.warning(
                "RIPDOCK_MAX_MESSAGE_BYTES below v1 minimum %s; clamping",
                MIN_MAIN_MESSAGE_BYTES,
            )
            return MIN_MAIN_MESSAGE_BYTES
        if value > MAX_MAIN_MESSAGE_BYTES:
            logger.warning(
                "RIPDOCK_MAX_MESSAGE_BYTES above v1 maximum %s; clamping",
                MAX_MAIN_MESSAGE_BYTES,
            )
            return MAX_MAIN_MESSAGE_BYTES
        return value

    def _message_size(self, message):
        if isinstance(message, bytes):
            return len(message)
        if isinstance(message, str):
            return len(message.encode("utf-8"))
        return len(str(message).encode("utf-8"))

    def _session_file_path(self):
        return Path(
            os.getenv(
                "RIPDOCK_SESSION_FILE",
                os.path.join(
                    str(_hermes_home()),
                    "ripdock",
                    "session.json",
                ),
            )
        )

    def _read_saved_session_id(self):
        saved = self._read_saved_session_state()
        session_id = saved.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
        return None

    def _read_saved_session_state(self):
        try:
            with self._session_file_path().open() as handle:
                saved = json.load(handle)
        except Exception:
            return {}

        return saved if isinstance(saved, dict) else {}

    def _save_session_id(self, session_id):
        self.session_id = session_id
        self._save_session_state()

    def _save_session_state(self):
        if not isinstance(self.session_id, str) or not self.session_id:
            return
        if not self.session_created_at or not self.session_expires_at or not self.session_idle_expires_at:
            self._reset_session_lifecycle()
        session_file = self._session_file_path()
        state = {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self.session_id,
            "session": {
                "id": self.session_id,
                "createdAt": self.session_created_at,
                "lastSeenAt": self.session_last_seen_at,
                "expiresAt": self.session_expires_at,
                "idleExpiresAt": self.session_idle_expires_at,
                "rotateOnResume": self._rotate_session_on_resume(),
            },
            "runtime_id": self.runtime_id,
            "runtime_public_key_fingerprint": self.runtime_identity.get("publicKeyFingerprint"),
            "runtime_identity_created_at": self.runtime_identity.get("createdAt"),
            "runtime_identity": self._public_runtime_identity(),
            "runtime_type": self.runtime_type,
            "display_name": self._runtime_display_name(),
            "pairing": {
                "mode": "direct",
                "paired": True,
                "runtime_url": self.embedded_public_url
                or os.getenv("RIPDOCK_DIRECT_RUNTIME_URL", "https://localhost:8443").rstrip("/"),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "runtime_settings": self.runtime_settings_by_runtime_id,
        }
        try:
            session_file.parent.mkdir(parents=True, exist_ok=True)
            with session_file.open("w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except Exception as exc:
            logger.warning("RipDock failed to persist Session state path=%s error=%s", session_file, repr(exc))

    def _restore_persisted_runtime_state(self, saved):
        saved_runtime_id = saved.get("runtime_id")
        saved_display_name = saved.get("display_name")
        if isinstance(saved_runtime_id, str) and saved_runtime_id and saved_runtime_id != self.runtime_id:
            logger.warning(
                "RipDock saved Session runtime_id differs saved_runtime_id=%s active_runtime_id=%s",
                saved_runtime_id,
                self.runtime_id,
            )
        self.runtime_settings_by_runtime_id[self.runtime_id] = {}
        logger.warning(
            "RipDock saved Session loaded sessionID=%s runtime_id=%s display_name=%s pairing_persisted=%s",
            self._redacted_session_id(saved.get("session_id")),
            saved_runtime_id or self.runtime_id,
            saved_display_name or self._runtime_display_name(),
            isinstance(saved.get("pairing"), dict),
        )

    def _invalidate_saved_session(self, reason):
        session_file = self._session_file_path()
        self.session_id = None
        self.session_created_at = None
        self.session_last_seen_at = None
        self.session_expires_at = None
        self.session_idle_expires_at = None
        self.pairing_code = None
        self.pairing_bound = False
        try:
            if session_file.exists():
                session_file.unlink()
        except Exception:
            logger.exception("RipDock failed to remove invalid Session file")
        logger.warning("RipDock pairing invalidated reason=%s", reason)

    def _require_secure_transport_url(self, value, label):
        parts = urlsplit(value)
        if parts.scheme not in {"https", "wss"}:
            raise ValueError(f"{label} must use https or wss.")
        return parts

    def _websocket_url(self, value, label):
        parts = self._require_secure_transport_url(value, label)
        return urlunsplit(("wss", parts.netloc, parts.path, parts.query, parts.fragment))

    def _embedded_runtime_transfer_base_url(self):
        public_url, source = self._public_runtime_url_with_source()
        if public_url:
            base = self._websocket_url(public_url, source).rstrip("/")
            logger.warning(
                "RipDock transfer_base_selected base_url=%s source=%s",
                base,
                source,
            )
            return base, source

        direct_url = (self.embedded_public_url or os.getenv("RIPDOCK_DIRECT_RUNTIME_URL", "https://localhost:8443")).rstrip("/")
        direct_parts = self._require_secure_transport_url(direct_url, "RIPDOCK_DIRECT_RUNTIME_URL")
        hostname = (direct_parts.hostname or "").lower()
        local_hostname = hostname in {"localhost", "0.0.0.0", "127.0.0.1", "::1"}
        if local_hostname and direct_parts.port in {None, 443}:
            host = os.getenv("RIPDOCK_EMBEDDED_HOST", self.embedded_host or "127.0.0.1")
            if host in {"0.0.0.0", "::", ""}:
                host = "127.0.0.1"
            port = int(os.getenv("RIPDOCK_EMBEDDED_PORT", str(self.embedded_port or 8788)))
            base = urlunsplit(("wss", f"{host}:{port}", "", "", "")).rstrip("/")
            logger.warning(
                "RipDock transfer_base_selected base_url=%s source=fallback_embedded_endpoint direct_url=%s",
                base,
                direct_url,
            )
            return base, "fallback_embedded_endpoint"

        base = self._websocket_url(direct_url, "RIPDOCK_DIRECT_RUNTIME_URL").rstrip("/")
        logger.warning(
            "RipDock transfer_base_selected base_url=%s source=direct_url",
            base,
        )
        return base, "direct_url"

    def _embedded_transfer_url(self, transfer_id, role):
        base, source = self._embedded_runtime_transfer_base_url()
        transfer_url = f"{base}/ripdock/transfer/{quote(transfer_id, safe='')}/{role}"
        logger.warning(
            "RipDock transfer_url_generated transfer_id=%s role=%s source=%s transfer_url=%s",
            transfer_id,
            role,
            source,
            transfer_url,
        )
        return transfer_url

    def _embedded_artifact_download_url(self, transfer_id):
        base, source = self._embedded_runtime_transfer_base_url()
        parts = urlsplit(base)
        scheme = "https"
        download_url = urlunsplit((scheme, parts.netloc, f"/ripdock/transfer/{quote(transfer_id, safe='')}/artifact", "", ""))
        logger.warning(
            "RipDock artifact_download_url_generated transfer_id=%s source=%s download_url=%s",
            transfer_id,
            source,
            download_url,
        )
        return download_url

    def _handle_embedded_artifact_download(self, transfer_id):
        transfer = self.transfers.get(transfer_id)
        if not transfer or transfer.get("direction") != "runtime_to_app":
            return _embedded_http_response(
                404,
                [("content-type", "application/json"), ("cache-control", "no-store")],
                (json.dumps({"ok": False, "message": "Artifact transfer not found."}) + "\n").encode("utf-8"),
            )

        failure_variant = transfer.get("failure_variant")
        if failure_variant == "invalid-url":
            return _embedded_http_response(
                404,
                [("content-type", "application/json"), ("cache-control", "no-store")],
                (json.dumps({"ok": False, "message": "Artifact download URL is invalid."}) + "\n").encode("utf-8"),
            )
        if failure_variant == "network-drop":
            return _embedded_http_response(
                503,
                [("content-type", "application/json"), ("cache-control", "no-store")],
                (json.dumps({"ok": False, "message": "Artifact download connection dropped."}) + "\n").encode("utf-8"),
            )

        try:
            path = Path(transfer.get("path", ""))
            if not path.exists() or not path.is_file():
                raise FileNotFoundError(str(path))
            data = path.read_bytes()
        except Exception as exc:
            logger.warning(
                "RipDock artifact_download_failed transfer_id=%s artifact_id=%s message=%s",
                transfer_id,
                transfer.get("artifact_id"),
                str(exc),
            )
            return _embedded_http_response(
                404,
                [("content-type", "application/json"), ("cache-control", "no-store")],
                (json.dumps({"ok": False, "message": "Artifact file is unavailable."}) + "\n").encode("utf-8"),
            )

        logger.warning(
            "RipDock artifact_download_served transfer_id=%s artifact_id=%s bytes=%s",
            transfer_id,
            transfer.get("artifact_id"),
            len(data),
        )
        return _embedded_http_response(
            200,
            [
                ("content-type", transfer.get("mime_type") or "application/octet-stream"),
                ("content-length", str(len(data))),
                ("cache-control", "no-store"),
                ("x-ripdock-transfer-id", transfer_id),
                ("x-ripdock-artifact-id", transfer.get("artifact_id") or ""),
                ("x-ripdock-sha256", transfer.get("sha256") or ""),
            ],
            data,
        )

    def _transfer_file_path(self, transfer_id):
        return Path(
            os.getenv(
                "RIPDOCK_TRANSFER_DIR",
                os.path.join(
                    str(_hermes_home()),
                    "ripdock",
                    "transfers",
                ),
            )
        ) / f"{transfer_id}.bin"

    def _max_artifact_bytes(self):
        raw_value = os.getenv("RIPDOCK_MAX_ARTIFACT_BYTES", str(MAX_FILE_BYTES))
        try:
            value = int(raw_value)
        except ValueError:
            logger.warning(
                "Invalid RIPDOCK_MAX_ARTIFACT_BYTES=%s; using default %s",
                raw_value,
                MAX_FILE_BYTES,
            )
            return MAX_FILE_BYTES
        return min(value, MAX_FILE_BYTES) if value > 0 else MAX_FILE_BYTES

    def _artifact_transfer_timeout(self):
        raw_value = os.getenv("RIPDOCK_ARTIFACT_TRANSFER_TIMEOUT", "60")
        try:
            value = int(raw_value)
        except ValueError:
            return 60
        return value if value > 0 else 60

    def _artifact_transfer_chunk_bytes(self, transfer=None):
        chunk_bytes = MAX_CHUNK_BYTES
        transfer_limit = transfer.get("max_chunk_bytes") if isinstance(transfer, dict) else None
        if isinstance(transfer_limit, int) and transfer_limit > 0:
            chunk_bytes = min(chunk_bytes, transfer_limit)

        capabilities = self._app_capabilities_for_current_session()
        payload = capabilities.get("payload") if isinstance(capabilities, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        artifact_limits = payload.get("artifact_limits")
        if not isinstance(artifact_limits, dict):
            return chunk_bytes
        advertised = artifact_limits.get("max_chunk_bytes")
        if not isinstance(advertised, int) or advertised < 1:
            return chunk_bytes
        return min(chunk_bytes, advertised)

    def _artifact_ids_for_message(self, message_id):
        artifact_ids = self._artifact_ids_by_message_id.get(message_id, [])
        return list(artifact_ids) if artifact_ids else []

    @staticmethod
    def extract_local_files(content):
        if RipDockAdapter._runtime_intent_from_content(content):
            return [], content
        base_extract = getattr(BasePlatformAdapter, "extract_local_files", None)
        if callable(base_extract):
            return base_extract(content)
        return [], content

    @staticmethod
    def _runtime_intent_from_content(content):
        text = RipDockAdapter._strip_single_json_fence(content)
        if not text:
            return None
        try:
            payload = json.loads(text)
        except Exception:
            return None
        if not isinstance(payload, dict) or "runtime_intent" not in payload:
            return None
        name = payload.get("runtime_intent")
        arguments = payload.get("arguments", {})
        visible_text = payload.get("visible_text", "")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return {
                "runtime_intent": "ripdock.intent.invalid",
                "arguments": {},
                "visible_text": "",
                "error": "Runtime intent must include string runtime_intent and object arguments.",
            }
        if not isinstance(visible_text, str):
            visible_text = ""
        return {
            "runtime_intent": name,
            "arguments": arguments,
            "visible_text": visible_text.strip(),
        }

    @staticmethod
    def _strip_single_json_fence(content):
        if not isinstance(content, str):
            return ""
        text = content.strip()
        if not text:
            return ""
        if not text.startswith("```"):
            return text
        lines = text.splitlines()
        if len(lines) < 3:
            return ""
        first = lines[0].strip().lower()
        last = lines[-1].strip()
        if first not in {"```json", "```"} or last != "```":
            return ""
        return "\n".join(lines[1:-1]).strip()

    def _content_may_be_runtime_intent(self, content):
        text = content.strip() if isinstance(content, str) else ""
        return (
            text.startswith("{")
            and '"runtime_intent"' in text
        ) or (
            text.startswith("```")
            and "runtime_intent" in text
        )

    def _conversation_prefers_runtime_intent_output(self, conversation_id):
        user_text = getattr(self, "_active_user_text_by_conversation", {}).get(conversation_id, "")
        if not isinstance(user_text, str):
            return False
        lowered = user_text.lower()
        intent_markers = (
            "send me",
            "send the",
            "send it",
            "deliver",
            "artifact",
            "runtime-local report",
            "existing artifact",
            "runtime activity",
            "activity reporting",
        )
        return any(marker in lowered for marker in intent_markers)

    def _is_pending_runtime_intent_fence(self, conversation_id, content):
        text = content.strip().lower() if isinstance(content, str) else ""
        if text not in {"```", "```json"}:
            return False
        return self._conversation_prefers_runtime_intent_output(conversation_id)

    def _is_pending_runtime_intent_fragment(self, conversation_id, content):
        if not self._conversation_prefers_runtime_intent_output(conversation_id):
            return False
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            return False
        normalized = re.sub(r"\s+", "", text).lower()
        runtime_intent_prefix = '{"runtime_intent"'
        runtime_key_prefix = '{"runtime'
        return (
            runtime_intent_prefix.startswith(normalized)
            or normalized.startswith(runtime_intent_prefix)
            or runtime_key_prefix.startswith(normalized)
        )

    def _runtime_intent_buffer(self):
        if not hasattr(self, "_pending_runtime_intent_by_message_id"):
            self._pending_runtime_intent_by_message_id = {}
        return self._pending_runtime_intent_by_message_id

    def _runtime_intent_buffered_content(self, message_id, content):
        text = content if isinstance(content, str) else ""
        existing = self._runtime_intent_buffer().get(message_id)
        if not existing:
            return text
        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("```"):
            return text
        return existing + text

    def _remember_runtime_intent_buffer(self, message_id, content):
        if isinstance(message_id, str) and message_id and isinstance(content, str):
            self._runtime_intent_buffer()[message_id] = content

    def _clear_runtime_intent_buffer(self, message_id):
        if isinstance(message_id, str) and message_id:
            self._runtime_intent_buffer().pop(message_id, None)

    async def _handle_or_buffer_runtime_intent_output(self, websocket, conversation_id, message_id, content, *, finalize=False):
        runtime_message_id = self._message_id_for_stream(conversation_id, message_id)
        candidate = self._runtime_intent_buffered_content(runtime_message_id, content)
        intent = self._runtime_intent_from_content(candidate)
        if intent and not finalize:
            self._remember_runtime_intent_buffer(runtime_message_id, candidate)
            return True, content
        if self._is_pending_runtime_intent_fence(conversation_id, candidate) and not finalize:
            self._remember_runtime_intent_buffer(runtime_message_id, candidate)
            logger.warning(
                "RipDock Runtime intent fence buffered conversation=%s message=%s",
                conversation_id,
                runtime_message_id,
            )
            return True, content
        if self._is_pending_runtime_intent_fragment(conversation_id, candidate) and not finalize:
            self._remember_runtime_intent_buffer(runtime_message_id, candidate)
            logger.warning(
                "RipDock Runtime intent fragment buffered conversation=%s message=%s bytes=%s",
                conversation_id,
                runtime_message_id,
                len(candidate.encode("utf-8")),
            )
            return True, content
        if await self._handle_runtime_intent_output(websocket, conversation_id, runtime_message_id, candidate, finalize=finalize):
            if finalize or self._runtime_intent_from_content(candidate):
                self._clear_runtime_intent_buffer(runtime_message_id)
            else:
                self._remember_runtime_intent_buffer(runtime_message_id, candidate)
            return True, content
        if candidate != content:
            self._clear_runtime_intent_buffer(runtime_message_id)
            return False, candidate
        return False, content

    def _runtime_intent_validation_error(self, name, arguments):
        if name == "ripdock.artifact.deliver":
            path = arguments.get("path")
            if not isinstance(path, str) or not path.strip():
                return "ripdock.artifact.deliver requires arguments.path."
        elif name == "ripdock.activity.report":
            tool_name = arguments.get("tool")
            if not isinstance(tool_name, str) or not tool_name.strip():
                return "ripdock.activity.report requires arguments.tool."
        return None

    async def _handle_runtime_intent_output(self, websocket, conversation_id, message_id, content, *, finalize=False):
        message_id = self._message_id_for_stream(conversation_id, message_id)
        intent = self._runtime_intent_from_content(content)
        if not intent:
            if self._content_may_be_runtime_intent(content):
                if not finalize:
                    return True
                if websocket:
                    stream = self._stream_for(conversation_id, message_id, websocket=websocket)
                    await stream.fail(
                        "runtime.intent_invalid",
                        "Runtime intent output must be a complete JSON object with runtime_intent and arguments.",
                    )
                return True
            return False
        if not finalize:
            return True
        name = intent["runtime_intent"]
        arguments = intent["arguments"]
        visible_text = intent.get("visible_text", "")
        logger.warning(
            "RipDock runtime intent received conversation=%s message=%s intent=%s",
            conversation_id,
            message_id,
            name,
        )
        supported_intents = {
            "ripdock.artifact.deliver",
            "ripdock.artifact.resolve_and_deliver",
            "ripdock.activity.report",
        }
        if name not in supported_intents:
            stream = self._stream_for(conversation_id, message_id, websocket=websocket)
            await stream.fail("runtime.intent_unsupported", f"Runtime intent is not supported: {name}")
            return True
        validation_error = self._runtime_intent_validation_error(name, arguments)
        if validation_error:
            stream = self._stream_for(conversation_id, message_id, websocket=websocket)
            await stream.fail("runtime.intent_invalid", validation_error)
            return True
        if visible_text and websocket:
            await self._stream_for(conversation_id, message_id, websocket=websocket).delta(visible_text, source="runtime_intent_visible_text")
        if name == "ripdock.artifact.deliver":
            await self._handle_artifact_deliver_intent(websocket, conversation_id, message_id, arguments)
        elif name == "ripdock.artifact.resolve_and_deliver":
            await self._handle_artifact_resolve_and_deliver_intent(websocket, conversation_id, message_id, arguments)
        elif name == "ripdock.activity.report":
            await self._handle_activity_report_intent(websocket, conversation_id, message_id, arguments)
        if finalize and websocket and message_id not in self._completed_message_ids:
            await self._stream_for(conversation_id, message_id, websocket=websocket).complete(
                artifact_ids=self._artifact_ids_for_message(message_id),
                source="runtime_intent_finalize",
            )
        return True

    async def _handle_artifact_deliver_intent(self, websocket, conversation_id, message_id, arguments):
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            stream = self._stream_for(conversation_id, message_id, websocket=websocket)
            await stream.fail("runtime.intent_invalid", "ripdock.artifact.deliver requires arguments.path.")
            return
        await self._deliver_artifact_path(websocket, conversation_id, message_id, Path(path), arguments.get("description"))

    async def _handle_artifact_resolve_and_deliver_intent(self, websocket, conversation_id, message_id, arguments):
        artifact = None
        artifact_id = arguments.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            artifact = self._generated_artifacts_by_id.get(artifact_id)
        if not artifact:
            path = arguments.get("path")
            if isinstance(path, str) and path.strip():
                await self._deliver_artifact_path(websocket, conversation_id, message_id, Path(path), arguments.get("description"))
                return
        if artifact:
            await self._start_generated_artifact_transfer(websocket, artifact)
            return
        await self._send_runtime_failure_message(
            websocket,
            conversation_id,
            message_id,
            "I couldn't find a matching file to send.",
        )

    async def _handle_activity_report_intent(self, websocket, conversation_id, message_id, arguments):
        tool_name = arguments.get("tool")
        if not isinstance(tool_name, str) or not tool_name.strip():
            stream = self._stream_for(conversation_id, message_id, websocket=websocket)
            await stream.fail("runtime.intent_invalid", "ripdock.activity.report requires arguments.tool.")
            return
        activity = {
            "tool_name": tool_name.strip(),
            "category": arguments.get("category") if isinstance(arguments.get("category"), str) else self._activity_category(tool_name.strip()),
            "summary": arguments.get("summary") if isinstance(arguments.get("summary"), str) else "Working",
            "detail_id": self._store_raw_tool_detail(tool_name.strip(), arguments.get("args") if isinstance(arguments.get("args"), dict) else {}, json.dumps(arguments, sort_keys=True)),
            "raw_detail": json.dumps(arguments, sort_keys=True),
            "args": arguments.get("args") if isinstance(arguments.get("args"), dict) else {},
            "status": arguments.get("status") if arguments.get("status") in {"running", "completed"} else "running",
        }
        await self._emit_runtime_activity(websocket, conversation_id, message_id, activity)

    async def _deliver_artifact_path(self, websocket, conversation_id, message_id, path, description=None):
        artifact = self._registered_artifact_record_for_path(path, conversation_id, message_id)
        if not artifact:
            artifact = self._validated_artifact_record(path, conversation_id, message_id, description=description)
        if not artifact:
            await self._send_runtime_failure_message(websocket, conversation_id, message_id, "I couldn't validate that file for delivery.")
            return
        await self._start_generated_artifact_transfer(websocket, artifact)

    async def _send_runtime_failure_message(self, websocket, conversation_id, message_id, content):
        if not websocket:
            return
        await self._stream_for(conversation_id, message_id, websocket=websocket).delta(content, source="runtime_failure_message")

    def _registered_artifact_record_for_path(self, path, conversation_id, message_id):
        try:
            resolved = Path(path).resolve()
        except Exception:
            return None
        source = None
        for artifact in self._generated_artifacts_by_id.values():
            try:
                artifact_path = Path(artifact.get("path", "")).resolve()
            except Exception:
                continue
            if artifact_path != resolved:
                continue
            if conversation_id and artifact.get("conversation_id") != conversation_id:
                continue
            source = artifact
            break
        if not source:
            return None
        if not resolved.exists() or not resolved.is_file():
            return None

        stat = resolved.stat()
        key = f"{message_id}:registered:{source.get('artifact_id')}:{resolved}:{stat.st_mtime_ns}:{stat.st_size}"
        existing = self._generated_artifacts_by_key.get(key)
        if existing:
            return None
        artifact_id = str(uuid.uuid4())
        artifact = {
            **source,
            "artifact_id": artifact_id,
            "conversation_id": conversation_id or source.get("conversation_id", ""),
            "message_id": message_id,
            "source_message_id": message_id,
            "source_runtime_id": self.runtime_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "size_bytes": stat.st_size,
            "sha256": source.get("sha256") or self._sha256_file(resolved),
        }
        self._generated_artifacts_by_key[key] = artifact_id
        self._generated_artifacts_by_id[artifact_id] = artifact
        self._artifact_ids_by_message_id.setdefault(message_id, []).append(artifact_id)
        logger.warning(
            "RipDock registered_artifact_reused artifact_id=%s source_artifact_id=%s path=%s filename=%s conversation=%s",
            artifact_id,
            source.get("artifact_id"),
            artifact["path"],
            artifact["filename"],
            artifact["conversation_id"],
        )
        return artifact

    def _remember_conversation_context(self, conversation_id, message_id, content):
        if not isinstance(conversation_id, str) or not conversation_id:
            return
        if not isinstance(content, str) or not content.strip():
            return
        content = self._apply_runtime_visibility_filter(content)
        if not content:
            return
        if not hasattr(self, "_conversation_context_by_id"):
            self._conversation_context_by_id = {}
        entries = self._conversation_context_by_id.setdefault(conversation_id, [])
        entries.append({
            "message_id": message_id or "",
            "content": content.strip(),
            "created_at": time.time(),
        })
        del entries[:-40]

    def _conversation_context_text(self, conversation_id, message_id=None, window=8):
        entries = getattr(self, "_conversation_context_by_id", {}).get(conversation_id or "", [])
        if not entries:
            return ""
        selected = entries[-window:]
        if message_id:
            for index, entry in enumerate(entries):
                if entry.get("message_id") == message_id:
                    start = max(0, index - window + 1)
                    selected = entries[start:index + 1]
                    break
        return "\n".join(str(entry.get("content", "")) for entry in selected if entry.get("content"))

    def _validated_artifact_record(self, path, conversation_id, message_id, description=None):
        try:
            resolved = Path(path).resolve()
        except Exception:
            return None

        logger.warning(
            "RipDock artifact_detected path=%s conversation=%s message=%s",
            resolved,
            conversation_id,
            message_id,
        )

        if not resolved.exists() or not resolved.is_file():
            logger.warning("RipDock artifact validation failed reason=missing path=%s", resolved)
            return None

        stat = resolved.stat()
        size_bytes = stat.st_size
        if size_bytes < 1:
            logger.warning("RipDock artifact validation failed reason=empty path=%s", resolved)
            return None
        max_artifact_bytes = self._max_artifact_bytes()
        if size_bytes > max_artifact_bytes:
            logger.warning(
                "RipDock artifact validation failed reason=file_too_large path=%s size_bytes=%s max_artifact_bytes=%s",
                resolved,
                size_bytes,
                max_artifact_bytes,
            )
            return None

        mime_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"

        key = f"{message_id}:{resolved}:{stat.st_mtime_ns}:{size_bytes}"
        existing = self._generated_artifacts_by_key.get(key)
        if existing:
            return None

        artifact_id = str(uuid.uuid4())
        artifact = {
            "artifact_id": artifact_id,
            "conversation_id": conversation_id or "",
            "message_id": message_id,
            "source_message_id": message_id,
            "source_runtime_id": self.runtime_id,
            "filename": resolved.name,
            "path": str(resolved),
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sha256": self._sha256_file(resolved),
            "description": description if isinstance(description, str) and description.strip() else f"Generated artifact: {resolved.name}",
            "context": self._conversation_context_text(conversation_id, message_id),
        }
        self._generated_artifacts_by_key[key] = artifact_id
        self._generated_artifacts_by_id[artifact_id] = artifact
        self._artifact_ids_by_message_id.setdefault(message_id, []).append(artifact_id)
        logger.warning(
            "RipDock artifact_validated artifact_id=%s path=%s filename=%s mime_type=%s size_bytes=%s sha256=%s",
            artifact_id,
            artifact["path"],
            artifact["filename"],
            mime_type,
            size_bytes,
            artifact["sha256"],
        )
        return artifact

    def _sha256_file(self, path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    async def _start_generated_artifact_transfer(self, websocket, artifact):
        await self._start_embedded_artifact_transfer(websocket, artifact)

    async def _start_embedded_artifact_transfer(self, websocket, artifact):
        transfer_id = str(uuid.uuid4())
        transfer = self._artifact_transfer_record(artifact, transfer_id)
        transfer["download_url"] = self._embedded_artifact_download_url(transfer_id)
        self.transfers[transfer_id] = transfer
        logger.warning(
            "RipDock transfer_created mode=http_download artifact_id=%s transfer_id=%s filename=%s path=%s mime_type=%s size_bytes=%s download_url=%s",
            artifact["artifact_id"],
            transfer_id,
            artifact["filename"],
            artifact["path"],
            artifact["mime_type"],
            artifact["size_bytes"],
            transfer["download_url"],
        )
        stream = self._ripdock_message_stream(artifact["conversation_id"], artifact["message_id"], websocket=websocket)
        await stream.artifact_created(artifact, transfer_id=transfer_id, source="artifact_transfer")
        await stream.transfer_requested(artifact, transfer, source="artifact_transfer")

    async def _start_qa_transfer_failure(self, websocket, artifact, variant):
        transfer_id = str(uuid.uuid4())
        transfer = self._artifact_transfer_record(artifact, transfer_id)
        transfer["failure_variant"] = variant["tag"]
        transfer["download_url"] = self._qa_transfer_failure_download_url(transfer_id, variant["tag"])
        if variant["tag"] == "hash-mismatch":
            transfer["sha256"] = artifact["actual_sha256"]
        self.transfers[transfer_id] = transfer
        stream = self._ripdock_message_stream(artifact["conversation_id"], artifact["message_id"], websocket=websocket)
        if variant["tag"] == "runtime-reject":
            await stream.artifact_created(artifact, source="qa_transfer_failure")
            await stream.transfer_failed(artifact, transfer_id, variant["code"], variant["message"], source="qa_transfer_failure")
            return

        await stream.artifact_created(artifact, transfer_id=transfer_id, download_url=transfer["download_url"], source="qa_transfer_failure")
        await stream.transfer_requested(artifact, transfer, source="qa_transfer_failure")

    def _artifact_transfer_record(self, artifact, transfer_id):
        return {
            "transfer_id": transfer_id,
            "artifact_id": artifact["artifact_id"],
            "conversation_id": artifact["conversation_id"],
            "message_id": artifact["message_id"],
            "filename": artifact["filename"],
            "mime_type": artifact["mime_type"],
            "size_bytes": artifact["size_bytes"],
            "received_bytes": 0,
            "downloaded_bytes": 0,
            "sent_bytes": 0,
            "chunks": 0,
            "completed": False,
            "failed": False,
            "path": artifact["path"],
            "sha256": artifact["sha256"],
            "direction": "runtime_to_app",
            "pending_chunks": [],
        }

    def _qa_transfer_failure_artifact(self, variant, conversation_id, message_id):
        artifact_id = f"qa-transfer-{variant['tag']}"
        filename = variant["filename"]
        content = f"RipDock QA transfer failure fixture: {variant['tag']}\n".encode("utf-8")
        path = self._transfer_file_path(artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if variant["tag"] == "download":
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            path.write_bytes(content)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        declared_sha256 = actual_sha256
        if variant["tag"] == "hash-mismatch":
            declared_sha256 = "0" * 64
        return {
            "artifact_id": artifact_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "filename": filename,
            "mime_type": "text/plain",
            "size_bytes": len(content),
            "created_at": self._now_iso(),
            "description": variant["message"],
            "source_runtime_id": self.runtime_id,
            "source_message_id": message_id,
            "path": str(path),
            "sha256": declared_sha256,
            "actual_sha256": actual_sha256,
        }

    def _qa_transfer_failure_download_url(self, transfer_id, tag):
        if tag == "invalid-url":
            base, _source = self._embedded_runtime_transfer_base_url()
            parts = urlsplit(base)
            return urlunsplit(("https", parts.netloc, f"/invalid-transfer/{quote(transfer_id, safe='')}/artifact", "", ""))
        return self._embedded_artifact_download_url(transfer_id)

    def _transfer_failed(self, conversation_id, code, message, transfer_id=None):
        payload = {
            "message": message,
            "code": code,
        }
        if transfer_id:
            payload["transfer_id"] = transfer_id
        return {
            "type": "transfer.failed",
            "protocol_version": PROTOCOL_VERSION,
            "conversation_id": conversation_id or "",
            "payload": payload,
        }

    def _validate_transfer_request(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            payload = {}

        conversation_id = message.get("conversation_id") if isinstance(message, dict) else ""
        mime_type = payload.get("mime_type")
        size_bytes = payload.get("size_bytes")

        if (
            not isinstance(conversation_id, str)
            or not conversation_id
            or not isinstance(mime_type, str)
            or not isinstance(size_bytes, int)
            or size_bytes < 1
        ):
            return self._transfer_failed(
                conversation_id if isinstance(conversation_id, str) else "",
                "transfer.invalid_request",
                "Transfer request is invalid.",
            )

        if mime_type not in SUPPORTED_TRANSFER_MIME_TYPES:
            return self._transfer_failed(
                conversation_id,
                "transfer.unsupported_mime_type",
                "Transfer MIME type is not supported.",
            )

        if size_bytes > MAX_FILE_BYTES:
            return self._transfer_failed(
                conversation_id,
                "transfer.file_too_large",
                "Transfer file exceeds endpoint maximum size.",
            )

        return None

    async def _handle_transfer_request(self, websocket, message, embedded):
        failure = self._validate_transfer_request(message)
        if failure:
            await self._send_json_to(websocket, failure)
            return

        payload = message.get("payload")
        transfer_id = str(uuid.uuid4())
        transfer = {
            "transfer_id": transfer_id,
            "conversation_id": message.get("conversation_id"),
            "filename": payload.get("filename", "unnamed"),
            "mime_type": payload.get("mime_type"),
            "size_bytes": payload.get("size_bytes"),
            "received_bytes": 0,
            "chunks": 0,
            "completed": False,
            "failed": False,
            "direction": payload.get("direction") or "app_to_runtime",
            "app_websocket": websocket,
        }
        self.transfers[transfer_id] = transfer

        if not embedded:
            return

        transfer_url = self._embedded_transfer_url(transfer_id, "app")
        await self._send_json_to(
            websocket,
            {
                "type": "transfer.ready",
                "protocol_version": PROTOCOL_VERSION,
                "conversation_id": transfer["conversation_id"],
                "payload": {
                    "transfer_id": transfer_id,
                    "transfer_url": transfer_url,
                    "max_file_bytes": MAX_FILE_BYTES,
                    "max_chunk_bytes": MAX_CHUNK_BYTES,
                },
            },
        )

    def _runtime_transfer_url_for_ready(self, transfer_url):
        if urlsplit(transfer_url).scheme in {"ws", "wss", "http", "https"}:
            parsed = urlsplit(transfer_url)
        else:
            base, _source = self._embedded_runtime_transfer_base_url()
            parsed = urlsplit(urljoin(f"{base}/", transfer_url))
        path = parsed.path
        if path.endswith("/app") and path.startswith("/ripdock/transfer/"):
            path = path[:-len("/app")] + "/runtime"
        return urlunsplit(("wss", parsed.netloc, path, parsed.query, parsed.fragment))

    async def _handle_transfer_ready(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            return

        transfer_id = payload.get("transfer_id")
        transfer_url = payload.get("transfer_url")
        if not isinstance(transfer_id, str) or not isinstance(transfer_url, str):
            return

        artifact = self._artifact_for_transfer_ready(message, payload)
        if artifact:
            await self._handle_artifact_transfer_ready(message, payload, transfer_id, transfer_url, artifact)
            return

        transfer = self.transfers.get(transfer_id, {})
        if transfer.get("direction") == "runtime_to_app" or transfer.get("artifact_id"):
            logger.error(
                "RipDock transfer_failed transfer_id=%s code=runtime.transfer.wrong_mode transfer_direction=runtime_to_app transfer_mode=receive",
                transfer_id,
            )
            await self._fail_artifact_transfer(
                transfer,
                "runtime.transfer.wrong_mode",
                "Generated artifact transfer entered receive mode.",
            )
            return
        transfer.update(
            {
                "transfer_id": transfer_id,
                "conversation_id": message.get("conversation_id", transfer.get("conversation_id", "")),
                "received_bytes": transfer.get("received_bytes", 0),
                "chunks": transfer.get("chunks", 0),
                "completed": transfer.get("completed", False),
                "failed": transfer.get("failed", False),
                "transfer_url": self._runtime_transfer_url_for_ready(transfer_url),
            }
        )
        self.transfers[transfer_id] = transfer
        logger.warning(
            "RipDock transfer_socket_opening transfer_id=%s transfer_direction=app_to_runtime transfer_mode=receive transfer_url=%s",
            transfer_id,
            transfer["transfer_url"],
        )
        asyncio.create_task(self._receive_runtime_transfer(transfer_id))

    def _artifact_for_transfer_ready(self, message, payload):
        artifact_id = payload.get("artifact_id")
        artifact = self._pending_artifact_transfers.pop(artifact_id, None) if isinstance(artifact_id, str) else None
        if not artifact:
            artifact = self._match_pending_artifact_transfer(message, payload)
        if artifact:
            return artifact
        if payload.get("direction") == "runtime_to_app" or isinstance(artifact_id, str):
            logger.warning(
                "RipDock artifact transfer ready ignored reason=unknown_artifact artifact_id=%s",
                artifact_id,
            )
        return None

    async def _handle_artifact_transfer_ready(self, message, payload, transfer_id, transfer_url, artifact=None):
        if not artifact:
            artifact = self._artifact_for_transfer_ready(message, payload)
        if not artifact:
            logger.warning(
                "RipDock artifact transfer ready ignored reason=unknown_artifact transfer_id=%s artifact_id=%s transfer_direction=runtime_to_app transfer_mode=send",
                transfer_id,
                payload.get("artifact_id"),
            )
            return

        transfer = self._artifact_transfer_record(artifact, transfer_id)
        transfer["transfer_url"] = self._runtime_transfer_url_for_ready(transfer_url)
        max_chunk_bytes = payload.get("max_chunk_bytes")
        if isinstance(max_chunk_bytes, int) and max_chunk_bytes > 0:
            transfer["max_chunk_bytes"] = max_chunk_bytes
        self.transfers[transfer_id] = transfer
        logger.warning(
            "RipDock transfer_created artifact_id=%s transfer_id=%s transfer_direction=runtime_to_app transfer_mode=send filename=%s path=%s mime_type=%s size_bytes=%s transfer_url=%s",
            artifact["artifact_id"],
            transfer_id,
            artifact["filename"],
            artifact["path"],
            artifact["mime_type"],
            artifact["size_bytes"],
            transfer["transfer_url"],
        )
        websocket = self._websocket_for_ripdock_send(
            conversation_id=artifact.get("conversation_id"),
            message_id=artifact.get("message_id"),
        )
        if not websocket:
            logger.warning(
                "RipDock artifact transfer dropped reason=missing_routed_websocket conversation=%s message=%s artifact_id=%s transfer_id=%s",
                artifact.get("conversation_id"),
                artifact.get("message_id"),
                artifact.get("artifact_id"),
                transfer_id,
            )
            return
        stream = self._ripdock_message_stream(artifact["conversation_id"], artifact["message_id"], websocket=websocket)
        await stream.artifact_created(artifact, transfer_id=transfer_id, source="artifact_transfer_socket")
        await self._send_artifact_transfer(transfer_id)

    def _match_pending_artifact_transfer(self, message, payload):
        conversation_id = message.get("conversation_id", "")
        filename = payload.get("filename")
        mime_type = payload.get("mime_type")
        size_bytes = payload.get("size_bytes")
        for artifact_id, artifact in list(self._pending_artifact_transfers.items()):
            if artifact.get("conversation_id") != conversation_id:
                continue
            if isinstance(filename, str) and filename and artifact.get("filename") != filename:
                continue
            if isinstance(mime_type, str) and mime_type and artifact.get("mime_type") != mime_type:
                continue
            if isinstance(size_bytes, int) and artifact.get("size_bytes") != size_bytes:
                continue
            if filename or mime_type or isinstance(size_bytes, int) or len(self._pending_artifact_transfers) == 1:
                self._pending_artifact_transfers.pop(artifact_id, None)
                logger.warning(
                    "RipDock transfer_ready matched pending artifact artifact_id=%s filename=%s mime_type=%s size_bytes=%s",
                    artifact_id,
                    artifact.get("filename"),
                    artifact.get("mime_type"),
                    artifact.get("size_bytes"),
                )
                return artifact
        return None

    async def _send_artifact_transfer(self, transfer_id):
        transfer = self.transfers.get(transfer_id)
        if not transfer:
            logger.warning("RipDock transfer_failed transfer_id=%s code=runtime.transfer.unknown", transfer_id)
            return

        timeout = self._artifact_transfer_timeout()
        try:
            path = Path(transfer.get("path", ""))
            if not path.exists() or not path.is_file():
                await self._fail_artifact_transfer(
                    transfer,
                    "runtime.transfer.missing_file",
                    f"Artifact file is unavailable: {path}",
                )
                return
            logger.warning(
                "RipDock transfer_started artifact_id=%s transfer_id=%s transfer_direction=runtime_to_app transfer_mode=send path=%s size_bytes=%s mime_type=%s timeout_seconds=%s chunk_bytes=%s",
                transfer.get("artifact_id"),
                transfer_id,
                path,
                path.stat().st_size,
                transfer.get("mime_type"),
                timeout,
                self._artifact_transfer_chunk_bytes(transfer),
            )
            await asyncio.wait_for(self._send_artifact_transfer_chunks(transfer), timeout=timeout)
            transfer["completed"] = transfer.get("sent_bytes") == transfer.get("size_bytes")
            if transfer["completed"]:
                logger.warning(
                    "RipDock transfer_completed artifact_id=%s transfer_id=%s chunk_count=%s size_bytes=%s",
                    transfer.get("artifact_id"),
                    transfer_id,
                    transfer.get("chunks"),
                    transfer.get("sent_bytes"),
                )
                await self._send_json_to(
                    self._transfer_app_websocket(transfer),
                    {
                        "type": "runtime.transfer.completed",
                        "protocol_version": PROTOCOL_VERSION,
                        "conversation_id": transfer.get("conversation_id", ""),
                        "message_id": transfer.get("message_id"),
                        "payload": {
                            "transfer_id": transfer_id,
                            "artifact_id": transfer.get("artifact_id"),
                            "size_bytes": transfer.get("sent_bytes"),
                            "sha256": transfer.get("sha256"),
                        },
                    },
                )
            else:
                await self._fail_artifact_transfer(transfer, "runtime.transfer.size_mismatch", "Artifact transfer completed with an unexpected byte count.")
        except asyncio.TimeoutError:
            await self._fail_artifact_transfer(transfer, "runtime.transfer.timeout", "Artifact transfer timed out.")
        except Exception as exc:
            logger.exception("RipDock artifact transfer failed transfer_id=%s", transfer_id)
            await self._fail_artifact_transfer(transfer, "runtime.transfer.failed", str(exc) or "Artifact transfer failed.")

    async def _send_artifact_transfer_chunks(self, transfer):
        logger.warning(
            "RipDock transfer_socket_opening artifact_id=%s transfer_id=%s transfer_direction=runtime_to_app transfer_mode=send transfer_url=%s",
            transfer.get("artifact_id"),
            transfer.get("transfer_id"),
            transfer.get("transfer_url"),
        )
        async with websockets.connect(transfer["transfer_url"]) as transfer_ws:
            logger.warning(
                "RipDock transfer_socket_connected artifact_id=%s transfer_id=%s transfer_direction=runtime_to_app transfer_mode=send",
                transfer.get("artifact_id"),
                transfer.get("transfer_id"),
            )
            file_path = Path(transfer["path"])
            chunk_bytes = self._artifact_transfer_chunk_bytes(transfer)
            with file_path.open("rb") as handle:
                logger.warning(
                    "RipDock file_opened artifact_id=%s transfer_id=%s transfer_direction=runtime_to_app transfer_mode=send path=%s size_bytes=%s mime_type=%s chunk_bytes=%s",
                    transfer.get("artifact_id"),
                    transfer.get("transfer_id"),
                    file_path,
                    file_path.stat().st_size,
                    transfer.get("mime_type"),
                    chunk_bytes,
                )
                while True:
                    chunk = handle.read(chunk_bytes)
                    if not chunk:
                        break
                    await transfer_ws.send(chunk)
                    transfer["sent_bytes"] = transfer.get("sent_bytes", 0) + len(chunk)
                    transfer["chunks"] = transfer.get("chunks", 0) + 1
                    logger.warning(
                        "RipDock chunk_sent artifact_id=%s transfer_id=%s transfer_direction=runtime_to_app transfer_mode=send chunk_index=%s chunk_bytes=%s sent_bytes=%s size_bytes=%s",
                        transfer.get("artifact_id"),
                        transfer.get("transfer_id"),
                        transfer.get("chunks") - 1,
                        len(chunk),
                        transfer.get("sent_bytes"),
                        transfer.get("size_bytes"),
                    )

    async def _fail_artifact_transfer(self, transfer, code, message):
        transfer["failed"] = True
        transfer["failure"] = message
        logger.warning(
            "RipDock transfer_failed artifact_id=%s transfer_id=%s code=%s message=%s",
            transfer.get("artifact_id"),
            transfer.get("transfer_id"),
            code,
            message,
        )
        artifact = {
            "artifact_id": transfer.get("artifact_id"),
            "conversation_id": transfer.get("conversation_id", ""),
            "message_id": transfer.get("message_id"),
        }
        stream = self._ripdock_message_stream(
            transfer.get("conversation_id", ""),
            transfer.get("message_id"),
            websocket=self._transfer_app_websocket(transfer),
        )
        await stream.transfer_failed(artifact, transfer.get("transfer_id"), code, message, source="artifact_transfer")

    async def _receive_runtime_transfer(self, transfer_id):
        transfer = self.transfers.get(transfer_id)
        if not transfer:
            return
        if transfer.get("direction") == "runtime_to_app" or transfer.get("artifact_id"):
            logger.error(
                "RipDock transfer_failed transfer_id=%s artifact_id=%s code=runtime.transfer.wrong_mode transfer_direction=runtime_to_app transfer_mode=receive",
                transfer_id,
                transfer.get("artifact_id"),
            )
            await self._fail_artifact_transfer(
                transfer,
                "runtime.transfer.wrong_mode",
                "Generated artifact transfer entered receive mode.",
            )
            return

        transfer_path = self._transfer_file_path(transfer_id)
        transfer_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            async with websockets.connect(transfer["transfer_url"]) as transfer_ws:
                with transfer_path.open("wb") as handle:
                    async for chunk in transfer_ws:
                        result = self._validate_transfer_chunk(transfer, chunk)
                        if result:
                            transfer["failed"] = True
                            transfer["failure"] = result["message"]
                            await transfer_ws.close(code=1009, reason=result["message"])
                            return
                        handle.write(chunk)
            transfer["path"] = str(transfer_path)
        except Exception:
            logger.exception("Runtime transfer receive failed")
            transfer["failed"] = True

    def _validate_transfer_chunk(self, transfer, chunk):
        if isinstance(chunk, str):
            return {
                "message": "Transfer chunks must be binary.",
                "event": self._transfer_failed(
                    transfer.get("conversation_id", ""),
                    "transfer.invalid_chunk",
                    "Transfer chunks must be binary.",
                    transfer.get("transfer_id"),
                ),
            }

        chunk_size = len(chunk)
        if chunk_size > MAX_CHUNK_BYTES:
            return {
                "message": "Transfer chunk exceeds endpoint maximum size.",
                "event": self._transfer_failed(
                    transfer.get("conversation_id", ""),
                    "transfer.chunk_too_large",
                    "Transfer chunk exceeds endpoint maximum size.",
                    transfer.get("transfer_id"),
                ),
            }

        next_size = transfer.get("received_bytes", 0) + chunk_size
        if next_size > MAX_FILE_BYTES or next_size > transfer.get("size_bytes", MAX_FILE_BYTES):
            return {
                "message": "Transfer exceeds endpoint maximum size.",
                "event": self._transfer_failed(
                    transfer.get("conversation_id", ""),
                    "transfer.file_too_large",
                    "Transfer exceeds endpoint maximum size.",
                    transfer.get("transfer_id"),
                ),
            }

        transfer["received_bytes"] = next_size
        transfer["chunks"] = transfer.get("chunks", 0) + 1
        return None

    async def _complete_embedded_transfer(self, transfer, transfer_socket=None):
        transfer["completed"] = transfer.get("received_bytes") == transfer.get("size_bytes")
        if transfer["completed"]:
            if transfer.get("direction") != "runtime_to_app":
                self._register_uploaded_transfer_artifact(transfer)
            event = {
                "type": "transfer.completed",
                "protocol_version": PROTOCOL_VERSION,
                "conversation_id": transfer.get("conversation_id", ""),
                "payload": {
                    "transfer_id": transfer.get("transfer_id"),
                    "size_bytes": transfer.get("received_bytes", 0),
                    "mime_type": transfer.get("mime_type"),
                },
            }
            await self._send_json_to(self._transfer_app_websocket(transfer), event)
            if transfer_socket is not None:
                await self._send_json_to(transfer_socket, event)
        else:
            transfer["failed"] = True
            event = self._transfer_failed(
                transfer.get("conversation_id", ""),
                "transfer.byte_count_mismatch",
                "Transfer completed with an unexpected byte count.",
                transfer.get("transfer_id"),
            )
            await self._send_json_to(self._transfer_app_websocket(transfer), event)
            if transfer_socket is not None:
                await self._send_json_to(transfer_socket, event)
        self._log_transfer_summary(transfer)

    def _complete_transfer(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            return
        transfer_id = payload.get("transfer_id")
        if not isinstance(transfer_id, str):
            return
        transfer = self.transfers.get(transfer_id)
        if not transfer:
            transfer = {
                "transfer_id": transfer_id,
                "conversation_id": message.get("conversation_id", ""),
                "filename": "unnamed",
                "mime_type": payload.get("mime_type"),
                "size_bytes": payload.get("size_bytes"),
                "received_bytes": 0,
                "chunks": 0,
            }
            self.transfers[transfer_id] = transfer
        if isinstance(payload.get("size_bytes"), int):
            transfer["size_bytes"] = payload["size_bytes"]
        if isinstance(payload.get("mime_type"), str):
            transfer["mime_type"] = payload["mime_type"]
        byte_count = transfer.get("sent_bytes") if transfer.get("direction") == "runtime_to_app" else transfer.get("received_bytes")
        transfer["completed"] = byte_count == transfer.get("size_bytes")
        if not transfer["completed"]:
            transfer["failed"] = True
            label = "sent" if transfer.get("direction") == "runtime_to_app" else "received"
            transfer["failure"] = f"{label} {byte_count} of {transfer.get('size_bytes')} bytes"
        self._log_transfer_summary(transfer)

    def _register_uploaded_transfer_artifact(self, transfer):
        path = transfer.get("path")
        filename = transfer.get("filename")
        conversation_id = transfer.get("conversation_id", "")
        transfer_id = transfer.get("transfer_id")
        if not path or not filename or not transfer_id:
            return
        try:
            resolved = Path(path).resolve()
        except Exception:
            return
        if not resolved.exists() or not resolved.is_file():
            return
        artifact_key = f"uploaded-transfer:{transfer_id}:{resolved}:{resolved.stat().st_mtime_ns}:{resolved.stat().st_size}"
        existing = self._generated_artifacts_by_key.get(artifact_key)
        if existing:
            return
        artifact_id = f"upload-{transfer_id}"
        artifact = {
            "artifact_id": artifact_id,
            "conversation_id": conversation_id or "",
            "message_id": "",
            "source_message_id": "",
            "source_runtime_id": self.runtime_id,
            "filename": filename,
            "path": str(resolved),
            "mime_type": transfer.get("mime_type") or mimetypes.guess_type(str(filename))[0] or "application/octet-stream",
            "size_bytes": transfer.get("received_bytes") or transfer.get("size_bytes") or resolved.stat().st_size,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sha256": self._sha256_file(resolved),
            "description": f"Uploaded attachment: {filename}",
            "context": f"Uploaded attachment {filename} in conversation {conversation_id}",
        }
        self._generated_artifacts_by_key[artifact_key] = artifact_id
        self._generated_artifacts_by_id[artifact_id] = artifact
        logger.warning(
            "RipDock uploaded_transfer_registered artifact_id=%s transfer_id=%s conversation=%s filename=%s path=%s mime_type=%s size_bytes=%s",
            artifact_id,
            transfer_id,
            conversation_id,
            filename,
            artifact["path"],
            artifact["mime_type"],
            artifact["size_bytes"],
        )

    def _fail_transfer(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            return
        transfer_id = payload.get("transfer_id")
        transfer = self.transfers.get(transfer_id) if isinstance(transfer_id, str) else None
        if transfer:
            transfer["failed"] = True
            transfer["failure"] = payload.get("message")
            self._log_transfer_summary(transfer)
        logger.warning("Transfer failed: %s %s", payload.get("code"), payload.get("message"))

    def _complete_runtime_artifact_transfer_ack(self, message):
        payload = message.get("payload") if isinstance(message, dict) else {}
        if not isinstance(payload, dict):
            return
        transfer_id = payload.get("transfer_id")
        transfer = self.transfers.get(transfer_id) if isinstance(transfer_id, str) else None
        if not transfer:
            logger.warning("RipDock runtime artifact ack ignored reason=unknown_transfer transfer_id=%s", transfer_id)
            return
        if transfer.get("direction") != "runtime_to_app":
            logger.warning("RipDock runtime artifact ack ignored reason=wrong_direction transfer_id=%s", transfer_id)
            return

        size_bytes = payload.get("size_bytes")
        sha256 = payload.get("sha256")
        transfer["downloaded_bytes"] = size_bytes if isinstance(size_bytes, int) else 0
        transfer["observed_sha256"] = sha256
        transfer["completed"] = (
            transfer["downloaded_bytes"] == transfer.get("size_bytes")
            and isinstance(sha256, str)
            and sha256.lower() == str(transfer.get("sha256", "")).lower()
        )
        if transfer["completed"]:
            transfer["failed"] = False
            logger.warning(
                "RipDock transfer_completed_ack artifact_id=%s transfer_id=%s size_bytes=%s sha256=%s",
                transfer.get("artifact_id"),
                transfer_id,
                transfer["downloaded_bytes"],
                sha256,
            )
        else:
            transfer["failed"] = True
            transfer["failure"] = "App acknowledgement did not match declared artifact bytes."
            logger.warning(
                "RipDock transfer_ack_mismatch artifact_id=%s transfer_id=%s downloaded_bytes=%s expected_bytes=%s observed_sha256=%s expected_sha256=%s",
                transfer.get("artifact_id"),
                transfer_id,
                transfer["downloaded_bytes"],
                transfer.get("size_bytes"),
                sha256,
                transfer.get("sha256"),
            )
        self._log_transfer_summary(transfer)

    def _log_transfer_summary(self, transfer):
        logger.warning(
            "RipDock transfer summary filename=%s mime_type=%s size_bytes=%s received_bytes=%s sent_bytes=%s downloaded_bytes=%s transfer_id=%s completed=%s failed=%s",
            transfer.get("filename", "unnamed"),
            transfer.get("mime_type"),
            transfer.get("size_bytes"),
            transfer.get("received_bytes"),
            transfer.get("sent_bytes"),
            transfer.get("downloaded_bytes"),
            transfer.get("transfer_id"),
            transfer.get("completed", False),
            transfer.get("failed", False),
        )

    def _direct_pairing_payload(self, pairing_code, public_host, public_port):
        runtime_url = os.getenv(
            "RIPDOCK_DIRECT_RUNTIME_URL",
            "https://localhost:8443",
        ).rstrip("/")
        self._require_secure_transport_url(runtime_url, "RIPDOCK_DIRECT_RUNTIME_URL")
        pairing_base_url = self._websocket_url(runtime_url, "RIPDOCK_DIRECT_RUNTIME_URL").rstrip("/")
        return {
            "runtime_url": f"{pairing_base_url}/ripdock/app/pair/{quote(str(pairing_code), safe='')}",
            "runtime_id": self.runtime_id,
            "runtime_public_key_fingerprint": self.runtime_identity.get("publicKeyFingerprint"),
            "runtime_identity": self._public_runtime_identity(),
            "pairing_code": pairing_code,
        }

    def _log_pairing(self, pairing_code, payload):
        if not isinstance(pairing_code, str) or not pairing_code:
            return

        logger.warning("Pairing Code:\n%s", pairing_code)

    def _create_pairing_code(self):
        return f"{secrets.randbelow(1_000_000):06d}"

    def _create_session_id(self):
        return str(uuid.uuid4())

    def _new_message_id(self):
        return str(uuid.uuid4())

    def _pairing_code_matches(self, pairing_code):
        created_at = getattr(self, "pairing_code_created_at", None)
        ttl = self._pairing_ttl_seconds()
        if isinstance(created_at, (int, float)) and time.time() - created_at > ttl:
            return False
        return (
            isinstance(pairing_code, str)
            and pairing_code == self.pairing_code
            and not self.pairing_bound
        )

    def _runtime_error(self, message, conversation_id=None, code="runtime_error"):
        event = {
            "type": "error",
            "protocol_version": PROTOCOL_VERSION,
            "message": message,
            "code": code,
        }
        if conversation_id:
            event["conversation_id"] = conversation_id
        return event

    def _pairing_error(self):
        return self._runtime_error(
            "Pairing code is invalid or expired.",
            code="pairing.invalid",
        )

    def _app_capabilities_for_current_session(self):
        return self.app_capabilities_by_session.get(
            self._metadata_session_key(),
            self._default_client_capabilities(),
        )

    def _metadata_session_key(self):
        return self.session_id or "unknown"

    def _default_client_capabilities(self):
        return {
            "type": "app.capabilities",
            "protocol_version": PROTOCOL_VERSION,
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
                "features": {
                    "streaming": True,
                    "semantic_blocks": True,
                    "attachments": True,
                    "inline_images": True,
                    "tool_cards": True,
                    "html": False,
                    "generated_artifacts": True,
                    "runtime_transfers": True,
                    "artifact_http_downloads": True,
                    "artifact_ack": True,
                },
                "client_capabilities": RIPDOCK_RICH_TEXT_V1_CAPABILITIES,
            },
        }

    def formatting_capabilities_for_current_session(self):
        capabilities = self._app_capabilities_for_current_session()
        payload = capabilities.get("payload") if isinstance(capabilities, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        client_capabilities = payload.get("client_capabilities")
        if not isinstance(client_capabilities, dict):
            client_capabilities = RIPDOCK_RICH_TEXT_V1_CAPABILITIES
        return {
            "type": "app.capabilities",
            "protocol_version": PROTOCOL_VERSION,
            "payload": {
                "content_types": payload.get("content_types", []),
                "features": payload.get("features", {}),
                "client_capabilities": client_capabilities,
            },
        }

    def _log_advertised_client_capabilities(self):
        payload = self.formatting_capabilities_for_current_session().get("payload", {})
        logger.warning(
            "RipDock advertised client capabilities session=%s content_types=%s features=%s client_capabilities=%s",
            self._redacted_session_id(self._metadata_session_key()),
            payload.get("content_types", []),
            payload.get("features", {}),
            payload.get("client_capabilities", {}),
        )

    def _app_supports_semantic_blocks(self, capabilities=None):
        if capabilities is None:
            capabilities = self._app_capabilities_for_current_session()
        payload = capabilities.get("payload") if isinstance(capabilities, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        features = payload.get("features")
        if not isinstance(features, dict):
            features = {}
        return features.get("semantic_blocks") is True

    def _app_supports_content_type(self, mime_type, capabilities=None):
        if capabilities is None:
            capabilities = self._app_capabilities_for_current_session()
        payload = capabilities.get("payload") if isinstance(capabilities, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        content_types = payload.get("content_types")
        if not isinstance(content_types, list):
            content_types = []
        return mime_type in content_types

    def _supported_semantic_blocks(self, capabilities=None, blocks=None):
        if blocks is None:
            blocks = SEMANTIC_BLOCK_DEMOS
        if not self._app_supports_semantic_blocks(capabilities):
            return []
        return [
            block
            for block in blocks
            if self._app_supports_content_type(block.get("mime_type"), capabilities)
        ]

    def _plain_text_for_blocks(self, blocks):
        parts = []
        for block in blocks:
            title = block.get("title")
            content = block.get("content", "")
            parts.append(f"{title}\n{content}" if title else content)
        return "\n\n".join(parts)

    def _store_app_capabilities(self, message):
        session_id = self._metadata_session_key()
        self.app_capabilities_by_session[session_id] = message

        payload = message.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        content_types = payload.get("content_types")
        if not isinstance(content_types, list):
            content_types = []

        features = payload.get("features")
        if not isinstance(features, dict):
            features = {}
        artifact_limits = payload.get("artifact_limits")
        if not isinstance(artifact_limits, dict):
            artifact_limits = {}

        logger.warning(
            "RipDock app.capabilities session=%s content_types=%s features=%s artifact_limits=%s client_capabilities=%s",
            self._redacted_session_id(session_id),
            content_types,
            features,
            artifact_limits,
            self.formatting_capabilities_for_current_session().get("payload", {}).get("client_capabilities", {}),
        )

    async def send_message(self, channel_id, text, **kwargs):
        logger.info(
            "RipDock send_message channel=%s text=%s",
            channel_id,
            text,
        )

        return await self.send(chat_id=channel_id, content=text, **kwargs)

    def _send_result(self, success=True, message_id=None, error=None, raw_response=None, retryable=False):
        try:
            return SendResult(
                success=success,
                message_id=message_id,
                error=error,
                raw_response=raw_response,
                retryable=retryable,
            )
        except TypeError:
            result = type("RipDockSendResult", (), {})()
            result.success = success
            result.message_id = message_id
            result.error = error
            result.raw_response = raw_response
            result.retryable = retryable
            return result

    async def send(self, chat_id: str = None, content: str = "", reply_to=None, metadata=None, **kwargs):
        conversation_id = chat_id or kwargs.get("channel_id") or kwargs.get("conversation_id")
        if self._is_generation_completed(conversation_id):
            logger.warning(
                "RipDock stream suppressed reason=completed conversation=%s generation=%s",
                conversation_id,
                self._current_generation(conversation_id),
            )
            return self._send_result(success=True, message_id=None)
        if self._is_generation_interrupted(conversation_id):
            logger.warning(
                "RipDock stream suppressed reason=interrupted conversation=%s generation=%s",
                conversation_id,
                self._current_generation(conversation_id),
            )
            return self._send_result(success=True, message_id=None)
        websocket = self._websocket_for_ripdock_send(metadata, conversation_id=conversation_id)
        message_id = self._new_message_id()

        normalized = self._normalize_stream_content(content or "")
        intent_handled, normalized = await self._handle_or_buffer_runtime_intent_output(
            websocket,
            conversation_id,
            message_id,
            normalized,
            finalize=False,
        )
        if intent_handled:
            return self._send_result(success=True, message_id=message_id)

        normalized = await self._emit_and_strip_hermes_tool_progress(websocket, conversation_id, message_id, normalized)
        content = self._prepare_runtime_output(normalized)
        if not content:
            logger.warning(
                "RipDock Hermes empty send ignored conversation=%s message=%s",
                conversation_id,
                message_id,
            )
            return self._send_result(success=True, message_id=message_id)
        if self._should_suppress_home_channel_notice(conversation_id, content):
            self._record_outbound_message_attempt(conversation_id)
            self._suppressed_home_channel_notice_conversations.add(conversation_id)
            logger.warning(
                "RipDock Hermes home-channel notice suppressed conversation=%s message=%s",
                conversation_id,
                message_id,
            )
            return self._send_result(success=True, message_id=message_id)
        self._record_outbound_message_attempt(conversation_id)

        logger.warning(
            "RipDock Hermes send opened stream conversation=%s message=%s bytes=%s",
            conversation_id,
            message_id,
            len(content.encode("utf-8")),
        )

        stream = self._stream_for(conversation_id, message_id, websocket=websocket)
        runtime_message_id = stream.message_id
        if message_id != runtime_message_id:
            self._outbound_conversation_by_message_id[message_id] = conversation_id
            self._outbound_content_by_message_id[message_id] = content
            self._remember_outbound_websocket(message_id, websocket)
        await stream.delta(content, source="hermes_send")

        return self._send_result(success=True, message_id=message_id)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False):
        conversation_id = self._outbound_conversation_by_message_id.get(message_id, chat_id)
        runtime_message_id = self._message_id_for_stream(conversation_id, message_id)
        if self._is_generation_completed(conversation_id):
            logger.warning(
                "RipDock stream edit suppressed reason=completed conversation=%s message=%s generation=%s finalize=%s",
                conversation_id,
                runtime_message_id,
                self._current_generation(conversation_id),
                finalize,
            )
            return self._send_result(success=True, message_id=runtime_message_id)
        if message_id in getattr(self, "_completed_message_ids", set()):
            logger.warning(
                "RipDock Hermes edit ignored reason=already_completed conversation=%s message=%s finalize=%s",
                conversation_id,
                message_id,
                finalize,
            )
            return self._send_result(success=True, message_id=message_id)
        if self._is_generation_interrupted(conversation_id):
            logger.warning(
                "RipDock stream edit suppressed reason=interrupted conversation=%s message=%s generation=%s",
                conversation_id,
                message_id,
                self._current_generation(conversation_id),
            )
            return self._send_result(success=True, message_id=message_id)
        normalized = self._normalize_stream_content(content or "")
        intent_handled, normalized = await self._handle_or_buffer_runtime_intent_output(
            self._websocket_for_ripdock_send(conversation_id=conversation_id, message_id=message_id),
            conversation_id,
            message_id,
            normalized,
            finalize=finalize,
        )
        if intent_handled:
            return self._send_result(success=True, message_id=message_id)

        websocket = self._websocket_for_ripdock_send(conversation_id=conversation_id, message_id=message_id)
        normalized = await self._emit_and_strip_hermes_tool_progress(websocket, conversation_id, message_id, normalized)
        content = self._prepare_runtime_output(normalized)
        platform_previous = self._outbound_content_by_message_id.get(message_id, "")
        runtime_previous = self._outbound_content_by_message_id.get(runtime_message_id, "")
        if platform_previous and content.startswith(platform_previous):
            delta = content[len(platform_previous):]
        elif content.startswith(runtime_previous):
            delta = content[len(runtime_previous):]
        elif content in runtime_previous:
            delta = ""
        else:
            delta = content
        if not content:
            logger.warning(
                "RipDock Hermes empty edit ignored conversation=%s message=%s finalize=%s",
                conversation_id,
                message_id,
                finalize,
            )
            return self._send_result(success=True, message_id=message_id)

        self._outbound_conversation_by_message_id[message_id] = conversation_id
        self._outbound_content_by_message_id[message_id] = content
        self._remember_outbound_websocket(message_id, websocket)
        stream = self._ripdock_message_stream(conversation_id, runtime_message_id, websocket=websocket)
        if delta:
            await stream.delta(delta, source="hermes_edit_snapshot")
        if finalize:
            logger.warning(
                "RipDock Hermes platform message finalized conversation=%s message=%s runtime_message=%s",
                conversation_id,
                message_id,
                runtime_message_id,
            )

        return self._send_result(success=True, message_id=runtime_message_id)

    def _normalize_stream_content(self, content):
        if not isinstance(content, str):
            return ""
        for cursor in (" ▉", "▉"):
            if content.endswith(cursor):
                return content[: -len(cursor)]
        return content

    def _record_outbound_message_attempt(self, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id:
            return
        counts = getattr(self, "_outbound_message_count_by_conversation", None)
        if counts is None:
            counts = {}
            self._outbound_message_count_by_conversation = counts
        counts[conversation_id] = counts.get(conversation_id, 0) + 1

    def _should_suppress_home_channel_notice(self, conversation_id, content):
        if not isinstance(conversation_id, str) or not conversation_id:
            return False
        counts = getattr(self, "_outbound_message_count_by_conversation", {})
        if counts.get(conversation_id, 0) != 0:
            return False
        return self._is_home_channel_notice(content)

    def _is_home_channel_notice(self, content):
        if not isinstance(content, str):
            return False
        return self._strip_home_channel_notice_prefix(content) == ""

    def _sync_hermes_display_settings(self):
        progress_mode = self._hermes_tool_progress_mode()
        os.environ["HERMES_TOOL_PROGRESS_MODE"] = progress_mode
        logger.warning(
            "RipDock Hermes display settings runtime_id=%s tool_progress_mode=%s",
            self.runtime_id,
            progress_mode,
        )

    def _install_ripdock_display_override(self):
        try:
            import gateway.display_config as display_config
        except Exception:
            return
        if getattr(display_config, "_ripdock_runtime_override", False):
            return

        original_resolve = display_config.resolve_display_setting

        def resolve_with_ripdock_runtime(user_config, platform_key, setting, fallback=None):
            if platform_key == "ripdock":
                if setting == "streaming":
                    display_config_value = user_config.get("display") if isinstance(user_config, dict) else {}
                    platforms = display_config_value.get("platforms") if isinstance(display_config_value, dict) else {}
                    ripdock_config = platforms.get("ripdock") if isinstance(platforms, dict) else {}
                    if isinstance(ripdock_config, dict) and ripdock_config.get("streaming") is not None:
                        return original_resolve(user_config, platform_key, setting, fallback)
                    return True
                if setting == "tool_progress":
                    return os.getenv("HERMES_TOOL_PROGRESS_MODE", "all")
                if setting == "tool_preview_length":
                    return 0
            return original_resolve(user_config, platform_key, setting, fallback)

        display_config.resolve_display_setting = resolve_with_ripdock_runtime
        display_config._ripdock_runtime_override = True

    def _install_ripdock_toolset_inheritance_override(self):
        try:
            import hermes_cli.tools_config as tools_config
        except Exception:
            return
        if getattr(tools_config, "_ripdock_toolset_inheritance_override", False):
            return

        original_get_platform_tools = tools_config._get_platform_tools

        def get_platform_tools_with_ripdock_inheritance(config, platform, **kwargs):
            if platform == "ripdock" and isinstance(config, dict):
                platform_toolsets = config.get("platform_toolsets")
                if isinstance(platform_toolsets, dict) and "ripdock" not in platform_toolsets:
                    cli_toolsets = platform_toolsets.get("cli")
                    if isinstance(cli_toolsets, list):
                        inherited_config = dict(config)
                        inherited_platform_toolsets = dict(platform_toolsets)
                        inherited_platform_toolsets["ripdock"] = list(cli_toolsets)
                        inherited_config["platform_toolsets"] = inherited_platform_toolsets
                        return original_get_platform_tools(inherited_config, platform, **kwargs)
            return original_get_platform_tools(config, platform, **kwargs)

        tools_config._get_platform_tools = get_platform_tools_with_ripdock_inheritance
        tools_config._ripdock_toolset_inheritance_override = True

    def _install_ripdock_context_diagnostics_override(self):
        try:
            import run_agent
        except Exception:
            return

        agent_class = getattr(run_agent, "AIAgent", None)
        if agent_class is None or not hasattr(agent_class, "_build_api_kwargs"):
            return
        if getattr(agent_class, "_ripdock_context_diagnostics_override", False):
            return

        original_build_api_kwargs = agent_class._build_api_kwargs

        def build_api_kwargs_with_ripdock_context_diagnostics(agent, api_messages):
            api_kwargs = original_build_api_kwargs(agent, api_messages)
            try:
                self._write_ripdock_context_diagnostics(agent, api_messages)
            except Exception:
                logger.exception("RipDock context diagnostics write failed")
            return api_kwargs

        agent_class._build_api_kwargs = build_api_kwargs_with_ripdock_context_diagnostics
        agent_class._ripdock_context_diagnostics_override = True

    def _write_ripdock_context_diagnostics(self, agent, api_messages):
        diagnostics_dir = os.getenv("RIPDOCK_CONTEXT_DIAGNOSTICS_DIR", "").strip()
        enabled = diagnostics_dir or os.getenv("RIPDOCK_CONTEXT_DIAGNOSTICS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not enabled:
            return
        if getattr(agent, "platform", None) != "ripdock":
            return

        try:
            from agent.model_metadata import estimate_messages_tokens_rough
        except Exception:
            estimate_messages_tokens_rough = None

        tools = getattr(agent, "tools", None) or []
        self._finalize_hermes_tool_progress_names_from_agent(tools)
        approx_message_tokens = None
        if estimate_messages_tokens_rough is not None:
            try:
                approx_message_tokens = estimate_messages_tokens_rough(api_messages)
            except Exception:
                approx_message_tokens = None

        if diagnostics_dir:
            output_dir = Path(diagnostics_dir)
        else:
            output_dir = _hermes_home() / "ripdock" / "diagnostics" / "context"
        output_dir.mkdir(parents=True, exist_ok=True)

        session_id = str(getattr(agent, "session_id", "") or "session").replace("/", "_")
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        filename_ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        payload = {
            "schema": "ripdock.context_diagnostics.v1",
            "created_at": created_at,
            "platform": getattr(agent, "platform", None),
            "session_id": getattr(agent, "session_id", None),
            "model": getattr(agent, "model", None),
            "provider": getattr(agent, "provider", None),
            "api_mode": getattr(agent, "api_mode", None),
            "message_count": len(api_messages) if isinstance(api_messages, list) else None,
            "tool_count": len(tools) if isinstance(tools, list) else None,
            "approx_message_tokens": approx_message_tokens,
            "messages": api_messages,
            "tools": tools,
        }
        output_path = output_dir / f"{filename_ts}-{session_id}.json"
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
        logger.warning(
            "RipDock context diagnostics wrote path=%s message_count=%s tool_count=%s approx_message_tokens=%s",
            output_path,
            payload["message_count"],
            payload["tool_count"],
            payload["approx_message_tokens"],
        )

    def _load_hermes_tool_progress_names(self):
        try:
            import model_tools  # noqa: F401
            from tools.registry import registry

            names = frozenset(str(name) for name in registry.get_all_tool_names() if name)
            logger.warning(
                "RipDock Hermes tool progress registry loaded tool_count=%s",
                len(names),
            )
            return names
        except Exception as exc:
            logger.warning(
                "RipDock Hermes tool progress registry unavailable error=%s",
                exc,
            )
            return frozenset()

    def _finalize_hermes_tool_progress_names_from_agent(self, tools):
        if getattr(self, "_hermes_tool_progress_names_finalized", False):
            return
        names = set(getattr(self, "_hermes_tool_progress_names", frozenset()) or frozenset())
        agent_names = set()
        if isinstance(tools, list):
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                function = tool.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if name:
                    agent_names.add(str(name))
        if agent_names:
            names.update(agent_names)
            self._hermes_tool_progress_names = frozenset(names)
            self._hermes_tool_progress_names_finalized = True
            logger.warning(
                "RipDock Hermes tool progress registry finalized tool_count=%s",
                len(self._hermes_tool_progress_names),
            )

    def _hermes_tool_progress_mode(self):
        return "verbose"

    def _prepare_runtime_output(self, content):
        normalized = self._normalize_stream_content(content or "")
        filtered = self._apply_runtime_visibility_filter(normalized)
        self._log_runtime_output_truncation(normalized, filtered)
        return filtered

    async def _emit_and_strip_hermes_tool_progress(self, websocket, conversation_id, message_id, content):
        cleaned, activities = self._strip_hermes_tool_progress(content)
        for activity in activities:
            await self._emit_runtime_activity(websocket, conversation_id, message_id, activity)
        return cleaned

    def _strip_hermes_tool_progress(self, content):
        if not isinstance(content, str) or not content:
            return "", []
        tool_names = getattr(self, "_hermes_tool_progress_names", frozenset())
        if not tool_names:
            return content, []

        lines = content.splitlines()
        kept = []
        activities = []
        index = 0
        while index < len(lines):
            line = lines[index]
            parsed = self._parse_hermes_tool_progress_line(line, tool_names)
            if not parsed:
                terminal_block = self._parse_hermes_terminal_progress_block(lines, index, tool_names)
                if terminal_block:
                    activity, consumed = terminal_block
                    activities.append(activity)
                    index += consumed
                    continue
            if not parsed:
                kept.append(line)
                index += 1
                continue

            tool_name, args, preview = parsed
            raw_lines = [line]
            if index + 1 < len(lines):
                json_args, consumed = self._parse_hermes_tool_progress_json_args(lines[index + 1])
                if consumed:
                    args.update(json_args)
                    raw_lines.append(lines[index + 1])
                    index += 1

            if preview and "preview" not in args:
                args["preview"] = preview
            activities.append(self._hermes_tool_progress_activity(tool_name, args, "\n".join(raw_lines)))
            index += 1

        return "\n".join(kept).lstrip(), activities

    def _parse_hermes_tool_progress_line(self, line, tool_names):
        if not isinstance(line, str):
            return None
        stripped = line.strip()
        if not stripped:
            return None
        for tool_name in sorted(tool_names, key=len, reverse=True):
            match = re.match(
                rf"^(?P<prefix>.*?)\b{re.escape(tool_name)}(?P<suffix>\.\.\.|\([^)\n]*\)|:\s*\"(?P<preview>[^\"]*)\")\s*(?:\(×\d+\))?\s*$",
                stripped,
            )
            if not match:
                continue
            prefix = match.group("prefix").strip()
            if prefix and len(prefix.split()) > 1:
                continue
            args = self._parse_hermes_tool_progress_arg_keys(match.group("suffix"))
            preview = match.groupdict().get("preview")
            return tool_name, args, preview
        return None

    def _parse_hermes_terminal_progress_block(self, lines, index, tool_names):
        if "terminal" not in tool_names:
            return None
        if index + 2 >= len(lines):
            return None
        line = lines[index]
        stripped = line.strip() if isinstance(line, str) else ""
        match = re.match(r"^(?P<prefix>.*?)\bterminal\s*$", stripped)
        if not match:
            return None
        prefix = match.group("prefix").strip()
        if prefix and len(prefix.split()) > 1:
            return None
        if lines[index + 1].strip() != "```":
            return None
        end = index + 2
        while end < len(lines) and lines[end].strip() != "```":
            end += 1
        if end >= len(lines):
            return None
        command = "\n".join(lines[index + 2:end]).rstrip()
        raw_detail = "\n".join(lines[index:end + 1])
        activity = self._hermes_tool_progress_activity("terminal", {"command": command}, raw_detail)
        return activity, end - index + 1

    def _parse_hermes_tool_progress_arg_keys(self, suffix):
        if not isinstance(suffix, str) or not suffix.startswith("("):
            return {}
        try:
            parsed = ast.literal_eval(suffix[1:-1].strip())
        except Exception:
            return {}
        if not isinstance(parsed, list):
            return {}
        return {str(key): None for key in parsed if isinstance(key, str)}

    def _parse_hermes_tool_progress_json_args(self, line):
        if not isinstance(line, str):
            return {}, False
        stripped = line.strip()
        if not stripped.startswith("{"):
            return {}, False
        try:
            parsed = json.loads(stripped)
        except Exception:
            return {}, False
        if not isinstance(parsed, dict):
            return {}, False
        return parsed, True

    def _hermes_tool_progress_activity(self, tool_name, args, raw_detail):
        if not isinstance(args, dict):
            args = {}
        return {
            "tool_name": tool_name,
            "category": self._activity_category(tool_name),
            "summary": self._activity_summary(tool_name, args),
            "detail_id": self._store_raw_tool_detail(tool_name, args, raw_detail),
            "raw_detail": raw_detail,
            "args": args,
            "status": "running",
        }

    async def _emit_runtime_activity(self, websocket, conversation_id, message_id, activity):
        if self._should_suppress_activity(conversation_id, activity):
            return

        logger.warning(
            "RipDock activity emitted display_profile=ripdock_mobile conversation=%s tool=%s summary=%s detail_id=%s raw_inline=false",
            conversation_id,
            activity["tool_name"],
            activity["summary"],
            activity["detail_id"],
        )
        if websocket:
            activity_block = self._runtime_activity_block(activity)
            stream = self._stream_for(conversation_id, message_id, websocket=websocket)
            if activity_block and self._app_supports_semantic_blocks() and self._app_supports_content_type(activity_block["mime_type"]):
                sent = await stream.block(activity_block, source="runtime_activity")
                if sent:
                    self._record_runtime_activity(conversation_id, stream.message_id, activity)

    def _record_runtime_activity(self, conversation_id, message_id, activity):
        if not isinstance(message_id, str) or not message_id or not isinstance(activity, dict):
            return
        detail_id = activity.get("detail_id")
        if not isinstance(detail_id, str) or not detail_id:
            return
        if not hasattr(self, "_running_activities_by_message_id"):
            self._running_activities_by_message_id = {}
        running = self._running_activities_by_message_id.setdefault(message_id, {})
        status = activity.get("status") or "running"
        if status == "running":
            tracked_activity = dict(activity)
            tracked_activity["conversation_id"] = conversation_id
            tracked_activity["message_id"] = message_id
            running[detail_id] = tracked_activity
            return
        running.pop(detail_id, None)
        if not running:
            self._running_activities_by_message_id.pop(message_id, None)

    async def _complete_running_activities_for_message(self, websocket, conversation_id, message_id):
        if not isinstance(message_id, str) or not message_id:
            return
        if not hasattr(self, "_running_activities_by_message_id"):
            self._running_activities_by_message_id = {}
        running = self._running_activities_by_message_id.pop(message_id, {})
        if not running:
            return
        stream = self._stream_for(conversation_id, message_id, websocket=websocket)
        for activity in running.values():
            completed_activity = dict(activity)
            completed_activity["status"] = "completed"
            activity_block = self._runtime_activity_block(completed_activity)
            if activity_block and self._app_supports_semantic_blocks() and self._app_supports_content_type(activity_block["mime_type"]):
                await stream.block(activity_block, source="runtime_activity_complete")

    def _clear_running_activities_for_message(self, message_id):
        if not isinstance(message_id, str) or not message_id:
            return
        if not hasattr(self, "_running_activities_by_message_id"):
            self._running_activities_by_message_id = {}
        self._running_activities_by_message_id.pop(message_id, None)

    def _clear_running_activities_for_conversation(self, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id:
            return
        if not hasattr(self, "_running_activities_by_message_id"):
            self._running_activities_by_message_id = {}
        stale_message_ids = [
            message_id
            for message_id, activities in self._running_activities_by_message_id.items()
            if any(activity.get("conversation_id") == conversation_id for activity in activities.values())
        ]
        for message_id in stale_message_ids:
            self._running_activities_by_message_id.pop(message_id, None)

    def _runtime_activity_block(self, activity):
        title = self._activity_title(activity.get("tool_name"), activity.get("category"))
        summary = activity.get("summary") or self._activity_detail(activity)
        detail = self._activity_detail(activity)
        content = {
            "category": activity.get("category") or "runtime",
            "tool": activity.get("tool_name"),
            "status": activity.get("status") or "running",
            "summary": summary,
            "detail": detail,
            "detail_id": activity.get("detail_id"),
            "args": activity.get("args") or {},
        }
        return {
            "kind": self._activity_block_kind(activity),
            "mime_type": "application/vnd.ripdock.activity+json",
            "title": title,
            "content": json.dumps(content, ensure_ascii=False, indent=2),
            "language": "json",
            "copyable": True,
            "wrap": True,
            "collapsed": True,
        }

    def _activity_block_kind(self, activity):
        category = activity.get("category")
        tool_name = activity.get("tool_name")
        if category == "file":
            if tool_name in {"search_files", "find_artifacts"}:
                return "activity.file.search"
            return "activity.file.resolve"
        if category == "command":
            return "activity.code.run"
        if category == "planning":
            return "activity.plan"
        if category == "memory":
            return "activity.status"
        return "activity.tool.progress"

    def _should_suppress_activity(self, conversation_id, activity):
        now = time.monotonic()
        key = conversation_id or "unknown"
        state = self._activity_state_by_conversation.get(key) or {}
        if not isinstance(state.get("recent"), dict):
            state = {
                "last": state if isinstance(state, dict) else {},
                "recent": {},
            }
        recent = state["recent"]
        previous = state.get("last") or {}
        summary = activity.get("summary") or ""
        tool_name = activity.get("tool_name") or ""
        signature = json.dumps(
            {
                "category": activity.get("category") or "",
                "status": activity.get("status") or "",
                "summary": summary,
                "tool_name": tool_name,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        previous_at = recent.get(signature)
        if previous_at is not None:
            elapsed = now - previous_at
            if elapsed < 300:
                logger.warning(
                    "RipDock activity suppressed reason=duplicate conversation=%s tool=%s summary=%s elapsed=%.2f",
                    conversation_id,
                    tool_name,
                    summary,
                    elapsed,
                )
                return True
        if previous:
            elapsed = now - previous.get("at", 0)
            if summary == previous.get("summary") and elapsed < 30:
                logger.warning(
                    "RipDock activity suppressed reason=duplicate conversation=%s tool=%s summary=%s elapsed=%.2f",
                    conversation_id,
                    tool_name,
                    summary,
                    elapsed,
                )
                return True
            noisy = {"status", "retry", "wait", "process"}
            if tool_name in noisy and elapsed < 3:
                logger.warning(
                    "RipDock activity suppressed reason=throttle conversation=%s tool=%s summary=%s elapsed=%.2f",
                    conversation_id,
                    tool_name,
                    summary,
                    elapsed,
                )
                return True
        recent[signature] = now
        state["recent"] = {
            item_signature: item_at
            for item_signature, item_at in recent.items()
            if now - item_at < 600
        }
        state["last"] = {
            "summary": summary,
            "tool_name": tool_name,
            "at": now,
        }
        self._activity_state_by_conversation[key] = state
        return False

    def _store_raw_tool_detail(self, tool_name, args, raw_detail):
        detail_id = str(uuid.uuid4())
        self._raw_tool_details[detail_id] = {
            "tool_name": tool_name,
            "args": args,
            "raw_detail": raw_detail,
            "created_at": time.time(),
        }
        return detail_id

    def _activity_summary(self, tool_name, args):
        if tool_name == "todo":
            return "Planning tasks"
        if tool_name in {"skill_view", "skills_list"}:
            return "Loading skill"
        if tool_name == "skill_manage":
            return "Updating skill"
        if tool_name == "session_search":
            return "Searching session history"
        if tool_name == "search_files":
            return "Searching files"
        if tool_name == "read_file":
            return "Reading files"
        if tool_name in {"terminal", "shell", "execute_code"}:
            return self._terminal_activity_summary(tool_name, args)
        if tool_name.startswith("browser") or tool_name in {"web_search", "web_extract"}:
            return self._browser_activity_summary(args)
        if tool_name in {"write_file", "patch", "file", "report"}:
            return "Updating files"
        if tool_name in {"image_generate", "vision_analyze"}:
            return "Working with images"
        return "Working"

    def _activity_title(self, tool_name, category):
        if tool_name == "todo":
            return "Planning"
        if tool_name in {"skill_view", "skills_list"}:
            return "Loading skill"
        if tool_name == "skill_manage":
            return "Updating skill"
        if tool_name in {"session_search", "search_files", "web_search", "web_extract"}:
            return "Searching"
        if tool_name in {"terminal", "shell", "process", "execute_code"}:
            return "Running"
        if tool_name in {"read_file"}:
            return "Reading"
        if tool_name in {"write_file", "patch", "file", "report"}:
            return "Updating"
        if tool_name and tool_name.startswith("browser"):
            return "Browsing"
        if tool_name in {"image_generate"}:
            return "Generating image"
        if tool_name in {"vision_analyze"}:
            return "Analyzing image"
        if category == "message_delivery":
            return "Delivering"
        if category == "background_job":
            return "Scheduling"
        return "Working"

    def _activity_detail(self, activity):
        tool_name = activity.get("tool_name")
        args = activity.get("args") or {}
        if tool_name == "session_search":
            return "Session history"
        if tool_name == "search_files":
            return "Files"
        if tool_name == "read_file":
            path = args.get("path")
            return str(path) if path else "Files"
        if tool_name in {"web_search", "web_extract"}:
            query = args.get("query") or args.get("url") or args.get("preview")
            return str(query) if query else "Web"
        if tool_name in {"skill_view", "skills_list", "skill_manage"}:
            name = args.get("name") or args.get("file_path") or args.get("preview")
            return str(name) if name else "Skill"
        if tool_name in {"terminal", "shell", "process", "execute_code"}:
            command = args.get("command") or args.get("code") or args.get("preview")
            if command:
                return self._short_activity_preview(command)
            return "Command"
        return activity.get("summary") or "Runtime activity"

    def _short_activity_preview(self, value, limit=96):
        text = str(value).strip().replace("\n", " ")
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def _activity_category(self, tool_name):
        if tool_name == "todo":
            return "planning"
        if tool_name in {"terminal", "shell", "process", "execute_code"}:
            return "command"
        if tool_name in {"read_file", "write_file", "patch", "search_files", "file", "report"}:
            return "file"
        if tool_name.startswith("browser"):
            return "browser"
        if tool_name in {"web_search", "web_extract", "session_search"}:
            return "search"
        if tool_name in {"image_generate", "vision_analyze", "text_to_speech"}:
            return "media"
        if tool_name in {"memory", "self_improvement"}:
            return "memory"
        if tool_name in {"skill_view", "skills_list", "skill_manage"}:
            return "skill"
        if tool_name in {"delegate_task", "mixture_of_agents"}:
            return "delegation"
        if tool_name in {"cronjob"}:
            return "background_job"
        if tool_name in {"send_message", "clarify"}:
            return "message_delivery"
        return "runtime"

    def _terminal_activity_summary(self, tool_name, args):
        command = str(args.get("command") or args.get("code") or "")
        lowered = command.lower()
        if tool_name == "execute_code":
            return "Running Python script"
        if "python" in lowered or "python3" in lowered:
            return "Running Python script"
        if "node" in lowered or "npm " in lowered or "pnpm " in lowered:
            return "Running JavaScript"
        if "git " in lowered:
            return "Running Git"
        return "Running shell command"

    def _browser_activity_summary(self, args):
        text = " ".join(str(value) for value in args.values())
        lowered = text.lower()
        if "amazon." in lowered or "amazon " in lowered:
            return "Browsing Amazon"
        if "github." in lowered or "github " in lowered:
            return "Browsing GitHub"
        if "google." in lowered or "google " in lowered:
            return "Browsing Google"
        return "Browsing web"

    def _apply_runtime_visibility_filter(self, content):
        if not isinstance(content, str) or not content:
            return ""
        filtered = content
        filtered = self._strip_activity_markers(filtered)
        filtered = self._strip_home_channel_notice_prefix(filtered)
        filtered = self._strip_internal_system_note_message(filtered)
        return filtered

    def _strip_home_channel_notice_prefix(self, content):
        if not isinstance(content, str) or not content:
            return ""
        pattern = re.compile(
            r"^\s*📬?\s*No\s+home\s+channel\s+is\s+set\s+for\s+.+?\.\s+"
            r"A\s+home\s+channel\s+is\s+where\s+Hermes\s+delivers\s+"
            r"cron\s+job\s+results\s+and\s+cross-platform\s+messages\.\s+"
            r"Type\s+/sethome\s+to\s+make\s+this\s+chat\s+your\s+home\s+channel,\s+"
            r"or\s+ignore\s+to\s+skip\.?",
            re.IGNORECASE | re.DOTALL,
        )
        stripped = pattern.sub("", content, count=1)
        return stripped.lstrip()

    def _strip_internal_system_note_message(self, content):
        if not isinstance(content, str) or not content:
            return ""
        if re.match(r"^\s*\[system note:[\s\S]*?\]\s*$", content, re.IGNORECASE):
            return ""
        return content

    def _log_runtime_output_truncation(self, original, emitted):
        original_text = original if isinstance(original, str) else ""
        emitted_text = emitted if isinstance(emitted, str) else ""
        original_length = len(original_text)
        emitted_length = len(emitted_text)
        truncation_applied = emitted_length < original_length
        if truncation_applied:
            reason = "runtime_visibility_filter"
        else:
            reason = "none"
        logger.warning(
            "RipDock runtime output truncation_applied=%s truncation_reason=%s original_length=%s emitted_length=%s",
            str(truncation_applied).lower(),
            reason,
            original_length,
            emitted_length,
        )

    def _strip_activity_markers(self, content):
        return "\n".join(
            line
            for line in content.splitlines()
            if not line.strip().lower().startswith(("activity:", "runtime activity:", "working:"))
        )

    def _websocket_for_ripdock_send(self, metadata=None, conversation_id=None, message_id=None):
        websocket = self._request_websocket_for_metadata(
            metadata=metadata,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        if websocket:
            return websocket
        logger.warning(
            "RipDock dropped Runtime send reason=missing_routed_websocket conversation=%s message=%s",
            conversation_id,
            message_id,
        )
        return None

    def _current_ripdock_websocket(self):
        websocket = getattr(self, "_last_ripdock_websocket", None)
        if websocket and not getattr(websocket, "closed", False):
            return websocket
        websocket = self._latest_open_embedded_app_websocket()
        return websocket or getattr(self, "ws", None)

    async def get_chat_info(self, channel_id: str):
        return {
            "id": channel_id,
            "title": "RipDock",
            "type": "private",
        }


def register(ctx):
    ctx.register_platform(
        name="ripdock",
        label="RipDock",
        adapter_factory=lambda cfg: RipDockAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        apply_yaml_config_fn=_apply_yaml_config,
        install_hint="Install Python dependencies with: python -m pip install -r requirements-dev.txt",
    )
