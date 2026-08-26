# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this folder is

A collection of standalone scripts for work and personal projects. It is a
grab-bag, not an application: there is no shared build, test runner, or
package manifest at the root. Each script (or small group of related scripts)
is self-contained and documents its own usage.

This file describes the conventions to follow as scripts
are added. Update it (especially the index below) when adding a script or
introducing a language/toolchain for the first time.

## Conventions

- **One directory per script or tool** (`<name>/`), even for single-file
  scripts, so each can carry its own README/usage notes, config, or deps
  without polluting the root. Truly trivial one-offs may live in `bin/`.
- **Usage header at the top of every script**: what it does, required
  args/env vars, and an example invocation. Scripts should print usage and
  exit non-zero when called with `-h`/`--help` or missing required args.
- **Dependencies stay local to the script's directory** (`package.json`,
  `requirements.txt`/`pyproject.toml`, etc.) — never at the root. Prefer
  zero-dependency scripts where the standard library suffices.
- **Secrets come from env vars or a git-ignored `.env`** in the script's
  directory, never hard-coded. Add an `.env.example` when a script needs one.
- **Work vs personal**: if a script is work-specific, say so in its header;
  keep employer-specific hostnames, account IDs, and internal URLs out of
  anything that may be shared.
- **Shell scripts**: `#!/usr/bin/env bash`, `set -euo pipefail`, pass
  `shellcheck`. Make them executable (`chmod +x`).
- **Python**: target the system `python3`; use `uv run` / a per-script venv
  if third-party packages are needed.
- **Node/TypeScript**: Node >= 22; use `npx tsx` for TS scripts rather than a
  build step.

## Running and checking

Because there is no root toolchain, run and check per script:

| Language | Run | Lint / check |
|----------|-----|--------------|
| Bash     | `./<name>/<script>.sh` | `shellcheck <name>/*.sh` |
| Python   | `python3 <name>/<script>.py` | `python3 -m py_compile ...`; `ruff check` if configured |
| Node/TS  | `node <name>/<script>.js` / `npx tsx <name>/<script>.ts` | per-directory `npm run lint` if present |

Add tests only where a script has real logic worth protecting; put them next
to the script (`<name>/test_*.py`, `<name>/*.test.ts`) and note the command
in the script's README.

## Script index

Keep this list current — one line per script: name, language, purpose.

- `mcp-curl/mcp_curl.py` — Python, no deps. Introspects an MCP server (Streamable HTTP, legacy SSE, or stdio) and prints curl commands for every tool/resource/prompt with schema-derived example args; `--call`/`--rpc` execute directly. See `mcp-curl/README.md`.
- `mcp-gen-client/mcp_gen_client.py` — Python, no deps. Generates a standalone Python client module for an MCP server: one typed function per tool, resource/prompt helpers, and a CLI; credentials become env-var lookups. Shares (duplicated, marker-delimited) transport code with mcp-curl. See `mcp-gen-client/README.md`.
- `mcp-gen-cli/mcp_gen_cli.py` — Python, no deps. Generates a standalone one-shot CLI for an MCP server (`--lang bash` = curl+jq, `--lang python` = stdlib): every tool is a subcommand, every invocation is a full initialize→call→close exchange. Same embedded runtime as the other two. See `mcp-gen-cli/README.md`.
