"""Hermes plugin entrypoint for the ripdock protocol implementation."""

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

try:
    from backend.adapter import RipDockAdapter, check_requirements, register, validate_config
except ModuleNotFoundError as exc:
    if exc.name not in {"gateway", "websockets", "fastapi", "uvicorn"}:
        raise
    RipDockAdapter = None
    check_requirements = None
    register = None
    validate_config = None

__all__ = [
    "RipDockAdapter",
    "check_requirements",
    "register",
    "validate_config",
]
