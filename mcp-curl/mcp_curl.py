#!/usr/bin/env python3
"""
mcp_curl.py — introspect an MCP server and print curl commands for every
request it supports.

Connects to a Model Context Protocol server, performs the initialize
handshake, lists its tools / resources / resource templates / prompts, and
prints a shell-ready cheat sheet: one curl command per request, with example
arguments generated from each tool's JSON schema.

Transports
  http    Streamable HTTP (MCP spec 2025-03-26+). Output is curl commands.
  sse     Legacy HTTP+SSE (spec 2024-11-05): GET /sse opens an event stream
          that names a POST endpoint; POSTs return 202 and responses arrive
          on the stream. Output is a background-curl recipe with an
          `mcp_post` shell helper that POSTs and waits for the reply.
  stdio   Local server launched as a subprocess. curl can't talk to a pipe,
          so output is `printf ... | <command>` examples instead.

  The HTTP transport is auto-detected: a URL ending in /sse, or a POST that
  gets 404/405, falls back to legacy SSE. Force it with --transport.

Usage
  mcp_curl.py URL [options]                    # HTTP server (either flavour)
  mcp_curl.py --stdio -- CMD [ARGS...]         # stdio server

  -H, --header "Name: value"   extra header for discovery AND generated curls
                               (repeatable), e.g. -H "Authorization: Bearer x"
      --bearer TOKEN           shortcut for -H "Authorization: Bearer TOKEN"
      --transport auto|http|sse  force an HTTP transport (default auto)
      --all-args               include optional tool args in examples
                               (default: required args only)
      --json                   emit machine-readable JSON instead of shell text
      --timeout SECONDS        network timeout (default 30)
      --protocol-version VER   MCP protocol version to negotiate
                               (default 2025-06-18)
  -v, --verbose                log raw JSON-RPC traffic to stderr

  Direct execution (skips the cheat sheet; works on every transport):
      --call TOOL [--args JSON]      run tools/call and print the result
      --rpc METHOD [--params JSON]   send any JSON-RPC request, print result

Examples
  mcp_curl.py http://localhost:3001/mcp
  mcp_curl.py http://localhost:3001/sse                  # legacy SSE server
  mcp_curl.py https://mcp.example.com/mcp --bearer "$TOKEN" --all-args
  mcp_curl.py http://localhost:3001/mcp --json > mcp.json
  mcp_curl.py --stdio -- npx -y @modelcontextprotocol/server-everything
  mcp_curl.py http://localhost:3001/sse --call echo --args '{"message":"hi"}'
  mcp_curl.py http://localhost:3001/mcp --rpc resources/read --params '{"uri":"demo://x"}'

Exit codes: 0 ok, 1 protocol/transport error, 2 bad usage.
No third-party dependencies (Python 3.9+).
"""

from __future__ import annotations

import argparse
import json
import queue
import shlex
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "mcp_curl", "version": "1.0.0"}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class McpError(Exception):
    """Transport or protocol failure that should abort the run."""


class NotStreamableHttp(McpError):
    """POST to the URL was rejected in a way that suggests legacy HTTP+SSE."""


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
class Transport:
    """Minimal JSON-RPC client interface used by the discovery code."""

    kind = "?"

    def request(self, method: str, params: Optional[dict] = None) -> Any:
        raise NotImplementedError

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _rpc_message(method: str, params: Optional[dict], msg_id: Optional[int]) -> dict:
    msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if msg_id is not None:
        msg["id"] = msg_id
    return msg


def _parse_sse(raw: str) -> Iterable[dict]:
    """Yield JSON payloads from a text/event-stream body."""
    data_lines: List[str] = []
    for line in raw.splitlines() + [""]:
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)


class HttpTransport(Transport):
    """MCP Streamable HTTP: JSON-RPC over POST, responses as JSON or SSE."""

    kind = "http"

    def __init__(self, url: str, headers: Dict[str, str], timeout: float,
                 protocol_version: str, verbose: bool):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.protocol_version = protocol_version
        self.verbose = verbose
        self.session_id: Optional[str] = None
        self._next_id = 1

    def _post(self, body: dict, expect_response: bool) -> Optional[dict]:
        data = json.dumps(body).encode()
        hdrs = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
            **self.headers,
        }
        if self.session_id:
            hdrs["Mcp-Session-Id"] = self.session_id
        if self.verbose:
            print(f"--> POST {self.url}\n    {json.dumps(body)}", file=sys.stderr)
        req = urllib.request.Request(self.url, data=data, headers=hdrs, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            if e.code in (404, 405):
                raise NotStreamableHttp(
                    f"HTTP {e.code} from POST {self.url} — not a Streamable HTTP endpoint "
                    "(legacy HTTP+SSE servers expose GET /sse instead; try that path, "
                    "or --transport sse)\n" + detail) from None
            hint = ""
            if e.code in (401, 403):
                hint = " — pass credentials with --bearer TOKEN or -H 'Authorization: ...'"
            raise McpError(f"HTTP {e.code} from {self.url}{hint}\n{detail}") from None
        except urllib.error.URLError as e:
            raise McpError(f"cannot reach {self.url}: {e.reason}") from None
        except OSError as e:  # socket timeout etc.
            raise McpError(f"POST {self.url}: {e}") from None

        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid and not self.session_id:
                self.session_id = sid
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            if not expect_response:
                resp.read()
                return None
            want_id = body.get("id")
            if ctype == "text/event-stream":
                # Read the stream until the response carrying our id shows up.
                buf = ""
                while True:
                    chunk = resp.readline()
                    if not chunk:
                        break
                    buf += chunk.decode(errors="replace")
                    if chunk.strip() == b"":
                        for msg in _parse_sse(buf):
                            if msg.get("id") == want_id:
                                return msg
                        buf = ""
                for msg in _parse_sse(buf):
                    if msg.get("id") == want_id:
                        return msg
                raise McpError(f"SSE stream ended without a response to id={want_id}")
            raw = resp.read().decode(errors="replace")
            if not raw.strip():
                raise McpError(f"empty response body for {body.get('method')}")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                raise McpError(f"non-JSON response ({ctype}): {raw[:300]}") from None
            if isinstance(parsed, list):  # batch
                for msg in parsed:
                    if msg.get("id") == want_id:
                        return msg
                raise McpError(f"batch response lacked id={want_id}")
            return parsed

    def request(self, method: str, params: Optional[dict] = None) -> Any:
        msg_id = self._next_id
        self._next_id += 1
        resp = self._post(_rpc_message(method, params, msg_id), expect_response=True)
        if self.verbose:
            print(f"<-- {json.dumps(resp)[:2000]}", file=sys.stderr)
        assert resp is not None
        if "error" in resp:
            err = resp["error"]
            raise McpError(f"{method} failed: {err.get('code')} {err.get('message')}")
        return resp.get("result")

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._post(_rpc_message(method, params, None), expect_response=False)

    def close(self) -> None:
        if not self.session_id:
            return
        # Best-effort session teardown; servers may not support DELETE.
        req = urllib.request.Request(
            self.url, method="DELETE",
            headers={"Mcp-Session-Id": self.session_id, **self.headers})
        try:
            urllib.request.urlopen(req, timeout=self.timeout).close()
        except Exception:
            pass


class LegacySseTransport(Transport):
    """MCP HTTP+SSE (2024-11-05): GET stream for responses, POST for requests."""

    kind = "sse"

    def __init__(self, url: str, headers: Dict[str, str], timeout: float, verbose: bool):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.verbose = verbose
        self._next_id = 1
        self._events: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._closed = False

        req = urllib.request.Request(
            url, headers={"Accept": "text/event-stream", **headers}, method="GET")
        try:
            # No socket timeout on the stream itself: it is meant to stay idle.
            self._stream = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as e:
            raise McpError(f"HTTP {e.code} from GET {url} — not an SSE endpoint") from None
        except urllib.error.URLError as e:
            raise McpError(f"cannot reach {url}: {e.reason}") from None
        except OSError as e:
            raise McpError(f"GET {url}: {e}") from None
        ctype = (self._stream.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "text/event-stream":
            self._stream.close()
            raise McpError(f"GET {url} returned {ctype or 'no content-type'}, "
                           "expected text/event-stream")

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        # First event must be `endpoint`, naming where to POST.
        deadline = timeout
        while True:
            try:
                event, data = self._events.get(timeout=deadline)
            except queue.Empty:
                self.close()
                raise McpError(f"no `endpoint` event from {url} within {timeout}s — "
                               "not a legacy HTTP+SSE server?") from None
            if event == "endpoint":
                self.endpoint = urllib.parse.urljoin(url, data.strip())
                break
        if self.verbose:
            print(f"<-- endpoint {self.endpoint}", file=sys.stderr)

    def _read_loop(self) -> None:
        event, data_lines = "message", []
        try:
            while not self._closed:
                raw = self._stream.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip("\r\n")
                if line == "":
                    if data_lines:
                        self._events.put((event, "\n".join(data_lines)))
                    event, data_lines = "message", []
                    continue
                if line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    event = value
                elif field == "data":
                    data_lines.append(value)
        except Exception:
            pass  # stream closed underneath us
        finally:
            self._events.put(("__eof__", ""))

    def _post(self, body: dict) -> None:
        data = json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json", **self.headers}
        if self.verbose:
            print(f"--> POST {self.endpoint}\n    {json.dumps(body)}", file=sys.stderr)
        req = urllib.request.Request(self.endpoint, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise McpError(f"HTTP {e.code} from POST {self.endpoint}\n{detail}") from None
        except urllib.error.URLError as e:
            raise McpError(f"cannot reach {self.endpoint}: {e.reason}") from None
        except OSError as e:
            raise McpError(f"POST {self.endpoint}: {e}") from None

    def request(self, method: str, params: Optional[dict] = None) -> Any:
        msg_id = self._next_id
        self._next_id += 1
        self._post(_rpc_message(method, params, msg_id))
        while True:
            try:
                event, data = self._events.get(timeout=self.timeout)
            except queue.Empty:
                raise McpError(f"no response to {method} on the SSE stream "
                               f"within {self.timeout}s") from None
            if event == "__eof__":
                raise McpError(f"SSE stream closed before answering {method}")
            if event != "message":
                continue
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            if self.verbose:
                print(f"<-- {data[:2000]}", file=sys.stderr)
            if msg.get("id") != msg_id:
                continue  # notification or server-initiated request
            if "error" in msg:
                err = msg["error"]
                raise McpError(f"{method} failed: {err.get('code')} {err.get('message')}")
            return msg.get("result")

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._post(_rpc_message(method, params, None))

    def close(self) -> None:
        self._closed = True
        # HTTPResponse.close() would block on the buffer lock the reader thread
        # holds while parked in readinto(); shut the socket down first so that
        # read returns, then close normally.
        try:
            self._stream.fp.raw._sock.shutdown(socket.SHUT_RDWR)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._reader.join(timeout=2)
        try:
            self._stream.close()
        except Exception:
            pass


class StdioTransport(Transport):
    """MCP stdio: newline-delimited JSON-RPC over a subprocess's stdin/stdout."""

    kind = "stdio"

    def __init__(self, command: List[str], verbose: bool):
        self.command = command
        self.verbose = verbose
        self._next_id = 1
        try:
            self.proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL if not verbose else None, text=True)
        except FileNotFoundError:
            raise McpError(f"command not found: {command[0]}") from None

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin
        if self.verbose:
            print(f"--> {json.dumps(msg)}", file=sys.stderr)
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            raise McpError("server process closed stdin (did it crash on startup?)") from None

    def request(self, method: str, params: Optional[dict] = None) -> Any:
        msg_id = self._next_id
        self._next_id += 1
        self._send(_rpc_message(method, params, msg_id))
        assert self.proc.stdout
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise McpError(f"server exited before answering {method} "
                               f"(exit code {self.proc.poll()})")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray log line on stdout
            if self.verbose:
                print(f"<-- {line[:2000]}", file=sys.stderr)
            if msg.get("id") != msg_id:
                continue  # notification or server-initiated request
            if "error" in msg:
                err = msg["error"]
                raise McpError(f"{method} failed: {err.get('code')} {err.get('message')}")
            return msg.get("result")

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send(_rpc_message(method, params, None))

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _paginate(t: Transport, method: str, key: str) -> List[dict]:
    items: List[dict] = []
    cursor: Optional[str] = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        result = t.request(method, params) or {}
        items.extend(result.get(key, []))
        cursor = result.get("nextCursor")
        if not cursor:
            return items


def discover(t: Transport, protocol_version: str) -> dict:
    init = t.request("initialize", {
        "protocolVersion": protocol_version,
        "capabilities": {},
        "clientInfo": CLIENT_INFO,
    }) or {}
    t.notify("notifications/initialized")

    caps = init.get("capabilities", {})
    info = {
        "server": init.get("serverInfo", {}),
        "protocolVersion": init.get("protocolVersion"),
        "capabilities": caps,
        "instructions": init.get("instructions"),
        "tools": [], "resources": [], "resourceTemplates": [], "prompts": [],
    }
    if "tools" in caps:
        info["tools"] = _paginate(t, "tools/list", "tools")
    if "resources" in caps:
        info["resources"] = _paginate(t, "resources/list", "resources")
        try:
            info["resourceTemplates"] = _paginate(
                t, "resources/templates/list", "resourceTemplates")
        except McpError:
            pass  # optional; many servers don't implement it
    if "prompts" in caps:
        info["prompts"] = _paginate(t, "prompts/list", "prompts")
    return info


# --------------------------------------------------------------------------- #
# Example-argument generation from JSON Schema
# --------------------------------------------------------------------------- #
def example_value(schema: dict, name: str = "value", depth: int = 0) -> Any:
    """Produce a plausible placeholder for a JSON-schema node."""
    if not isinstance(schema, dict) or depth > 6:
        return f"<{name}>"
    for k in ("const", "default"):
        if k in schema:
            return schema[k]
    if schema.get("examples"):
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]
    for combo in ("anyOf", "oneOf", "allOf"):
        if schema.get(combo):
            return example_value(schema[combo][0], name, depth + 1)

    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((x for x in typ if x != "null"), typ[0] if typ else None)
    if typ == "string":
        fmt = schema.get("format")
        return {"date-time": "2024-01-01T00:00:00Z", "date": "2024-01-01",
                "uri": "https://example.com", "email": "user@example.com",
                "uuid": "00000000-0000-0000-0000-000000000000"}.get(fmt, f"<{name}>")
    if typ == "integer":
        return schema.get("minimum", 0)
    if typ == "number":
        return schema.get("minimum", 0.0)
    if typ == "boolean":
        return False
    if typ == "null":
        return None
    if typ == "array":
        item = schema.get("items", {})
        return [example_value(item, f"{name}[0]", depth + 1)] if item else []
    if typ == "object" or "properties" in schema:
        return example_args(schema, include_optional=True, depth=depth + 1)
    return f"<{name}>"


def example_args(schema: dict, include_optional: bool, depth: int = 0) -> dict:
    props = (schema or {}).get("properties", {}) or {}
    required = set((schema or {}).get("required", []) or [])
    out = {}
    for name, sub in props.items():
        if include_optional or name in required:
            out[name] = example_value(sub, name, depth)
    return out


def describe_type(schema: dict) -> str:
    if not isinstance(schema, dict):
        return "any"
    if schema.get("enum"):
        return "enum[" + ", ".join(json.dumps(v) for v in schema["enum"]) + "]"
    typ = schema.get("type", "any")
    if isinstance(typ, list):
        typ = "|".join(typ)
    if typ == "array":
        return f"array<{describe_type(schema.get('items', {}))}>"
    return str(typ)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def sq(s: str) -> str:
    return shlex.quote(s)


def rpc_json(method: str, params: Optional[dict], msg_id: Optional[int] = 1) -> str:
    return json.dumps(_rpc_message(method, params, msg_id), ensure_ascii=False)


class CurlRenderer:
    """Render Streamable HTTP requests as curl commands."""

    def __init__(self, url: str, headers: Dict[str, str], protocol_version: str,
                 uses_session: bool):
        self.url = url
        self.headers = headers
        self.protocol_version = protocol_version
        self.uses_session = uses_session

    def _header_flags(self, with_session: bool) -> List[str]:
        flags = [
            '-H "Content-Type: application/json"',
            '-H "Accept: application/json, text/event-stream"',
            f'-H "MCP-Protocol-Version: {self.protocol_version}"',
        ]
        if with_session and self.uses_session:
            flags.append('-H "Mcp-Session-Id: $MCP_SESSION"')
        flags += [f"-H {sq(f'{k}: {v}')}" for k, v in self.headers.items()]
        return flags

    def command(self, method: str, params: Optional[dict], msg_id: Optional[int] = 1,
                with_session: bool = True) -> str:
        body = rpc_json(method, params, msg_id)
        lines = ['curl -sS "$MCP_URL" \\'] + [f"  {f} \\" for f in self._header_flags(with_session)]
        lines.append(f"  -d {sq(body)}")
        return "\n".join(lines)

    def preamble(self) -> str:
        init_params = {"protocolVersion": self.protocol_version, "capabilities": {},
                       "clientInfo": {"name": "curl", "version": "0"}}
        out = [f"export MCP_URL={sq(self.url)}", ""]
        if self.uses_session:
            out += [
                "# 1. initialize — the server issues a session id in the Mcp-Session-Id",
                "#    response header; every later request must send it back.",
                "export MCP_SESSION=$(curl -sS -D - -o /dev/null \"$MCP_URL\" \\",
            ]
            out += [f"  {f} \\" for f in self._header_flags(with_session=False)]
            out += [
                f"  -d {sq(rpc_json('initialize', init_params, 1))} \\",
                "  | awk 'tolower($1)==\"mcp-session-id:\"{print $2}' | tr -d '\\r')",
                'echo "session: $MCP_SESSION"',
            ]
        else:
            out += [
                "# 1. initialize (this server is stateless — no session id is issued,",
                "#    so each request below works on its own)",
                self.command("initialize", init_params, 1, with_session=False),
            ]
        out += [
            "",
            "# 2. initialized notification (no id → server replies 202 with no body)",
            self.command("notifications/initialized", None, None),
        ]
        return "\n".join(out)


class StdioRenderer:
    """Render stdio requests as a printf pipeline that includes the handshake."""

    def __init__(self, command: List[str], protocol_version: str):
        self.command = command
        self.protocol_version = protocol_version

    def command(self, method: str, params: Optional[dict], msg_id: Optional[int] = 2) -> str:
        if msg_id is None:
            msg_id = 2
        init_params = {"protocolVersion": self.protocol_version, "capabilities": {},
                       "clientInfo": {"name": "shell", "version": "0"}}
        msgs = [rpc_json("initialize", init_params, 1),
                rpc_json("notifications/initialized", None, None),
                rpc_json(method, params, msg_id)]
        cmd = " ".join(sq(c) for c in self.command)
        return "printf '%s\\n' \\\n" + " \\\n".join(f"  {sq(m)}" for m in msgs) + f" \\\n  | {cmd}"

    def preamble(self) -> str:
        return ("# stdio transport: curl can't talk to a subprocess, so each example\n"
                "# pipes the full handshake (initialize, initialized, request) into the\n"
                "# server and reads JSON-RPC responses from its stdout.")


class LegacySseRenderer:
    """Render legacy HTTP+SSE requests: background curl stream + POST helper."""

    def __init__(self, sse_url: str, endpoint: str, headers: Dict[str, str]):
        self.sse_url = sse_url
        self.endpoint = endpoint
        self.headers = headers

    def _extra_headers(self) -> str:
        return "".join(f" \\\n  -H {sq(f'{k}: {v}')}" for k, v in self.headers.items())

    def command(self, method: str, params: Optional[dict], msg_id: Optional[int] = 1) -> str:
        return f"mcp_post {sq(rpc_json(method, params, msg_id))}"

    def preamble(self) -> str:
        init_params = {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "curl", "version": "0"}}
        origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(self.sse_url))
        return "\n".join([
            "# Legacy HTTP+SSE: responses do not come back on the POST (it returns",
            "# 202 with an empty body) — they arrive on a long-lived GET stream. So:",
            "# keep one `curl -N` running in the background, writing the stream to a",
            "# log file, and read replies from there.",
            f"export MCP_SSE_URL={sq(self.sse_url)}",
            f"export MCP_ORIGIN={sq(origin)}",
            'export MCP_SSE_LOG=$(mktemp -t mcp-sse)',
            "",
            "# 1. open the event stream (stays open for the whole session)",
            'curl -sN "$MCP_SSE_URL" -H "Accept: text/event-stream"'
            + self._extra_headers() + ' > "$MCP_SSE_LOG" &',
            "MCP_SSE_PID=$!",
            "",
            "# 2. the first event is `endpoint`: the URL to POST to (carries the session id)",
            'until grep -q "^event: endpoint" "$MCP_SSE_LOG"; do sleep 0.2; done',
            "MCP_POST=$(awk '/^event: endpoint/{getline; sub(/^data: */,\"\"); print; exit}' "
            "\"$MCP_SSE_LOG\" | tr -d '\\r')",
            'case "$MCP_POST" in http*) ;; *) MCP_POST="$MCP_ORIGIN$MCP_POST";; esac',
            "export MCP_POST",
            'echo "POST endpoint: $MCP_POST"',
            f"#    (during discovery it was: {self.endpoint})",
            "",
            "# 3. helper: POST one JSON-RPC message, then print the reply from the stream",
            "#    (messages without an id are notifications — nothing to wait for)",
            "mcp_post() {",
            '  local before replies; before=$(grep -c "^data:" "$MCP_SSE_LOG")',
            '  replies=$(grep -c "^data:.*\\"id\\"" "$MCP_SSE_LOG")',
            '  curl -sS -o /dev/null -w "POST -> HTTP %{http_code}\\n" "$MCP_POST" \\',
            '    -H "Content-Type: application/json"' + self._extra_headers().replace("\n  ", "\n    ") + ' \\',
            '    -d "$1"',
            '  case "$1" in *\'"id"\'*) ;; *) return 0;; esac',
            "  for _ in $(seq 1 100); do",
            '    [ "$(grep -c "^data:.*\\"id\\"" "$MCP_SSE_LOG")" -gt "$replies" ] && break',
            "    sleep 0.1",
            "  done",
            '  grep "^data:" "$MCP_SSE_LOG" | tail -n +"$((before + 1))" | sed "s/^data: //"',
            "}",
            "",
            "# 4. handshake",
            self.command("initialize", init_params, 1),
            self.command("notifications/initialized", None, None),
            "",
            '# when finished:',
            '#   kill "$MCP_SSE_PID" 2>/dev/null; wait "$MCP_SSE_PID" 2>/dev/null; rm -f "$MCP_SSE_LOG"',
        ])


def render_text(info: dict, r, all_args: bool) -> str:
    render = r.command
    srv = info["server"]
    out: List[str] = []
    out.append("#" * 78)
    out.append(f"# MCP server: {srv.get('name', '?')} {srv.get('version', '')}".rstrip())
    out.append(f"# protocol:   {info.get('protocolVersion')}")
    caps = info["capabilities"]
    out.append("# capabilities: " + (", ".join(sorted(caps)) if caps else "(none)"))
    out.append(f"# tools: {len(info['tools'])}  resources: {len(info['resources'])}  "
               f"templates: {len(info['resourceTemplates'])}  prompts: {len(info['prompts'])}")
    if info.get("instructions"):
        out.append("#")
        for line in str(info["instructions"]).strip().splitlines():
            out.append(f"# {line}")
    out.append("#" * 78)
    out.append("")
    out.append("## Handshake")
    out.append(r.preamble())
    out.append("")

    out.append("## Listing")
    for method in ("tools/list", "resources/list", "resources/templates/list", "prompts/list"):
        out.append(f"# {method}")
        out.append(render(method, {}))
        out.append("")
    out.append("# ping")
    out.append(render("ping", None))
    out.append("")

    if info["tools"]:
        out.append(f"## Tools ({len(info['tools'])})")
        for tool in info["tools"]:
            schema = tool.get("inputSchema", {}) or {}
            props = schema.get("properties", {}) or {}
            required = set(schema.get("required", []) or [])
            out.append(f"### {tool['name']}")
            desc = (tool.get("description") or "").strip()
            for line in desc.splitlines():
                out.append(f"#   {line}")
            if props:
                out.append("#   args:")
                for pname, psub in props.items():
                    flag = "required" if pname in required else "optional"
                    pdesc = (psub.get("description") or "").strip().splitlines() if isinstance(psub, dict) else []
                    tail = f" — {pdesc[0]}" if pdesc else ""
                    out.append(f"#     {pname}: {describe_type(psub)} ({flag}){tail}")
            else:
                out.append("#   args: none")
            args = example_args(schema, include_optional=all_args)
            out.append(render("tools/call", {"name": tool["name"], "arguments": args}))
            out.append("")

    if info["resources"]:
        out.append(f"## Resources ({len(info['resources'])})")
        for res in info["resources"]:
            out.append(f"### {res.get('name') or res['uri']}")
            if res.get("description"):
                out.append(f"#   {res['description'].strip().splitlines()[0]}")
            if res.get("mimeType"):
                out.append(f"#   mimeType: {res['mimeType']}")
            out.append(render("resources/read", {"uri": res["uri"]}))
            out.append("")

    if info["resourceTemplates"]:
        out.append(f"## Resource templates ({len(info['resourceTemplates'])})")
        for tpl in info["resourceTemplates"]:
            out.append(f"### {tpl.get('name') or tpl['uriTemplate']}")
            if tpl.get("description"):
                out.append(f"#   {tpl['description'].strip().splitlines()[0]}")
            out.append(f"#   template: {tpl['uriTemplate']} — fill the {{placeholders}} before sending")
            out.append(render("resources/read", {"uri": tpl["uriTemplate"]}))
            out.append("")

    if info["prompts"]:
        out.append(f"## Prompts ({len(info['prompts'])})")
        for p in info["prompts"]:
            out.append(f"### {p['name']}")
            if p.get("description"):
                out.append(f"#   {p['description'].strip().splitlines()[0]}")
            pargs = p.get("arguments") or []
            if pargs:
                out.append("#   args:")
                for a in pargs:
                    flag = "required" if a.get("required") else "optional"
                    tail = f" — {a['description'].strip().splitlines()[0]}" if a.get("description") else ""
                    out.append(f"#     {a['name']}: string ({flag}){tail}")
            args = {a["name"]: f"<{a['name']}>" for a in pargs
                    if all_args or a.get("required")}
            out.append(render("prompts/get", {"name": p["name"], "arguments": args}))
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_json(info: dict, r, all_args: bool, transport_kind: str) -> str:
    render = r.command
    doc = {
        "transport": transport_kind,
        "server": info["server"],
        "protocolVersion": info.get("protocolVersion"),
        "capabilities": info["capabilities"],
        "instructions": info.get("instructions"),
        "handshake": r.preamble(),
        "requests": [],
    }
    for method in ("tools/list", "resources/list", "resources/templates/list", "prompts/list"):
        doc["requests"].append({"kind": "list", "method": method, "params": {},
                                "command": render(method, {})})
    for tool in info["tools"]:
        params = {"name": tool["name"],
                  "arguments": example_args(tool.get("inputSchema", {}) or {}, all_args)}
        doc["requests"].append({"kind": "tool", "name": tool["name"],
                                "description": tool.get("description"),
                                "inputSchema": tool.get("inputSchema"),
                                "method": "tools/call", "params": params,
                                "command": render("tools/call", params)})
    for res in info["resources"]:
        params = {"uri": res["uri"]}
        doc["requests"].append({"kind": "resource", "name": res.get("name"),
                                "description": res.get("description"), "mimeType": res.get("mimeType"),
                                "method": "resources/read", "params": params,
                                "command": render("resources/read", params)})
    for tpl in info["resourceTemplates"]:
        params = {"uri": tpl["uriTemplate"]}
        doc["requests"].append({"kind": "resourceTemplate", "name": tpl.get("name"),
                                "description": tpl.get("description"),
                                "uriTemplate": tpl["uriTemplate"],
                                "method": "resources/read", "params": params,
                                "command": render("resources/read", params)})
    for p in info["prompts"]:
        pargs = p.get("arguments") or []
        params = {"name": p["name"], "arguments": {a["name"]: f"<{a['name']}>" for a in pargs
                                                   if all_args or a.get("required")}}
        doc["requests"].append({"kind": "prompt", "name": p["name"],
                                "description": p.get("description"), "arguments": pargs,
                                "method": "prompts/get", "params": params,
                                "command": render("prompts/get", params)})
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_headers(items: List[str]) -> Dict[str, str]:
    headers = {}
    for item in items:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise SystemExit(f"error: bad header {item!r}; expected 'Name: value'")
        headers[name.strip()] = value.strip()
    return headers


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcp_curl.py",
        description="Introspect an MCP server and print curl commands for every request it supports.",
        epilog="stdio form: mcp_curl.py --stdio -- CMD [ARGS...]")
    p.add_argument("url", nargs="?", help="Streamable HTTP endpoint, e.g. http://localhost:3001/mcp")
    p.add_argument("--stdio", action="store_true", help="launch a stdio server; command follows '--'")
    p.add_argument("-H", "--header", action="append", default=[], metavar="'Name: value'",
                   help="extra header for discovery and generated curls (repeatable)")
    p.add_argument("--bearer", metavar="TOKEN", help="shortcut for -H 'Authorization: Bearer TOKEN'")
    p.add_argument("--transport", choices=["auto", "http", "sse"], default="auto",
                   help="HTTP transport: streamable 'http', legacy 'sse', or auto-detect")
    p.add_argument("--all-args", action="store_true", help="include optional args in examples")
    p.add_argument("--call", metavar="TOOL", help="execute tools/call for TOOL and print the result")
    p.add_argument("--args", metavar="JSON", help="arguments for --call (JSON object)")
    p.add_argument("--rpc", metavar="METHOD", help="execute an arbitrary JSON-RPC request")
    p.add_argument("--params", metavar="JSON", help="params for --rpc (JSON object)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of shell text")
    p.add_argument("--timeout", type=float, default=30.0, help="network timeout in seconds")
    p.add_argument("--protocol-version", default=DEFAULT_PROTOCOL_VERSION)
    p.add_argument("-v", "--verbose", action="store_true", help="log JSON-RPC traffic to stderr")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after a literal '--' is the stdio server command; argparse
    # never sees it, so options may appear anywhere before it.
    cmd: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    args = parser.parse_args(argv)

    headers = parse_headers(args.header)
    if args.bearer:
        headers["Authorization"] = f"Bearer {args.bearer}"

    def parse_json_obj(text: Optional[str], flag: str) -> dict:
        if not text:
            return {}
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            parser.error(f"{flag} is not valid JSON: {e}")
        if not isinstance(obj, dict):
            parser.error(f"{flag} must be a JSON object")
        return obj

    direct: Optional[Tuple[str, dict]] = None
    if args.call and args.rpc:
        parser.error("--call and --rpc are mutually exclusive")
    if args.call:
        direct = ("tools/call", {"name": args.call, "arguments": parse_json_obj(args.args, "--args")})
    elif args.rpc:
        direct = (args.rpc, parse_json_obj(args.params, "--params"))

    protocol_version = args.protocol_version
    try:
        transport: Transport
        if args.stdio:
            if args.url:
                cmd.insert(0, args.url)  # bare `--stdio cmd` without '--'
            if not cmd:
                parser.error("--stdio requires a command after '--'")
            transport = StdioTransport(cmd, args.verbose)
        else:
            if cmd:
                parser.error("a command after '--' only makes sense with --stdio")
            if not args.url:
                parser.error("give an HTTP URL, or use --stdio -- CMD [ARGS...]")
            if not args.url.startswith(("http://", "https://")):
                parser.error("URL must start with http:// or https://")
            transport = open_http_transport(args.url, headers, args.timeout,
                                            protocol_version, args.transport, args.verbose)
        if isinstance(transport, LegacySseTransport):
            protocol_version = "2024-11-05"  # the only version that transport exists in
    except McpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        if direct:
            transport.request("initialize", {"protocolVersion": protocol_version,
                                             "capabilities": {}, "clientInfo": CLIENT_INFO})
            transport.notify("notifications/initialized")
            method, params = direct
            result = transport.request(method, params)
            transport.close()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        info = discover(transport, protocol_version)
    except McpError as e:
        print(f"error: {e}", file=sys.stderr)
        transport.close()
        return 1
    except KeyboardInterrupt:
        transport.close()
        return 130

    if isinstance(transport, HttpTransport):
        renderer = CurlRenderer(args.url, headers, protocol_version,
                                uses_session=bool(transport.session_id))
    elif isinstance(transport, LegacySseTransport):
        renderer = LegacySseRenderer(args.url, transport.endpoint, headers)
    else:
        renderer = StdioRenderer(cmd, protocol_version)
    transport.close()

    text = render_json(info, renderer, args.all_args, transport.kind) if args.json \
        else render_text(info, renderer, args.all_args)
    sys.stdout.write(text)
    return 0


def open_http_transport(url: str, headers: Dict[str, str], timeout: float,
                        protocol_version: str, mode: str, verbose: bool) -> Transport:
    """Pick Streamable HTTP or legacy SSE, probing the server when mode is auto."""
    if mode == "sse":
        return LegacySseTransport(url, headers, timeout, verbose)
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    if mode == "auto" and path.endswith("/sse"):
        try:
            return LegacySseTransport(url, headers, timeout, verbose)
        except McpError as e:
            if verbose:
                print(f"legacy SSE probe failed ({e}); trying Streamable HTTP", file=sys.stderr)
    http = HttpTransport(url, headers, timeout, protocol_version, verbose)
    if mode == "http":
        return http
    # auto: a cheap probe — send `ping` and see whether POST is accepted at all.
    try:
        http._post(_rpc_message("ping", None, 0), expect_response=True)
        return http
    except NotStreamableHttp as e:
        if verbose:
            print(f"not Streamable HTTP ({e.args[0].splitlines()[0]}); trying legacy SSE",
                  file=sys.stderr)
        try:
            return LegacySseTransport(url, headers, timeout, verbose)
        except McpError as e2:
            raise McpError(f"{e}\n...and legacy SSE also failed: {e2}") from None
    except McpError:
        return http  # e.g. server rejects pre-init ping; let initialize report properly


if __name__ == "__main__":
    sys.exit(main())
