# mcp-curl

Introspect an MCP server and print a shell-ready cheat sheet of every request
it supports, as curl commands with example arguments generated from each
tool's JSON schema. Can also execute calls directly (`--call`, `--rpc`) for
quick tests without curl.

Python 3.9+, no dependencies.

## Usage

```sh
# Streamable HTTP server → curl commands
./mcp_curl.py http://localhost:3001/mcp
./mcp_curl.py https://mcp.example.com/mcp --bearer "$TOKEN"
./mcp_curl.py http://localhost:3001/mcp --all-args        # include optional args
./mcp_curl.py http://localhost:3001/mcp --json > mcp.json # machine-readable

# Legacy HTTP+SSE server (auto-detected from the /sse path or a 404/405 on POST)
./mcp_curl.py http://localhost:3001/sse

# stdio server → printf | command examples (curl can't talk to a pipe)
./mcp_curl.py --stdio -- npx -y @modelcontextprotocol/server-everything stdio

# Execute directly, no curl — works on every transport
./mcp_curl.py http://localhost:3001/sse --call echo --args '{"message":"hi"}'
./mcp_curl.py http://localhost:3001/mcp --rpc resources/read --params '{"uri":"demo://x"}'

# See the raw JSON-RPC exchange
./mcp_curl.py http://localhost:3001/mcp -v
```

| Flag | Meaning |
|------|---------|
| `-H "Name: value"` | extra header, used for discovery *and* baked into every generated curl (repeatable) |
| `--bearer TOKEN` | shortcut for `-H "Authorization: Bearer TOKEN"` |
| `--transport auto\|http\|sse` | force Streamable HTTP or legacy SSE instead of auto-detecting |
| `--call TOOL [--args JSON]` | run `tools/call` and print the result (skips the cheat sheet) |
| `--rpc METHOD [--params JSON]` | send any JSON-RPC request and print the result |
| `--all-args` | put optional tool/prompt args in the examples (default: required only; optional ones are listed in comments) |
| `--json` | emit JSON instead of shell text (`requests[]` each carry `method`, `params`, `command`) |
| `--timeout N` | network timeout, seconds (default 30) |
| `--protocol-version V` | MCP version to negotiate (default `2025-06-18`) |

Output is valid shell: `./mcp_curl.py URL > mcp.sh` then `source mcp.sh`
runs the handshake (which sets `$MCP_URL` / `$MCP_SESSION`) — after that each
printed curl can be pasted on its own.

## What it does

1. `initialize` → `notifications/initialized` handshake, capturing the
   `Mcp-Session-Id` if the server issues one.
2. `tools/list`, `resources/list`, `resources/templates/list`, `prompts/list`
   (following `nextCursor` pagination).
3. Prints, per item, a `tools/call` / `resources/read` / `prompts/get` command
   with placeholder args: `"<name>"` for strings, `0`/`false` for
   numbers/booleans, first `enum` value, `default`/`examples` when present.

Responses may come back as plain JSON or as `text/event-stream`; the generated
curls set `Accept` for both, so what you see depends on the server.

## Transports

| Transport | Detected by | Generated output |
|-----------|-------------|------------------|
| Streamable HTTP (2025-03-26+) | POST accepted | plain `curl` commands; handshake captures `Mcp-Session-Id` into `$MCP_SESSION` |
| Legacy HTTP+SSE (2024-11-05) | URL ends in `/sse`, or POST gets 404/405 | background `curl -N` writing the stream to a log, plus an `mcp_post` shell function that POSTs and prints the reply |
| stdio | `--stdio -- CMD` | `printf handshake+request \| CMD` |

Legacy SSE needs the two-connection dance because the POST only returns
`202 Accepted`; the JSON-RPC response arrives on the GET stream. `mcp_post`
handles that: it notes how many replies are in the log, POSTs, waits for a new
`data:` line carrying an `"id"`, and prints everything new. Kill the background
curl when done (the hint is at the end of the handshake block).

## Quick self-test

```sh
PORT=3001 npx -y @modelcontextprotocol/server-everything streamableHttp &
./mcp_curl.py http://localhost:3001/mcp > mcp.sh && source mcp.sh

PORT=3002 npx -y @modelcontextprotocol/server-everything sse &
./mcp_curl.py http://localhost:3002/sse --call echo --args '{"message":"hi"}'
```
