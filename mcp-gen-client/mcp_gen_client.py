#!/usr/bin/env python3
"""
mcp_gen_client.py — generate a standalone Python client for an MCP server.

Connects to a Model Context Protocol server, discovers its tools, resources
and prompts, and writes a single dependency-free Python file that exposes
every tool as a typed function (plus a CLI), so anyone can call the server
without knowing MCP:

    import everything_mcp as mcp
    mcp.echo(message="hi")                       # -> tools/call result dict
    mcp.result_text(mcp.get_sum(a=1, b=2))       # -> "3"

    python everything_mcp.py list
    python everything_mcp.py call echo --message hi
    python everything_mcp.py call get-annotated-message --message-type error --text
    python everything_mcp.py read demo://resource/1
    python everything_mcp.py prompt simple_prompt

The generated file embeds this script's MCP runtime (Streamable HTTP, legacy
HTTP+SSE, and stdio transports) verbatim, so it has no imports beyond the
standard library and does not depend on this generator afterwards.

Usage
  mcp_gen_client.py URL [options]                # HTTP server (either flavour)
  mcp_gen_client.py --stdio -- CMD [ARGS...]     # stdio server

  -o, --output FILE            where to write (default: <server-name>_mcp.py)
      --module-name NAME       override the name used in docs/CLI examples
  -H, --header "Name: value"   header for discovery AND baked into the client
                               (repeatable). Credentials are NOT baked in:
                               Authorization / *token* / *key* / *secret*
                               headers become $MCP_<NAME> env-var lookups.
      --bearer TOKEN           shortcut for -H "Authorization: Bearer TOKEN"
      --transport auto|http|sse  force an HTTP transport (default auto)
      --timeout SECONDS        network timeout (default 30)
      --protocol-version VER   MCP protocol version (default 2025-06-18)
  -v, --verbose                log JSON-RPC traffic to stderr

Examples
  mcp_gen_client.py http://localhost:3001/mcp
  mcp_gen_client.py http://localhost:3001/sse -o everything.py
  mcp_gen_client.py https://mcp.example.com/mcp --bearer "$TOKEN"
  mcp_gen_client.py --stdio -- npx -y @modelcontextprotocol/server-everything stdio

Exit codes: 0 ok, 1 protocol/transport/generation error, 2 bad usage.
Python 3.9+, no third-party dependencies (generator or generated file).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import keyword
import os
import pprint
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# ==== MCP RUNTIME BEGIN (embedded verbatim into generated clients) ====
import json as _json
import queue as _queue
import shlex as _shlex
import socket as _socket
import subprocess as _subprocess
import sys as _sys
import threading as _threading
import urllib.error as _urlerror
import urllib.parse as _urlparse
import urllib.request as _urlrequest

_STREAMABLE_PROTOCOL = "2025-06-18"
_LEGACY_PROTOCOL = "2024-11-05"


class McpError(Exception):
    """Transport or protocol failure."""


class _NotStreamableHttp(McpError):
    """POST rejected in a way that suggests a legacy HTTP+SSE server."""


def _rpc(method, params, msg_id):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if msg_id is not None:
        msg["id"] = msg_id
    return msg


def _iter_sse(raw):
    """Yield (event, data) pairs from a complete text/event-stream body."""
    event, data = "message", []
    for line in raw.splitlines() + [""]:
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)


class _Transport:
    kind = "?"
    protocol = _STREAMABLE_PROTOCOL

    def request(self, method, params=None):
        raise NotImplementedError

    def notify(self, method, params=None):
        raise NotImplementedError

    def close(self):
        pass

    @staticmethod
    def _check(method, msg):
        if "error" in msg:
            err = msg["error"]
            raise McpError("%s failed: %s %s" % (method, err.get("code"), err.get("message")))
        return msg.get("result")


class _HttpTransport(_Transport):
    """MCP Streamable HTTP: JSON-RPC over POST; responses as JSON or SSE."""

    kind = "http"

    def __init__(self, url, headers, timeout, verbose=False):
        self.url, self.headers, self.timeout, self.verbose = url, headers, timeout, verbose
        self.session_id = None
        self._next_id = 1

    def _post(self, body, expect_response):
        hdrs = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": self.protocol}
        hdrs.update(self.headers)
        if self.session_id:
            hdrs["Mcp-Session-Id"] = self.session_id
        if self.verbose:
            print("--> POST %s %s" % (self.url, _json.dumps(body)), file=_sys.stderr)
        req = _urlrequest.Request(self.url, data=_json.dumps(body).encode(), headers=hdrs,
                                  method="POST")
        try:
            resp = _urlrequest.urlopen(req, timeout=self.timeout)
        except _urlerror.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (404, 405):
                raise _NotStreamableHttp("HTTP %d from POST %s" % (e.code, self.url)) from None
            hint = " (check credentials)" if e.code in (401, 403) else ""
            raise McpError("HTTP %d from %s%s\n%s" % (e.code, self.url, hint, detail)) from None
        except _urlerror.URLError as e:
            raise McpError("cannot reach %s: %s" % (self.url, e.reason)) from None
        except OSError as e:
            raise McpError("POST %s: %s" % (self.url, e)) from None
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid and not self.session_id:
                self.session_id = sid
            if not expect_response:
                resp.read()
                return None
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            want = body.get("id")
            if ctype == "text/event-stream":
                buf = ""
                while True:
                    chunk = resp.readline()
                    if not chunk:
                        break
                    buf += chunk.decode(errors="replace")
                    if chunk.strip() == b"":
                        for _ev, data in _iter_sse(buf):
                            try:
                                msg = _json.loads(data)
                            except ValueError:
                                continue
                            if msg.get("id") == want:
                                return msg
                        buf = ""
                raise McpError("SSE stream ended without a response to id=%s" % want)
            raw = resp.read().decode(errors="replace")
            try:
                parsed = _json.loads(raw)
            except ValueError:
                raise McpError("non-JSON response (%s): %s" % (ctype, raw[:200])) from None
            if isinstance(parsed, list):
                parsed = next((m for m in parsed if m.get("id") == want), None)
                if parsed is None:
                    raise McpError("batch response lacked id=%s" % want)
            return parsed

    def request(self, method, params=None):
        msg_id = self._next_id
        self._next_id += 1
        resp = self._post(_rpc(method, params, msg_id), True)
        if self.verbose:
            print("<-- %s" % _json.dumps(resp)[:2000], file=_sys.stderr)
        return self._check(method, resp)

    def notify(self, method, params=None):
        self._post(_rpc(method, params, None), False)

    def close(self):
        if not self.session_id:
            return
        req = _urlrequest.Request(self.url, method="DELETE",
                                  headers=dict(self.headers, **{"Mcp-Session-Id": self.session_id}))
        try:
            _urlrequest.urlopen(req, timeout=self.timeout).close()
        except Exception:
            pass


class _LegacySseTransport(_Transport):
    """MCP HTTP+SSE (2024-11-05): GET stream for responses, POST for requests."""

    kind = "sse"
    protocol = _LEGACY_PROTOCOL

    def __init__(self, url, headers, timeout, verbose=False):
        self.url, self.headers, self.timeout, self.verbose = url, headers, timeout, verbose
        self._next_id = 1
        self._events = _queue.Queue()
        self._closed = False
        req = _urlrequest.Request(url, headers=dict(headers, Accept="text/event-stream"),
                                  method="GET")
        try:
            self._stream = _urlrequest.urlopen(req, timeout=None)
        except _urlerror.HTTPError as e:
            raise McpError("HTTP %d from GET %s — not an SSE endpoint" % (e.code, url)) from None
        except _urlerror.URLError as e:
            raise McpError("cannot reach %s: %s" % (url, e.reason)) from None
        except OSError as e:
            raise McpError("GET %s: %s" % (url, e)) from None
        ctype = (self._stream.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "text/event-stream":
            self._stream.close()
            raise McpError("GET %s returned %s, expected text/event-stream" % (url, ctype or "nothing"))
        self._reader = _threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        while True:
            try:
                event, data = self._events.get(timeout=timeout)
            except _queue.Empty:
                self.close()
                raise McpError("no `endpoint` event from %s within %ss" % (url, timeout)) from None
            if event == "endpoint":
                self.endpoint = _urlparse.urljoin(url, data.strip())
                break

    def _read_loop(self):
        event, data = "message", []
        try:
            while not self._closed:
                raw = self._stream.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip("\r\n")
                if line == "":
                    if data:
                        self._events.put((event, "\n".join(data)))
                    event, data = "message", []
                    continue
                if line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    event = value
                elif field == "data":
                    data.append(value)
        except Exception:
            pass
        finally:
            self._events.put(("__eof__", ""))

    def _post(self, body):
        if self.verbose:
            print("--> POST %s %s" % (self.endpoint, _json.dumps(body)), file=_sys.stderr)
        req = _urlrequest.Request(self.endpoint, data=_json.dumps(body).encode(),
                                  headers=dict(self.headers, **{"Content-Type": "application/json"}),
                                  method="POST")
        try:
            with _urlrequest.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except _urlerror.HTTPError as e:
            raise McpError("HTTP %d from POST %s\n%s" % (
                e.code, self.endpoint, e.read().decode(errors="replace")[:300])) from None
        except _urlerror.URLError as e:
            raise McpError("cannot reach %s: %s" % (self.endpoint, e.reason)) from None
        except OSError as e:
            raise McpError("POST %s: %s" % (self.endpoint, e)) from None

    def request(self, method, params=None):
        msg_id = self._next_id
        self._next_id += 1
        self._post(_rpc(method, params, msg_id))
        while True:
            try:
                event, data = self._events.get(timeout=self.timeout)
            except _queue.Empty:
                raise McpError("no response to %s within %ss" % (method, self.timeout)) from None
            if event == "__eof__":
                raise McpError("SSE stream closed before answering %s" % method)
            if event != "message":
                continue
            try:
                msg = _json.loads(data)
            except ValueError:
                continue
            if self.verbose:
                print("<-- %s" % data[:2000], file=_sys.stderr)
            if msg.get("id") == msg_id:
                return self._check(method, msg)

    def notify(self, method, params=None):
        self._post(_rpc(method, params, None))

    def close(self):
        self._closed = True
        try:  # unblock the reader before closing, else close() deadlocks on the buffer lock
            self._stream.fp.raw._sock.shutdown(_socket.SHUT_RDWR)
        except Exception:
            pass
        self._reader.join(timeout=2)
        try:
            self._stream.close()
        except Exception:
            pass


class _StdioTransport(_Transport):
    """MCP stdio: newline-delimited JSON-RPC over a subprocess."""

    kind = "stdio"

    def __init__(self, command, verbose=False):
        self.command, self.verbose = list(command), verbose
        self._next_id = 1
        try:
            self.proc = _subprocess.Popen(
                self.command, stdin=_subprocess.PIPE, stdout=_subprocess.PIPE,
                stderr=None if verbose else _subprocess.DEVNULL, text=True)
        except FileNotFoundError:
            raise McpError("command not found: %s" % self.command[0]) from None

    def _send(self, msg):
        if self.verbose:
            print("--> %s" % _json.dumps(msg), file=_sys.stderr)
        try:
            self.proc.stdin.write(_json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise McpError("server process closed stdin (crashed on startup?)") from None

    def request(self, method, params=None):
        msg_id = self._next_id
        self._next_id += 1
        self._send(_rpc(method, params, msg_id))
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise McpError("server exited before answering %s (exit %s)"
                               % (method, self.proc.poll()))
            line = line.strip()
            if not line:
                continue
            try:
                msg = _json.loads(line)
            except ValueError:
                continue
            if self.verbose:
                print("<-- %s" % line[:2000], file=_sys.stderr)
            if msg.get("id") == msg_id:
                return self._check(method, msg)

    def notify(self, method, params=None):
        self._send(_rpc(method, params, None))

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _open_http(url, headers, timeout, mode, verbose):
    """Pick Streamable HTTP or legacy SSE, probing when mode is 'auto'."""
    if mode == "sse":
        return _LegacySseTransport(url, headers, timeout, verbose)
    if mode == "auto" and _urlparse.urlsplit(url).path.rstrip("/").endswith("/sse"):
        try:
            return _LegacySseTransport(url, headers, timeout, verbose)
        except McpError:
            pass
    http = _HttpTransport(url, headers, timeout, verbose)
    if mode == "http":
        return http
    try:
        http._post(_rpc("ping", None, 0), True)
        return http
    except _NotStreamableHttp as e:
        try:
            return _LegacySseTransport(url, headers, timeout, verbose)
        except McpError as e2:
            raise McpError("%s; legacy SSE also failed: %s" % (e, e2)) from None
    except McpError:
        return http


class McpClient:
    """Minimal MCP client: connect, initialize, then call tools/resources/prompts.

    McpClient(url="http://host/mcp")           Streamable HTTP or legacy SSE (auto)
    McpClient(command=["npx", "-y", "..."])    stdio
    Use as a context manager or call close() when done.
    """

    def __init__(self, url=None, command=None, headers=None, timeout=30.0,
                 transport="auto", verbose=False, client_name="mcp-client"):
        headers = dict(headers or {})
        if command:
            self._t = _StdioTransport(command, verbose)
        elif url:
            self._t = _open_http(url, headers, timeout, transport, verbose)
        else:
            raise McpError("McpClient needs a url or a command")
        init = self._t.request("initialize", {
            "protocolVersion": self._t.protocol, "capabilities": {},
            "clientInfo": {"name": client_name, "version": "1.0"}})
        self._t.notify("notifications/initialized")
        self.server_info = init.get("serverInfo", {})
        self.capabilities = init.get("capabilities", {})
        self.instructions = init.get("instructions")
        self.transport = self._t.kind

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._t.close()

    def rpc(self, method, params=None):
        """Send any JSON-RPC request and return its result."""
        return self._t.request(method, params)

    def _paginate(self, method, key):
        items, cursor = [], None
        while True:
            result = self._t.request(method, {"cursor": cursor} if cursor else {}) or {}
            items.extend(result.get(key, []))
            cursor = result.get("nextCursor")
            if not cursor:
                return items

    def list_tools(self):
        return self._paginate("tools/list", "tools") if "tools" in self.capabilities else []

    def list_resources(self):
        return self._paginate("resources/list", "resources") if "resources" in self.capabilities else []

    def list_resource_templates(self):
        if "resources" not in self.capabilities:
            return []
        try:
            return self._paginate("resources/templates/list", "resourceTemplates")
        except McpError:
            return []

    def list_prompts(self):
        return self._paginate("prompts/list", "prompts") if "prompts" in self.capabilities else []

    def call_tool(self, name, arguments=None):
        return self._t.request("tools/call", {"name": name, "arguments": arguments or {}})

    def read_resource(self, uri):
        return self._t.request("resources/read", {"uri": uri})

    def get_prompt(self, name, arguments=None):
        return self._t.request("prompts/get", {"name": name, "arguments": arguments or {}})


def result_text(result):
    """Join the text parts of a tools/call, resources/read or prompts/get result."""
    parts = []
    for item in (result or {}).get("content", []) or []:
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
    for item in (result or {}).get("contents", []) or []:
        if "text" in item:
            parts.append(item["text"])
    for msg in (result or {}).get("messages", []) or []:
        content = msg.get("content") or {}
        if content.get("type") == "text":
            parts.append(content.get("text", ""))
    return "\n".join(parts)
# ==== MCP RUNTIME END ====


# --------------------------------------------------------------------------- #
# Code generation helpers
# --------------------------------------------------------------------------- #
RESERVED = {"McpClient", "McpError", "result_text", "connect", "client", "close",
            "read_resource", "get_prompt", "list_tools", "call_tool", "rpc", "main",
            "TOOLS", "RESOURCES", "RESOURCE_TEMPLATES", "PROMPTS", "SERVER"}
SENSITIVE_HEADER = re.compile(r"authorization|token|secret|api[-_]?key|cookie", re.I)


def py_ident(name: str, taken: set, suffix: str = "_") -> str:
    ident = re.sub(r"\W", "_", name)
    if not ident or ident[0].isdigit():
        ident = "_" + ident
    if keyword.iskeyword(ident) or ident in RESERVED or ident.startswith("__"):
        ident += suffix
    base, n = ident, 2
    while ident in taken:
        ident, n = f"{base}{n}", n + 1
    taken.add(ident)
    return ident


def py_type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "Any"
    enum = schema.get("enum")
    if enum and all(isinstance(v, (str, int, bool)) for v in enum):
        return "Literal[" + ", ".join(repr(v) for v in enum) + "]"
    typ = schema.get("type")
    if isinstance(typ, list):
        non_null = [t for t in typ if t != "null"]
        if len(non_null) == 1:
            inner = py_type(dict(schema, type=non_null[0]))
            return f"Optional[{inner}]" if "null" in typ else inner
        return "Any"
    if typ == "string":
        return "str"
    if typ == "integer":
        return "int"
    if typ == "number":
        return "float"
    if typ == "boolean":
        return "bool"
    if typ == "array":
        return f"List[{py_type(schema.get('items'))}]"
    if typ == "object" or "properties" in schema:
        return "Dict[str, Any]"
    for combo in ("anyOf", "oneOf"):
        if schema.get(combo):
            return "Any"
    return "Any"


def py_literal(obj: Any, indent: int = 0) -> str:
    """Render JSON-ish data as a Python literal (json.dumps would emit true/null)."""
    text = pprint.pformat(obj, width=100, sort_dicts=False)
    if indent:
        pad = " " * indent
        text = ("\n" + pad).join(text.splitlines())
    return text


def first_line(text: Optional[str]) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def doc_block(text: str, indent: str) -> List[str]:
    """Indent a multi-line description for use inside a docstring."""
    out = []
    for line in (text or "").strip().replace('"""', "'''").splitlines():
        out.append((indent + line).rstrip())
    return out


def gen_tool_function(tool: dict, fn_name: str, tool_names: set) -> List[str]:
    schema = tool.get("inputSchema") or {}
    props: Dict[str, dict] = schema.get("properties") or {}
    required = [p for p in (schema.get("required") or []) if p in props]
    optional = [p for p in props if p not in required]
    taken: set = set()
    params: List[Tuple[str, str, dict, bool]] = []  # (json_name, py_name, schema, required)
    for p in required + optional:
        params.append((p, py_ident(p, taken), props[p] if isinstance(props[p], dict) else {},
                       p in required))

    sig = []
    for json_name, py_name, sub, req in params:
        if req:
            sig.append(f"{py_name}: {py_type(sub)}")
    if optional:
        sig.append("*")
        for json_name, py_name, sub, req in params:
            if not req:
                t = py_type(sub)
                if not t.startswith("Optional["):
                    t = f"Optional[{t}]"
                sig.append(f"{py_name}: {t} = None")
    if sig and sig[-1] == "*":
        sig.pop()

    lines = [f"def {fn_name}({', '.join(sig)}) -> Dict[str, Any]:"]
    doc = [f'    """{first_line(tool.get("description")) or "Call the " + json.dumps(tool["name"]) + " tool."}']
    rest = "\n".join((tool.get("description") or "").strip().splitlines()[1:]).strip()
    if rest:
        doc.append("")
        doc += doc_block(rest, "    ")
    if params:
        doc.append("")
        doc.append("    Args:")
        for json_name, py_name, sub, req in params:
            desc = first_line(sub.get("description"))
            tag = "required" if req else "optional"
            label = py_name if py_name == json_name else f"{py_name} (json: {json_name!r})"
            doc.append(f"        {label}: {tag}" + (f" — {desc}" if desc else ""))
    doc.append("")
    doc.append("    Returns the raw tools/call result; pass it to result_text() for the text.")
    doc.append('    """')
    lines += doc
    lines.append("    args: Dict[str, Any] = {}")
    for json_name, py_name, sub, req in params:
        if req:
            lines.append(f"    args[{json_name!r}] = {py_name}")
    for json_name, py_name, sub, req in params:
        if not req:
            lines.append(f"    if {py_name} is not None:")
            lines.append(f"        args[{json_name!r}] = {py_name}")
    lines.append(f"    return client().call_tool({tool['name']!r}, args)")
    return lines


def gen_prompt_function(prompt: dict, fn_name: str) -> List[str]:
    pargs = prompt.get("arguments") or []
    taken: set = set()
    params = [(a["name"], py_ident(a["name"], taken), bool(a.get("required")), a) for a in pargs]
    sig = [f"{py}: str" for name, py, req, a in params if req]
    opt = [f"{py}: Optional[str] = None" for name, py, req, a in params if not req]
    if opt:
        sig += ["*"] + opt
    lines = [f"def {fn_name}({', '.join(sig)}) -> Dict[str, Any]:"]
    lines.append(f'    """{first_line(prompt.get("description")) or "Get the " + json.dumps(prompt["name"]) + " prompt."}')
    if params:
        lines.append("")
        lines.append("    Args:")
        for name, py, req, a in params:
            desc = first_line(a.get("description"))
            lines.append(f"        {py}: {'required' if req else 'optional'}" + (f" — {desc}" if desc else ""))
    lines.append("")
    lines.append("    Returns the raw prompts/get result (messages list).")
    lines.append('    """')
    lines.append("    args: Dict[str, str] = {}")
    for name, py, req, a in params:
        if req:
            lines.append(f"    args[{name!r}] = {py}")
    for name, py, req, a in params:
        if not req:
            lines.append(f"    if {py} is not None:")
            lines.append(f"        args[{name!r}] = {py}")
    lines.append(f"    return client().get_prompt({prompt['name']!r}, args)")
    return lines


def runtime_source() -> str:
    """The embedded runtime is this file's text between the two markers."""
    with open(__file__, encoding="utf-8") as f:
        src = f.read()
    start = src.index("# ==== MCP RUNTIME BEGIN")
    end = src.index("# ==== MCP RUNTIME END ====") + len("# ==== MCP RUNTIME END ====")
    return src[start:end]


def generate(info: dict, *, url: Optional[str], command: Optional[List[str]],
             transport: str, headers: Dict[str, str], timeout: float,
             module_name: str, generator_cmd: str) -> Tuple[str, List[str]]:
    """Return (source, notes) for the generated client module."""
    notes: List[str] = []
    baked_headers: Dict[str, str] = {}
    for k, v in headers.items():
        if SENSITIVE_HEADER.search(k):
            env = "MCP_" + re.sub(r"\W", "_", k).upper()
            baked_headers[k] = "$" + env
            notes.append(f"header {k!r} is not baked in — set {env}='{v.split(' ')[0]} ...' when running the client")
        else:
            baked_headers[k] = v

    srv = info["server"]
    tools, resources, templates, prompts = (info["tools"], info["resources"],
                                            info["resourceTemplates"], info["prompts"])
    taken: set = set()
    tool_fns = [(t, py_ident(t["name"], taken)) for t in tools]
    prompt_fns = [(p, py_ident("prompt_" + p["name"], taken)) for p in prompts]

    L: List[str] = []
    L.append("#!/usr/bin/env python3")
    L.append('"""')
    L.append(f"{module_name}.py — Python client for the MCP server "
             f"{srv.get('name', '?')} {srv.get('version', '')}".rstrip())
    L.append("")
    L.append(f"Generated {_dt.date.today().isoformat()} by mcp_gen_client.py from:")
    L.append(f"    {generator_cmd}")
    L.append("Every tool the server exposes is a function below; nothing here needs")
    L.append("anything outside the Python standard library (3.9+).")
    L.append("")
    L.append("Library use")
    L.append(f"    import {module_name} as mcp")
    if tool_fns:
        t0, f0 = tool_fns[0]
        props = (t0.get("inputSchema") or {}).get("properties") or {}
        req = [p for p in (t0.get("inputSchema") or {}).get("required", []) if p in props]
        ex = ", ".join(f"{re.sub(r'\\W', '_', p)}=..." for p in req) if req else ""
        L.append(f"    result = mcp.{f0}({ex})")
        L.append("    print(mcp.result_text(result))")
    L.append("    mcp.close()                     # optional; also runs at exit")
    L.append("")
    L.append("CLI use")
    L.append(f"    python {module_name}.py list                    # tools, resources, prompts")
    L.append(f"    python {module_name}.py schema TOOL             # input schema as JSON")
    if tool_fns:
        t0, f0 = tool_fns[0]
        props = (t0.get("inputSchema") or {}).get("properties") or {}
        req = [p for p in (t0.get("inputSchema") or {}).get("required", []) if p in props]
        flags = " ".join(f"--{cli_flag(p)} VALUE" for p in req)
        L.append(f"    python {module_name}.py call {t0['name']} {flags}".rstrip())
    L.append(f"    python {module_name}.py call TOOL ... --text    # print only text content")
    L.append(f"    python {module_name}.py read URI")
    L.append(f"    python {module_name}.py prompt NAME [--arg value ...]")
    L.append(f"    python {module_name}.py rpc METHOD ['{{json params}}']")
    L.append("")
    L.append("Connection")
    if command:
        L.append(f"    stdio server, launched as: {' '.join(_shlex.quote(c) for c in command)}")
        L.append("    Override with MCP_COMMAND (a shell-style string).")
    else:
        L.append(f"    {url}  ({'legacy HTTP+SSE' if transport == 'sse' else 'Streamable HTTP'})")
        L.append("    Override with MCP_URL (transport auto-detected; force with MCP_TRANSPORT=http|sse).")
        L.append("    Extra headers: MCP_HEADERS='Name: v; Other: w'.")
    for k, v in baked_headers.items():
        if v.startswith("$"):
            L.append(f"    header {k}: taken from ${v[1:]} (required)")
    if info.get("instructions"):
        L.append("")
        L.append("Server instructions")
        L += doc_block(info["instructions"], "    ")
    L.append('"""')
    L.append("")
    L.append("from __future__ import annotations")
    L.append("")
    L.append("import argparse")
    L.append("import atexit")
    L.append("import json")
    L.append("import os")
    L.append("import sys")
    L.append("from typing import Any, Dict, List, Literal, Optional")
    L.append("")
    L.append(runtime_source())
    L.append("")
    L.append("")
    L.append("# --------------------------------------------------------------------------- #")
    L.append("# Connection settings (env vars override)")
    L.append("# --------------------------------------------------------------------------- #")
    L.append(f"DEFAULT_URL = {url!r}")
    L.append(f"DEFAULT_COMMAND = {command!r}")
    L.append(f"DEFAULT_TRANSPORT = {transport!r}")
    L.append(f"DEFAULT_HEADERS = {baked_headers!r}")
    L.append(f"DEFAULT_TIMEOUT = {timeout!r}")
    L.append(f"SERVER = {py_literal(srv)}")
    L.append("")
    L.append("_client: Optional[McpClient] = None")
    L.append("")
    L.append("")
    L.append("def _headers() -> Dict[str, str]:")
    L.append("    out = {}")
    L.append("    for k, v in DEFAULT_HEADERS.items():")
    L.append("        if v.startswith('$'):")
    L.append("            v = os.environ.get(v[1:], '')")
    L.append("            if not v:")
    L.append("                print('warning: header %s needs env var %s' % (k, DEFAULT_HEADERS[k][1:]), file=sys.stderr)")
    L.append("                continue")
    L.append("        out[k] = v")
    L.append("    for item in os.environ.get('MCP_HEADERS', '').split(';'):")
    L.append("        name, sep, value = item.partition(':')")
    L.append("        if sep and name.strip():")
    L.append("            out[name.strip()] = value.strip()")
    L.append("    return out")
    L.append("")
    L.append("")
    L.append("def connect(url: Optional[str] = None, command: Optional[List[str]] = None, **kw: Any) -> McpClient:")
    L.append('    """Create (and register as default) a client; kwargs go to McpClient."""')
    L.append("    global _client")
    L.append("    if _client is not None:")
    L.append("        _client.close()")
    L.append("    env_cmd = os.environ.get('MCP_COMMAND')")
    L.append("    command = command or (_shlex.split(env_cmd) if env_cmd else None) or DEFAULT_COMMAND")
    L.append("    url = url or os.environ.get('MCP_URL') or DEFAULT_URL")
    L.append("    kw.setdefault('headers', _headers())")
    L.append("    kw.setdefault('timeout', float(os.environ.get('MCP_TIMEOUT', DEFAULT_TIMEOUT)))")
    L.append("    # the baked-in transport only applies to the baked-in URL; anything else is probed")
    L.append("    kw.setdefault('transport', os.environ.get('MCP_TRANSPORT')")
    L.append("                  or (DEFAULT_TRANSPORT if url == DEFAULT_URL else 'auto'))")
    L.append("    kw.setdefault('verbose', bool(os.environ.get('MCP_VERBOSE')))")
    L.append(f"    kw.setdefault('client_name', {module_name!r})")
    L.append("    if command:")
    L.append("        _client = McpClient(command=command, **kw)")
    L.append("    else:")
    L.append("        _client = McpClient(url=url, **kw)")
    L.append("    return _client")
    L.append("")
    L.append("")
    L.append("def client() -> McpClient:")
    L.append('    """The default client, connecting lazily on first use."""')
    L.append("    return _client if _client is not None else connect()")
    L.append("")
    L.append("")
    L.append("def close() -> None:")
    L.append("    global _client")
    L.append("    if _client is not None:")
    L.append("        _client.close()")
    L.append("        _client = None")
    L.append("")
    L.append("")
    L.append("atexit.register(close)")
    L.append("")
    L.append("")
    L.append("def read_resource(uri: str) -> Dict[str, Any]:")
    L.append('    """resources/read for any URI (see RESOURCES and RESOURCE_TEMPLATES)."""')
    L.append("    return client().read_resource(uri)")
    L.append("")
    L.append("")
    L.append("def get_prompt(name: str, arguments: Optional[Dict[str, str]] = None) -> Dict[str, Any]:")
    L.append('    """prompts/get for any prompt name (see PROMPTS)."""')
    L.append("    return client().get_prompt(name, arguments)")
    L.append("")
    L.append("")
    L.append("def call_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:")
    L.append('    """tools/call by name, for tools added after this file was generated."""')
    L.append("    return client().call_tool(name, arguments)")
    L.append("")
    L.append("")
    L.append("def rpc(method: str, params: Optional[Dict[str, Any]] = None) -> Any:")
    L.append('    """Any raw JSON-RPC request."""')
    L.append("    return client().rpc(method, params)")
    L.append("")

    L.append("")
    L.append("# --------------------------------------------------------------------------- #")
    L.append(f"# Tools ({len(tools)})")
    L.append("# --------------------------------------------------------------------------- #")
    for tool, fn in tool_fns:
        L.append("")
        L += gen_tool_function(tool, fn, taken)
        L.append("")
    L.append("")
    L.append("TOOLS: Dict[str, Dict[str, Any]] = {")
    for tool, fn in tool_fns:
        L.append(f"    {tool['name']!r}: {{")
        L.append(f"        'function': {fn},")
        L.append(f"        'description': {py_literal(tool.get('description'), 8)},")
        L.append(f"        'inputSchema': {py_literal(tool.get('inputSchema') or {}, 8)},")
        L.append("    },")
    L.append("}")

    L.append("")
    L.append("")
    L.append("# --------------------------------------------------------------------------- #")
    L.append(f"# Resources ({len(resources)}) and templates ({len(templates)})")
    L.append("# --------------------------------------------------------------------------- #")
    L.append("RESOURCES: List[Dict[str, Any]] = " + py_literal(
        [{k: r.get(k) for k in ("uri", "name", "description", "mimeType") if r.get(k) is not None}
         for r in resources]))
    L.append("")
    L.append("RESOURCE_TEMPLATES: List[Dict[str, Any]] = " + py_literal(
        [{k: r.get(k) for k in ("uriTemplate", "name", "description", "mimeType") if r.get(k) is not None}
         for r in templates]))

    L.append("")
    L.append("")
    L.append("# --------------------------------------------------------------------------- #")
    L.append(f"# Prompts ({len(prompts)})")
    L.append("# --------------------------------------------------------------------------- #")
    for prompt, fn in prompt_fns:
        L.append("")
        L += gen_prompt_function(prompt, fn)
        L.append("")
    L.append("")
    L.append("PROMPTS: List[Dict[str, Any]] = " + py_literal(
        [{k: p.get(k) for k in ("name", "description", "arguments") if p.get(k) is not None}
         for p in prompts]))

    L.append("")
    L.append("")
    L += CLI_SOURCE.splitlines()
    return "\n".join(L).rstrip() + "\n", notes


def cli_flag(name: str) -> str:
    """messageType -> message-type, some_arg -> some-arg (mirrors _cli_flag in CLI_SOURCE)."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "arg"


CLI_SOURCE = r'''
# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli_flag(name: str) -> str:
    import re
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "arg"


def _add_schema_args(parser: argparse.ArgumentParser, schema: Dict[str, Any]) -> Dict[str, str]:
    """Add one option per schema property; return dest -> json name map."""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    mapping = {}
    for name, sub in props.items():
        sub = sub if isinstance(sub, dict) else {}
        flag, dest = "--" + _cli_flag(name), "arg_" + _cli_flag(name).replace("-", "_")
        mapping[dest] = name
        typ = sub.get("type")
        if isinstance(typ, list):
            typ = next((t for t in typ if t != "null"), None)
        help_text = (sub.get("description") or "").strip().splitlines()[:1]
        help_text = (help_text[0] if help_text else "") + (" (required)" if name in required else "")
        kw: Dict[str, Any] = {"dest": dest, "help": help_text, "required": name in required}
        if sub.get("enum"):
            kw["choices"] = [str(v) for v in sub["enum"]]
            if typ in ("integer", "number"):
                kw["type"] = int if typ == "integer" else float
                kw["choices"] = sub["enum"]
        elif typ == "integer":
            kw["type"] = int
        elif typ == "number":
            kw["type"] = float
        elif typ == "boolean":
            grp = parser.add_mutually_exclusive_group(required=name in required)
            grp.add_argument(flag, dest=dest, action="store_true", default=None, help=help_text)
            grp.add_argument("--no-" + _cli_flag(name), dest=dest, action="store_false", help=argparse.SUPPRESS)
            continue
        elif typ in ("array", "object"):
            kw["type"] = json.loads
            kw["metavar"] = "JSON"
        parser.add_argument(flag, **kw)
    return mapping


def _print(result: Any, text_only: bool) -> None:
    if text_only:
        print(result_text(result))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--url", help="override the server URL (or set MCP_URL)")
    p.add_argument("--command", help="override the stdio command (or set MCP_COMMAND)")
    p.add_argument("-v", "--verbose", action="store_true", help="log JSON-RPC traffic")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list tools, resources and prompts")
    sp = sub.add_parser("schema", help="print a tool's input schema")
    sp.add_argument("tool", choices=sorted(TOOLS))
    sp = sub.add_parser("read", help="read a resource")
    sp.add_argument("uri")
    sp.add_argument("--text", action="store_true")
    sp = sub.add_parser("prompt", help="get a prompt")
    sp.add_argument("name")
    sp.add_argument("--arg", action="append", default=[], metavar="NAME=VALUE")
    sp.add_argument("--text", action="store_true")
    sp = sub.add_parser("rpc", help="raw JSON-RPC request")
    sp.add_argument("method")
    sp.add_argument("params", nargs="?", default="{}", help="JSON object")

    call = sub.add_parser("call", help="call a tool").add_subparsers(dest="tool", required=True)
    mappings = {}
    for name, meta in TOOLS.items():
        first = (meta["description"] or "").strip().splitlines()[:1]
        tp = call.add_parser(name, help=first[0] if first else None,
                             description=meta["description"])
        mappings[name] = _add_schema_args(tp, meta["inputSchema"])
        tp.add_argument("--text", action="store_true", help="print only text content")

    a = p.parse_args(argv)
    try:
        if a.url or a.command or a.verbose:
            connect(url=a.url, command=_shlex.split(a.command) if a.command else None,
                    verbose=a.verbose)
        if a.cmd == "list":
            print("server: %s %s  (%s)" % (SERVER.get("name"), SERVER.get("version", ""), client().transport))
            print("\ntools:")
            for name, meta in TOOLS.items():
                first = (meta["description"] or "").strip().splitlines()[:1]
                print("  %-32s %s" % (name, first[0] if first else ""))
            if RESOURCES:
                print("\nresources:")
                for r in RESOURCES:
                    print("  %-48s %s" % (r["uri"], r.get("name", "")))
            if RESOURCE_TEMPLATES:
                print("\nresource templates:")
                for r in RESOURCE_TEMPLATES:
                    print("  %-48s %s" % (r["uriTemplate"], r.get("name", "")))
            if PROMPTS:
                print("\nprompts:")
                for pr in PROMPTS:
                    args = ", ".join(x["name"] + ("" if x.get("required") else "?") for x in pr.get("arguments") or [])
                    print("  %-32s %s" % (pr["name"] + ("(" + args + ")" if args else ""), pr.get("description") or ""))
        elif a.cmd == "schema":
            print(json.dumps(TOOLS[a.tool]["inputSchema"], indent=2))
        elif a.cmd == "read":
            _print(read_resource(a.uri), a.text)
        elif a.cmd == "prompt":
            args = dict(item.split("=", 1) for item in a.arg)
            _print(get_prompt(a.name, args), a.text)
        elif a.cmd == "rpc":
            _print(rpc(a.method, json.loads(a.params)), False)
        elif a.cmd == "call":
            args = {json_name: getattr(a, dest) for dest, json_name in mappings[a.tool].items()
                    if getattr(a, dest, None) is not None}
            _print(call_tool(a.tool, args), a.text)
        return 0
    except McpError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print("error: bad JSON argument: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
'''


# --------------------------------------------------------------------------- #
# Generator CLI
# --------------------------------------------------------------------------- #
def parse_headers(items: List[str]) -> Dict[str, str]:
    headers = {}
    for item in items:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise SystemExit(f"error: bad header {item!r}; expected 'Name: value'")
        headers[name.strip()] = value.strip()
    return headers


def default_module_name(server_name: str, url: Optional[str], command: Optional[List[str]]) -> str:
    base = server_name.split("/")[-1] if server_name else ""
    if not base and url:
        base = re.sub(r"^www\.", "", (re.match(r"https?://([^/:]+)", url) or [None, ""])[1])
    if not base and command:
        base = os.path.basename(command[-1])
    base = re.sub(r"\W+", "_", base).strip("_").lower() or "mcp_server"
    if base[0].isdigit():
        base = "_" + base
    return base + "_mcp"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="mcp_gen_client.py",
        description="Generate a standalone Python client module for an MCP server.",
        epilog="stdio form: mcp_gen_client.py --stdio -- CMD [ARGS...]")
    p.add_argument("url", nargs="?", help="HTTP endpoint, e.g. http://localhost:3001/mcp")
    p.add_argument("--stdio", action="store_true", help="launch a stdio server; command follows '--'")
    p.add_argument("-o", "--output", help="output file (default: <server-name>_mcp.py)")
    p.add_argument("--module-name", help="module name used in docs (default: from output file)")
    p.add_argument("-H", "--header", action="append", default=[], metavar="'Name: value'")
    p.add_argument("--bearer", metavar="TOKEN")
    p.add_argument("--transport", choices=["auto", "http", "sse"], default="auto")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--protocol-version", default=_STREAMABLE_PROTOCOL)
    p.add_argument("-v", "--verbose", action="store_true")

    argv = list(sys.argv[1:] if argv is None else argv)
    cmd: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    args = p.parse_args(argv)
    generator_cmd = "mcp_gen_client.py " + " ".join(_shlex.quote(a) for a in argv + (["--"] + cmd if cmd else []))
    if args.bearer:  # never echo the token into the generated file's header
        generator_cmd = re.sub(r"--bearer \S+", "--bearer ***", generator_cmd)

    headers = parse_headers(args.header)
    if args.bearer:
        headers["Authorization"] = f"Bearer {args.bearer}"

    if args.stdio:
        if args.url:
            cmd.insert(0, args.url)
        if not cmd:
            p.error("--stdio requires a command after '--'")
    else:
        if cmd:
            p.error("a command after '--' only makes sense with --stdio")
        if not args.url:
            p.error("give an HTTP URL, or use --stdio -- CMD [ARGS...]")
        if not args.url.startswith(("http://", "https://")):
            p.error("URL must start with http:// or https://")

    _HttpTransport.protocol = args.protocol_version
    try:
        if args.stdio:
            c = McpClient(command=cmd, verbose=args.verbose, client_name="mcp_gen_client")
        else:
            c = McpClient(url=args.url, headers=headers, timeout=args.timeout,
                          transport=args.transport, verbose=args.verbose,
                          client_name="mcp_gen_client")
        with c:
            info = {
                "server": c.server_info, "capabilities": c.capabilities,
                "instructions": c.instructions,
                "tools": c.list_tools(), "resources": c.list_resources(),
                "resourceTemplates": c.list_resource_templates(), "prompts": c.list_prompts(),
            }
            transport = c.transport
    except McpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    output = args.output or default_module_name(info["server"].get("name", ""), args.url, cmd or None) + ".py"
    module_name = args.module_name or re.sub(r"\.py$", "", os.path.basename(output))
    if not module_name.isidentifier():
        module_name = re.sub(r"\W", "_", module_name)

    source, notes = generate(info, url=None if args.stdio else args.url,
                             command=cmd if args.stdio else None, transport=transport,
                             headers=headers, timeout=args.timeout, module_name=module_name,
                             generator_cmd=generator_cmd)
    try:
        code = compile(source, output, "exec")
        exec(code, {"__name__": "_mcp_gen_selfcheck", "__file__": output})  # import-time only
    except Exception as e:
        broken = output + ".broken"
        with open(broken, "w", encoding="utf-8") as f:
            f.write(source)
        print(f"error: generated module fails to load ({type(e).__name__}: {e}); "
              f"written to {broken} for inspection", file=sys.stderr)
        return 1

    with open(output, "w", encoding="utf-8") as f:
        f.write(source)
    try:
        os.chmod(output, os.stat(output).st_mode | 0o111)
    except OSError:
        pass

    srv = info["server"]
    print(f"wrote {output}: {srv.get('name', '?')} {srv.get('version', '')} via {transport} — "
          f"{len(info['tools'])} tools, {len(info['resources'])} resources, "
          f"{len(info['resourceTemplates'])} templates, {len(info['prompts'])} prompts",
          file=sys.stderr)
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
