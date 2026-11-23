# MCP protocol conformance (2026-07-28) and statelessness

**Protocol version implemented: `2026-07-28`** (served by FastMCP 4.2.5 on the official `mcp` SDK 2.3.0).
`server/discover` advertises `supportedVersions: ["2026-07-28"]`. Older handshake clients (`initialize`, e.g. `2025-11-25`) are still accepted as a
compatibility path; they are not the primary protocol and are tested separately.

## What is verified (tests: `tests/protocol/test_mcp_2026_07_28.py`, raw HTTP, no MCP library involved)
| Requirement | Evidence |
|---|---|
| `server/discover` answers without any prior `initialize` and without a session id | `test_server_discover_without_initialize_or_session` (result carries `supportedVersions`, `serverInfo`, `capabilities`, `instructions`; no `Mcp-Session-Id` header) |
| `tools/list` and `tools/call` are self-contained requests | `test_tools_list_and_call_are_self_contained_requests` |
| Request-level metadata is **required** (`_meta` protocolVersion, clientInfo, clientCapabilities) | `test_request_level_metadata_is_required` → HTTP 400, JSON-RPC `-32602` |
| Unsupported version → `-32022` with the supported list | `test_unsupported_version_is_rejected_with_supported_list` |
| Auth is enforced on the modern wire | `test_auth_still_enforced_on_modern_wire` (401) |
| A real client negotiates 2026-07-28 | `test_fastmcp_client_negotiates_modern_protocol` (`protocol_version == "2026-07-28"`) |
| Legacy-handshake client still works statelessly | `test_legacy_handshake_clients_still_work_statelessly` |
| Required HTTP headers sent by clients: `Mcp-Protocol-Version`, `Mcp-Method`, `Mcp-Name` (tool calls) | raw tests set them explicitly |

The server runs with `stateless_http=True`: no in-memory session table, no `Mcp-Session-Id` is ever issued (0 occurrences across all tests and the demo below).

## Two-process, no-sticky-session demonstration (`python -m eval.two_instance`; also run as a pytest test)
Two independent server processes (own PIDs/ports/Neo4j pools) behind a reverse proxy that alternates **every HTTP request** between them.
Result (`eval/results/two_instance_stateless.json`, 2026-11-23, local machine):
* Raw 2026-07-28 wire, 12 requests alternating A/B (discover, list, call, …): **12/12 OK, 0 `Mcp-Session-Id` headers, identical call results**.
* FastMCP client (negotiated `2026-07-28`) through the proxy: **30/30 calls OK**.
* Legacy-handshake SDK client through the proxy: 10/10 calls OK (initialize served by one process, calls by both).
* Per-process `tool_call` log lines: **A = 22, B = 22**: both processes served requests.
* Instance A killed mid-run: the same long-lived client continued, **10/10 OK, all served by B**, no re-handshake.
* Proxy saw **0** responses carrying a session id.

## Honest limits
* This demonstrates request-level statelessness on one host; it is not a multi-region/HA claim.
* Rate limiting and the concurrency cap are per process (in memory), so two instances double the effective limit.
* Advertised capabilities are trimmed to what is implemented: `server/discover` reports only `tools` (`listChanged: false`); no prompts, resources, logging, completions, extensions or `subscriptions/listen` (`test_advertised_capabilities_match_supported_functionality`). `ttlMs` is 0 (no discover caching).
* The MCP Inspector was not run (see RELEASE_EVIDENCE).
