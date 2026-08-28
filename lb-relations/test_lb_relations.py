#!/usr/bin/env python3
"""Tests for lb_relations.py — no third-party deps.

Builds a tiny LoopBack app in a temp dir (a belongsTo/hasMany pair plus a
hasAndBelongsToMany), runs the mapper, and asserts the generated TypeScript
carries the right decorators, imports, and inverse-side property names.

Run:  python3 lb-relations/test_lb_relations.py
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lb_relations as lr  # noqa: E402


def _write_app(root):
    """Write common/models/*.json for Customer, Order, Assembly, Part."""
    models_dir = os.path.join(root, "common", "models")
    os.makedirs(models_dir)
    specs = {
        "customer.json": {
            "name": "Customer",
            "properties": {"id": {"type": "number", "id": True}, "name": "string"},
            "relations": {
                "orders": {"type": "hasMany", "model": "Order",
                           "foreignKey": "customerId"},
            },
        },
        "order.json": {
            "name": "Order",
            "properties": {"id": {"type": "number", "id": True},
                           "total": "number"},
            "relations": {
                "customer": {"type": "belongsTo", "model": "Customer",
                             "foreignKey": "customerId"},
            },
        },
        # hasAndBelongsToMany pair (implicit junction, no through)
        "assembly.json": {
            "name": "Assembly",
            "properties": {"id": {"type": "number", "id": True}, "name": "string"},
            "relations": {
                "parts": {"type": "hasAndBelongsToMany", "model": "Part"},
            },
        },
        "part.json": {
            "name": "Part",
            "properties": {"id": {"type": "number", "id": True}, "name": "string"},
            "relations": {
                "assemblies": {"type": "hasAndBelongsToMany", "model": "Assembly"},
            },
        },
    }
    for fn, spec in specs.items():
        with open(os.path.join(models_dir, fn), "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
    return root


class NameHelpers(unittest.TestCase):
    def test_case_conversions(self):
        self.assertEqual(lr.pascal("coffee-shop"), "CoffeeShop")
        self.assertEqual(lr.camel("CoffeeShop"), "coffeeShop")
        self.assertEqual(lr.kebab("CoffeeShop"), "coffee-shop")
        self.assertEqual(lr.pluralize("Assembly"), "Assemblies")


class Mapping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _write_app(self.tmp)
        self.models, _ = lr.load_models(self.tmp)
        self.frags, self.manual, self.by_name = lr.build_fragments(self.models)

    def _frag(self, name):
        self.assertIn(name, self.frags, f"no fragment for {name}")
        return self.frags[name]

    def test_belongs_to_is_manytoone_with_joincolumn(self):
        order = self._frag("Order")
        self.assertIn("@ManyToOne(() => Customer", order)
        self.assertIn("@JoinColumn({ name: 'customerId' })", order)
        self.assertIn("customer: Customer;", order)
        # inverse-side property name comes from Customer.orders
        self.assertIn("(customer) => customer.orders", order)
        # imports both the decorators and the referenced entity
        self.assertIn("from 'typeorm'", order)
        self.assertIn("import { Customer } from '../customer/customer.entity';", order)
        self.assertIn("ManyToOne", order.split("from 'typeorm'")[0])
        self.assertIn("JoinColumn", order.split("from 'typeorm'")[0])

    def test_has_many_is_onetomany_inverse(self):
        customer = self._frag("Customer")
        self.assertIn("@OneToMany(() => Order", customer)
        self.assertIn("orders: Order[];", customer)
        # inverse-side property name comes from Order.customer
        self.assertIn("(order) => order.customer", customer)
        # OneToMany never carries @JoinColumn
        self.assertNotIn("@JoinColumn", customer)
        self.assertIn("import { Order } from '../order/order.entity';", customer)

    def test_habtm_is_manytomany_with_single_jointable(self):
        assembly = self._frag("Assembly")
        part = self._frag("Part")
        self.assertIn("@ManyToMany(() => Part", assembly)
        self.assertIn("@ManyToMany(() => Assembly", part)
        self.assertIn("parts: Part[];", assembly)
        self.assertIn("assemblies: Assembly[];", part)
        # @JoinTable appears on exactly one side (Assembly < Part lexically)
        self.assertIn("@JoinTable()", assembly)
        self.assertNotIn("@JoinTable", part)
        # inverse arrows on both sides
        self.assertIn("(part) => part.assemblies", assembly)
        self.assertIn("(assembly) => assembly.parts", part)

    def test_report_lists_no_false_manual_items(self):
        # This fixture maps cleanly, so the report should say so.
        report = lr.build_report(self.models, self.by_name, list(self.manual))
        self.assertIn("Models: 4", report)
        self.assertIn("mapped cleanly", report)


class SpecialCases(unittest.TestCase):
    def _one_model_app(self, spec):
        tmp = tempfile.mkdtemp()
        md = os.path.join(tmp, "common", "models")
        os.makedirs(md)
        with open(os.path.join(md, "m.json"), "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
        models, _ = lr.load_models(tmp)
        return lr.build_fragments(models)

    def test_references_many_is_array_column_and_flagged(self):
        frags, manual, _ = self._one_model_app({
            "name": "Post",
            "properties": {"id": {"type": "number", "id": True}},
            "relations": {"tags": {"type": "referencesMany", "model": "Tag",
                                   "foreignKey": "tagIds"}},
        })
        frag = frags["Post"]
        self.assertIn("@Column", frag)
        self.assertIn("tagIds:", frag)
        self.assertTrue(any(e["category"] == "referencesMany" for e in manual))

    def test_has_many_through_becomes_manytomany_jointable(self):
        # Physician hasMany Patient through Appointment
        tmp = tempfile.mkdtemp()
        md = os.path.join(tmp, "common", "models")
        os.makedirs(md)
        specs = {
            "physician.json": {
                "name": "Physician",
                "properties": {"id": {"type": "number", "id": True}},
                "relations": {"patients": {
                    "type": "hasMany", "model": "Patient",
                    "through": "Appointment",
                    "foreignKey": "physicianId", "keyThrough": "patientId"}},
            },
            "patient.json": {
                "name": "Patient",
                "properties": {"id": {"type": "number", "id": True}},
                "relations": {"physicians": {
                    "type": "hasMany", "model": "Physician",
                    "through": "Appointment",
                    "foreignKey": "patientId", "keyThrough": "physicianId"}},
            },
            "appointment.json": {
                "name": "Appointment",
                "properties": {"id": {"type": "number", "id": True}},
                "relations": {},
            },
        }
        for fn, spec in specs.items():
            with open(os.path.join(md, fn), "w", encoding="utf-8") as fh:
                json.dump(spec, fh)
        models, _ = lr.load_models(tmp)
        frags, manual, _ = lr.build_fragments(models)
        phys = frags["Patient"]  # Patient < Physician lexically -> owns JoinTable
        self.assertIn("@ManyToMany(() => Physician", phys)
        self.assertIn("@JoinTable({ name: 'appointments'", phys)
        self.assertIn("joinColumn: { name: 'patientId' }", phys)
        self.assertIn("inverseJoinColumn: { name: 'physicianId' }", phys)
        # the other side is inverse-only, no JoinTable
        self.assertNotIn("@JoinTable", frags["Physician"])
        self.assertTrue(any(e["category"] == "through" for e in manual))

    def test_has_one_is_onetoone_inverse_and_belongsto_owns_joincolumn(self):
        tmp = tempfile.mkdtemp()
        md = os.path.join(tmp, "common", "models")
        os.makedirs(md)
        specs = {
            "supplier.json": {
                "name": "Supplier",
                "properties": {"id": {"type": "number", "id": True}},
                "relations": {"account": {"type": "hasOne", "model": "Account",
                                          "foreignKey": "supplierId"}},
            },
            "account.json": {
                "name": "Account",
                "properties": {"id": {"type": "number", "id": True}},
                "relations": {"supplier": {"type": "belongsTo", "model": "Supplier",
                                           "foreignKey": "supplierId"}},
            },
        }
        for fn, spec in specs.items():
            with open(os.path.join(md, fn), "w", encoding="utf-8") as fh:
                json.dump(spec, fh)
        models, _ = lr.load_models(tmp)
        frags, _, _ = lr.build_fragments(models)
        supplier = frags["Supplier"]
        account = frags["Account"]
        # hasOne side -> OneToOne inverse, no JoinColumn
        self.assertIn("@OneToOne(() => Account", supplier)
        self.assertNotIn("@JoinColumn", supplier)
        # belongsTo paired with hasOne -> OneToOne owning + JoinColumn
        self.assertIn("@OneToOne(() => Supplier", account)
        self.assertIn("@JoinColumn({ name: 'supplierId' })", account)

    def test_embeds_one_and_many_are_flagged(self):
        frags, manual, _ = self._one_model_app({
            "name": "Customer",
            "properties": {"id": {"type": "number", "id": True}},
            "relations": {
                "billingAddress": {"type": "embedsOne", "model": "Address"},
                "emails": {"type": "embedsMany", "model": "Email"},
            },
        })
        frag = frags["Customer"]
        self.assertIn("@Column(() => Address)", frag)
        self.assertIn("@Column({ type: 'simple-json' })", frag)
        cats = {e["category"] for e in manual}
        self.assertIn("embedsOne", cats)
        self.assertIn("embedsMany", cats)


class WriteMode(unittest.TestCase):
    def test_write_files_and_overwrite_guard(self):
        src = tempfile.mkdtemp()
        _write_app(src)
        models, _ = lr.load_models(src)
        out = os.path.join(tempfile.mkdtemp(), "rel-out")

        written = lr.write_files(models, out, force=False, dry_run=False)
        self.assertTrue(any(w.endswith("order.relations.ts") for w in written))
        self.assertTrue(any(w.endswith("RELATIONS.md") for w in written))
        self.assertTrue(os.path.isfile(os.path.join(out, "order.relations.ts")))

        # refuses to overwrite a non-empty dir without --force
        with self.assertRaises(lr.RelationError):
            lr.write_files(models, out, force=False, dry_run=False)
        # --force is allowed
        lr.write_files(models, out, force=True, dry_run=False)

    def test_dry_run_writes_nothing(self):
        src = tempfile.mkdtemp()
        _write_app(src)
        models, _ = lr.load_models(src)
        out = os.path.join(tempfile.mkdtemp(), "rel-out")
        written = lr.write_files(models, out, force=False, dry_run=True)
        self.assertTrue(written)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
