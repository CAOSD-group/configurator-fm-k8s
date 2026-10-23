"""Pruebas del piloto sin instalar FlamaPy ni usar un clúster Kubernetes.

Comprueban selección desde metadatos y orquestación frente a un generador simulado.
No validan la generación real con un UVL grande.
"""
import argparse
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiment_runner import encode_property, entries_for_root, generate, make_cases


class FakeMapper:
    gvk_to_root = {
        ("apps/v1", "Deployment"): "io_apps_v1_Deployment",
        ("v1", "Service"): "io_core_v1_Service",
    }
    serialization_entries = []


def entry(root, path, role="property", schema="string", **extra):
    return {
        "feature": root + ("_" + path.replace(".", "_") if path else ""),
        "configPath": path, "serializationKind": "scalar",
        "serializationRole": role, "schemaType": schema,
        "synthetic": False, **extra,
    }


def fixture_entries():
    deployment, service = FakeMapper.gvk_to_root.values()
    items = []
    for root in (deployment, service):
        items.append({"feature": root, "configPath": "", "serializationRole": "structural",
                      "synthetic": False})
        for path in ("apiVersion", "kind", "metadata.name"):
            items.append(entry(root, path))
    for path, schema in (("spec.strategy.type", "string"), ("spec.replicas", "integer"),
                         ("spec.revisionHistoryLimit", "integer")):
        items.append(entry(deployment, path, schema=schema))
    for path in ("spec.type", "spec.sessionAffinity"):
        items.append(entry(service, path))
    for root, path, values in (
        (deployment, "spec.strategy.type", ("RollingUpdate", "Recreate")),
        (service, "spec.type", ("ClusterIP", "NodePort")),
        (service, "spec.sessionAffinity", ("None", "ClientIP")),
    ):
        for value in values:
            items.append(entry(root, path, role="value", synthetic=True,
                               feature=f"{root}_{path.replace('.', '_')}_{value}",
                               configValue=value, configValueSource="description"))
    return items


class FakeFeature:
    def __init__(self, name, parent=None):
        self.name, self.parent = name, parent

    def get_parent(self):
        return self.parent


class FakeGenerator:
    """Sustituye la interfaz pública de main_k8s.py en las pruebas de integración."""
    class Configuration:
        def __init__(self, elements):
            self.elements = dict(elements)

    class UVLReader:
        def __init__(self, path):
            self.path = path

        def transform(self):
            return FakeGenerator.FM()

    class FM:
        def get_features(self):
            features = []
            roots = {root: FakeFeature(root) for root in FakeMapper.gvk_to_root.values()}
            for root in roots.values():
                features.append(root)
            for e in FakeMapper.serialization_entries:
                name = e.get("feature")
                if name in roots:
                    continue
                root = next((r for r in roots if name.startswith(r + "_")), None)
                if root:
                    features.append(FakeFeature(name, roots[root]))
            return features

    class FmToPysat:
        def __init__(self, fm):
            self.fm = fm

        def transform(self):
            return "fake_sat_model"

    @staticmethod
    def complete_and_minimize(fm, sat_model, config, expand_to_leaves=False):
        if any(name.endswith("_Recreate") for name in config.elements):
            raise ValueError("caso rechazado por el modelo simulado")
        return config

    @staticmethod
    def render_manifest(fm, config):
        return "apiVersion: apps/v1\nkind: Deployment\n"

    @staticmethod
    def write_manifest(content, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class PilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeMapper.serialization_entries = fixture_entries()
        cls.profile = json.loads(Path(__file__).with_name("pilot_profile.json").read_text())

    def test_exact_alternatives_and_numeric_zero(self):
        index = entries_for_root(FakeMapper.serialization_entries, "io_apps_v1_Deployment")
        decisions, feature, _ = encode_property(index, "spec.strategy.type", "RollingUpdate")
        self.assertIs(decisions[feature], True)
        self.assertEqual(encode_property(index, "spec.replicas", 0)[0]
                         ["io_apps_v1_Deployment_spec_replicas"], 0)
        with self.assertRaises(ValueError):
            encode_property(index, "spec.strategy.type", "SomethingElse")
        with self.assertRaises(ValueError):
            encode_property(index, "spec.replicas", "3")

    def test_plans_12_cases_without_selecting_root(self):
        cases = make_cases(self.profile, FakeMapper())
        self.assertEqual(len(cases), 12)
        self.assertTrue(all(not case.error for case in cases))
        self.assertTrue(all(case.root not in case.decisions for case in cases))
        self.assertEqual(sum(case.path == "(base)" for case in cases), 2)

    def test_plan_cli_with_metadata_mapper(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            descriptions = {"schema_metadata": []}
            for (api, kind), root in FakeMapper.gvk_to_root.items():
                group, version = api.split("/", 1) if "/" in api else ("", api)
                descriptions["schema_metadata"].append({
                    "feature_name": root, "feature_type": "schema-root",
                    "x_kubernetes": {"x-kubernetes-group-version-kind": [
                        {"group": group, "version": version, "kind": kind}]},
                })
            (directory / "metadata.json").write_text(
                json.dumps({"features": FakeMapper.serialization_entries}), encoding="utf-8")
            (directory / "descriptions.json").write_text(
                json.dumps(descriptions), encoding="utf-8")
            root = Path(__file__).parent
            result = subprocess.run([
                sys.executable, str(root / "experiment_runner.py"),
                "--profile", str(root / "pilot_profile.json"),
                "--mapper", str(root / "metadata_parser01.py"),
                "--metadata", str(directory / "metadata.json"),
                "--descriptions", str(directory / "descriptions.json"),
                "--output-dir", str(directory / "out"),
                "--plan", "--limit", "3",
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            files = list((directory / "out" / "partial_configs").glob("*.json"))
            self.assertEqual(len(files), 3)
            with (directory / "out" / "generation_results.csv").open(
                    encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 3)
            self.assertEqual({r["status"] for r in rows}, {"planned"})
            self.assertFalse((directory / "out" / "manifests").exists())

    def test_generate_manifest_and_record_failure(self):
        cases = make_cases(self.profile, FakeMapper())[:3]
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            rows = [{"case_id": c.case_id, "status": "planned", "error": "",
                     **{name: "" for name in
                        ("kind", "api_version", "variation_point", "requested_value",
                         "choice_feature", "value_source", "partial_json",
                         "full_config_json", "manifest_yaml", "elapsed_seconds")}}
                    for c in cases]
            args = argparse.Namespace(generator=Path("fake_generator.py"),
                                      uvl=Path("fake.uvl"), output_dir=out)
            with patch("experiment_runner.load_module", return_value=FakeGenerator):
                generate(cases, rows, args)
            self.assertEqual([r["status"] for r in rows],
                             ["generated", "generated", "failed"])
            self.assertEqual(len(list((out / "manifests").glob("*.yaml"))), 2)
            self.assertEqual(len(list((out / "full_configs").glob("*.json"))), 2)
            self.assertTrue((out / "generation_results.csv").is_file())


if __name__ == "__main__":
    unittest.main()
