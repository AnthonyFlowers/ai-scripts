#!/usr/bin/env python3
"""Tests for ctx_pack. Run: python3 ctx-pack/test_ctx_pack.py"""

import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("ctx_pack.py")


def run(args, stdin):
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "--quiet", *args],
        input=stdin, capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f"exit {p.returncode}: {p.stderr}")
    return p.stdout


class TestCtxPack(unittest.TestCase):
    def test_strips_ansi(self):
        out = run([], "\x1b[31mred error\x1b[0m text\n")
        self.assertEqual(out, "red error text\n")

    def test_collapses_duplicate_lines_modulo_numbers(self):
        stdin = "\n".join(f"2026-08-27T01:00:{i:02d}Z retry attempt {i}" for i in range(50))
        out = run([], stdin)
        self.assertIn("repeated 49 more times", out)
        self.assertEqual(len(out.splitlines()), 2)

    def test_squashes_blobs(self):
        blob = "A" * 200
        out = run([], f"payload={blob}\n")
        self.assertIn("<blob:200 chars>", out)
        self.assertNotIn(blob, out)

    def test_truncates_long_lines(self):
        out = run(["--max-line", "10", "--no-blobs"], "abcdefghijKLMNOP\n")
        self.assertTrue(out.startswith("abcdefghij …[+6 chars]"))

    def test_errors_only_keeps_context(self):
        lines = [f"line {i}" for i in range(20)]
        lines[10] = "something FAILED here"
        out = run(["--errors-only", "-C", "1"], "\n".join(lines))
        body = out.splitlines()
        self.assertIn("line 9", body)
        self.assertIn("something FAILED here", body)
        self.assertIn("line 11", body)
        self.assertNotIn("line 5", body)
        self.assertTrue(any("lines skipped" in l for l in body))

    def test_keep_regex_survives_dedupe(self):
        stdin = "keepme 1\nkeepme 2\nkeepme 3\n"
        out = run(["--keep", "keepme"], stdin)
        self.assertEqual(out.splitlines(), ["keepme 1", "keepme 2", "keepme 3"])

    def test_max_tokens_elides_middle(self):
        stdin = "\n".join(f"unique line number {i} with some padding text" for i in range(2000))
        out = run(["--max-tokens", "500", "--no-dedupe"], stdin)
        self.assertIn("elided to fit", out)
        self.assertLess(len(out), len(stdin) // 2)
        self.assertIn("unique line number 0 ", out)
        self.assertIn("unique line number 1999 ", out)

    def test_blank_runs_collapse(self):
        out = run([], "a\n\n\n\n\nb\n")
        self.assertEqual(out, "a\n\nb\n")


if __name__ == "__main__":
    unittest.main()
