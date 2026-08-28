#!/usr/bin/env python3
"""Tests for lb_acl.py — no third-party deps, run with `python3 test_lb_acl.py`.

Builds a throwaway LoopBack app in a temp dir with a couple of models whose
ACLs exercise the interesting cases: a $everyone DENY lockdown, a $everyone
READ ALLOW, a named-role ALLOW EXECUTE on a specific method, a $owner WRITE,
and an $authenticated grant. Then asserts the resolved matrix and the
generated guard/decorator content.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lb_acl  # noqa: E402


# Classic LoopBack "review.json" lockdown + selective grants, plus an
# EXECUTE-on-a-named-method role grant and an owner write.
REVIEW_MODEL = {
    "name": "Review",
    "base": "PersistedModel",
    "properties": {"content": "string"},
    "acls": [
        {"accessType": "*", "principalType": "ROLE",
         "principalId": "$everyone", "permission": "DENY"},
        {"accessType": "READ", "principalType": "ROLE",
         "principalId": "$everyone", "permission": "ALLOW"},
        {"accessType": "EXECUTE", "principalType": "ROLE",
         "principalId": "admin", "permission": "ALLOW", "property": "approve"},
        {"accessType": "WRITE", "principalType": "ROLE",
         "principalId": "$owner", "permission": "ALLOW"},
    ],
}

# A model with no ACLs (should surface in the checklist).
NOTE_MODEL = {"name": "Note", "base": "PersistedModel",
              "properties": {"body": "string"}, "acls": []}


class LbAclTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lb-acl-test-")
        models_dir = os.path.join(self.root, "common", "models")
        os.makedirs(models_dir)
        for model in (REVIEW_MODEL, NOTE_MODEL):
            with open(os.path.join(models_dir, model["name"].lower() + ".json"),
                      "w", encoding="utf-8") as fh:
                json.dump(model, fh)
        self.app = lb_acl.load_app(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # --- parsing ---------------------------------------------------------

    def test_loads_both_models_and_acls(self):
        names = sorted(m["name"] for m in self.app["models"])
        self.assertEqual(names, ["Note", "Review"])
        review = next(m for m in self.app["models"] if m["name"] == "Review")
        self.assertEqual(len(review["acls"]), 4)

    # --- resolution / matrix --------------------------------------------

    def _review_scopes(self):
        matrix = lb_acl.build_matrix(self.app)
        return matrix["models"]["Review"]["scopes"]

    def test_everyone_read_is_public(self):
        read = self._review_scopes()["READ"]
        self.assertTrue(read["public"])
        self.assertIn("$everyone", read["allowedPrincipals"])
        self.assertEqual(read["annotations"], ["@Public()"])

    def test_write_is_owner_only_and_flagged(self):
        write = self._review_scopes()["WRITE"]
        self.assertFalse(write["public"])
        self.assertIn("$owner", write["allowedPrincipals"])
        # $everyone DENY (from the "*" entry) locks the scope down for others
        self.assertIn("$everyone", write["deniedPrincipals"])
        self.assertTrue(any("OwnerGuard" in a for a in write["annotations"]))
        self.assertTrue(any("$owner" in f for f in write["flags"]))

    def test_execute_named_method_grants_role(self):
        scopes = self._review_scopes()
        self.assertIn("METHOD:approve", scopes)
        approve = scopes["METHOD:approve"]
        self.assertEqual(approve["allowedPrincipals"], ["admin"])
        self.assertIn("@Roles('admin')", approve["annotations"])
        self.assertIn("@UseGuards(RolesGuard)", approve["annotations"])

    def test_everyone_deny_locks_generic_execute(self):
        # The bare "*" DENY covers the generic EXECUTE bucket with no ALLOW.
        execute = self._review_scopes()["EXECUTE"]
        self.assertFalse(execute["public"])
        self.assertTrue(execute["lockedDown"])
        self.assertEqual(execute["allowedPrincipals"], [])

    def test_no_acl_model_in_checklist(self):
        matrix = lb_acl.build_matrix(self.app)
        joined = " ".join(matrix["manualReviewChecklist"])
        self.assertIn("Note", joined)
        self.assertIn("no ACLs", joined)

    # --- generated scaffold ---------------------------------------------

    def test_generated_guard_and_decorators(self):
        files = lb_acl.build_files(self.app)
        self.assertIn("auth/roles.guard.ts", files)
        self.assertIn("auth/roles.decorator.ts", files)
        self.assertIn("auth/public.decorator.ts", files)
        self.assertIn("auth/owner.guard.ts", files)

        guard = files["auth/roles.guard.ts"]
        self.assertIn("implements CanActivate", guard)
        self.assertIn("getAllAndOverride", guard)
        self.assertIn("IS_PUBLIC_KEY", guard)
        self.assertIn("ROLES_KEY", guard)

        decorator = files["auth/roles.decorator.ts"]
        self.assertIn("export const ROLES_KEY = 'roles';", decorator)
        self.assertIn("SetMetadata(ROLES_KEY, roles)", decorator)

        public = files["auth/public.decorator.ts"]
        self.assertIn("IS_PUBLIC_KEY", public)
        self.assertIn("SetMetadata(IS_PUBLIC_KEY, true)", public)

    def test_matrix_json_is_valid_and_structured(self):
        files = lb_acl.build_files(self.app)
        parsed = json.loads(files["permission-matrix.json"])
        self.assertIn("Review", parsed["models"])
        self.assertIn("scopes", parsed["models"]["Review"])

    def test_report_renders(self):
        report = lb_acl.build_report(self.app)
        self.assertIn("# LoopBack ACLs -> NestJS authorization", report)
        self.assertIn("Review", report)
        self.assertIn("Resolved permission matrix", report)
        self.assertIn("@Public()", report)

    # --- writing ---------------------------------------------------------

    def test_write_refuses_nonempty_without_force(self):
        out = os.path.join(self.root, "out")
        os.makedirs(out)
        with open(os.path.join(out, "sentinel"), "w") as fh:
            fh.write("x")
        files = lb_acl.build_files(self.app)
        with self.assertRaises(lb_acl.AclError):
            lb_acl.write_files(files, out, force=False, dry_run=False)
        # force succeeds
        written = lb_acl.write_files(files, out, force=True, dry_run=False)
        self.assertTrue(any(w.endswith("roles.guard.ts") for w in written))
        self.assertTrue(os.path.exists(os.path.join(out, "auth", "roles.guard.ts")))

    def test_dry_run_writes_nothing(self):
        out = os.path.join(self.root, "dry")
        files = lb_acl.build_files(self.app)
        written = lb_acl.write_files(files, out, force=False, dry_run=True)
        self.assertTrue(written)
        self.assertFalse(os.path.exists(out))

    # --- single-file input ----------------------------------------------

    def test_single_model_file(self):
        path = os.path.join(self.root, "common", "models", "review.json")
        app = lb_acl.load_app(path)
        self.assertEqual(len(app["models"]), 1)
        self.assertEqual(app["models"][0]["name"], "Review")


if __name__ == "__main__":
    unittest.main(verbosity=2)
