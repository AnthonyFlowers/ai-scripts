#!/usr/bin/env python3
"""ctx_pack.py — shrink logs/CI output/dumps before pasting them into an LLM.

Reads text, removes what a model does not need (ANSI codes, repeated lines,
giant base64 blobs, boilerplate), optionally keeps only error lines with
context, and prints the packed result to stdout. A before/after size report
(bytes, lines, estimated tokens) goes to stderr so the savings are visible.

Python 3.9+, no dependencies. Token estimate is chars/4 — rough, but fine
for "did this get 10x smaller?".

Usage:
  ./ctx_pack.py build.log                     # pack a file to stdout
  some-command 2>&1 | ./ctx_pack.py           # pack a pipe
  ./ctx_pack.py --errors-only -C 5 ci.log     # only error lines ±5 context
  ./ctx_pack.py --max-tokens 8000 huge.log    # hard budget: keeps head+tail
  ./ctx_pack.py --keep 'my_service' app.log   # never drop matching lines

Options:
  FILE ...           input files (default: stdin); multiple files are packed
                     in sequence with a "==> name <==" header each
  --errors-only      keep only lines matching the error pattern (plus context)
  -C, --context N    context lines around kept lines in --errors-only (default 3)
  --errors-regex RE  override the built-in error pattern
  --keep RE          lines matching RE are always kept verbatim (repeatable)
  --max-line N       truncate lines longer than N chars (default 400, 0=off)
  --max-tokens N     if the packed result still exceeds ~N tokens, keep the
                     first and last halves and elide the middle (0=off)
  --no-dedupe        don't collapse consecutive (near-)duplicate lines
  --no-blobs         don't squash long base64/hex runs
  --quiet            suppress the stats report on stderr

Exit codes: 0 ok, 1 bad args / unreadable input.
"""

import argparse
import re
import sys

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*(\x07|\x1b\\)")
# unbroken base64ish / hex / hash runs that carry no meaning for a model
BLOB_RE = re.compile(r"[A-Za-z0-9+/_-]{64,}={0,2}")
# used to decide two lines are "the same" modulo counters/ids/timestamps
NORM_SUBS = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"\b\d+\b"), "<n>"),
]
DEFAULT_ERRORS_RE = (
    r"(?i)\b(error|err!|fail(ed|ure)?|exception|traceback|fatal|panic"
    r"|assert(ion)?|segfault|denied|refused|timeout|timed out|unhandled"
    r"|cannot|not found|undefined|null pointer|stack trace)\b"
)


def estimate_tokens(text):
    return len(text) // 4


def normalize(line):
    for rx, repl in NORM_SUBS:
        line = rx.sub(repl, line)
    return line


def pack_lines(lines, args, keep_res):
    out = []

    def emit(line):
        if args.max_line and len(line) > args.max_line:
            line = line[: args.max_line] + f" …[+{len(line) - args.max_line} chars]"
        out.append(line)

    # 1. strip ANSI, trailing whitespace; squash blobs
    cleaned = []
    for line in lines:
        line = ANSI_RE.sub("", line).rstrip()
        if not args.no_blobs:
            line = BLOB_RE.sub(lambda m: f"<blob:{len(m.group(0))} chars>", line)
        cleaned.append(line)

    # 2. --errors-only: keep matching lines ± context, plus --keep lines
    if args.errors_only:
        err_re = re.compile(args.errors_regex)
        keep_idx = set()
        for i, line in enumerate(cleaned):
            if err_re.search(line) or any(r.search(line) for r in keep_res):
                keep_idx.update(range(max(0, i - args.context),
                                      min(len(cleaned), i + args.context + 1)))
        selected, last = [], None
        for i in sorted(keep_idx):
            if last is not None and i > last + 1:
                selected.append(f"⋮ [{i - last - 1} lines skipped]")
            selected.append(cleaned[i])
            last = i
        if last is not None and last < len(cleaned) - 1:
            selected.append(f"⋮ [{len(cleaned) - 1 - last} lines skipped]")
        cleaned = selected

    # 3. collapse blank runs and consecutive near-duplicates
    prev_norm, run_first, run_count, blank_run = None, None, 0, 0

    def flush_run():
        nonlocal run_first, run_count, prev_norm
        if run_first is not None:
            emit(run_first)
            if run_count > 1:
                emit(f"  [previous line repeated {run_count - 1} more times]")
        run_first, run_count, prev_norm = None, 0, None

    for line in cleaned:
        if not line:
            flush_run()
            blank_run += 1
            if blank_run == 1:
                out.append("")
            continue
        blank_run = 0
        if args.no_dedupe or any(r.search(line) for r in keep_res):
            flush_run()
            emit(line)
            continue
        norm = normalize(line)
        if norm == prev_norm:
            run_count += 1
        else:
            flush_run()
            run_first, run_count, prev_norm = line, 1, norm
    flush_run()

    # 4. hard token budget: keep head + tail
    if args.max_tokens:
        text = "\n".join(out)
        if estimate_tokens(text) > args.max_tokens:
            budget_chars = args.max_tokens * 4
            head_chars = budget_chars * 6 // 10   # errors usually cluster at the end,
            tail_chars = budget_chars - head_chars  # but the head sets the scene
            head, tail, dropped = [], [], 0
            n = 0
            for line in out:
                if n + len(line) <= head_chars:
                    head.append(line)
                    n += len(line) + 1
                else:
                    break
            n = 0
            for line in reversed(out[len(head):]):
                if n + len(line) <= tail_chars:
                    tail.append(line)
                    n += len(line) + 1
                else:
                    break
            dropped = len(out) - len(head) - len(tail)
            if dropped > 0:
                out = head + [f"⋮ [{dropped} lines elided to fit "
                              f"~{args.max_tokens} token budget]"] + list(reversed(tail))

    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--errors-only", action="store_true")
    ap.add_argument("-C", "--context", type=int, default=3)
    ap.add_argument("--errors-regex", default=DEFAULT_ERRORS_RE)
    ap.add_argument("--keep", action="append", default=[])
    ap.add_argument("--max-line", type=int, default=400)
    ap.add_argument("--max-tokens", type=int, default=0)
    ap.add_argument("--no-dedupe", action="store_true")
    ap.add_argument("--no-blobs", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        keep_res = [re.compile(p) for p in args.keep]
        re.compile(args.errors_regex)
    except re.error as e:
        sys.exit(f"error: bad regex: {e}")

    sources = []
    if args.files:
        for path in args.files:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    sources.append((path, fh.read()))
            except OSError as e:
                sys.exit(f"error: {e}")
    else:
        sources.append(("stdin", sys.stdin.read()))

    in_chars = sum(len(t) for _, t in sources)
    in_lines = sum(t.count("\n") + 1 for _, t in sources)

    chunks = []
    for name, text in sources:
        packed = pack_lines(text.splitlines(), args, keep_res)
        if len(sources) > 1:
            chunks.append(f"==> {name} <==")
        chunks.extend(packed)
    result = "\n".join(chunks)
    sys.stdout.write(result + ("\n" if result else ""))

    if not args.quiet:
        out_chars = len(result)
        saved = 100.0 * (1 - out_chars / in_chars) if in_chars else 0.0
        print(
            f"ctx-pack: {in_chars:,} chars / {in_lines:,} lines "
            f"(~{in_chars // 4:,} tok) -> "
            f"{out_chars:,} chars / {len(chunks):,} lines "
            f"(~{estimate_tokens(result):,} tok)  saved {saved:.1f}%",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
