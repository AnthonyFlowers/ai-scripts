# ctx-pack

Shrink logs, CI output, and dumps before pasting them into an LLM. Most of
a build log is noise a model pays for by the token: ANSI color codes,
thousands of near-identical retry lines, base64 blobs, boilerplate far from
the error. `ctx-pack` strips that deterministically and reports how much it
saved.

Python 3.9+, no dependencies.

## Usage

```sh
./ctx_pack.py build.log                      # pack a file → stdout
make 2>&1 | ./ctx_pack.py                    # pack a pipe
./ctx_pack.py --errors-only -C 5 ci.log      # only error lines ± 5 lines context
./ctx_pack.py --max-tokens 8000 huge.log     # hard budget: keep head + tail
./ctx_pack.py --keep 'my_service' app.log    # matching lines always survive
```

The packed text goes to stdout; a stats line goes to stderr:

```
ctx-pack: 1,842,113 chars / 24,551 lines (~460,528 tok) -> 41,092 chars / 512 lines (~10,273 tok)  saved 97.8%
```

| Flag | Meaning |
|------|---------|
| `--errors-only` | keep only lines matching the error pattern, plus context |
| `-C, --context N` | context lines in `--errors-only` (default 3) |
| `--errors-regex RE` | replace the built-in error pattern |
| `--keep RE` | lines matching RE are always kept verbatim (repeatable) |
| `--max-line N` | truncate lines longer than N chars (default 400, 0 = off) |
| `--max-tokens N` | elide the middle if the result still exceeds ~N tokens |
| `--no-dedupe` / `--no-blobs` | disable those passes |
| `--quiet` | no stats line |

## What it does, in order

1. Strips ANSI escapes and trailing whitespace; squashes unbroken
   base64/hex runs ≥ 64 chars to `<blob:N chars>`.
2. `--errors-only`: keeps lines matching the error pattern (or any `--keep`
   pattern) ± N context lines, with `⋮ [N lines skipped]` markers between
   islands.
3. Collapses blank-line runs and consecutive lines that are identical after
   normalizing timestamps/hex/numbers — 500 retry lines become one line plus
   `[previous line repeated 499 more times]`. `--keep` lines are exempt.
4. Truncates overlong lines.
5. `--max-tokens`: if still over budget, keeps the head (~60%) and tail
   (~40%) and elides the middle — errors usually cluster at the end, the
   head sets the scene.

Token estimates are chars/4: rough, but right at the "did this get 10×
smaller?" granularity that matters.

## Tests

```sh
python3 ctx-pack/test_ctx_pack.py
```
