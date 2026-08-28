#!/usr/bin/env python3
"""lb_config.py — convert LoopBack 3 server config into a NestJS 12 ConfigModule setup.

A LoopBack 3 app configures itself through data files under `server/`:

  * `config.json`            — app-level settings (host, port, restApiRoot,
                               remoting, legacyExplorer, ...), layered per
                               environment by `config.local.{json,js}` and
                               `config.<NODE_ENV>.{json,js}`.
  * `datasources.json`       — one object per named datasource (connector,
                               host, port, database, user, password, ...),
                               layered the same way.
  * `component-config.json`  — LoopBack components mounted at boot
                               (loopback-component-explorer, ...).

Because that configuration is declarative JSON, most of the port to NestJS'
`@nestjs/config` is a deterministic transform. This script reads the files,
merges the environment layering the way loopback-boot does (base <-
`.local.json` <- `.<env>.json`, later wins; `.js` variants execute code and
are flagged for a human), and generates:

  * `config/app.config.ts`      — `registerAs('app', ...)` (host, port, and
                                  the global prefix derived from restApiRoot).
  * `config/database.config.ts` — one `registerAs` namespace per datasource,
                                  connection fields read from env vars.
  * `config/configuration.ts`   — a default factory aggregating the above.
  * `.env.example`              — every env var the config references, with
                                  placeholders (never real secret values).
  * `CONFIG_MIGRATION.md`       — what maps where, plus a manual-attention
                                  checklist (executable `.js` config, custom
                                  components, strong-remoting options).

Sensitive values (password, secret, token, apiKey) are ALWAYS emitted as
`process.env.X` references with a placeholder in `.env.example` — the real
value from the input is never copied into generated code.

Python 3.9+, no dependencies. Emits TypeScript; nothing here runs Node.

Usage:
  ./lb_config.py <app-dir>                      # print the migration report
  ./lb_config.py <app-dir> --env production     # pick the NODE_ENV layer
  ./lb_config.py <app-dir> -o ./nest-config     # write the generated files
  ./lb_config.py <app-dir> -o ./out --dry-run   # list files, write nothing
  ./lb_config.py <app-dir> -o ./out --force     # overwrite a non-empty dir

<app-dir> may be a LoopBack app root (config found under `server/`) or the
`server/` directory itself.

Options:
  --env ENV        NODE_ENV layer to merge (default: $NODE_ENV or development)
  -o, --out DIR    write generated files here (default: print report to stdout)
  --force          overwrite an existing non-empty output directory
  --dry-run        with -o, list files that would be written; write nothing

Exit codes: 0 ok, 1 bad args / no config found / refusing to overwrite.
"""

import argparse
import json
import os
import re
import sys


class MigrationError(Exception):
    pass


# ---------------------------------------------------------------------------
# Reference tables
# ---------------------------------------------------------------------------

# LoopBack connector -> a suggested TypeORM/driver `type` string (best effort).
CONNECTOR_TYPE = {
    "mysql": "mysql",
    "postgresql": "postgres",
    "postgres": "postgres",
    "mssql": "mssql",
    "sqlserver": "mssql",
    "oracle": "oracle",
    "mongodb": "mongodb",
    "sqlite3": "sqlite",
    "memory": "sqlite",
    "redis": "redis",
}

# LoopBack component npm module -> its usual NestJS analogue.
COMPONENT_ANALOGUE = {
    "loopback-component-explorer":
        "@nestjs/swagger (Swagger UI at a mount path via SwaggerModule.setup)",
    "loopback-component-passport":
        "@nestjs/passport + passport strategies",
    "loopback-component-storage":
        "@nestjs/platform-express Multer, or a dedicated storage module",
    "loopback-component-push":
        "a custom provider (e.g. firebase-admin) — no direct Nest equivalent",
    "loopback-component-oauth2":
        "@nestjs/passport with an OAuth2 strategy",
}

# config.json keys handled explicitly; everything else scalar is passed through.
KNOWN_APP_KEYS = {"host", "port", "restApiRoot", "remoting", "legacyExplorer"}

# Substrings (in a normalized key) that mark a value as sensitive.
SENSITIVE_HINTS = ("password", "passwd", "pwd", "apikey", "secret", "token")

# Datasource keys treated as the connection fields, in emit order.
# loopback key -> (ts field name, env suffix, kind)  kind in {str,int,secret}
DS_FIELDS = [
    ("host", ("host", "HOST", "str")),
    ("port", ("port", "PORT", "int")),
    ("url", ("url", "URL", "secret")),        # may embed credentials
    ("database", ("database", "DATABASE", "str")),
    ("user", ("user", "USER", "str")),
    ("username", ("user", "USER", "str")),
    ("password", ("password", "PASSWORD", "secret")),
]


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------


def _words(name):
    """Split an identifier into lowercase words (handles camel, snake, kebab)."""
    s = re.sub(r"[_\-\s]+", " ", str(name))
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return [w for w in s.lower().split() if w]


def pascal(name):
    return "".join(w[:1].upper() + w[1:] for w in _words(name)) or "Config"


def camel(name):
    p = pascal(name)
    return p[:1].lower() + p[1:]


def snake(name):
    return "_".join(_words(name)) or "config"


def screaming(name):
    return snake(name).upper() or "CONFIG"


def is_sensitive(key):
    norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(h in norm for h in SENSITIVE_HINTS)


# ---------------------------------------------------------------------------
# Reading + environment layering
# ---------------------------------------------------------------------------


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise MigrationError(f"cannot read JSON {path}: {exc}")


def deep_merge(base, over):
    """Deep-merge two dicts, values in `over` winning (loopback-boot semantics)."""
    out = dict(base)
    for key, val in over.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_layer(server_dir, base_name, env):
    """Merge base_name.json <- .local.json <- .<env>.json (later wins).

    Returns (merged_dict, loaded_json_files, executable_js_files). The `.js`
    variants are recorded but never executed — they are flagged for a human.
    """
    merged = {}
    loaded, js_files = [], []
    # precedence low -> high (later overrides earlier), mirroring loopback-boot:
    #   base.json  <  base.local.json  <  base.<env>.json
    json_order = [
        f"{base_name}.json",
        f"{base_name}.local.json",
        f"{base_name}.{env}.json",
    ]
    for fname in json_order:
        path = os.path.join(server_dir, fname)
        if os.path.isfile(path):
            data = _read_json(path)
            if isinstance(data, dict):
                merged = deep_merge(merged, data)
            loaded.append(fname)
    for fname in (f"{base_name}.local.js", f"{base_name}.{env}.js"):
        if os.path.isfile(os.path.join(server_dir, fname)):
            js_files.append(fname)
    return merged, loaded, js_files


def find_server_dir(path):
    """Resolve the directory holding config.json / datasources.json."""
    if not os.path.isdir(path):
        raise MigrationError(f"not a directory: {path}")
    candidates = [os.path.join(path, "server"), path]
    for cand in candidates:
        for probe in ("config.json", "datasources.json", "component-config.json"):
            if os.path.isfile(os.path.join(cand, probe)):
                return cand
    raise MigrationError(
        f"no LoopBack config found under {path} "
        "(looked for config.json / datasources.json / component-config.json "
        "in the given dir and its server/ subdir)")


def load_app_config(path, env):
    """Read and env-merge every LoopBack config layer for the app."""
    server = find_server_dir(path)
    app_cfg, app_loaded, app_js = load_layer(server, "config", env)
    ds_cfg, ds_loaded, ds_js = load_layer(server, "datasources", env)
    comp_cfg, comp_loaded, comp_js = load_layer(server, "component-config", env)
    datasources = {k: v for k, v in ds_cfg.items()
                   if not k.startswith("_") and isinstance(v, dict)}
    components = {k: v for k, v in comp_cfg.items() if not k.startswith("_")}
    return {
        "server_dir": server,
        "env": env,
        "app": app_cfg,
        "datasources": datasources,
        "components": components,
        "loaded": {"config": app_loaded, "datasources": ds_loaded,
                   "component-config": comp_loaded},
        "js": {"config": app_js, "datasources": ds_js, "component-config": comp_js},
    }


# ---------------------------------------------------------------------------
# Value emit helpers
# ---------------------------------------------------------------------------


def env_expr(env_name, default, kind):
    """A TypeScript expression reading env var `env_name` (kind: str/int/secret)."""
    if kind == "secret":
        return f"process.env.{env_name}"          # required at runtime, no literal
    if kind == "int":
        d = default if default is not None else 0
        return f"parseInt(process.env.{env_name} ?? '{d}', 10)"
    return f"process.env.{env_name} ?? {json.dumps(default)}"


def ts_literal(value, indent):
    """Render a JSON-ish value as a TypeScript literal at the given indent."""
    return json.dumps(value, indent=2).replace("\n", "\n" + " " * indent)


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------


def build_app_config(app):
    """Return (env_entries, app_fields, passthrough_notes) for config.json."""
    cfg = app["app"]
    env_entries = []   # (section, env_name, placeholder, kind)
    fields = []        # (ts_field, expression, comment)
    notes = []         # object-valued keys deferred to the report

    port = cfg.get("port", 3000)
    host = cfg.get("host", "0.0.0.0")
    fields.append(("host", env_expr("HOST", host, "str"), None))
    fields.append(("port", env_expr("PORT", port, "int"), None))
    env_entries.append(("App", "HOST", host, "str"))
    env_entries.append(("App", "PORT", port, "int"))

    rest_root = cfg.get("restApiRoot", "/api")
    prefix = str(rest_root).lstrip("/") or "api"
    fields.append(("globalPrefix", env_expr("API_PREFIX", prefix, "str"),
                   "restApiRoot -> app.setGlobalPrefix(config.app.globalPrefix)"))
    env_entries.append(("App", "API_PREFIX", prefix, "str"))

    if "legacyExplorer" in cfg:
        notes.append(("legacyExplorer", cfg["legacyExplorer"],
                      "LoopBack's /explorer; use @nestjs/swagger SwaggerModule"))

    # Extra scalar keys pass through as literals / env refs; objects -> notes.
    for key, val in cfg.items():
        if key in KNOWN_APP_KEYS or key.startswith("_"):
            if key == "remoting":
                notes.append(("remoting", val,
                              "strong-remoting options (rest/json/cors/errorHandler) "
                              "— no direct Nest equivalent; configure Express/pipes"))
            continue
        if isinstance(val, (dict, list)):
            notes.append((key, val, "object/array value — review by hand"))
            continue
        env_name = screaming(key)
        if is_sensitive(key):
            fields.append((camel(key), env_expr(env_name, val, "secret"), None))
            env_entries.append(("App", env_name, None, "secret"))
        else:
            kind = "int" if isinstance(val, (int, float)) and not isinstance(val, bool) else "str"
            if isinstance(val, bool):
                fields.append((camel(key), json.dumps(val), "boolean flag (literal)"))
            else:
                fields.append((camel(key), env_expr(env_name, val, kind), None))
                env_entries.append(("App", env_name, val, kind))
    return env_entries, fields, notes


def render_app_config(fields):
    lines = ["import { registerAs } from '@nestjs/config';", "",
             "export default registerAs('app', () => ({"]
    for name, expr, comment in fields:
        c = f"  // {comment}\n" if comment else ""
        lines.append(f"{c}  {name}: {expr},")
    lines.append("}));")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Database config
# ---------------------------------------------------------------------------


def datasource_namespace(name, index):
    """First datasource -> 'database'; the rest -> 'database_<name>'."""
    return "database" if index == 0 else f"database_{snake(name)}"


def build_datasource(name, cfg, index):
    """Return the emit plan for one datasource."""
    prefix = screaming(name)
    ns = datasource_namespace(name, index)
    fields = []       # (ts_field, expression, comment)
    env_entries = []  # (env_name, placeholder, kind)
    seen_ts = set()

    connector = str(cfg.get("connector", "")).lower()
    if connector:
        fields.append(("connector", json.dumps(connector), None))
        mapped = CONNECTOR_TYPE.get(connector)
        if mapped:
            fields.append(("type", json.dumps(mapped),
                           "connector -> ORM driver type (verify)"))

    for lb_key, (ts_field, suffix, kind) in DS_FIELDS:
        if lb_key not in cfg or ts_field in seen_ts:
            continue
        seen_ts.add(ts_field)
        env_name = f"{prefix}_{suffix}"
        default = cfg[lb_key]
        # force sensitive-by-name even if the field list said otherwise
        k = "secret" if is_sensitive(lb_key) else kind
        fields.append((ts_field, env_expr(env_name, default, k), None))
        env_entries.append((env_name, None if k == "secret" else default, k))

    # remaining scalar keys not already handled
    handled = {"connector", "name"} | {lb for lb, _ in DS_FIELDS}
    for key, val in cfg.items():
        if key in handled or key.startswith("_"):
            continue
        if isinstance(val, (dict, list)):
            fields.append((camel(key), ts_literal(val, 2),
                           "structured value copied literally — review"))
            continue
        env_name = f"{prefix}_{screaming(key)}"
        if is_sensitive(key):
            fields.append((camel(key), env_expr(env_name, val, "secret"), None))
            env_entries.append((env_name, None, "secret"))
        elif isinstance(val, bool):
            fields.append((camel(key), json.dumps(val), "boolean flag (literal)"))
        else:
            kind = "int" if isinstance(val, (int, float)) else "str"
            fields.append((camel(key), env_expr(env_name, val, kind), None))
            env_entries.append((env_name, val, kind))

    return {"name": name, "namespace": ns, "prefix": prefix,
            "connector": connector, "fields": fields, "env": env_entries}


def render_database_config(plans):
    lines = ["import { registerAs } from '@nestjs/config';", ""]
    exports = []
    for i, plan in enumerate(plans):
        var = "databaseConfig" if i == 0 else f"{camel(plan['name'])}Config"
        exports.append((var, plan["namespace"], i == 0))
        lines.append(f"// datasource '{plan['name']}'"
                     + (f" (connector: {plan['connector']})" if plan["connector"] else ""))
        lines.append(f"export const {var} = registerAs('{plan['namespace']}', () => ({{")
        for name, expr, comment in plan["fields"]:
            c = f"  // {comment}\n" if comment else ""
            lines.append(f"{c}  {name}: {expr},")
        lines.append("}));")
        lines.append("")
    if exports:
        lines.append(f"export default {exports[0][0]};")
    return "\n".join(lines) + "\n", exports


def render_configuration(app_present, db_exports):
    """The aggregate default factory tying the namespaced factories together."""
    lines = []
    imports = []
    body = []
    if app_present:
        imports.append("import appConfig from './app.config';")
        body.append("  app: appConfig(),")
    for var, ns, is_default in db_exports:
        if is_default:
            imports.append("import databaseConfig from './database.config';")
            body.append("  database: databaseConfig(),")
        else:
            imports.append(f"import {{ {var} }} from './database.config';")
            body.append(f"  {ns}: {var}(),")
    lines.extend(imports)
    lines.append("")
    lines.append("// Aggregate factory: `ConfigModule.forRoot({ load: [configuration] })`.")
    lines.append("// Equivalent to loading the namespaced factories individually.")
    lines.append("export default () => ({")
    lines.extend(body)
    lines.append("});")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------


def render_env_example(app_env, db_plans):
    lines = ["# Generated from LoopBack config by lb_config.py.",
             "# Secrets are placeholders — fill them in; never commit real values.", ""]
    if app_env:
        lines.append("# --- App ---")
        for _section, name, placeholder, kind in app_env:
            lines.append(env_line(name, placeholder, kind))
        lines.append("")
    for plan in db_plans:
        lines.append(f"# --- Datasource: {plan['name']}"
                     + (f" ({plan['connector']})" if plan["connector"] else "") + " ---")
        for name, placeholder, kind in plan["env"]:
            lines.append(env_line(name, placeholder, kind))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def env_line(name, placeholder, kind):
    if kind == "secret":
        return f"{name}=change-me   # secret — set a real value, do not commit"
    if placeholder is None or placeholder == "":
        return f"{name}="
    return f"{name}={placeholder}"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_report(app, app_env, app_fields, app_notes, db_plans, db_exports):
    L = []
    L.append("# LoopBack -> NestJS config migration")
    L.append("")
    L.append(f"Source: `{app['server_dir']}`  ")
    L.append(f"NODE_ENV layer: **{app['env']}**")
    L.append("")

    # Layering
    L.append("## Environment layering")
    L.append("")
    L.append("LoopBack merges, in ascending precedence, "
             "`X.json` < `X.local.json` < `X.<env>.json` (later wins). This run merged:")
    L.append("")
    for base, files in app["loaded"].items():
        L.append(f"- `{base}`: {', '.join('`' + f + '`' for f in files) or '_none found_'}")
    js_all = [(b, f) for b, fs in app["js"].items() for f in fs]
    if js_all:
        L.append("")
        L.append("**Executable `.js` config found — NOT evaluated (flagged below):** "
                 + ", ".join(f"`{f}`" for _b, f in js_all))
    L.append("")

    # App config mapping
    L.append("## App config (`config.json` -> `config/app.config.ts`)")
    L.append("")
    L.append("| LoopBack key | Nest config path | Env var |")
    L.append("|--------------|------------------|---------|")
    rest_root = app["app"].get("restApiRoot", "/api")
    L.append(f"| port | `app.port` | `PORT` |")
    L.append(f"| host | `app.host` | `HOST` |")
    L.append(f"| restApiRoot (`{rest_root}`) | `app.globalPrefix` | `API_PREFIX` |")
    for name, _expr, _c in app_fields:
        if name in ("host", "port", "globalPrefix"):
            continue
        L.append(f"| {name} | `app.{name}` | (see .env.example) |")
    L.append("")
    L.append("Wire the prefix in `main.ts`:")
    L.append("")
    L.append("```ts")
    L.append("const cfg = app.get(ConfigService);")
    L.append("app.setGlobalPrefix(cfg.get<string>('app.globalPrefix'));")
    L.append("await app.listen(cfg.get<number>('app.port'), cfg.get<string>('app.host'));")
    L.append("```")
    L.append("")

    # Datasources
    L.append("## Datasources (`datasources.json` -> `config/database.config.ts`)")
    L.append("")
    if not db_plans:
        L.append("_No datasources found._")
    for plan in db_plans:
        L.append(f"### `{plan['name']}` -> namespace `{plan['namespace']}`"
                 + (f" (connector: {plan['connector']})" if plan["connector"] else ""))
        L.append("")
        L.append("| Field | Env var | Sensitive |")
        L.append("|-------|---------|-----------|")
        for env_name, _ph, kind in plan["env"]:
            L.append(f"| {env_name.split('_', 1)[-1].lower()} | `{env_name}` | "
                     f"{'**yes**' if kind == 'secret' else 'no'} |")
        L.append("")
    L.append("Sensitive fields (password/secret/token/apiKey) are emitted as "
             "`process.env.X` with no literal fallback; real values are never copied.")
    L.append("")

    # Components
    L.append("## Components (`component-config.json`)")
    L.append("")
    if not app["components"]:
        L.append("_No component-config.json entries._")
    else:
        L.append("| Component | Nest analogue |")
        L.append("|-----------|---------------|")
        for name in app["components"]:
            analogue = COMPONENT_ANALOGUE.get(name, "no direct equivalent — evaluate")
            L.append(f"| `{name}` | {analogue} |")
    L.append("")

    # Wiring
    L.append("## Wiring `ConfigModule`")
    L.append("")
    L.append("```ts")
    L.append("import { ConfigModule } from '@nestjs/config';")
    L.append("import appConfig from './config/app.config';")
    load_names = ["appConfig"]
    for i, (var, _ns, is_default) in enumerate(db_exports):
        if is_default:
            L.append("import databaseConfig from './config/database.config';")
            load_names.append("databaseConfig")
        else:
            L.append(f"import {{ {var} }} from './config/database.config';")
            load_names.append(var)
    L.append("")
    L.append("@Module({")
    L.append("  imports: [")
    L.append("    ConfigModule.forRoot({")
    L.append("      isGlobal: true,")
    L.append(f"      load: [{', '.join(load_names)}],")
    L.append("      envFilePath: '.env',")
    L.append("    }),")
    L.append("  ],")
    L.append("})")
    L.append("export class AppModule {}")
    L.append("```")
    L.append("")
    L.append("Read values with `configService.get('app.port')` / "
             "`configService.get('" + (db_plans[0]["namespace"] if db_plans else "database")
             + ".host')`, or inject a namespace typed via "
             "`ConfigType<typeof databaseConfig>`.")
    L.append("")

    # Manual attention
    L.append("## Needs manual attention")
    L.append("")
    checklist = []
    if js_all:
        checklist.append("**Executable `.js` config** (" + ", ".join(f"`{f}`" for _b, f in js_all)
                         + ") runs arbitrary code at boot — it was NOT evaluated. Port its "
                         "logic into the config factories or a validation schema by hand.")
    if any(n[0] == "remoting" for n in app_notes):
        checklist.append("**`remoting`** (strong-remoting: rest/json/urlencoded/cors/"
                         "errorHandler) has no direct Nest equivalent — configure body "
                         "parser limits, CORS (`app.enableCors`), and error filters instead.")
    if any(n[0] == "legacyExplorer" for n in app_notes):
        checklist.append("**`legacyExplorer`/explorer** — replace LoopBack's `/explorer` "
                         "with `@nestjs/swagger` (`SwaggerModule.setup`).")
    other_notes = [n for n in app_notes if n[0] not in ("remoting", "legacyExplorer")]
    if other_notes:
        checklist.append("**Object-valued config.json keys** ("
                         + ", ".join(f"`{n[0]}`" for n in other_notes)
                         + ") were not flattened — map them explicitly.")
    if app["components"]:
        checklist.append("**Components** — each entry in component-config.json is an "
                         "npm module mounted at boot; wire the Nest analogue per the table above.")
    checklist.append("**Validation** — add a `validationSchema` (Joi) or `validate` fn to "
                     "`ConfigModule.forRoot` to fail fast on missing env vars.")
    checklist.append("**Secrets** — fill `.env` from `.env.example`; the generated code "
                     "reads secrets only from `process.env`.")
    for c in checklist:
        L.append(f"- [ ] {c}")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Assemble + write
# ---------------------------------------------------------------------------


def build_outputs(app):
    app_env, app_fields, app_notes = build_app_config(app)
    db_plans = [build_datasource(name, cfg, i)
                for i, (name, cfg) in enumerate(app["datasources"].items())]
    db_text, db_exports = render_database_config(db_plans)

    files = {}
    files["config/app.config.ts"] = render_app_config(app_fields)
    if db_plans:
        files["config/database.config.ts"] = db_text
    files["config/configuration.ts"] = render_configuration(True, db_exports)
    files[".env.example"] = render_env_example(app_env, db_plans)
    report = build_report(app, app_env, app_fields, app_notes, db_plans, db_exports)
    files["CONFIG_MIGRATION.md"] = report + "\n"
    return files, report


def write_files(files, out_dir, force, dry_run):
    if os.path.isdir(out_dir) and os.listdir(out_dir) and not force and not dry_run:
        raise MigrationError(
            f"output directory {out_dir} is not empty; pass --force to overwrite")
    written = []
    for rel, content in sorted(files.items()):
        dest = os.path.join(out_dir, rel)
        written.append(dest)
        if dry_run:
            continue
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lb_config.py",
        description="Convert LoopBack 3 server config into a NestJS ConfigModule setup.")
    parser.add_argument("app", help="LoopBack app root or its server/ directory")
    parser.add_argument("--env", default=os.environ.get("NODE_ENV", "development"),
                        help="NODE_ENV layer to merge (default: $NODE_ENV or development)")
    parser.add_argument("-o", "--out",
                        help="write generated files here (default: print report to stdout)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing non-empty output directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="with -o, list files that would be written; write nothing")
    args = parser.parse_args(argv)

    try:
        app = load_app_config(args.app, args.env)
        files, report = build_outputs(app)
        if not args.out:
            print(report)
            return 0
        written = write_files(files, args.out, args.force, args.dry_run)
        verb = "would write" if args.dry_run else "wrote"
        for w in written:
            print(f"{verb} {w}")
        print(f"\n{len(written)} files -> {args.out} (env: {args.env})", file=sys.stderr)
        if not args.dry_run:
            print("Next: read CONFIG_MIGRATION.md, then wire ConfigModule.forRoot({ load: [...] }).",
                  file=sys.stderr)
        return 0
    except MigrationError as exc:
        print(f"lb_config: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
