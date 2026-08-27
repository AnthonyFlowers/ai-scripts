#!/usr/bin/env python3
"""token_report.py — where do my Claude Code tokens actually go?

Parses Claude Code transcript files (~/.claude/projects/**/*.jsonl) and
aggregates token usage per project, session, model, or day, so you can see
which workflows burn the budget before optimizing anything.

Python 3.9+, no dependencies. Reads local transcripts only; nothing leaves
the machine.

Usage:
  ./token_report.py                          # all projects, grouped by project
  ./token_report.py --by session             # group by session
  ./token_report.py --by model --since 2026-08-01
  ./token_report.py --by day --json
  ./token_report.py ~/other/.claude/projects # explicit transcript root(s)

Options:
  ROOT ...            transcript roots to scan (default: ~/.claude/projects)
  --by KEY            project | session | model | day   (default: project)
  --since YYYY-MM-DD  ignore entries before this date (UTC)
  --until YYYY-MM-DD  ignore entries after this date (UTC)
  --top N             show only the top N rows (default: all)
  --json              machine-readable output instead of a table
  --price-in X        optional $/Mtok for input tokens  } adds a cost column,
  --price-out X       optional $/Mtok for output tokens } you supply the rates
                      (cache reads are counted at 10% of --price-in, cache
                      writes at 125%, matching the usual pricing structure)

Exit codes: 0 ok, 1 bad args, 2 no transcripts found.
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

USAGE_KEYS = ("input", "output", "cache_read", "cache_create")


def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        sys.exit(f"error: bad date {s!r}, expected YYYY-MM-DD")


def parse_ts(s):
    # transcripts use ISO 8601, e.g. 2026-08-27T01:10:00.123Z
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def project_label(path, root):
    """Directory name under the root, with Claude Code's path-mangling undone
    enough to be readable (-home-user-ai-scripts -> ~/…/ai-scripts is not
    reliably reversible, so just show the raw directory name)."""
    rel = os.path.relpath(path, root)
    return rel.split(os.sep)[0] if os.sep in rel else "(root)"


def iter_usage(files, root):
    """Yield one record per assistant API response found in the transcripts.

    The same response can appear in several files (session resume/branch
    copies the history), so records are deduplicated by message id (falling
    back to the entry uuid) by the caller.
    """
    for path in files:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"warning: cannot read {path}: {e}", file=sys.stderr)
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line or '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                yield {
                    "id": msg.get("id") or entry.get("uuid"),
                    "ts": parse_ts(entry.get("timestamp")),
                    "project": project_label(path, root),
                    "session": entry.get("sessionId") or "(unknown)",
                    "model": msg.get("model") or "(unknown)",
                    "input": usage.get("input_tokens") or 0,
                    "output": usage.get("output_tokens") or 0,
                    "cache_read": usage.get("cache_read_input_tokens") or 0,
                    "cache_create": usage.get("cache_creation_input_tokens") or 0,
                }


def group_key(rec, by):
    if by == "day":
        return rec["ts"].strftime("%Y-%m-%d") if rec["ts"] else "(no date)"
    return rec[by]


def cost(row, price_in, price_out):
    return (
        row["input"] * price_in
        + row["cache_read"] * price_in * 0.10
        + row["cache_create"] * price_in * 1.25
        + row["output"] * price_out
    ) / 1_000_000


def fmt(n):
    if n >= 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="*", default=None)
    ap.add_argument("--by", choices=["project", "session", "model", "day"],
                    default="project")
    ap.add_argument("--since", type=parse_date)
    ap.add_argument("--until", type=parse_date)
    ap.add_argument("--top", type=int)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--price-in", type=float)
    ap.add_argument("--price-out", type=float)
    args = ap.parse_args()

    if (args.price_in is None) != (args.price_out is None):
        sys.exit("error: --price-in and --price-out must be given together")

    roots = args.roots or [os.path.expanduser("~/.claude/projects")]
    files = []
    for root in roots:
        files += glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    if not files:
        print(f"no transcripts found under: {', '.join(roots)}", file=sys.stderr)
        sys.exit(2)

    seen = set()
    groups = {}
    sessions = {}
    for root in roots:
        root_files = [f for f in files if f.startswith(os.path.abspath(root))
                      or f.startswith(root)]
        for rec in iter_usage(root_files, root):
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            if args.since and (not rec["ts"] or rec["ts"] < args.since):
                continue
            if args.until and (not rec["ts"] or rec["ts"] > args.until):
                continue
            key = group_key(rec, args.by)
            row = groups.setdefault(key, dict.fromkeys(USAGE_KEYS, 0) | {"calls": 0})
            for k in USAGE_KEYS:
                row[k] += rec[k]
            row["calls"] += 1
            sessions.setdefault(key, set()).add(rec["session"])

    def total(row):
        return sum(row[k] for k in USAGE_KEYS)

    ranked = sorted(groups.items(), key=lambda kv: total(kv[1]), reverse=True)
    if args.top:
        ranked = ranked[: args.top]

    priced = args.price_in is not None

    if args.json:
        out = []
        for key, row in ranked:
            item = {args.by: key, **row, "total": total(row),
                    "sessions": len(sessions[key])}
            if priced:
                item["cost_usd"] = round(cost(row, args.price_in, args.price_out), 4)
            out.append(item)
        json.dump(out, sys.stdout, indent=2)
        print()
        return

    headers = [args.by, "total", "input", "output", "cache_rd", "cache_wr",
               "calls", "sessions"] + (["$"] if priced else [])
    rows = []
    for key, row in ranked:
        r = [key, fmt(total(row)), fmt(row["input"]), fmt(row["output"]),
             fmt(row["cache_read"]), fmt(row["cache_create"]),
             str(row["calls"]), str(len(sessions[key]))]
        if priced:
            r.append(f"{cost(row, args.price_in, args.price_out):.2f}")
        rows.append(r)

    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))

    grand = dict.fromkeys(USAGE_KEYS, 0)
    for _, row in groups.items():
        for k in USAGE_KEYS:
            grand[k] += row[k]
    summary = (f"\nall groups: total {fmt(total(grand))}  "
               f"(input {fmt(grand['input'])}, output {fmt(grand['output'])}, "
               f"cache read {fmt(grand['cache_read'])}, "
               f"cache write {fmt(grand['cache_create'])})")
    if priced:
        summary += f"  ≈ ${cost(grand, args.price_in, args.price_out):.2f}"
    print(summary)


if __name__ == "__main__":
    main()
