"""Pinned command/environment shared by deployment preparation and normal MCP use."""

import os
import sys
from pathlib import Path

from ..config import get_settings

AMAP_SERVER_VERSION = "0.1.9"
MCP_VERSION = "1.28.1"
PYDANTIC_VERSION = "2.13.4"


def amap_runtime(allow_install=False):
    settings = get_settings()
    command = [sys.executable, "-m", "uv", "tool", "run"]
    if not allow_install:
        command.append("--offline")
    command.extend([
        "--python", sys.executable,
        "--overrides", str(Path(__file__).resolve().parents[2] / "mcp-overrides.txt"),
        "--with", f"mcp=={MCP_VERSION}", "--with", f"pydantic=={PYDANTIC_VERSION}",
        "--from", f"amap-mcp-server=={AMAP_SERVER_VERSION}", "amap-mcp-server",
    ])
    forwarded = (
        "UV_CACHE_DIR", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_HTTP_TIMEOUT", "UV_HTTP_RETRIES",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    )
    env = {name: os.environ[name] for name in forwarded if os.environ.get(name)}
    if settings.mcp_package_index:
        env["UV_DEFAULT_INDEX"] = settings.mcp_package_index
    env["AMAP_MAPS_API_KEY"] = settings.amap_api_key
    return command, env
