#!/usr/bin/env python3
"""Audit an MCP server against the Universe contract, over the wire.

Usage:
    python scripts/universe_probe.py list --url URL [--key KEY]
    python scripts/universe_probe.py call TOOL --url URL [--key KEY] [--args JSON]

`--key` falls back to the MCP_API_KEY environment variable. Stdlib only, so it
runs anywhere the server can be reached from.

Checks it performs:
  list  - protocolVersion echo, tool title + annotations, argument descriptions,
          and whether an outputSchema is present
  call  - isError flag, character count and content of every item, URL leakage
          in failure text
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

MAX_FAIL_CHARS = 80


def post(url: str, payload: dict, key: str, session: str | None = None, timeout: float = 300.0):
    """POST one JSON-RPC message and return (result, session_id)."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if key:
        req.add_header("X-API-Key", key)
        req.add_header("Authorization", f"Bearer {key}")
    if session:
        req.add_header("Mcp-Session-Id", session)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            sid = resp.headers.get("mcp-session-id", session)
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code} from {url}: {exc.read().decode(errors='replace')[:400]}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {url}: {exc.reason}")

    data_lines = [ln[6:] for ln in raw.splitlines() if ln.startswith("data: ")]
    body = json.loads(data_lines[0] if data_lines else raw)
    if "error" in body:
        sys.exit(f"JSON-RPC error: {json.dumps(body['error'])[:400]}")
    return body.get("result", {}), sid


def initialize(url: str, key: str):
    result, sid = post(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "universe-probe", "version": "1.0", "player_id": "probe-player"},
        },
    }, key)
    version = result.get("protocolVersion")
    print(f"protocolVersion echoed: {version}")
    if version != "2024-11-05":
        print("  WARNING: Universe speaks 2024-11-05; this server negotiated something else.")
    return sid


def cmd_list(args) -> int:
    sid = initialize(args.url, args.key)
    result, _ = post(args.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, args.key, sid)
    problems = 0
    for tool in result.get("tools", []):
        name = tool.get("name")
        title = tool.get("title") or (tool.get("annotations") or {}).get("title")
        params = tool.get("inputSchema", {}).get("properties", {})
        missing_desc = [p for p, spec in params.items() if not spec.get("description")]
        has_schema = "outputSchema" in tool

        print(f"\n{name}")
        print(f"  title:            {title!r}")
        print(f"  annotations:      {json.dumps(tool.get('annotations') or {})}")
        print(f"  description:      {(tool.get('description') or '')[:100]!r}")
        print(f"  args:             {', '.join(params) or '(none)'}")
        print(f"  args w/o desc:    {', '.join(missing_desc) or 'none'}")
        print(f"  outputSchema:     {'present (Universe ignores it)' if has_schema else 'absent'}")

        if not title:
            print("  PROBLEM: no title — the steps line falls back to the derived name")
            problems += 1
        if missing_desc:
            print("  PROBLEM: some arguments have no description for the model")
            problems += 1
    return 1 if problems else 0


def cmd_call(args) -> int:
    sid = initialize(args.url, args.key)
    try:
        arguments = json.loads(args.args)
    except json.JSONDecodeError as exc:
        sys.exit(f"--args is not valid JSON: {exc}")

    started = time.monotonic()
    result, _ = post(args.url, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": args.tool, "arguments": arguments},
    }, args.key, sid)
    elapsed = time.monotonic() - started

    is_error = bool(result.get("isError"))
    content = result.get("content", [])
    print(f"tool: {args.tool}  wall_time: {elapsed:.1f}s  isError: {is_error}  items: {len(content)}")

    problems = 0
    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            print(f"  text ({len(text)} chars): {text!r}")
            if is_error:
                if len(text) > MAX_FAIL_CHARS:
                    print(f"  PROBLEM: failure text is longer than {MAX_FAIL_CHARS} characters")
                    problems += 1
                if "http://" in text or "https://" in text or "localhost" in text:
                    print("  PROBLEM: failure text leaks a URL")
                    problems += 1
                if "Error executing tool" in text:
                    print("  PROBLEM: raw SDK exception wrapper leaked into the result")
                    problems += 1
        elif item.get("type") == "resource":
            res = item.get("resource", {})
            blob = res.get("blob") or ""
            print(f"  resource: mimeType={res.get('mimeType')} uri={res.get('uri')} "
                  f"~{len(blob) * 3 // 4} bytes")
            if not res.get("mimeType"):
                print("  PROBLEM: resource has no mimeType — Universe will not post it")
                problems += 1
        else:
            print(f"  {item.get('type')}: (not inspected)")

    if not is_error:
        texts = [i for i in content if i.get("type") == "text"]
        if not texts:
            print("  PROBLEM: no text item — the model gets nothing to say")
            problems += 1
        elif len(texts[0].get("text", "")) > 300:
            print("  WARNING: success text is long; the bot owner pays per word")
    return 1 if problems else 0


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", required=True, help="streamable HTTP endpoint, e.g. https://host/mcp")
    common.add_argument("--key", default=os.getenv("MCP_API_KEY", ""),
                        help="API key (default: $MCP_API_KEY)")

    parser = argparse.ArgumentParser(description="Audit an MCP server against the Universe contract.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", parents=[common], help="initialize + tools/list audit")

    call = sub.add_parser("call", parents=[common], help="tools/call audit")
    call.add_argument("tool", help="tool name")
    call.add_argument("--args", default="{}", help="arguments as JSON")

    args = parser.parse_args()
    return cmd_list(args) if args.command == "list" else cmd_call(args)


if __name__ == "__main__":
    sys.exit(main())
