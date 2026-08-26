# mcp-gen-client

Point it at an MCP server and it writes a standalone Python module that
exposes every tool as a typed function, plus resources, prompts, and a CLI —
so anyone can use the server without knowing MCP.

Python 3.9+, no dependencies (neither the generator nor what it generates).

## Usage

```sh
./mcp_gen_client.py http://localhost:3001/mcp                # → everything_mcp.py
./mcp_gen_client.py http://localhost:3001/sse -o client.py   # legacy SSE, auto-detected
./mcp_gen_client.py https://mcp.example.com/mcp --bearer "$TOKEN"
./mcp_gen_client.py --stdio -- npx -y @modelcontextprotocol/server-everything stdio
```

| Flag | Meaning |
|------|---------|
| `-o FILE` | output path (default `<server-name>_mcp.py`) |
| `-H "Name: value"` / `--bearer TOKEN` | headers for discovery, baked into the client — **except credentials** (see below) |
| `--transport auto\|http\|sse` | force Streamable HTTP or legacy SSE |
| `--timeout N`, `--protocol-version V`, `-v` | as in mcp-curl |

The generator compiles and import-loads the result before writing it, so a
generated file that reaches disk is known to load.

## What the generated file gives you

```python
import everything_mcp as mcp

mcp.get_sum(a=2, b=3)                                   # -> raw tools/call result
mcp.result_text(mcp.get_annotated_message("success", includeImage=False))
mcp.read_resource(mcp.RESOURCES[0]["uri"])
mcp.prompt_args_prompt(city="Paris")
mcp.call_tool("some-tool-added-later", {"x": 1})        # escape hatch by name
mcp.rpc("ping")                                         # any JSON-RPC method
mcp.close()                                             # optional; runs at exit anyway
```

- One function per tool, named from the tool (`get-sum` → `get_sum`). Required
  args are positional, optional ones keyword-only defaulting to `None` (and
  omitted from the request). Type hints come from the schema, including
  `Literal[...]` for enums. Docstrings carry the descriptions.
- `TOOLS`, `RESOURCES`, `RESOURCE_TEMPLATES`, `PROMPTS` registries for
  programmatic use.
- Connection is lazy (importing never touches the network) and shared; the
  same `McpClient` class is available if you want several connections.

```sh
python everything_mcp.py list                                # tools, resources, prompts
python everything_mcp.py schema get-sum                      # input schema
python everything_mcp.py call get-sum --a 2 --b 3 --text     # typed flags from the schema
python everything_mcp.py call get-annotated-message --message-type debug --include-image
python everything_mcp.py read demo://resource/1 --text
python everything_mcp.py prompt args-prompt --arg city=Rome
python everything_mcp.py rpc ping
```

CLI flags are derived from parameter names (`messageType` → `--message-type`),
typed (`int`/`float`/JSON for arrays and objects), enums become `choices`,
booleans become `--flag` / `--no-flag`, required args are enforced by argparse.

## Connection settings in the generated file

Baked in: URL (or stdio command), transport, non-sensitive headers, timeout.
Overridable at run time:

| Env var | Effect |
|---------|--------|
| `MCP_URL` / `MCP_COMMAND` | point the client elsewhere (transport re-detected unless `MCP_TRANSPORT` is set) |
| `MCP_HEADERS` | extra headers, `'Name: v; Other: w'` |
| `MCP_<HEADER_NAME>` | value for a credential header, e.g. `MCP_AUTHORIZATION='Bearer …'` |
| `MCP_TIMEOUT`, `MCP_VERBOSE` | timeout seconds; log JSON-RPC traffic |

**Credentials are never written to the file.** Any header whose name looks
like `Authorization`, `*token*`, `*key*`, `*secret*` or `Cookie` is replaced
by an env-var lookup, and the generator prints which variable to set. The
generation command recorded in the file's docstring has `--bearer` redacted.

## Relationship to mcp-curl

Same embedded transport code (Streamable HTTP, legacy HTTP+SSE, stdio) as
`../mcp-curl/mcp_curl.py`, kept separate on purpose so each script stays
self-contained. If you fix a transport bug in one, port it to the other; the
runtime lives between the `MCP RUNTIME BEGIN/END` markers here.
