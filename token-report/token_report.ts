#!/usr/bin/env -S npx tsx
/**
 * token_report.ts — where do my Claude Code tokens actually go?
 *
 * Parses Claude Code transcript files (~/.claude/projects/**\/*.jsonl) and
 * aggregates token usage per project, session, model, or day, so you can see
 * which workflows burn the budget before optimizing anything.
 *
 * Node >= 22, no dependencies (Node stdlib only). Run via `npx tsx`.
 * Reads local transcripts only; nothing leaves the machine.
 *
 * Usage:
 *   ./token_report.ts                          # all projects, grouped by project
 *   ./token_report.ts --by session             # group by session
 *   ./token_report.ts --by model --since 2026-08-01
 *   ./token_report.ts --by day --json
 *   ./token_report.ts ~/other/.claude/projects # explicit transcript root(s)
 *
 * Options:
 *   ROOT ...            transcript roots to scan (default: ~/.claude/projects)
 *   --by KEY            project | session | model | day   (default: project)
 *   --since YYYY-MM-DD  ignore entries before this date (UTC)
 *   --until YYYY-MM-DD  ignore entries after this date (UTC)
 *   --top N             show only the top N rows (default: all)
 *   --json              machine-readable output instead of a table
 *   --price-in X        optional $/Mtok for input tokens  } adds a cost column,
 *   --price-out X       optional $/Mtok for output tokens } you supply the rates
 *                       (cache reads are counted at 10% of --price-in, cache
 *                       writes at 125%, matching the usual pricing structure)
 *
 * Exit codes: 0 ok, 1 bad args, 2 no transcripts found.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import * as process from "node:process";

const USAGE_KEYS = ["input", "output", "cache_read", "cache_create"] as const;
type UsageKey = (typeof USAGE_KEYS)[number];

type GroupBy = "project" | "session" | "model" | "day";

interface Record_ {
  id: string;
  ts: Date | null;
  project: string;
  session: string;
  model: string;
  input: number;
  output: number;
  cache_read: number;
  cache_create: number;
}

interface Row {
  input: number;
  output: number;
  cache_read: number;
  cache_create: number;
  calls: number;
}

const USAGE = `token_report.ts — where do my Claude Code tokens actually go?

Parses Claude Code transcript files (~/.claude/projects/**/*.jsonl) and
aggregates token usage per project, session, model, or day.

Usage:
  ./token_report.ts                          # all projects, grouped by project
  ./token_report.ts --by session             # group by session
  ./token_report.ts --by model --since 2026-08-01
  ./token_report.ts --by day --json
  ./token_report.ts ~/other/.claude/projects # explicit transcript root(s)

Options:
  ROOT ...            transcript roots to scan (default: ~/.claude/projects)
  --by KEY            project | session | model | day   (default: project)
  --since YYYY-MM-DD  ignore entries before this date (UTC)
  --until YYYY-MM-DD  ignore entries after this date (UTC)
  --top N             show only the top N rows (default: all)
  --json              machine-readable output instead of a table
  --price-in X        optional $/Mtok for input tokens  } adds a cost column,
  --price-out X       optional $/Mtok for output tokens } you supply the rates
                      (cache reads at 10% of --price-in, cache writes at 125%)

Exit codes: 0 ok, 1 bad args, 2 no transcripts found.`;

function die(msg: string, code = 1): never {
  process.stderr.write(`error: ${msg}\n`);
  process.exit(code);
}

function parseDate(s: string): Date {
  // strict YYYY-MM-DD, interpreted as UTC midnight
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) die(`bad date ${JSON.stringify(s)}, expected YYYY-MM-DD`);
  const [, y, mo, d] = m as unknown as [string, string, string, string];
  const dt = new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d)));
  // reject overflow like 2026-13-40
  if (
    dt.getUTCFullYear() !== Number(y) ||
    dt.getUTCMonth() !== Number(mo) - 1 ||
    dt.getUTCDate() !== Number(d)
  ) {
    die(`bad date ${JSON.stringify(s)}, expected YYYY-MM-DD`);
  }
  return dt;
}

function parseTs(s: unknown): Date | null {
  // transcripts use ISO 8601, e.g. 2026-08-27T01:10:00.123Z
  if (typeof s !== "string" || !s) return null;
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : new Date(t);
}

function projectLabel(filePath: string, root: string): string {
  const rel = path.relative(root, filePath);
  return rel.includes(path.sep) ? rel.split(path.sep)[0]! : "(root)";
}

function* walkJsonl(root: string): Generator<string> {
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(root, { withFileTypes: true });
  } catch {
    return;
  }
  for (const ent of entries) {
    const full = path.join(root, ent.name);
    if (ent.isDirectory()) {
      yield* walkJsonl(full);
    } else if (ent.isFile() && ent.name.endsWith(".jsonl")) {
      yield full;
    }
  }
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function* iterUsage(root: string): Generator<Record_> {
  for (const filePath of walkJsonl(root)) {
    let content: string;
    try {
      content = fs.readFileSync(filePath, "utf-8");
    } catch (e) {
      process.stderr.write(`warning: cannot read ${filePath}: ${String(e)}\n`);
      continue;
    }
    for (const raw of content.split("\n")) {
      const line = raw.trim();
      if (!line || !line.includes('"usage"')) continue;
      let entry: any;
      try {
        entry = JSON.parse(line);
      } catch {
        continue;
      }
      if (!entry || entry.type !== "assistant") continue;
      const msg = entry.message || {};
      const usage = msg.usage || {};
      if (!usage || typeof usage !== "object" || Object.keys(usage).length === 0)
        continue;
      yield {
        id: msg.id || entry.uuid,
        ts: parseTs(entry.timestamp),
        project: projectLabel(filePath, root),
        session: entry.sessionId || "(unknown)",
        model: msg.model || "(unknown)",
        input: num(usage.input_tokens),
        output: num(usage.output_tokens),
        cache_read: num(usage.cache_read_input_tokens),
        cache_create: num(usage.cache_creation_input_tokens),
      };
    }
  }
}

function groupKey(rec: Record_, by: GroupBy): string {
  if (by === "day") {
    if (!rec.ts) return "(no date)";
    const y = rec.ts.getUTCFullYear().toString().padStart(4, "0");
    const m = (rec.ts.getUTCMonth() + 1).toString().padStart(2, "0");
    const d = rec.ts.getUTCDate().toString().padStart(2, "0");
    return `${y}-${m}-${d}`;
  }
  return rec[by];
}

function cost(row: Row, priceIn: number, priceOut: number): number {
  return (
    (row.input * priceIn +
      row.cache_read * priceIn * 0.1 +
      row.cache_create * priceIn * 1.25 +
      row.output * priceOut) /
    1_000_000
  );
}

function fmt(n: number): string {
  if (n >= 10_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(0)}k`;
  return String(n);
}

function total(row: Row): number {
  return row.input + row.output + row.cache_read + row.cache_create;
}

function round4(x: number): number {
  return Math.round((x + Number.EPSILON) * 10000) / 10000;
}

function main(): void {
  const argv = process.argv.slice(2);
  const roots: string[] = [];
  let by: GroupBy = "project";
  let since: Date | null = null;
  let until: Date | null = null;
  let top: number | null = null;
  let asJson = false;
  let priceIn: number | null = null;
  let priceOut: number | null = null;

  const needValue = (name: string, inline: string | undefined, next: () => string | undefined): string => {
    if (inline !== undefined) return inline;
    const v = next();
    if (v === undefined) die(`argument ${name}: expected one argument`);
    return v;
  };

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]!;
    let flag = arg;
    let inline: string | undefined;
    const eq = arg.indexOf("=");
    if (arg.startsWith("--") && eq !== -1) {
      flag = arg.slice(0, eq);
      inline = arg.slice(eq + 1);
    }
    const next = () => argv[++i];

    switch (flag) {
      case "-h":
      case "--help":
        process.stdout.write(USAGE + "\n");
        process.exit(2);
        break;
      case "--by": {
        const v = needValue("--by", inline, next);
        if (v !== "project" && v !== "session" && v !== "model" && v !== "day") {
          die(`argument --by: invalid choice ${JSON.stringify(v)} (choose from project, session, model, day)`);
        }
        by = v;
        break;
      }
      case "--since":
        since = parseDate(needValue("--since", inline, next));
        break;
      case "--until":
        until = parseDate(needValue("--until", inline, next));
        break;
      case "--top": {
        const v = needValue("--top", inline, next);
        const n = Number(v);
        if (!Number.isInteger(n)) die(`argument --top: invalid int value: ${JSON.stringify(v)}`);
        top = n;
        break;
      }
      case "--json":
        asJson = true;
        break;
      case "--price-in": {
        const v = needValue("--price-in", inline, next);
        const n = Number(v);
        if (!Number.isFinite(n)) die(`argument --price-in: invalid float value: ${JSON.stringify(v)}`);
        priceIn = n;
        break;
      }
      case "--price-out": {
        const v = needValue("--price-out", inline, next);
        const n = Number(v);
        if (!Number.isFinite(n)) die(`argument --price-out: invalid float value: ${JSON.stringify(v)}`);
        priceOut = n;
        break;
      }
      default:
        if (arg.startsWith("-") && arg !== "-") {
          die(`unrecognized arguments: ${arg}`);
        }
        roots.push(arg);
    }
  }

  if ((priceIn === null) !== (priceOut === null)) {
    die("--price-in and --price-out must be given together");
  }

  const rootList = roots.length ? roots : [path.join(os.homedir(), ".claude", "projects")];

  // Collect distinct files (for the "no transcripts" check) across all roots.
  let anyFiles = false;
  for (const root of rootList) {
    for (const _ of walkJsonl(root)) {
      anyFiles = true;
      break;
    }
    if (anyFiles) break;
  }
  if (!anyFiles) {
    process.stderr.write(`no transcripts found under: ${rootList.join(", ")}\n`);
    process.exit(2);
  }

  const seen = new Set<string>();
  const groups = new Map<string, Row>();
  const sessions = new Map<string, Set<string>>();

  for (const root of rootList) {
    for (const rec of iterUsage(root)) {
      if (rec.id === undefined || rec.id === null) continue;
      if (seen.has(rec.id)) continue;
      seen.add(rec.id);
      if (since && (!rec.ts || rec.ts < since)) continue;
      if (until && (!rec.ts || rec.ts > until)) continue;
      const key = groupKey(rec, by);
      let row = groups.get(key);
      if (!row) {
        row = { input: 0, output: 0, cache_read: 0, cache_create: 0, calls: 0 };
        groups.set(key, row);
      }
      for (const k of USAGE_KEYS) row[k] += rec[k];
      row.calls += 1;
      let s = sessions.get(key);
      if (!s) {
        s = new Set<string>();
        sessions.set(key, s);
      }
      s.add(rec.session);
    }
  }

  let ranked = [...groups.entries()].sort((a, b) => total(b[1]) - total(a[1]));
  if (top !== null && top) ranked = ranked.slice(0, top);

  const priced = priceIn !== null;

  if (asJson) {
    const out = ranked.map(([key, row]) => {
      const item: { [k: string]: unknown } = {
        [by]: key,
        input: row.input,
        output: row.output,
        cache_read: row.cache_read,
        cache_create: row.cache_create,
        calls: row.calls,
        total: total(row),
        sessions: sessions.get(key)!.size,
      };
      if (priced) item.cost_usd = round4(cost(row, priceIn!, priceOut!));
      return item;
    });
    process.stdout.write(JSON.stringify(out, null, 2) + "\n");
    return;
  }

  const headers = [
    by,
    "total",
    "input",
    "output",
    "cache_rd",
    "cache_wr",
    "calls",
    "sessions",
    ...(priced ? ["$"] : []),
  ];

  const rows: string[][] = ranked.map(([key, row]) => {
    const r = [
      key,
      fmt(total(row)),
      fmt(row.input),
      fmt(row.output),
      fmt(row.cache_read),
      fmt(row.cache_create),
      String(row.calls),
      String(sessions.get(key)!.size),
    ];
    if (priced) r.push(cost(row, priceIn!, priceOut!).toFixed(2));
    return r;
  });

  const widths = headers.map((h, i) =>
    rows.length ? Math.max(h.length, ...rows.map((r) => r[i]!.length)) : h.length,
  );

  process.stdout.write(headers.map((h, i) => h.padEnd(widths[i]!)).join("  ") + "\n");
  process.stdout.write(widths.map((w) => "-".repeat(w)).join("  ") + "\n");
  for (const r of rows) {
    process.stdout.write(r.map((v, i) => v.padEnd(widths[i]!)).join("  ") + "\n");
  }

  const grand: Row = { input: 0, output: 0, cache_read: 0, cache_create: 0, calls: 0 };
  for (const row of groups.values()) {
    for (const k of USAGE_KEYS) grand[k] += row[k];
  }
  let summary =
    `\nall groups: total ${fmt(total(grand))}  ` +
    `(input ${fmt(grand.input)}, output ${fmt(grand.output)}, ` +
    `cache read ${fmt(grand.cache_read)}, ` +
    `cache write ${fmt(grand.cache_create)})`;
  if (priced) summary += `  ≈ $${cost(grand, priceIn!, priceOut!).toFixed(2)}`;
  process.stdout.write(summary + "\n");
}

main();
