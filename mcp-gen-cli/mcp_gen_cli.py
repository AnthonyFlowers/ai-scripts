#!/usr/bin/env python3
"""
mcp_gen_cli.py — generate a standalone bash script that exposes every tool of
an MCP server as a one-shot subcommand.

Each invocation of the generated script is a single, complete request: it
opens a session, runs the initialize handshake, makes exactly one call, prints
the result and closes the session. Nothing persists between invocations, so
the script is safe to drop into cron jobs, CI steps, other shell scripts, or
hand to someone who just wants `./server.sh some-tool --arg value`.

    ./everything_mcp.sh list
    ./everything_mcp.sh get-sum --a 1 --b 2 --text
    ./everything_mcp.sh get-annotated-message --message-type debug --include-image
    ./everything_mcp.sh read demo://resource/1 --text
    ./everything_mcp.sh prompt args-prompt --arg city=Rome
    ./everything_mcp.sh rpc ping

Two flavours, same command surface:
  --lang bash    needs bash 3.2+, curl, and jq (python3 as JSON fallback);
                 the curl calls are right there in the file.
  --lang python  needs only python3 (stdlib); embeds the MCP runtime.
Both support Streamable HTTP, legacy HTTP+SSE and stdio servers.

Usage
  mcp_gen_cli.py URL [options]                # HTTP server (either flavour)
  mcp_gen_cli.py --stdio -- CMD [ARGS...]     # stdio server

      --lang bash|python       what to generate (default bash). The Python
                               flavour has the same commands and flags, needs
                               only python3 (no curl/jq), and works on Windows.
  -o, --output FILE            where to write (default: <server-name>_mcp.sh|.py)
  -H, --header "Name: value"   header for discovery AND baked into the script
                               (repeatable). Credentials are NOT baked in:
                               Authorization / *token* / *key* / *secret*
                               headers become $MCP_<NAME> env-var lookups.
      --bearer TOKEN           shortcut for -H "Authorization: Bearer TOKEN"
      --transport auto|http|sse  force an HTTP transport (default auto)
      --timeout SECONDS        network timeout (default 30)
      --protocol-version VER   MCP protocol version (default 2025-06-18)
  -v, --verbose                log JSON-RPC traffic to stderr

Exit codes: 0 ok, 1 protocol/transport/generation error, 2 bad usage.
Python 3.9+, no third-party dependencies.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import pprint
import re
import shutil
import subprocess
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
# bash templates (@@PLACEHOLDER@@ substituted by generate())
# --------------------------------------------------------------------------- #
SH_COMMON = r'''
set -euo pipefail

# --------------------------------------------------------------------------- #
# Connection (env vars override the baked-in defaults)
# --------------------------------------------------------------------------- #
@@CONNECTION@@
MCP_TIMEOUT="${MCP_TIMEOUT:-@@TIMEOUT@@}"
MCP_PROTOCOL="@@PROTOCOL@@"
MCP_VERBOSE="${MCP_VERBOSE:-}"

_hdr=()
@@BAKED_HEADERS@@
# MCP_HEADERS='Name: value; Other: value' adds headers at run time
if [ -n "${MCP_HEADERS:-}" ]; then
  _old_ifs=$IFS; IFS=';'
  for _h in $MCP_HEADERS; do
    _h="${_h#"${_h%%[![:space:]]*}"}"
    [ -n "$_h" ] && _hdr+=(-H "$_h")
  done
  IFS=$_old_ifs
fi

_die() { echo "error: $*" >&2; exit 1; }
_log() { [ -n "$MCP_VERBOSE" ] && echo "$*" >&2 || true; }

# --------------------------------------------------------------------------- #
# JSON helpers — jq when available, otherwise python3
# --------------------------------------------------------------------------- #
if command -v jq >/dev/null 2>&1; then _JSON=jq
elif command -v python3 >/dev/null 2>&1; then _JSON=py
else _die "need jq or python3 for JSON handling"; fi

_PYJSON="
import json, sys
op, argv = sys.argv[1], sys.argv[2:]
def messages(raw):
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith(\"{\") or raw.startswith(\"[\"):
        doc = json.loads(raw)
        return doc if isinstance(doc, list) else [doc]
    out = []
    for line in raw.splitlines():
        if line.startswith(\"data:\"):
            try:
                out.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    return out
if op == \"args\":
    obj = {}
    for i in range(0, len(argv), 3):
        typ, key, val = argv[i], argv[i + 1], argv[i + 2]
        if typ == \"string\":
            obj[key] = val
        elif typ == \"number\":
            obj[key] = float(val) if \".\" in val or \"e\" in val.lower() else int(val)
        elif typ == \"bool\":
            obj[key] = val == \"true\"
        else:
            obj[key] = json.loads(val)
    print(json.dumps(obj))
elif op == \"pick\":
    want = int(argv[0])
    for m in messages(sys.stdin.read()):
        if isinstance(m, dict) and m.get(\"id\") == want:
            print(json.dumps(m)); break
elif op == \"text\":
    m = json.loads(sys.stdin.read()); r = m.get(\"result\") or {}; parts = []
    parts += [c.get(\"text\", \"\") for c in r.get(\"content\") or [] if c.get(\"type\") == \"text\"]
    parts += [c[\"text\"] for c in r.get(\"contents\") or [] if \"text\" in c]
    parts += [x[\"content\"].get(\"text\", \"\") for x in r.get(\"messages\") or []
              if (x.get(\"content\") or {}).get(\"type\") == \"text\"]
    print(\"\\n\".join(parts))
elif op == \"result\":
    print(json.dumps(json.loads(sys.stdin.read()).get(\"result\"), indent=2, ensure_ascii=False))
elif op == \"error\":
    e = json.loads(sys.stdin.read()).get(\"error\")
    if e: print(\"%s %s\" % (e.get(\"code\"), e.get(\"message\")))
"

# _json_args TYPE KEY VALUE [TYPE KEY VALUE ...]  -> JSON object
_json_args() {
  local i=1
  while [ $i -le $# ]; do  # validate before either backend sees the values
    eval "_t=\${$i}; _k=\${$((i + 1))}; _v=\${$((i + 2))}"
    case "$_t" in
      number) [[ "$_v" =~ ^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$ ]] || _die "--$_k expects a number, got '$_v'" ;;
      json)   if [ "$_JSON" = jq ]; then printf '%s' "$_v" | jq -e . >/dev/null 2>&1
              else printf '%s' "$_v" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; fi \
              || _die "--$_k expects JSON, got '$_v'" ;;
    esac
    i=$((i + 3))
  done
  if [ "$_JSON" = jq ]; then
    local jqargs=()
    while [ $# -ge 3 ]; do
      case "$1" in
        string) jqargs+=(--arg "$2" "$3") ;;
        *)      jqargs+=(--argjson "$2" "$3") ;;
      esac
      shift 3
    done
    jq -cn '$ARGS.named' ${jqargs[@]+"${jqargs[@]}"}
  else
    python3 -c "$_PYJSON" args "$@"
  fi
}
# stdin: raw response body (JSON, JSON array, or SSE framing) -> the message with id $1
_json_pick() {
  local body; body=$(cat)
  if [ "$_JSON" = jq ]; then
    case "$body" in
      [\[\{]*) printf '%s' "$body" | jq -c --argjson id "$1" 'if type=="array" then .[] else . end | select(type=="object" and .id == $id)' 2>/dev/null | head -n 1 ;;
      *) printf '%s\n' "$body" | sed -n 's/^data: *//p' | jq -cR --argjson id "$1" 'fromjson? // empty | select(type=="object" and .id == $id)' 2>/dev/null | head -n 1 ;;
    esac
  else printf '%s' "$body" | python3 -c "$_PYJSON" pick "$1"; fi
}
_json_text() {
  if [ "$_JSON" = jq ]; then
    jq -r '[.result.content[]? | select(.type=="text") | .text] + [.result.contents[]? | .text? // empty] + [.result.messages[]? | .content | select(.type=="text") | .text] | join("\n")'
  else python3 -c "$_PYJSON" text; fi
}
_json_result() { if [ "$_JSON" = jq ]; then jq '.result'; else python3 -c "$_PYJSON" result; fi; }
_json_error()  { if [ "$_JSON" = jq ]; then jq -r '.error | select(. != null) | "\(.code) \(.message)"'; else python3 -c "$_PYJSON" error; fi; }
_json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
_json_msg() { # ID METHOD [PARAMS_JSON]
  if [ -n "${3:-}" ]; then printf '{"jsonrpc":"2.0","id":%s,"method":"%s","params":%s}' "$1" "$(_json_escape "$2")" "$3"
  else printf '{"jsonrpc":"2.0","id":%s,"method":"%s"}' "$1" "$(_json_escape "$2")"; fi
}
_init_msg() { printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"%s","capabilities":{},"clientInfo":{"name":"%s","version":"1.0"}}}' "$MCP_PROTOCOL" "@@SCRIPT_NAME@@"; }
_initialized_msg() { printf '{"jsonrpc":"2.0","method":"notifications/initialized"}'; }
'''

SH_HTTP = r'''
# --------------------------------------------------------------------------- #
# Transport: Streamable HTTP — every call is initialize → initialized → request → DELETE
# --------------------------------------------------------------------------- #
MCP_SESSION=""
_tmp=$(mktemp -t mcp-sh)
trap 'rm -f "$_tmp" "$_tmp.h"; _mcp_close' EXIT

_http_post() { # BODY -> prints response body; response headers land in $_tmp.h
  local sess=() code
  [ -n "$MCP_SESSION" ] && sess=(-H "Mcp-Session-Id: $MCP_SESSION")
  _log "--> POST $MCP_URL $1"
  code=$(curl -sS --max-time "$MCP_TIMEOUT" "$MCP_URL" -o "$_tmp" -D "$_tmp.h" -w '%{http_code}' \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "MCP-Protocol-Version: $MCP_PROTOCOL" ${sess[@]+"${sess[@]}"} ${_hdr[@]+"${_hdr[@]}"} \
    -d "$1") || _die "curl failed talking to $MCP_URL"
  case "$code" in
    2*) ;;
    401|403) _die "HTTP $code from $MCP_URL — set MCP_AUTHORIZATION / MCP_HEADERS" ;;
    *) _die "HTTP $code from $MCP_URL: $(head -c 300 "$_tmp")" ;;
  esac
  cat "$_tmp"
}
_mcp_open() {
  local body msg
  body=$(_http_post "$(_init_msg)")
  MCP_SESSION=$(awk 'tolower($1)=="mcp-session-id:"{print $2}' "$_tmp.h" | tr -d '\r')
  msg=$(printf '%s' "$body" | _json_pick 1) || true
  [ -n "$msg" ] || _die "no initialize response from $MCP_URL: $(printf '%s' "$body" | head -c 300)"
  _log "<-- $msg"
  _http_post "$(_initialized_msg)" >/dev/null
}
_mcp_rpc() { # METHOD [PARAMS_JSON] -> prints the JSON-RPC response message (id 2)
  local body msg
  body=$(_http_post "$(_json_msg 2 "$1" "${2:-}")")
  msg=$(printf '%s' "$body" | _json_pick 2) || true
  [ -n "$msg" ] || _die "no response to $1: $(printf '%s' "$body" | head -c 300)"
  _log "<-- $msg"
  printf '%s\n' "$msg"
}
_mcp_close() {
  [ -n "${MCP_SESSION:-}" ] || return 0
  curl -sS --max-time 5 -X DELETE "$MCP_URL" -H "Mcp-Session-Id: $MCP_SESSION" ${_hdr[@]+"${_hdr[@]}"} -o /dev/null 2>/dev/null || true
  MCP_SESSION=""
}
'''

SH_SSE = r'''
# --------------------------------------------------------------------------- #
# Transport: legacy HTTP+SSE — a background `curl -N` holds the event stream;
# POSTs return 202 and the replies are read back from the stream's log file.
# --------------------------------------------------------------------------- #
_sselog=""; _ssepid=""; MCP_POST=""
trap '_mcp_close' EXIT

_sse_wait() { # PATTERN [MIN_COUNT] -> waits until `grep -c PATTERN` on the log exceeds MIN_COUNT
  local i=0 min="${2:-0}" ticks
  ticks=$(( ${MCP_TIMEOUT%.*} * 10 ))
  while [ "$(grep -c "$1" "$_sselog" 2>/dev/null || true)" -le "$min" ]; do
    i=$((i + 1)); [ "$i" -gt "$ticks" ] && return 1
    kill -0 "$_ssepid" 2>/dev/null || _die "SSE stream to $MCP_URL closed"
    sleep 0.1
  done
}
_sse_post() { # BODY -> expects 2xx (usually 202 with no body)
  local code
  _log "--> POST $MCP_POST $1"
  code=$(curl -sS --max-time "$MCP_TIMEOUT" "$MCP_POST" -o /dev/null -w '%{http_code}' \
    -H "Content-Type: application/json" ${_hdr[@]+"${_hdr[@]}"} -d "$1") || _die "curl failed talking to $MCP_POST"
  case "$code" in 2*) ;; *) _die "HTTP $code from POST $MCP_POST" ;; esac
}
_mcp_open() {
  local ep origin
  _sselog=$(mktemp -t mcp-sse)
  curl -sN --max-time 0 "$MCP_URL" -H "Accept: text/event-stream" ${_hdr[@]+"${_hdr[@]}"} > "$_sselog" 2>/dev/null &
  _ssepid=$!
  _sse_wait '^event: endpoint' || _die "no endpoint event from $MCP_URL (not a legacy SSE server?)"
  ep=$(awk '/^event: endpoint/{getline; sub(/^data: */,""); print; exit}' "$_sselog" | tr -d '\r')
  origin="${MCP_URL%%://*}://$(printf '%s' "${MCP_URL#*://}" | cut -d/ -f1)"
  case "$ep" in http*) MCP_POST="$ep" ;; /*) MCP_POST="$origin$ep" ;; *) MCP_POST="${MCP_URL%/*}/$ep" ;; esac
  _log "<-- endpoint $MCP_POST"
  _sse_post "$(_init_msg)"
  _sse_wait '^data:.*"id"' 0 || _die "no initialize response on the SSE stream"
  _sse_post "$(_initialized_msg)"
}
_mcp_rpc() { # METHOD [PARAMS_JSON] -> prints the JSON-RPC response message (id 2)
  local before msg
  before=$(grep -c '^data:.*"id"' "$_sselog" || true)
  _sse_post "$(_json_msg 2 "$1" "${2:-}")"
  _sse_wait '^data:.*"id"' "$before" || _die "no response to $1 within ${MCP_TIMEOUT}s"
  msg=$(_json_pick 2 < "$_sselog") || true
  [ -n "$msg" ] || _die "no response to $1 on the SSE stream"
  _log "<-- $msg"
  printf '%s\n' "$msg"
}
_mcp_close() {
  [ -n "${_ssepid:-}" ] && { kill "$_ssepid" 2>/dev/null || true; wait "$_ssepid" 2>/dev/null || true; }
  [ -n "${_sselog:-}" ] && rm -f "$_sselog"
  _ssepid=""; _sselog=""
}
'''

SH_STDIO = r'''
# --------------------------------------------------------------------------- #
# Transport: stdio — the whole conversation is piped into one server process
# --------------------------------------------------------------------------- #
_mcp_open() { :; }
_mcp_close() { :; }
_mcp_rpc() { # METHOD [PARAMS_JSON] -> prints the JSON-RPC response message (id 2)
  local req msg
  req=$(_json_msg 2 "$1" "${2:-}")
  _log "--> $req"
  msg=$(printf '%s\n' "$(_init_msg)" "$(_initialized_msg)" "$req" | "${MCP_CMD[@]}" 2>/dev/null | _json_pick 2) || true
  [ -n "$msg" ] || _die "no response to $1 from: ${MCP_CMD[*]}"
  _log "<-- $msg"
  printf '%s\n' "$msg"
}
'''

SH_RUN = r'''
# --------------------------------------------------------------------------- #
# Running one request and printing its outcome
# --------------------------------------------------------------------------- #
_OUT=result   # result (pretty JSON) | text | raw (whole JSON-RPC message)
_emit() { # MESSAGE
  local err
  err=$(printf '%s' "$1" | _json_error)
  [ -z "$err" ] || _die "server returned error $err"
  case "$_OUT" in
    text) printf '%s' "$1" | _json_text ;;
    raw)  printf '%s\n' "$1" ;;
    *)    printf '%s' "$1" | _json_result ;;
  esac
}
_run() { # METHOD [PARAMS_JSON]
  local msg
  _mcp_open
  msg=$(_mcp_rpc "$1" "${2:-}") || exit 1
  _emit "$msg"
  _mcp_close
}
_run_tool() { # NAME ARGS_JSON
  _run tools/call "$(printf '{"name":"%s","arguments":%s}' "$(_json_escape "$1")" "$2")"
}
_output_flag() { # sets _OUT when $1 is --text/--raw/--json; returns 1 otherwise
  case "$1" in --text) _OUT=text ;; --raw) _OUT=raw ;; --json) _OUT=result ;; *) return 1 ;; esac
}
'''

SH_MAIN = r'''
# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
_usage() {
  cat <<'USAGE'
@@USAGE@@
USAGE
}
_list() {
  cat <<'LIST'
@@LIST@@
LIST
}

_cmd="${1:-}"; [ $# -gt 0 ] && shift
case "$_cmd" in
  ""|-h|--help|help) _usage ;;
  list) _list ;;
  read)
    [ $# -ge 1 ] || _die "usage: read URI [--text|--raw]"
    _uri="$1"; shift
    while [ $# -gt 0 ]; do _output_flag "$1" || _die "unknown option $1"; shift; done
    _args=$(_json_args string uri "$_uri") || exit 1
    _run resources/read "$_args" ;;
  prompt)
    [ $# -ge 1 ] || _die "usage: prompt NAME [--arg key=value ...] [--text|--raw]"
    _name="$1"; shift; _kv=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --arg) [ -n "${2:-}" ] || _die "--arg needs key=value"; _kv+=(string "${2%%=*}" "${2#*=}"); shift 2 ;;
        *) _output_flag "$1" || _die "unknown option $1"; shift ;;
      esac
    done
    _args=$(_json_args ${_kv[@]+"${_kv[@]}"}) || exit 1
    _run prompts/get "$(printf '{"name":"%s","arguments":%s}' "$(_json_escape "$_name")" "$_args")" ;;
  rpc)
    [ $# -ge 1 ] || _die "usage: rpc METHOD [PARAMS_JSON] [--raw]"
    _method="$1"; _params=""; shift
    while [ $# -gt 0 ]; do
      _output_flag "$1" || { [ -z "$_params" ] && _params="$1" || _die "unexpected argument $1"; }
      shift
    done
    _run "$_method" "$_params" ;;
@@DISPATCH@@
  *) _die "unknown command '$_cmd' (try: $0 list)" ;;
esac
'''


# --------------------------------------------------------------------------- #
# Code generation
# --------------------------------------------------------------------------- #
SENSITIVE_HEADER = re.compile(r"authorization|token|secret|api[-_]?key|cookie", re.I)


def sq(s: str) -> str:
    return _shlex.quote(s)


def cli_flag(name: str) -> str:
    """messageType -> message-type, some_arg -> some-arg."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "arg"


def fn_ident(name: str) -> str:
    ident = re.sub(r"\W", "_", name)
    return ident if ident and not ident[0].isdigit() else "_" + ident


def first_line(text: Optional[str]) -> str:
    t = (text or "").strip()
    return t.splitlines()[0] if t else ""


def schema_kind(sub: Any) -> Tuple[str, Optional[list]]:
    """-> (string|number|bool|json, enum-values-or-None)"""
    if not isinstance(sub, dict):
        return "string", None
    typ = sub.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), None)
    enum = sub.get("enum") if all(isinstance(v, (str, int, float)) for v in (sub.get("enum") or [])) else None
    if typ in ("integer", "number"):
        return "number", enum
    if typ == "boolean":
        return "bool", None
    if typ in ("array", "object") or "properties" in sub:
        return "json", None
    if typ == "string" or typ is None and enum:
        return "string", enum
    return "json" if typ is None and any(k in sub for k in ("anyOf", "oneOf")) else "string", enum


def type_label(kind: str, enum: Optional[list]) -> str:
    if enum:
        return "{" + ",".join(str(v) for v in enum) + "}"
    return {"string": "STRING", "number": "NUMBER", "bool": "", "json": "JSON"}[kind]


RESERVED_COMMANDS = {"list", "read", "prompt", "rpc", "help"}


def command_name(tool_name: str) -> str:
    return tool_name + "-tool" if tool_name in RESERVED_COMMANDS else tool_name


def gen_tool(tool: dict, script: str) -> Tuple[List[str], str, List[str]]:
    """-> (function lines, dispatch case line, usage lines)"""
    name = command_name(tool["name"])
    fn = "_tool_" + fn_ident(name)
    schema = tool.get("inputSchema") or {}
    props: Dict[str, Any] = schema.get("properties") or {}
    required = [p for p in (schema.get("required") or []) if p in props]
    params = []
    for pname, sub in props.items():
        kind, enum = schema_kind(sub)
        params.append((pname, cli_flag(pname), kind, enum, pname in required,
                       first_line(sub.get("description") if isinstance(sub, dict) else "")))

    # --- help text for this tool
    help_lines = [f"{script} {name} " + " ".join(
        (f"--{flag}" if kind == "bool" else f"--{flag} {type_label(kind, enum)}")
        + ("" if req else "]") if False else
        (("" if req else "[") + (f"--{flag}" if kind == "bool" else f"--{flag} {type_label(kind, enum)}") + ("" if req else "]"))
        for _, flag, kind, enum, req, _ in params) + " [--text|--raw]"]
    desc = (tool.get("description") or "").strip()
    if desc:
        help_lines += ["  " + l for l in desc.splitlines()]
    if params:
        help_lines.append("  options:")
        for _, flag, kind, enum, req, pdesc in params:
            opt = f"--{flag}" + ("" if kind == "bool" else " " + type_label(kind, enum))
            if kind == "bool":
                opt += f" | --no-{flag}"
            help_lines.append(f"    {opt:<36} {'required' if req else 'optional'}" + (f" — {pdesc}" if pdesc else ""))
    help_lines.append("  --text prints only text content; --raw prints the whole JSON-RPC message")

    # --- the function
    L = [f"{fn}() {{"]
    L.append("  local kv=()" + "".join(f" _set_{fn_ident(p)}=0" for p, *_ in params))
    L.append('  while [ $# -gt 0 ]; do')
    L.append('    case "$1" in')
    for pname, flag, kind, enum, req, _ in params:
        setv = f"_set_{fn_ident(pname)}=1"
        if kind == "bool":
            L.append(f"      --{flag}) kv+=(bool {sq(pname)} true); {setv}; shift ;;")
            L.append(f"      --no-{flag}) kv+=(bool {sq(pname)} false); {setv}; shift ;;")
            continue
        L.append(f'      --{flag}) [ $# -ge 2 ] || _die "--{flag} needs a value"')
        if enum:
            L.append('        case "$2" in ' + "|".join(sq(str(v)) for v in enum)
                     + f') ;; *) _die "--{flag} must be one of: {", ".join(str(v) for v in enum)}" ;; esac')
        L.append(f'        kv+=({kind} {sq(pname)} "$2"); {setv}; shift 2 ;;')
    L.append(f'      -h|--help) _help_{fn_ident(name)}; return 0 ;;')
    L.append(f'      *) _output_flag "$1" || _die "unknown option $1 for {name} (try --help)"; shift ;;')
    L.append("    esac")
    L.append("  done")
    for pname, flag, kind, enum, req, _ in params:
        if req:
            L.append(f'  [ "$_set_{fn_ident(pname)}" = 1 ] || _die "{name}: --{flag} is required"')
    L.append('  local args; args=$(_json_args ${kv[@]+"${kv[@]}"}) || exit 1')
    L.append('  _run_tool ' + sq(tool["name"]) + ' "$args"')
    L.append("}")
    L.append(f"_help_{fn_ident(name)}() {{")
    L.append("  cat <<'HELP'")
    L += help_lines
    L.append("HELP")
    L.append("}")
    case_line = f"  {sq(name)}) {fn} \"$@\" ;;"
    return L, case_line, help_lines


def generate(info: dict, *, url: Optional[str], command: Optional[List[str]], transport: str,
             headers: Dict[str, str], timeout: float, protocol: str, script: str,
             generator_cmd: str) -> Tuple[str, List[str]]:
    notes: List[str] = []
    baked: List[str] = []
    for k, v in headers.items():
        if SENSITIVE_HEADER.search(k):
            env = "MCP_" + re.sub(r"\W", "_", k).upper()
            baked.append(f'[ -n "${{{env}:-}}" ] && _hdr+=(-H "{k}: ${env}") || echo "warning: header {k} needs env var {env}" >&2')
            notes.append(f"header {k!r} is not baked in — set {env}='{v.split(' ')[0]} ...' when running the script")
        else:
            baked.append(f"_hdr+=(-H {sq(f'{k}: {v}')})")

    srv = info["server"]
    tools, resources, templates, prompts = info["tools"], info["resources"], info["resourceTemplates"], info["prompts"]

    if command:
        connection = "\n".join([
            f"MCP_CMD_DEFAULT=({' '.join(sq(c) for c in command)})",
            '# MCP_COMMAND="cmd arg arg" overrides the server command',
            'if [ -n "${MCP_COMMAND:-}" ]; then eval "MCP_CMD=($MCP_COMMAND)"; else MCP_CMD=("${MCP_CMD_DEFAULT[@]}"); fi',
        ])
        conn_doc = f"stdio server: {' '.join(sq(c) for c in command)}  (override: MCP_COMMAND)"
    else:
        connection = f'MCP_URL="${{MCP_URL:-{url}}}"'
        conn_doc = f"{url}  ({'legacy HTTP+SSE' if transport == 'sse' else 'Streamable HTTP'}; override: MCP_URL)"

    tool_fns: List[str] = []
    dispatch: List[str] = []
    usage_tools: List[str] = []
    for t in tools:
        fn_lines, case_line, help_lines = gen_tool(t, script)
        tool_fns += fn_lines + [""]
        dispatch.append(case_line)
        usage_tools.append(f"  {command_name(t['name']):<34} {first_line(t.get('description'))}")

    usage = [f"{script} — one-shot CLI for MCP server {srv.get('name', '?')} {srv.get('version', '')}".rstrip(), "",
             f"usage: {script} COMMAND [options]",
             "  list                         everything the server exposes",
             "  TOOL [--flag value ...]      call a tool (TOOL --help shows its flags)",
             "  read URI                     read a resource",
             "  prompt NAME [--arg k=v ...]  get a prompt",
             "  rpc METHOD [PARAMS_JSON]     any JSON-RPC request",
             "output: --json (default, the result) | --text (text content only) | --raw (whole message)",
             "", "tools:"] + usage_tools + [
             "", f"connection: {conn_doc}",
             "env: MCP_HEADERS='Name: v; Other: w'  MCP_TIMEOUT=secs  MCP_VERBOSE=1 (log JSON-RPC)"]
    for line in baked:
        m = re.search(r"needs env var (MCP_\w+)", line)
        if m:
            usage.append(f"     {m.group(1)}='...' (required credential header)")

    listing = [f"server: {srv.get('name', '?')} {srv.get('version', '')}  protocol {protocol}  transport {transport}".rstrip(), "", f"tools ({len(tools)}):"]
    for t in tools:
        schema = t.get("inputSchema") or {}
        props = schema.get("properties") or {}
        req = set(schema.get("required") or [])
        flags = " ".join(("" if p in req else "[") + "--" + cli_flag(p) + ("" if p in req else "]") for p in props)
        listing.append(f"  {command_name(t['name'])} {flags}".rstrip())
        if first_line(t.get("description")):
            listing.append(f"      {first_line(t.get('description'))}")
    if resources:
        listing += ["", f"resources ({len(resources)}):  -> {script} read URI"]
        listing += [f"  {r['uri']:<52} {r.get('name', '')}".rstrip() for r in resources]
    if templates:
        listing += ["", f"resource templates ({len(templates)}):"]
        listing += [f"  {r['uriTemplate']:<52} {r.get('name', '')}".rstrip() for r in templates]
    if prompts:
        listing += ["", f"prompts ({len(prompts)}):  -> {script} prompt NAME --arg key=value"]
        for p in prompts:
            args = " ".join(("" if a.get("required") else "[") + f"--arg {a['name']}=..." + ("" if a.get("required") else "]")
                            for a in p.get("arguments") or [])
            listing.append(f"  {p['name']} {args}".rstrip())
            if first_line(p.get("description")):
                listing.append(f"      {first_line(p.get('description'))}")

    def heredoc_safe(lines: List[str]) -> str:  # a quoted heredoc is literal; only its terminator matters
        return "\n".join(l for l in lines if l.strip() not in ("USAGE", "LIST", "HELP"))

    header = [
        "#!/usr/bin/env bash",
        f"# {script} — one-shot CLI for the MCP server {srv.get('name', '?')} {srv.get('version', '')}".rstrip(),
        "#",
        f"# Generated {_dt.date.today().isoformat()} by mcp_gen_cli.py from:",
        f"#   {generator_cmd}",
        "#",
        "# Every invocation is one complete MCP exchange (initialize → call → close);",
        "# nothing is cached between runs. Needs bash 3.2+, curl, and jq or python3.",
        f"#   {script} list",
        f"#   {script} TOOL --help",
        f"#   {script} read URI --text",
        "#",
        f"# connection: {conn_doc}",
    ]
    if info.get("instructions"):
        header += ["#", "# Server instructions:"] + ["#   " + l for l in str(info["instructions"]).strip().splitlines()]

    body = SH_COMMON \
        .replace("@@CONNECTION@@", connection) \
        .replace("@@TIMEOUT@@", str(int(timeout) if float(timeout).is_integer() else timeout)) \
        .replace("@@PROTOCOL@@", protocol) \
        .replace("@@BAKED_HEADERS@@", "\n".join(baked) if baked else "# (no baked-in headers)") \
        .replace("@@SCRIPT_NAME@@", script)
    body += {"http": SH_HTTP, "sse": SH_SSE, "stdio": SH_STDIO}[transport]
    body += SH_RUN
    body += "\n# --------------------------------------------------------------------------- #\n"
    body += f"# Tools ({len(tools)})\n"
    body += "# --------------------------------------------------------------------------- #\n"
    body += "\n".join(tool_fns)
    body += SH_MAIN \
        .replace("@@USAGE@@", heredoc_safe(usage)) \
        .replace("@@LIST@@", heredoc_safe(listing)) \
        .replace("@@DISPATCH@@", "\n".join(dispatch))
    return "\n".join(header) + "\n" + body.rstrip() + "\n", notes


# --------------------------------------------------------------------------- #
# Python flavour
# --------------------------------------------------------------------------- #
PY_TEMPLATE = r"""
# --------------------------------------------------------------------------- #
# Connection (env vars override the baked-in defaults)
# --------------------------------------------------------------------------- #
DEFAULT_URL = @@URL@@
DEFAULT_COMMAND = @@COMMAND@@
DEFAULT_TRANSPORT = @@TRANSPORT@@
DEFAULT_HEADERS = @@HEADERS@@
DEFAULT_TIMEOUT = @@TIMEOUT@@
SCRIPT = @@SCRIPT@@
SERVER = @@SERVER@@
RESERVED_COMMANDS = {"list", "read", "prompt", "rpc"}


def _headers() -> Dict[str, str]:
    out = {}
    for k, v in DEFAULT_HEADERS.items():
        if v.startswith("$"):
            v = os.environ.get(v[1:], "")
            if not v:
                print("warning: header %s needs env var %s" % (k, DEFAULT_HEADERS[k][1:]), file=sys.stderr)
                continue
        out[k] = v
    for item in os.environ.get("MCP_HEADERS", "").split(";"):
        name, sep, value = item.partition(":")
        if sep and name.strip():
            out[name.strip()] = value.strip()
    return out


def _connect(url: Optional[str] = None, command: Optional[str] = None, verbose: bool = False) -> McpClient:
    env_cmd = command or os.environ.get("MCP_COMMAND")
    cmd = _shlex.split(env_cmd) if env_cmd else DEFAULT_COMMAND
    url = url or os.environ.get("MCP_URL") or DEFAULT_URL
    transport = os.environ.get("MCP_TRANSPORT") or (DEFAULT_TRANSPORT if url == DEFAULT_URL else "auto")
    kw = dict(headers=_headers(), timeout=float(os.environ.get("MCP_TIMEOUT", DEFAULT_TIMEOUT)),
              transport=transport, verbose=verbose or bool(os.environ.get("MCP_VERBOSE")), client_name=SCRIPT)
    return McpClient(command=cmd, **kw) if cmd else McpClient(url=url, **kw)


# --------------------------------------------------------------------------- #
# What the server exposes (captured at generation time)
# --------------------------------------------------------------------------- #
TOOLS: List[Dict[str, Any]] = @@TOOLS@@

RESOURCES: List[Dict[str, Any]] = @@RESOURCES@@

RESOURCE_TEMPLATES: List[Dict[str, Any]] = @@TEMPLATES@@

PROMPTS: List[Dict[str, Any]] = @@PROMPTS@@


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _flag(name: str) -> str:
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "arg"


def _command_name(tool_name: str) -> str:
    return tool_name + "-tool" if tool_name in RESERVED_COMMANDS else tool_name


def _add_schema_args(parser: argparse.ArgumentParser, schema: Dict[str, Any]) -> Dict[str, str]:
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    mapping = {}
    for name, sub in props.items():
        sub = sub if isinstance(sub, dict) else {}
        flag, dest = "--" + _flag(name), "arg_" + _flag(name).replace("-", "_")
        mapping[dest] = name
        typ = sub.get("type")
        if isinstance(typ, list):
            typ = next((t for t in typ if t != "null"), None)
        first = (sub.get("description") or "").strip().splitlines()[:1]
        help_text = (first[0] if first else "") + (" (required)" if name in required else "")
        kw: Dict[str, Any] = {"dest": dest, "help": help_text, "required": name in required}
        if sub.get("enum"):
            kw["choices"] = sub["enum"] if typ in ("integer", "number") else [str(v) for v in sub["enum"]]
            if typ in ("integer", "number"):
                kw["type"] = int if typ == "integer" else float
        elif typ == "integer":
            kw["type"] = int
        elif typ == "number":
            kw["type"] = float
        elif typ == "boolean":
            grp = parser.add_mutually_exclusive_group(required=name in required)
            grp.add_argument(flag, dest=dest, action="store_true", default=None, help=help_text)
            grp.add_argument("--no-" + _flag(name), dest=dest, action="store_false", help=argparse.SUPPRESS)
            continue
        elif typ in ("array", "object"):
            kw["type"] = json.loads
            kw["metavar"] = "JSON"
        parser.add_argument(flag, **kw)
    return mapping


def _add_output_flags(parser: argparse.ArgumentParser) -> None:
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--text", dest="out", action="store_const", const="text", help="print only text content")
    g.add_argument("--raw", dest="out", action="store_const", const="raw", help="print the whole JSON-RPC message")
    g.add_argument("--json", dest="out", action="store_const", const="result", help="pretty JSON result (default)")


def _emit(result: Any, out: Optional[str]) -> None:
    if out == "text":
        print(result_text(result))
    elif out == "raw":
        print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": result}, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def _list() -> None:
    print("server: %s %s  transport %s" % (SERVER.get("name", "?"), SERVER.get("version", ""), DEFAULT_TRANSPORT))
    print("\ntools (%d):" % len(TOOLS))
    for t in TOOLS:
        schema = t.get("inputSchema") or {}
        req = set(schema.get("required") or [])
        flags = " ".join(("" if p in req else "[") + "--" + _flag(p) + ("" if p in req else "]")
                         for p in schema.get("properties") or {})
        print(("  %s %s" % (_command_name(t["name"]), flags)).rstrip())
        first = (t.get("description") or "").strip().splitlines()[:1]
        if first:
            print("      " + first[0])
    if RESOURCES:
        print("\nresources (%d):  -> %s read URI" % (len(RESOURCES), SCRIPT))
        for r in RESOURCES:
            print(("  %-52s %s" % (r["uri"], r.get("name", ""))).rstrip())
    if RESOURCE_TEMPLATES:
        print("\nresource templates (%d):" % len(RESOURCE_TEMPLATES))
        for r in RESOURCE_TEMPLATES:
            print(("  %-52s %s" % (r["uriTemplate"], r.get("name", ""))).rstrip())
    if PROMPTS:
        print("\nprompts (%d):  -> %s prompt NAME --arg key=value" % (len(PROMPTS), SCRIPT))
        for p in PROMPTS:
            args = " ".join(("" if a.get("required") else "[") + "--arg %s=..." % a["name"] + ("" if a.get("required") else "]")
                            for a in p.get("arguments") or [])
            print(("  %s %s" % (p["name"], args)).rstrip())
            first = (p.get("description") or "").strip().splitlines()[:1]
            if first:
                print("      " + first[0])


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog=SCRIPT, description=__doc__.strip().splitlines()[0],
                                epilog="every invocation is one complete MCP exchange (initialize -> call -> close)")
    p.add_argument("--url", help="override the server URL (or set MCP_URL)")
    p.add_argument("--command", help="override the stdio command (or set MCP_COMMAND)")
    p.add_argument("-v", "--verbose", action="store_true", help="log JSON-RPC traffic to stderr")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    sub.add_parser("list", help="everything the server exposes")
    sp = sub.add_parser("read", help="read a resource"); sp.add_argument("uri"); _add_output_flags(sp)
    sp = sub.add_parser("prompt", help="get a prompt"); sp.add_argument("name")
    sp.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE"); _add_output_flags(sp)
    sp = sub.add_parser("rpc", help="any JSON-RPC request"); sp.add_argument("method")
    sp.add_argument("params", nargs="?", default=None, help="JSON object"); _add_output_flags(sp)
    mappings: Dict[str, Dict[str, str]] = {}
    for t in TOOLS:
        first = (t.get("description") or "").strip().splitlines()[:1]
        tp = sub.add_parser(_command_name(t["name"]), help=first[0] if first else None, description=t.get("description"))
        mappings[_command_name(t["name"])] = _add_schema_args(tp, t.get("inputSchema") or {})
        _add_output_flags(tp)

    a = p.parse_args(argv)
    if a.cmd == "list":
        _list()
        return 0
    try:
        with _connect(a.url, a.command, a.verbose) as c:
            if a.cmd == "read":
                result = c.read_resource(a.uri)
            elif a.cmd == "prompt":
                result = c.get_prompt(a.name, dict(item.split("=", 1) for item in a.arg))
            elif a.cmd == "rpc":
                result = c.rpc(a.method, json.loads(a.params) if a.params else None)
            else:
                tool = next(t for t in TOOLS if _command_name(t["name"]) == a.cmd)
                args = {json_name: getattr(a, dest) for dest, json_name in mappings[a.cmd].items()
                        if getattr(a, dest, None) is not None}
                result = c.call_tool(tool["name"], args)
        _emit(result, a.out)
        return 0
    except McpError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except (json.JSONDecodeError, ValueError) as e:
        print("error: bad JSON argument: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
"""


def py_literal(obj: Any) -> str:
    return pprint.pformat(obj, width=100, sort_dicts=False)


def runtime_source() -> str:
    with open(__file__, encoding="utf-8") as f:
        src = f.read()
    start = src.index("# ==== MCP RUNTIME BEGIN")
    end = src.index("# ==== MCP RUNTIME END ====") + len("# ==== MCP RUNTIME END ====")
    return src[start:end]


def generate_python(info: dict, *, url: Optional[str], command: Optional[List[str]], transport: str,
                    headers: Dict[str, str], timeout: float, protocol: str, script: str,
                    generator_cmd: str) -> Tuple[str, List[str]]:
    notes: List[str] = []
    baked: Dict[str, str] = {}
    for k, v in headers.items():
        if SENSITIVE_HEADER.search(k):
            env = "MCP_" + re.sub(r"\W", "_", k).upper()
            baked[k] = "$" + env
            notes.append(f"header {k!r} is not baked in — set {env}='{v.split(' ')[0]} ...' when running the script")
        else:
            baked[k] = v
    srv = info["server"]
    if command:
        conn_doc = f"stdio server: {' '.join(sq(c) for c in command)}  (override: MCP_COMMAND)"
    else:
        conn_doc = f"{url}  ({'legacy HTTP+SSE' if transport == 'sse' else 'Streamable HTTP'}; override: MCP_URL)"
    doc = [
        f"{script} — one-shot CLI for the MCP server {srv.get('name', '?')} {srv.get('version', '')}".rstrip(),
        "",
        f"Generated {_dt.date.today().isoformat()} by mcp_gen_cli.py from:",
        f"    {generator_cmd}",
        "",
        "Every invocation is one complete MCP exchange (initialize -> call -> close);",
        "nothing is cached between runs. Needs only python3 (standard library).",
        f"    python {script} list",
        f"    python {script} TOOL --help",
        f"    python {script} read URI --text",
        f"    python {script} prompt NAME --arg key=value",
        f"    python {script} rpc METHOD '{{json params}}'",
        "",
        f"connection: {conn_doc}",
        "env: MCP_HEADERS='Name: v; Other: w'  MCP_TIMEOUT=secs  MCP_VERBOSE=1  MCP_TRANSPORT=http|sse",
    ]
    doc += [f"     {v[1:]}='...' (required credential header {k})" for k, v in baked.items() if v.startswith("$")]
    if info.get("instructions"):
        safe = str(info["instructions"]).strip().replace('"' * 3, "'" * 3)
        doc += ["", "Server instructions:"] + ["    " + l for l in safe.splitlines()]
    L = ["#!/usr/bin/env python3", '"' * 3] + doc + ['"' * 3, "",
         "from __future__ import annotations", "",
         "import argparse", "import json", "import os", "import re", "import sys",
         "from typing import Any, Dict, List, Optional", "",
         runtime_source(), ""]
    body = PY_TEMPLATE \
        .replace("@@URL@@", repr(url)) \
        .replace("@@COMMAND@@", repr(command)) \
        .replace("@@TRANSPORT@@", repr(transport)) \
        .replace("@@HEADERS@@", repr(baked)) \
        .replace("@@TIMEOUT@@", repr(timeout)) \
        .replace("@@SCRIPT@@", repr(script)) \
        .replace("@@SERVER@@", py_literal(srv)) \
        .replace("@@TOOLS@@", py_literal([{k: t.get(k) for k in ("name", "description", "inputSchema")} for t in info["tools"]])) \
        .replace("@@RESOURCES@@", py_literal([{k: r[k] for k in ("uri", "name", "description", "mimeType") if r.get(k) is not None} for r in info["resources"]])) \
        .replace("@@TEMPLATES@@", py_literal([{k: r[k] for k in ("uriTemplate", "name", "description", "mimeType") if r.get(k) is not None} for r in info["resourceTemplates"]])) \
        .replace("@@PROMPTS@@", py_literal([{k: p[k] for k in ("name", "description", "arguments") if p.get(k) is not None} for p in info["prompts"]]))
    return "\n".join(L) + body.rstrip() + "\n", notes


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


def default_script_name(server_name: str, url: Optional[str], command: Optional[List[str]],
                        lang: str = "bash") -> str:
    base = server_name.split("/")[-1] if server_name else ""
    if not base and url:
        base = re.sub(r"^www\.", "", (re.match(r"https?://([^/:]+)", url) or [None, ""])[1])
    if not base and command:
        base = os.path.basename(command[-1])
    base = re.sub(r"\W+", "_", base).strip("_").lower() or "mcp_server"
    return base + ("_mcp.py" if lang == "python" else "_mcp.sh")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="mcp_gen_cli.py",
        description="Generate a standalone bash script exposing every MCP tool as a one-shot subcommand.",
        epilog="stdio form: mcp_gen_cli.py --stdio -- CMD [ARGS...]")
    p.add_argument("url", nargs="?", help="HTTP endpoint, e.g. http://localhost:3001/mcp")
    p.add_argument("--stdio", action="store_true", help="launch a stdio server; command follows '--'")
    p.add_argument("--lang", choices=["bash", "python"], default="bash", help="flavour to generate")
    p.add_argument("-o", "--output", help="output file (default: <server-name>_mcp.sh or .py)")
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
    generator_cmd = "mcp_gen_cli.py " + " ".join(sq(a) for a in argv + (["--"] + cmd if cmd else []))
    if args.bearer:
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
            c = McpClient(command=cmd, verbose=args.verbose, client_name="mcp_gen_cli")
        else:
            c = McpClient(url=args.url, headers=headers, timeout=args.timeout,
                          transport=args.transport, verbose=args.verbose, client_name="mcp_gen_cli")
        with c:
            info = {"server": c.server_info, "capabilities": c.capabilities, "instructions": c.instructions,
                    "tools": c.list_tools(), "resources": c.list_resources(),
                    "resourceTemplates": c.list_resource_templates(), "prompts": c.list_prompts()}
            transport, protocol = c.transport, c._t.protocol
    except McpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    output = args.output or default_script_name(info["server"].get("name", ""), args.url, cmd or None, args.lang)
    script = os.path.basename(output)
    gen = generate_python if args.lang == "python" else generate
    source, notes = gen(info, url=None if args.stdio else args.url, command=cmd if args.stdio else None,
                        transport=transport, headers=headers, timeout=args.timeout, protocol=protocol,
                        script=script, generator_cmd=generator_cmd)
    if args.lang == "python":
        try:
            code = compile(source, output, "exec")
            exec(code, {"__name__": "_mcp_gen_selfcheck", "__file__": output})  # import-time only
        except Exception as e:
            with open(output + ".broken", "w", encoding="utf-8") as f:
                f.write(source)
            print(f"error: generated script fails to load ({type(e).__name__}: {e}); "
                  f"written to {output}.broken", file=sys.stderr)
            return 1

    with open(output, "w", encoding="utf-8") as f:
        f.write(source)
    try:
        os.chmod(output, os.stat(output).st_mode | 0o111)
    except OSError:
        pass

    # Self-checks: bash must parse it; shellcheck is advisory if installed.
    bash = shutil.which("bash") if args.lang == "bash" else None
    if bash:
        chk = subprocess.run([bash, "-n", output], capture_output=True, text=True)
        if chk.returncode != 0:
            print(f"error: generated script fails `bash -n`:\n{chk.stderr}", file=sys.stderr)
            return 1
    if args.lang == "bash" and shutil.which("shellcheck"):
        chk = subprocess.run(["shellcheck", "-S", "warning", output], capture_output=True, text=True)
        if chk.returncode != 0:
            notes.append("shellcheck reported warnings:\n" + chk.stdout.strip())

    srv = info["server"]
    print(f"wrote {output}: {srv.get('name', '?')} {srv.get('version', '')} via {transport} — "
          f"{len(info['tools'])} tools, {len(info['resources'])} resources, "
          f"{len(info['resourceTemplates'])} templates, {len(info['prompts'])} prompts", file=sys.stderr)
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
