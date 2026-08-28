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

**Work stack:** the day job is a **LoopBack 4** codebase written in
**TypeScript**. Prefer TypeScript for new scripts (see the Node/TypeScript
convention below), and when a script is meant to understand or transform the
work codebase, target LoopBack 4 (`@loopback/*`, decorator-and-DI style,
`src/**` with `*.model.ts` / `*.repository.ts` / `*.controller.ts`), not
LoopBack 3.

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
- `token-report/token_report.py` — Python, no deps. Aggregates Claude Code token usage from local transcripts (`~/.claude/projects/**/*.jsonl`) per project/session/model/day; optional user-supplied $/Mtok rates for a cost column. See `token-report/README.md`. TypeScript port: `token-report/token_report.ts` (`npx tsx`, zero-dep, byte-for-byte equivalent).
- `ctx-pack/ctx_pack.py` — Python, no deps. Shrinks logs/CI output before pasting into an LLM: strips ANSI, squashes blobs, collapses near-duplicate lines, `--errors-only` context extraction, `--max-tokens` budget; prints savings to stderr. Tests: `python3 ctx-pack/test_ctx_pack.py`. See `ctx-pack/README.md`. TypeScript port: `ctx-pack/ctx_pack.ts` (`npx tsx`, zero-dep, byte-for-byte equivalent); tests `npx tsx --test ctx-pack/ctx_pack.test.ts`.
- `loopback-to-nest/lb2nest.py` — Python, no deps. Migrates a LoopBack 3 codebase to NestJS 12: `scan` prints a migration inventory + manual-attention checklist; `generate` scaffolds entities, DTOs (class-validator), services, controllers (CRUD + remote-method stubs), modules, a TypeORM `data-source.ts` from `datasources.json`, and a root `app.module.ts`. `--orm typeorm|none`. Tests: `python3 loopback-to-nest/test_lb2nest.py`. See `loopback-to-nest/README.md`.
- `lb-relations/lb_relations.py` — Python, no deps. Converts LoopBack 3 model relations into TypeORM relation decorators (`@ManyToOne`/`@OneToMany`/`@OneToOne`/`@ManyToMany` with `@JoinColumn`/`@JoinTable` and inverse-side arrows), wiring both ends across all models and flagging referencesMany/embeds/polymorphic for manual review; fills the gap lb2nest leaves. Tests: `python3 lb-relations/test_lb_relations.py`. See `lb-relations/README.md`.
- `lb-filter/lb_filter.py` — Python, no deps. Generates a NestJS 12 + TypeORM module (`LoopbackFilterPipe` + `toFindManyOptions` helper) that parses a LoopBack 3 `filter` query (where/include/order/limit/skip/fields, all operators) into a TypeORM `FindManyOptions`, so existing REST clients keep working. Tests: `python3 lb-filter/test_lb_filter.py`. See `lb-filter/README.md`.
- `lb-acl/lb_acl.py` — Python, no deps. Converts LoopBack 3 model `acls` into a NestJS 12 authorization scaffold: a reusable RolesGuard + `@Roles`/`@Public` decorators, a per-model report with suggested `@UseGuards` annotations, and a machine-readable permission matrix; flags `$owner`/DENY-specificity cases for manual review. Tests: `python3 lb-acl/test_lb_acl.py`. See `lb-acl/README.md`.
- `lb-hooks/lb_hooks.py` — Python, no deps. Scans LoopBack 3 model `.js` files for operation/remote hooks and scaffolds NestJS 12 stubs (TypeORM `EntitySubscriber` methods + interceptors) that embed the original hook source for a human to port. Tests: `python3 lb-hooks/test_lb_hooks.py`. See `lb-hooks/README.md`.
- `lb-config/lb_config.py` — Python, no deps. Converts LoopBack 3 server config (`config.json`/`datasources.json`/`component-config.json`, with NODE_ENV layering) into a NestJS 12 `@nestjs/config` setup — namespaced `registerAs` factories, `.env.example`, and a mapping/manual-attention report; secrets become `process.env` refs, never literals. Tests: `python3 lb-config/test_lb_config.py`. See `lb-config/README.md`.

## Docs

`docs/` holds repo-level markdown notes. `docs/script-recommendations.md`
is the ranked backlog of scripts to build next (ordered by token-saving
usefulness) — update it when a script gets built or priorities change.
