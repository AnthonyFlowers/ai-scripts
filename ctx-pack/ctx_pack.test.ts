#!/usr/bin/env -S npx tsx
/**
 * Tests for ctx_pack. Run: npx tsx --test ctx-pack/ctx_pack.test.ts
 *
 * Ports the 8 Python tests from test_ctx_pack.py. Most exercise the exported
 * `pack()` core directly; one spawns the CLI as a subprocess to prove wiring.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";

import { pack } from "./ctx_pack.js";

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "ctx_pack.ts");

/** Mirror of the Python helper: run the packer, fail on non-zero exit. */
function run(args: string[], stdin: string): string {
  const { stdout, stderr, code } = pack(["--quiet", ...args], stdin);
  if (code !== 0) throw new Error(`exit ${code}: ${stderr}`);
  return stdout;
}

function splitlines(s: string): string[] {
  return s === "" ? [] : s.replace(/\n$/, "").split("\n");
}

test("strips_ansi", () => {
  const out = run([], "\x1b[31mred error\x1b[0m text\n");
  assert.equal(out, "red error text\n");
});

test("collapses_duplicate_lines_modulo_numbers", () => {
  const stdin = Array.from(
    { length: 50 },
    (_, i) => `2026-08-27T01:00:${String(i).padStart(2, "0")}Z retry attempt ${i}`,
  ).join("\n");
  const out = run([], stdin);
  assert.ok(out.includes("repeated 49 more times"));
  assert.equal(splitlines(out).length, 2);
});

test("squashes_blobs", () => {
  const blob = "A".repeat(200);
  const out = run([], `payload=${blob}\n`);
  assert.ok(out.includes("<blob:200 chars>"));
  assert.ok(!out.includes(blob));
});

test("truncates_long_lines", () => {
  const out = run(["--max-line", "10", "--no-blobs"], "abcdefghijKLMNOP\n");
  assert.ok(out.startsWith("abcdefghij …[+6 chars]"));
});

test("errors_only_keeps_context", () => {
  const lines = Array.from({ length: 20 }, (_, i) => `line ${i}`);
  lines[10] = "something FAILED here";
  const out = run(["--errors-only", "-C", "1"], lines.join("\n"));
  const body = splitlines(out);
  assert.ok(body.includes("line 9"));
  assert.ok(body.includes("something FAILED here"));
  assert.ok(body.includes("line 11"));
  assert.ok(!body.includes("line 5"));
  assert.ok(body.some((l) => l.includes("lines skipped")));
});

test("keep_regex_survives_dedupe", () => {
  const stdin = "keepme 1\nkeepme 2\nkeepme 3\n";
  const out = run(["--keep", "keepme"], stdin);
  assert.deepEqual(splitlines(out), ["keepme 1", "keepme 2", "keepme 3"]);
});

test("max_tokens_elides_middle", () => {
  const stdin = Array.from(
    { length: 2000 },
    (_, i) => `unique line number ${i} with some padding text`,
  ).join("\n");
  const out = run(["--max-tokens", "500", "--no-dedupe"], stdin);
  assert.ok(out.includes("elided to fit"));
  assert.ok(out.length < stdin.length / 2);
  assert.ok(out.includes("unique line number 0 "));
  assert.ok(out.includes("unique line number 1999 "));
});

test("blank_runs_collapse", () => {
  const out = run([], "a\n\n\n\n\nb\n");
  assert.equal(out, "a\n\nb\n");
});

// Subprocess smoke test: prove the CLI wiring (stdin -> stdout, stats -> stderr).
test("cli_subprocess_smoke", () => {
  const p = spawnSync(
    "npx",
    ["tsx", SCRIPT, "--errors-only"],
    { input: "a\n\x1b[31merror: boom\x1b[0m\nb\n", encoding: "utf-8" },
  );
  assert.equal(p.status, 0);
  assert.ok(p.stdout.includes("error: boom"));
  assert.ok(!p.stdout.includes("\x1b["));
  assert.ok(p.stderr.includes("ctx-pack:"));
});
