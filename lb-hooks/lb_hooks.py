#!/usr/bin/env python3
"""lb_hooks.py — inventory LoopBack 3 model hooks and scaffold NestJS 12 equivalents.

LoopBack 3 models attach behavior in their `.js` file through two hook systems:

  * **Operation hooks** — `Model.observe('before save' | 'after save' |
    'before delete' | 'after delete' | 'access' | 'loaded' | 'persist', fn)` —
    fire around the persistence layer (create/update/delete/find), the same
    place TypeORM's **EntitySubscriber** lifecycle methods run.
  * **Remote hooks** — `Model.beforeRemote(method, fn)`,
    `Model.afterRemote(method, fn)`, `Model.afterRemoteError(method, fn)` —
    wrap a REST method invocation, the same place a NestJS **interceptor**
    (or exception filter) runs.

This script scans those `.js` files, inventories every hook, and generates
NestJS 12 scaffolding to reimplement them. It does NOT transpile the JavaScript
body — it embeds the *original LoopBack source* as a reference comment inside
each generated stub, with a `// TODO: port` marker, so a human (or an agent)
can port the logic with the original in front of them.

Deterministic mapping (grounded in the LoopBack, TypeORM, and NestJS docs):

  Operation hook (Model.observe)   -> TypeORM EntitySubscriberInterface method
  --------------------------------    -----------------------------------------
  'before save'                       beforeInsert + beforeUpdate   (LB before
                                      save fires for BOTH create and update)
  'after save'                        afterInsert  + afterUpdate
  'before delete'                     beforeRemove
  'after delete'                      afterRemove
  'loaded'                            afterLoad
  'access'                            note only (closest: a Nest guard / a
                                      repository query filter — no lifecycle hook)
  'persist'                           note only (no clean TypeORM equivalent)

  Remote hook                       -> NestJS artifact
  --------------------------------    -----------------------------------------
  beforeRemote(method, fn)            interceptor (logic before next.handle())
  afterRemote(method, fn)             interceptor (logic in .pipe(map(...)))
  afterRemoteError(method, fn)        note only (reimplement as an ExceptionFilter)

Scanning is best-effort and regex/brace-matching based (LoopBack `.js` files
are hand-written, so this is heuristic, not a full JS parse). One model per
`.js` file; the model name comes from the `module.exports = function(Name)`
parameter, falling back to the file name.

Python 3.9+, no dependencies. Emits TypeScript; nothing here runs Node.

Usage:
  ./lb_hooks.py <path>                    # print the hook inventory report
  ./lb_hooks.py <path> -o out-dir         # write the NestJS stubs into out-dir
  ./lb_hooks.py <path> -o out-dir --dry-run   # list files, write nothing
  ./lb_hooks.py <path> -o out-dir --force     # overwrite a non-empty out-dir

<path> may be a single model `.js` file or a directory (scanned recursively,
plus the LoopBack-conventional common/models, server/models, models dirs).
Only `.js` files that actually contain hook calls are treated as models.

Options:
  -o, --out DIR   write generated stubs here (default: print report to stdout)
  --dry-run       with --out: list the files that would be written, write nothing
  --force         with --out: overwrite a non-empty output directory

Exit codes: 0 ok, 1 bad args / nothing found / refusing to overwrite.
"""

import argparse
import os
import re
import sys


class HookError(Exception):
    pass


# ---------------------------------------------------------------------------
# Name helpers  (identical conventions to lb2nest.py)
# ---------------------------------------------------------------------------


def _words(name):
    """Split an identifier into lowercase words (handles camel, snake, kebab)."""
    s = re.sub(r"[_\-\s.]+", " ", name)
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


# ---------------------------------------------------------------------------
# Hook mapping tables
# ---------------------------------------------------------------------------

# LoopBack operation-hook phase -> TypeORM EntitySubscriber method(s).
OP_MAP = {
    "before save": ["beforeInsert", "beforeUpdate"],
    "after save": ["afterInsert", "afterUpdate"],
    "before delete": ["beforeRemove"],
    "after delete": ["afterRemove"],
    "loaded": ["afterLoad"],
}

# Operation-hook phases with no clean TypeORM lifecycle equivalent (report only).
OP_NOTE = {
    "access": "no lifecycle equivalent — closest is a Nest guard or a "
              "repository query filter applied on every read/write.",
    "persist": "no clean TypeORM equivalent — it fires between the JS instance "
               "and the raw datasource payload; port into beforeInsert/"
               "beforeUpdate only if you truly need the pre-serialization shape.",
}

# TypeORM subscriber method -> its event-argument type.
METHOD_EVENT = {
    "beforeInsert": "InsertEvent",
    "afterInsert": "InsertEvent",
    "beforeUpdate": "UpdateEvent",
    "afterUpdate": "UpdateEvent",
    "beforeRemove": "RemoveEvent",
    "afterRemove": "RemoveEvent",
    "afterLoad": "LoadEvent",
}

# Canonical emission order for subscriber methods.
METHOD_ORDER = ["afterLoad", "beforeInsert", "afterInsert",
                "beforeUpdate", "afterUpdate", "beforeRemove", "afterRemove"]

# A one-line semantic note embedded in each generated subscriber method.
METHOD_NOTE = {
    "beforeInsert": "LoopBack 'before save' fires for BOTH create and update; "
                    "this is the create half. `event.entity` is the new instance.",
    "afterInsert": "LoopBack 'after save' fires for BOTH create and update; "
                   "this is the create half. `event.entity` is the saved row.",
    "beforeUpdate": "LoopBack 'before save' fires for BOTH create and update; "
                    "this is the update half. `event.entity` = new, "
                    "`event.databaseEntity` = old (only changed columns trigger it).",
    "afterUpdate": "LoopBack 'after save' fires for BOTH create and update; "
                   "this is the update half. `event.entity` holds the saved values.",
    "beforeRemove": "LoopBack 'before delete' — `event.entity` / `event.entityId` "
                    "identify the target (LB `ctx.where` has no 1:1 equal here).",
    "afterRemove": "LoopBack 'after delete' — runs after the row is removed.",
    "afterLoad": "LoopBack 'loaded' — runs after hydration; where LB gave you the "
                 "raw `ctx.data`, here `entity` is the constructed model.",
}


# ---------------------------------------------------------------------------
# JS scanning  (best-effort; string/comment-aware brace matching)
# ---------------------------------------------------------------------------

_HOOK_CALL = re.compile(
    r"(\b[\w$]+)\s*\.\s*(observe|beforeRemote|afterRemote|afterRemoteError)\s*\(")
_EXPORT_FN = re.compile(
    r"module\.exports\s*=\s*function\s*\(\s*([\w$]+)")


def _skip_string(text, i):
    """Given text[i] is a quote char, return the index just past the close quote."""
    q = text[i]
    i += 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == q:
            return i + 1
        i += 1
    return n


def _match_paren(text, i):
    """text[i] is '('. Return index of the matching ')', or None. String/comment aware."""
    n = len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c in "'\"`":
            i = _skip_string(text, i)
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _first_string_arg(text, start, end):
    """Return the content of the first string literal in text[start:end], or None."""
    i = start
    while i < end:
        c = text[i]
        if c in "'\"`":
            j = _skip_string(text, i)
            return text[i + 1:j - 1]
        if c == "/" and i + 1 < end and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = end if nl < 0 else nl
            continue
        if c == "/" and i + 1 < end and text[i + 1] == "*":
            cl = text.find("*/", i + 2)
            i = end if cl < 0 else cl + 2
            continue
        i += 1
    return None


def scan_hooks(text):
    """Best-effort scan of a model `.js` body. Return a list of hook dicts:
    {kind, name, receiver, source}. `source` is the full original call text."""
    hooks = []
    for m in _HOOK_CALL.finditer(text):
        receiver, kind = m.group(1), m.group(2)
        open_paren = m.end() - 1
        close = _match_paren(text, open_paren)
        if close is None:
            continue
        name = _first_string_arg(text, open_paren + 1, close)
        if name is None:
            continue
        src = text[m.start():close + 1]
        if close + 1 < len(text) and text[close + 1] == ";":
            src += ";"
        hooks.append({"kind": kind, "name": name, "receiver": receiver,
                      "source": src.strip()})
    return hooks


def model_name_from_js(text, path):
    """Model name: the `module.exports = function(Name)` param, else the filename."""
    m = _EXPORT_FN.search(text)
    if m:
        return m.group(1)
    return pascal(os.path.splitext(os.path.basename(path))[0])


def scan_file(path):
    """Return a model dict for one `.js` file, or None if it has no hooks."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise HookError(f"cannot read {path}: {exc}")
    hooks = scan_hooks(text)
    if not hooks:
        return None
    return {
        "name": model_name_from_js(text, path),
        "source_file": path,
        "hooks": hooks,
    }


def _iter_js_files(path):
    """Yield candidate `.js` files for a file-or-directory input path."""
    if os.path.isfile(path):
        if path.endswith(".js"):
            yield path
        return
    if not os.path.isdir(path):
        raise HookError(f"not a directory or .js file: {path}")
    seen = set()
    # Prefer LoopBack-conventional model dirs, then a recursive sweep.
    roots = []
    for cand in ("common/models", "server/models", "models"):
        full = os.path.join(path, cand)
        if os.path.isdir(full):
            roots.append(full)
    roots.append(path)
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            if "node_modules" in dirpath.split(os.sep):
                continue
            for fn in sorted(files):
                if fn.endswith(".js"):
                    ap = os.path.abspath(os.path.join(dirpath, fn))
                    if ap not in seen:
                        seen.add(ap)
                        yield os.path.join(dirpath, fn)


def scan_path(path):
    """Scan a file/dir and return the list of model dicts that carry hooks."""
    models = []
    for jsf in _iter_js_files(path):
        model = scan_file(jsf)
        if model:
            models.append(model)
    return sorted(models, key=lambda m: m["name"])


# ---------------------------------------------------------------------------
# Hook classification helpers
# ---------------------------------------------------------------------------


def op_hooks(model):
    return [h for h in model["hooks"] if h["kind"] == "observe"]


def remote_hooks(model):
    return [h for h in model["hooks"]
            if h["kind"] in ("beforeRemote", "afterRemote")]


def error_hooks(model):
    return [h for h in model["hooks"] if h["kind"] == "afterRemoteError"]


def note_op_hooks(model):
    """Operation hooks with no TypeORM lifecycle equivalent (access/persist/unknown)."""
    return [h for h in op_hooks(model) if h["name"] not in OP_MAP]


def subscriber_methods(model):
    """method name -> list of (phase, source) for every mapped operation hook."""
    out = {}
    for h in op_hooks(model):
        for method in OP_MAP.get(h["name"], []):
            out.setdefault(method, []).append((h["name"], h["source"]))
    return out


# ---------------------------------------------------------------------------
# TypeScript generation
# ---------------------------------------------------------------------------


def _as_comment(src, indent="    "):
    lines = src.splitlines() or [""]
    return "\n".join((indent + "// " + ln) if ln.strip() else (indent + "//")
                     for ln in lines)


def _method_ident(raw):
    """Sanitize a remote-hook method name ('prototype.updateAttributes', '*.find',
    'create') into words safe for pascal/kebab. '*' / empty -> 'all'."""
    cleaned = raw.replace("prototype.", " ").replace("*", " ").replace(".", " ")
    return cleaned if cleaned.strip() else "all"


def gen_subscriber(model):
    """Return the `<model>.subscriber.ts` content, or None if no mapped op hooks."""
    methods = subscriber_methods(model)
    if not methods:
        return None
    name = pascal(model["name"])
    kn = kebab(model["name"])

    event_types = {METHOD_EVENT[m] for m in methods}
    typeorm_imports = sorted({"EntitySubscriberInterface", "EventSubscriber"}
                             | event_types)

    body = []
    for method in METHOD_ORDER:
        if method not in methods:
            continue
        ev = METHOD_EVENT[method]
        if method == "afterLoad":
            sig = (f"  afterLoad(entity: {name}, event?: {ev}<{name}>)"
                   ": void | Promise<any> {")
        else:
            sig = f"  {method}(event: {ev}<{name}>): void | Promise<any> {{"
        lines = [sig]
        for phase, src in methods[method]:
            lines.append(f"    // ---- LoopBack '{phase}' hook — original source ----")
            lines.append(_as_comment(src, "    "))
        lines.append(f"    // {METHOD_NOTE[method]}")
        lines.append("    // TODO: port the LoopBack hook body shown above.")
        lines.append("  }")
        body.append("\n".join(lines))

    imports = ("import {\n  " + ",\n  ".join(typeorm_imports)
               + ",\n} from 'typeorm';\n"
               f"import {{ {name} }} from './{kn}.entity';\n\n")
    header = (f"// Generated by lb_hooks.py from {model['source_file']}\n"
              "// Register this subscriber with your DataSource (subscribers: [...])\n"
              "// or let TypeORM auto-load it via the `subscribers` glob.\n\n")
    return (header + imports
            + "@EventSubscriber()\n"
            f"export class {name}Subscriber "
            f"implements EntitySubscriberInterface<{name}> {{\n"
            "  listenTo() {\n"
            f"    return {name};\n"
            "  }\n\n"
            + "\n\n".join(body)
            + "\n}\n")


def gen_interceptor(model, hook):
    """Return (relpath, content) for one before/after remote hook."""
    name = pascal(model["name"])
    kn = kebab(model["name"])
    method_raw = hook["name"]
    ident = _method_ident(method_raw)
    phase = "Before" if hook["kind"] == "beforeRemote" else "After"
    cls = f"{name}{pascal(ident)}{phase}Interceptor"
    fname = f"{kn}-{kebab(ident)}-{phase.lower()}.interceptor.ts"
    handler = camel(ident)

    ref = [f"    // ---- LoopBack {hook['kind']}('{method_raw}') hook — original source ----"]
    ref.append(_as_comment(hook["source"], "    "))

    header = (f"// Generated by lb_hooks.py from {model['source_file']}\n"
              f"// Bind with @UseInterceptors({cls}) on "
              f"{name}Controller.{handler} (the '{method_raw}' method).\n\n")
    common_imports = ("import {\n"
                      "  CallHandler,\n"
                      "  ExecutionContext,\n"
                      "  Injectable,\n"
                      "  NestInterceptor,\n"
                      "} from '@nestjs/common';\n"
                      "import { Observable } from 'rxjs';\n")

    if hook["kind"] == "beforeRemote":
        content = (header + common_imports + "\n"
                   "@Injectable()\n"
                   f"export class {cls} implements NestInterceptor {{\n"
                   "  intercept(context: ExecutionContext, next: CallHandler): "
                   "Observable<any> {\n"
                   + "\n".join(ref) + "\n"
                   "    // This runs BEFORE the controller handler. LoopBack `ctx.args`\n"
                   "    // / `ctx.req` -> context.switchToHttp().getRequest(); mutate the\n"
                   "    // request here, then fall through to next.handle().\n"
                   "    // TODO: port the LoopBack hook body shown above.\n"
                   "    return next.handle();\n"
                   "  }\n"
                   "}\n")
    else:  # afterRemote
        content = (header + common_imports
                   + "import { map } from 'rxjs/operators';\n\n"
                   "@Injectable()\n"
                   f"export class {cls} implements NestInterceptor {{\n"
                   "  intercept(context: ExecutionContext, next: CallHandler): "
                   "Observable<any> {\n"
                   + "\n".join(ref) + "\n"
                   "    // This runs AFTER the controller handler. LoopBack `ctx.result`\n"
                   "    // -> the emitted `data` below; transform and return it.\n"
                   "    // TODO: port the LoopBack hook body shown above.\n"
                   "    return next.handle().pipe(\n"
                   "      map((data) => {\n"
                   "        return data;\n"
                   "      }),\n"
                   "    );\n"
                   "  }\n"
                   "}\n")
    return fname, content


def build_files(models):
    """Return {relpath: content} for every generated stub, plus the report."""
    files = {}
    for model in models:
        kn = kebab(model["name"])
        sub = gen_subscriber(model)
        if sub is not None:
            files[f"{kn}/{kn}.subscriber.ts"] = sub
        for hook in remote_hooks(model):
            fname, content = gen_interceptor(model, hook)
            files[f"{kn}/interceptors/{fname}"] = content
    files["HOOKS_REPORT.md"] = build_report(models)
    return files


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_report(models, source_label=None):
    lines = ["# LoopBack hooks -> NestJS report", ""]
    if source_label:
        lines.append(f"Source: `{source_label}`  ")
    n_op = sum(len(op_hooks(m)) for m in models)
    n_remote = sum(len(remote_hooks(m)) for m in models)
    n_err = sum(len(error_hooks(m)) for m in models)
    lines.append(f"Models with hooks: **{len(models)}**  ")
    lines.append(f"Operation hooks: {n_op} · Remote hooks: {n_remote} · "
                 f"afterRemoteError hooks: {n_err}")
    lines.append("")

    if not models:
        lines.append("_No LoopBack hooks found._")
        lines.append("")
        return "\n".join(lines)

    # Operation hooks
    lines.append("## Operation hooks (Model.observe) -> TypeORM subscribers")
    lines.append("")
    lines.append("| Model | Phase | Maps to |")
    lines.append("|-------|-------|---------|")
    for m in models:
        for h in op_hooks(m):
            mapped = OP_MAP.get(h["name"])
            target = " + ".join(mapped) if mapped else "_note (see below)_"
            lines.append(f"| {m['name']} | `{h['name']}` | {target} |")
    lines.append("")

    # Remote hooks
    lines.append("## Remote hooks -> NestJS interceptors")
    lines.append("")
    if n_remote:
        lines.append("| Model | Hook | Method | Maps to |")
        lines.append("|-------|------|--------|---------|")
        for m in models:
            for h in remote_hooks(m):
                lines.append(f"| {m['name']} | `{h['kind']}` | `{h['name']}` "
                             "| interceptor |")
        lines.append("")
    else:
        lines.append("_None found._")
        lines.append("")

    # Files
    files = {k: v for k, v in build_files_paths(models)}
    lines.append("## Generated files")
    lines.append("")
    if files:
        for rel in sorted(files):
            lines.append(f"- `{rel}` — {files[rel]}")
    else:
        lines.append("_No subscriber/interceptor stubs (only note-only hooks)._")
    lines.append("")

    # Manual attention
    lines.append("## Needs manual attention (no faithful mechanical mapping)")
    lines.append("")
    notes = []
    for m in models:
        for h in note_op_hooks(m):
            why = OP_NOTE.get(h["name"],
                              "unrecognized operation-hook phase — port by hand.")
            notes.append(f"**{m['name']} `{h['name']}`** — {why}")
    for m in models:
        for h in error_hooks(m):
            notes.append(f"**{m['name']} afterRemoteError(`{h['name']}`)** — "
                         "reimplement as a NestJS `ExceptionFilter` "
                         "(`@Catch()` + `@UseFilters(...)` on the controller/method).")
    if notes:
        for note in notes:
            lines.append(f"- [ ] {note}")
    else:
        lines.append("- None. Every hook found has a generated stub.")
    lines.append("")
    lines.append("> Bodies are NOT transpiled. Each stub embeds the original "
                 "LoopBack source as a comment with a `// TODO: port` marker.")
    lines.append("")
    return "\n".join(lines)


def build_files_paths(models):
    """(relpath, short-description) pairs for the report's file listing."""
    out = []
    for model in models:
        kn = kebab(model["name"])
        if gen_subscriber(model) is not None:
            methods = subscriber_methods(model)
            desc = "EntitySubscriber: " + ", ".join(
                m for m in METHOD_ORDER if m in methods)
            out.append((f"{kn}/{kn}.subscriber.ts", desc))
        for hook in remote_hooks(model):
            fname, _ = gen_interceptor(model, hook)
            out.append((f"{kn}/interceptors/{fname}",
                        f"interceptor for {hook['kind']}('{hook['name']}')"))
    return out


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write_files(files, out_dir, force, dry_run):
    if os.path.isdir(out_dir) and os.listdir(out_dir) and not force and not dry_run:
        raise HookError(
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
        prog="lb_hooks.py",
        description="Inventory LoopBack 3 model hooks and scaffold NestJS 12 "
                    "subscribers/interceptors to reimplement them.")
    parser.add_argument("path", help="a model .js file or a directory to scan")
    parser.add_argument("-o", "--out",
                        help="write generated stubs here (default: print report)")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --out: list files, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="with --out: overwrite a non-empty output directory")

    args = parser.parse_args(argv)

    try:
        models = scan_path(args.path)

        if not args.out:
            if args.dry_run:
                print("lb_hooks: --dry-run has no effect without --out; "
                      "printing the report.", file=sys.stderr)
            print(build_report(models, source_label=args.path))
            return 0

        if not models:
            raise HookError(f"no LoopBack hooks found under {args.path}; "
                            "nothing to generate")
        files = build_files(models)
        written = write_files(files, args.out, args.force, args.dry_run)
        verb = "would write" if args.dry_run else "wrote"
        for w in written:
            print(f"{verb} {w}")
        n_op = sum(len(op_hooks(m)) for m in models)
        n_remote = sum(len(remote_hooks(m)) for m in models)
        print(f"\n{len(written)} files · {len(models)} models · "
              f"{n_op} operation hooks · {n_remote} remote hooks -> {args.out}",
              file=sys.stderr)
        if not args.dry_run:
            print("Next: move the stubs into your NestJS src/, wire subscribers "
                  "into the DataSource and interceptors onto controllers, then "
                  "port each `// TODO` body. See HOOKS_REPORT.md.", file=sys.stderr)
        return 0
    except HookError as exc:
        print(f"lb_hooks: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
