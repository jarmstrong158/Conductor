"""Conformance tests for MCP protocol revision 2026-07-28.

Driven through a real in-process client so the dispatch path that applies cache
hints and result metadata is actually exercised; reading the handler functions
directly would bypass it and these assertions would check nothing.
"""

from __future__ import annotations

import anyio

import server
from mcp.client.client import Client

PROTOCOL_VERSION = "2026-07-28"


def _run(body):
    async def main():
        async with Client(server.server) as client:
            value = body(client)
            return await value if hasattr(value, "__await__") else value
    return anyio.run(main)


def _wire(body):
    return _run(body).model_dump(by_alias=True, exclude_none=True)


def test_negotiates_2026_07_28():
    """This server hand-rolled a JSON-RPC loop advertising 2024-11-05 before the
    migration; there is no handshake to answer any more."""
    assert _run(lambda c: c.protocol_version) == PROTOCOL_VERSION


def test_tools_list_carries_cache_hints():
    """SEP-2549, asserted on the serialized wire form so a regression in the
    camelCase aliases is caught rather than passing on the snake_case fields."""
    wire = _wire(lambda c: c.list_tools())
    assert wire["ttlMs"] == 300_000
    assert wire["cacheScope"] == "public"


def test_results_carry_result_type():
    assert _wire(lambda c: c.list_tools())["resultType"] == "complete"


def test_server_identifies_itself_in_result_meta():
    info = _wire(lambda c: c.list_tools())["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert info["name"] == "conductor-mcp"
    assert info["version"] == server.VERSION


def test_server_discover_advertises_supported_versions():
    wire = _wire(lambda c: c.session.discover())
    assert PROTOCOL_VERSION in wire["supportedVersions"]
    assert wire["ttlMs"] == 300_000


def test_tool_order_is_deterministic_and_matches_TOOLS():
    """Deterministic ordering keeps client-side and LLM prompt caches hitting."""
    first = [t["name"] for t in _wire(lambda c: c.list_tools())["tools"]]
    second = [t["name"] for t in _wire(lambda c: c.list_tools())["tools"]]
    assert first == [t["name"] for t in server.TOOLS]
    assert first == second


def test_schemas_pass_through_verbatim():
    """TOOLS is handed to the SDK as-is rather than re-derived from signatures.
    Guard that, so the advertised schemas cannot drift under the migration."""
    served = {t["name"]: t for t in _wire(lambda c: c.list_tools())["tools"]}
    declared = {t["name"]: t for t in server.TOOLS}
    assert set(served) == set(declared)
    for name, decl in declared.items():
        assert served[name]["inputSchema"] == decl["inputSchema"], f"schema drift in {name}"


def test_unknown_tool_result_shape_is_unchanged():
    """Pre-migration this server never set isError, returning even an unknown
    tool as a successful result carrying an {"error": ...} body. That is
    preserved deliberately rather than changed as a migration side effect."""
    wire = _wire(lambda c: c.call_tool("does_not_exist", {}))
    assert not wire.get("isError")
    assert "Unknown tool" in wire["content"][0]["text"]
