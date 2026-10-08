#!/usr/bin/env python3
"""
Audit semantic information contained in Kubernetes _definitions.json descriptions
against the extraction logic of the current FM generator.

Purpose
-------
This script is NOT a new FM generator. It closes the remaining uncertainties before
freezing the next generator version:

1) Which documented defaults contain an extractable concrete value?
2) Which "Required" mentions are truly terminal/unconditional vs conditional prose?
3) How many descriptions with allowed-value language are handled by extract_values()?
4) How much deprecation wording is recognized by the current generator?
5) Which mutual-exclusion descriptions should be deferred to the semantic-constraint phase?

The script can import the exact SchemaProcessor from the generator under test, so the
coverage numbers reflect the implementation actually used.

Example
-------
python audit_description_semantics.py ^
  ../../resources/kubernetes-json-v1.37.1/_definitions_1-37.json ^
  --generator ./convert01_v1_37_definitive.py ^
  --out-dir ../../resources/kubernetes-json-v1.37.1/description_audit

Outputs
-------
description_audit_summary.json
description_audit_summary.csv
defaults.csv
required.csv
allowed_values.csv
deprecated.csv
mutual_exclusions.csv
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import importlib.util
import io
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Candidate detection: deliberately broader than the generator extractor.
# The audit compares "candidate mentions" against "what the generator extracts".
# ---------------------------------------------------------------------------

DEFAULT_VALUE_CLAIM_RE = re.compile(
    r"\bdefaults?\s+to\b|"
    r"\bdefault\s+value\s+is\b|"
    r"\bdefault\s+is\b|"
    r"\bimplicitly\s+inferred\s+to\s+be\b",
    re.IGNORECASE,
)
DEFAULT_CONTEXT_ONLY_RE = re.compile(r"\bby\s+default\b", re.IGNORECASE)

REQUIRED_TERMINAL_RE = re.compile(r"\bRequired\.\s*$", re.IGNORECASE)
REQUIRED_CONDITIONAL_RE = re.compile(
    r"\brequired\s+(?:when|if|for|to)\b|"
    r"\brequired\s+unless\b|"
    r"\bmust\s+be\s+set\s+if\b|"
    r"\bmust\s+be\s+set\s+when\b|"
    r"\brequired\s+when\s+scope\b",
    re.IGNORECASE,
)
REQUIRED_ANY_RE = re.compile(r"\brequired\b", re.IGNORECASE)

ALLOWED_VALUES_CANDIDATE_RE = re.compile(
    r"\bvalid values?\b|"
    r"\bpossible values?\b|"
    r"\ballowed values?\b|"
    r"\bsupported values?\b|"
    r"\bvalid options?\b|"
    r"\bsupported types?\b|"
    r"\bvalid operators?\b|"
    r"\bonly valid values?\b|"
    r"\bshould be one of\b|"
    r"\bwill be one of\b|"
    r"\bmust be one of\b|"
    r"\bone of\b|"
    r"\bcan be\b",
    re.IGNORECASE,
)

ALLOWED_VALUES_STRONG_RE = re.compile(
    r"\bvalid values?\b|"
    r"\ballowed values?\b|"
    r"\bsupported values?\b|"
    r"\bvalid options?\b|"
    r"\bsupported types?\b|"
    r"\bvalid operators?\b|"
    r"\bonly valid values?\b|"
    r"\bshould be one of\b|"
    r"\bwill be one of\b|"
    r"\bmust be one of\b|"
    r"\bone of\b",
    re.IGNORECASE,
)

DEPRECATED_RE = re.compile(r"\bdeprecated\b", re.IGNORECASE)

MUTUAL_EXCLUSION_RE = re.compile(
    r"\bmutually exclusive\b|"
    r"\bnot both\b|"
    r"\beither\b.{0,160}\bor\b|"
    r"\bcannot be set when\b|"
    r"\bmay not be set\b|"
    r"\bmust not be set\b",
    re.IGNORECASE | re.DOTALL,
)


SCHEMA_MAP_KEYS = {
    "properties", "patternProperties", "definitions", "$defs", "dependentSchemas"
}
SCHEMA_SINGLE_KEYS = {
    "additionalProperties", "additionalItems", "not", "contains",
    "propertyNames", "if", "then", "else",
    "unevaluatedProperties", "unevaluatedItems"
}
SCHEMA_LIST_KEYS = {
    "oneOf", "anyOf", "allOf", "prefixItems"
}


def jp_escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def load_generator_processor(generator_path: Optional[Path], definitions: Dict[str, Any]):
    """Load SchemaProcessor from the exact generator file, if supplied."""
    if generator_path is None:
        return FallbackExtractor()

    generator_path = generator_path.resolve()
    spec = importlib.util.spec_from_file_location("fm_generator_under_audit", generator_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import generator: {generator_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cls = getattr(module, "SchemaProcessor", None)
    if cls is None:
        raise RuntimeError(f"{generator_path} does not define SchemaProcessor")
    return cls(definitions)


class FallbackExtractor:
    """
    Conservative fallback mirroring the relevant logic from the 2026-10-07
    definitive generator. Prefer --generator so the exact implementation is used.
    """

    def extract_documented_default(self, description: str):
        if not description:
            return None
        prefix = r"(?:defaults? to|default value is|default is|implicitly inferred to be)"
        patterns = [
            re.compile(prefix + r'\s+["`](.*?)["`]', re.IGNORECASE),
            re.compile(prefix + r"\s+([-+]?\d+(?:\.\d+)?)", re.IGNORECASE),
            re.compile(prefix + r"\s+(true|false)\b", re.IGNORECASE),
            re.compile(prefix + r"\s+([A-Za-z0-9_*/%+:-]+)", re.IGNORECASE),
        ]
        for pattern in patterns:
            match = pattern.search(description)
            if match:
                return match.group(1).strip()
        return None

    def is_required_based_on_description(self, description: str):
        return description.strip().endswith("Required.")

    def extract_values(self, description: str):
        # This fallback intentionally does not pretend to reproduce the long legacy
        # regex list. Use --generator for exact allowed-value coverage.
        return None


def generator_deprecated_recognized(description: str) -> bool:
    """Mirror the current inline deprecation recognition in the generator."""
    if not description:
        return False
    return (
        "DEPRECATED:" in description
        or "deprecated." in description.lower()
        or "this field is deprecated," in description.lower()
        or "deprecated field" in description.lower()
    )


def safe_extract_values(processor, description: str):
    """Call legacy extract_values without flooding the audit console."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            values = processor.extract_values(description)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if values is None:
        return None, None
    if isinstance(values, set):
        values = sorted(values)
    elif isinstance(values, tuple):
        values = list(values)
    elif not isinstance(values, list):
        try:
            values = list(values)
        except TypeError:
            values = [values]
    return values, None


def walk_property_nodes(
    node: Any,
    schema_path: str,
    root_definition: str,
    parent_required: Optional[Iterable[str]] = None,
):
    """
    Yield actual property schema nodes with their parent's required[] context.

    This does not confuse a property named "enum"/"default"/"pattern" with the
    corresponding JSON-Schema keyword.
    """
    if not isinstance(node, dict):
        return

    required_here = set(node.get("required", [])) if isinstance(node.get("required"), list) else set()
    props = node.get("properties")

    if isinstance(props, dict):
        for prop_name, prop_schema in props.items():
            prop_path = f"{schema_path}/properties/{jp_escape(prop_name)}"
            if isinstance(prop_schema, dict):
                yield {
                    "definition": root_definition,
                    "property": prop_name,
                    "path": prop_path,
                    "schema": prop_schema,
                    "required_by_schema": prop_name in required_here,
                }
                yield from walk_property_nodes(
                    prop_schema,
                    prop_path,
                    root_definition,
                )

    for key in SCHEMA_MAP_KEYS - {"properties"}:
        mapping = node.get(key)
        if isinstance(mapping, dict):
            for child_name, child_schema in mapping.items():
                if isinstance(child_schema, dict):
                    child_path = f"{schema_path}/{jp_escape(key)}/{jp_escape(str(child_name))}"
                    yield from walk_property_nodes(child_schema, child_path, root_definition)

    items = node.get("items")
    if isinstance(items, dict):
        yield from walk_property_nodes(items, f"{schema_path}/items", root_definition)
    elif isinstance(items, list):
        for i, child in enumerate(items):
            if isinstance(child, dict):
                yield from walk_property_nodes(child, f"{schema_path}/items/{i}", root_definition)

    for key in SCHEMA_SINGLE_KEYS:
        child = node.get(key)
        if isinstance(child, dict):
            yield from walk_property_nodes(child, f"{schema_path}/{jp_escape(key)}", root_definition)

    for key in SCHEMA_LIST_KEYS:
        children = node.get(key)
        if isinstance(children, list):
            for i, child in enumerate(children):
                if isinstance(child, dict):
                    yield from walk_property_nodes(
                        child, f"{schema_path}/{jp_escape(key)}/{i}", root_definition
                    )

    deps = node.get("dependencies")
    if isinstance(deps, dict):
        for dep_name, dep_value in deps.items():
            if isinstance(dep_value, dict):
                yield from walk_property_nodes(
                    dep_value,
                    f"{schema_path}/dependencies/{jp_escape(str(dep_name))}",
                    root_definition,
                )


def rows_to_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            cooked = {}
            for key in fieldnames:
                value = row.get(key, "")
                if isinstance(value, (list, dict, tuple, set)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                cooked[key] = value
            writer.writerow(cooked)


def audit(definitions: Dict[str, Any], processor) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
    definitions_map = definitions.get("definitions", {})
    if not isinstance(definitions_map, dict):
        raise ValueError("Input has no top-level 'definitions' object.")

    defaults_rows = []
    required_rows = []
    allowed_rows = []
    deprecated_rows = []
    mutual_rows = []

    counts = Counter()
    required_overlap = Counter()

    for def_name, def_schema in definitions_map.items():
        if not isinstance(def_schema, dict):
            continue

        root_path = f"#/definitions/{jp_escape(def_name)}"

        for item in walk_property_nodes(def_schema, root_path, def_name):
            details = item["schema"]
            description = details.get("description", "")
            if not isinstance(description, str) or not description:
                continue

            counts["properties_with_description"] += 1

            # ---------------------------------------------------------------
            # Defaults
            # ---------------------------------------------------------------
            default_claim = bool(DEFAULT_VALUE_CLAIM_RE.search(description))
            default_context = bool(DEFAULT_CONTEXT_ONLY_RE.search(description))
            extracted_default = None
            if hasattr(processor, "extract_documented_default"):
                try:
                    extracted_default = processor.extract_documented_default(description)
                except Exception:
                    extracted_default = None

            if default_claim or default_context:
                if default_claim:
                    counts["default_value_claim_candidates"] += 1
                if default_context:
                    counts["default_context_by_default"] += 1
                if extracted_default is not None:
                    counts["default_values_extracted"] += 1
                elif default_claim:
                    counts["default_value_claim_missed"] += 1

                defaults_rows.append({
                    "definition": item["definition"],
                    "property": item["property"],
                    "path": item["path"],
                    "candidate_kind": (
                        "value_claim+by_default" if default_claim and default_context
                        else "value_claim" if default_claim
                        else "by_default_context"
                    ),
                    "extracted_value": extracted_default if extracted_default is not None else "",
                    "generator_recognized": extracted_default is not None,
                    "description": description,
                })

            # ---------------------------------------------------------------
            # Required
            # ---------------------------------------------------------------
            required_any = bool(REQUIRED_ANY_RE.search(description))
            terminal_required = bool(REQUIRED_TERMINAL_RE.search(description))
            conditional_required = bool(REQUIRED_CONDITIONAL_RE.search(description))
            generator_required = False
            if hasattr(processor, "is_required_based_on_description"):
                try:
                    generator_required = bool(processor.is_required_based_on_description(description))
                except Exception:
                    generator_required = False

            if item["required_by_schema"]:
                required_overlap["schema_required"] += 1
            if terminal_required:
                required_overlap["terminal_required"] += 1
            if conditional_required:
                required_overlap["conditional_required"] += 1
            if item["required_by_schema"] and terminal_required:
                required_overlap["schema_and_terminal"] += 1
            if terminal_required and not item["required_by_schema"]:
                required_overlap["terminal_only"] += 1
            if item["required_by_schema"] and not terminal_required:
                required_overlap["schema_only"] += 1
            if generator_required:
                required_overlap["generator_marks_required"] += 1

            if required_any:
                if terminal_required:
                    required_class = "terminal_unconditional_candidate"
                elif conditional_required:
                    required_class = "conditional_semantic"
                else:
                    required_class = "other_required_mention"

                required_rows.append({
                    "definition": item["definition"],
                    "property": item["property"],
                    "path": item["path"],
                    "required_by_schema": item["required_by_schema"],
                    "terminal_required": terminal_required,
                    "conditional_required": conditional_required,
                    "generator_marks_required": generator_required,
                    "classification": required_class,
                    "recommended_phase": (
                        "STRUCTURAL_REVIEW"
                        if terminal_required and not item["required_by_schema"]
                        else "SEMANTIC_CONSTRAINTS"
                        if conditional_required
                        else "STRUCTURAL_ALREADY_COVERED"
                        if item["required_by_schema"]
                        else "DOCUMENTATION_ONLY_REVIEW"
                    ),
                    "description": description,
                })

            # ---------------------------------------------------------------
            # Allowed values
            # ---------------------------------------------------------------
            allowed_candidate = bool(ALLOWED_VALUES_CANDIDATE_RE.search(description))
            allowed_strong = bool(ALLOWED_VALUES_STRONG_RE.search(description))
            if allowed_candidate:
                counts["allowed_values_candidates"] += 1
                if allowed_strong:
                    counts["allowed_values_strong_candidates"] += 1
                extracted_values, extraction_error = safe_extract_values(processor, description)
                if extracted_values:
                    counts["allowed_values_recognized"] += 1
                    if allowed_strong:
                        counts["allowed_values_strong_recognized"] += 1
                    counts["allowed_values_extracted_values_total"] += len(extracted_values)
                else:
                    counts["allowed_values_missed"] += 1
                    if allowed_strong:
                        counts["allowed_values_strong_missed"] += 1

                allowed_rows.append({
                    "definition": item["definition"],
                    "property": item["property"],
                    "path": item["path"],
                    "schema_type": details.get("type", ""),
                    "schema_enum": details.get("enum", ""),
                    "strong_candidate": allowed_strong,
                    "extracted_values": extracted_values or [],
                    "extracted_count": len(extracted_values) if extracted_values else 0,
                    "generator_recognized": bool(extracted_values),
                    "extraction_error": extraction_error or "",
                    "description": description,
                })

            # ---------------------------------------------------------------
            # Deprecated
            # ---------------------------------------------------------------
            deprecated_candidate = bool(DEPRECATED_RE.search(description))
            if deprecated_candidate:
                counts["deprecated_candidates"] += 1
                recognized = generator_deprecated_recognized(description)
                if recognized:
                    counts["deprecated_recognized"] += 1
                else:
                    counts["deprecated_missed"] += 1
                deprecated_rows.append({
                    "definition": item["definition"],
                    "property": item["property"],
                    "path": item["path"],
                    "generator_recognized": recognized,
                    "description": description,
                })

            # ---------------------------------------------------------------
            # Mutual exclusion / semantic-only constraints
            # ---------------------------------------------------------------
            mutual_candidate = bool(MUTUAL_EXCLUSION_RE.search(description))
            if mutual_candidate:
                counts["mutual_exclusion_candidates"] += 1
                mutual_rows.append({
                    "definition": item["definition"],
                    "property": item["property"],
                    "path": item["path"],
                    "recommended_phase": "SEMANTIC_CONSTRAINTS",
                    "description": description,
                })

    # Derived metrics.
    def pct(num, den):
        return round(100.0 * num / den, 2) if den else None

    summary = {
        "properties_with_description": counts["properties_with_description"],
        "defaults": {
            "value_claim_candidates": counts["default_value_claim_candidates"],
            "by_default_context_mentions": counts["default_context_by_default"],
            "values_extracted": counts["default_values_extracted"],
            "value_claim_missed": counts["default_value_claim_missed"],
            "value_claim_extraction_coverage_pct": pct(
                counts["default_values_extracted"],
                counts["default_value_claim_candidates"],
            ),
        },
        "required": {
            **dict(required_overlap),
            "terminal_only_requires_manual_review": required_overlap["terminal_only"],
        },
        "allowed_values": {
            "broad_candidates": counts["allowed_values_candidates"],
            "broad_recognized": counts["allowed_values_recognized"],
            "broad_missed": counts["allowed_values_missed"],
            "broad_coverage_pct": pct(
                counts["allowed_values_recognized"],
                counts["allowed_values_candidates"],
            ),
            "strong_candidates": counts["allowed_values_strong_candidates"],
            "strong_recognized": counts["allowed_values_strong_recognized"],
            "strong_missed": counts["allowed_values_strong_missed"],
            "strong_coverage_pct": pct(
                counts["allowed_values_strong_recognized"],
                counts["allowed_values_strong_candidates"],
            ),
            "total_values_extracted": counts["allowed_values_extracted_values_total"],
        },
        "deprecated": {
            "candidates": counts["deprecated_candidates"],
            "recognized": counts["deprecated_recognized"],
            "missed": counts["deprecated_missed"],
            "coverage_pct": pct(
                counts["deprecated_recognized"],
                counts["deprecated_candidates"],
            ),
        },
        "mutual_exclusion": {
            "candidates": counts["mutual_exclusion_candidates"],
            "decision": "defer_to_semantic_constraint_phase",
        },
        "recommended_decisions": {
            "defaults": (
                "Keep as documentedDefault metadata; do not treat as JSON-Schema default."
            ),
            "required": (
                "Do not promote all 'required' mentions. Review only terminal Required. "
                "cases absent from schema required[]; conditional mentions belong to semantic constraints."
            ),
            "allowed_values": (
                "Retain description extraction only where schema has no multi-value enum; "
                "inspect missed candidates before changing regexes."
            ),
            "deprecated": (
                "Keep as metadata from description; broaden detector only if missed rows are genuine."
            ),
            "mutual_exclusion": (
                "Do not change structural FM; defer to semantic-constraint enrichment."
            ),
        },
    }

    tables = {
        "defaults": defaults_rows,
        "required": required_rows,
        "allowed_values": allowed_rows,
        "deprecated": deprecated_rows,
        "mutual_exclusions": mutual_rows,
    }
    return summary, tables


def write_summary_csv(path: Path, summary: Dict[str, Any]):
    rows = []

    def flatten(prefix: str, value: Any):
        if isinstance(value, dict):
            for k, v in value.items():
                flatten(f"{prefix}.{k}" if prefix else k, v)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            rows.append({"metric": prefix, "value": value})

    flatten("", summary)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["metric", "value"])
        writer.writeheader()
        writer.writerows(rows)


def print_summary(summary: Dict[str, Any]):
    print("\n=== Description semantics audit ===")
    print(f"Properties with description: {summary['properties_with_description']}")

    d = summary["defaults"]
    print("\n[Documented defaults]")
    print(f"  concrete-value claim candidates: {d['value_claim_candidates']}")
    print(f"  'by default' context mentions:   {d['by_default_context_mentions']}")
    print(f"  concrete values extracted:       {d['values_extracted']}")
    print(f"  value-claim misses:              {d['value_claim_missed']}")
    print(f"  extraction coverage:             {d['value_claim_extraction_coverage_pct']}%")

    r = summary["required"]
    print("\n[Required]")
    for key in (
        "schema_required", "terminal_required", "conditional_required",
        "schema_and_terminal", "terminal_only", "schema_only",
        "generator_marks_required",
    ):
        print(f"  {key:32s} {r.get(key, 0)}")

    a = summary["allowed_values"]
    print("\n[Allowed values]")
    print(f"  strong candidates:               {a['strong_candidates']}")
    print(f"  strong recognized:               {a['strong_recognized']}")
    print(f"  strong missed:                   {a['strong_missed']}")
    print(f"  strong coverage:                 {a['strong_coverage_pct']}%")
    print(f"  broad candidates:                {a['broad_candidates']}")
    print(f"  broad recognized:                {a['broad_recognized']}")
    print(f"  broad missed:                    {a['broad_missed']}")
    print(f"  broad coverage:                  {a['broad_coverage_pct']}%")
    print(f"  total values extracted:          {a['total_values_extracted']}")

    dep = summary["deprecated"]
    print("\n[Deprecated]")
    print(f"  candidates:                      {dep['candidates']}")
    print(f"  recognized:                      {dep['recognized']}")
    print(f"  missed:                          {dep['missed']}")
    print(f"  coverage:                        {dep['coverage_pct']}%")

    m = summary["mutual_exclusion"]
    print("\n[Mutual exclusion]")
    print(f"  candidates:                      {m['candidates']}")
    print("  action:                          defer to semantic-constraint phase")



def print_targeted_samples(tables: Dict[str, List[Dict[str, Any]]], limit: int = 8):
    terminal_only = [
        r for r in tables["required"]
        if r.get("terminal_required") and not r.get("required_by_schema")
    ]
    allowed_missed = [
        r for r in tables["allowed_values"]
        if r.get("strong_candidate") and not r.get("generator_recognized")
    ]
    deprecated_missed = [
        r for r in tables["deprecated"]
        if not r.get("generator_recognized")
    ]

    print("\\n[Targeted samples: terminal Required. outside schema required[]]")
    for row in terminal_only[:limit]:
        print(f"  - {row['path']}: {row['description'][:300]}")

    print("\\n[Targeted samples: strong allowed-value candidates missed]")
    for row in allowed_missed[:limit]:
        print(f"  - {row['path']}: {row['description'][:300]}")

    print("\\n[Targeted samples: deprecated mentions missed]")
    for row in deprecated_missed[:limit]:
        print(f"  - {row['path']}: {row['description'][:300]}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("definitions_json", type=Path)
    parser.add_argument(
        "--generator",
        type=Path,
        required=True,
        help="Generator .py containing SchemaProcessor. Required so coverage uses the exact implementation.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Default: <definitions_dir>/description_audit",
    )
    args = parser.parse_args()

    definitions_path = args.definitions_json.resolve()
    out_dir = (args.out_dir or definitions_path.parent / "description_audit").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with definitions_path.open("r", encoding="utf-8") as f:
        definitions = json.load(f)

    processor = load_generator_processor(args.generator, definitions)
    summary, tables = audit(definitions, processor)

    summary_json = out_dir / "description_audit_summary.json"
    summary_csv = out_dir / "description_audit_summary.csv"

    with summary_json.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    write_summary_csv(summary_csv, summary)

    rows_to_csv(
        out_dir / "defaults.csv",
        tables["defaults"],
        [
            "definition", "property", "path", "candidate_kind",
            "extracted_value", "generator_recognized", "description",
        ],
    )
    rows_to_csv(
        out_dir / "required.csv",
        tables["required"],
        [
            "definition", "property", "path", "required_by_schema",
            "terminal_required", "conditional_required", "generator_marks_required",
            "classification", "recommended_phase", "description",
        ],
    )
    rows_to_csv(
        out_dir / "allowed_values.csv",
        tables["allowed_values"],
        [
            "definition", "property", "path", "schema_type", "schema_enum", "strong_candidate",
            "extracted_values", "extracted_count", "generator_recognized",
            "extraction_error", "description",
        ],
    )
    rows_to_csv(
        out_dir / "deprecated.csv",
        tables["deprecated"],
        [
            "definition", "property", "path", "generator_recognized", "description",
        ],
    )
    rows_to_csv(
        out_dir / "mutual_exclusions.csv",
        tables["mutual_exclusions"],
        [
            "definition", "property", "path", "recommended_phase", "description",
        ],
    )

    print_summary(summary)
    print_targeted_samples(tables)
    print(f"\nOutput directory: {out_dir}")
    print(f"Summary JSON:     {summary_json}")
    print(f"Summary CSV:      {summary_csv}")


if __name__ == "__main__":
    main()
