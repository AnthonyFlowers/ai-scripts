# lb-acl

Convert **LoopBack 3 ACLs** into a **NestJS 12 authorization scaffold**.

LoopBack 3 stores access control as data — every model `.json` can carry an
`acls` array. NestJS has no such data model: authorization is code (a
`CanActivate` guard reading route metadata set by custom decorators via the
`Reflector`). `lb_acl.py` reads the ACLs across a LoopBack app and generates
the guard, the decorators, and a report + machine-readable matrix so you can
apply the annotations mechanically.

Python 3.9+, standard library only. It emits TypeScript; nothing here runs Node.

## What it produces

A reusable `auth/` module (generated once):

| File | What it is |
|------|-----------|
| `auth/roles.decorator.ts` | `@Roles(...roles)` + `ROLES_KEY` (SetMetadata) |
| `auth/public.decorator.ts` | `@Public()` + `IS_PUBLIC_KEY` |
| `auth/roles.guard.ts` | `RolesGuard implements CanActivate`, reads metadata with `Reflector.getAllAndOverride`, honors `@Public()` |
| `auth/owner.guard.ts` | `OwnerGuard` **stub** (throws) — emitted because `$owner` ownership is not mechanically portable |
| `auth/auth.module.ts` | wires `RolesGuard` as a global `APP_GUARD` |

Plus, per run:

- **`ACL_MAP.md`** — per model: the declared ACLs, a resolved permission
  matrix, the suggested `@UseGuards`/`@Roles`/`@Public` annotations per REST
  handler, and a manual-review checklist.
- **`permission-matrix.json`** — machine-readable: `model -> method-scope ->
  allowed principals` (+ denied, public/authRequired/lockedDown flags,
  suggested annotations, and per-scope review flags).

## Usage

```sh
./lb_acl.py <app-dir>                  # print the ACL report to stdout
./lb_acl.py <app-dir> --json           # print the permission matrix (JSON)
./lb_acl.py <app-dir> -o auth-out       # write guard files + report + matrix
./lb_acl.py <app-dir> -o auth-out --dry-run   # list files, write nothing
./lb_acl.py <app-dir> -o auth-out --force     # overwrite a non-empty dir
./lb_acl.py common/models/review.json   # a single model .json
```

`<app-dir>` may be a LoopBack app root (models found under `common/models`,
`server/models`, or `models`), a models directory, or a single model JSON file.

With no `-o`, the report (or `--json` matrix) prints to stdout. With `-o`, the
scaffold is written; a non-empty output directory is refused unless `--force`.

## The mapping

Verified against the LoopBack 3 docs (Controlling-data-access,
Define-access-controls) and the NestJS guards/authorization docs.

### accessType → REST scope

| LoopBack `accessType` | NestJS scope / handlers |
|-----------------------|-------------------------|
| `READ` | GET / `find*`, `count`, `exists` → `findAll`, `findOne` |
| `WRITE` | POST/PATCH/PUT/DELETE → `create`, `update`, `remove` |
| `EXECUTE` | a named remote method (from `property`), or all custom methods |
| `*` | all of the above |

An entry with a `property` (a single method name) is treated as
method-specific regardless of `accessType`.

### principalId → guard/decorator

| LoopBack `principalId` | NestJS |
|------------------------|--------|
| `$everyone` (ALLOW) | `@Public()` — no guard |
| `$authenticated` | authentication required → `@UseGuards(AuthGuard)` |
| `$unauthenticated` | anonymous-only → TODO `AnonymousOnlyGuard` (flagged) |
| `$owner` | ownership check → `@UseGuards(OwnerGuard)` stub (flagged) |
| a role / user / app id | `@Roles('name')` + `@UseGuards(RolesGuard)` |

### permission

`ALLOW` grants; `DENY` removes. A `$everyone DENY` locks a scope down (the
common deny-by-default pattern) while more specific ALLOWs survive it. LoopBack
resolves overlapping entries by **specificity** (an exact match beats a `*`
wildcard) and, when scores tie, **DENY beats ALLOW**. `ALARM`/`AUDIT` log but
do not decide access.

Non-trivial overlaps are **flagged for manual review, never silently
collapsed**:

- the same principal with ALLOW *and* DENY at equal specificity (DENY wins);
- a DENY for a specific principal overlapping an ALLOW for another at the same
  accessType (specificity resolution needed);
- any `$owner` or `$unauthenticated` usage;
- any `ALARM`/`AUDIT` permission;
- a model with no ACLs at all (LoopBack falls back to the base model / open
  access).

## Applying the output

1. Copy `auth/` into your Nest `src/`, import `AuthModule` in your `AppModule`.
2. `RolesGuard` only checks roles — register **your** authentication guard
   (JWT/passport/etc.) in front of it so `request.user` (with
   `roles: string[]`) is populated before it runs.
3. Apply the per-handler annotations from `ACL_MAP.md`, and work the
   manual-review checklist (implement `OwnerGuard`, resolve flagged
   DENY/ALLOW overlaps, decide defaults for ACL-less models).

## Tests

```sh
python3 lb-acl/test_lb_acl.py
```

No third-party dependencies. The suite builds a throwaway LoopBack app (a
`$everyone` DENY lockdown, a `$everyone` READ ALLOW, a role ALLOW EXECUTE on a
named method, an owner write, and an ACL-less model) and asserts the resolved
matrix and the generated guard/decorator content.
