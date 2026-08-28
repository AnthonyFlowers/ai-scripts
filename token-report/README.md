# token-report

Find out where your Claude Code tokens actually go before optimizing
anything. Parses the local transcript files Claude Code already writes
(`~/.claude/projects/**/*.jsonl`) and aggregates usage per project, session,
model, or day. Nothing leaves the machine.

Python 3.9+, no dependencies.

## Usage

```sh
./token_report.py                           # grouped by project
./token_report.py --by session --top 10     # ten hungriest sessions
./token_report.py --by model --since 2026-08-01
./token_report.py --by day --json           # machine-readable
./token_report.py --price-in 3 --price-out 15   # add a $ column (rates you supply, $/Mtok)
```

| Flag | Meaning |
|------|---------|
| `ROOT ...` | transcript roots to scan (default `~/.claude/projects`) |
| `--by project\|session\|model\|day` | grouping key (default `project`) |
| `--since` / `--until YYYY-MM-DD` | date window (UTC) |
| `--top N` | only the top N rows |
| `--json` | JSON array instead of a table |
| `--price-in X --price-out X` | $/Mtok rates to derive a cost column — cache reads billed at 10% of input, cache writes at 125% |

## Notes

- Rows are sorted by total tokens (input + output + cache read + cache write)
  so the biggest sink is on top. `cache_rd` is usually the dominant number in
  long sessions; it is also the cheapest per token, which is why the optional
  cost column can rank things differently than the token totals.
- Duplicate API responses (session resume/branching copies history between
  files) are deduplicated by message id, so totals aren't inflated.
- Pricing is deliberately **not** built in — rates change; pass your own.

## Interpreting it

The point is to pick optimization targets: a project dominated by `cache_rd`
with many calls per session means long conversations re-reading a big
context (candidates: `repo-brief`-style pre-digested context, shorter
sessions); big `input` with few calls means large one-shot pastes
(candidate: `ctx-pack`).
