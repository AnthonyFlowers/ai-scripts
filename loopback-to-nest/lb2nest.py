#!/usr/bin/env python3
"""lb2nest.py — scaffold a NestJS 12 app from a LoopBack 3 codebase.

LoopBack 3 keeps its models and wiring as JSON data (`common/models/*.json`,
`server/model-config.json`, `server/datasources.json`), which makes the bulk
of a LoopBack -> NestJS migration a deterministic transform rather than a
job for a language model. This script does that transform: it reads a
LoopBack app, and either

  * `scan`     prints a migration inventory + effort report (models,
               relations, remote methods, ACLs, datasources, boot scripts,
               and a manual-attention checklist), or
  * `generate` writes NestJS 12 scaffolding — per model a kebab-case folder
               with an entity, Create/Update DTOs (class-validator), a
               service, a controller (CRUD + stubs for custom remote
               methods), and a module — plus a root `app.module.ts`, a
               TypeORM `data-source.ts` derived from `datasources.json`, and
               a `MIGRATION.md` listing what still needs a human.

The point is to hand an agent (or a person) a compiling skeleton and a short
checklist instead of the whole LoopBack tree — the mechanical 80% done for
free, the LoopBack-specific 20% (operation/remote hooks, ACLs, mixins,
custom connectors) flagged explicitly.

Python 3.9+, no dependencies. Emits TypeScript; nothing here runs Node.

Usage:
  ./lb2nest.py scan  <app-dir>                     # inventory + effort report
  ./lb2nest.py scan  <app-dir> -o report.md        # write report to a file
  ./lb2nest.py generate <app-dir>                  # scaffold into ./nest-out
  ./lb2nest.py generate <app-dir> -o ../nest-app   # choose the output dir
  ./lb2nest.py generate <app-dir> --orm none       # plain classes, no TypeORM
  ./lb2nest.py generate models/coffee-shop.json    # a single model file

<app-dir> may be a LoopBack app root (models found under common/models and
server/models, config under server/), a models directory, or a single model
JSON file.

Options (generate):
  -o, --out DIR       output directory (default: ./nest-out)
  --orm {typeorm,none}
                      typeorm (default): TypeORM entities + repository
                      services; none: plain model classes + in-memory services
  --force             overwrite an existing non-empty output directory
  --dry-run           list the files that would be written, write nothing

Options (scan):
  -o, --out FILE      write the Markdown report here (default: stdout)

Exit codes: 0 ok, 1 bad args / nothing found / refusing to overwrite.
"""

import argparse
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# LoopBack type system
# ---------------------------------------------------------------------------

# LoopBack property type (lowercased) -> TypeScript type.
LB_TS = {
    "string": "string",
    "number": "number",
    "boolean": "boolean",
    "date": "Date",
    "object": "Record<string, unknown>",
    "any": "any",
    "buffer": "Buffer",
    "geopoint": "{ lat: number; lng: number }",
}

# LoopBack type -> class-validator decorator (without the @, without parens).
LB_VALIDATOR = {
    "string": "IsString",
    "number": "IsNumber",
    "boolean": "IsBoolean",
    "date": "IsDate",
    "object": "IsObject",
    "buffer": "IsObject",
    "geopoint": "IsObject",
}

# LoopBack connector -> (TypeORM `type`, npm driver package) suggestion.
CONNECTOR_TYPEORM = {
    "mysql": ("mysql", "mysql2"),
    "postgresql": ("postgres", "pg"),
    "mssql": ("mssql", "mssql"),
    "oracle": ("oracle", "oracledb"),
    "mongodb": ("mongodb", "mongodb"),
    "sqlite3": ("sqlite", "sqlite3"),
    "memory": ("sqlite", "sqlite3"),
}

LB_RELATION_TYPES = {
    "belongsTo", "hasOne", "hasMany", "hasManyThrough",
    "hasAndBelongsToMany", "referencesMany", "embedsOne", "embedsMany",
}

# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------


def _words(name):
    """Split an identifier into lowercase words (handles camel, snake, kebab)."""
    s = re.sub(r"[_\-\s]+", " ", name)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return [w for w in s.lower().split() if w]


def pascal(name):
    return "".join(w[:1].upper() + w[1:] for w in _words(name)) or "Model"


def camel(name):
    p = pascal(name)
    return p[:1].lower() + p[1:]


def kebab(name):
    return "-".join(_words(name)) or "model"


def snake(name):
    return "_".join(_words(name)) or "model"


def pluralize(name):
    """Small English pluralizer — good enough for route names, noted as best-effort."""
    if not name:
        return name
    lower = name.lower()
    irregular = {"person": "people", "child": "children", "foot": "feet",
                 "man": "men", "woman": "women", "mouse": "mice", "goose": "geese"}
    if lower in irregular:
        out = irregular[lower]
        return out if name.islower() else out.capitalize()
    if re.search(r"[^aeiou]y$", name):
        return name[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", name):
        return name + "es"
    return name + "s"


# ---------------------------------------------------------------------------
# Parsing a LoopBack app
# ---------------------------------------------------------------------------


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise MigrationError(f"cannot read JSON {path}: {exc}")


class MigrationError(Exception):
    pass


def parse_property(name, spec):
    """Normalize a LoopBack property spec (string | list | dict) to a dict."""
    prop = {
        "name": name, "lb_type": "any", "array": False, "required": False,
        "id": False, "generated": False, "default": None, "description": None,
    }
    if isinstance(spec, str):
        prop["lb_type"] = spec
    elif isinstance(spec, list):
        prop["array"] = True
        prop["lb_type"] = spec[0] if spec and isinstance(spec[0], str) else "any"
    elif isinstance(spec, dict):
        t = spec.get("type", "any")
        if isinstance(t, list):
            prop["array"] = True
            t = t[0] if t and isinstance(t[0], str) else "any"
        prop["lb_type"] = t if isinstance(t, str) else "any"
        prop["required"] = bool(spec.get("required", False))
        prop["id"] = bool(spec.get("id", False))
        prop["generated"] = bool(spec.get("generated", spec.get("id", False)))
        prop["default"] = spec.get("default", spec.get("defaultFn", None))
        prop["description"] = spec.get("description")
    return prop


def parse_relation(name, spec):
    spec = spec or {}
    return {
        "name": name,
        "type": spec.get("type", "hasMany"),
        "model": spec.get("model", ""),
        "foreignKey": spec.get("foreignKey", ""),
        "through": spec.get("through"),
    }


def parse_model_json(path):
    """Load a LoopBack model .json, and glean remote methods from a sibling .js."""
    raw = _read_json(path)
    name = raw.get("name") or pascal(os.path.splitext(os.path.basename(path))[0])
    props = [parse_property(n, s) for n, s in (raw.get("properties") or {}).items()]

    id_injection = raw.get("idInjection", True)
    base = raw.get("base", "PersistedModel")
    persisted = base not in ("Model", "KeyValueModel")
    has_explicit_id = any(p["id"] for p in props)
    if persisted and id_injection and not has_explicit_id:
        props.insert(0, parse_property("id", {"type": "number", "id": True,
                                              "generated": True, "required": False}))

    relations = [parse_relation(n, s) for n, s in (raw.get("relations") or {}).items()]

    # Custom remote methods: JSON `methods` block (LB3), plus a JS scan.
    methods = []
    for mname, mspec in (raw.get("methods") or {}).items():
        methods.append(_remote_from_json(mname, mspec))
    js_path = os.path.splitext(path)[0] + ".js"
    js_methods, hooks = ([], [])
    if os.path.exists(js_path):
        js_methods, hooks = _scan_model_js(js_path)
    # de-dupe by method name, JSON definitions win (they carry http metadata)
    known = {m["name"] for m in methods}
    for m in js_methods:
        if m["name"] not in known:
            methods.append(m)

    return {
        "name": name,
        "base": base,
        "persisted": persisted,
        "properties": props,
        "relations": relations,
        "methods": methods,
        "hooks": hooks,
        "acls": raw.get("acls") or [],
        "mixins": list((raw.get("mixins") or {}).keys()),
        "validations": raw.get("validations") or [],
        "hidden": raw.get("hidden") or [],
        "scopes": raw.get("scope") or raw.get("scopes"),
        "source": path,
    }


def _remote_from_json(mname, mspec):
    http = mspec.get("http") if isinstance(mspec, dict) else None
    if isinstance(http, list):
        http = http[0] if http else None
    verb = (http or {}).get("verb", "post").upper() if isinstance(http, dict) else "POST"
    path = (http or {}).get("path", "/" + kebab(mname)) if isinstance(http, dict) else "/" + kebab(mname)
    accepts = mspec.get("accepts") if isinstance(mspec, dict) else None
    if isinstance(accepts, dict):
        accepts = [accepts]
    is_static = not (isinstance(mspec, dict) and mspec.get("isStatic") is False)
    return {"name": mname, "verb": verb, "path": path, "static": is_static,
            "accepts": accepts or [], "from": "json"}


_JS_REMOTE = re.compile(
    r"""\.remoteMethod\(\s*['"]([A-Za-z0-9_.]+)['"]""", re.VERBOSE)
_JS_HOOK = re.compile(
    r"""\.(observe|beforeRemote|afterRemote|afterRemoteError)\(\s*['"]([^'"]+)['"]""")


def _scan_model_js(path):
    """Best-effort regex scan of a model's .js for remote methods and hooks."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return [], []
    methods = [{"name": m.group(1).split(".")[-1], "verb": "POST",
                "path": "/" + kebab(m.group(1).split(".")[-1]), "static": True,
                "accepts": [], "from": "js"} for m in _JS_REMOTE.finditer(text)]
    hooks = [{"kind": m.group(1), "name": m.group(2)} for m in _JS_HOOK.finditer(text)]
    return methods, hooks


def _find_models_dirs(app_dir):
    dirs = []
    for cand in ("common/models", "server/models", "models"):
        full = os.path.join(app_dir, cand)
        if os.path.isdir(full):
            dirs.append(full)
    if not dirs and os.path.isdir(app_dir):
        # maybe app_dir *is* a models dir
        if any(f.endswith(".json") for f in os.listdir(app_dir)):
            dirs.append(app_dir)
    return dirs


def load_app(path):
    """Return the parsed app: models + config, from a root/models-dir/file."""
    app = {"models": [], "model_config": {}, "datasources": {},
           "boot_scripts": [], "middleware": None, "components": None,
           "app_dir": path, "server_dir": None}

    if os.path.isfile(path) and path.endswith(".json"):
        app["models"].append(parse_model_json(path))
        app["app_dir"] = os.path.dirname(path) or "."
        return app

    if not os.path.isdir(path):
        raise MigrationError(f"not a directory or .json file: {path}")

    model_files = []
    for d in _find_models_dirs(path):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json") and fn != "model-config.json":
                model_files.append(os.path.join(d, fn))
    for mf in model_files:
        app["models"].append(parse_model_json(mf))

    # config: look under <root>/server and <root> itself
    for server in (os.path.join(path, "server"), path):
        mc = os.path.join(server, "model-config.json")
        ds = os.path.join(server, "datasources.json")
        if os.path.isfile(mc):
            app["server_dir"] = server
            raw = _read_json(mc)
            app["model_config"] = {k: v for k, v in raw.items()
                                   if not k.startswith("_") and isinstance(v, dict)}
        if os.path.isfile(ds):
            app["datasources"] = _read_json(ds)
        boot = os.path.join(server, "boot")
        if os.path.isdir(boot):
            app["boot_scripts"] = sorted(
                f for f in os.listdir(boot) if f.endswith(".js"))
        mw = os.path.join(server, "middleware.json")
        if os.path.isfile(mw):
            app["middleware"] = _read_json(mw)
        comp = os.path.join(server, "component-config.json")
        if os.path.isfile(comp):
            app["components"] = _read_json(comp)
        if app["server_dir"]:
            break

    if not app["models"]:
        raise MigrationError(
            f"no LoopBack model .json files found under {path} "
            "(looked in common/models, server/models, models)")
    return app


# ---------------------------------------------------------------------------
# TypeScript helpers
# ---------------------------------------------------------------------------


def ts_type(prop, model_names):
    base = prop["lb_type"]
    low = base.lower()
    if low in LB_TS:
        ts = LB_TS[low]
    elif base in model_names or base[:1].isupper():
        ts = base  # reference to another model / embedded type
    else:
        ts = "any"
    return ts + "[]" if prop["array"] else ts


def _is_exposed(app, model_name):
    """A model is REST-exposed if model-config marks it public (default true)."""
    cfg = app["model_config"].get(model_name)
    if cfg is None:
        return None  # unknown — no model-config present
    return cfg.get("public", True) is not False


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------


def gen_entity(model, model_names, orm):
    name = pascal(model["name"])
    props = model["properties"]
    lines = []
    if orm == "typeorm":
        decos = {"Entity", "Column"}
        body = []
        for p in props:
            ts = ts_type(p, model_names)
            opt = "" if p["required"] else "?"
            if p["id"]:
                if p["generated"]:
                    decos.add("PrimaryGeneratedColumn")
                    body.append("  @PrimaryGeneratedColumn()")
                else:
                    decos.add("PrimaryColumn")
                    body.append("  @PrimaryColumn()")
            else:
                col_opts = []
                if not p["required"]:
                    col_opts.append("nullable: true")
                if p["lb_type"].lower() == "date":
                    col_opts.append("type: 'timestamp'")
                elif p["lb_type"].lower() in ("object", "any", "geopoint") or p["array"]:
                    col_opts.append("type: 'simple-json'")
                opts = "{ " + ", ".join(col_opts) + " }" if col_opts else ""
                body.append(f"  @Column({opts})")
            body.append(f"  {camel(p['name'])}{opt}: {ts};\n")
        # relations as comments — TypeORM relations need manual FK/owner decisions
        rel_notes = _relation_comments(model)
        imports = "import { " + ", ".join(sorted(decos)) + " } from 'typeorm';\n\n"
        lines.append(imports)
        lines.append(f"@Entity({{ name: '{snake(pluralize(model['name']))}' }})\n")
        lines.append(f"export class {name} {{\n")
        lines.append("\n".join(body))
        if rel_notes:
            lines.append("\n" + rel_notes)
        lines.append("}\n")
    else:
        body = []
        for p in props:
            ts = ts_type(p, model_names)
            opt = "" if p["required"] else "?"
            body.append(f"  {camel(p['name'])}{opt}: {ts};")
        rel_notes = _relation_comments(model)
        lines.append(f"export class {name} {{\n")
        lines.append("\n".join(body))
        if rel_notes:
            lines.append("\n\n" + rel_notes)
        lines.append("\n}\n")
    return "".join(lines)


def _relation_comments(model):
    if not model["relations"]:
        return ""
    out = ["  // LoopBack relations — wire these up by hand:"]
    for r in model["relations"]:
        out.append(f"  //   {r['name']}: {r['type']} -> {r['model'] or '?'}"
                   + (f" through {r['through']}" if r.get("through") else ""))
    return "\n".join(out) + "\n"


def gen_dtos(model, model_names):
    name = pascal(model["name"])
    validators = set()
    fields = []
    for p in model["properties"]:
        if p["id"]:
            continue  # id is server-assigned; not part of Create DTO
        ts = ts_type(p, model_names)
        low = p["lb_type"].lower()
        decos = []
        if not p["required"]:
            validators.add("IsOptional")
            decos.append("  @IsOptional()")
        v = LB_VALIDATOR.get(low, "IsNotEmpty" if p["required"] else None)
        if p["array"]:
            validators.add("IsArray")
            decos.append("  @IsArray()")
            if low in LB_VALIDATOR and low not in ("object", "buffer", "geopoint"):
                validators.add(LB_VALIDATOR[low])
                decos.append(f"  @{LB_VALIDATOR[low]}({{ each: true }})")
        elif v:
            validators.add(v)
            decos.append(f"  @{v}()")
        opt = "" if p["required"] else "?"
        fields.append("\n".join(decos) + f"\n  {camel(p['name'])}{opt}: {ts};\n")

    imp = ("import { " + ", ".join(sorted(validators)) + " } from 'class-validator';\n\n"
           if validators else "")
    create = (f"{imp}export class Create{name}Dto {{\n"
              + "\n".join(fields) + "}\n")
    update = ("import { PartialType } from '@nestjs/mapped-types';\n"
              f"import {{ Create{name}Dto }} from './create-{kebab(model['name'])}.dto';\n\n"
              f"export class Update{name}Dto extends PartialType(Create{name}Dto) {{}}\n")
    return create, update


def gen_service(model, orm):
    name = pascal(model["name"])
    kn = kebab(model["name"])
    id_prop = next((p for p in model["properties"] if p["id"]), None)
    id_ts = "number" if (id_prop and id_prop["lb_type"].lower() == "number") else "string"
    if orm == "typeorm":
        return f"""import {{ Injectable, NotFoundException }} from '@nestjs/common';
import {{ InjectRepository }} from '@nestjs/typeorm';
import {{ Repository }} from 'typeorm';
import {{ {name} }} from './{kn}.entity';
import {{ Create{name}Dto }} from './dto/create-{kn}.dto';
import {{ Update{name}Dto }} from './dto/update-{kn}.dto';

@Injectable()
export class {name}Service {{
  constructor(
    @InjectRepository({name})
    private readonly repo: Repository<{name}>,
  ) {{}}

  create(dto: Create{name}Dto): Promise<{name}> {{
    return this.repo.save(this.repo.create(dto as Partial<{name}>));
  }}

  findAll(): Promise<{name}[]> {{
    return this.repo.find();
  }}

  async findOne(id: {id_ts}): Promise<{name}> {{
    const found = await this.repo.findOne({{ where: {{ id: id as any }} }});
    if (!found) throw new NotFoundException(`{name} ${{id}} not found`);
    return found;
  }}

  async update(id: {id_ts}, dto: Update{name}Dto): Promise<{name}> {{
    const found = await this.findOne(id);
    Object.assign(found, dto);
    return this.repo.save(found);
  }}

  async remove(id: {id_ts}): Promise<void> {{
    await this.findOne(id);
    await this.repo.delete(id as any);
  }}
}}
"""
    # orm none: in-memory store, so the skeleton runs with zero infra.
    return f"""import {{ Injectable, NotFoundException }} from '@nestjs/common';
import {{ {name} }} from './{kn}.entity';
import {{ Create{name}Dto }} from './dto/create-{kn}.dto';
import {{ Update{name}Dto }} from './dto/update-{kn}.dto';

@Injectable()
export class {name}Service {{
  private readonly items: {name}[] = [];
  private seq = 1;

  create(dto: Create{name}Dto): {name} {{
    const item = {{ id: this.seq++, ...dto }} as unknown as {name};
    this.items.push(item);
    return item;
  }}

  findAll(): {name}[] {{
    return this.items;
  }}

  findOne(id: {id_ts}): {name} {{
    const found = this.items.find((i) => (i as any).id === id);
    if (!found) throw new NotFoundException(`{name} ${{id}} not found`);
    return found;
  }}

  update(id: {id_ts}, dto: Update{name}Dto): {name} {{
    const found = this.findOne(id);
    Object.assign(found, dto);
    return found;
  }}

  remove(id: {id_ts}): void {{
    const idx = this.items.findIndex((i) => (i as any).id === id);
    if (idx < 0) throw new NotFoundException(`{name} ${{id}} not found`);
    this.items.splice(idx, 1);
  }}
}}
"""


def gen_controller(model):
    name = pascal(model["name"])
    kn = kebab(model["name"])
    cn = camel(model["name"])
    route = kebab(pluralize(model["name"]))
    id_prop = next((p for p in model["properties"] if p["id"]), None)
    id_num = id_prop and id_prop["lb_type"].lower() == "number"
    id_param = "@Param('id', ParseIntPipe) id: number" if id_num else "@Param('id') id: string"
    decos = {"Controller", "Get", "Post", "Patch", "Delete", "Body", "Param"}
    if id_num:
        decos.add("ParseIntPipe")

    custom = []
    for m in model["methods"]:
        verb = m["verb"].capitalize()
        if verb not in ("Get", "Post", "Put", "Patch", "Delete"):
            verb = "Post"
        decos.add(verb)
        path = m["path"].lstrip("/")
        custom.append(f"""
  // TODO: ported from LoopBack remote method `{model['name']}.{m['name']}`
  //       ({m['from']}); reimplement the body against {name}Service.
  @{verb}('{path}')
  {camel(m['name'])}(@Body() body: any): any {{
    throw new Error('Not implemented: {m['name']} (migrated from LoopBack)');
  }}""")

    imports = "import { " + ", ".join(sorted(decos)) + " } from '@nestjs/common';\n"
    return f"""{imports}import {{ {name}Service }} from './{kn}.service';
import {{ Create{name}Dto }} from './dto/create-{kn}.dto';
import {{ Update{name}Dto }} from './dto/update-{kn}.dto';

@Controller('{route}')
export class {name}Controller {{
  constructor(private readonly {cn}Service: {name}Service) {{}}

  @Post()
  create(@Body() dto: Create{name}Dto) {{
    return this.{cn}Service.create(dto);
  }}

  @Get()
  findAll() {{
    return this.{cn}Service.findAll();
  }}

  @Get(':id')
  findOne({id_param}) {{
    return this.{cn}Service.findOne(id);
  }}

  @Patch(':id')
  update({id_param}, @Body() dto: Update{name}Dto) {{
    return this.{cn}Service.update(id, dto);
  }}

  @Delete(':id')
  remove({id_param}) {{
    return this.{cn}Service.remove(id);
  }}
{"".join(custom)}
}}
"""


def gen_module(model, orm):
    name = pascal(model["name"])
    kn = kebab(model["name"])
    if orm == "typeorm":
        return f"""import {{ Module }} from '@nestjs/common';
import {{ TypeOrmModule }} from '@nestjs/typeorm';
import {{ {name} }} from './{kn}.entity';
import {{ {name}Service }} from './{kn}.service';
import {{ {name}Controller }} from './{kn}.controller';

@Module({{
  imports: [TypeOrmModule.forFeature([{name}])],
  controllers: [{name}Controller],
  providers: [{name}Service],
  exports: [{name}Service],
}})
export class {name}Module {{}}
"""
    return f"""import {{ Module }} from '@nestjs/common';
import {{ {name}Service }} from './{kn}.service';
import {{ {name}Controller }} from './{kn}.controller';

@Module({{
  controllers: [{name}Controller],
  providers: [{name}Service],
  exports: [{name}Service],
}})
export class {name}Module {{}}
"""


def gen_data_source(app, orm):
    """Turn datasources.json into a TypeORM DataSource, or a note when orm=none."""
    ds = app["datasources"] or {}
    primary = None
    for key, cfg in ds.items():
        if key.startswith("_") or not isinstance(cfg, dict):
            continue
        if cfg.get("connector", "").lower() != "memory":
            primary = (key, cfg)
            break
    if primary is None and ds:
        for key, cfg in ds.items():
            if not key.startswith("_") and isinstance(cfg, dict):
                primary = (key, cfg)
                break

    if orm != "typeorm":
        return None
    if not primary:
        conn, driver, host, port, db = "postgres", "pg", "localhost", 5432, "app"
        note = "// No datasources.json found — filled with Postgres placeholders.\n"
    else:
        _, cfg = primary
        connector = cfg.get("connector", "postgresql").lower()
        conn, driver = CONNECTOR_TYPEORM.get(connector, ("postgres", "pg"))
        host = cfg.get("host", "localhost")
        port = cfg.get("port", 5432)
        db = cfg.get("database", cfg.get("name", "app"))
        note = f"// Derived from LoopBack datasource (connector: {connector}, driver: {driver}).\n"
    return f"""{note}import {{ DataSource }} from 'typeorm';

export const AppDataSource = new DataSource({{
  type: '{conn}',
  host: process.env.DB_HOST ?? '{host}',
  port: Number(process.env.DB_PORT ?? {port}),
  username: process.env.DB_USER ?? 'user',
  password: process.env.DB_PASSWORD ?? 'password',
  database: process.env.DB_NAME ?? '{db}',
  entities: [__dirname + '/**/*.entity.{{ts,js}}'],
  synchronize: false, // LoopBack auto-migrate had no schema versioning; use real migrations
}});
"""


def gen_app_module(app, models, orm):
    exposed = [m for m in models if _is_exposed(app, m["name"]) is not False]
    imports = []
    names = []
    for m in exposed:
        name = pascal(m["name"])
        imports.append(f"import {{ {name}Module }} from './{kebab(m['name'])}/{kebab(m['name'])}.module';")
        names.append(f"{name}Module")
    head = "import { Module } from '@nestjs/common';\n"
    typeorm_line = ""
    if orm == "typeorm":
        head += "import { TypeOrmModule } from '@nestjs/typeorm';\n"
        head += "import { AppDataSource } from './data-source';\n"
        typeorm_line = "    TypeOrmModule.forRoot(AppDataSource.options),\n"
    body = "\n".join(imports)
    module_list = ",\n    ".join(names)
    return f"""{head}{body}

@Module({{
  imports: [
{typeorm_line}    {module_list},
  ],
}})
export class AppModule {{}}
"""


def gen_main_ts():
    return """import { NestFactory } from '@nestjs/core';
import { ValidationPipe } from '@nestjs/common';
import { AppModule } from './app.module';

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  // LoopBack applied "reject unknown properties" per-model; do it globally here.
  app.useGlobalPipes(new ValidationPipe({ whitelist: true, transform: true }));
  await app.listen(process.env.PORT ?? 3000);
}
bootstrap();
"""


# ---------------------------------------------------------------------------
# Report (scan)
# ---------------------------------------------------------------------------


def build_report(app):
    models = app["models"]
    lines = ["# LoopBack -> NestJS migration report", ""]
    lines.append(f"Source: `{app['app_dir']}`  ")
    lines.append(f"Models: **{len(models)}**  ")
    total_props = sum(len(m["properties"]) for m in models)
    total_rel = sum(len(m["relations"]) for m in models)
    total_methods = sum(len(m["methods"]) for m in models)
    total_hooks = sum(len(m["hooks"]) for m in models)
    total_acls = sum(len(m["acls"]) for m in models)
    lines.append(f"Properties: {total_props} · Relations: {total_rel} · "
                 f"Custom remote methods: {total_methods} · Hooks: {total_hooks} · "
                 f"ACL entries: {total_acls}")
    lines.append("")

    lines.append("## Models")
    lines.append("")
    lines.append("| Model | Base | Props | Relations | Remote methods | ACLs | REST |")
    lines.append("|-------|------|------:|----------:|---------------:|-----:|------|")
    for m in sorted(models, key=lambda x: x["name"]):
        exposed = _is_exposed(app, m["name"])
        rest = "?" if exposed is None else ("yes" if exposed else "no")
        rels = ", ".join(f"{r['type']}" for r in m["relations"]) or "—"
        lines.append(f"| {m['name']} | {m['base']} | {len(m['properties'])} | "
                     f"{rels} | {len(m['methods'])} | {len(m['acls'])} | {rest} |")
    lines.append("")

    # Datasources
    lines.append("## Datasources")
    lines.append("")
    ds = {k: v for k, v in (app["datasources"] or {}).items()
          if not k.startswith("_") and isinstance(v, dict)}
    if ds:
        lines.append("| Name | Connector | TypeORM type | Driver pkg |")
        lines.append("|------|-----------|--------------|------------|")
        for k, cfg in ds.items():
            conn = cfg.get("connector", "?").lower()
            to, drv = CONNECTOR_TYPEORM.get(conn, ("(manual)", "(manual)"))
            lines.append(f"| {k} | {conn} | {to} | {drv} |")
    else:
        lines.append("_No `datasources.json` found._")
    lines.append("")

    # Manual attention
    lines.append("## Needs manual attention")
    lines.append("")
    checklist = []
    if total_hooks:
        hook_models = [m["name"] for m in models if m["hooks"]]
        checklist.append(f"**Operation/remote hooks** ({total_hooks}) in: "
                         f"{', '.join(hook_models)} — NestJS has no `Model.observe`; "
                         "reimplement as interceptors, guards, or service logic.")
    if total_methods:
        checklist.append(f"**Custom remote methods** ({total_methods}) become controller "
                         "stubs that throw — port each body against the service.")
    if total_acls:
        checklist.append(f"**ACL entries** ({total_acls}) — LoopBack ACLs map to Nest "
                         "Guards (e.g. `@UseGuards`) + a roles/permissions strategy; not generated.")
    mixin_models = [(m["name"], m["mixins"]) for m in models if m["mixins"]]
    if mixin_models:
        checklist.append("**Mixins**: "
                         + "; ".join(f"{n} ({', '.join(mx)})" for n, mx in mixin_models)
                         + " — reimplement as base classes, decorators, or shared services.")
    validated = [m["name"] for m in models if m["validations"]]
    if validated:
        checklist.append(f"**Declarative validations** on {', '.join(validated)} — "
                         "moved to DTO class-validator decorators where the type is known; "
                         "custom validators need porting.")
    if app["boot_scripts"]:
        checklist.append(f"**Boot scripts** ({len(app['boot_scripts'])}: "
                         f"{', '.join(app['boot_scripts'])}) — run-once wiring; move to a "
                         "Nest `OnApplicationBootstrap` hook or a seeding script.")
    if app["middleware"]:
        checklist.append("**middleware.json** — Express middleware order; re-add as Nest "
                         "middleware/interceptors in the right sequence.")
    if app["components"]:
        checklist.append("**component-config.json** — LoopBack components "
                         f"({', '.join(k for k in app['components'] if not k.startswith('_'))}) "
                         "have no direct Nest equivalent; evaluate each.")
    checklist.append("**Relations** are emitted as comments on entities — decide owning side, "
                     "FKs, and eager/lazy loading, then add TypeORM relation decorators.")
    checklist.append("**Pluralized routes** are best-effort English; check irregular nouns.")
    for c in checklist:
        lines.append(f"- [ ] {c}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# generate: assemble the file map
# ---------------------------------------------------------------------------


def build_files(app, orm):
    """Return {relpath: content} for the whole generated NestJS app under src/."""
    models = app["models"]
    model_names = {m["name"] for m in models}
    files = {}
    for m in models:
        kn = kebab(m["name"])
        base = f"src/{kn}"
        files[f"{base}/{kn}.entity.ts"] = gen_entity(m, model_names, orm)
        create, update = gen_dtos(m, model_names)
        files[f"{base}/dto/create-{kn}.dto.ts"] = create
        files[f"{base}/dto/update-{kn}.dto.ts"] = update
        files[f"{base}/{kn}.service.ts"] = gen_service(m, orm)
        files[f"{base}/{kn}.controller.ts"] = gen_controller(m)
        files[f"{base}/{kn}.module.ts"] = gen_module(m, orm)

    files["src/app.module.ts"] = gen_app_module(app, models, orm)
    files["src/main.ts"] = gen_main_ts()
    ds = gen_data_source(app, orm)
    if ds:
        files["src/data-source.ts"] = ds
    files["MIGRATION.md"] = build_report(app)
    files["package.json"] = gen_package_json(app, orm)
    return files


def gen_package_json(app, orm):
    deps = {
        "@nestjs/common": "^12.0.0",
        "@nestjs/core": "^12.0.0",
        "@nestjs/platform-express": "^12.0.0",
        "@nestjs/mapped-types": "^2.1.0",
        "class-validator": "^0.14.1",
        "class-transformer": "^0.5.1",
        "reflect-metadata": "^0.2.2",
        "rxjs": "^7.8.1",
    }
    if orm == "typeorm":
        deps["@nestjs/typeorm"] = "^11.0.0"
        deps["typeorm"] = "^0.3.20"
        # add the driver for the primary datasource
        for cfg in (app["datasources"] or {}).values():
            if isinstance(cfg, dict):
                conn = cfg.get("connector", "").lower()
                if conn in CONNECTOR_TYPEORM and conn != "memory":
                    deps[CONNECTOR_TYPEORM[conn][1]] = "*"
                    break
        else:
            deps["pg"] = "*"
    pkg = {
        "name": "migrated-nest-app",
        "version": "0.0.1",
        "description": "NestJS 12 app scaffolded from a LoopBack 3 codebase by lb2nest.py",
        "scripts": {
            "build": "nest build",
            "start": "nest start",
            "start:dev": "nest start --watch",
        },
        "dependencies": dict(sorted(deps.items())),
        "devDependencies": {
            "@nestjs/cli": "^12.0.0",
            "typescript": "^5.5.0",
        },
    }
    return json.dumps(pkg, indent=2) + "\n"


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
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lb2nest.py",
        description="Scan or scaffold a NestJS 12 app from a LoopBack 3 codebase.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("scan", help="print a migration inventory / effort report")
    ps.add_argument("app", help="LoopBack app dir, models dir, or a model .json")
    ps.add_argument("-o", "--out", help="write the report here (default: stdout)")

    pg = sub.add_parser("generate", help="write NestJS scaffolding")
    pg.add_argument("app", help="LoopBack app dir, models dir, or a model .json")
    pg.add_argument("-o", "--out", default="./nest-out", help="output dir (default: ./nest-out)")
    pg.add_argument("--orm", choices=("typeorm", "none"), default="typeorm",
                    help="typeorm (default) or none (plain classes, in-memory services)")
    pg.add_argument("--force", action="store_true", help="overwrite a non-empty output dir")
    pg.add_argument("--dry-run", action="store_true", help="list files, write nothing")

    args = parser.parse_args(argv)

    try:
        app = load_app(args.app)
        if args.cmd == "scan":
            report = build_report(app)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as fh:
                    fh.write(report)
                print(f"wrote {args.out}", file=sys.stderr)
            else:
                print(report)
            return 0

        files = build_files(app, args.orm)
        written = write_files(files, args.out, args.force, args.dry_run)
        verb = "would write" if args.dry_run else "wrote"
        for w in written:
            print(f"{verb} {w}")
        print(f"\n{len(written)} files, {len(app['models'])} models -> {args.out}",
              file=sys.stderr)
        if not args.dry_run:
            print(f"Next: cd {args.out} && npm install && npm run build; "
                  "read MIGRATION.md for what still needs a human.", file=sys.stderr)
        return 0
    except MigrationError as exc:
        print(f"lb2nest: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
