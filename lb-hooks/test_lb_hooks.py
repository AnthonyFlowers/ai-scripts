#!/usr/bin/env python3
"""Tests for lb_hooks.py — run: python3 lb-hooks/test_lb_hooks.py

No third-party deps. Builds a tiny LoopBack model `.js` in a temp dir, scans it,
and checks the inventory report and generated NestJS stubs. Exits non-zero on
failure.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lb_hooks as L  # noqa: E402

_failures = []


def check(cond, msg):
    if not cond:
        _failures.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  ok:   {msg}")


# A fixture model .js exercising: before save (observe), loaded (observe),
# access (note-only observe), a beforeRemote and an afterRemote, and an
# afterRemoteError (note-only).
FIXTURE_JS = """\
module.exports = function(CoffeeShop) {
  CoffeeShop.observe('before save', function(ctx, next) {
    if (ctx.instance) {
      ctx.instance.slug = ctx.instance.name.toLowerCase();
    }
    next();
  });

  CoffeeShop.observe('loaded', function(ctx, next) {
    ctx.data.loadedAt = new Date();
    next();
  });

  CoffeeShop.observe('access', function(ctx, next) {
    ctx.query.where = ctx.query.where || {};
    next();
  });

  CoffeeShop.beforeRemote('status', function(ctx, unused, next) {
    ctx.args.options = ctx.args.options || {};
    next();
  });

  CoffeeShop.afterRemote('prototype.__get__reviews', function(ctx, result, next) {
    ctx.result = { reviews: result };
    next();
  });

  CoffeeShop.afterRemoteError('status', function(ctx, next) {
    console.error(ctx.error);
    next();
  });
};
"""


def build_fixture(root):
    models = os.path.join(root, "common", "models")
    os.makedirs(models)
    path = os.path.join(models, "coffee-shop.js")
    with open(path, "w") as fh:
        fh.write(FIXTURE_JS)
    # a non-model .js with no hooks — must be ignored
    with open(os.path.join(models, "boot-thing.js"), "w") as fh:
        fh.write("module.exports = function(app) { /* nothing */ };\n")
    return path


# --- name helpers ----------------------------------------------------------

def test_names():
    print("names")
    check(L.pascal("coffee-shop") == "CoffeeShop", "pascal kebab")
    check(L.camel("CoffeeShop") == "coffeeShop", "camel")
    check(L.kebab("CoffeeShop") == "coffee-shop", "kebab")
    check(L._method_ident("prototype.__get__reviews").strip() != "", "method ident non-empty")
    check(L._method_ident("*").strip() == "all", "wildcard -> all")


# --- scanning --------------------------------------------------------------

def test_scan():
    print("scan")
    hooks = L.scan_hooks(FIXTURE_JS)
    kinds = [(h["kind"], h["name"]) for h in hooks]
    check(("observe", "before save") in kinds, "before save scanned")
    check(("observe", "loaded") in kinds, "loaded scanned")
    check(("observe", "access") in kinds, "access scanned")
    check(("beforeRemote", "status") in kinds, "beforeRemote scanned")
    check(("afterRemote", "prototype.__get__reviews") in kinds, "afterRemote scanned")
    check(("afterRemoteError", "status") in kinds, "afterRemoteError scanned")
    # the embedded source keeps the original body text
    bs = next(h for h in hooks if h["name"] == "before save")
    check("toLowerCase()" in bs["source"], "before save source captured")
    check(bs["source"].rstrip().endswith(");"), "captured full call incl close paren")


def test_scan_path_and_model_name():
    print("scan_path")
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        models = L.scan_path(root)
        check(len(models) == 1, "one model found (hookless file ignored)")
        check(models[0]["name"] == "CoffeeShop", "model name from export fn param")
        check(len(models[0]["hooks"]) == 6, "all six hooks recorded")


# --- subscriber generation -------------------------------------------------

def test_subscriber():
    print("subscriber")
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        model = L.scan_path(root)[0]
        sub = L.gen_subscriber(model)
        check(sub is not None, "subscriber generated")
        # 'before save' -> BOTH beforeInsert and beforeUpdate
        check("beforeInsert(event: InsertEvent<CoffeeShop>)" in sub, "beforeInsert present")
        check("beforeUpdate(event: UpdateEvent<CoffeeShop>)" in sub, "beforeUpdate present")
        # 'loaded' -> afterLoad
        check("afterLoad(entity: CoffeeShop" in sub, "afterLoad present")
        # embedded original source appears in the mapped methods
        check(sub.count("toLowerCase()") == 2,
              "before-save source embedded in BOTH insert+update methods")
        check("// TODO: port the LoopBack hook body shown above." in sub, "TODO marker")
        check("implements EntitySubscriberInterface<CoffeeShop>" in sub, "implements interface")
        check("@EventSubscriber()" in sub, "decorator present")
        # imports include the event types actually used
        check("InsertEvent" in sub and "UpdateEvent" in sub and "LoadEvent" in sub,
              "event-type imports")
        # access hook is note-only: not a subscriber method
        check("access" not in sub.replace("import", ""), "access not in subscriber")


# --- interceptor generation ------------------------------------------------

def test_interceptors():
    print("interceptors")
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        model = L.scan_path(root)[0]
        remotes = L.remote_hooks(model)
        check(len(remotes) == 2, "two remote hooks")
        before = next(h for h in remotes if h["kind"] == "beforeRemote")
        fname, content = L.gen_interceptor(model, before)
        check(fname.endswith(".interceptor.ts"), "interceptor file name")
        check("implements NestInterceptor" in content, "implements NestInterceptor")
        check("intercept(context: ExecutionContext, next: CallHandler)" in content,
              "intercept signature")
        check("return next.handle();" in content, "before hook returns next.handle()")
        check("ctx.args.options" in content, "beforeRemote source embedded")
        check("@UseInterceptors(" in content, "binding hint in header")

        after = next(h for h in remotes if h["kind"] == "afterRemote")
        _, acontent = L.gen_interceptor(model, after)
        check(".pipe(" in acontent and "map((data)" in acontent, "afterRemote uses pipe/map")
        check("ctx.result = { reviews: result }" in acontent, "afterRemote source embedded")


# --- report ----------------------------------------------------------------

def test_report():
    print("report")
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        models = L.scan_path(root)
        report = L.build_report(models, source_label=root)
        check("CoffeeShop" in report, "report names the model")
        check("beforeInsert + beforeUpdate" in report, "report shows before-save mapping")
        check("Needs manual attention" in report, "report has manual-attention section")
        check("`access`" in report and "guard" in report,
              "report flags access as a note (guard/query filter)")
        check("afterRemoteError" in report and "ExceptionFilter" in report,
              "report flags afterRemoteError -> ExceptionFilter")


# --- write path ------------------------------------------------------------

def test_build_and_write():
    print("build_files + write guard")
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        models = L.scan_path(root)
        files = L.build_files(models)
        check("coffee-shop/coffee-shop.subscriber.ts" in files, "subscriber path")
        check(any(k.startswith("coffee-shop/interceptors/") for k in files),
              "interceptor path")
        check("HOOKS_REPORT.md" in files, "report file")

        out = os.path.join(root, "out")
        os.makedirs(out)
        with open(os.path.join(out, "keep.txt"), "w") as fh:
            fh.write("x")
        # dry-run writes nothing
        w = L.write_files(files, out, force=False, dry_run=True)
        check(len(w) == len(files), "dry-run lists all files")
        check(not os.path.exists(os.path.join(out, "coffee-shop")),
              "dry-run wrote nothing")
        # non-empty without force -> error
        try:
            L.write_files(files, out, force=False, dry_run=False)
            check(False, "expected HookError on non-empty dir")
        except L.HookError:
            check(True, "refuses non-empty dir without --force")
        # force -> writes
        L.write_files(files, out, force=True, dry_run=False)
        check(os.path.exists(os.path.join(out, "coffee-shop",
                                          "coffee-shop.subscriber.ts")),
              "force writes subscriber")


def main():
    for t in (test_names, test_scan, test_scan_path_and_model_name,
              test_subscriber, test_interceptors, test_report,
              test_build_and_write):
        t()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S)")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
