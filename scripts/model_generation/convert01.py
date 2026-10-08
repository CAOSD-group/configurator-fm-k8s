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
    - A JSON file with parsed feature descriptions
"""

import json
import re
from collections import deque

# Import function of process restricctions
from scripts.model_generation.analisisScript01 import generar_constraintsDef

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
            'dependencies': []

        }
        self.is_cardinality = False
        self.is_deprecated = False
        self.seen_descriptions = set()

        # Patterns for classifying descriptions into categories of values, constraints and dependencies
        self.patterns = {
            'values': re.compile(r'^\b$', re.IGNORECASE), # values are|valid|supported|acceptable|can be
            'restrictions': re.compile(r'If the operator is|template.spec.restartPolicy|conditions may not be|Details about a waiting|TCPSocket is NOT|must be between|Note that this field cannot be set when|valid port number|must be in the range|must be greater than|are mutually exclusive properties|Must be set if type is|field MUST be empty if|must be non-empty if and only if|only if type|\. Required when|required when scope|\. At least one of|a least one of|Exactly one of|resource access request|datasetUUID is|succeededIndexes specifies|Represents the requirement on the container|ResourceClaim object in the same namespace as this pod|indicates which one of|may be non-empty only if|Minimum value is|Value must be non-negative|minimum valid value for|in the range 1-', re.IGNORECASE),
            'dependencies': re.compile(r'^\b$', re.IGNORECASE) ## (requires|if[\s\S]*?only if|only if) # depends on ningun caso especial, quitar relies on: no hay casos, contingent upon: igual = related to

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
        """
        Sanitize a type data by replacing invalid types for UVL.

        Args:
            type_data (str): A str within a type of data definition

        Returns:
            type_data: The valid type data converstion if necessary.
        """

        if type_data in ['array']: ## modify so that a different state is stored in the array to be taken into account => cardinality
            self.is_cardinality = True ## Tag to know that cardinality must be added [1..*].
            type_data = 'Boolean'
        elif type_data in ['Object', 'object']: ## Default boolean
            type_data = 'Boolean'
            self.is_cardinality = True ### Adjustment so that a cardinality is generated in object...
        elif type_data in ['number', 'Number']:
            type_data = 'Integer'
        elif type_data == 'Boolean':
            type_data = ''
        return type_data

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

    def is_required_based_on_description(self, description):
        """
        Determines whether a property is required based on whether Required appears at the end of its description.

        Args:
            description: The description of the feature related

        Returns:
            True/False: True if "Requires" appears at end of its description
        """
        return description.strip().endswith("Required.")

    def extract_values(self, description):
        """
        Extract a list of valid values from a feature description.

        Args:
            description (str): The feature description.

        Returns:
            list or None: Extracted values or None.
        """

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
        default_value = self.patterns_process_enum_values_default(description)
        for pattern in value_patterns:
            matches = pattern.findall(description)
            for match in matches:
                split_values = re.split(r',\s*|\s+or\s+|\sor|or\s|\s+and\s+|and\s', match)  # Make sure that "or" is surrounded by spaces.
                for v in split_values:
                    v = v.strip()
                    v = v.replace('*', 'estrella') # Replace '*' for "estrella", * invalid in uvl
                    v = v.replace('"', '').replace("'", '').replace('`','')  # Removes double, single and closed quotation marks                    
                    v = v.replace(' ', '_').replace('/', '_')

                    # Filter values that contain periods, square brackets, braces or are too long
                    if v and len(v) <= 24 and not any(char in v for char in {'.', '{', '}', '[', ']',';', ':', 'prefixed_keys'}): # added / due to syntax problems 'yet', ## Added prefixed_keys, handled to remove, are not values
                        if len(v) >= 20 and '_' in v:
                        # Exclude values with underscore and size >= 20
                            print(f"Excluding values: {v}")
                        else:
                        # Add the value if it does not have underscore or if it is less than 20 characters
                            if v == default_value:
                                v = f"{v} {{default}}"
                            values.append(v)

        case_not_none = ['NodePort', f"ClusterIP {{default}}", 'None', 'LoadBalancer', 'ExternalName'] ## List where None was added and was not part of the possible value set
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

    def patterns_process_enum_values_default(self, description):
 
        patterns_default_values = ['defaults to', '. implicitly inferred to be', 'the currently supported reasons are', '. default is'] # 'Defaults to', al comprobar luego con minus en mayuscula no cuenta
        
        if not any(keyword in description.lower() for keyword in patterns_default_values):
            return None
        default_value = ""
        
        default_patterns = [  
            re.compile(r'(?<=defaults to\s)(["\']?[\w\s\.\-"\']+?)(?=\.)', re.IGNORECASE),  # Capture is stopped at the literal point
            re.compile(r'(?<=Defaults to\s)(["\']?[\w\s\.\-"\']+["\']?)'),  
            re.compile(r'Implicitly inferred to be\s["\'](.*?)["\']', re.IGNORECASE),
            re.compile(r'default to use\s["\'](.*?)["\'](?=\.)', re.IGNORECASE), #
            re.compile(r'\. Default is\s["\']?(.*?)["\']?(?=\.)', re.IGNORECASE),
            #Implicitly inferred to be
        ]
        
        for pattern in default_patterns:
            matches = pattern.findall(description)
            for match in matches:
                first_part = match.split('.')[0]  # We only take what is before the first point
                v = first_part.strip().replace('"', '').replace("'", '').replace('.', '').strip()
                if v == '*': ## case where the default value is "*" and this character cannot be represented: it is changed to "star".
                    v = 'estrella'
                # Apply restrictions: length and type of value
                if v and len(v) <= 50:
                    default_value = v
        if not default_value:
            return None
        
        return default_value

    def contains_non_ascii(self, text): ## Searching for ascii characters in the docs
        """
        Identify non-ASCII characters and specific special characters within the input text.

        Args:
            text (str): The text to analyze.

        Returns:
            tuple: A set of non-ASCII characters and a set of special characters found
                (specifically carriage return `\\r` and newline `\\n`).
        """
        non_ascii_chars = {c for c in text if ord(c) > 127}  # Caracters searching > 127
        special_chars = {'\r', '\n'}  # List of specific caracters to be deleted
        found_specials = {c for c in text if c in special_chars}  # Detect \r y \n

        return non_ascii_chars, found_specials

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

        if not self.is_valid_description(description, feature_name):
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

    def clean_description(description): ## Function to remove invalid characters in the doc and in flamapy parsing
        """
        Sanitize a description by removing or replacing problematic characters.

        This prepares the string for use in UVL generation or text analysis tools like Flamapy.

        Args:
            description (str): Original description text.

        Returns:
            str: Cleaned version of the description with special characters removed or replaced.
        """
        cleaned_description = description.replace('\n', '').replace('`', '').replace("´", '') \
                                        .replace("'", "_").replace('{', '').replace('}', '') \
                                        .replace('"', '').replace("\\", "_").replace(".", "").replace("//","_") ## replace("\\", "_").replace(".", "") //
        return cleaned_description

    def process_oneOf(self, oneOf, full_name, type_feature):
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
        # Process each option within 'oneOf'
        for option in oneOf:
            if 'type' in option:
                option_type_data = option['type'].capitalize()  # Capture type (e.g. string, number, integer)
                sanitized_name = full_name.replace(" cardinality [1..*]", "") ## Addendum to remove cardinality from name inheritance

                if ' {default ' in sanitized_name: ## Part added to avoid adding the {default X} as part of the name for some sub-features generating an error: feature_name_{default X}_asType
                    sanitized_name = re.sub(r'\s*\{.*?\}', '', sanitized_name) # All content inside the square brackets and the space ## sanitized_name = re.sub(r'\s* "default", ‘’, sanitized_name) is deleted
                # Create subfeature with appropriate name
                aux_description_sub_feature = f"Sub-feature added of type {option_type_data}"

                sub_feature = {
                    'name': f"{sanitized_name}_as{option_type_data} {{doc '{aux_description_sub_feature}'}}", ##  ## quizas mas adelante definir una descr personalizada para el sub_feature
                    'type': 'alternative',  # By default, it is added as alternative
                    'description': aux_description_sub_feature,
                    'sub_features': [],
                    'type_data': self.sanitize_type_data(option_type_data)
                }

                # Add the subfeature to the list of sub_features of the main feature
                feature['sub_features'].append(sub_feature)

        return feature
    
    def process_enum_defaultInte(self, property, full_name, description):
        """
        Add default value information from enums or text description to the feature name.

        This method detects default values (e.g., integers or booleans) defined via 'enum'
        or pattern-matched in the description and embeds that information in the feature name.

        Args:
            property (dict): Schema property dictionary, may include `enum`.
            full_name (str): Base feature name.
            description (str): Natural language description of the property.

        Returns:
            tuple:
                str: Feature name including `{default ...}` and doc metadata if a default was found.
                bool: Flag indicating whether a default value was successfully detected.
        """
        patterns_default_values_numbers = ['defaults to', 'default value is', 'default to', 'default false', 'Default is false', 'is \"false', 'is \"true'] # Grupo alternativo al anterior para definir los patrones que tienen Integers por defecto o grupos similares
        default_integer = 0
        default_bool = False
        default_full_name = ''
        if 'enum' in property and property['enum']:
            cleaned_description = description.replace('\n', '').replace('`', '').replace("´", '').replace("'", "_").replace('{','').replace('}','').replace('"', '').replace("\\", "_").replace(".", "").replace("//","_") ## Saneamiento de las descripciones con los caracteres que causan conflicto y errores en el formato uvls
            cleaned_description = ''.join(c for c in cleaned_description if ord(c) < 128)
            default_value = property['enum'][0]
            default_full_name = f"{full_name} {{default '{default_value}', doc '{cleaned_description}'}}"
            default_bool = True
            return default_full_name, default_bool

        if any(keyword in description.lower() for keyword in patterns_default_values_numbers):
            default_patterns = [
                re.compile(r'(?<=Defaults to\s)(\d+)(?=\D|$)', re.IGNORECASE),
                re.compile(r'(?<=Default value is\s)(\d+)(?=\D|$)', re.IGNORECASE),
                re.compile(r'(?<=Default to\s)(\d+)(?=\D|$)'), ## Test insert more defaults.. added 250 default 10 and various... 0 => 20
                re.compile(r'(?<=Default to\s)([\w\s\.])(?=\.)'),
            ]
            cleaned_description = description.replace('\n', '').replace('`', '').replace("´", '').replace("'", "_").replace('{','').replace('}','').replace('"', '').replace("\\", "_").replace(".", "").replace("//","_") ## Saneamiento de las descripciones con los caracteres que causan conflicto y errores en el formato uvls
            cleaned_description = ''.join(c for c in cleaned_description if ord(c) < 128)

            for pattern in default_patterns:
                matches = pattern.search(description)
                if matches:
                    default = matches.group(1)
                    default_integer = default
                    if default_integer == '0644':
                        default_integer = 644
                    default_full_name = f"{full_name} {{default {default_integer}, doc '{cleaned_description}'}}"
                    default_bool = True
            
            pattern_defaults = re.compile(r'Default is\s+\\?"(true|false)\\?"') ## Pattern for default is with escaped quotation marks in descriptions
            match = pattern_defaults.search(description)

            if match: ## If it matches, it is also added to the matching features. Default is \"true\" y Default is \"false\".
                default_boolean = match.group(1)
                default_bool = True
                default_full_name = f"{full_name} {{default {default_boolean}, doc '{cleaned_description}'}}"

            if 'Default to false' in description or 'defaults to false' in description.lower() or 'default false' in description.lower() or 'Default is false' in description:
                default_bool = True
                if 'defaults to false' in description.lower() and "deprecated." in description.lower(): ## Specific case where it had a default and is deprecated
                    default_full_name = f"{full_name} {{default false, deprecated, doc '{cleaned_description}'}}"
                else:    
                    default_full_name = f"{full_name} {{default false, doc '{cleaned_description}'}}"
            elif 'Default to true' in description or 'defaults to true' in description.lower(): ## or 'Default is \"t' in description deprecated. 
                default_bool = True
                default_full_name = f"{full_name} {{default true, doc '{cleaned_description}'}}"

            if default_full_name != '':
                return default_full_name, default_bool
                
        return full_name, default_bool

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
                

    def parse_properties(self, properties, required, parent_name="", depth=0, local_stack_refs=None):
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
        
        queue = deque([(properties, required, parent_name, depth)])

        while queue:
            current_properties, current_required, current_parent, current_depth = queue.popleft()
            for prop, details in current_properties.items():
                sanitized_name = self.sanitize_name(prop)
                full_name = f"{current_parent}_{sanitized_name}" if current_parent else sanitized_name

                if full_name in self.processed_features:
                    continue

                self.is_cardinality = False ## Start with False : it is delimited in the types
                self.is_deprecated = False
                bool_added_value = False ## Added to try to avoid duplication of alternative and mandatory, values and stringValue
                # Verify if the property is required based on its description
                description = details.get('description', '')
                is_required_by_description = self.is_required_based_on_description(description)
                feature_type = 'mandatory' if prop in current_required or is_required_by_description else 'optional'
                # Parsing of data types and invalid data types
                feature_type_data = details.get('type', 'Boolean')
                feature_type_data = self.sanitize_type_data(feature_type_data) 
                # Here we call process_enum to modify the name if it has an enum
                full_name, default_bool = self.process_enum_defaultInte(details, full_name, description) ## Modificacion del name para añadir default Integer

                if self.is_cardinality and 'cardinality' in full_name: ## Bloque de condiciones para agregar el cardinality a los features de tipo array y marcarlos o desmarcarlos para eliminar la etiqueta
                    full_name = full_name.replace(" cardinality [1..*]", "")
                    if 'unstructured key value map' in description:
                        full_name = f"{full_name} cardinality [0..*]"
                    else:
                        full_name = f"{full_name} cardinality [1..*]"
                elif self.is_cardinality and not 'cardinality' in full_name:
                    if 'unstructured key value map' in description:
                        full_name = f"{full_name} cardinality [0..*]"
                    else:
                        full_name = f"{full_name} cardinality [1..*]"
                else:
                    self.is_cardinality = False ## To avoid cases where the cardinality of the previous feature is maintained
                    full_name = full_name.replace(" cardinality [1..*]", "")
                    full_name = full_name.replace(" cardinality [0..*]", "")

                #description = details.get('description', '')
                if description:
                    feature_type_data, abstract_bool = self.update_type_data(full_name, feature_type_data, description) ### Modificion para que en descriptions_01.json se cambie de String a Boolean si coincide con el nombre
                    self.categorize_description(description, full_name, feature_type_data) # categorized = 
                    cleaned_description = description.replace('\n', '').replace('`', '').replace("´", '').replace("'", "_").replace('{','').replace('}','').replace('"', '').replace("\\", "_").replace(".", "").replace("//","_") ## Saneamiento de las descripciones con los caracteres que causan conflicto y errores en el formato uvl
                    cleaned_description = ''.join(c for c in cleaned_description if ord(c) < 128)
                    # Check non-ASCII and specific characters
                    #text = "Ejemplo con ñ, á, é, í, ó, ú y saltos de línea.\nOtro más.\r"
                    non_ascii, specials = self.contains_non_ascii(cleaned_description)
                    # Show results
                    if non_ascii or specials:
                        print(f"Caracteres no ASCII encontrados: {non_ascii}")
                        print(f"Caracteres especiales encontrados: {specials}")
                        # Ejemplo de texto con caracteres especiales
                    res = bool(re.match(r'^[\x00-\x7F]*$', cleaned_description))
                    if not res:
                        print(f"Caracteres no ASCII encontrados: {cleaned_description}")
                        print(str(res))

                    if "DEPRECATED:" in cleaned_description or "deprecated." in cleaned_description.lower() or "This field is deprecated," in cleaned_description or "deprecated field" in cleaned_description:
                        self.is_deprecated = True ## Probar si no altera algun otro etiquetado de los features
                        
                    if not default_bool and not abstract_bool and not self.is_deprecated: # Condición agregada para agregar el atributo doc a features que no sean default ni abstract
                        full_name = f"{full_name} {{doc '{cleaned_description}'}}"
                        self.is_deprecated = False
                    elif self.is_deprecated and not default_bool:
                        full_name = f"{full_name} {{deprecated, doc '{cleaned_description}'}}"
                        self.is_deprecated = False
                feature = {                  
                    'name': full_name if not abstract_bool else f"{full_name} {{abstract, doc '{cleaned_description}'}}", ## Añadir {abstract} a los features creados para tener mejor definición de las constraints
                    'type': feature_type,
                    'description': description,
                    'sub_features': [],
                    'type_data': '' if feature_type_data == 'Boolean' else feature_type_data ## String ##
                }
                full_name = re.sub(r'\s*\{.*?\}', '', full_name)
                # Process references
                # Extract and add values as subfeatures
                extracted_values = self.extract_values(description)
                bool_added_value = bool(extracted_values)

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
                            sub_mandatory, sub_optional = self.parse_properties(sub_properties, sub_required, full_name, current_depth + 1, local_stack_refs)
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
                            feature_type = 'mandatory' if prop in current_required or is_required_by_description else 'optional'
                            oneOf_feature = self.process_oneOf(ref_schema['oneOf'], self.sanitize_name(f"{full_name}"), feature_type) #_{ref_oneOf}
                            feature_sub = oneOf_feature['sub_features']
                            # Add the reference that contains the oneOf feature
                            feature['sub_features'].extend(feature_sub)

                        else:
                            # If there is no 'properties', process as a simple type
                            # Determinate if the reference is 'mandatory' u 'optional'
                            sanitized_ref = self.sanitize_name(ref_name.split('_')[-1])
                            # Add the procesed refence as a simple type
                            aux_description_simples_schemas = ref_schema.get('description', '')
                            aux_description_simples_schemas_sanitized = aux_description_simples_schemas.replace('\n', '').replace('`', '').replace("´", '').replace("'", "_").replace('{','').replace('}','').replace('"', '').replace("\\", "_").replace(".", "").replace("//","_") ## Cleanup of descriptions with conflicting characters and errors in uvl formatting
                            aux_description_simples_schemas_sanitized = ''.join(c for c in aux_description_simples_schemas_sanitized if ord(c) < 128)

                            type_data_schemas_refs_simple = self.sanitize_type_data(ref_schema.get('type', ''))
                            feature['sub_features'].append({ ## Addition at the last level of references to simple schemas that do not have properties
                                'name': f"{full_name}_{sanitized_ref} {{doc '{aux_description_simples_schemas_sanitized}'}}",
                                'type': 'optional', 
                                'description': f"{aux_description_simples_schemas}",
                                'sub_features': [],
                                'type_data': type_data_schemas_refs_simple, # The data type of the simple schema ## is left as default for compatibility in simple schemas and feature property.
                            })
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
                                #sub_mandatory, sub_optional = self.parse_properties(sub_properties, sub_required, full_name, current_depth + 1, local_stack_refs) ## Another way to do it
                                item_mandatory, item_optional = self.parse_properties(ref_schema['properties'], ref_schema.get('required', []), full_name, current_depth + 1, local_stack_refs)
                                feature['sub_features'].extend(item_mandatory + item_optional)
                            else:
                                # If there is no 'properties', process as a simple type
                                #feature_type = 'mandatory' if prop in current_required else 'optional' # Determinar si la referencia es 'mandatory' u 'optional'  #sanitized_ref = self.sanitize_name(ref_name.split('_')[-1]) # ref_name = self.sanitize_name(ref.split('/')[-1])
                                sanitized_ref = self.sanitize_name(ref_name.split('_')[-1]) # ref_name = self.sanitize_name(ref.split('/')[-1])
                                full_name = full_name.replace(" cardinality [1..*]", "") ## Added to omit the cardinality when it does not correspond...
                                aux_description_items_schemas = ref_schema.get('description', '')
                                aux_description_items_sanitized = aux_description_items_schemas.replace('\n', '').replace('`', '').replace("´", '').replace("'", "_").replace('{','').replace('}','').replace('"', '').replace("\\", "_").replace(".", "").replace("//","_") ## Saneamiento de las descripciones con los caracteres que causan conflicto y errores en el formato uvl
                                aux_description_items_sanitized = ''.join(c for c in aux_description_items_sanitized if ord(c) < 128)

                                # Add the processed reference as a simple type
                                feature['sub_features'].append({
                                    'name': f"{full_name}_{sanitized_ref} {{doc '{aux_description_items_sanitized}'}}",
                                    'type': 'optional',  # It is left as optional by default (Varies according to the interpretation that you want to give it)
                                    'description': aux_description_items_sanitized,
                                    'sub_features': [],
                                    'type_data': '' ## Default for compatibility in simple schemes and feature ownership: Boolean
                                })
                        # Remove local stack reference when exiting this branch
                        local_stack_refs.pop()
                    elif 'type' in items and self.is_cardinality: ## Addition to generate the leaf node with the data type referenced in items
                        type_data_items = items['type']
                        full_name = full_name.replace(" cardinality [1..*]", "") ## Added to omit the cardinality when it does not correspond...

                        if type_data_items == 'string' and not bool_added_value:
                            aux_description_string_items = f"Added String mandatory for complete structure Array in the model The modified is not in json but provide represents, Array of Strings: StringValue"
                            feature['sub_features'].append({
                                'name': f"{full_name}_StringValue {{doc '{aux_description_string_items}'}}",
                                'type': 'mandatory',
                                'description': aux_description_string_items, #f"Added String mandatory for adding the structure Array in the model: StringValue",
                                'sub_features': [],
                                'type_data': 'String'
                            })
                        elif type_data_items == 'integer': ## Addition of yamls to json mapping view with features
                            aux_description_string_items = f"Added Integer mandatory for complete structure Array in the model The modified is not in json but provide represents, Array of Integers: IntegerValue"
                            feature['sub_features'].append({
                                'name': f"{full_name}_IntegerValue {{doc '{aux_description_string_items}'}}",
                                'type': 'mandatory',
                                'description': aux_description_string_items, #f"Added String mandatory for adding the structure Array in the model: StringValue",
                                'sub_features': [],
                                'type_data': 'Integer'
                            })
                        else:
                            print("Tipo de dato en array no controlado. Exclusion de tipos, no compatibilidad.")
                # Process additional properties
                elif 'additionalProperties' in details:
                    additional_properties = details['additionalProperties']
                    if '$ref' in additional_properties:
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
                                item_mandatory, item_optional = self.parse_properties(ref_schema['properties'], [], full_name, current_depth + 1, local_stack_refs)
                                feature['sub_features'].extend(item_mandatory + item_optional)
                            elif 'oneOf' in ref_schema:
                                full_name = full_name.replace(" cardinality [1..*]", "")
                                oneOf_feature = self.process_oneOf(ref_schema['oneOf'], self.sanitize_name(f"{full_name}"), feature_type)
                                feature_sub = oneOf_feature['sub_features']
                                # Add the reference that contains the oneOf feature
                                feature['sub_features'].extend(feature_sub)
                            else:
                                sanitized_ref = self.sanitize_name(ref_name.split('_')[-1])
                                full_name = full_name.replace(" cardinality [1..*]", "")
                                aux_description_additional_schemas = ref_schema.get('description', '')
                                aux_description_additional_sanitized = aux_description_additional_schemas.replace('\n', '').replace('`', '').replace("´", '').replace("'", "_").replace('{','').replace('}','').replace('"', '').replace("\\", "_").replace(".", "").replace("//","_") ## Saneamiento de las descripciones con los caracteres que causan conflicto y errores en el formato uvl
                                aux_description_additional_sanitized  = ''.join(c for c in aux_description_additional_sanitized if ord(c) < 128)
                                feature['sub_features'].append({
                                    'name': f"{full_name}_{sanitized_ref} {{doc '{aux_description_additional_sanitized}'}}", 
                                    'type': 'optional',
                                    'description': aux_description_additional_schemas, 
                                    'sub_features': [],
                                    'type_data': ''
                                })
                        local_stack_refs.pop()
                    elif 'items' in additional_properties and self.is_cardinality: ## Addition to generate the leaf node with the data type referenced in items
                        items = additional_properties['items']
                        type_data_additional_items = items['type'] ## Data type items within additionalProperties

                        if type_data_additional_items == 'string' and not bool_added_value:
                            full_name = full_name.replace(" cardinality [1..*]", "")
                            aux_description_string_AP_items = f"Added String mandatory for complete structure Array in the model into AdditionalProperties array Array of Strings: StringValue"
                            feature['sub_features'].append({
                                'name': f"{full_name}_StringValueAdditional {{doc '{aux_description_string_AP_items}'}}",
                                'type': 'mandatory',
                                'description': aux_description_string_AP_items, #f"Added String mandatory for adding the structure Array in the model: StringValue",
                                'sub_features': [],
                                'type_data': 'String'
                            })
                    elif 'type' in additional_properties and self.is_cardinality:
                        type_data_additional_properties = additional_properties['type']

                        if type_data_additional_properties == 'string' and not bool_added_value:
                            full_name = full_name.replace(" cardinality [1..*]", "")
                            full_name = full_name.replace(" cardinality [0..*]", "")
                            aux_description_string_properties = f"Added String mandatory for complete structure Object in the model The modified is not in json but provide represents, Array of Strings: StringValue"
                            aux_description_maps_properties = f"Added Map for complete structure Object in the model The modified is not in json but provide represents, Array of pairs key, value: ValueMap, KeyMap"
                            list_local_features_maps = ['Map of', 'matchLabels is a map of', 'label keys and values'] ## 'unstructured key value map',

                            if any(wordMap in description for wordMap in list_local_features_maps): ## Option to add sub-features as maps
                                feature['sub_features'].append({
                                'name': f"{full_name}_KeyMap {{doc 'key: {aux_description_maps_properties}'}}",
                                'type': 'mandatory',
                                'description': aux_description_maps_properties, #f"Added String mandatory for adding the structure Array in the model: StringValue",
                                'sub_features': [],
                                'type_data': 'String'
                                })
                                feature['sub_features'].append({
                                'name': f"{full_name}_ValueMap {{doc 'value: {aux_description_maps_properties}'}}",
                                'type': 'mandatory',
                                'description': aux_description_maps_properties, #f"Added String mandatory for adding the structure Array in the model: StringValue",
                                'sub_features': [],
                                'type_data': 'String'
                                })
                            elif 'unstructured key value map' in description:
                                ## Caso especial de objeto que puede ser null/optional
                                feature['sub_features'].append({
                                'name': f"{full_name}_KeyMap {{doc 'key: {aux_description_maps_properties}'}}",
                                'type': 'optional',
                                'description': aux_description_maps_properties,
                                'sub_features': [],
                                'type_data': 'String'
                                })
                                feature['sub_features'].append({
                                'name': f"{full_name}_ValueMap {{doc 'value: {aux_description_maps_properties}'}}",
                                'type': 'optional',
                                'description': aux_description_maps_properties,
                                'sub_features': [],
                                'type_data': 'String'
                                })                                
                            else:        
                                feature['sub_features'].append({ ## Case in case there is a searched structure without the map expressions
                                    'name': f"{full_name}_StringValueAdditional {{doc '{aux_description_string_properties}'}}",
                                    'type': 'mandatory',
                                    'description': aux_description_string_properties,
                                    'sub_features': [],
                                    'type_data': 'String'
                                })

                # Extract and add values as subfeatures
                ## All values extracted are "String", to facilitate the representation of the preset values the type is changed to Boolean.
                if extracted_values:
                    feature['type_data'] = '' ## The data type of the current FEATURE is accessed: From Boolean to empty ''.
                    full_name = full_name.replace(" cardinality [1..*]", "") ## In case the cardinality is passed at any point
                    for value in extracted_values:
                        bool_default_value = False
                        if ('{default' in value): ## Condition to check if any of the values is default, check and remove the default to add it together with doc.
                            bool_default_value = True
                            value = value.replace(" {default}", "") ##  The {default} is removed and marked to be added together with the doc.

                        full_name_value = f"{full_name}_{value}"
                        if '_Healthy' in full_name_value: ## Check for omitting values that should not be added to the model
                            print("OMITIENDO HEALTHY", full_name_value)
                            continue
                        aux_description_value = f"Specific value: {value}"

                        feature['sub_features'].append({
                            'name': f"{full_name_value} {{default, doc '{aux_description_value}'}}" if bool_default_value else f"{full_name_value} {{doc '{aux_description_value}'}}",
                            'type': 'alternative', # All values are usually alternatives (Choice of only one)
                            'description': aux_description_value,
                            'sub_features': [],
                            'type_data': ''  # Default boolean: changed to empty
                        })
                else:
                    if (any(keyword in full_name for keyword in self.boolean_keywords) or any(re.search(keyword, full_name) for keyword in self.boolean_keywords_regex) or any(special_name in full_name for special_name in self.special_features_config) and 'Note that this field cannot be set when' in description):
                        full_name = full_name.replace(" {abstract}", "")
                        aux_description_mandatory = f"Added String mandatory for changing booleans of boolean_keywords: {self.feature_aux_original_type} *_name"

                        if self.feature_aux_original_type == 'String' or self.feature_aux_original_type == 'string': ## It is checked against the original value of the feature. To add the sub-feature as String or Integer
                            feature['sub_features'].append({
                            'name': f"{full_name}_nameStr {{doc '{aux_description_mandatory}'}}",
                            'type': 'mandatory',
                            'description': aux_description_mandatory,
                            'sub_features': [],
                            'type_data': 'String'  # String by default: an open feature is required to be able to enter a text field
                        })
                        elif self.feature_aux_original_type == 'Integer' or self.feature_aux_original_type == 'integer':
                            feature['sub_features'].append({
                            'name': f"{full_name}_valueInt {{doc '{aux_description_mandatory}'}}",
                            'type': 'mandatory',
                            'description': aux_description_mandatory,
                            'sub_features': [],
                            'type_data': 'Integer'  # Default Integer: an open feature is required to enter a positive integer
                        })

                # Processing nested properties
                if 'properties' in details:
                    sub_properties = details['properties']
                    sub_required = details.get('required', [])
                    value_sanitized_name = re.sub(r'\s*\{.*?\}', '', full_name)
                    sub_mandatory, sub_optional = self.parse_properties(sub_properties, sub_required, value_sanitized_name, current_depth + 1, local_stack_refs)
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
        if type_str == 'Boolean ':
            type_str = ''

        if any(keyword in feature['name'] for keyword in boolean_keywords) and not feature['name'].endswith('nameStr'): ## Specific case 002-localhostProfile String to Boolean: Added to keep String the features added in the Boolean branch.
            type_str = ''

        if feature['sub_features']:
            
            uvl_output += f"{indent_str}{type_str}{feature['name']}\n"
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
            uvl_output += f"{indent_str}{type_str}{feature['name']}\n"
    return uvl_output

def generate_uvl_from_definitions(definitions_file, output_file, descriptions_file):
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
    uvl_output = "namespace KubernetesTest1\nfeatures\n\tKubernetes {abstract}\n\t\toptional\n" # Initialize the basic structure of the UVL file {{abstract}}

    # Procesar cada definición en el archivo JSON
    for schema_name, schema in definitions.get('definitions', {}).items():
        root_schema = schema.get('properties', {})
        required = schema.get('required', [])
        mandatory_features, optional_features = processor.parse_properties(root_schema, required, processor.sanitize_name(schema_name)) # Obtain mandatory and optional features
        
        schema_description_aux = schema.get('description', "") ## The descriptions of the main schemes are obtained to show them as well.
        if schema_description_aux:
            cleaned_description = schema_description_aux.replace('\n', '').replace('`', '').replace("´", '').replace("'", "_").replace('{','').replace('}','').replace('"', '').replace("\\", "_").replace(".", "").replace("//","_") ## Saneamiento de las descripciones con los caracteres que causan conflicto y errores en el formato uvl
            cleaned_description = ''.join(c for c in cleaned_description if ord(c) < 128)
            non_ascii, specials = processor.contains_non_ascii(cleaned_description)
            # Show results
            if non_ascii or specials:
                print(f"Caracteres no ASCII encontrados: {non_ascii}")
                print(f"Caracteres especiales encontrados: {specials}")
        else:
            cleaned_description = "Auto doc generate for not add empty Strings No descripcion in schemas JSON"  
        
        # Adding mandatory and optional features to the UVL file
        if mandatory_features:
            uvl_output += f"\t\t\t{processor.sanitize_name(schema_name)} {{doc '{cleaned_description}'}}\n" 
            uvl_output += f"\t\t\t\tmandatory\n"
            uvl_output += properties_to_uvl(mandatory_features, indent=5)

            if optional_features:
                uvl_output += f"\t\t\t\toptional\n"
                uvl_output += properties_to_uvl(optional_features, indent=5)
        elif optional_features:
            uvl_output += f"\t\t\t{processor.sanitize_name(schema_name)} {{doc '{cleaned_description}'}}\n" 
            uvl_output += f"\t\t\t\toptional\n"
            uvl_output += properties_to_uvl(optional_features, indent=5)
        # Adjustment addition simple schemes
        if not root_schema: ## To take into account schemas that do not have properties: such as RawExtension, JSONSchemaPropsOrBool, JSONSchemaPropsOrArray that only have a description.
            if 'oneOf' in schema:
                oneOf_feature = processor.process_oneOf(schema['oneOf'], processor.sanitize_name(schema_name), type_feature='optional')
                if oneOf_feature:
                    uvl_output += properties_to_uvl([oneOf_feature], indent=3) 
            else:
                uvl_output += f"\t\t\t{processor.sanitize_name(schema_name)} {{doc '{cleaned_description}'}}\n" # {type_str_feature+' '} ## Omitiendo Bool

    # Save the generated UVL file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(uvl_output)
    print(f"UVL output saved to {output_file}")

    # save the extracted descriptions
    processor.save_descriptions(descriptions_file)
    
    # Save the restrictions in the UVL file
    ### processor.save_constraints(output_file) ## Duplicated method to write constraints

# Relative file paths
#definitions_file = "../../resources/kubernetes-json-v1.30.2/_definitions.json"
#output_file = "../../variability_model/kubernetes_combined_04-1.uvl"
#descriptions_file = "../../resources/model_generation/descriptions_01-1.json"

definitions_file = "../../resources/kubernetes-json-v1.37.1/_definitions_1-37.json"
output_file = "../../variability_model/kubernetes_combined_1-37.uvl"
descriptions_file = "../../resources/model_generation/descriptions_1-37.json"


# Generate UVL file and save descriptions
generate_uvl_from_definitions(definitions_file, output_file, descriptions_file)

# Generate UVL constraints and add them to the end of the file
restrictions = generar_constraintsDef(descriptions_file)
with open(output_file, 'a', encoding='utf-8') as f_out:
    f_out.write("constraints\n")
    for restrict in restrictions:
        f_out.write(f"\t{restrict}\n")

print(f"FM UVL and restricctions saved in {output_file}")