# mcp-gen-cli

Point it at an MCP server and it writes a standalone script in which **every
tool is a subcommand and every invocation is one complete request**: open a
session, initialize, make the one call, print the result, close. Nothing is
cached between runs, so the output drops straight into cron jobs, CI steps,
other scripts, or the hands of someone who just wants
`./server.sh some-tool --arg value`.

Two flavours with the same command surface:

| `--lang` | Needs | Why pick it |
|----------|-------|-------------|
| `bash` (default) | bash 3.2+, curl, jq (python3 as JSON fallback) | the curl calls are right there in the file — good for seeing exactly what goes over the wire |
| `python` | python3 only (stdlib) | no curl/jq, works on Windows, embeds the MCP runtime |

Both support Streamable HTTP, legacy HTTP+SSE (auto-detected; the bash one
holds the stream open with a background `curl -N`) and stdio servers.

## Usage

```sh
./mcp_gen_cli.py http://localhost:3001/mcp                       # → everything_mcp.sh
./mcp_gen_cli.py http://localhost:3001/mcp --lang python         # → everything_mcp.py
./mcp_gen_cli.py http://localhost:3001/sse -o srv.sh             # legacy SSE
./mcp_gen_cli.py https://mcp.example.com/mcp --bearer "$TOKEN"
./mcp_gen_cli.py --stdio -- npx -y @modelcontextprotocol/server-everything stdio
```

| Flag | Meaning |
|------|---------|
| `--lang bash\|python` | flavour (default bash) |
| `-o FILE` | output path (default `<server-name>_mcp.sh` / `.py`) |
| `-H "Name: value"` / `--bearer TOKEN` | headers for discovery, baked into the script — **except credentials** (see below) |
| `--transport auto\|http\|sse` | force Streamable HTTP or legacy SSE |
| `--timeout N`, `--protocol-version V`, `-v` | as in mcp-curl |

Self-checks before the file is kept: `bash -n` (and shellcheck if installed)
for the bash flavour; compile + import-load for the Python one.

## What the generated script does

```sh
./everything_mcp.sh list                                      # tools, resources, prompts
./everything_mcp.sh get-sum --help                            # flags for one tool
./everything_mcp.sh get-sum --a 1 --b 2 --text                # call it, print text content
./everything_mcp.sh get-annotated-message --message-type debug --include-image
./everything_mcp.sh read demo://resource/1 --text
./everything_mcp.sh prompt args-prompt --arg city=Rome
./everything_mcp.sh rpc ping
```

- Flags come from each tool's schema: `messageType` → `--message-type`;
  numbers are validated; enums are checked against their values; booleans are
  `--flag` / `--no-flag`; arrays/objects take JSON; required flags are enforced.
- Output: `--json` (default, the pretty result), `--text` (text content only),
  `--raw` (whole JSON-RPC message). Server errors → message on stderr, exit 1.
- A tool whose name collides with `list`/`read`/`prompt`/`rpc` is exposed as
  `<name>-tool`.

Run-time overrides: `MCP_URL` / `MCP_COMMAND` (transport re-detected in the
Python flavour, or set `MCP_TRANSPORT`), `MCP_HEADERS='Name: v; Other: w'`,
`MCP_TIMEOUT`, `MCP_VERBOSE=1` to log the JSON-RPC traffic.

**Credentials are never written to the file.** Headers named like
`Authorization`, `*token*`, `*key*`, `*secret*` or `Cookie` become
`$MCP_<NAME>` env lookups (e.g. `MCP_AUTHORIZATION='Bearer …'`), the generator
tells you which variable to set, and `--bearer` is redacted from the recorded
generation command.

## Relationship to the other MCP scripts

- `../mcp-curl` prints curl commands for a human to paste.
- `../mcp-gen-client` generates a Python *library* (typed function per tool,
  shared lazy session) that also has a CLI.
- This one generates a *one-shot CLI*: no library surface, no shared state.

All three embed the same transport code (marker-delimited `MCP RUNTIME
BEGIN/END`); if you fix a transport bug in one, port it to the others.
