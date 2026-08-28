#!/usr/bin/env python3
"""lb_acl.py — turn LoopBack 3 ACLs into a NestJS 12 authorization scaffold.

LoopBack 3 keeps access control as data: every model .json can carry an
`acls` array whose entries name an `accessType` (READ/WRITE/EXECUTE/*), a
`principalType` (ROLE/USER/APP), a `principalId` (a role name, a user/app id,
or a dynamic role like `$everyone`/`$authenticated`/`$unauthenticated`/
`$owner`), a `permission` (ALLOW/DENY/ALARM/AUDIT) and an optional `property`
(a single method name, or `*`). LoopBack resolves overlapping entries by
specificity (exact match beats a `*` wildcard) and, when scores tie, DENY
beats ALLOW. NestJS has no such data model — authorization is code: a
`CanActivate` guard reading route metadata set by custom decorators
(`@Roles(...)`, `@Public()`) via the `Reflector`.

This script reads the ACLs across a LoopBack app and produces:

  * a reusable `auth/` module — RolesGuard, @Roles/@Public decorators, an
    OwnerGuard stub, and an AuthModule that wires the guard globally;
  * a human report (`ACL_MAP.md`) with, per model, a raw-ACL table, a
    resolved permission matrix, the suggested `@UseGuards`/`@Roles`
    annotations per REST handler, and a manual-review checklist; and
  * a machine-readable permission matrix (`permission-matrix.json`) mapping
    model + method-scope -> allowed principals, plus flags.

Deterministic mapping (see README for the full table):
  accessType  READ -> GET/find*; WRITE -> POST/PATCH/PUT/DELETE;
              EXECUTE -> a named remote method (`property`); * -> all.
  principalId $everyone -> @Public (no guard); $authenticated -> auth
              required; $unauthenticated -> anonymous-only (manual guard);
              $owner -> OwnerGuard stub (manual); a role name -> @Roles(name).
  permission  DENY removes access; a $everyone DENY locks a scope down.
              Non-trivial DENY+ALLOW overlaps are FLAGGED for manual review,
              never silently collapsed.

Python 3.9+, no dependencies. Emits TypeScript; nothing here runs Node.

Usage:
  ./lb_acl.py <app-dir>                 # print the ACL report to stdout
  ./lb_acl.py <app-dir> --json          # print the permission matrix (JSON)
  ./lb_acl.py <app-dir> -o auth-out      # write guard files + report + matrix
  ./lb_acl.py <app-dir> -o auth-out --dry-run   # list files, write nothing
  ./lb_acl.py <app-dir> -o auth-out --force     # overwrite a non-empty dir
  ./lb_acl.py common/models/review.json  # a single model .json

<app-dir> may be a LoopBack app root (models found under common/models,
server/models, or models), a models directory, or a single model JSON file.

Options:
  -o, --out DIR   write the scaffold here (default: print report to stdout)
  --json          print the permission matrix as JSON (with no -o) instead
                  of the Markdown report
  --force         overwrite an existing non-empty output directory
  --dry-run       list the files that would be written, write nothing

Exit codes: 0 ok, 1 bad args / nothing found / refusing to overwrite.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# LoopBack ACL vocabulary (verified against the LoopBack 3 docs)
# ---------------------------------------------------------------------------

ACCESS_TYPES = ("READ", "WRITE", "EXECUTE", "*")
PRINCIPAL_TYPES = ("ROLE", "USER", "APP")
PERMISSIONS = ("ALLOW", "DENY", "ALARM", "AUDIT")
DYNAMIC_ROLES = ("$everyone", "$authenticated", "$unauthenticated", "$owner")

# Non-deciding permissions: LoopBack logs/alarms but does not grant or deny.
NON_DECIDING = ("ALARM", "AUDIT")

# accessType -> the REST handlers a generated Nest controller would expose.
READ_HANDLERS = (("findAll", "@Get()"), ("findOne", "@Get(':id')"))
WRITE_HANDLERS = (("create", "@Post()"), ("update", "@Patch(':id')"),
                  ("remove", "@Delete(':id')"))

SCOPE_LABELS = {
    "READ": "READ (GET / find*, count, exists)",
    "WRITE": "WRITE (POST / PATCH / PUT / DELETE)",
    "EXECUTE": "EXECUTE (all custom remote methods)",
}


class AclError(Exception):
    pass


# ---------------------------------------------------------------------------
# Name helpers (camel/snake/kebab aware)
# ---------------------------------------------------------------------------


def _words(name):
    """Split an identifier into lowercase words (handles camel, snake, kebab)."""
    s = re.sub(r"[_\-\s]+", " ", str(name))
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


def pluralize(name):
    """Small English pluralizer — best-effort, for route names only."""
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
# Parsing a LoopBack app for its ACLs
# ---------------------------------------------------------------------------


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise AclError(f"cannot read JSON {path}: {exc}")


def normalize_acl(entry):
    """Normalize one raw ACL dict to canonical, upper-cased fields."""
    if not isinstance(entry, dict):
        raise AclError(f"ACL entry is not an object: {entry!r}")
    access = str(entry.get("accessType", "*")).upper()
    if access not in ACCESS_TYPES:
        access = "*"
    ptype = str(entry.get("principalType", "ROLE")).upper()
    if ptype not in PRINCIPAL_TYPES:
        ptype = "ROLE"
    pid = entry.get("principalId", "$everyone")
    pid = str(pid) if pid is not None else "$everyone"
    perm = str(entry.get("permission", "ALLOW")).upper()
    if perm not in PERMISSIONS:
        perm = "ALLOW"
    prop = entry.get("property")
    if isinstance(prop, list):
        prop = prop[0] if prop else None
    prop = str(prop) if prop not in (None, "") else None
    return {"accessType": access, "principalType": ptype, "principalId": pid,
            "permission": perm, "property": prop}


def parse_model_json(path):
    """Load a LoopBack model .json and return its name + normalized ACLs."""
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise AclError(f"{path}: model JSON is not an object")
    name = raw.get("name") or pascal(os.path.splitext(os.path.basename(path))[0])
    acls = [normalize_acl(a) for a in (raw.get("acls") or [])]
    return {"name": name, "base": raw.get("base", "PersistedModel"),
            "acls": acls, "source": path}


def _find_models_dirs(app_dir):
    dirs = []
    for cand in ("common/models", "server/models", "models"):
        full = os.path.join(app_dir, cand)
        if os.path.isdir(full):
            dirs.append(full)
    if not dirs and os.path.isdir(app_dir):
        if any(f.endswith(".json") for f in os.listdir(app_dir)):
            dirs.append(app_dir)
    return dirs


def load_app(path):
    """Return {'models': [...], 'app_dir': ...} from a root, models dir, or file."""
    if os.path.isfile(path) and path.endswith(".json"):
        return {"models": [parse_model_json(path)],
                "app_dir": os.path.dirname(path) or "."}
    if not os.path.isdir(path):
        raise AclError(f"not a directory or .json file: {path}")

    model_files = []
    for d in _find_models_dirs(path):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json") and fn != "model-config.json":
                model_files.append(os.path.join(d, fn))
    models = [parse_model_json(mf) for mf in model_files]
    if not models:
        raise AclError(
            f"no LoopBack model .json files found under {path} "
            "(looked in common/models, server/models, models)")
    return {"models": models, "app_dir": path}


# ---------------------------------------------------------------------------
# ACL resolution
# ---------------------------------------------------------------------------


def scopes_for(acl):
    """Which method-scopes an ACL entry covers.

    A `property` (a single named method) makes the entry method-specific
    regardless of accessType. Otherwise accessType picks the bucket, and `*`
    fans out to READ, WRITE and EXECUTE.
    """
    prop = acl["property"]
    if prop and prop != "*":
        return ["METHOD:" + prop]
    at = acl["accessType"]
    if at == "READ":
        return ["READ"]
    if at == "WRITE":
        return ["WRITE"]
    if at == "EXECUTE":
        return ["EXECUTE"]
    return ["READ", "WRITE", "EXECUTE"]  # "*"


def _specificity(acl):
    """LoopBack-style score: exact accessType/property beats a wildcard."""
    score = 0
    score += 3 if acl["accessType"] != "*" else 2
    if acl["property"] and acl["property"] != "*":
        score += 3
    else:
        score += 2
    return score


def _resolve_principal(entries):
    """Resolve one principal's entries in a scope to a single decision.

    Highest specificity wins; on a tie DENY beats ALLOW (LoopBack rule).
    Returns (permission, tie_conflict: bool). Non-deciding perms
    (ALARM/AUDIT) are ignored unless they are all there is.
    """
    deciding = [e for e in entries if e["permission"] not in NON_DECIDING]
    if not deciding:
        return "AUDIT", False  # only ALARM/AUDIT — logs, no access decision
    best = max(_specificity(e) for e in deciding)
    top = [e for e in deciding if _specificity(e) == best]
    perms = {e["permission"] for e in top}
    if "DENY" in perms and "ALLOW" in perms:
        return "DENY", True  # same principal, same specificity: DENY wins, flag
    if "DENY" in perms:
        return "DENY", False
    return "ALLOW", False


def resolve_scope(acls, scope):
    """Resolve every principal for one scope. Returns a scope record dict."""
    covering = [a for a in acls if scope in scopes_for(a)]
    by_principal = defaultdict(list)
    for a in covering:
        by_principal[a["principalId"]].append(a)

    allow, deny, audit, conflicts = [], [], [], []
    for pid, entries in by_principal.items():
        perm, tie = _resolve_principal(entries)
        if tie:
            conflicts.append(pid)
        if perm == "ALLOW":
            allow.append(pid)
        elif perm == "DENY":
            deny.append(pid)
        else:
            audit.append(pid)
    allow, deny, audit = sorted(set(allow)), sorted(set(deny)), sorted(set(audit))

    public = "$everyone" in allow
    locked_down = ("$everyone" in deny) or (not allow)
    # Specific ALLOWs survive a broad $everyone DENY (higher principal
    # specificity). If everyone is allowed outright the scope is public.
    effective_allow = ["$everyone"] if public else allow

    flags = []
    for pid in conflicts:
        flags.append(f"same principal `{pid}` has ALLOW and DENY at equal "
                     f"specificity in {scope}: DENY wins per LoopBack — confirm intent")
    narrow_denies = [d for d in deny if d != "$everyone"]
    if narrow_denies and allow:
        flags.append(f"DENY for {narrow_denies} overlaps ALLOW for {allow} in "
                     f"{scope}: LoopBack resolves this by principal specificity — "
                     "review the intended precedence manually")
    if "$owner" in allow or "$owner" in deny:
        flags.append(f"$owner used in {scope}: ownership is not mechanically "
                     "portable — implement OwnerGuard")
    if "$unauthenticated" in allow:
        flags.append(f"$unauthenticated ALLOW in {scope}: anonymous-only access — "
                     "implement an AnonymousOnlyGuard")
    if audit:
        flags.append(f"ALARM/AUDIT permission for {audit} in {scope}: this logs, "
                     "it does not grant/deny — reimplement as an interceptor")

    return {
        "scope": scope,
        "allow": allow,
        "deny": deny,
        "audit": audit,
        "effective_allow": effective_allow,
        "public": public,
        "auth_required": (not public) and bool(effective_allow),
        "locked_down": locked_down and not public,
        "flags": flags,
        "annotations": None,   # filled by scope_annotations
    }


def model_scopes(model):
    """Ordered list of scope keys present for a model, resolved."""
    acls = model["acls"]
    present = []
    for a in acls:
        for s in scopes_for(a):
            if s not in present:
                present.append(s)
    # stable order: READ, WRITE, EXECUTE, then named methods alphabetically
    order = {"READ": 0, "WRITE": 1, "EXECUTE": 2}
    named = sorted(s for s in present if s.startswith("METHOD:"))
    buckets = sorted((s for s in present if not s.startswith("METHOD:")),
                     key=lambda s: order.get(s, 9))
    records = []
    for s in buckets + named:
        rec = resolve_scope(acls, s)
        rec["annotations"] = scope_annotations(rec)
        records.append(rec)
    return records


def scope_label(scope):
    if scope.startswith("METHOD:"):
        return "method: " + scope[len("METHOD:"):]
    return SCOPE_LABELS.get(scope, scope)


def scope_handlers(scope):
    """REST handler names a generated controller would carry for a scope."""
    if scope == "READ":
        return list(READ_HANDLERS)
    if scope == "WRITE":
        return list(WRITE_HANDLERS)
    if scope == "EXECUTE":
        return [("<each custom remote-method handler>", "@Post()/@Get() (per method)")]
    if scope.startswith("METHOD:"):
        return [(camel(scope[len("METHOD:"):]), "custom handler")]
    return []


# ---------------------------------------------------------------------------
# Suggested NestJS annotations
# ---------------------------------------------------------------------------


def scope_annotations(rec):
    """Suggested @UseGuards/@Roles/@Public lines for a resolved scope."""
    if rec["public"]:
        return ["@Public()"]
    allow = rec["effective_allow"]
    if not allow:
        return ["// LOCKED DOWN: no principal is granted this access; every "
                "request is denied. Confirm this is intended."]

    roles = [p for p in allow if not p.startswith("$")]
    dyn = [p for p in allow if p.startswith("$")]
    owner = "$owner" in dyn
    anon = "$unauthenticated" in dyn
    authd = "$authenticated" in dyn

    guards = []
    if roles:
        guards.append("RolesGuard")
    if owner:
        guards.append("OwnerGuard")
    if anon:
        guards.append("AnonymousOnlyGuard")
    if authd and not roles and not owner and not anon:
        guards.append("AuthGuard")

    anns = []
    if guards:
        anns.append("@UseGuards(" + ", ".join(dict.fromkeys(guards)) + ")")
    if roles:
        anns.append("@Roles(" + ", ".join(f"'{r}'" for r in roles) + ")")
    todo = []
    if owner:
        todo.append("$owner -> implement OwnerGuard (ownership check)")
    if anon:
        todo.append("$unauthenticated -> implement AnonymousOnlyGuard")
    if authd and (roles or owner):
        pass  # auth implied by RolesGuard/OwnerGuard running after AuthGuard
    if todo:
        anns.append("// TODO: " + "; ".join(todo))
    if not anns:
        anns.append("// requires authentication (no specific role)")
    return anns


# ---------------------------------------------------------------------------
# Machine-readable permission matrix
# ---------------------------------------------------------------------------


def build_matrix(app):
    models_out = {}
    checklist = set()
    for m in sorted(app["models"], key=lambda x: x["name"]):
        recs = model_scopes(m)
        scopes = {}
        for r in recs:
            scopes[r["scope"]] = {
                "label": scope_label(r["scope"]),
                "allowedPrincipals": r["effective_allow"],
                "deniedPrincipals": r["deny"],
                "public": r["public"],
                "authRequired": r["auth_required"],
                "lockedDown": r["locked_down"],
                "restHandlers": [f"{h}  {d}" for h, d in scope_handlers(r["scope"])],
                "annotations": r["annotations"],
                "flags": r["flags"],
            }
            for f in r["flags"]:
                checklist.add(f"{m['name']}: {f}")
        models_out[m["name"]] = {
            "source": m["source"],
            "base": m["base"],
            "aclCount": len(m["acls"]),
            "scopes": scopes,
        }
        if not m["acls"]:
            checklist.add(f"{m['name']}: no ACLs declared — LoopBack falls back to "
                          f"the base model ({m['base']}) ACLs / open access; decide "
                          "the intended default guard explicitly")
    return {
        "generatedBy": "lb_acl.py",
        "note": "model -> method-scope -> allowed principals. $everyone means "
                "public; $authenticated/$owner/$unauthenticated are dynamic "
                "roles; other ids are LoopBack role/user/app principals.",
        "models": models_out,
        "manualReviewChecklist": sorted(checklist),
    }


# ---------------------------------------------------------------------------
# Human report (ACL_MAP.md)
# ---------------------------------------------------------------------------


def _md_escape(text):
    return str(text).replace("|", "\\|")


def build_report(app):
    models = sorted(app["models"], key=lambda x: x["name"])
    total_acls = sum(len(m["acls"]) for m in models)
    lines = ["# LoopBack ACLs -> NestJS authorization", ""]
    lines.append(f"Source: `{app['app_dir']}`  ")
    lines.append(f"Models: **{len(models)}** · ACL entries: **{total_acls}**")
    lines.append("")
    lines.append("Reusable scaffold generated under `auth/`: `RolesGuard`, "
                 "`@Roles()`, `@Public()`, an `OwnerGuard` stub, and `AuthModule`. "
                 "Suggested annotations below assume an upstream auth guard has set "
                 "`request.user` (with `roles: string[]`).")
    lines.append("")

    checklist = set()
    for m in models:
        lines.append(f"## {m['name']}")
        lines.append("")
        lines.append(f"Base: `{m['base']}` · Source: `{m['source']}`")
        lines.append("")
        if not m["acls"]:
            lines.append("_No `acls` declared — LoopBack falls back to the base "
                         "model's ACLs (often open access). Decide the intended "
                         "default guard explicitly._")
            lines.append("")
            checklist.add(f"{m['name']}: no ACLs declared — decide the default guard "
                          "(the base model may grant open access)")
            continue

        # Raw ACL table
        lines.append("### Declared ACLs")
        lines.append("")
        lines.append("| accessType | principalType | principalId | permission | property |")
        lines.append("|------------|---------------|-------------|------------|----------|")
        for a in m["acls"]:
            lines.append("| {} | {} | {} | {} | {} |".format(
                a["accessType"], a["principalType"], _md_escape(a["principalId"]),
                a["permission"], a["property"] or "*"))
        lines.append("")

        # Resolved matrix
        recs = model_scopes(m)
        lines.append("### Resolved permission matrix")
        lines.append("")
        lines.append("| Scope | Allowed principals | Denied | Public | Auth req. | Locked |")
        lines.append("|-------|--------------------|--------|:------:|:---------:|:------:|")
        for r in recs:
            allow = ", ".join(_md_escape(p) for p in r["effective_allow"]) or "—"
            deny = ", ".join(_md_escape(p) for p in r["deny"]) or "—"
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                scope_label(r["scope"]), allow, deny,
                "yes" if r["public"] else "no",
                "yes" if r["auth_required"] else "no",
                "yes" if r["locked_down"] else "no"))
        lines.append("")

        # Suggested annotations
        lines.append("### Suggested controller annotations")
        lines.append("")
        for r in recs:
            handlers = scope_handlers(r["scope"])
            hlist = ", ".join(f"`{h}` {d}" for h, d in handlers) or "—"
            lines.append(f"- **{scope_label(r['scope'])}** → {hlist}")
            for ann in r["annotations"]:
                lines.append(f"  - `{ann}`")
            for f in r["flags"]:
                checklist.add(f"{m['name']}: {f}")
        lines.append("")

    lines.append("## Manual-review checklist")
    lines.append("")
    if checklist:
        for item in sorted(checklist):
            lines.append(f"- [ ] {item}")
    else:
        lines.append("_Nothing flagged — all ACLs mapped cleanly._")
    lines.append("")
    lines.append("### Reminders")
    lines.append("- `RolesGuard` only checks roles; register your authentication "
                 "guard (JWT/passport/etc.) in front of it so `request.user` exists.")
    lines.append("- LoopBack resolves overlapping ACLs by specificity (exact beats "
                 "`*`) and, on a tie, DENY beats ALLOW. Flagged overlaps above were "
                 "not collapsed automatically.")
    lines.append("- Pluralized/kebab route names are best-effort; confirm against "
                 "your controllers.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reusable auth/ scaffold (static TypeScript)
# ---------------------------------------------------------------------------

ROLES_DECORATOR_TS = """import { SetMetadata } from '@nestjs/common';

/** Metadata key under which required role names are stored. */
export const ROLES_KEY = 'roles';

/**
 * Attach one or more required role names to a route handler or controller.
 * Generated from LoopBack ACL entries whose principalId was a named ROLE.
 *
 *   @Roles('admin', 'auditor')
 */
export const Roles = (...roles: string[]) => SetMetadata(ROLES_KEY, roles);
"""

PUBLIC_DECORATOR_TS = """import { SetMetadata } from '@nestjs/common';

/** Metadata key marking a handler as publicly accessible (no auth). */
export const IS_PUBLIC_KEY = 'isPublic';

/**
 * Mark a route handler or controller as public: RolesGuard lets it through
 * without any role check. Generated for LoopBack ACLs that granted ALLOW to
 * the dynamic role $everyone.
 *
 *   @Public()
 */
export const Public = () => SetMetadata(IS_PUBLIC_KEY, true);
"""

ROLES_GUARD_TS = """import { CanActivate, ExecutionContext, Injectable } from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { IS_PUBLIC_KEY } from './public.decorator';
import { ROLES_KEY } from './roles.decorator';

/**
 * Role-based guard generated from LoopBack 3 ACLs by lb_acl.py.
 *
 * It expects an upstream authentication guard/middleware to have populated
 * `request.user` with a `roles: string[]` array (the LoopBack principalId
 * role names). Wire your real auth in front of it — see auth.module.ts.
 *
 * Handlers/classes carrying @Public() are always allowed. Handlers/classes
 * carrying @Roles(...) require the user to hold at least one listed role.
 * Anything with neither is allowed (nothing role-specific to enforce).
 */
@Injectable()
export class RolesGuard implements CanActivate {
  constructor(private readonly reflector: Reflector) {}

  canActivate(context: ExecutionContext): boolean {
    const isPublic = this.reflector.getAllAndOverride<boolean>(IS_PUBLIC_KEY, [
      context.getHandler(),
      context.getClass(),
    ]);
    if (isPublic) {
      return true;
    }

    const requiredRoles = this.reflector.getAllAndOverride<string[]>(ROLES_KEY, [
      context.getHandler(),
      context.getClass(),
    ]);
    if (!requiredRoles || requiredRoles.length === 0) {
      return true;
    }

    const { user } = context.switchToHttp().getRequest();
    const userRoles: string[] = user?.roles ?? [];
    return requiredRoles.some((role) => userRoles.includes(role));
  }
}
"""

OWNER_GUARD_TS = """import { CanActivate, ExecutionContext, Injectable } from '@nestjs/common';

/**
 * TODO (manual): ownership guard stubbed out because one or more LoopBack ACLs
 * used the dynamic role `$owner`. LoopBack resolved ownership by comparing the
 * authenticated user id against the target model instance's owner/foreign key.
 * There is no generic equivalent — implement the check against your model and
 * data layer, then return true/false instead of throwing.
 */
@Injectable()
export class OwnerGuard implements CanActivate {
  async canActivate(context: ExecutionContext): Promise<boolean> {
    const request = context.switchToHttp().getRequest();
    const user = request.user;
    // TODO: load the target record (e.g. by request.params.id) and compare its
    //       owner id with user.id. Return false to deny access.
    void user;
    throw new Error(
      'OwnerGuard not implemented (migrated from a LoopBack $owner ACL)',
    );
  }
}
"""

AUTH_MODULE_TS = """import { Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { RolesGuard } from './roles.guard';

/**
 * Reusable authorization module generated from LoopBack 3 ACLs by lb_acl.py.
 *
 * Registers RolesGuard globally, so every route is role-checked unless it (or
 * its controller) is marked @Public(). Prefer per-controller opt-in? Remove
 * the providers block and use `@UseGuards(RolesGuard)` on controllers instead.
 *
 * NOTE: RolesGuard only checks roles; it assumes authentication already ran and
 * set `request.user`. Register your AuthGuard (JWT/passport/etc.) BEFORE this
 * one so `request.user.roles` is populated by the time RolesGuard runs.
 */
@Module({
  providers: [
    {
      provide: APP_GUARD,
      useClass: RolesGuard,
    },
  ],
})
export class AuthModule {}
"""


def auth_files():
    return {
        "auth/roles.decorator.ts": ROLES_DECORATOR_TS,
        "auth/public.decorator.ts": PUBLIC_DECORATOR_TS,
        "auth/roles.guard.ts": ROLES_GUARD_TS,
        "auth/owner.guard.ts": OWNER_GUARD_TS,
        "auth/auth.module.ts": AUTH_MODULE_TS,
    }


# ---------------------------------------------------------------------------
# generate: assemble the file map
# ---------------------------------------------------------------------------


def build_files(app):
    files = dict(auth_files())
    files["ACL_MAP.md"] = build_report(app)
    files["permission-matrix.json"] = json.dumps(build_matrix(app), indent=2) + "\n"
    return files


def write_files(files, out_dir, force, dry_run):
    if os.path.isdir(out_dir) and os.listdir(out_dir) and not force and not dry_run:
        raise AclError(
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
        prog="lb_acl.py",
        description="Convert LoopBack 3 ACLs into a NestJS 12 authorization scaffold.")
    parser.add_argument("app", help="LoopBack app dir, models dir, or a model .json")
    parser.add_argument("-o", "--out",
                        help="write the scaffold here (default: print report to stdout)")
    parser.add_argument("--json", action="store_true",
                        help="with no -o, print the permission matrix as JSON")
    parser.add_argument("--force", action="store_true",
                        help="overwrite a non-empty output directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="list files that would be written, write nothing")

    args = parser.parse_args(argv)
    try:
        app = load_app(args.app)
        if not args.out:
            if args.json:
                print(json.dumps(build_matrix(app), indent=2))
            else:
                print(build_report(app))
            return 0

        files = build_files(app)
        written = write_files(files, args.out, args.force, args.dry_run)
        verb = "would write" if args.dry_run else "wrote"
        for w in written:
            print(f"{verb} {w}")
        print(f"\n{len(written)} files, {len(app['models'])} models -> {args.out}",
              file=sys.stderr)
        if not args.dry_run:
            print("Next: import AuthModule in your AppModule, put an auth guard in "
                  "front of RolesGuard, then apply the annotations from ACL_MAP.md.",
                  file=sys.stderr)
        return 0
    except AclError as exc:
        print(f"lb_acl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
