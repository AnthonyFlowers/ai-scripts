#!/usr/bin/env python3
"""lb_relations.py — turn LoopBack 3 model relations into TypeORM decorators.

`lb2nest.py` scaffolds a whole NestJS 12 app from a LoopBack 3 codebase but
deliberately punts on relations: it emits them as `// wire this up by hand`
comments on each entity, because deciding the owning side, the foreign key,
and the inverse-side property is the fiddly part. This script fills that gap.

It reads every model in a LoopBack app (so it can see *both* ends of each
relation), pairs them up, and emits, per model, a TypeScript fragment of
relation properties carrying the right TypeORM decorators, imports, and the
inverse-side arrow functions `() => OtherEntity`.

Deterministic mapping (LoopBack 3 -> TypeORM):

  belongsTo               -> @ManyToOne + @JoinColumn   (owning side, holds FK)
  belongsTo (vs hasOne)   -> @OneToOne  + @JoinColumn   (owning side, holds FK)
  hasMany                 -> @OneToMany                 (inverse side)
  hasMany + through       -> @ManyToMany + @JoinTable   (junction table)
  hasOne                  -> @OneToOne                  (inverse; FK side gets @JoinColumn)
  hasAndBelongsToMany     -> @ManyToMany + @JoinTable
  referencesMany          -> array-of-ids column        (no native equivalent; note)
  embedsOne               -> @Column(() => Type)         (embedded entity; note)
  embedsMany              -> JSON column                (array; note)

For a many-to-many, `@JoinTable` is placed on exactly one (owning) side —
deterministically the lexicographically-smaller entity name — and the other
side is emitted inverse-only, matching TypeORM's rule that `@JoinTable` must
appear on a single side. Relations with no clean mapping (referencesMany,
embeds, polymorphic, self-referential, or a missing counterpart) are also
collected into a short "needs manual attention" report.

Python 3.9+, no dependencies. Emits TypeScript; nothing here runs Node.

Usage:
  ./lb_relations.py <app-dir>                    # print snippets + report (default)
  ./lb_relations.py <app-dir> --report-only      # just the manual-attention report
  ./lb_relations.py <app-dir> -o report.txt      # write the printed output to a file
  ./lb_relations.py <app-dir> --out ./rel-out    # write per-model .relations.ts files
  ./lb_relations.py <app-dir> --out ./rel-out --dry-run
  ./lb_relations.py models/order.json            # a single model file (limited pairing)

<app-dir> may be a LoopBack app root (models found under common/models,
server/models, models), a models directory, or a single model JSON file. A
model's relations live in its `relations` key, each `{type, model,
foreignKey, through, keyThrough, primaryKey, polymorphic}`.

Options:
  -o, --out DEST   with a directory-ish DEST used via --out: write per-model
                   `<model>.relations.ts` files plus RELATIONS.md into it.
                   Without --out, print to stdout (or to the -o FILE given).
  --out DIR        write per-model `<model>.relations.ts` files into DIR
  --report-only    print only the manual-attention report
  --force          overwrite an existing non-empty output directory
  --dry-run        list the files that would be written, write nothing

Exit codes: 0 ok, 1 bad args / nothing found / refusing to overwrite.
"""

import argparse
import json
import os
import sys
import re

# ---------------------------------------------------------------------------
# Relation model
# ---------------------------------------------------------------------------

# The LoopBack relation types this script understands.
LB_RELATION_TYPES = {
    "belongsTo", "hasOne", "hasMany", "hasManyThrough",
    "hasAndBelongsToMany", "referencesMany", "embedsOne", "embedsMany",
}


class RelationError(Exception):
    """Raised for bad input, nothing found, or a refused overwrite."""


# ---------------------------------------------------------------------------
# Name helpers  (kept local so the script is standalone; mirrors lb2nest.py)
# ---------------------------------------------------------------------------


def _words(name):
    """Split an identifier into lowercase words (handles camel, snake, kebab)."""
    s = re.sub(r"[_\-\s]+", " ", name or "")
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
    """Small English pluralizer — good enough for table names, best-effort."""
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
# Parsing a LoopBack app  (relation-focused subset of lb2nest.py's parser)
# ---------------------------------------------------------------------------


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise RelationError(f"cannot read JSON {path}: {exc}")


def parse_property(name, spec):
    """Normalize a LoopBack property spec to the bits we need (id + type)."""
    prop = {"name": name, "lb_type": "any", "id": False}
    if isinstance(spec, str):
        prop["lb_type"] = spec
    elif isinstance(spec, list):
        prop["lb_type"] = spec[0] if spec and isinstance(spec[0], str) else "any"
    elif isinstance(spec, dict):
        t = spec.get("type", "any")
        if isinstance(t, list):
            t = t[0] if t and isinstance(t[0], str) else "any"
        prop["lb_type"] = t if isinstance(t, str) else "any"
        prop["id"] = bool(spec.get("id", False))
    return prop


def parse_relation(name, spec):
    """Normalize one entry of a model's `relations` block."""
    spec = spec or {}
    rtype = spec.get("type", "hasMany")
    # LoopBack writes hasMany-through as type "hasMany" with a `through` key;
    # normalize it to a distinct pseudo-type so the mapper can branch cleanly.
    if rtype == "hasMany" and spec.get("through"):
        rtype = "hasManyThrough"
    return {
        "name": name,
        "type": rtype,
        "model": spec.get("model", ""),
        "foreignKey": spec.get("foreignKey", ""),
        "through": spec.get("through"),
        "keyThrough": spec.get("keyThrough", ""),
        "primaryKey": spec.get("primaryKey", ""),
        "polymorphic": spec.get("polymorphic"),
        "property": spec.get("property", ""),  # embedsOne/embedsMany target field
    }


def parse_model_json(path):
    raw = _read_json(path)
    name = raw.get("name") or pascal(os.path.splitext(os.path.basename(path))[0])
    props = [parse_property(n, s) for n, s in (raw.get("properties") or {}).items()]
    relations = [parse_relation(n, s)
                 for n, s in (raw.get("relations") or {}).items()]
    return {"name": name, "properties": props, "relations": relations,
            "source": path}


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


def load_models(path):
    """Return (models, app_dir). Accepts an app root, models dir, or .json."""
    if os.path.isfile(path) and path.endswith(".json"):
        return [parse_model_json(path)], os.path.dirname(path) or "."
    if not os.path.isdir(path):
        raise RelationError(f"not a directory or .json file: {path}")

    model_files = []
    for d in _find_models_dirs(path):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json") and fn != "model-config.json":
                model_files.append(os.path.join(d, fn))
    models = [parse_model_json(mf) for mf in model_files]
    if not models:
        raise RelationError(
            f"no LoopBack model .json files found under {path} "
            "(looked in common/models, server/models, models)")
    return models, path


# ---------------------------------------------------------------------------
# Foreign-key / counterpart resolution
# ---------------------------------------------------------------------------


def _fk_for(rel, owner_model):
    """The resolved foreign-key column name for a relation, applying defaults.

    LoopBack default FK = camelCase(relationName) + 'Id' for belongsTo, and
    camelCase(sourceModelName) + 'Id' for hasOne/hasMany (the FK lives on the
    *target* and points back at the source)."""
    if rel["foreignKey"]:
        return rel["foreignKey"]
    if rel["type"] == "belongsTo":
        return camel(rel["name"]) + "Id"
    if rel["type"] in ("hasOne", "hasMany"):
        return camel(owner_model["name"]) + "Id"
    if rel["type"] == "referencesMany":
        return camel(rel["name"]) + "Ids"
    return camel(rel["name"]) + "Id"


def _target_id_ts(target_model):
    """Best-effort TS type of a model's id (LoopBack ids default to number)."""
    if not target_model:
        return "number"
    for p in target_model["properties"]:
        if p["id"]:
            return "number" if p["lb_type"].lower() == "number" else "string"
    return "number"


def find_counterpart(rel, owner_model, models_by_name):
    """The relation on the target model that points back at owner_model.

    Prefers a counterpart whose resolved FK matches, so multiple relations
    between the same pair of models line up correctly."""
    target = models_by_name.get(pascal(rel["model"]))
    if not target:
        return None, None
    back = [rr for rr in target["relations"]
            if pascal(rr["model"]) == pascal(owner_model["name"])]
    if not back:
        return target, None
    if len(back) > 1 and rel["foreignKey"]:
        my_fk = _fk_for(rel, owner_model)
        for rr in back:
            if _fk_for(rr, target) == my_fk:
                return target, rr
    if len(back) > 1:
        # disambiguate belongsTo<->hasMany/hasOne by expected pairing
        want = {"belongsTo": ("hasMany", "hasOne"),
                "hasMany": ("belongsTo",), "hasOne": ("belongsTo",),
                "hasManyThrough": ("hasManyThrough", "hasAndBelongsToMany"),
                "hasAndBelongsToMany": ("hasAndBelongsToMany", "hasManyThrough")}
        for rr in back:
            if rr["type"] in want.get(rel["type"], ()):
                return target, rr
    return target, back[0]


# ---------------------------------------------------------------------------
# Mapping one relation -> a TypeORM property block
# ---------------------------------------------------------------------------


def _inverse_arrow(target_pascal, counterpart):
    """`, (x) => x.prop` when the inverse property is known, else ``."""
    if not counterpart:
        return ""
    param = camel(target_pascal)
    return f", ({param}) => {param}.{camel(counterpart['name'])}"


def plan_relation(rel, model, models_by_name):
    """Map one LoopBack relation to a TypeORM property block + metadata.

    Returns a dict: decorators [str], prop_line str, typeorm {import names},
    entities {pascal names to import}, note (str|None), manual (bool),
    category (str for the report)."""
    self_pascal = pascal(model["name"])
    target = pascal(rel["model"]) if rel["model"] else "Unknown"
    target_model, counterpart = find_counterpart(rel, model, models_by_name)
    self_ref = target == self_pascal and rel["model"] != ""
    result = {"decorators": [], "prop_line": "", "typeorm": set(),
              "entities": set(), "note": None, "manual": False,
              "category": None, "name": rel["name"], "type": rel["type"]}

    def use_entity():
        if target != "Unknown":
            result["entities"].add(target)

    # ---- polymorphic: no concrete target -> hand off to a human -----------
    if rel.get("polymorphic"):
        result["manual"] = True
        result["category"] = "polymorphic"
        result["note"] = (
            f"polymorphic {rel['type']} — the target type is chosen at runtime; "
            "TypeORM has no polymorphic relation. Model it as separate nullable "
            "@ManyToOne columns per concrete type, or a (type, id) pair.")
        result["decorators"] = [f"// TODO polymorphic {rel['type']} -> {target or '?'}"]
        result["prop_line"] = f"// {camel(rel['name'])}: unknown; // polymorphic"
        return result

    rtype = rel["type"]

    # ---- belongsTo : owning side, holds the FK ---------------------------
    if rtype == "belongsTo":
        fk = _fk_for(rel, model)
        use_entity()
        if counterpart and counterpart["type"] == "hasOne":
            # one-to-one: this side owns the FK
            arrow = _inverse_arrow(target, counterpart)
            result["decorators"] = [
                f"@OneToOne(() => {target}{arrow})",
                f"@JoinColumn({{ name: '{fk}' }})"]
            result["typeorm"] |= {"OneToOne", "JoinColumn"}
        else:
            arrow = _inverse_arrow(target, counterpart)
            result["decorators"] = [
                f"@ManyToOne(() => {target}{arrow})",
                f"@JoinColumn({{ name: '{fk}' }})"]
            result["typeorm"] |= {"ManyToOne", "JoinColumn"}
        result["prop_line"] = f"{camel(rel['name'])}: {target};"
        if not counterpart:
            result["note"] = (
                f"no inverse relation found on {target}; the arrow function is "
                "one-directional. Add a matching @OneToMany/@OneToOne there if "
                "you need the reverse.")
        return result

    # ---- hasMany (no through) : inverse side, @OneToMany ------------------
    if rtype == "hasMany":
        use_entity()
        arrow = _inverse_arrow(target, counterpart)
        result["decorators"] = [f"@OneToMany(() => {target}{arrow})"]
        result["typeorm"].add("OneToMany")
        result["prop_line"] = f"{camel(rel['name'])}: {target}[];"
        if not counterpart:
            result["manual"] = True
            result["category"] = "missing-inverse"
            result["note"] = (
                f"@OneToMany REQUIRES an inverse @ManyToOne on {target}, but no "
                f"belongsTo back to {self_pascal} was found. Add the owning side "
                "or this will not compile.")
        return result

    # ---- hasMany-through / hasAndBelongsToMany : @ManyToMany -------------
    if rtype in ("hasManyThrough", "hasAndBelongsToMany"):
        use_entity()
        arrow = _inverse_arrow(target, counterpart)
        result["decorators"] = [f"@ManyToMany(() => {target}{arrow})"]
        result["typeorm"].add("ManyToMany")
        # @JoinTable belongs on exactly one side: pick it deterministically.
        owns = self_ref or (self_pascal <= target)
        if owns:
            result["typeorm"].add("JoinTable")
            if rtype == "hasManyThrough" and rel["through"]:
                jt_name = snake(pluralize(rel["through"]))
                fk = _fk_for(rel, model)
                other = rel["keyThrough"] or (camel(rel["model"]) + "Id")
                result["decorators"].append(
                    f"@JoinTable({{ name: '{jt_name}', "
                    f"joinColumn: {{ name: '{fk}' }}, "
                    f"inverseJoinColumn: {{ name: '{other}' }} }})")
                result["note"] = (
                    f"through model '{rel['through']}' became a junction table via "
                    "@JoinTable. If it carries its own columns/behaviour, keep it "
                    "as a real entity with two @ManyToOne sides instead.")
                result["manual"] = True
                result["category"] = "through"
            else:
                result["decorators"].append("@JoinTable()")
        else:
            result["note"] = (
                f"inverse side of the many-to-many; the JoinTable decorator is "
                f"emitted on {target} (owning side). Do not add JoinTable here.")
        result["prop_line"] = f"{camel(rel['name'])}: {target}[];"
        return result

    # ---- hasOne : inverse side of a one-to-one --------------------------
    if rtype == "hasOne":
        use_entity()
        arrow = _inverse_arrow(target, counterpart)
        result["decorators"] = [f"@OneToOne(() => {target}{arrow})"]
        result["typeorm"].add("OneToOne")
        result["prop_line"] = f"{camel(rel['name'])}: {target};"
        if not counterpart:
            fk = _fk_for(rel, model)
            result["note"] = (
                f"the FK '{fk}' lives on {target}; add @OneToOne(() => "
                f"{self_pascal}) + @JoinColumn({{ name: '{fk}' }}) on {target}. "
                "No reverse belongsTo was declared in LoopBack.")
        return result

    # ---- referencesMany : array of ids, no native relation --------------
    if rtype == "referencesMany":
        fk = _fk_for(rel, model)
        id_ts = _target_id_ts(target_model)
        result["decorators"] = ["@Column({ type: 'simple-json' })"]
        result["typeorm"].add("Column")
        result["prop_line"] = f"{fk}: {id_ts}[];"
        result["manual"] = True
        result["category"] = "referencesMany"
        result["note"] = (
            f"referencesMany -> {target}: stored as an array of ids ('{fk}'). "
            "TypeORM has no equivalent; switch to @ManyToMany + @JoinTable if a "
            "junction table is acceptable.")
        return result

    # ---- embedsOne : embedded entity column -----------------------------
    if rtype == "embedsOne":
        use_entity()
        prop = camel(rel["property"] or rel["name"])
        result["decorators"] = [f"@Column(() => {target})"]
        result["typeorm"].add("Column")
        result["prop_line"] = f"{prop}: {target};"
        result["manual"] = True
        result["category"] = "embedsOne"
        result["note"] = (
            f"embedsOne -> {target}: mapped to a TypeORM embedded entity "
            f"(@Column(() => {target})), which flattens {target}'s columns into "
            "this table with a prefix. Use a 'json' column instead to keep it "
            "as a nested document.")
        return result

    # ---- embedsMany : JSON array column ---------------------------------
    if rtype == "embedsMany":
        prop = camel(rel["property"] or rel["name"])
        result["decorators"] = ["@Column({ type: 'simple-json' })"]
        result["typeorm"].add("Column")
        result["entities"].add(target)  # for the type annotation only
        result["prop_line"] = f"{prop}: {target}[];"
        result["manual"] = True
        result["category"] = "embedsMany"
        result["note"] = (
            f"embedsMany -> {target}: TypeORM embedded entities can't be arrays, "
            "so this is a JSON column holding the array. Import the type for the "
            "annotation, or use @ManyToMany if they should be their own rows.")
        return result

    # ---- anything else --------------------------------------------------
    result["manual"] = True
    result["category"] = "unknown"
    result["note"] = f"unrecognized relation type '{rtype}' -> {target}; map by hand."
    result["decorators"] = [f"// TODO {rtype} -> {target}"]
    result["prop_line"] = f"// {camel(rel['name'])}: {target};"
    return result


# ---------------------------------------------------------------------------
# Assembling a per-model TypeScript fragment
# ---------------------------------------------------------------------------


def entity_import_path(target_pascal):
    """Path lb2nest.py would place the target entity at, relative to a sibling."""
    kn = kebab(target_pascal)
    return f"../{kn}/{kn}.entity"


def gen_model_fragment(model, models_by_name):
    """Return (fragment_str, [manual_entries]) for one model, or ('', []) if no relations."""
    name = pascal(model["name"])
    if not model["relations"]:
        return "", []

    plans = [plan_relation(r, model, models_by_name) for r in model["relations"]]
    typeorm_imports = set()
    entities = {}   # pascal -> import path (skip self)
    blocks = []
    manual = []
    for p in plans:
        typeorm_imports |= p["typeorm"]
        for ent in p["entities"]:
            if ent != name and ent != "Unknown":
                entities[ent] = entity_import_path(ent)
        if p["manual"]:
            manual.append({"model": name, **p})
        block = []
        if p["note"]:
            for i, line in enumerate(_wrap(p["note"], 74)):
                block.append(("  // NOTE: " if i == 0 else "  //       ") + line)
        for deco in p["decorators"]:
            block.append("  " + deco)
        block.append("  " + p["prop_line"])
        blocks.append("\n".join(block))

    lines = [
        f"// Relations for {name} — generated from LoopBack 3 by lb_relations.py.",
        f"// Merge the property block into {kebab(model['name'])}.entity.ts and",
        "// fold these imports into the entity's existing import statements.",
    ]
    if typeorm_imports:
        lines.append("import { " + ", ".join(sorted(typeorm_imports))
                     + " } from 'typeorm';")
    for ent in sorted(entities):
        lines.append(f"import {{ {ent} }} from '{entities[ent]}';")
    lines.append("")
    lines.append(f"export class {name}Relations {{")
    lines.append("\n\n".join(blocks))
    lines.append("}")
    return "\n".join(lines) + "\n", manual


def _wrap(text, width):
    """Tiny word-wrap so long notes stay readable (no textwrap import needed)."""
    words, line, out = text.split(), "", []
    for w in words:
        if line and len(line) + 1 + len(w) > width:
            out.append(line)
            line = w
        else:
            line = w if not line else line + " " + w
    if line:
        out.append(line)
    return out or [""]


# ---------------------------------------------------------------------------
# Manual-attention report
# ---------------------------------------------------------------------------


CATEGORY_TITLES = {
    "referencesMany": "referencesMany (array of ids — no native TypeORM relation)",
    "embedsOne": "embedsOne (embedded entity / JSON column)",
    "embedsMany": "embedsMany (JSON array column)",
    "through": "hasMany-through (junction table — check for extra columns)",
    "polymorphic": "polymorphic relations (no TypeORM equivalent)",
    "missing-inverse": "@OneToMany with no matching inverse @ManyToOne",
    "self-referential": "self-referential relations",
    "unknown": "unrecognized relation types",
}


def build_report(models, models_by_name, manual_all):
    lines = ["LoopBack relations -> TypeORM: manual-attention report", ""]
    total = sum(len(m["relations"]) for m in models)
    lines.append(f"Models: {len(models)} · Relations: {total} · "
                 f"Need review: {len(manual_all)}")
    lines.append("")

    # self-referential detection across all models
    for m in models:
        for r in m["relations"]:
            if r["model"] and pascal(r["model"]) == pascal(m["name"]):
                manual_all.append({"model": pascal(m["name"]), "name": r["name"],
                                   "type": r["type"], "category": "self-referential",
                                   "note": "self-referential relation; the arrow "
                                   "function points at the same entity — verify the "
                                   "inverse property and any nullable FK."})

    if not manual_all:
        lines.append("Nothing needs manual attention — every relation mapped cleanly.")
        return "\n".join(lines) + "\n"

    by_cat = {}
    for entry in manual_all:
        by_cat.setdefault(entry.get("category") or "unknown", []).append(entry)

    for cat in ("polymorphic", "through", "referencesMany", "embedsOne",
                "embedsMany", "missing-inverse", "self-referential", "unknown"):
        entries = by_cat.get(cat)
        if not entries:
            continue
        lines.append(f"## {CATEGORY_TITLES.get(cat, cat)}")
        for e in entries:
            lines.append(f"  - {e['model']}.{e['name']} ({e['type']})")
            if e.get("note"):
                for line in _wrap(e["note"], 72):
                    lines.append(f"      {line}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------


def build_fragments(models):
    """Return (fragments {model_name: text}, manual_all [entries])."""
    models_by_name = {pascal(m["name"]): m for m in models}
    fragments = {}
    manual_all = []
    for m in models:
        frag, manual = gen_model_fragment(m, models_by_name)
        if frag:
            fragments[pascal(m["name"])] = frag
        manual_all.extend(manual)
    return fragments, manual_all, models_by_name


def print_output(models, report_only):
    fragments, manual_all, models_by_name = build_fragments(models)
    out = []
    if not report_only:
        if not fragments:
            out.append("// No relations found in any model.\n")
        for mname in sorted(fragments):
            out.append(fragments[mname])
            out.append("")
    out.append(build_report(models, models_by_name, manual_all))
    return "\n".join(out).rstrip() + "\n"


def write_files(models, out_dir, force, dry_run):
    fragments, manual_all, models_by_name = build_fragments(models)
    files = {}
    for mname, frag in fragments.items():
        files[f"{kebab(mname)}.relations.ts"] = frag
    files["RELATIONS.md"] = build_report(models, models_by_name, manual_all)

    if os.path.isdir(out_dir) and os.listdir(out_dir) and not force and not dry_run:
        raise RelationError(
            f"output directory {out_dir} is not empty; pass --force to overwrite")
    written = []
    for rel, content in sorted(files.items()):
        dest = os.path.join(out_dir, rel)
        written.append(dest)
        if dry_run:
            continue
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lb_relations.py",
        description="Convert LoopBack 3 model relations into TypeORM decorators.")
    parser.add_argument("app", help="LoopBack app dir, models dir, or a model .json")
    parser.add_argument("-o", "--out", dest="out",
                        help="a directory: write per-model <model>.relations.ts + "
                             "RELATIONS.md into it; a file (with --to-file): write "
                             "the printed output there")
    parser.add_argument("--to-file", action="store_true",
                        help="treat --out as a file to write the printed snippets to")
    parser.add_argument("--report-only", action="store_true",
                        help="print only the manual-attention report")
    parser.add_argument("--force", action="store_true",
                        help="overwrite a non-empty output directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="list files that would be written, write nothing")
    args = parser.parse_args(argv)

    try:
        models, _ = load_models(args.app)

        # Directory-output mode: --out given without --to-file.
        if args.out and not args.to_file:
            written = write_files(models, args.out, args.force, args.dry_run)
            verb = "would write" if args.dry_run else "wrote"
            for w in written:
                print(f"{verb} {w}")
            print(f"\n{len(written)} files -> {args.out}", file=sys.stderr)
            return 0

        # Print mode (default), optionally to a file via --to-file.
        text = print_output(models, args.report_only)
        if args.out and args.to_file:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            sys.stdout.write(text)
        return 0
    except RelationError as exc:
        print(f"lb_relations: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
