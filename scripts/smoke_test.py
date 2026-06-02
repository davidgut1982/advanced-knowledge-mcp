#!/usr/bin/env python3
"""Post-deployment smoke test for the Lore MCP server.

Why: curl /health is not enough to declare a deployment done. This script
exercises the critical KB write/read/delete path against a live instance
so regressions are caught before they reach users.

What: Runs five steps in sequence — health check, kb_add, kb_search,
kb_get, kb_get_batch, kb_delete — and prints a PASS/FAIL line for each.

Test: Run against a live instance:
    python scripts/smoke_test.py --url http://localhost:5555
All steps should print PASS and the script should exit 0.

Usage:
    python scripts/smoke_test.py [--url URL] [--token TOKEN]

Arguments:
    --url    Base URL of the Lore instance (default: http://localhost:5555)
    --token  Optional Bearer token for Authorization header
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# ANSI colour helpers — no external deps
# ---------------------------------------------------------------------------

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"
BOLD = "\033[1m"


def _pass(label: str, detail: str = "") -> None:
    """Why: uniform PASS output with optional detail.
    What: Prints a green PASS line to stdout.
    Test: Call and assert stdout contains 'PASS'.
    """
    suffix = f"  ({detail})" if detail else ""
    print(f"  {GREEN}{BOLD}PASS{RESET}  {label}{suffix}")


def _fail(label: str, reason: str) -> None:
    """Why: uniform FAIL output so failures are impossible to miss.
    What: Prints a red FAIL line to stdout.
    Test: Call and assert stdout contains 'FAIL'.
    """
    print(f"  {RED}{BOLD}FAIL{RESET}  {label}  {RED}{reason}{RESET}")


def _info(msg: str) -> None:
    print(f"  {YELLOW}...{RESET}  {msg}")


# ---------------------------------------------------------------------------
# Minimal MCP client (mirrors tests/e2e/client.py — no pytest dep)
# ---------------------------------------------------------------------------


class SmokeClientError(Exception):
    """Why: distinguish MCP-level errors from transport errors.
    What: Carries the JSON-RPC error code and message.
    Test: Raise with code=-1, message='oops'; assert str() contains 'oops'.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.data = data
        super().__init__(f"MCP error {code}: {message}")


class SmokeClient:
    """Thin synchronous MCP HTTP client used only by the smoke test.

    Why: Replicates the LoreClient pattern from tests/e2e/client.py so the
    smoke test uses the same wire protocol without importing pytest fixtures.
    What: POSTs JSON-RPC 2.0 to /mcp, unwraps SSE framing + MCP TextContent
    envelope + Lore business envelope, returns the inner data dict.
    Test: Instantiate with a real URL, call tool('kb_add', {...}), assert
    'kb_id' in result.
    """

    def __init__(self, url: str, token: str | None = None, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(timeout=timeout, headers=headers)
        self._id = 0

    # ------------------------------------------------------------------
    # Wire-level helpers
    # ------------------------------------------------------------------

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Why: single entry point for all JSON-RPC calls.
        What: POSTs the request, parses SSE-framed response, surfaces
        JSON-RPC errors as SmokeClientError.
        Test: Mock httpx.Client.post; assert payload shape and return value.
        """
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params or {},
        }
        resp = self._client.post(f"{self.url}/mcp", json=payload)
        resp.raise_for_status()

        # Server returns SSE-framed responses: "event: message\ndata: {...}\n\n"
        raw = resp.text
        body_str: str | None = None
        for line in raw.splitlines():
            if line.startswith("data: "):
                body_str = line[len("data: ") :]
                break
        if body_str is None:
            body_str = raw

        body: dict[str, Any] = json.loads(body_str)

        if "error" in body and body["error"] is not None:
            err = body["error"]
            raise SmokeClientError(
                code=err.get("code", -1),
                message=err.get("message", "unknown error"),
                data=err.get("data"),
            )

        return body

    def _unwrap(self, rpc_response: dict[str, Any]) -> dict[str, Any]:
        """Why: FastMCP wraps every tool result in a TextContent envelope.
        What: Extracts and JSON-parses result.content[0].text.
        Test: Pass a hand-crafted envelope; assert the inner dict is returned.
        """
        result = rpc_response.get("result", {})
        content = result.get("content")
        if content and isinstance(content, list):
            first = content[0]
            if isinstance(first, dict) and first.get("type") == "text":
                raw_text = first.get("text", "")
                try:
                    return json.loads(raw_text)  # type: ignore[return-value]
                except json.JSONDecodeError:
                    return {"text": raw_text}
        return result  # type: ignore[return-value]

    def _unwrap_tool_result(self, inner: dict[str, Any]) -> dict[str, Any]:
        """Why: Lore adds its own ok/data/error envelope inside the TextContent.
        What: Extracts data on ok=True, raises SmokeClientError on ok=False.
        Test: Pass {ok: False, error: 'boom'}; assert SmokeClientError raised.
        """
        if not isinstance(inner, dict):
            return inner  # type: ignore[return-value]
        if inner.get("ok") is False:
            raise SmokeClientError(
                code=-1,
                message=f"Tool failed: {inner.get('error')} — {inner.get('message')}",
            )
        if "data" in inner and isinstance(inner["data"], dict):
            return inner["data"]
        return inner

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Why: top-level tool call used by every smoke step.
        What: Calls _call → _unwrap → _unwrap_tool_result and returns the
        inner data dict.
        Test: Call tool('kb_add', {...}) against a live server; assert
        'kb_id' in result.
        """
        rpc = self._call("tools/call", {"name": name, "arguments": arguments or {}})
        unwrapped = self._unwrap(rpc)
        return self._unwrap_tool_result(unwrapped)

    def close(self) -> None:
        """Why: release the underlying httpx connection pool cleanly.
        What: Closes the httpx.Client.
        Test: Call close(), then assert any subsequent call raises.
        """
        self._client.close()

    def __enter__(self) -> SmokeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Smoke steps
# ---------------------------------------------------------------------------


def step_health(base_url: str, client: httpx.Client) -> bool:
    """Why: confirms the server process is up and responding before touching the DB.
    What: GETs /health and asserts HTTP 200.
    Test: Point at a live server; assert returns True.
    """
    label = "GET /health → 200"
    try:
        resp = client.get(f"{base_url}/health")
        if resp.status_code == 200:
            _pass(label, f"HTTP {resp.status_code}")
            return True
        else:
            _fail(label, f"HTTP {resp.status_code}: {resp.text[:120]}")
            return False
    except Exception as exc:
        _fail(label, str(exc))
        return False


def step_kb_add(client: SmokeClient, topic: str, timestamp: str) -> tuple[bool, str]:
    """Why: verifies the write path — DB connection, schema, and insert logic.
    What: Calls kb_add with a unique title and returns (ok, kb_id).
    Test: Call against live server; assert ok=True and kb_id is non-empty.
    """
    label = "kb_add smoke entry"
    title = f"smoke-test-{timestamp}"
    try:
        result = client.tool(
            "kb_add",
            {
                "topic": topic,
                "title": title,
                "content": "smoke test entry",
            },
        )
        kb_id = result.get("kb_id", "")
        if not kb_id:
            _fail(label, f"No kb_id in response: {result}")
            return False, ""
        _pass(label, f"id={kb_id}")
        return True, kb_id
    except Exception as exc:
        _fail(label, str(exc))
        return False, ""


def step_kb_search(client: SmokeClient, topic: str, kb_id: str) -> bool:
    """Why: verifies the read/search path and that the just-added entry is indexed.
    What: Calls kb_search and asserts the expected kb_id appears in results.
    Test: Call after kb_add; assert kb_id found in results.
    """
    label = "kb_search finds entry"
    try:
        result = client.tool(
            "kb_search",
            {
                "query": "smoke test entry",
                "topic": topic,
            },
        )
        results: list[dict[str, Any]] = result.get("results") or result.get("entries") or []
        found_ids = [r.get("kb_id") for r in results]
        if kb_id in found_ids:
            _pass(label, f"{len(results)} result(s), id={kb_id} present")
            return True
        else:
            _fail(label, f"id={kb_id} not found in results: {found_ids}")
            return False
    except Exception as exc:
        _fail(label, str(exc))
        return False


def step_kb_get(client: SmokeClient, kb_id: str) -> bool:
    """Why: verifies direct lookup by ID and that content round-trips correctly.
    What: Calls kb_get and asserts the content field contains 'smoke test entry'.
    Test: Call after kb_add; assert content match.
    """
    label = "kb_get returns correct content"
    try:
        result = client.tool("kb_get", {"kb_id": kb_id})
        content = result.get("content") or ""
        if "smoke test entry" in content:
            _pass(label, f"content matches")
            return True
        else:
            _fail(label, f"'smoke test entry' not found in content: {content!r}")
            return False
    except Exception as exc:
        _fail(label, str(exc))
        return False


def step_kb_get_batch(client: SmokeClient, kb_id: str) -> bool:
    """Why: verifies the batch-get path is wired correctly end-to-end.
    What: Calls kb_get_batch with [kb_id] and asserts exactly 1 result.
    Test: Call after kb_add; assert len(results)==1 and id matches.
    """
    label = "kb_get_batch returns 1 result"
    try:
        result = client.tool("kb_get_batch", {"kb_ids": [kb_id]})
        entries: list[dict[str, Any]] = (
            result.get("entries")
            or result.get("results")
            or result.get("items")
            or []
        )
        if len(entries) == 1 and entries[0].get("kb_id") == kb_id:
            _pass(label, f"1 entry, id={kb_id}")
            return True
        else:
            _fail(label, f"Expected 1 entry with id={kb_id}, got: {entries}")
            return False
    except Exception as exc:
        _fail(label, str(exc))
        return False


def step_kb_delete(client: SmokeClient, kb_id: str) -> None:
    """Why: cleanup — leaves the target KB in the same state it was in before the test.
    What: Calls kb_delete best-effort; logs outcome but never fails the suite.
    Test: Call after kb_add; assert no exception propagates to caller.
    """
    label = "kb_delete cleanup"
    try:
        client.tool("kb_delete", {"kb_id": kb_id, "confirm": True})
        _pass(label, "entry removed")
    except Exception as exc:
        # Best-effort: deletion failure must not cause overall FAIL
        _info(f"cleanup skipped ({exc}) — entry {kb_id} may remain in smoke-test topic")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run_smoke(base_url: str, token: str | None) -> int:
    """Why: orchestrates all steps, tracks overall pass/fail, returns exit code.
    What: Runs health → kb_add → kb_search → kb_get → kb_get_batch → kb_delete
    and exits 0 on full pass, 1 on any failure.
    Test: Call with a live URL; assert return value is 0.
    """
    timestamp = str(int(time.time()))
    topic = f"smoke-test"

    print(f"\n{BOLD}Lore MCP smoke test{RESET}  target={base_url}")
    print("-" * 60)

    failures: list[str] = []

    # Step 1: health
    http_client = httpx.Client(timeout=10.0)
    if token:
        http_client.headers["Authorization"] = f"Bearer {token}"
    try:
        if not step_health(base_url, http_client):
            failures.append("health")
    finally:
        http_client.close()

    # Steps 2-6 use the MCP client
    with SmokeClient(base_url, token=token) as mc:
        ok_add, kb_id = step_kb_add(mc, topic, timestamp)
        if not ok_add:
            failures.append("kb_add")
            # Cannot continue without an ID
            _info("skipping remaining steps (kb_add failed)")
        else:
            # Brief pause to allow FTS indexing on slower instances
            time.sleep(0.5)

            if not step_kb_search(mc, topic, kb_id):
                failures.append("kb_search")

            if not step_kb_get(mc, kb_id):
                failures.append("kb_get")

            if not step_kb_get_batch(mc, kb_id):
                failures.append("kb_get_batch")

            # Best-effort cleanup regardless of earlier failures
            step_kb_delete(mc, kb_id)

    print("-" * 60)
    if not failures:
        print(f"{GREEN}{BOLD}SMOKE TEST PASSED{RESET}  (all steps OK)\n")
        return 0
    else:
        failed_str = ", ".join(failures)
        print(f"{RED}{BOLD}SMOKE TEST FAILED{RESET}  failing steps: {failed_str}\n")
        return 1


def main() -> None:
    """Why: CLI entry point with --url and --token args.
    What: Parses args and delegates to run_smoke(), exits with its return code.
    Test: Run 'python scripts/smoke_test.py --url http://localhost:5555'.
    """
    parser = argparse.ArgumentParser(
        description="Post-deployment smoke test for the Lore MCP server."
    )
    parser.add_argument(
        "--url",
        default="http://localhost:5555",
        help="Base URL of the Lore instance (default: http://localhost:5555)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Optional Bearer token for Authorization header",
    )
    args = parser.parse_args()
    sys.exit(run_smoke(args.url, args.token))


if __name__ == "__main__":
    main()
