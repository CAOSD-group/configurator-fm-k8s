
"""
Metadata-driven Kubernetes YAML -> UVL feature mapper.

This is the successor of the CSV-based mapper.  It intentionally preserves the
legacy mapping behaviours required by ConfigurationJSON/FlamaPy, but resolves
them from generated model metadata instead of from CSV columns and feature-name
heuristics.

Inputs
------
1. descriptions_*.json
   - schema-root metadata
   - x-kubernetes-group-version-kind declarations

2. serialization_metadata_*.json
   - configPath
   - serializationKind
   - serializationRole
   - configValue
   - schemaType / refs
   - synthetic helper metadata

Manifest roots are derived once when the mapper is initialized. A schema-root is
accepted only when it has at least one Kubernetes GVK and the generated
serialization metadata contains root-level ``apiVersion`` and ``kind`` paths.
No resource root is inferred from feature/schema names.

Output
------
    {"config": <UVL-feature-shaped JSON>}

The output shape remains compatible with the existing ConfigurationJSON reader.

Files can be supplied as YAML/YML or JSON. YAML is parsed with ``yaml.safe_load_all``
because Kubernetes manifests are not JSON files and may contain multiple documents.
"""

from __future__ import annotations

import json
import yaml
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any


class MetadataMappingError(ValueError):
    """Raised when a YAML construct cannot be mapped to the selected FM root."""


class MetadataMapper:
    """Map one Kubernetes manifest to UVL feature identifiers."""

    LEGACY_IGNORED_KEYS = {"clusterName"}

    def __init__(
        self,
        descriptions_path: str,
        serialization_metadata_path: str,
        *,
        strict: bool = False,
    ) -> None:
        self.descriptions_path = Path(descriptions_path)
        self.serialization_metadata_path = Path(serialization_metadata_path)
        self.strict = strict

        self.descriptions = self._load_json(self.descriptions_path)
        serialization_payload = self._load_json(self.serialization_metadata_path)

        self.serialization_entries = serialization_payload.get("features", [])
        if not isinstance(self.serialization_entries, list):
            raise MetadataMappingError(
                "serialization metadata must contain a list in 'features'."
            )

        self.root_features = self._collect_root_features()
        self.root_audit: dict[str, Any] = {}
        self.gvk_to_root = self._build_gvk_index()

        self.last_unmapped: list[dict[str, Any]] = []
        self.last_ignored: list[dict[str, Any]] = []
        self.last_root_feature: str | None = None

        self._path_index: dict[str, list[dict[str, Any]]] = {}
        self._active_entries: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Metadata loading / root selection
    # ------------------------------------------------------------------

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise MetadataMappingError(f"Expected JSON object in {path}")
        return data

    @staticmethod
    def _api_version(group: str | None, version: str) -> str:
        group = (group or "").strip()
        return f"{group}/{version}" if group else version

    def _collect_root_features(self) -> set[str]:
        """Return all top-level schema-definition features in serialization metadata."""
        roots: set[str] = set()

        for entry in self.serialization_entries:
            if not isinstance(entry, dict):
                continue
            if (
                entry.get("configPath") == ""
                and entry.get("serializationRole") == "structural"
                and not entry.get("synthetic", False)
                and entry.get("feature")
            ):
                roots.add(entry["feature"])

        return roots

    def _root_has_property_path(self, root_feature: str, path: str) -> bool:
        """Check whether a root definition actually serializes a root-level path."""
        for entry in self.serialization_entries:
            if not isinstance(entry, dict):
                continue

            feature = entry.get("feature")
            if not feature or not self._belongs_to_root(feature, root_feature):
                continue

            if (
                entry.get("configPath") == path
                and entry.get("serializationRole") == "property"
                and not entry.get("synthetic", False)
            ):
                return True

        return False

    def _build_gvk_index(self) -> dict[tuple[str, str], str]:
        """Build the exact manifest-root index from already-generated metadata.

        A schema definition is accepted as a manifest root iff:
        - it is a schema-root represented in serialization metadata;
        - it has at least one x-kubernetes-group-version-kind entry;
        - it has root-level apiVersion and kind properties.

        Definitions that fail this criterion remain valid internal FM types; they
        are simply not allowed to initiate YAML-to-FM mapping.
        """
        index: dict[tuple[str, str], str] = {}
        rejected: list[dict[str, Any]] = []
        definitions_with_gvk = 0
        unique_gvks: set[tuple[str, str, str]] = set()
        accepted_roots: set[str] = set()

        for meta in self.descriptions.get("schema_metadata", []):
            if not isinstance(meta, dict):
                continue
            if meta.get("feature_type") != "schema-root":
                continue

            feature_name = meta.get("feature_name")
            if not feature_name or feature_name not in self.root_features:
                continue

            xk = meta.get("x_kubernetes") or {}
            if not isinstance(xk, dict):
                continue

            gvk_entries = xk.get("x-kubernetes-group-version-kind", [])
            if isinstance(gvk_entries, dict):
                gvk_entries = [gvk_entries]
            if not isinstance(gvk_entries, list):
                continue

            valid_gvks = []
            seen_local: set[tuple[str, str, str]] = set()

            for gvk in gvk_entries:
                if not isinstance(gvk, dict):
                    continue

                group = (gvk.get("group") or "").strip()
                version = gvk.get("version")
                kind = gvk.get("kind")

                if not version or not kind:
                    continue

                identity = (group, version, kind)
                if identity in seen_local:
                    continue

                seen_local.add(identity)
                unique_gvks.add(identity)
                valid_gvks.append(identity)

            if not valid_gvks:
                continue

            definitions_with_gvk += 1

            missing = [
                path
                for path in ("apiVersion", "kind")
                if not self._root_has_property_path(feature_name, path)
            ]

            if missing:
                rejected.append({
                    "feature": feature_name,
                    "reason": "missing-root-properties",
                    "missingProperties": missing,
                    "gvkCount": len(valid_gvks),
                })
                continue

            accepted_roots.add(feature_name)

            for group, version, kind in valid_gvks:
                api_version = self._api_version(group, version)
                key = (api_version, kind)

                previous = index.get(key)
                if previous is not None and previous != feature_name:
                    raise MetadataMappingError(
                        "Ambiguous Kubernetes manifest GVK in generated metadata: "
                        f"apiVersion={api_version!r}, kind={kind!r} maps to both "
                        f"{previous!r} and {feature_name!r}."
                    )

                index[key] = feature_name

        self.root_audit = {
            "definitionsWithGVK": definitions_with_gvk,
            "uniqueGVKEntries": len(unique_gvks),
            "manifestRootDefinitions": len(accepted_roots),
            "supportedManifestGVKs": len(index),
            "rejectedGVKDefinitions": len(rejected),
            "ambiguousGVKs": 0,
            "rejectedDefinitions": rejected,
        }

        return index

    def resolve_root_feature(self, api_version: str, kind: str) -> str:
        """Resolve an exact converter-supported Kubernetes manifest GVK."""
        root = self.gvk_to_root.get((api_version, kind))

        if root is None:
            raise MetadataMappingError(
                "Unsupported Kubernetes manifest root: "
                f"apiVersion={api_version!r}, kind={kind!r}"
            )

        return root

    @staticmethod
    def _belongs_to_root(feature: str, root: str) -> bool:
        return feature == root or feature.startswith(root + "_")

    def _activate_root(self, root: str) -> None:
        self.last_root_feature = root
        self._path_index = {}
        self._active_entries = []

        for entry in self.serialization_entries:
            if not isinstance(entry, dict):
                continue
            feature = entry.get("feature")
            path = entry.get("configPath")
            if not feature or path is None:
                continue
            if not self._belongs_to_root(feature, root):
                continue

            self._active_entries.append(entry)
            self._path_index.setdefault(path, []).append(entry)

    # ------------------------------------------------------------------
    # Diagnostics / compatibility normalisation
    # ------------------------------------------------------------------

    def _report_unmapped(self, path: str, value: Any, reason: str) -> None:
        item = {
            "path": path,
            "reason": reason,
            "valueType": type(value).__name__,
        }
        self.last_unmapped.append(item)
        if self.strict:
            raise MetadataMappingError(
                f"Cannot map '{path}' ({type(value).__name__}): {reason}"
            )

    def _report_ignored(self, path: str, value: Any, reason: str) -> None:
        self.last_ignored.append(
            {
                "path": path,
                "reason": reason,
                "valueType": type(value).__name__,
            }
        )

    @classmethod
    def _normalise_value(cls, value: Any) -> Any:
        """Preserve the datetime/date/time behaviour of the legacy mapper."""
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.isoformat()
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(value, (date, time)):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: cls._normalise_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._normalise_value(v) for v in value]
        return value

    # ------------------------------------------------------------------
    # Metadata queries
    # ------------------------------------------------------------------

    def _entries(self, path: str) -> list[dict[str, Any]]:
        return self._path_index.get(path, [])

    def _property_entry(self, path: str) -> dict[str, Any] | None:
        entries = self._entries(path)

        candidates = [
            e
            for e in entries
            if not e.get("synthetic", False)
            and e.get("serializationRole") in {"property", "structural"}
        ]

        if not candidates:
            return None

        union_structural = [
            e
            for e in candidates
            if e.get("serializationRole") == "structural"
            and e.get("serializationKind") == "union"
        ]
        if union_structural:
            return union_structural[0]

        property_candidates = [
            e for e in candidates if e.get("serializationRole") == "property"
        ]
        if property_candidates:
            return property_candidates[0]

        return candidates[0]

    def _role_entries(self, path: str, role: str) -> list[dict[str, Any]]:
        return [
            e
            for e in self._entries(path)
            if e.get("serializationRole") == role
        ]

    def _has_descendant_metadata(self, path: str) -> bool:
        prefixes = (
            path + ".",
            path + "[]",
            path + "{}",
        )
        for entry in self._active_entries:
            ep = entry.get("configPath", "")
            if ep != path and any(ep.startswith(prefix) for prefix in prefixes):
                return True
        return False

    # ------------------------------------------------------------------
    # Value matching
    # ------------------------------------------------------------------

    @staticmethod
    def _config_value_matches(expected: Any, actual: Any) -> bool:
        # Exact structured values (None, {}, [], booleans...) first.
        if type(expected) is type(actual) and expected == actual:
            return True

        # Description-derived enum alternatives are commonly strings.
        if isinstance(expected, str) and not isinstance(actual, (dict, list)):
            return str(actual) == expected

        return False

    @staticmethod
    def _schema_type_rank(schema_type: str | None, value: Any) -> int | None:
        if schema_type == "boolean":
            return 0 if isinstance(value, bool) else None

        if schema_type == "integer":
            return 0 if isinstance(value, int) and not isinstance(value, bool) else None

        if schema_type == "number":
            if isinstance(value, float):
                return 0
            if isinstance(value, int) and not isinstance(value, bool):
                return 1
            return None

        if schema_type == "string":
            return 0 if isinstance(value, str) else None

        if schema_type == "null":
            return 0 if value is None else None

        return None

    def _select_value_entry(
        self,
        entries: list[dict[str, Any]],
        value: Any,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Choose a concrete enum/synthetic/union representation.

        Returns:
            (entry, is_boolean_selection)

        configValue matches are Boolean feature selections.
        Typed synthetic representations carry the actual scalar value.
        """
        concrete = [
            e
            for e in entries
            if "configValue" in e
            and self._config_value_matches(e.get("configValue"), value)
        ]
        if concrete:
            return concrete[0], True

        typed = []
        for entry in entries:
            schema_type = entry.get("schemaType")
            rank = self._schema_type_rank(schema_type, value)
            if rank is not None:
                typed.append((rank, entry))

        if typed:
            typed.sort(key=lambda item: item[0])
            return typed[0][1], False

        no_config_value = [e for e in entries if "configValue" not in e]
        if len(no_config_value) == 1:
            # Legacy nameStr/valueInt/simple-ref helpers sometimes have no
            # explicit schemaType in the sidecar. One candidate is unambiguous.
            return no_config_value[0], False

        return None, False

    def _match_config_value_entries(
        self,
        path: str,
        value: Any,
    ) -> list[dict[str, Any]]:
        return [
            e
            for e in self._role_entries(path, "value")
            if "configValue" in e
            and self._config_value_matches(e.get("configValue"), value)
        ]

    def _check_fixed_value(
        self,
        entry: dict[str, Any],
        value: Any,
        path: str,
    ) -> bool:
        if "fixedValue" not in entry:
            return True
        expected = entry["fixedValue"]
        if self._config_value_matches(expected, value):
            return True
        self._report_unmapped(
            path,
            value,
            f"value differs from fixedValue {expected!r}",
        )
        return False

    # ------------------------------------------------------------------
    # Concrete serializers to the ConfigurationJSON-compatible JSON shape
    # ------------------------------------------------------------------

    def _map_scalar(
        self,
        path: str,
        entry: dict[str, Any],
        value: Any,
    ) -> dict[str, Any] | None:
        feature = entry["feature"]

        if not self._check_fixed_value(entry, value, path):
            return None

        value_entries = self._role_entries(path, "value")
        if value_entries:
            selected, as_boolean = self._select_value_entry(value_entries, value)
            if selected is not None:
                return {
                    feature: {
                        selected["feature"]: True if as_boolean else value
                    }
                }

            self._report_unmapped(
                path,
                value,
                "no value/union alternative matches the YAML value",
            )
            return None

        return {feature: value}

    def _map_primitive_item(
        self,
        path: str,
        value: Any,
        role: str,
    ) -> dict[str, Any] | None:
        entries = self._role_entries(path, role)
        selected, as_boolean = self._select_value_entry(entries, value)
        if selected is None:
            self._report_unmapped(
                path,
                value,
                f"no {role} feature matches the YAML value",
            )
            return None

        return {
            selected["feature"]: True if as_boolean else value
        }

    def _map_array_config_values(
        self,
        path: str,
        parent_feature: str,
        value: list[Any],
    ) -> dict[str, Any] | None:
        """Preserve the old 'value occurs in YAML list' behaviour.

        This is only used when the model contains explicit configValue children
        at the array property's own path.
        """
        value_entries = [
            e
            for e in self._role_entries(path, "value")
            if "configValue" in e
        ]
        if not value_entries:
            return None

        selected_children: dict[str, bool] = {}
        matched_count = 0

        for item in value:
            matches = [
                e
                for e in value_entries
                if self._config_value_matches(e.get("configValue"), item)
            ]
            if matches:
                selected_children[matches[0]["feature"]] = True
                matched_count += 1

        if matched_count == len(value) and selected_children:
            return {parent_feature: selected_children}

        return None

    def _map_array(
        self,
        path: str,
        entry: dict[str, Any],
        value: list[Any],
    ) -> dict[str, Any]:
        feature = entry["feature"]

        legacy_values = self._map_array_config_values(path, feature, value)
        if legacy_values is not None:
            return legacy_values

        item_path = f"{path}[]"
        mapped_items: list[Any] = []

        for item in value:
            if isinstance(item, dict):
                mapped_items.append(self._map_mapping(item, item_path))
            elif isinstance(item, list):
                self._report_unmapped(
                    item_path,
                    item,
                    "nested list item is not currently represented by the generated metadata",
                )
            else:
                mapped = self._map_primitive_item(item_path, item, "arrayItem")
                if mapped is not None:
                    mapped_items.append(mapped)

        return {feature: mapped_items}

    def _map_map_array_value_legacy(
        self,
        map_key: Any,
        map_value: list[Any],
        value_entries: list[dict[str, Any]],
        path: str,
    ) -> dict[str, Any] | None:
        """Compatibility for legacy StringValueAdditional.

        The old mapper represented `additionalProperties: array[string]` as a
        synthetic scalar carrying "key:[values]". Keeping this exact shape
        avoids changing ConfigurationJSON at the same time.
        """
        array_entries = [
            e
            for e in value_entries
            if e.get("serializationKind") == "array"
        ]
        if len(array_entries) != 1:
            return None

        selected = array_entries[0]
        return {
            selected["feature"]: f"{map_key}:{map_value}"
        }

    def _map_dynamic_map(
        self,
        path: str,
        entry: dict[str, Any],
        value: dict[Any, Any],
    ) -> dict[str, Any]:
        feature = entry["feature"]
        item_path = f"{path}{{}}"

        key_entries = self._role_entries(item_path, "mapKey")
        key_entry = key_entries[0] if key_entries else None
        value_entries = self._role_entries(item_path, "mapValue")

        rows: list[dict[str, Any]] = []

        for map_key, map_value in value.items():
            # Legacy AdditionalProperties<Array<String>> representation.
            if isinstance(map_value, list):
                legacy = self._map_map_array_value_legacy(
                    map_key,
                    map_value,
                    value_entries,
                    item_path,
                )
                if legacy is not None:
                    rows.append(legacy)
                    continue

            row: dict[str, Any] = {}

            if key_entry is not None:
                row[key_entry["feature"]] = map_key
            else:
                self._report_unmapped(
                    item_path,
                    map_key,
                    "dynamic map has no mapKey metadata",
                )

            if isinstance(map_value, dict):
                # Structured additionalProperties object.
                row.update(self._map_mapping(map_value, item_path))

            elif isinstance(map_value, list):
                self._report_unmapped(
                    item_path,
                    map_value,
                    "no array-valued mapValue representation matches this dynamic map",
                )

            else:
                selected, as_boolean = self._select_value_entry(
                    value_entries,
                    map_value,
                )
                if selected is not None:
                    row[selected["feature"]] = (
                        True if as_boolean else map_value
                    )
                else:
                    self._report_unmapped(
                        item_path,
                        map_value,
                        "no mapValue representation matches scalar value",
                    )

            rows.append(row)

        return {feature: rows}

    def _map_special_config_value(
        self,
        path: str,
        parent_feature: str,
        value: Any,
    ) -> dict[str, Any] | None:
        """Handle synthetic null/empty/etc. before normal object/list recursion."""
        matches = self._match_config_value_entries(path, value)
        if not matches:
            return None
        return {
            parent_feature: {
                matches[0]["feature"]: True
            }
        }

    def _map_property(
        self,
        path: str,
        value: Any,
    ) -> dict[str, Any] | None:
        entry = self._property_entry(path)
        if entry is None:
            self._report_unmapped(
                path,
                value,
                "no real property/structural feature exists for this path",
            )
            return None

        feature = entry["feature"]
        serialization_kind = entry.get("serializationKind")

        if not self._check_fixed_value(entry, value, path):
            return None

        # Must run before dict/list handling. This covers generated helpers such
        # as _isNull, _isEmpty and _isEmpty02 through configValue metadata.
        special = self._map_special_config_value(path, feature, value)
        if special is not None:
            return special

        if isinstance(value, dict):
            if serialization_kind == "map":
                return self._map_dynamic_map(path, entry, value)

            # Preserve legacy preserveUnknownFields/specialType behaviour:
            # opaque objects without modelled descendants activate the parent
            # feature but do not attempt to map arbitrary unknown keys.
            if not self._has_descendant_metadata(path):
                return {feature: {}}

            return {
                feature: self._map_mapping(value, path)
            }

        if isinstance(value, list):
            if serialization_kind != "array":
                self._report_unmapped(
                    path,
                    value,
                    f"YAML list mapped to non-array serializationKind={serialization_kind!r}",
                )
                return None
            return self._map_array(path, entry, value)

        return self._map_scalar(path, entry, value)

    def _map_mapping(
        self,
        data: dict[str, Any],
        base_path: str = "",
    ) -> dict[str, Any]:
        mapped: dict[str, Any] = {}

        for key, value in data.items():
            if key is None:
                self._report_unmapped(base_path, value, "YAML key is null")
                continue

            path = f"{base_path}.{key}" if base_path else str(key)
            value = self._normalise_value(value)

            # Compatibility with the previous mapper. Only ignore clusterName
            # when the generated FM actually has no metadata for that path.
            if (
                key in self.LEGACY_IGNORED_KEYS
                and self._property_entry(path) is None
            ):
                self._report_ignored(
                    path,
                    value,
                    "legacy unsupported Kubernetes property",
                )
                continue

            fragment = self._map_property(path, value)
            if fragment:
                mapped.update(fragment)

        return mapped

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def load_manifest_file(path: str | Path) -> list[dict[str, Any]]:
        """Load one JSON file or one/multiple Kubernetes YAML documents.

        Parsing errors are converted into MetadataMappingError with source
        line/column information, avoiding an unhandled PyYAML traceback.
        The mapper does not try to repair malformed YAML because changing
        indentation or structure automatically could change manifest semantics.
        """
        file_path = Path(path)
        suffix = file_path.suffix.lower()

        try:
            if suffix in {".yaml", ".yml"}:
                with file_path.open("r", encoding="utf-8") as f:
                    docs = [
                        doc for doc in yaml.safe_load_all(f)
                        if doc is not None
                    ]

            elif suffix == ".json":
                with file_path.open("r", encoding="utf-8") as f:
                    docs = [json.load(f)]

            else:
                raise MetadataMappingError(
                    f"Unsupported manifest extension: {file_path.suffix}"
                )

        except yaml.MarkedYAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            problem = getattr(exc, "problem", None) or str(exc)

            if mark is not None:
                location = f"line {mark.line + 1}, column {mark.column + 1}"
            else:
                location = "unknown location"

            raise MetadataMappingError(
                f"Invalid YAML in {file_path} at {location}: {problem}"
            ) from exc

        except yaml.YAMLError as exc:
            raise MetadataMappingError(
                f"Invalid YAML in {file_path}: {exc}"
            ) from exc

        except json.JSONDecodeError as exc:
            raise MetadataMappingError(
                f"Invalid JSON in {file_path} at line {exc.lineno}, "
                f"column {exc.colno}: {exc.msg}"
            ) from exc

        except OSError as exc:
            raise MetadataMappingError(
                f"Cannot read manifest file {file_path}: {exc}"
            ) from exc

        for index, doc in enumerate(docs, start=1):
            if not isinstance(doc, dict):
                raise MetadataMappingError(
                    f"Document {index} in {file_path} is not a mapping/object."
                )

        return docs

    def transform_file(self, path: str | Path) -> list[dict[str, Any]]:
        """Load and map all Kubernetes documents contained in a file."""
        return [
            self.transform_manifest(doc)
            for doc in self.load_manifest_file(path)
        ]

    def transform_manifest(self, doc_dict: dict[str, Any]) -> dict[str, Any]:
        """Map one Kubernetes YAML document to ConfigurationJSON-compatible JSON."""
        if not isinstance(doc_dict, dict):
            raise MetadataMappingError("The Kubernetes document must be a mapping/object.")

        doc_dict = self._normalise_value(doc_dict)

        api_version = doc_dict.get("apiVersion")
        kind = doc_dict.get("kind")
        if not isinstance(api_version, str) or not isinstance(kind, str):
            raise MetadataMappingError(
                "The manifest must contain string root fields apiVersion and kind."
            )

        root = self.resolve_root_feature(api_version, kind)
        self._activate_root(root)

        self.last_unmapped = []
        self.last_ignored = []

        mapped = self._map_mapping(doc_dict)

        return {
            "config": mapped
        }

    def mapping_report(self) -> dict[str, Any]:
        """Compact diagnostics for regression/evaluation runs."""
        return {
            "rootFeature": self.last_root_feature,
            "rootAudit": dict(self.root_audit),
            "unmapped": list(self.last_unmapped),
            "ignored": list(self.last_ignored),
        }


__all__ = ["MetadataMapper", "MetadataMappingError"]

if __name__ == '__main__':
    import sys

    #if len(sys.argv) != 4:
    #    print("Usage: python metadata_parser.py <descriptions.json> <serialization_metadata.json> <manifest.json>")
    #    sys.exit(1)

    #descriptions_path = sys.argv[1]
    descriptions_path = '../../resources/model_generation/descriptions_1-37_V2.json'
    #serialization_metadata_path = sys.argv[2]
    serialization_metadata_path = '../../resources/model_generation/serialization_metadata_1-37_V2.json'
    #manifest_path = sys.argv[3]
    manifest_path = '../../resources/testing_yamls/deployment01.yaml'

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = yaml.safe_load(f)
        #with open(manifest_path, 'r', encoding='utf-8') as f

    mapper = MetadataMapper(descriptions_path, serialization_metadata_path)
    result = mapper.transform_manifest(manifest)

    print(json.dumps(result, indent=2))