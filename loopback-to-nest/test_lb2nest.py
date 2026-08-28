#!/usr/bin/env python3
"""Tests for lb2nest.py — run: python3 loopback-to-nest/test_lb2nest.py

No third-party deps. Builds a tiny LoopBack 3 app in a temp dir, then checks
the scan report and the generated NestJS files. Exits non-zero on failure.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lb2nest as L  # noqa: E402

_failures = []


def check(cond, msg):
    if not cond:
        _failures.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  ok:   {msg}")


# --- name helpers ----------------------------------------------------------

def test_names():
    print("names")
    check(L.pascal("coffee-shop") == "CoffeeShop", "pascal kebab")
    check(L.pascal("coffeeShop") == "CoffeeShop", "pascal camel")
    check(L.camel("CoffeeShop") == "coffeeShop", "camel")
    check(L.kebab("CoffeeShop") == "coffee-shop", "kebab")
    check(L.snake("CoffeeShop") == "coffee_shop", "snake")
    check(L.pluralize("CoffeeShop") == "CoffeeShops", "plural +s")
    check(L.pluralize("category") == "categories", "plural y->ies")
    check(L.pluralize("box") == "boxes", "plural x->es")
    check(L.pluralize("person") == "people", "plural irregular")


# --- property parsing ------------------------------------------------------

def test_parse_property():
    print("parse_property")
    check(L.parse_property("a", "string")["lb_type"] == "string", "shorthand string")
    p = L.parse_property("tags", ["string"])
    check(p["array"] and p["lb_type"] == "string", "array shorthand")
    p = L.parse_property("name", {"type": "string", "required": True})
    check(p["required"] is True, "required flag")
    p = L.parse_property("scores", {"type": ["number"]})
    check(p["array"] and p["lb_type"] == "number", "array via type list")


# --- fixture app -----------------------------------------------------------

def build_fixture(root):
    models = os.path.join(root, "common", "models")
    server = os.path.join(root, "server")
    os.makedirs(models)
    os.makedirs(os.path.join(server, "boot"))

    coffee = {
        "name": "CoffeeShop",
        "base": "PersistedModel",
        "properties": {
            "name": {"type": "string", "required": True},
            "city": "string",
            "tags": {"type": ["string"]},
            "openedOn": {"type": "date"},
        },
        "relations": {
            "reviews": {"type": "hasMany", "model": "Review", "foreignKey": "shopId"},
        },
        "methods": {
            "nearby": {
                "http": {"verb": "get", "path": "/nearby"},
                "accepts": [{"arg": "lat", "type": "number"}],
                "returns": {"arg": "shops", "type": "array"},
            },
        },
        "acls": [{"principalType": "ROLE", "principalId": "$everyone", "permission": "DENY"}],
        "mixins": {"TimeStamp": {}},
    }
    review = {
        "name": "Review",
        "base": "PersistedModel",
        "properties": {"rating": {"type": "number", "required": True}, "body": "string"},
        "relations": {"shop": {"type": "belongsTo", "model": "CoffeeShop"}},
    }
    with open(os.path.join(models, "coffee-shop.json"), "w") as fh:
        json.dump(coffee, fh)
    with open(os.path.join(models, "review.json"), "w") as fh:
        json.dump(review, fh)
    # a model .js with a remote method + an operation hook
    with open(os.path.join(models, "coffee-shop.js"), "w") as fh:
        fh.write("module.exports = function(CoffeeShop) {\n"
                 "  CoffeeShop.remoteMethod('status', {http: {path: '/status'}});\n"
                 "  CoffeeShop.observe('before save', function(ctx, next) { next(); });\n"
                 "};\n")

    with open(os.path.join(server, "model-config.json"), "w") as fh:
        json.dump({
            "_meta": {"sources": ["../common/models"]},
            "CoffeeShop": {"dataSource": "db", "public": True},
            "Review": {"dataSource": "db", "public": False},
        }, fh)
    with open(os.path.join(server, "datasources.json"), "w") as fh:
        json.dump({
            "db": {"connector": "postgresql", "host": "db.local", "port": 5432,
                   "database": "coffee"},
        }, fh)
    with open(os.path.join(server, "boot", "seed.js"), "w") as fh:
        fh.write("module.exports = function(app){};\n")


def test_load_and_scan():
    print("load + scan")
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        app = L.load_app(root)
        check(len(app["models"]) == 2, "found 2 models")
        coffee = next(m for m in app["models"] if m["name"] == "CoffeeShop")
        # id auto-injected for PersistedModel
        check(any(p["id"] for p in coffee["properties"]), "id injected")
        # remote methods: JSON `nearby` + JS `status`
        names = {m["name"] for m in coffee["methods"]}
        check("nearby" in names and "status" in names, "remote methods from json+js")
        check(any(h["name"] == "before save" for h in coffee["hooks"]), "hook scanned")
        check(L._is_exposed(app, "Review") is False, "review not public")

        report = L.build_report(app)
        check("CoffeeShop" in report and "postgresql" in report, "report mentions model+ds")
        check("Operation/remote hooks" in report, "report flags hooks")
        check("ACL entries" in report, "report flags acls")


def test_generate_typeorm():
    print("generate (typeorm)")
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        app = L.load_app(root)
        files = L.build_files(app, "typeorm")

        ent = files["src/coffee-shop/coffee-shop.entity.ts"]
        check("@Entity({ name: 'coffee_shops' })" in ent, "entity table name pluralized+snake")
        check("@PrimaryGeneratedColumn()" in ent, "pk decorator")
        check("name: string;" in ent, "required prop not optional")
        check("city?: string;" in ent, "optional prop is optional")
        check("tags" in ent and "simple-json" in ent, "array/json column")
        check("reviews: hasMany" in ent, "relation as comment")

        create = files["src/coffee-shop/dto/create-coffee-shop.dto.ts"]
        check("@IsString()" in create and "@IsOptional()" in create, "dto validators")
        body = create.split("CreateCoffeeShopDto")[1]
        check("id?:" not in body and "id:" not in body, "id excluded from create dto")

        ctrl = files["src/coffee-shop/coffee-shop.controller.ts"]
        check("@Controller('coffee-shops')" in ctrl, "controller route plural")
        check("ParseIntPipe" in ctrl, "numeric id pipe")
        check("nearby" in ctrl and "@Get('nearby')" in ctrl, "custom method stub with verb")
        check("status" in ctrl, "js remote method stubbed")

        svc = files["src/coffee-shop/coffee-shop.service.ts"]
        check("@InjectRepository(CoffeeShop)" in svc, "service repo injection")

        mod = files["src/coffee-shop/coffee-shop.module.ts"]
        check("TypeOrmModule.forFeature([CoffeeShop])" in mod, "module forFeature")

        appmod = files["src/app.module.ts"]
        check("CoffeeShopModule" in appmod, "app module imports exposed model")
        check("ReviewModule" not in appmod, "non-public model omitted from app module")

        ds = files["src/data-source.ts"]
        check("type: 'postgres'" in ds and "coffee" in ds, "datasource from connector")

        pkg = json.loads(files["package.json"])
        check("@nestjs/typeorm" in pkg["dependencies"], "typeorm dep")
        check("pg" in pkg["dependencies"], "pg driver dep")
        check(pkg["dependencies"]["@nestjs/common"].startswith("^12"), "nest 12")


def test_generate_none():
    print("generate (orm=none)")
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        app = L.load_app(root)
        files = L.build_files(app, "none")
        check("src/data-source.ts" not in files, "no data-source without orm")
        svc = files["src/coffee-shop/coffee-shop.service.ts"]
        check("private readonly items" in svc, "in-memory service")
        ent = files["src/coffee-shop/coffee-shop.entity.ts"]
        check("@Entity" not in ent, "plain class, no typeorm decorators")
        pkg = json.loads(files["package.json"])
        check("typeorm" not in pkg["dependencies"], "no typeorm dep")


def test_single_file():
    print("single model file")
    with tempfile.TemporaryDirectory() as root:
        p = os.path.join(root, "widget.json")
        with open(p, "w") as fh:
            json.dump({"name": "Widget", "properties": {"label": "string"}}, fh)
        app = L.load_app(p)
        check(len(app["models"]) == 1 and app["models"][0]["name"] == "Widget",
              "single file loads one model")


def test_write_guard():
    print("overwrite guard + dry-run")
    with tempfile.TemporaryDirectory() as root:
        build_fixture(root)
        app = L.load_app(root)
        files = L.build_files(app, "typeorm")
        out = os.path.join(root, "out")
        os.makedirs(out)
        with open(os.path.join(out, "keep.txt"), "w") as fh:
            fh.write("x")
        # dry-run writes nothing
        w = L.write_files(files, out, force=False, dry_run=True)
        check(len(w) == len(files), "dry-run lists all files")
        check(not os.path.exists(os.path.join(out, "src")), "dry-run wrote nothing")
        # non-empty without force -> error
        try:
            L.write_files(files, out, force=False, dry_run=False)
            check(False, "expected MigrationError on non-empty dir")
        except L.MigrationError:
            check(True, "refuses non-empty dir without --force")
        # force -> writes
        L.write_files(files, out, force=True, dry_run=False)
        check(os.path.exists(os.path.join(out, "src", "app.module.ts")), "force writes")


def main():
    for t in (test_names, test_parse_property, test_load_and_scan,
              test_generate_typeorm, test_generate_none, test_single_file,
              test_write_guard):
        t()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S)")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
