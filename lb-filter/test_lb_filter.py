#!/usr/bin/env python3
"""test_lb_filter.py — checks for the lb_filter.py generator.

No third-party deps. Because the emitted artifact is TypeScript (which we can't
compile here), these tests validate the emitter's OUTPUT: that every operator
mapping, class name, and import is present; that braces/parens balance in each
generated .ts file; and that the CLI's write/force/dry-run/stdout behaviour is
correct.

Run:  python3 lb-filter/test_lb_filter.py
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lb_filter  # noqa: E402


def _balanced(text, open_ch, close_ch):
    """True if open/close chars balance (ignoring that they may sit in strings;
    valid TS keeps them balanced overall, so a mismatch flags a template typo)."""
    return text.count(open_ch) == text.count(close_ch)


class EmitterContentTests(unittest.TestCase):
    def setUp(self):
        self.files = lb_filter.build_files()
        self.conv = self.files["loopback-filter.converter.ts"]
        self.pipe = self.files["loopback-filter.pipe.ts"]
        self.types = self.files["loopback-filter.types.ts"]
        self.index = self.files["index.ts"]

    def test_all_files_present(self):
        for name in ("loopback-filter.types.ts", "loopback-filter.converter.ts",
                     "loopback-filter.pipe.ts", "index.ts"):
            self.assertIn(name, self.files)
            self.assertTrue(self.files[name].strip())

    def test_operator_mappings_present(self):
        # Each LoopBack operator must appear as a switch case wired to the
        # documented TypeORM operator.
        pairs = {
            "gt": "MoreThan(arg as any)",
            "gte": "MoreThanOrEqual(arg as any)",
            "lt": "LessThan(arg as any)",
            "lte": "LessThanOrEqual(arg as any)",
            "between": "Between(arg[0], arg[1])",
            "inq": "In(arg)",
            "nin": "Not(In(arg))",
            "neq": "Not(arg as any)",
            "like": "Like(arg as string)",
            "nlike": "Not(caseInsensitive ? ILike(arg as string) : Like(arg as string))",
            "ilike": "ILike(arg as string)",
            "nilike": "Not(ILike(arg as string))",
        }
        for op, rhs in pairs.items():
            self.assertIn(f"case '{op}':", self.conv, f"missing case for {op}")
            self.assertIn(rhs, self.conv, f"missing mapping {op} -> {rhs}")

    def test_operator_map_table_in_sync(self):
        # The Python-side OPERATOR_MAP is the documented table; every supported
        # operator it lists (minus and/or, which aren't switch cases) must have
        # a switch case in the emitted converter.
        for op in lb_filter.OPERATOR_MAP:
            if op in ("and", "or"):
                self.assertIn(f"'{op}'", self.conv)
            else:
                self.assertIn(f"case '{op}':", self.conv)

    def test_and_or_composition(self):
        # OR -> where array; AND -> merged clause via And()/mergeClause.
        self.assertIn("whereToClauses", self.conv)
        self.assertIn("andCombine", self.conv)
        self.assertIn("mergeClause", self.conv)
        self.assertIn("And(...operators)", self.conv)
        # `or` and `and` are handled as where keys, not operator switch cases:
        self.assertNotIn("case 'or':", self.conv)
        self.assertNotIn("case 'and':", self.conv)
        self.assertIn("if (key === 'and')", self.conv)
        self.assertIn("} else if (key === 'or')", self.conv)

    def test_include_order_limit_skip_fields(self):
        self.assertIn("options.relations = relations;", self.conv)
        self.assertIn("collectRelations", self.conv)
        self.assertIn("options.order = convertOrder", self.conv)
        self.assertIn("options.take = Number(parsed.limit)", self.conv)
        self.assertIn("options.skip = Number(parsed.skip ?? parsed.offset)", self.conv)
        self.assertIn("options.select = convertFields", self.conv)

    def test_string_or_object_filter(self):
        self.assertIn("typeof filter === 'string'", self.conv)
        self.assertIn("JSON.parse(filter)", self.conv)

    def test_unsupported_operators_throw(self):
        for op in lb_filter.UNSUPPORTED_OPERATORS:
            self.assertIn(f"case '{op}':", self.conv)
            self.assertIn(f"'{op}'", self.conv)
        self.assertIn("regexp", self.conv)
        self.assertIn("near", self.conv)
        self.assertIn("LoopbackFilterError", self.conv)

    def test_class_and_error_names(self):
        self.assertIn("export class LoopbackFilterError extends Error", self.conv)
        self.assertIn("export function toFindManyOptions", self.conv)
        self.assertIn("export class LoopbackFilterPipe", self.pipe)
        self.assertIn("implements", self.pipe)
        self.assertIn("PipeTransform", self.pipe)

    def test_imports(self):
        # converter pulls the right TypeORM operators
        for sym in ("And", "Between", "Equal", "FindManyOptions", "FindOperator",
                    "FindOptionsOrder", "FindOptionsWhere", "ILike", "In", "IsNull",
                    "LessThan", "LessThanOrEqual", "Like", "MoreThan",
                    "MoreThanOrEqual", "Not"):
            self.assertIn(sym, self.conv)
        self.assertIn("from 'typeorm';", self.conv)
        # pipe pulls NestJS symbols
        self.assertIn("from '@nestjs/common';", self.pipe)
        self.assertIn("Injectable", self.pipe)
        self.assertIn("BadRequestException", self.pipe)
        self.assertIn("PipeTransform", self.pipe)
        self.assertIn("from 'typeorm';", self.pipe)
        self.assertIn("./loopback-filter.converter", self.pipe)

    def test_types_shape(self):
        for key in ("where?", "include?", "order?", "limit?", "skip?", "offset?",
                    "fields?"):
            self.assertIn(key, self.types)
        self.assertIn("export interface LoopbackFilter", self.types)

    def test_index_barrel(self):
        self.assertIn("export * from './loopback-filter.types'", self.index)
        self.assertIn("export * from './loopback-filter.converter'", self.index)
        self.assertIn("export * from './loopback-filter.pipe'", self.index)

    def test_braces_and_parens_balanced(self):
        for name, content in self.files.items():
            self.assertTrue(_balanced(content, "{", "}"),
                            f"unbalanced braces in {name}")
            self.assertTrue(_balanced(content, "(", ")"),
                            f"unbalanced parens in {name}")
            self.assertTrue(_balanced(content, "[", "]"),
                            f"unbalanced brackets in {name}")


class FixtureTests(unittest.TestCase):
    """A representative LoopBack filter, checked against expected emitted logic.

    We can't run the TypeScript, but we can assert that the emitted converter
    contains the exact code paths this fixture would exercise end to end.
    """

    FIXTURE = {
        "where": {
            "or": [
                {"city": "NYC"},
                {"and": [{"rating": {"gte": 4}}, {"price": {"lt": 20}}]},
            ],
        },
        "include": [{"relation": "reviews", "scope": {"include": "author"}}],
        "order": ["name ASC", "rating DESC"],
        "limit": 10,
        "skip": 20,
        "fields": ["id", "name"],
    }

    def test_fixture_paths_exist(self):
        conv = lb_filter.build_files()["loopback-filter.converter.ts"]
        # where: or/and both handled
        self.assertIn("case 'gte':", conv)
        self.assertIn("case 'lt':", conv)
        self.assertIn("if (key === 'and')", conv)
        self.assertIn("} else if (key === 'or')", conv)
        # include with { relation, scope: { include } }
        self.assertIn("obj.relation", conv)
        self.assertIn("scope.include", conv)
        # order array with ASC/DESC parsing
        self.assertIn("=== 'DESC' ? 'DESC' : 'ASC'", conv)
        # limit/skip/fields
        self.assertIn("options.take = Number(parsed.limit)", conv)
        self.assertIn("options.skip = Number", conv)
        self.assertIn("convertFields", conv)

    def test_fixture_is_valid_json_roundtrip(self):
        import json
        # The emitted code JSON.parses string filters; make sure our fixture is
        # the kind of object that survives a JSON string round-trip.
        s = json.dumps(self.FIXTURE)
        self.assertEqual(json.loads(s), self.FIXTURE)


class CliTests(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lb_filter.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_writes_files(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = os.path.join(d, "gen")
            code, _, _ = self._run(["-o", out_dir])
            self.assertEqual(code, 0)
            for name in lb_filter.build_files():
                self.assertTrue(os.path.isfile(os.path.join(out_dir, name)))

    def test_refuses_non_empty_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            code1, _, _ = self._run(["-o", d])  # d already exists (empty ok on 1st)
            self.assertEqual(code1, 0)
            # now non-empty; second run must refuse
            code2, _, err = self._run(["-o", d])
            self.assertEqual(code2, 1)
            self.assertIn("not empty", err)

    def test_force_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            self._run(["-o", d])
            code, _, _ = self._run(["-o", d, "--force"])
            self.assertEqual(code, 0)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = os.path.join(d, "gen")
            code, out, _ = self._run(["-o", out_dir, "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("would write", out)
            self.assertFalse(os.path.isdir(out_dir))

    def test_stdout(self):
        code, out, _ = self._run(["--stdout"])
        self.assertEqual(code, 0)
        self.assertIn("// ==== loopback-filter.converter.ts ====", out)
        self.assertIn("export class LoopbackFilterPipe", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
