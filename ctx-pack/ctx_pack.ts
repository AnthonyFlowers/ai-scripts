#!/usr/bin/env -S npx tsx
/**
 * ctx_pack.ts — shrink logs/CI output/dumps before pasting them into an LLM.
 *
 * TypeScript port of ctx_pack.py (functionally equivalent). Reads text, removes
 * what a model does not need (ANSI codes, repeated lines, giant base64 blobs,
 * boilerplate), optionally keeps only error lines with context, and prints the
 * packed result to stdout. A before/after size report (chars, lines, estimated
 * tokens) goes to stderr so the savings are visible.
 *
 * Node >= 22, zero runtime deps (stdlib only), ESM, run via `npx tsx` (no build).
 * Token estimate is chars/4 — rough, but fine for "did this get 10x smaller?".
 *
 * Usage:
 *   ./ctx_pack.ts build.log                     # pack a file to stdout
 *   some-command 2>&1 | ./ctx_pack.ts           # pack a pipe
 *   ./ctx_pack.ts --errors-only -C 5 ci.log     # only error lines +/-5 context
 *   ./ctx_pack.ts --max-tokens 8000 huge.log    # hard budget: keeps head+tail
 *   ./ctx_pack.ts --keep 'my_service' app.log   # never drop matching lines
 *
 * Options:
 *   FILE ...           input files (default: stdin); multiple files are packed
 *                      in sequence with a "==> name <==" header each
 *   --errors-only      keep only lines matching the error pattern (plus context)
 *   -C, --context N    context lines around kept lines in --errors-only (def 3)
 *   --errors-regex RE  override the built-in error pattern
 *   --keep RE          lines matching RE are always kept verbatim (repeatable)
 *   --max-line N       truncate lines longer than N chars (default 400, 0=off)
 *   --max-tokens N     if the packed result still exceeds ~N tokens, keep the
 *                      first and last halves and elide the middle (0=off)
 *   --no-dedupe        don't collapse consecutive (near-)duplicate lines
 *   --no-blobs         don't squash long base64/hex runs
 *   --quiet            suppress the stats report on stderr
 *   -h, --help         print this usage and exit non-zero
 *
 * Exit codes: 0 ok, 1 bad args / unreadable input, 2 usage/help.
 */

import { readFileSync } from "node:fs";
import process from "node:process";
import { fileURLToPath } from "node:url";

const ANSI_RE = /\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*(\x07|\x1b\\)/g;
// unbroken base64ish / hex / hash runs that carry no meaning for a model
const BLOB_RE = /[A-Za-z0-9+/_-]{64,}={0,2}/g;
// used to decide two lines are "the same" modulo counters/ids/timestamps
const NORM_SUBS: [RegExp, string][] = [
  [/\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?/g, "<ts>"],
  [/\b0x[0-9a-fA-F]+\b/g, "<hex>"],
  [/\b[0-9a-fA-F]{8,}\b/g, "<hex>"],
  [/\b\d+\b/g, "<n>"],
];
const DEFAULT_ERRORS_RE =
  "\\b(error|err!|fail(ed|ure)?|exception|traceback|fatal|panic" +
  "|assert(ion)?|segfault|denied|refused|timeout|timed out|unhandled" +
  "|cannot|not found|undefined|null pointer|stack trace)\\b";

export interface Options {
  files: string[];
  errorsOnly: boolean;
  context: number;
  errorsRegex: string;
  keep: string[];
  maxLine: number;
  maxTokens: number;
  noDedupe: boolean;
  noBlobs: boolean;
  quiet: boolean;
}

export interface PackResult {
  stdout: string;
  stderr: string;
  code: number;
}

function defaultOptions(): Options {
  return {
    files: [],
    errorsOnly: false,
    context: 3,
    errorsRegex: DEFAULT_ERRORS_RE,
    keep: [],
    maxLine: 400,
    maxTokens: 0,
    noDedupe: false,
    noBlobs: false,
    quiet: false,
  };
}

export function estimateTokens(text: string): number {
  return Math.floor(text.length / 4);
}

export function normalize(line: string): string {
  for (const [rx, repl] of NORM_SUBS) {
    line = line.replace(rx, repl);
  }
  return line;
}

/** Mirror of Python str.splitlines() for the common line terminators. */
function splitLines(text: string): string[] {
  if (text === "") return [];
  const parts = text.split(/\r\n|\r|\n/);
  // Python's splitlines() does not emit a trailing "" for a final terminator.
  if (/[\r\n]$/.test(text)) parts.pop();
  return parts;
}

function fmt(n: number): string {
  return n.toLocaleString("en-US");
}

export function packLines(
  lines: string[],
  opts: Options,
  keepRes: RegExp[],
): string[] {
  const out: string[] = [];

  const emit = (line: string): void => {
    if (opts.maxLine && line.length > opts.maxLine) {
      line =
        line.slice(0, opts.maxLine) +
        ` …[+${line.length - opts.maxLine} chars]`;
    }
    out.push(line);
  };

  // 1. strip ANSI, trailing whitespace; squash blobs
  let cleaned: string[] = [];
  for (let line of lines) {
    line = line.replace(ANSI_RE, "").replace(/\s+$/, "");
    if (!opts.noBlobs) {
      line = line.replace(BLOB_RE, (m) => `<blob:${m.length} chars>`);
    }
    cleaned.push(line);
  }

  // 2. --errors-only: keep matching lines +/- context, plus --keep lines
  if (opts.errorsOnly) {
    const errRe = new RegExp(opts.errorsRegex, "i");
    const keepIdx = new Set<number>();
    for (let i = 0; i < cleaned.length; i++) {
      const line = cleaned[i];
      if (errRe.test(line) || keepRes.some((r) => r.test(line))) {
        const lo = Math.max(0, i - opts.context);
        const hi = Math.min(cleaned.length, i + opts.context + 1);
        for (let j = lo; j < hi; j++) keepIdx.add(j);
      }
    }
    const selected: string[] = [];
    let last: number | null = null;
    for (const i of [...keepIdx].sort((a, b) => a - b)) {
      if (last !== null && i > last + 1) {
        selected.push(`⋮ [${i - last - 1} lines skipped]`);
      }
      selected.push(cleaned[i]);
      last = i;
    }
    if (last !== null && last < cleaned.length - 1) {
      selected.push(`⋮ [${cleaned.length - 1 - last} lines skipped]`);
    }
    cleaned = selected;
  }

  // 3. collapse blank runs and consecutive near-duplicates
  let prevNorm: string | null = null;
  let runFirst: string | null = null;
  let runCount = 0;
  let blankRun = 0;

  const flushRun = (): void => {
    if (runFirst !== null) {
      emit(runFirst);
      if (runCount > 1) {
        emit(`  [previous line repeated ${runCount - 1} more times]`);
      }
    }
    runFirst = null;
    runCount = 0;
    prevNorm = null;
  };

  for (const line of cleaned) {
    if (!line) {
      flushRun();
      blankRun += 1;
      if (blankRun === 1) out.push("");
      continue;
    }
    blankRun = 0;
    if (opts.noDedupe || keepRes.some((r) => r.test(line))) {
      flushRun();
      emit(line);
      continue;
    }
    const norm = normalize(line);
    if (norm === prevNorm) {
      runCount += 1;
    } else {
      flushRun();
      runFirst = line;
      runCount = 1;
      prevNorm = norm;
    }
  }
  flushRun();

  // 4. hard token budget: keep head + tail
  if (opts.maxTokens) {
    const text = out.join("\n");
    if (estimateTokens(text) > opts.maxTokens) {
      const budgetChars = opts.maxTokens * 4;
      const headChars = Math.floor((budgetChars * 6) / 10);
      const tailChars = budgetChars - headChars;
      const head: string[] = [];
      const tail: string[] = [];
      let n = 0;
      for (const line of out) {
        if (n + line.length <= headChars) {
          head.push(line);
          n += line.length + 1;
        } else {
          break;
        }
      }
      n = 0;
      const rest = out.slice(head.length);
      for (let k = rest.length - 1; k >= 0; k--) {
        const line = rest[k];
        if (n + line.length <= tailChars) {
          tail.push(line);
          n += line.length + 1;
        } else {
          break;
        }
      }
      const dropped = out.length - head.length - tail.length;
      if (dropped > 0) {
        return [
          ...head,
          `⋮ [${dropped} lines elided to fit ~${opts.maxTokens} token budget]`,
          ...tail.reverse(),
        ];
      }
    }
  }

  return out;
}

const USAGE = `ctx-pack — shrink logs/CI output/dumps before pasting into an LLM.

Usage:
  ctx_pack.ts [options] [FILE ...]
  some-command 2>&1 | ctx_pack.ts [options]

Options:
  --errors-only      keep only lines matching the error pattern (plus context)
  -C, --context N    context lines around kept lines in --errors-only (default 3)
  --errors-regex RE  override the built-in error pattern
  --keep RE          lines matching RE are always kept verbatim (repeatable)
  --max-line N       truncate lines longer than N chars (default 400, 0=off)
  --max-tokens N     keep head+tail and elide the middle if over ~N tokens (0=off)
  --no-dedupe        don't collapse consecutive (near-)duplicate lines
  --no-blobs         don't squash long base64/hex runs
  --quiet            suppress the stats report on stderr
  -h, --help         print this usage and exit non-zero`;

interface ParseResult {
  opts?: Options;
  help?: boolean;
  error?: string;
}

function parseArgs(argv: string[]): ParseResult {
  const opts = defaultOptions();
  let onlyFiles = false;

  const parseInt10 = (s: string, flag: string): number | null => {
    if (!/^[+-]?\d+$/.test(s)) return null;
    const v = Number.parseInt(s, 10);
    return Number.isNaN(v) ? null : v;
  };

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (onlyFiles) {
      opts.files.push(a);
      continue;
    }
    const eq = a.indexOf("=");
    const flag = a.startsWith("--") && eq !== -1 ? a.slice(0, eq) : a;
    const inlineVal = a.startsWith("--") && eq !== -1 ? a.slice(eq + 1) : null;
    const takeVal = (): string | null => {
      if (inlineVal !== null) return inlineVal;
      if (i + 1 < argv.length) return argv[++i];
      return null;
    };

    if (a === "--") {
      onlyFiles = true;
    } else if (a === "-h" || a === "--help") {
      return { help: true };
    } else if (flag === "--errors-only") {
      opts.errorsOnly = true;
    } else if (flag === "--no-dedupe") {
      opts.noDedupe = true;
    } else if (flag === "--no-blobs") {
      opts.noBlobs = true;
    } else if (flag === "--quiet") {
      opts.quiet = true;
    } else if (flag === "-C" || flag === "--context") {
      const raw = flag === "-C" ? takeVal() : takeVal();
      if (raw === null) return { error: `argument ${flag}: expected one argument` };
      const v = parseInt10(raw, flag);
      if (v === null) return { error: `argument -C/--context: invalid int value: '${raw}'` };
      opts.context = v;
    } else if (a.startsWith("-C") && a.length > 2 && a[2] !== "-") {
      const raw = a.slice(2);
      const v = parseInt10(raw, "-C");
      if (v === null) return { error: `argument -C/--context: invalid int value: '${raw}'` };
      opts.context = v;
    } else if (flag === "--errors-regex") {
      const raw = takeVal();
      if (raw === null) return { error: "argument --errors-regex: expected one argument" };
      opts.errorsRegex = raw;
    } else if (flag === "--keep") {
      const raw = takeVal();
      if (raw === null) return { error: "argument --keep: expected one argument" };
      opts.keep.push(raw);
    } else if (flag === "--max-line") {
      const raw = takeVal();
      if (raw === null) return { error: "argument --max-line: expected one argument" };
      const v = parseInt10(raw, "--max-line");
      if (v === null) return { error: `argument --max-line: invalid int value: '${raw}'` };
      opts.maxLine = v;
    } else if (flag === "--max-tokens") {
      const raw = takeVal();
      if (raw === null) return { error: "argument --max-tokens: expected one argument" };
      const v = parseInt10(raw, "--max-tokens");
      if (v === null) return { error: `argument --max-tokens: invalid int value: '${raw}'` };
      opts.maxTokens = v;
    } else if (a.length > 1 && a.startsWith("-") && a !== "-") {
      return { error: `unrecognized arguments: ${a}` };
    } else {
      opts.files.push(a);
    }
  }

  return { opts };
}

/**
 * Run the packer with the given argv and stdin text, returning captured output
 * instead of touching process streams. Pure enough to unit-test directly.
 */
export function pack(argv: string[], stdin: string): PackResult {
  const parsed = parseArgs(argv);
  if (parsed.help) {
    return { stdout: USAGE + "\n", stderr: "", code: 2 };
  }
  if (parsed.error || !parsed.opts) {
    return { stdout: "", stderr: `error: ${parsed.error}\n`, code: 2 };
  }
  const opts = parsed.opts;

  let keepRes: RegExp[];
  try {
    keepRes = opts.keep.map((p) => new RegExp(p));
    new RegExp(opts.errorsRegex);
  } catch (e) {
    return { stdout: "", stderr: `error: bad regex: ${(e as Error).message}\n`, code: 1 };
  }

  const sources: [string, string][] = [];
  if (opts.files.length > 0) {
    for (const path of opts.files) {
      try {
        sources.push([path, readFileSync(path, "utf-8")]);
      } catch (e) {
        return { stdout: "", stderr: `error: ${(e as Error).message}\n`, code: 1 };
      }
    }
  } else {
    sources.push(["stdin", stdin]);
  }

  let inChars = 0;
  let inLines = 0;
  for (const [, t] of sources) {
    inChars += t.length;
    inLines += (t.split("\n").length - 1) + 1;
  }

  const chunks: string[] = [];
  for (const [name, text] of sources) {
    const packed = packLines(splitLines(text), opts, keepRes);
    if (sources.length > 1) chunks.push(`==> ${name} <==`);
    chunks.push(...packed);
  }
  const result = chunks.join("\n");
  const stdout = result + (result ? "\n" : "");

  let stderr = "";
  if (!opts.quiet) {
    const outChars = result.length;
    const saved = inChars ? 100.0 * (1 - outChars / inChars) : 0.0;
    stderr =
      `ctx-pack: ${fmt(inChars)} chars / ${fmt(inLines)} lines ` +
      `(~${fmt(Math.floor(inChars / 4))} tok) -> ` +
      `${fmt(outChars)} chars / ${fmt(chunks.length)} lines ` +
      `(~${fmt(estimateTokens(result))} tok)  saved ${saved.toFixed(1)}%\n`;
  }

  return { stdout, stderr, code: 0 };
}

function main(): void {
  const argv = process.argv.slice(2);
  const hasFiles = parseArgs(argv).opts?.files.length ?? 0;
  let stdin = "";
  if (hasFiles === 0) {
    try {
      stdin = readFileSync(0, "utf-8");
    } catch {
      stdin = "";
    }
  }
  const { stdout, stderr, code } = pack(argv, stdin);
  if (stdout) process.stdout.write(stdout);
  if (stderr) process.stderr.write(stderr);
  process.exit(code);
}

const isMain =
  process.argv[1] !== undefined &&
  fileURLToPath(import.meta.url) === process.argv[1];
if (isMain) {
  main();
}
