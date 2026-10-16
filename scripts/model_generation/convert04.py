"""
@author: bfl699 @group: caosd
This script processes a Kubernetes JSON schema to generate a UVL (Universal Variability Language)
feature model. It extracts feature descriptions, values, constraints, and handles schema references.

Main Components:
- `SchemaProcessor`: Core class for parsing and transforming JSON schema definitions into UVL-compatible structures.
- Constraint extraction using `analisisScript01` integration.
- Value extraction from descriptions using regular expressions.
- Final UVL model output and constraints written to a `.uvl` file.

Usage:
    Simply run the script to transform the input JSON schema into a UVL model with extracted constraints.

Inputs:
    - A definitions JSON file with Kubernetes schema (e.g., _definitions.json)
Outputs:
    - A UVL file representing the variability model
    - A JSON file with parsed feature descriptions and schema metadata

Final revision 2026-10-08 (Kubernetes 1.37.1 structural-enriched baseline):
[M1] Preserve OpenAPI/Kubernetes metadata as UVL attributes without changing the feature hierarchy.
[M2] Preserve descriptions with minimal UVL-safe escaping instead of deleting punctuation.
[M3] Keep JSON-Schema defaults authoritative; use one compact extractor to distinguish documented from conditional prose defaults.
[M4] Treat singleton schema enums as fixedValue metadata; multi-value enums remain alternatives.
[M5] Distinguish arrays, structured objects, and dynamic maps for cardinality generation.
[M6] Map JSON Schema number to UVL Real instead of Integer.
[M7] Use minItems/maxItems and minProperties/maxProperties when cardinality is explicitly available.
[M8] Mandatory = schema required[] OR a terminal 'Required.' documentation statement, with provenance.
[M9] Preserve a structural-enriched baseline and generate semantic constraints only in a separate UVL variant.
[M10] Record raw schema metadata in the descriptions sidecar for later explanations/provenance.
[M11] Detect deprecation conservatively when the current field itself is explicitly marked deprecated.
[M12] Keep description-based allowed-value extraction conservative; relational 'one of field A or B'
      statements are deferred to the semantic-constraint phase.
[M13] Add a schema-derived serialization contract: configKey, configValue,
      serializationKind and serializationRole.
[M14] Generate a versioned serialization metadata sidecar directly from OpenAPI,
      instead of reconstructing YAML structure later from UVL text.
[M15] Restore `default` on an alternative only when an unconditional default value
      matches that alternative. Scalar defaults remain separate (`defaultValue`,
      `documentedDefault`, `conditionalDefault`).
[M16] Keep UVL serialization metadata compact: scalar/object + property are implicit;
      array/map/union and synthetic roles remain explicit. The sidecar keeps the full contract.
[M17] Render `doc` last for readability.
[M18] Model direct oneOf properties (e.g. IntOrString) as abstract grouping features;
      typed synthetic alternatives own configKey and typed defaults.
[M19] Use `defaultValue` + provenance for scalar defaults from descriptions; reserve
      bare `default` for the default alternative/representation.
[M20] Extend semantic-candidate capture minimally: classify "present only if" as a
      dependency and zero-value conditional phrases as restrictions; omission-context
      defaults remain defaults rather than semantic constraints.
[M21] Optionally move direct-union `doc` metadata from the abstract parent to each
      concrete `_asType` alternative for FlamaPy-driven rendering.
"""

import json
import re
import hashlib
from collections import deque

# [M9] Keep semantic enrichment separate from the structural/serialization baseline.
# Enable this only after the new baseline has been validated.
GENERATE_SEMANTIC_VARIANT = True

SYSTEM_NAME = "kubernetes"
SYSTEM_VERSION = "1.37.1"

# When enabled, a direct union property (e.g. IntOrString) keeps its parent
# as a structural/abstract variability node and copies the property's `doc`
# to each concrete `_asType` alternative. This makes the selected FlamaPy
# feature self-contained for configuration rendering.
MOVE_UNION_DOC_TO_ALTERNATIVES = True

# [M8] Kubernetes 1.37.1 audit showed genuine terminal "Required." statements
# that are absent from schema required[]. Only this narrow terminal form is promoted
# to mandatory; conditional "required when/if/..." prose remains semantic metadata.
INFER_TERMINAL_REQUIRED_FROM_DESCRIPTION = True

class SchemaProcessor:
    """
    Class responsible for parsing a Kubernetes JSON schema and converting it
    into a UVL feature model. It categorizes descriptions, resolves references,
    and extracts values and constraints.
    """
    def __init__(self, definitions):
        self.definitions = definitions # A dictionary that organizes descriptions into three categories:
        self.resolved_references = {}
        self.seen_references = set()
        self.seen_features = set() ## Add condition to viewed refs to avoid omitting already viewed refs
        self.processed_features = set()
        self.constraints = []  # List to store dependencies as constraints
        self.feature_aux_original_type = ""
        # A dictionary is initialized for storing descriptions per group
        self.descriptions = {
            'values': [],
            'restrictions': [],
            'dependencies': [],
            # [M10] Every processed property is recorded here, even if its description
            # does not match any heuristic category. This keeps provenance for feedback.
            'schema_metadata': []
        }
        self.is_cardinality = False
        self.is_deprecated = False
        self.seen_descriptions = set()

        # [M13/M14] Serialization information is captured while the OpenAPI node
        # is still available. This avoids reconstructing syntax later from UVL names.
        self.serialization_features = []
        self._last_extracted_config_values = {}

        # Patterns for classifying descriptions into categories of values, constraints and dependencies
        self.patterns = {
            'values': re.compile(r'^\b$', re.IGNORECASE), # values are|valid|supported|acceptable|can be
            'restrictions': re.compile(r'If the operator is|template.spec.restartPolicy|conditions may not be|Details about a waiting|TCPSocket is NOT|must be between|Note that this field cannot be set when|valid port number|must be in the range|must be greater than|are mutually exclusive properties|Must be set if type is|field MUST be empty if|must be non-empty if and only if|only if type|\. Required when|required when scope|\. At least one of|a least one of|Exactly one of|resource access request|datasetUUID is|succeededIndexes specifies|Represents the requirement on the container|ResourceClaim object in the same namespace as this pod|indicates which one of|may be non-empty only if|Minimum value is|Value must be non-negative|minimum valid value for|in the range 1-|can not be 0 if|cannot be 0 if|must not be 0 if', re.IGNORECASE),
            # [M20] Presence dependencies such as
            # "Present only if DeploymentStrategyType = RollingUpdate."
            # are kept separate from numeric/cross-value restrictions.
            'dependencies': re.compile(r'\bpresent only if\b', re.IGNORECASE)

        }

        # List of part names of features whose data type is changed to Boolean for compatibility with constraints and uvl. ### Those that are changed to add one more level to represent the String that is omitted when changing the type to Boolean.
        self.boolean_keywords = ['AppArmorProfile_localhostProfile', 'appArmorProfile_localhostProfile', 'seccompProfile_localhostProfile', 'SeccompProfile_localhostProfile', 'IngressClassList_items_spec_parameters_namespace',
                        'IngressClassParametersReference_namespace', 'IngressClassSpec_parameters_namespace', 'IngressClass_spec_parameters_namespace','_tolerations_value','_Toleration_value', '_clientConfig_url', '_WebhookClientConfig_url',
                        '_succeededIndexes', '_succeededCount', 'source_resourceClaimName', '_ClaimSource_resourceClaimName', '_resourceClaimTemplateName', '_datasetUUID', '_datasetName']  # Lista para modificar a otros posibles tipos de los features (Cambiado del original por la compatibilidad) ##
        # List of regular expressions for cases where the above list needs more precision to just alter the type in the required parameters
        self.boolean_keywords_regex = [r'.*_paramRef_name$', r'.*_ParamRef_name$']


        # Defining feature sections with specific configurations for compatibility with # os.name constraints
        self.special_features_config = [ '_template_spec_', '_Pod_spec_', '_PodList_items_spec_', '_core_v1_PodSpec_', '_PodTemplateSpec_spec_', '_v1_PodSecurityContext_'
                                       , '_v1_Container_securityContext_', '_v1_EphemeralContainer_securityContext_', '_v1_SecurityContext_']
        
        # Here you can add more special feature configurations

    def sanitize_name(self, name):
        """
        Sanitize a name by replacing problematic characters for UVL.

        Args:
            name (str): Raw input name.

        Returns:
            str: Cleaned name.
        """
        return name.replace("-", "_").replace(".", "_").replace("$", "")

    def sanitize_type_data(self, type_data):
        """Map JSON Schema scalar types to UVL feature types.

        [M5/M6] Arrays and objects are represented as Boolean structural features;
        cardinality is decided from the complete schema node in `parse_properties`,
        not from the type name alone. JSON Schema `number` maps to UVL `Real`.
        """
        if type_data in ['array', 'Object', 'object']:
            return 'Boolean'
        if type_data in ['number', 'Number']:
            return 'Real'
        if type_data in ['integer', 'Integer']:
            return 'Integer'
        if type_data in ['string', 'String']:
            return 'String'
        if type_data in ['boolean', 'Boolean']:
            return ''
        return type_data

    @staticmethod
    def clean_description(description):
        """Return a UVL STRING-safe version while preserving as much text as possible.

        [M2] UVL strings are delimited by single quotes. The previous implementation
        removed dots, quotes, braces and slashes, which discarded useful documentation.
        Here line breaks are escaped textually and the ASCII quote delimiter is replaced by a typographic apostrophe.
        """
        if description is None:
            return ''
        text = str(description).replace('\r\n', '\\n').replace('\n', '\\n').replace('\r', '\\n')
        text = text.replace("'", "’")
        # Avoid accidental tabs/newlines and keep the generated UVL on one physical line.
        text = re.sub(r'[\t ]+', ' ', text).strip()
        return text

    @staticmethod
    def _attribute_key(key):
        """Convert schema/extension keys into valid UVL attribute identifiers."""
        key = re.sub(r'[^A-Za-z0-9_]', '_', str(key))
        if not key or not key[0].isalpha():
            key = f"schema_{key}"
        return key

    def _uvl_attribute_value(self, value):
        """Serialize a Python value as a UVL attribute value.

        Lists/dicts are intentionally stored as compact JSON inside a UVL string. This
        avoids depending on vector support in older FlamaPy versions while preserving
        the complete metadata value.
        """
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, float):
            return repr(value)
        if value is None:
            return "'null'"
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        if isinstance(value, str) and value == '':
            # UVL STRING requires at least one character; keep a reversible textual marker.
            value = r'\u0000'
        return f"'{self.clean_description(value)}'"

    def format_uvl_attributes(self, attributes):
        """Format ordered `(key, value)` pairs as one UVL attribute block.

        For readability, ``doc`` is always rendered last because it is usually the
        longest attribute and otherwise hides the machine-actionable metadata.
        """
        ordered = [item for item in attributes if item[0] != 'doc']
        ordered.extend(item for item in attributes if item[0] == 'doc')

        rendered = []
        for key, value in ordered:
            key = self._attribute_key(key)
            # Legacy flag attributes (e.g. abstract/deprecated) are represented by None.
            if value is None:
                rendered.append(key)
            else:
                rendered.append(f"{key} {self._uvl_attribute_value(value)}")
        return f" {{{', '.join(rendered)}}}" if rendered else ''

    @staticmethod
    def is_dynamic_map_schema(details):
        """Identify OpenAPI maps represented by object + additionalProperties."""
        return (
            details.get('type') in ('object', 'Object')
            and isinstance(details.get('additionalProperties'), dict)
        )

    @staticmethod
    def _bounded_cardinality(min_value, max_value):
        """Return a UVL feature-cardinality string `[min..max]`."""
        minimum = 0 if min_value is None else int(min_value)
        maximum = '*' if max_value is None else int(max_value)
        return f"[{minimum}..{maximum}]"

    def cardinality_for_schema(self, details):
        """Infer cardinality only for arrays and dynamic maps.

        [M5/M7] A structured object is not a repeated feature. Arrays use
        minItems/maxItems, while dynamic maps use minProperties/maxProperties.
        """
        if details.get('type') == 'array':
            return self._bounded_cardinality(details.get('minItems'), details.get('maxItems'))
        if self.is_dynamic_map_schema(details):
            return self._bounded_cardinality(details.get('minProperties'), details.get('maxProperties'))
        return None

    @staticmethod
    def strip_cardinality(feature_name):
        """Remove any UVL feature-cardinality suffix from a feature reference. [M7]"""
        return re.sub(r'\s+cardinality\s+\[[^\]]+\]', '', feature_name)

    @staticmethod
    def _enum_feature_token(raw_value):
        """Return a stable UVL-safe token while preserving the raw config value separately."""
        if raw_value is None:
            token = 'null'
        elif isinstance(raw_value, bool):
            token = 'true' if raw_value else 'false'
        else:
            token = str(raw_value)
        token = token.replace('*', 'estrella')
        return re.sub(r'[^A-Za-z0-9_#§%?\\]', '_', token).strip('_') or 'empty'

    def infer_serialization_kind(self, details, _seen_refs=None):
        """Infer how a schema node is serialized in YAML."""
        if not isinstance(details, dict):
            return 'unknown'

        if self.is_dynamic_map_schema(details):
            return 'map'

        schema_type = details.get('type')
        if schema_type == 'array':
            return 'array'
        if schema_type in ('object', 'Object') or isinstance(details.get('properties'), dict):
            return 'object'
        if schema_type in ('string', 'integer', 'number', 'boolean'):
            return 'scalar'
        if isinstance(details.get('oneOf'), list):
            return 'union'

        ref = details.get('$ref')
        if ref:
            if _seen_refs is None:
                _seen_refs = set()
            if ref in _seen_refs:
                return 'unknown'
            _seen_refs = set(_seen_refs)
            _seen_refs.add(ref)
            ref_schema = self.resolve_reference(ref)
            if ref_schema:
                return self.infer_serialization_kind(ref_schema, _seen_refs)

        return 'unknown'

    def serialization_attributes(
        self,
        *,
        config_key=None,
        serialization_kind=None,
        serialization_role=None,
        config_value_marker=False,
        config_value=None,
        config_value_source=None,
    ):
        """Return only serialization attributes that add information in the UVL.

        The sidecar still keeps the complete serialization contract.

        UVL rules:
        - ``configKey`` is kept for real YAML properties.
        - ``serializationKind`` is emitted only for non-trivial shapes:
          ``array``, ``map`` and ``union``.
        - ``serializationRole`` is emitted only for concrete synthetic roles
          (value, mapKey, mapValue, arrayItem). ``property`` and ``structural``
          are implicit in the UVL; the sidecar still stores them explicitly.
        """
        attrs = []

        if config_key is not None:
            attrs.append(('configKey', config_key))

        if serialization_kind in {'array', 'map', 'union'}:
            attrs.append(('serializationKind', serialization_kind))

        if serialization_role and serialization_role not in {'property', 'structural'}:
            attrs.append(('serializationRole', serialization_role))

        if config_value_marker:
            attrs.append(('configValue', config_value))
            if config_value_source:
                attrs.append(('configValueSource', config_value_source))

        return attrs

    def record_serialization_metadata(
        self,
        feature_name,
        *,
        config_path,
        serialization_kind,
        serialization_role,
        config_key=None,
        config_value_marker=False,
        config_value=None,
        config_value_source=None,
        required_source=None,
        synthetic=False,
        details=None,
        default_info=None,
        default_variant=False,
    ):
        """Record machine-actionable YAML rendering metadata for one feature."""
        clean_feature_name = self.strip_cardinality(feature_name)
        entry = {
            'feature': clean_feature_name,
            'configPath': config_path,
            'serializationKind': serialization_kind,
            'serializationRole': serialization_role,
            'synthetic': bool(synthetic),
        }

        if config_key is not None:
            entry['configKey'] = config_key
        if config_value_marker:
            entry['configValue'] = config_value
            if config_value_source:
                entry['configValueSource'] = config_value_source
        if required_source:
            entry['requiredSource'] = required_source
        if default_variant:
            entry['defaultVariant'] = True

        if default_info is not None:
            if default_info.get('semantics') == 'conditional':
                entry['conditionalDefault'] = default_info.get('value')
                entry['conditionalDefaultSource'] = default_info.get('source')
            else:
                entry['defaultValue'] = default_info.get('value')
                entry['defaultValueSource'] = default_info.get('source')

        if isinstance(details, dict):
            if details.get('type') is not None:
                entry['schemaType'] = details.get('type')
            if details.get('$ref') is not None:
                entry['schemaRef'] = details.get('$ref')
            if isinstance(details.get('items'), dict):
                if details['items'].get('type') is not None:
                    entry['itemType'] = details['items'].get('type')
                if details['items'].get('$ref') is not None:
                    entry['itemRef'] = details['items'].get('$ref')
            if isinstance(details.get('additionalProperties'), dict):
                additional = details['additionalProperties']
                if additional.get('type') is not None:
                    entry['additionalPropertiesType'] = additional.get('type')
                if additional.get('$ref') is not None:
                    entry['additionalPropertiesRef'] = additional.get('$ref')
            if 'default' in details:
                entry['defaultValue'] = details['default']
                entry['defaultValueSource'] = 'schema'
            if isinstance(details.get('enum'), list) and len(details['enum']) == 1:
                entry['fixedValue'] = details['enum'][0]
                if 'configValue' not in entry:
                    entry['configValue'] = details['enum'][0]
                    entry['configValueSource'] = 'schema.enum'

        self.serialization_features.append(entry)

    def save_serialization_metadata(
        self,
        file_path,
        *,
        schema_source,
        schema_sha256,
        system=SYSTEM_NAME,
        version=SYSTEM_VERSION,
    ):
        """Save the schema-derived serialization contract as a separate artifact."""
        payload = {
            'system': system,
            'version': version,
            'schemaSource': schema_source,
            'schemaSha256': schema_sha256,
            'featureCount': len(self.serialization_features),
            'features': self.serialization_features,
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
        print(f"Serialization metadata saved to {file_path}")

    def extract_documented_default(self, description):
        """Extract and classify a default documented in natural-language prose.

        Returns a compact record with the extracted value, provenance and a type
        hint. The type hint is only used to place defaults on the correct branch of
        unions such as IntOrString.

        Important: conditions are inspected only in the same sentence as the
        default statement. This prevents text such as
        ``Defaults to 25%. Example: when ...`` from being misclassified as a
        conditional default.
        """
        if not description:
            return None

        prefix = r'(?:defaults? to|default value is|default is|implicitly inferred to be)'
        patterns = [
            # Quoted values are strings even if they look numeric.
            (re.compile(prefix + r'\s+["\'`](.*?)["\'`]', re.IGNORECASE), 'string'),
            # Percentage syntax is serialized as String by IntOrString.
            (re.compile(prefix + r'\s+([-+]?\d+(?:\.\d+)?%)', re.IGNORECASE), 'string'),
            (re.compile(prefix + r'\s+([-+]?\d+)\b', re.IGNORECASE), 'integer'),
            (re.compile(prefix + r'\s+([-+]?\d+\.\d+)\b', re.IGNORECASE), 'number'),
            (re.compile(prefix + r'\s+(true|false)\b', re.IGNORECASE), 'boolean'),
            (re.compile(prefix + r'\s+([A-Za-z0-9_*/%+:-]+)', re.IGNORECASE), 'string'),
        ]

        for pattern, value_type_hint in patterns:
            match = pattern.search(description)
            if not match:
                continue

            value = match.group(1).strip()

            # Only the remainder of the current sentence can qualify this default.
            tail = description[match.end():]
            sentence_tail = re.split(r'[.\n]', tail, maxsplit=1)[0]

            # "if/when unspecified|unset|omitted|..." merely states when a default
            # is applied; it is not a cross-feature semantic condition.
            omission_context = bool(re.search(
                r'\b(?:if|when)\s+(?:unset|omitted|unspecified|not specified|'
                r'not set|absent)\b',
                sentence_tail,
                re.IGNORECASE,
            ))

            has_strong_condition = bool(re.search(
                r'\b(?:unless|otherwise|depending on|depending upon|'
                r'implementation-defined|implementation defined)\b',
                sentence_tail,
                re.IGNORECASE,
            ))

            has_if_when = bool(re.search(
                r'\b(?:if|when)\b',
                sentence_tail,
                re.IGNORECASE,
            ))

            is_conditional = has_strong_condition or (has_if_when and not omission_context)

            return {
                'value': value,
                'semantics': 'conditional' if is_conditional else 'documented',
                'source': 'description',
                'valueTypeHint': value_type_hint,
            }

        return None

    @staticmethod
    def coerce_default_value(value, schema_type):
        """Convert a textual documented default to the target scalar UVL value."""
        if value is None:
            return None

        if schema_type == 'integer':
            try:
                return int(str(value))
            except (TypeError, ValueError):
                return value

        if schema_type == 'number':
            try:
                return float(str(value))
            except (TypeError, ValueError):
                return value

        if schema_type == 'boolean':
            if isinstance(value, bool):
                return value
            lower = str(value).strip().lower()
            if lower == 'true':
                return True
            if lower == 'false':
                return False

        return str(value)

    @staticmethod
    def union_default_type(default_info, one_of):
        """Return the oneOf schema type that should receive the default."""
        if not default_info or default_info.get('semantics') != 'documented':
            return None

        available = [
            option.get('type')
            for option in one_of
            if isinstance(option, dict) and option.get('type')
        ]
        hint = default_info.get('valueTypeHint')

        if hint in available:
            return hint

        # `number` defaults may still fit a Real branch if present.
        if hint == 'integer' and 'number' in available and 'integer' not in available:
            return 'number'

        # Conservative fallback for textual values.
        if 'string' in available and hint == 'string':
            return 'string'

        return None

    def schema_attributes(
        self,
        details,
        description='',
        abstract=False,
        deprecated=False,
        enum_source=None,
        config_key=None,
        serialization_kind=None,
        serialization_role=None,
        include_description_default=True,
    ):
        """Collect OpenAPI/Kubernetes facts as UVL attributes.

        [M1/M3/M10] These attributes enrich the FM for future explanations without
        changing its hierarchy. All x-kubernetes-* extensions are preserved generically.
        """
        attrs = []
        if abstract:
            attrs.append(('abstract', None))
        if deprecated or details.get('deprecated') is True:
            attrs.append(('deprecated', None))

        if description:
            attrs.append(('doc', self.clean_description(description)))

        # [M13] Minimal serialization contract embedded directly in the FM.
        attrs.extend(self.serialization_attributes(
            config_key=config_key,
            serialization_kind=serialization_kind,
            serialization_role=serialization_role,
        ))

        # Authoritative JSON Schema facts useful for validation/explanation.
        direct_keys = (
            'type', 'format', 'pattern', 'minimum', 'maximum', 'exclusiveMinimum',
            'exclusiveMaximum', 'multipleOf', 'minLength', 'maxLength', 'minItems',
            'maxItems', 'uniqueItems', 'minProperties', 'maxProperties', 'nullable',
            'readOnly', 'writeOnly'
        )
        for key in direct_keys:
            if key in details:
                attrs.append((f'schema_{key}', details[key]))

        if 'default' in details:
            # [M15] `default` is reserved for the default alternative in UVL.
            # A scalar/configuration default is a different concept.
            attrs.append(('defaultValue', details['default']))
            attrs.append(('defaultValueSource', 'schema'))

        # Additional provenance that is useful for explanations but does not alter
        # the feature hierarchy.
        if '$ref' in details:
            attrs.append(('schemaRef', details['$ref']))
        if isinstance(details.get('required'), list) and details['required']:
            attrs.append(('schemaRequiredFields', details['required']))
        if isinstance(details.get('items'), dict):
            if 'type' in details['items']:
                attrs.append(('schemaItemsType', details['items']['type']))
            if '$ref' in details['items']:
                attrs.append(('schemaItemsRef', details['items']['$ref']))
        if isinstance(details.get('additionalProperties'), dict):
            additional = details['additionalProperties']
            if 'type' in additional:
                attrs.append(('schemaAdditionalPropertiesType', additional['type']))
            if '$ref' in additional:
                attrs.append(('schemaAdditionalPropertiesRef', additional['$ref']))
        for combinator in ('oneOf', 'anyOf', 'allOf'):
            if isinstance(details.get(combinator), list):
                attrs.append((f'schema{combinator[0].upper()}{combinator[1:]}Count', len(details[combinator])))
                if combinator == 'oneOf':
                    one_of_types = [
                        option.get('type')
                        for option in details[combinator]
                        if isinstance(option, dict) and option.get('type') is not None
                    ]
                    if len(one_of_types) == len(details[combinator]):
                        attrs.append(('schemaOneOfTypes', one_of_types))

        default_info = self.extract_documented_default(description)
        if include_description_default and default_info is not None:
            if default_info['semantics'] == 'conditional':
                attrs.append(('conditionalDefault', default_info['value']))
                attrs.append(('conditionalDefaultSource', 'description'))
            elif 'default' not in details:
                # `defaultValue` is the scalar/configuration default. Provenance
                # tells us that Kubernetes documented it in the description.
                attrs.append((
                    'defaultValue',
                    self.coerce_default_value(default_info['value'], details.get('type'))
                ))
                attrs.append(('defaultValueSource', 'description'))

        if 'enum' in details and details['enum'] is not None:
            attrs.append(('schemaEnumValues', details['enum']))
            attrs.append(('enumSource', enum_source or 'schema'))

            # [M4] In Kubernetes 1.37.1 all actual enum keywords are singleton `kind`
            # declarations. A singleton enum is a fixed admissible value, not a default.
            if isinstance(details['enum'], list) and len(details['enum']) == 1:
                attrs.append(('fixedValue', details['enum'][0]))
                attrs.append(('fixedValueSource', 'schema.enum'))
                attrs.append(('configValue', details['enum'][0]))
                attrs.append(('configValueSource', 'schema.enum'))
        elif enum_source:
            attrs.append(('enumSource', enum_source))

        # Preserve every Kubernetes/OpenAPI extension automatically, including future ones.
        for key, value in details.items():
            if key.startswith('x-kubernetes-'):
                attrs.append((key, value))

        # Convenience attributes for the resource identity. The original extension
        # is still kept above, so no information is lost when multiple GVKs exist.
        gvks = details.get('x-kubernetes-group-version-kind')
        if isinstance(gvks, list) and gvks:
            attrs.append(('k8sGVK', gvks))
            if len(gvks) == 1 and isinstance(gvks[0], dict):
                attrs.append(('k8sGroup', gvks[0].get('group', '')))
                attrs.append(('k8sVersion', gvks[0].get('version', '')))
                attrs.append(('k8sKind', gvks[0].get('kind', '')))

        return attrs

    def record_schema_metadata(
        self,
        feature_name,
        details,
        description,
        feature_type,
        required_source=None,
    ):
        """Store a compact provenance record in the sidecar JSON. [M10]"""
        meta = {
            'feature_name': feature_name,
            'feature_type': feature_type,
            'schema_type': details.get('type'),
            'description': description,
            'mandatory_in_fm': feature_type == 'mandatory',
            'required_by_schema': required_source in ('schema', 'schema+description'),
            'required_by_description': required_source in ('description', 'schema+description'),
        }
        if required_source:
            meta['required_source'] = required_source

        default_info = self.extract_documented_default(description)
        if default_info is not None:
            meta['documented_default'] = {
                'value': default_info['value'],
                'semantics': default_info['semantics'],
                'source': default_info['source'],
            }

        for key in ('default', 'enum', 'format', 'pattern', 'minimum', 'maximum',
                    'minItems', 'maxItems', 'minProperties', 'maxProperties',
                    'uniqueItems', 'nullable', 'deprecated'):
            if key in details:
                meta[key] = details[key]
        x_kubernetes = {k: v for k, v in details.items() if k.startswith('x-kubernetes-')}
        if x_kubernetes:
            meta['x_kubernetes'] = x_kubernetes

        # Keep a near-lossless view of non-hierarchical schema keywords. `properties`
        # is excluded to avoid recursively duplicating the complete schema tree.
        meta['schema_keywords'] = {
            k: v for k, v in details.items()
            if k not in ('properties', 'description')
        }
        self.descriptions['schema_metadata'].append(meta)

    def enum_values_from_schema(self, details):
        """Return UVL-safe enum child names from JSON Schema `enum`. [M4]"""
        values = details.get('enum')
        if not isinstance(values, list) or len(values) < 2:
            return None
        default_present = 'default' in details
        default_value = details.get('default')
        result = []
        seen = set()
        for raw_value in values:
            token = self._enum_feature_token(raw_value)
            if token in seen:
                continue
            seen.add(token)
            if default_present and raw_value == default_value:
                token = f"{token} {{default}}"
            result.append(token)
        return result if len(result) >= 2 else None

    def enum_config_value_map(self, details):
        """Map UVL enum tokens to the original serialized schema values."""
        values = details.get('enum')
        if not isinstance(values, list):
            return {}
        result = {}
        for raw_value in values:
            result[self._enum_feature_token(raw_value)] = raw_value
        return result

    def resolve_reference(self, ref):
        """
        Resolve a JSON Schema reference from the definitions.

        Args:
            ref (str): A reference path like "#/definitions/SomeType".

        Returns:
            dict or None: The resolved object or None if not found.
        """

        if ref in self.resolved_references: # Check whether the reference has already been solved.
            return self.resolved_references[ref]

        parts = ref.strip('#/').split('/') # The reference is divided into parts
        schema = self.definitions

        try:
            for part in parts: # The parts of the reference are traversed to find the scheme
                schema = schema.get(part, {})
                if not schema:
                    print(f"Warning: Not could be posible resolve the reference: {ref}") # Used to check if there is a reference that is lost and not processed.
                    return None

            self.resolved_references[ref] = schema
            return schema
        except Exception as e:
            print("Error when resolve the reference: {ref}: {e}")
            return None

    def is_valid_description(self, feature_name, description):
        """
        Check if a description is valid (not too short and without repetitions) and then analyze it for restrictions.

        Args:
            feature_name (str): The feature name of the property
            description: The description of the feature related

        Returns:
            True or False: Depends of validation description
        """
        if len(description) < 10:
            print(description)
            return False
        # Create a unique key by combining the feature name and description
        description_key = f"{feature_name}:{description}"
        if description_key in self.seen_descriptions:
            return False
        self.seen_descriptions.add(description_key)
        return True

    # Final Required policy: preserve the original explicit terminal marker
    # while recording provenance in the UVL attributes and sidecar metadata.
    def is_required_based_on_description(self, description):
        """Return True only for an unconditional terminal ``Required.`` statement.

        [M8] This deliberately does NOT match conditional wording such as
        ``Required when ...`` or relational statements such as ``Exactly one of ...``.
        """
        if not description:
            return False

        # Keep the deliberately narrow convention used by the original generator:
        # only the explicit terminal marker "Required." (capital R) is structural.
        # This avoids promoting relational prose such as
        # "Exactly one of url or service is required." to a mandatory field.
        return description.rstrip().endswith("Required.")

    def required_source(self, prop, current_required, description):
        """Return provenance for a mandatory feature, or None when optional."""
        by_schema = prop in current_required
        by_description = (
            INFER_TERMINAL_REQUIRED_FROM_DESCRIPTION
            and self.is_required_based_on_description(description)
        )
        if by_schema and by_description:
            return 'schema+description'
        if by_schema:
            return 'schema'
        if by_description:
            return 'description'
        return None

    def is_deprecated_description(self, prop, description):
        """Conservatively detect deprecation of the *current* field.

        [M11] A bare occurrence of the word ``deprecated`` is insufficient because
        descriptions may discuss another deprecated field/type. We accept explicit
        ``Deprecated:`` markers, ``this field is deprecated``, or a statement that
        names the current property and marks it deprecated.
        """
        if not description:
            return False

        if re.search(r'\bDeprecated\s*:', description, re.IGNORECASE):
            return True
        if re.search(r'\bthis field is deprecated\b', description, re.IGNORECASE):
            return True

        prop_pattern = re.escape(str(prop))
        if re.search(
            rf'\b{prop_pattern}\b[^.\n]{{0,120}}\bis deprecated\b',
            description,
            re.IGNORECASE,
        ):
            return True

        return False

    def extract_values(self, description):
        """
        Extract a list of valid values from a feature description.

        Args:
            description (str): The feature description.

        Returns:
            list or None: Extracted values or None.
        """

        self._last_extracted_config_values = {}

        palabras_patrones_minus = ['values are', 'following states', '. must be', 'implicitly inferred to be', 'the currently supported reasons are', '. can be', 'it can be in any of following states',
                                   'valid options are', 'a value of `', 'the supported types are', 'valid operators are', 'status of the condition,', 'status of the condition.',
                                    'type of the condition.', 'status of the condition (', 'node address type', 'should be one of', 'will be one of', 'means that requests that', 'only valid values',
                                    'a volume should be', 'the metric type is', 'valid policies are']
        ## . must be causes many aggregations of a single value since there are several constraints that coincide with this expression... define better in the future if unit values are necessary.
        ## Patterns that have been removed as ‘repetitive’: , 'possible values are', , 'the currently supported values are', 'expected values are'
        palabras_patrones_may = ['Supports', 'Type of job condition', 'Status of the condition for', 'Type of condition', '. One of', 'Host Caching mode', 'This may be set to', 'Supported values:',
                                'completions are tracked. It can be', 'Services can be', 'this API group are'] ## 'values are', ## Type pendiente de sumar Healthy
        
        if not any(keyword in description.lower() for keyword in palabras_patrones_minus) and not any(keyword in description for keyword in palabras_patrones_may): # , '. Must be' , 'allowed valures are'
            return None

        value_patterns = [

            # Captures values between escaped or unescaped quotation marks
            re.compile(r'\\?["\'](.*?)\\?["\']'), ## Ex: A value of `\"Exempt\"`...

            re.compile(r'-\s*[\'"]?([a-zA-Z/.\s]+[a-zA-Z])[\'"]?\s*:', re.IGNORECASE), # Pattern that captures values preceded by a hyphen and ending with a colon: # Expression to be modified in the future to avoid capturing "prefixed_keys" (captures long phrases without being displayed but...)
            
            re.compile(r'(?<=Valid values are:)[\s\S]*?(?=\.)'),
            re.compile(r'(?<=Possible values are:)[\s\S]*?(?=\.)'),
            re.compile(r'(?<=Allowed values are)[\s\S]*?(?=\.|\s+Required)', re.IGNORECASE),

            re.compile(r'\b(UDP.*?SCTP)\b'),
            re.compile(r'\n\s*-\s+(\w+)\s*\n', re.IGNORECASE), ## single case Infeasible, Pending...
            re.compile(r'\b(Localhost|RuntimeDefault|Unconfined)\b'), ### Valid options are:
            re.compile(r'\b(Retain|Delete|Recycle)\b'),
            re.compile(r'(?<=The currently supported values are\s)([a-zA-Z\s,]+)(?=\.)', re.IGNORECASE),

            re.compile(r'(?<=Valid operators are\s)([A-Za-z\s,]+)(?=\.)', re.IGNORECASE),
            re.compile(r'\b(Gt|Lt)\b'),

            re.compile(r'(?<=Acceptable values are:)([A-Za-z\s,]+)(?=\()'), ### Group to add the values of "Acceptable values are:"
            
            re.compile(r'(?<=status of the condition, one of\s)([a-zA-Z\s,]+)(?=\.)', re.IGNORECASE), ## True, False, Unknown, expr: 'status of the condition,'
            re.compile(r'(?<=Type of job condition,\s)([a-zA-Z\s,]+)(?=\.)'), ## Complete or Failed, expr: 'Type of job condition'
            ### status of the condition. Can be (7)
            re.compile(r'(?<=status of the condition. Can be\s)([a-zA-Z\s,]+)(?=\.)'), ## Variant of the previous pattern: Can be True, False, Unknown.. expr arriba: 'status of the condition.'
            ### Valid value: \"Healthy\" I have omitted the result of values with only 1 value but this one defines that it has only one possible option...
            re.compile(r'(?<=Types include\s)([a-zA-Z\s,]+)(?=\.)'), ## Pattern for a single description: Established, NamesAccepted and Terminating 'type of the condition.' (2)
            
            re.compile(r'(?<=status of the condition \()([a-zA-Z\s,]+)(?=\))'), ## unique case of values (1 descr): (True, False, Unknown), expr: 'status of the condition (' (1)
            re.compile(r'(?<=Node address type, one of\s)([a-zA-Z\s,]+)(?=\.)'), ## Pattern for a description: Hostname, ExternalIP or InternalIP 'node address type' (1)
            re.compile(r'(?<=. One of\s)([a-zA-Z\s,]+)(?=\.)'), ## Pattern for a description: [Always, Never, IfNotPresent], Never, PreemptLowerPriority, [Always, OnFailure, Never], \"Success\" or \"Failure\" '. One of' (6)
            re.compile(r'(?<=Host Caching mode:\s)([a-zA-Z\s,]+)(?=\.)'),
            re.compile(r'(?<=Supported values:\s)([a-zA-Z\s,]+)(?=\.)'), # Supported values: cpu, memory. (87,87)
            
            re.compile(r'\b(Shared|Dedicated|Managed)\b'),
            re.compile(r'(?<=a volume should be\s)([a-zA-Z\s,]+)(?=\.)'), ## for a volume should be ThickProvisioned or ThinProvisioned. (38,38)
            re.compile(r'\b(NonIndexed|Indexed)\b'), # completions are tracked. It can be `NonIndexed` (default) or `Indexed`. (7,7) ## re.compile(r'are tracked\.\s*It can be\s*`([^`]*)`')
                    
            re.compile(r'(?<=the metric type is\s)([a-zA-Z\s,]+)'), ## the metric type is Utilization, Value, or AverageValue", (26,26,26)
            # 
            re.compile(r'(?<=Valid policies are\s)([a-zA-Z\s,]+)(?=\.)') ## Valid policies are IfHealthyBudget and AlwaysAllow. (3,3)

            ## Other values added by the general regex: Services can be (3,3 ,3)
            #re.compile(r'(?<=It can be\s)`([a-zA-Z\s,]+)`(?=\.)'),
            # Valid policies are
            #re.compile(r'(?<=kind expected values are\s)([A-Za-z]+)(?=[:,]|$)'),
            ## Host Caching mode
            ## Expressions aggregated directly by generic patterns "[$value]":... 'should be one of', 'will be one of': \"ContainerResource\", \"External\", \"Object\", \"Pods\" or \"Resource\", 'only valid values': 'Apply' and 'Update'
            ##. One of
            ## Node address type, one of 
            ## status of the condition (
            ## Types include
        ]

        values = []
        # [M15] Prose defaults remain metadata on the parameter, but an
        # unconditional prose default may also identify the default alternative.
        for pattern in value_patterns:
            matches = pattern.findall(description)
            for match in matches:
                split_values = re.split(r',\s*|\s+or\s+|\sor|or\s|\s+and\s+|and\s', match)  # Make sure that "or" is surrounded by spaces.
                for v in split_values:
                    raw_config_value = v.strip()
                    raw_config_value = raw_config_value.replace('"', '').replace("'", '').replace('`', '')
                    raw_config_value = raw_config_value.strip()

                    v = raw_config_value.replace('*', 'estrella') # Replace '*' for "estrella", * invalid in uvl
                    v = v.replace(' ', '_').replace('/', '_')

                    # Filter values that contain periods, square brackets, braces or are too long
                    if v and len(v) <= 24 and not any(char in v for char in {'.', '{', '}', '[', ']',';', ':', 'prefixed_keys'}): # added / due to syntax problems 'yet', ## Added prefixed_keys, handled to remove, are not values
                        if len(v) >= 20 and '_' in v:
                        # Exclude values with underscore and size >= 20
                            print(f"Excluding values: {v}")
                        else:
                        # Add the value if it does not have underscore or if it is less than 20 characters
                            values.append(v)
                            self._last_extracted_config_values[v] = raw_config_value

        case_not_none = ['NodePort', 'ClusterIP', 'None', 'LoadBalancer', 'ExternalName'] ## List where None was added and was not part of the possible value set
        case_not_policies = {'IfHealthyBudget', 'AlwaysAllow', 'Ready', 'True', 'Running'} ## Set to avoid defining another list and using set(). Check if something goes wrong
        case_not_none = set(case_not_none) # Get list regardless of order
        values = set(values)  # Remove duplicates

        if not values or len(values) == 1:
            return None
        
        if case_not_none == values: ## We want to omit "type_None" in the model.
            values.remove('None')
        elif case_not_policies == values: ## If there are more cases generalize the functionality to an auxiliary with the parameters
            list_policies_to_delete = {'Ready', 'True', 'Running'} ## Set of elements to be deleted from the values. They are added by the general regex "/"/
            values = case_not_policies - list_policies_to_delete
        return values #, add_quotes  # Returns the values and name of the feature

    # Legacy prose-default parser removed; `extract_documented_default` is the single source.

    def categorize_description(self, description, feature_name, type_data):
        """
        Categorize a feature description into values, restrictions, or dependencies.

        Args:
            description (str): Natural language description.
            feature_name (str): The full name of the feature.
            type_data (str): Type of the feature (e.g., String, Boolean).

        Returns:
            bool: True if the description matched a category.
        """

        if not self.is_valid_description(feature_name, description):
            return False

        if type_data == '':
            type_data = 'Boolean'
        
        feature_name_descriptions = ""
        if "cardinality" in feature_name:
            feature_name_descriptions = feature_name.split(" cardinality")[0]
        elif "{" in feature_name:
            feature_name_descriptions = feature_name.split(" {")[0]
        else:
            feature_name_descriptions = feature_name

        # Description input with type data to improve the accuracy of the rules
        description_entry = {
        "feature_name": feature_name_descriptions,
        "description": description,
        "type_data":type_data  # Type addition to have the data type for the constraints.
    }
        for category, pattern in self.patterns.items():
            if pattern.search(description):
                self.descriptions[category].append((description_entry))
                return True
        
        return False

    def process_oneOf(
        self,
        oneOf,
        full_name,
        type_feature,
        serialization_role='value',
        config_path='',
        config_key=None,
        default_info=None,
        inherited_doc=None,
    ):
        """
        Process a JSON Schema `oneOf` field and generate subfeatures based on type alternatives.

        This function is used to capture type alternatives (e.g., String vs Integer) in UVL modeling
        by appending `_asType` subfeatures.

        Args:
            oneOf (list): List of schema type alternatives.
            full_name (str): The name of the parent feature.
            type_feature (str): The feature type (e.g., optional, alternative).

        Returns:
            dict: A dictionary representing the main feature and its typed subfeatures.
        """

        feature = {
            'name': full_name,
            'type': type_feature,  # We set it as 'optional' since it can be one of several types of 'optional'.
            'description': f"Feature based on oneOf in {full_name}",    
            'sub_features': [],
            'type_data': 'Boolean'  # Here we define the type (e.g.: String, Number)
        }

        default_union_type = self.union_default_type(default_info, oneOf)

        # Process each option within 'oneOf'
        for option in oneOf:
            if 'type' in option:
                option_type_data = option['type'].capitalize()  # Capture type (e.g. string, number, integer)
                sanitized_name = self.strip_cardinality(full_name) ## Addendum to remove cardinality from name inheritance

                if ' {default ' in sanitized_name: ## Part added to avoid adding the {default X} as part of the name for some sub-features generating an error: feature_name_{default X}_asType
                    sanitized_name = re.sub(r'\s*\{.*?\}', '', sanitized_name) # All content inside the square brackets and the space ## sanitized_name = re.sub(r'\s* "default", ‘’, sanitized_name) is deleted
                # Create subfeature with appropriate name.
                # For direct union properties we can move/copy the semantic
                # description to the selected concrete representation so that
                # FlamaPy does not need to inspect the abstract parent.
                if MOVE_UNION_DOC_TO_ALTERNATIVES and inherited_doc:
                    aux_description_sub_feature = inherited_doc
                else:
                    aux_description_sub_feature = f"Sub-feature added of type {option_type_data}"

                sub_name = f"{sanitized_name}_as{option_type_data}"
                is_default_branch = (
                    default_union_type is not None
                    and option.get('type') == default_union_type
                )

                sub_attrs = []
                if is_default_branch:
                    sub_attrs.append(('default', None))
                if config_key is not None:
                    sub_attrs.append(('configKey', config_key))
                if is_default_branch:
                    sub_attrs.append((
                        'defaultValue',
                        self.coerce_default_value(
                            default_info['value'],
                            option.get('type')
                        )
                    ))
                    sub_attrs.append(('defaultValueSource', default_info['source']))

                sub_attrs.extend([
                    ('serializationRole', serialization_role),
                    ('doc', aux_description_sub_feature),
                ])
                sub_feature = {
                    'name': sub_name,
                    'type': 'alternative',  # Exactly one representation of the union value.
                    'description': aux_description_sub_feature,
                    'sub_features': [],
                    'type_data': self.sanitize_type_data(option_type_data),
                    'attributes': sub_attrs,
                    'attribute_text': self.format_uvl_attributes(sub_attrs),
                }

                self.record_serialization_metadata(
                    sub_name,
                    config_path=config_path,
                    config_key=config_key,
                    serialization_kind='scalar',
                    serialization_role=serialization_role,
                    synthetic=True,
                    details=option,
                    default_info=default_info if is_default_branch else None,
                    default_variant=is_default_branch,
                )

                # Add the subfeature to the list of sub_features of the main feature
                feature['sub_features'].append(sub_feature)

        return feature
    
    # [M3/M4] Legacy `process_enum_defaultInte` removed: it treated the first enum value
    # as a default and mixed prose-derived defaults with schema defaults.

    def update_type_data(self, full_name, feature_type_data, description):
        """
        Update the feature's data type based on keyword matches or contextual logic.

        This method heuristically sets a feature type to 'Boolean' if its name or description
        indicates it represents a toggle or a flag. It also detects special cases that require
        abstract Boolean typing.

        Args:
            full_name (str): Complete name of the feature.
            feature_type_data (str): Original detected type (e.g., String, Integer).
            description (str): Natural language description of the feature.

        Returns:
            tuple:
                str: Updated feature type.
                bool: True if the feature should be treated as an abstract Boolean.
        """
        abstract_bool = False
        self.feature_aux_original_type = ''

        if any(keyword in full_name for keyword in self.boolean_keywords) and not full_name.endswith('nameStr') and not full_name.endswith('valueInt'): ### and not full_name.endswith('StringValue')
            self.feature_aux_original_type = feature_type_data
            feature_type_data = 'Boolean'
            abstract_bool = True

        ## Addition of a check required for the use of String/integer additions correctly
        if any(special_name in full_name for special_name in self.special_features_config) and 'Note that this field cannot be set when' in description and not full_name.endswith('nameStr') and not full_name.endswith('valueInt'):
            self.feature_aux_original_type = feature_type_data ## A similar logic is applied to the first if to save the aux and then check if it is different from bool.
            feature_type_data = 'Boolean'
            if self.feature_aux_original_type != 'boolean' and self.feature_aux_original_type != feature_type_data and self.feature_aux_original_type != '': ## hay tipos que son vacios y luego se definen por defecto como bool
                abstract_bool = True
        # Check matches with regular expressions
        for pattern in self.boolean_keywords_regex:
            if re.search(pattern, full_name) and not full_name.endswith('nameStr') and not full_name.endswith('valueInt'): ## Para mantener el tipo original del feature
                self.feature_aux_original_type = feature_type_data
                feature_type_data = 'Boolean'
                abstract_bool = True  
        return feature_type_data, abstract_bool
                

    def parse_properties(
        self,
        properties,
        required,
        parent_name="",
        depth=0,
        local_stack_refs=None,
        config_path="",
    ):
        """
        Recursively parse a set of schema properties and transform them into UVL feature nodes.

        This function traverses the JSON schema, generating UVL features by resolving types,
        default values, cardinality, and references. It also handles sanitization, feature categorization,
        and detection of deprecated or abstract fields.

        Args:
            properties (dict): Dictionary of properties from the JSON schema.
            required (list): List of required property names.
            parent_name (str, optional): Full hierarchical name of the parent feature. Defaults to "".
            depth (int, optional): Depth in the feature tree for indentation or tracking. Defaults to 0.
            local_stack_refs (list, optional): Stack of `$ref` paths to prevent cycles. Defaults to None.

        Returns:
            tuple: (mandatory_features, optional_features), where each is a list of UVL feature dictionaries.
        """
        
        if local_stack_refs is None:
            local_stack_refs = []  # Create a new list for this branch

        mandatory_features = [] # Group of mandatory properties
        optional_features = [] # Group of optional properties
        abstract_bool = False ## Property defining whether a feature is abstract or not
        
        queue = deque([(properties, required, parent_name, depth, config_path)])

        while queue:
            current_properties, current_required, current_parent, current_depth, current_config_path = queue.popleft()
            for prop, details in current_properties.items():
                # Avoid leaking the abstract state from the previous property.
                abstract_bool = False
                sanitized_name = self.sanitize_name(prop)
                full_name = f"{current_parent}_{sanitized_name}" if current_parent else sanitized_name
                property_config_path = (
                    f"{current_config_path}.{prop}" if current_config_path else prop
                )
                serialization_kind = self.infer_serialization_kind(details)

                if full_name in self.processed_features:
                    continue

                self.is_cardinality = False
                self.is_deprecated = False
                bool_added_value = False

                description = details.get('description', '')
                is_required_by_description = self.is_required_based_on_description(description)
                required_source = self.required_source(prop, current_required, description)

                # [M8] Final policy for the Kubernetes 1.37.1 baseline:
                # schema required[] OR an unconditional terminal "Required." statement.
                feature_type = 'mandatory' if required_source else 'optional'

                # [M5/M6] Scalar type conversion is independent from collection cardinality.
                raw_schema_type = details.get('type', 'Boolean')
                feature_type_data = self.sanitize_type_data(raw_schema_type)

                # [M5/M7] Only arrays and dynamic maps receive feature cardinality.
                schema_cardinality = self.cardinality_for_schema(details)
                self.is_cardinality = schema_cardinality is not None
                if schema_cardinality is not None:
                    full_name = f"{full_name} cardinality {schema_cardinality}"

                if description:
                    feature_type_data, abstract_bool = self.update_type_data(full_name, feature_type_data, description)
                    self.categorize_description(description, full_name, feature_type_data)

                self.is_deprecated = (
                    details.get('deprecated') is True
                    or self.is_deprecated_description(prop, description)
                )

                # [M4/M12] Multi-value schema enum is authoritative; description extraction is a conservative fallback.
                extracted_values = self.enum_values_from_schema(details)
                enum_source = 'schema' if extracted_values else None
                value_config_map = self.enum_config_value_map(details) if extracted_values else {}

                if not extracted_values:
                    extracted_values = self.extract_values(description)
                    if extracted_values:
                        # Keep deterministic output; the previous implementation used an unordered set.
                        extracted_values = sorted(extracted_values)
                        enum_source = 'description'
                        value_config_map = dict(self._last_extracted_config_values)

                bool_added_value = bool(extracted_values)
                is_union_property = serialization_kind == 'union'
                parent_config_key = None if is_union_property else prop
                parent_serialization_role = 'structural' if is_union_property else 'property'

                parent_uvl_description = (
                    ''
                    if is_union_property and MOVE_UNION_DOC_TO_ALTERNATIVES
                    else description
                )

                attributes = self.schema_attributes(
                    details,
                    description=parent_uvl_description,
                    abstract=(abstract_bool or is_union_property),
                    deprecated=self.is_deprecated,
                    enum_source=enum_source,
                    config_key=parent_config_key,
                    serialization_kind=serialization_kind,
                    serialization_role=parent_serialization_role,
                    include_description_default=not (is_union_property or bool_added_value),
                )
                if required_source:
                    attributes.append(('requiredSource', required_source))
                if is_required_by_description:
                    attributes.append(('documentedRequired', True))

                feature = {
                    'name': full_name,
                    'type': feature_type,
                    'description': description,
                    'sub_features': [],
                    'type_data': '' if feature_type_data == 'Boolean' else feature_type_data,
                    'attributes': attributes,
                    'attribute_text': self.format_uvl_attributes(attributes),
                }
                self.record_schema_metadata(
                    full_name,
                    details,
                    description,
                    feature_type,
                    required_source=required_source,
                )
                self.record_serialization_metadata(
                    full_name,
                    config_path=property_config_path,
                    config_key=parent_config_key,
                    serialization_kind=serialization_kind,
                    serialization_role=parent_serialization_role,
                    required_source=required_source,
                    details=details,
                    default_info=self.extract_documented_default(description),
                )

                # `full_name` remains a pure feature reference (plus cardinality), with no
                # attribute block embedded in it. This prevents metadata from contaminating
                # recursive names and makes attribute rendering deterministic.

                if '$ref' in details:
                    ref = details['$ref']
                    # Check if it is already in the local stack of the current branch (i.e., one cycle).
                    if ref in local_stack_refs:
                        #print(f"*****Referencia cíclica detectada: {ref}. Saltando esta propiedad****")
                        # If it is a cycle, we skip this property but continue processing other properties.
                        continue
                    
                    # Add the reference to the local stack
                    local_stack_refs.append(ref)
                    ref_schema = self.resolve_reference(ref)

                    if ref_schema:
                        ## Lines not needed in this implementation: would be used in omission of the refs (V_1.0)
                        ref_name = self.sanitize_name(ref.split('/')[-1])

                        if 'properties' in ref_schema:
                            sub_properties = ref_schema['properties']
                            sub_required = ref_schema.get('required', [])
                            # Recursive call with the local stack specific to this branch
                            sub_mandatory, sub_optional = self.parse_properties(
                                sub_properties,
                                sub_required,
                                self.strip_cardinality(full_name),
                                current_depth + 1,
                                local_stack_refs,
                                config_path=property_config_path,
                            )
                            # Add subfeatures
                            feature['sub_features'].extend(sub_mandatory + sub_optional)

                            ## Addition of properties that could be null/empty {}
                            if full_name.endswith('emptyDir') or full_name.endswith('EmptyDirVolumeSource'): ## Capture of properties with "emptyDir" and the main schema "EmptyDirVolumeSource".
                                feature['sub_features'].append({ ## Addition at the last level of references to simple schemas that do not have properties
                                'name': f"{full_name}_isEmpty {{doc 'Added option to select when emptyDir is empty declared {{}} '}}", # RefName apart {full_name}_{ref_name}: Names of simple schemas indexed to maintain references to these schemas
                                'type': 'optional', # Since they are references to simple schemas, there is no type. By default it is left optional
                                'description': f"{{doc 'Added option to select when emptyDir is empty declared {{}} '}}",
                                'sub_features': [],
                                'type_data': '' # Default bool for compatibility in simple schemas and feature property
                            })


                        elif 'oneOf' in ref_schema:
                            feature_type = 'mandatory' if required_source else 'optional'
                            oneOf_feature = self.process_oneOf(
                                ref_schema['oneOf'],
                                self.sanitize_name(f"{full_name}"),
                                feature_type,
                                serialization_role='value',
                                config_path=property_config_path,
                                config_key=prop,
                                default_info=self.extract_documented_default(description),
                                inherited_doc=description,
                            ) #_{ref_oneOf}
                            feature_sub = oneOf_feature['sub_features']
                            # Add the reference that contains the oneOf feature
                            feature['sub_features'].extend(feature_sub)

                        else:
                            # If there is no 'properties', process as a simple type
                            # Determinate if the reference is 'mandatory' u 'optional'
                            sanitized_ref = self.sanitize_name(ref_name.split('_')[-1])
                            # Add the procesed refence as a simple type
                            aux_description_simples_schemas = ref_schema.get('description', '')
                            aux_description_simples_schemas_sanitized = self.clean_description(aux_description_simples_schemas) ## Cleanup of descriptions with conflicting characters and errors in uvl formatting

                            type_data_schemas_refs_simple = self.sanitize_type_data(ref_schema.get('type', ''))
                            simple_ref_name = f"{full_name}_{sanitized_ref}"
                            simple_ref_attrs = [
                                ('doc', aux_description_simples_schemas_sanitized),
                                ('serializationKind', self.infer_serialization_kind(ref_schema)),
                                ('serializationRole', 'value'),
                            ]
                            feature['sub_features'].append({
                                'name': simple_ref_name,
                                'type': 'optional',
                                'description': f"{aux_description_simples_schemas}",
                                'sub_features': [],
                                'type_data': type_data_schemas_refs_simple,
                                'attributes': simple_ref_attrs,
                                'attribute_text': self.format_uvl_attributes(simple_ref_attrs),
                            })
                            self.record_serialization_metadata(
                                simple_ref_name,
                                config_path=property_config_path,
                                serialization_kind=self.infer_serialization_kind(ref_schema),
                                serialization_role='value',
                                synthetic=True,
                                details=ref_schema,
                            )
                            if full_name.endswith('creationTimestamp'): ## Addition of a sub-property bool to "accept" null values of creation in the model
                                feature['sub_features'].append({ ## Addition at the last level of references to simple schemas that do not have properties
                                'name': f"{full_name}_isNull {{doc 'Added option to select when creationTimestamp is empty declared: null'}}",
                                'type': 'optional',
                                'description': f"{{doc 'Added option to select when creationTimestamp is empty declared: null'}}",
                                'sub_features': [],
                                'type_data': '' 
                            })
                            elif full_name.endswith('fieldsV1'): ##  Addition of a sub-property bool to "accept" null values of creation in the model
                                feature['sub_features'].append({
                                'name': f"{full_name}_isEmpty02 {{doc 'Added option to select when fieldsV1 is empty declared: {{}}'}}",
                                'type': 'optional', 
                                'description': f"{{doc 'Added option to select when fieldsV1 is empty declared: {{}}'}}", 
                                'sub_features': [],
                                'type_data': '' 
                            })                    
                    local_stack_refs.pop() # Remove local stack reference when exiting this branch

                # Processing items in arrays or additional properties
                elif 'items' in details:
                    items = details['items']
                    if '$ref' in items:
                        ref = items['$ref']
                        # Check if it is already in the local stack of the current branch (i.e., one cycle).
                        if ref in local_stack_refs:
                            #print(f"*****Referencia cíclica detectada en items: {ref}. Saltando esta propiedad****")
                            continue

                        # Añadir la referencia a la pila local
                        local_stack_refs.append(ref)
                        ref_schema = self.resolve_reference(ref)

                        if ref_schema:
                            ref_name = self.sanitize_name(ref.split('/')[-1])
                            
                            if 'properties' in ref_schema:
                                #sub_item_properties = ref_schema['properties']
                                #sub_item_required = ref_schema.get('required', [])
                                #sub_mandatory, sub_optional = self.parse_properties(sub_properties, sub_required, self.strip_cardinality(full_name), current_depth + 1, local_stack_refs) ## Another way to do it
                                item_mandatory, item_optional = self.parse_properties(
                                    ref_schema['properties'],
                                    ref_schema.get('required', []),
                                    self.strip_cardinality(full_name),
                                    current_depth + 1,
                                    local_stack_refs,
                                    config_path=f"{property_config_path}[]",
                                )
                                feature['sub_features'].extend(item_mandatory + item_optional)
                            else:
                                # If there is no 'properties', process as a simple type
                                #feature_type = 'mandatory' if prop in current_required else 'optional' # Determinar si la referencia es 'mandatory' u 'optional'  #sanitized_ref = self.sanitize_name(ref_name.split('_')[-1]) # ref_name = self.sanitize_name(ref.split('/')[-1])
                                sanitized_ref = self.sanitize_name(ref_name.split('_')[-1]) # ref_name = self.sanitize_name(ref.split('/')[-1])
                                full_name = self.strip_cardinality(full_name) ## Added to omit the cardinality when it does not correspond...
                                aux_description_items_schemas = ref_schema.get('description', '')
                                aux_description_items_sanitized = self.clean_description(aux_description_items_schemas) ## Saneamiento de las descripciones con los caracteres que causan conflicto y errores en el formato uvl

                                # Add the processed reference preserving its scalar UVL type.
                                type_data_items_ref = self.sanitize_type_data(ref_schema.get('type', ''))
                                array_ref_name = f"{full_name}_{sanitized_ref}"
                                array_ref_attrs = [
                                    ('doc', aux_description_items_sanitized),
                                    ('serializationKind', self.infer_serialization_kind(ref_schema)),
                                    ('serializationRole', 'arrayItem'),
                                ]
                                feature['sub_features'].append({
                                    'name': array_ref_name,
                                    'type': 'optional',
                                    'description': aux_description_items_sanitized,
                                    'sub_features': [],
                                    'type_data': type_data_items_ref,
                                    'attributes': array_ref_attrs,
                                    'attribute_text': self.format_uvl_attributes(array_ref_attrs),
                                })
                                self.record_serialization_metadata(
                                    array_ref_name,
                                    config_path=f"{property_config_path}[]",
                                    serialization_kind=self.infer_serialization_kind(ref_schema),
                                    serialization_role='arrayItem',
                                    synthetic=True,
                                    details=ref_schema,
                                )
                        # Remove local stack reference when exiting this branch
                        local_stack_refs.pop()
                    elif isinstance(items, dict) and 'oneOf' in items and self.is_cardinality:
                        base_name = self.strip_cardinality(full_name)
                        oneOf_feature = self.process_oneOf(
                            items['oneOf'],
                            base_name,
                            feature_type,
                            serialization_role='arrayItem',
                            config_path=f"{property_config_path}[]",
                            config_key=None,
                            default_info=None,
                        )
                        feature['sub_features'].extend(oneOf_feature['sub_features'])
                    elif isinstance(items, dict) and 'type' in items and self.is_cardinality:
                        # [M5/M6] Preserve all JSON Schema scalar array element types.
                        type_data_items = items['type']
                        full_name = self.strip_cardinality(full_name)
                        scalar_array_types = {
                            'string': ('String', 'StringValue'),
                            'integer': ('Integer', 'IntegerValue'),
                            'number': ('Real', 'RealValue'),
                            'boolean': ('', 'BooleanValue'),
                        }
                        if type_data_items in scalar_array_types and not bool_added_value:
                            uvl_type, suffix = scalar_array_types[type_data_items]
                            aux_description_items = (
                                f"Synthetic value for OpenAPI array; schema item type: {type_data_items}"
                            )
                            item_name = f"{full_name}_{suffix}"
                            item_attrs = [
                                ('doc', aux_description_items),
                                ('serializationRole', 'arrayItem'),
                            ]
                            feature['sub_features'].append({
                                'name': item_name,
                                'type': 'mandatory',
                                'description': aux_description_items,
                                'sub_features': [],
                                'type_data': uvl_type,
                                'attributes': item_attrs,
                                'attribute_text': self.format_uvl_attributes(item_attrs),
                            })
                            self.record_serialization_metadata(
                                item_name,
                                config_path=f"{property_config_path}[]",
                                serialization_kind='scalar',
                                serialization_role='arrayItem',
                                synthetic=True,
                                details=items,
                            )
                        else:
                            print(f"Tipo de dato en array no controlado: {type_data_items}")
                # Process additional properties
                elif 'additionalProperties' in details:
                    additional_properties = details['additionalProperties']

                    # [M5] Dynamic maps are detected from schema structure, never from
                    # description phrases. UVL has no native Map feature type, so the
                    # map key is represented explicitly and the parent cardinality
                    # represents the number of entries.
                    map_base_name = self.strip_cardinality(full_name)
                    if self.is_dynamic_map_schema(details):
                        aux_map_key_doc = "Synthetic String key for an OpenAPI object with additionalProperties"
                        map_key_name = f"{map_base_name}_KeyMap"
                        map_key_attrs = [
                            ('doc', aux_map_key_doc),
                            ('serializationRole', 'mapKey'),
                        ]
                        feature['sub_features'].append({
                            'name': map_key_name,
                            'type': 'mandatory',
                            'description': aux_map_key_doc,
                            'sub_features': [],
                            'type_data': 'String',
                            'attributes': map_key_attrs,
                            'attribute_text': self.format_uvl_attributes(map_key_attrs),
                        })
                        self.record_serialization_metadata(
                            map_key_name,
                            config_path=f"{property_config_path}{{}}",
                            serialization_kind='scalar',
                            serialization_role='mapKey',
                            synthetic=True,
                        )

                    if isinstance(additional_properties, dict) and '$ref' in additional_properties:
                        ref = additional_properties['$ref']
                        
                        # Check if it is already in the local stack of the current branch (i.e., one cycle).
                        if ref in local_stack_refs:
                            #print(f"*****Referencia cíclica detectada en additionalProperties: {ref}. Saltando esta propiedad****")
                            continue

                        # Add the reference to the local stack
                        local_stack_refs.append(ref)
                        ref_schema = self.resolve_reference(ref)

                        if ref_schema:
                            ## Line not necessary in this implementation: would be used in omission of the refs (V_1.0)
                            ref_name = self.sanitize_name(ref.split('/')[-1]) 

                            if 'properties' in ref_schema:
                                item_mandatory, item_optional = self.parse_properties(
                                    ref_schema['properties'],
                                    [],
                                    self.strip_cardinality(full_name),
                                    current_depth + 1,
                                    local_stack_refs,
                                    config_path=f"{property_config_path}{{}}",
                                )
                                feature['sub_features'].extend(item_mandatory + item_optional)
                            elif 'oneOf' in ref_schema:
                                full_name = self.strip_cardinality(full_name)
                                oneOf_feature = self.process_oneOf(
                                    ref_schema['oneOf'],
                                    self.sanitize_name(f"{full_name}"),
                                    feature_type,
                                    serialization_role='mapValue',
                                    config_path=f"{property_config_path}{{}}",
                                    config_key=None,
                                    default_info=None,
                                )
                                feature_sub = oneOf_feature['sub_features']
                                # Add the reference that contains the oneOf feature
                                feature['sub_features'].extend(feature_sub)
                            else:
                                sanitized_ref = self.sanitize_name(ref_name.split('_')[-1])
                                full_name = self.strip_cardinality(full_name)
                                aux_description_additional_schemas = ref_schema.get('description', '')
                                aux_description_additional_sanitized = self.clean_description(aux_description_additional_schemas) ## Saneamiento de las descripciones con los caracteres que causan conflicto y errores en el formato uvl
                                type_data_additional_ref = self.sanitize_type_data(ref_schema.get('type', ''))
                                map_ref_name = f"{full_name}_{sanitized_ref}"
                                map_ref_attrs = [
                                    ('doc', aux_description_additional_sanitized),
                                    ('serializationKind', self.infer_serialization_kind(ref_schema)),
                                    ('serializationRole', 'mapValue'),
                                ]
                                feature['sub_features'].append({
                                    'name': map_ref_name,
                                    'type': 'optional',
                                    'description': aux_description_additional_schemas,
                                    'sub_features': [],
                                    'type_data': type_data_additional_ref,
                                    'attributes': map_ref_attrs,
                                    'attribute_text': self.format_uvl_attributes(map_ref_attrs),
                                })
                                self.record_serialization_metadata(
                                    map_ref_name,
                                    config_path=f"{property_config_path}{{}}",
                                    serialization_kind=self.infer_serialization_kind(ref_schema),
                                    serialization_role='mapValue',
                                    synthetic=True,
                                    details=ref_schema,
                                )
                        local_stack_refs.pop()
                    elif isinstance(additional_properties, dict) and 'items' in additional_properties and self.is_cardinality: ## Addition to generate the leaf node with the data type referenced in items
                        items = additional_properties['items']
                        type_data_additional_items = items['type'] ## Data type items within additionalProperties

                        if type_data_additional_items == 'string' and not bool_added_value:
                            full_name = self.strip_cardinality(full_name)
                            aux_description_string_AP_items = f"Added String mandatory for complete structure Array in the model into AdditionalProperties array Array of Strings: StringValue"
                            map_array_name = f"{full_name}_StringValueAdditional"
                            map_array_attrs = [
                                ('doc', aux_description_string_AP_items),
                                ('serializationKind', 'array'),
                                ('serializationRole', 'mapValue'),
                            ]
                            feature['sub_features'].append({
                                'name': map_array_name,
                                'type': 'mandatory',
                                'description': aux_description_string_AP_items,
                                'sub_features': [],
                                'type_data': 'String',
                                'attributes': map_array_attrs,
                                'attribute_text': self.format_uvl_attributes(map_array_attrs),
                            })
                            self.record_serialization_metadata(
                                map_array_name,
                                config_path=f"{property_config_path}{{}}",
                                serialization_kind='array',
                                serialization_role='mapValue',
                                synthetic=True,
                                details=additional_properties,
                            )
                    elif isinstance(additional_properties, dict) and 'type' in additional_properties and self.is_cardinality:
                        type_data_additional_properties = additional_properties['type']

                        # [M5] For scalar map values, generate one explicit ValueMap
                        # feature whose UVL type follows the OpenAPI scalar type.
                        if type_data_additional_properties in ('string', 'integer', 'number', 'boolean') and not bool_added_value:
                            map_value_type = self.sanitize_type_data(type_data_additional_properties)
                            aux_description_maps_properties = (
                                f"Synthetic value for OpenAPI dynamic map; schema value type: "
                                f"{type_data_additional_properties}"
                            )
                            map_value_name = f"{map_base_name}_ValueMap"
                            map_value_attrs = [
                                ('doc', aux_description_maps_properties),
                                ('serializationRole', 'mapValue'),
                            ]
                            feature['sub_features'].append({
                                'name': map_value_name,
                                'type': 'mandatory',
                                'description': aux_description_maps_properties,
                                'sub_features': [],
                                'type_data': map_value_type,
                                'attributes': map_value_attrs,
                                'attribute_text': self.format_uvl_attributes(map_value_attrs),
                            })
                            self.record_serialization_metadata(
                                map_value_name,
                                config_path=f"{property_config_path}{{}}",
                                serialization_kind='scalar',
                                serialization_role='mapValue',
                                synthetic=True,
                                details=additional_properties,
                            )


                # Extract and add values as subfeatures
                ## All values extracted are "String", to facilitate the representation of the preset values the type is changed to Boolean.
                if extracted_values:
                    feature['type_data'] = '' ## Values are represented by alternative Boolean children.
                    full_name = self.strip_cardinality(full_name)

                    documented_default = self.extract_documented_default(description)

                    for value in extracted_values:
                        bool_default_value = False

                        # Schema enums may already carry a default marker.
                        if '{default' in value:
                            bool_default_value = True
                            value = value.replace(" {default}", "")

                        config_value = value_config_map.get(
                            value,
                            '*' if value == 'estrella' else value
                        )

                        # [M15] An unconditional description-derived default can select the default
                        # alternative. Conditional defaults must never select one
                        # globally because the chosen value depends on context.
                        if (
                            enum_source == 'description'
                            and documented_default is not None
                            and documented_default['semantics'] == 'documented'
                            and str(config_value) == str(documented_default['value'])
                        ):
                            bool_default_value = True

                        full_name_value = f"{full_name}_{value}"
                        if '_Healthy' in full_name_value:
                            print("OMITIENDO HEALTHY", full_name_value)
                            continue

                        aux_description_value = f"Specific value: {config_value}"
                        value_attrs = []
                        if bool_default_value:
                            value_attrs.append(('default', None))
                        value_attrs.extend([
                            ('doc', aux_description_value),
                            ('configValue', config_value),
                            ('configValueSource', enum_source or 'description'),
                            ('serializationRole', 'value'),
                        ])

                        feature['sub_features'].append({
                            'name': full_name_value,
                            'type': 'alternative',
                            'description': aux_description_value,
                            'sub_features': [],
                            'type_data': '',
                            'attributes': value_attrs,
                            'attribute_text': self.format_uvl_attributes(value_attrs),
                        })
                        self.record_serialization_metadata(
                            full_name_value,
                            config_path=property_config_path,
                            serialization_kind='scalar',
                            serialization_role='value',
                            config_value_marker=True,
                            config_value=config_value,
                            config_value_source=enum_source or 'description',
                            synthetic=True,
                            default_variant=bool_default_value,
                        )
                else:
                    if (any(keyword in full_name for keyword in self.boolean_keywords) or any(re.search(keyword, full_name) for keyword in self.boolean_keywords_regex) or any(special_name in full_name for special_name in self.special_features_config) and 'Note that this field cannot be set when' in description):
                        full_name = full_name.replace(" {abstract}", "")
                        aux_description_mandatory = f"Added String mandatory for changing booleans of boolean_keywords: {self.feature_aux_original_type} *_name"

                        if self.feature_aux_original_type == 'String' or self.feature_aux_original_type == 'string': ## It is checked against the original value of the feature. To add the sub-feature as String or Integer
                            helper_name = f"{full_name}_nameStr"
                            helper_attrs = [
                                ('doc', aux_description_mandatory),
                                ('serializationKind', 'scalar'),
                                ('serializationRole', 'value'),
                            ]
                            feature['sub_features'].append({
                            'name': helper_name,
                            'type': 'mandatory',
                            'description': aux_description_mandatory,
                            'sub_features': [],
                            'type_data': 'String',
                            'attributes': helper_attrs,
                            'attribute_text': self.format_uvl_attributes(helper_attrs),
                        })
                            self.record_serialization_metadata(
                                helper_name,
                                config_path=property_config_path,
                                serialization_kind='scalar',
                                serialization_role='value',
                                synthetic=True,
                            )
                        elif self.feature_aux_original_type == 'Integer' or self.feature_aux_original_type == 'integer':
                            helper_name = f"{full_name}_valueInt"
                            helper_attrs = [
                                ('doc', aux_description_mandatory),
                                ('serializationKind', 'scalar'),
                                ('serializationRole', 'value'),
                            ]
                            feature['sub_features'].append({
                            'name': helper_name,
                            'type': 'mandatory',
                            'description': aux_description_mandatory,
                            'sub_features': [],
                            'type_data': 'Integer',
                            'attributes': helper_attrs,
                            'attribute_text': self.format_uvl_attributes(helper_attrs),
                        })
                            self.record_serialization_metadata(
                                helper_name,
                                config_path=property_config_path,
                                serialization_kind='scalar',
                                serialization_role='value',
                                synthetic=True,
                            )

                # Processing nested properties
                if 'properties' in details:
                    sub_properties = details['properties']
                    sub_required = details.get('required', [])
                    value_sanitized_name = re.sub(r'\s*\{.*?\}', '', full_name)
                    sub_mandatory, sub_optional = self.parse_properties(
                        sub_properties,
                        sub_required,
                        value_sanitized_name,
                        current_depth + 1,
                        local_stack_refs,
                        config_path=property_config_path,
                    )
                    feature['sub_features'].extend(sub_mandatory + sub_optional)

                if feature_type == 'mandatory':
                    mandatory_features.append(feature)
                else:
                    optional_features.append(feature)

                self.processed_features.add(full_name)
        return mandatory_features, optional_features
            
    def save_descriptions(self, file_path):
        """
        Save the collected feature descriptions to a JSON file.

        Args:
            file_path (str): Path to the file where descriptions will be saved.
        """
        print(f"Saving descriptions to {file_path}...")
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(self.descriptions, f, indent=4, ensure_ascii=False)
        print("Descriptions saved successfully.")

    def save_constraints(self, file_path):
        """
        Save all collected constraints to a UVL file, appending them after the feature tree.

        Args:
            file_path (str): Path to the UVL file where constraints will be appended.
        """
        print(f"Saving constraints to {file_path}...")
        with open(file_path, 'a', encoding='utf-8') as f:
            f.write("constraints\n") # Quitar para las pruebas con flamapy. Quitado: Restricciones obtenidas de las referencias:
            for constraint in self.constraints:
                f.write(f"\t{constraint}\n")
        print("Constraints saved successfully.")

def load_json_file(file_path):
    """
    Load and parse a JSON file.

    Args:
        file_path (str): Path to the JSON file.

    Returns:
        dict: Parsed content of the JSON file.
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def properties_to_uvl(feature_list, indent=1):
    """
    Convert a list of feature dictionaries to UVL format recursively.

    Features with subfeatures are grouped under `mandatory`, `optional`, or `alternative` blocks.

    Args:
        feature_list (list): List of UVL features to convert.
        indent (int, optional): Indentation level for formatting. Defaults to 1.

    Returns:
        str: UVL-formatted string representing the features.
    """

    uvl_output = ""
    indent_str = '\t' * indent
    boolean_keywords = ['AppArmorProfile_localhostProfile', 'appArmorProfile_localhostProfile', 'seccompProfile_localhostProfile', 'SeccompProfile_localhostProfile', 'IngressClassList_items_spec_parameters_namespace',
                        'IngressClassParametersReference_namespace', 'IngressClass_spec_parameters_namespace', 'IngressClassSpec_parameters_namespace'] ## Added Ingress...Custom for restricction ***
    for feature in feature_list:
        type_str = f"{feature['type_data'].capitalize()} " if feature['type_data'] else "Boolean "
        rendered_feature_name = f"{feature['name']}{feature.get('attribute_text', '')}"
        if type_str == 'Boolean ':
            type_str = ''

        if any(keyword in feature['name'] for keyword in boolean_keywords) and not feature['name'].endswith('nameStr'): ## Specific case 002-localhostProfile String to Boolean: Added to keep String the features added in the Boolean branch.
            type_str = ''

        if feature['sub_features']:
            
            uvl_output += f"{indent_str}{type_str}{rendered_feature_name}\n"
            # Separate mandatory and optional features
            sub_mandatory = [f for f in feature['sub_features'] if f['type'] == 'mandatory']
            sub_optional = [f for f in feature['sub_features'] if f['type'] == 'optional']
            sub_alternative = [f for f in feature['sub_features'] if f['type'] == 'alternative']

            if sub_mandatory:
                uvl_output += f"{indent_str}\tmandatory\n"
                uvl_output += properties_to_uvl(sub_mandatory, indent + 2)
            if sub_optional:
                uvl_output += f"{indent_str}\toptional\n"
                uvl_output += properties_to_uvl(sub_optional, indent + 2)
            if sub_alternative:
                uvl_output += f"{indent_str}\talternative\n"
                uvl_output += properties_to_uvl(sub_alternative, indent + 2)
        else:
            uvl_output += f"{indent_str}{type_str}{rendered_feature_name}\n"
    return uvl_output

def generate_uvl_from_definitions(
    definitions_file,
    output_file,
    descriptions_file,
    serialization_metadata_file=None,
    system=SYSTEM_NAME,
    version=SYSTEM_VERSION,
):
    """
    Generate a UVL feature model and associated documentation from a JSON schema definitions file.

    This function:
    - Loads the schema
    - Parses features using `SchemaProcessor`
    - Writes the UVL model to disk
    - Saves descriptions and constraints

    Args:
        definitions_file (str): Path to the JSON definitions input file.
        output_file (str): Path to write the resulting .uvl file.
        descriptions_file (str): Path to write extracted descriptions in JSON format.
    """

    definitions = load_json_file(definitions_file) # Load JSON definition file
    processor = SchemaProcessor(definitions) # Initialize the schema processor with loaded definitions

    root_attrs = processor.format_uvl_attributes([
        ('abstract', None),
        ('system', system),
        ('systemVersion', version),
    ])
    uvl_output = f"namespace KubernetesTest1\nfeatures\n\tKubernetes{root_attrs}\n\t\toptional\n"

    # Procesar cada definición en el archivo JSON
    for schema_name, schema in definitions.get('definitions', {}).items():
        root_schema = schema.get('properties', {})
        required = schema.get('required', [])
        mandatory_features, optional_features = processor.parse_properties(
            root_schema,
            required,
            processor.sanitize_name(schema_name),
            config_path="",
        ) # Obtain mandatory and optional features
        
        schema_description_aux = schema.get('description', '')
        root_feature_name = processor.sanitize_name(schema_name)

        # [M1/M2/M10] Root definitions also retain schema/OpenAPI/Kubernetes metadata,
        # including x-kubernetes-group-version-kind when present. No artificial doc
        # string is inserted when the source schema has no description.
        root_serialization_kind = processor.infer_serialization_kind(schema)
        root_attributes = processor.schema_attributes(
            schema,
            description=schema_description_aux,
            deprecated=schema.get('deprecated') is True,
            serialization_kind=root_serialization_kind,
            serialization_role='structural',
        )
        root_attribute_text = processor.format_uvl_attributes(root_attributes)
        processor.record_schema_metadata(root_feature_name, schema, schema_description_aux, 'schema-root')
        processor.record_serialization_metadata(
            root_feature_name,
            config_path="",
            serialization_kind=root_serialization_kind,
            serialization_role='structural',
            synthetic=False,
            details=schema,
        )

        # Adding mandatory and optional features to the UVL file
        if mandatory_features:
            uvl_output += f"\t\t\t{root_feature_name}{root_attribute_text}\n"
            uvl_output += f"\t\t\t\tmandatory\n"
            uvl_output += properties_to_uvl(mandatory_features, indent=5)

            if optional_features:
                uvl_output += f"\t\t\t\toptional\n"
                uvl_output += properties_to_uvl(optional_features, indent=5)
        elif optional_features:
            uvl_output += f"\t\t\t{root_feature_name}{root_attribute_text}\n"
            uvl_output += f"\t\t\t\toptional\n"
            uvl_output += properties_to_uvl(optional_features, indent=5)

        # Adjustment addition simple schemes
        if not root_schema: ## RawExtension, JSONSchemaPropsOrBool, IntOrString-like definitions, etc.
            if 'oneOf' in schema:
                oneOf_feature = processor.process_oneOf(
                    schema['oneOf'],
                    root_feature_name,
                    type_feature='optional',
                    serialization_role='value',
                    config_path="",
                    config_key=None,
                    default_info=None,
                )
                if oneOf_feature:
                    oneOf_feature['attribute_text'] = root_attribute_text
                    uvl_output += properties_to_uvl([oneOf_feature], indent=3)
            else:
                uvl_output += f"\t\t\t{root_feature_name}{root_attribute_text}\n"

    # Save the generated UVL file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(uvl_output)
    print(f"UVL output saved to {output_file}")

    # save the extracted descriptions
    processor.save_descriptions(descriptions_file)

    if serialization_metadata_file:
        sha256 = hashlib.sha256()
        with open(definitions_file, 'rb') as f_schema:
            for chunk in iter(lambda: f_schema.read(1024 * 1024), b''):
                sha256.update(chunk)
        processor.save_serialization_metadata(
            serialization_metadata_file,
            schema_source=definitions_file,
            schema_sha256=sha256.hexdigest(),
            system=system,
            version=version,
        )
    
    # Save the restrictions in the UVL file
    ### processor.save_constraints(output_file) ## Duplicated method to write constraints


def generate_semantic_variant(
    structural_output_file,
    semantic_output_file,
    descriptions_file,
    constraints_report_file=None,
):
    """Create a constrained UVL variant without modifying the structural baseline.

    The current semantic constraints are still heuristic/description-derived.
    Therefore they are deliberately isolated in a separate artifact that can be
    parsed, validated and evaluated independently.
    """
    from analisisScript01 import generar_constraintsDef

    restrictions = generar_constraintsDef(descriptions_file) or []

    # Preserve order while removing exact duplicates/blank entries.
    unique_restrictions = []
    seen = set()
    for restriction in restrictions:
        restriction = str(restriction).strip()
        if not restriction or restriction in seen:
            continue
        seen.add(restriction)
        unique_restrictions.append(restriction)

    with open(structural_output_file, 'r', encoding='utf-8') as f_in:
        structural_model = f_in.read().rstrip()

    with open(semantic_output_file, 'w', encoding='utf-8') as f_out:
        f_out.write(structural_model)
        f_out.write("\n")
        if unique_restrictions:
            f_out.write("constraints\n")
            for restriction in unique_restrictions:
                f_out.write(f"\t{restriction}\n")

    if constraints_report_file:
        with open(constraints_report_file, 'w', encoding='utf-8') as f_report:
            for restriction in unique_restrictions:
                f_report.write(restriction + "\n")

    print(
        f"Semantic FM variant saved in {semantic_output_file} "
        f"with {len(unique_restrictions)} unique constraints"
    )
    return unique_restrictions


# Relative file paths
#definitions_file = "../../resources/kubernetes-json-v1.30.2/_definitions.json"
#output_file = "../../variability_model/kubernetes_combined_04-1.uvl"
#descriptions_file = "../../resources/model_generation/descriptions_01-1.json"

definitions_file = "../../resources/kubernetes-json-v1.37.1/_definitions_1-37.json"

# Frozen structural-enriched baseline.
output_file = "../../variability_model/kubernetes_combined_1-37_V2.uvl"
descriptions_file = "../../resources/model_generation/descriptions_1-37_V2.json"
serialization_metadata_file = "../../resources/model_generation/serialization_metadata_1-37_V2.json"

# Separate semantic-enrichment artifact. This never overwrites the baseline.
semantic_output_file = "../../variability_model/kubernetes_combined_1-37_V2_semantic.uvl"
constraints_report_file = "../../resources/model_generation/constraints_1-37_V2.txt"


if __name__ == "__main__":
    # Generate structural UVL file and save description/schema metadata.
    generate_uvl_from_definitions(
        definitions_file,
        output_file,
        descriptions_file,
        serialization_metadata_file,
        system=SYSTEM_NAME,
        version=SYSTEM_VERSION,
    )

    print(f"Structural-enriched FM baseline saved in {output_file}")

    # [M9] Start the semantic-constraint phase without contaminating the baseline.
    if GENERATE_SEMANTIC_VARIANT:
        try:
            generate_semantic_variant(
                output_file,
                semantic_output_file,
                descriptions_file,
                constraints_report_file,
            )
        except Exception as exc:
            # The structural FM remains valid/useful even if semantic extraction
            # still contains a rule that needs debugging.
            print(
                "WARNING: semantic variant could not be generated. "
                f"Structural baseline is untouched. Reason: {exc}"
            )
