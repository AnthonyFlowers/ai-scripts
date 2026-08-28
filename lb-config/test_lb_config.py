#!/usr/bin/env python3
"""Tests for lb_config.py — zero third-party deps (stdlib unittest).

Run: python3 lb-config/test_lb_config.py
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lb_config  # noqa: E402


def make_app(tmp):
    """A LoopBack server/ dir: base config + a production override + a datasource."""
    server = os.path.join(tmp, "server")
    os.makedirs(server)

    # base config
    with open(os.path.join(server, "config.json"), "w") as fh:
        json.dump({
            "restApiRoot": "/api",
            "host": "0.0.0.0",
            "port": 3000,
            "legacyExplorer": False,
            "remoting": {"rest": {"handleErrors": False}},
        }, fh)

    # production override: the env layer must win over the base
    with open(os.path.join(server, "config.production.json"), "w") as fh:
        json.dump({"port": 8080}, fh)

    # an executable local.js — must be flagged, never evaluated
    with open(os.path.join(server, "config.local.js"), "w") as fh:
        fh.write("module.exports = { port: 9999 };\n")

    # datasource with a real secret in the input
    with open(os.path.join(server, "datasources.json"), "w") as fh:
        json.dump({
            "db": {
                "name": "db",
                "connector": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "mydb",
                "user": "admin",
                "password": "s3cr3t-should-never-appear",
            }
        }, fh)

    # a component
    with open(os.path.join(server, "component-config.json"), "w") as fh:
        json.dump({"loopback-component-explorer": {"mountPath": "/explorer"}}, fh)

    return tmp


class TestLbConfig(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        make_app(self.tmp)
        self.app = lb_config.load_app_config(self.tmp, "production")
        self.files, self.report = lb_config.build_outputs(self.app)

    # --- env layering ---

    def test_env_override_wins(self):
        # base port 3000, production override 8080 -> merged value is 8080
        self.assertEqual(self.app["app"]["port"], 8080)

    def test_env_default_selection(self):
        self.assertEqual(self.app["env"], "production")
        app_cfg = self.files["config/app.config.ts"]
        # port default in the factory reflects the merged (overridden) value
        self.assertIn("PORT ?? '8080'", app_cfg)

    def test_js_variant_flagged_not_evaluated(self):
        self.assertIn("config.local.js", self.app["js"]["config"])
        # 9999 from the .js file must NOT leak into merged config or output
        self.assertNotEqual(self.app["app"].get("port"), 9999)
        self.assertNotIn("9999", self.files["config/app.config.ts"])
        self.assertIn("config.local.js", self.report)

    # --- secrets ---

    def test_password_becomes_env_reference(self):
        db = self.files["config/database.config.ts"]
        self.assertIn("password: process.env.DB_PASSWORD", db)

    def test_secret_literal_never_emitted(self):
        secret = "s3cr3t-should-never-appear"
        for name, content in self.files.items():
            self.assertNotIn(secret, content, f"secret leaked into {name}")

    def test_env_example_lists_password(self):
        env = self.files[".env.example"]
        self.assertIn("DB_PASSWORD=", env)
        # placeholder, not the real value
        self.assertNotIn("s3cr3t-should-never-appear", env)
        self.assertIn("secret", env.lower())

    # --- mappings ---

    def test_datasource_namespace_and_fields(self):
        db = self.files["config/database.config.ts"]
        self.assertIn("registerAs('database'", db)
        self.assertIn("DB_HOST", db)
        self.assertIn("DB_PORT", db)
        self.assertIn("DB_DATABASE", db)
        self.assertIn("type: \"postgres\"", db)  # connector -> type

    def test_rest_api_root_becomes_global_prefix(self):
        app_cfg = self.files["config/app.config.ts"]
        self.assertIn("globalPrefix", app_cfg)
        self.assertIn("API_PREFIX ?? \"api\"", app_cfg)
        self.assertIn("setGlobalPrefix", self.report)

    def test_component_analogue_in_report(self):
        self.assertIn("loopback-component-explorer", self.report)
        self.assertIn("@nestjs/swagger", self.report)

    def test_configuration_aggregates(self):
        conf = self.files["config/configuration.ts"]
        self.assertIn("appConfig()", conf)
        self.assertIn("databaseConfig()", conf)

    def test_wiring_shown(self):
        self.assertIn("ConfigModule.forRoot", self.report)
        self.assertIn("load: [", self.report)

    # --- multiple datasources -> multiple namespaces ---

    def test_multiple_datasources(self):
        with open(os.path.join(self.app["server_dir"], "datasources.json"), "w") as fh:
            json.dump({
                "db": {"connector": "postgresql", "host": "h", "database": "d"},
                "cache": {"connector": "redis", "host": "r", "password": "p"},
            }, fh)
        app = lb_config.load_app_config(self.tmp, "production")
        files, _ = lb_config.build_outputs(app)
        db = files["config/database.config.ts"]
        self.assertIn("registerAs('database'", db)
        self.assertIn("registerAs('database_cache'", db)
        self.assertIn("CACHE_PASSWORD", db)

    # --- write behavior ---

    def test_refuses_non_empty_dir_without_force(self):
        out = tempfile.mkdtemp()
        with open(os.path.join(out, "x.txt"), "w") as fh:
            fh.write("existing")
        with self.assertRaises(lb_config.MigrationError):
            lb_config.write_files(self.files, out, force=False, dry_run=False)
        # force overwrites
        written = lb_config.write_files(self.files, out, force=True, dry_run=False)
        self.assertTrue(any("app.config.ts" in w for w in written))

    def test_dry_run_writes_nothing(self):
        out = tempfile.mkdtemp()
        written = lb_config.write_files(self.files, out, force=False, dry_run=True)
        self.assertTrue(written)
        self.assertFalse(os.path.exists(os.path.join(out, "config", "app.config.ts")))


if __name__ == "__main__":
    unittest.main()
