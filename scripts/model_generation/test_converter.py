
import argparse
import importlib.util
import sys
import unittest
from pathlib import Path

CONVERTER_PATH = None


def load_converter(path: Path):
    spec = importlib.util.spec_from_file_location("convert04_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load converter: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Convert04CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_converter(CONVERTER_PATH)

    def new_processor(self, definitions=None):
        definitions = definitions or {"definitions": {}}
        return self.module.SchemaProcessor(definitions)

    def test_unconditional_documented_default_marks_default_variant(self):
        p = self.new_processor()
        desc = (
            'Type of deployment. Can be "Recreate" or "RollingUpdate". '
            'Default is RollingUpdate.'
        )

        mandatory, optional = p.parse_properties(
            {"type": {"type": "string", "description": desc}},
            [],
            "io_k8s_api_apps_v1_Deployment_spec_strategy",
            config_path="spec.strategy",
        )

        self.assertEqual(len(mandatory), 0)
        self.assertEqual(len(optional), 1)

        feature = optional[0]
        parent_attrs = dict((k, v) for k, v in feature["attributes"] if v is not None)

        self.assertEqual(parent_attrs.get("configKey"), "type")
        self.assertEqual(parent_attrs.get("serializationKind"), "scalar")
        self.assertEqual(parent_attrs.get("serializationRole"), "property")
        self.assertEqual(parent_attrs.get("documentedDefault"), "RollingUpdate")

        children = {c["name"].split("_")[-1]: c for c in feature["sub_features"]}
        self.assertIn("Recreate", children)
        self.assertIn("RollingUpdate", children)

        rolling = children["RollingUpdate"]
        rolling_flags = {k for k, v in rolling["attributes"] if v is None}
        rolling_attrs = {k: v for k, v in rolling["attributes"] if v is not None}

        self.assertIn("default", rolling_flags)
        self.assertEqual(rolling_attrs.get("configValue"), "RollingUpdate")
        self.assertEqual(rolling_attrs.get("serializationRole"), "value")

        recreate_flags = {k for k, v in children["Recreate"]["attributes"] if v is None}
        self.assertNotIn("default", recreate_flags)

    def test_conditional_default_does_not_mark_global_default_variant(self):
        p = self.new_processor()
        desc = 'Mode can be "A" or "B". Defaults to A if X is enabled, otherwise B.'

        _, optional = p.parse_properties(
            {"mode": {"type": "string", "description": desc}},
            [],
            "Example",
            config_path="spec",
        )

        feature = optional[0]
        attrs = dict((k, v) for k, v in feature["attributes"] if v is not None)

        self.assertEqual(attrs.get("conditionalDefault"), "A")
        self.assertNotIn("documentedDefault", attrs)

        for child in feature["sub_features"]:
            flags = {k for k, v in child["attributes"] if v is None}
            self.assertNotIn("default", flags)

    def test_schema_default_is_defaultValue_not_default_variant(self):
        p = self.new_processor()

        attrs = p.schema_attributes(
            {"type": "integer", "default": 3},
            config_key="retries",
            serialization_kind="scalar",
            serialization_role="property",
        )

        attrs_dict = dict((k, v) for k, v in attrs if v is not None)
        flags = {k for k, v in attrs if v is None}

        self.assertEqual(attrs_dict.get("defaultValue"), 3)
        self.assertEqual(attrs_dict.get("defaultValueSource"), "schema")
        self.assertNotIn("default", flags)

    def test_singleton_enum_is_fixedValue_and_configValue(self):
        p = self.new_processor()

        attrs = p.schema_attributes(
            {"type": "string", "enum": ["Deployment"]},
            config_key="kind",
            serialization_kind="scalar",
            serialization_role="property",
            enum_source="schema",
        )

        attrs_dict = dict((k, v) for k, v in attrs if v is not None)

        self.assertEqual(attrs_dict.get("fixedValue"), "Deployment")
        self.assertEqual(attrs_dict.get("configValue"), "Deployment")
        self.assertEqual(attrs_dict.get("configValueSource"), "schema.enum")

    def test_dynamic_map_is_explicitly_marked_as_map(self):
        definitions = {
            "definitions": {
                "io.k8s.apimachinery.pkg.api.resource.Quantity": {
                    "oneOf": [{"type": "string"}, {"type": "number"}]
                }
            }
        }
        p = self.new_processor(definitions)

        desc = (
            "Requests describes the minimum amount of compute resources required. "
            "If Requests is omitted for a container, it defaults to Limits if that is "
            "explicitly specified, otherwise to an implementation-defined value."
        )

        _, optional = p.parse_properties(
            {
                "requests": {
                    "type": "object",
                    "description": desc,
                    "additionalProperties": {
                        "$ref": "#/definitions/io.k8s.apimachinery.pkg.api.resource.Quantity"
                    },
                }
            },
            [],
            "Container_resources",
            config_path="resources",
        )

        feature = optional[0]
        attrs = dict((k, v) for k, v in feature["attributes"] if v is not None)

        self.assertEqual(attrs.get("serializationKind"), "map")
        self.assertEqual(attrs.get("configKey"), "requests")
        self.assertEqual(attrs.get("conditionalDefault"), "Limits")

        key = next(c for c in feature["sub_features"] if c["name"].endswith("_KeyMap"))
        key_attrs = dict((k, v) for k, v in key["attributes"] if v is not None)
        self.assertEqual(key_attrs.get("serializationRole"), "mapKey")

        values = [
            c for c in feature["sub_features"]
            if c["name"].endswith("_asString") or c["name"].endswith("_asNumber")
        ]
        self.assertEqual(len(values), 2)
        for value in values:
            value_attrs = dict((k, v) for k, v in value["attributes"] if v is not None)
            self.assertEqual(value_attrs.get("serializationRole"), "mapValue")

    def test_terminal_required_is_mandatory_with_provenance(self):
        p = self.new_processor()
        desc = "Only valid property names are accessible. Required."

        mandatory, optional = p.parse_properties(
            {"expression": {"type": "string", "description": desc}},
            [],
            "Example",
            config_path="spec",
        )

        self.assertEqual(len(mandatory), 1)
        self.assertEqual(len(optional), 0)

        attrs = dict((k, v) for k, v in mandatory[0]["attributes"] if v is not None)
        self.assertEqual(attrs.get("requiredSource"), "description")
        self.assertTrue(attrs.get("documentedRequired"))

    def test_conditional_required_text_is_not_promoted(self):
        p = self.new_processor()

        mandatory, optional = p.parse_properties(
            {
                "service": {
                    "type": "string",
                    "description": "Exactly one of url or service is required."
                }
            },
            [],
            "Example",
            config_path="spec",
        )

        self.assertEqual(len(mandatory), 0)
        self.assertEqual(len(optional), 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--converter",
        type=Path,
        default=Path(__file__).with_name("convert04_serialization.py"),
        help="Path to convert04_serialization.py",
    )
    args, remaining = parser.parse_known_args()

    CONVERTER_PATH = args.converter.resolve()
    sys.argv = [sys.argv[0], *remaining]
    unittest.main(verbosity=2)