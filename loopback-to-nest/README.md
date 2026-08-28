# loopback-to-nest

Scaffold a **NestJS 12** app from a **LoopBack 3** codebase, deterministically.

LoopBack 3 stores its models and wiring as JSON (`common/models/*.json`,
`server/model-config.json`, `server/datasources.json`). That makes most of a
LoopBack → NestJS migration a mechanical transform — not a job worth spending
LLM tokens on. `lb2nest.py` does the mechanical 80% (entities, DTOs, services,
controllers, modules, a TypeORM `data-source.ts`, a root `app.module.ts`) and
prints a short checklist of the LoopBack-specific 20% that still needs a human
(operation/remote hooks, ACLs, mixins, boot scripts, relations).

Hand an agent the generated skeleton plus `MIGRATION.md` instead of the whole
LoopBack tree.

Python 3.9+, no dependencies. Emits TypeScript; nothing here runs Node.

## Usage

```sh
./lb2nest.py scan  path/to/loopback-app                 # inventory + effort report
./lb2nest.py scan  path/to/loopback-app -o report.md    # write the report to a file

./lb2nest.py generate path/to/loopback-app              # scaffold into ./nest-out
./lb2nest.py generate path/to/loopback-app -o ../nest   # choose the output dir
./lb2nest.py generate path/to/loopback-app --orm none   # plain classes, in-memory services
./lb2nest.py generate path/to/loopback-app --dry-run    # list files, write nothing
./lb2nest.py generate common/models/coffee-shop.json    # a single model file
```

The app argument may be a **LoopBack app root** (models are discovered under
`common/models`, `server/models`, or `models`; config under `server/`), a
**models directory**, or a **single model `.json`**.

### `scan`

Prints a Markdown report: a per-model table (base, property count, relation
types, remote-method count, ACL count, whether REST-exposed per
`model-config.json`), a datasource table with the suggested TypeORM type and
driver package, and a **"Needs manual attention"** checklist.

### `generate`

Writes a compiling NestJS 12 skeleton:

```
nest-out/
  package.json                 # NestJS 12 + (TypeORM) deps, pinned
  MIGRATION.md                 # the scan report, shipped alongside the code
  src/
    main.ts                    # global ValidationPipe (LoopBack rejected unknown props)
    app.module.ts              # imports every REST-exposed model's module
    data-source.ts             # TypeORM DataSource derived from datasources.json
    <model>/
      <model>.entity.ts        # TypeORM entity (or plain class with --orm none)
      <model>.service.ts       # repository-backed CRUD (or in-memory with --orm none)
      <model>.controller.ts    # REST CRUD + a stub per custom remote method
      <model>.module.ts
      dto/
        create-<model>.dto.ts  # class-validator decorators from property types
        update-<model>.dto.ts  # PartialType(Create…Dto)
```

| Flag | Meaning |
|------|---------|
| `-o, --out DIR` | output directory (default `./nest-out`) |
| `--orm typeorm` | (default) TypeORM entities + repository services + `data-source.ts` |
| `--orm none` | plain model classes + in-memory services, zero infra to run |
| `--force` | overwrite a non-empty output directory |
| `--dry-run` | list the files that would be written, write nothing |

## What is converted, and how

| LoopBack | NestJS output |
|----------|---------------|
| Model `properties` (`string`/`number`/`boolean`/`date`/`object`/`buffer`/`geopoint`, arrays, shorthand) | typed entity columns + DTO fields |
| `required: true` | non-optional field + validator (no `@IsOptional()`) |
| Injected `id` (PersistedModel, unless `idInjection:false` or an explicit id) | `@PrimaryGeneratedColumn()`, excluded from the Create DTO |
| Default CRUD REST endpoints | `@Get/@Post/@Patch/@Delete` controller + service |
| Custom remote methods (JSON `methods`, and `Model.remoteMethod(...)` in the `.js`) | controller stubs with the original verb/path that throw `Not implemented`, flagged as TODO |
| `datasources.json` connector | TypeORM `type` + driver package (mysql/postgres/mssql/oracle/mongodb/sqlite) |
| `model-config.json` `public: false` | model still generated, but omitted from `app.module.ts` |

## What it deliberately does *not* do (the checklist)

These have no faithful mechanical mapping and are listed in `MIGRATION.md`:

- **Operation/remote hooks** (`Model.observe`, `beforeRemote`/`afterRemote`) —
  reimplement as interceptors, guards, or service logic.
- **ACLs** — map to Nest Guards + a roles/permissions strategy.
- **Mixins** — reimplement as base classes, decorators, or shared services.
- **Relations** — emitted as comments on the entity; you decide the owning
  side, FKs, and eager/lazy loading, then add the TypeORM relation decorators.
- **Boot scripts / middleware.json / component-config.json** — port to Nest
  lifecycle hooks, middleware, or seeding scripts.
- **Custom declarative validations** beyond simple type/required.

Pluralized route names use a small English pluralizer; check irregular nouns.

## Scope

Targets **LoopBack 3** (JSON-defined models — the common case for "migrate our
old LoopBack app"). LoopBack 4 already uses decorator-based
models/controllers/repositories and needs a different, largely AST-level
transform; this tool does not attempt it.

## Tests

```sh
python3 loopback-to-nest/test_lb2nest.py
```

Builds a small LoopBack 3 app in a temp dir and checks the scan report and
every generated file kind, for both `--orm` modes.
