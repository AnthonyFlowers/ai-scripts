# lb-hooks

Inventory the **hooks** in a **LoopBack 3** model and scaffold the **NestJS 12**
equivalents.

`lb2nest.py` (in `../loopback-to-nest`) turns LoopBack's JSON-defined models
into a NestJS skeleton, but deliberately leaves the behavior in the model `.js`
files — the operation and remote **hooks** — as a manual-attention note.
`lb_hooks.py` picks up exactly that piece: it scans the `.js` files, inventories
every hook, and generates the NestJS artifacts that host the ported logic.

It does **not** transpile the JavaScript. Each generated stub embeds the
*original LoopBack source* as a reference comment with a `// TODO: port` marker,
so a human (or an agent) ports the body with the original in front of them.

Python 3.9+, no dependencies. Emits TypeScript; nothing here runs Node.

## The two hook systems, and where they map

LoopBack attaches behavior in a model's `.js` file two ways:

**Operation hooks** — `Model.observe(phase, fn)` — fire around the persistence
layer, the same place TypeORM's `EntitySubscriber` lifecycle methods run:

| LoopBack `observe(...)` | TypeORM `EntitySubscriberInterface` |
|-------------------------|-------------------------------------|
| `'before save'` | `beforeInsert` **and** `beforeUpdate` (LB *before save* fires for both create and update) |
| `'after save'` | `afterInsert` **and** `afterUpdate` |
| `'before delete'` | `beforeRemove` |
| `'after delete'` | `afterRemove` |
| `'loaded'` | `afterLoad` |
| `'access'` | *note only* — no lifecycle equivalent; closest is a Nest guard or a repository query filter |
| `'persist'` | *note only* — no clean TypeORM equivalent |

**Remote hooks** — wrap a REST method invocation, the same place a NestJS
interceptor (or exception filter) runs:

| LoopBack | NestJS |
|----------|--------|
| `beforeRemote(method, fn)` | interceptor — logic before `next.handle()` |
| `afterRemote(method, fn)` | interceptor — logic in `.pipe(map(...))` |
| `afterRemoteError(method, fn)` | *note only* — reimplement as an `ExceptionFilter` |

## Usage

```sh
./lb_hooks.py path/to/model.js                 # print the hook inventory report
./lb_hooks.py path/to/loopback-app             # scan a directory (recursively)
./lb_hooks.py path/to/loopback-app -o out-dir  # write the generated stubs
./lb_hooks.py path/to/loopback-app -o out-dir --dry-run   # list files, write nothing
./lb_hooks.py path/to/loopback-app -o out-dir --force     # overwrite a non-empty dir
```

The path may be a **single model `.js`** or a **directory**. Directories are
walked recursively (and the LoopBack-conventional `common/models`,
`server/models`, `models` first); `node_modules` is skipped. Only `.js` files
that actually contain hook calls are treated as models — the model name comes
from the `module.exports = function(Name)` parameter, falling back to the file
name.

With no `-o`, it prints a Markdown inventory report to stdout. With `-o DIR` it
writes stubs (and ships the same report as `HOOKS_REPORT.md`).

## Output

```
out-dir/
  HOOKS_REPORT.md                       # the inventory report
  <model>/
    <model>.subscriber.ts               # EntitySubscriberInterface with the mapped
                                        # lifecycle methods, each embedding the
                                        # original observe() source + a TODO
    interceptors/
      <model>-<method>-before.interceptor.ts   # per beforeRemote hook
      <model>-<method>-after.interceptor.ts    # per afterRemote hook
```

Each `.subscriber.ts` implements `EntitySubscriberInterface<Model>` with a
`listenTo()` returning the entity, and one method per mapped phase. A
`'before save'` observe is embedded into **both** `beforeInsert` and
`beforeUpdate`, mirroring LoopBack's create-and-update semantics.

Each interceptor implements `NestInterceptor` and carries a `@UseInterceptors`
binding hint in its header comment naming the controller method to attach it to.

`access`, `persist`, and `afterRemoteError` hooks have no faithful mechanical
mapping, so they are listed under **"Needs manual attention"** in the report
rather than generated.

## Assumptions and limits

- Scanning is **best-effort**: a string/comment-aware brace matcher, not a full
  JavaScript parser. It finds `.observe(...)`, `.beforeRemote(...)`,
  `.afterRemote(...)`, and `.afterRemoteError(...)` calls and captures the first
  string argument (the phase / method name) and the full call source.
- The subscriber imports `./<model>.entity` — pair this with the entity that
  `lb2nest.py` generates (or your own).
- One model per `.js` file (the LoopBack 3 convention).

## Tests

```sh
python3 lb-hooks/test_lb_hooks.py
```

Builds a temp model `.js` exercising every hook kind and checks the scan, the
generated subscriber (both `before save` targets, embedded source, TODO
markers), the interceptors, the report, and the overwrite guard. No third-party
deps.
