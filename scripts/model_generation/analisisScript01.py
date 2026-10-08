"""
This module extracts and generates UVL (Universal Variability Language) constraints
based on natural language descriptions from Kubernetes schema features.

It supports analysis of descriptions for types like Boolean, Integer, String,
and applies rules such as "required when", "must be empty if", mutual exclusivity,
minimum values, and other constraints commonly found in Kubernetes specifications.

The extracted constraints are used to generate logic rules suitable for feature modeling tools.

Typical usage:
    python analisisScript01.py

Dependencies:
    - Input JSON file with feature descriptions and metadata.
    - Outputs a plain text file with extracted UVL constraints.
"""

import json
import re

count = 0  # Number to count the number of descriptions invalids

# Dict to convert "zero" y "one"
word_to_num = {
    "zero": 0,
    "one": 1
}

def load_json_features(file_path):
    """
    Load feature descriptions from a JSON file.

    Args:
        file_path (str): Path to the JSON file.

    Returns:
        dict: Parsed JSON content.
    """

    with open(file_path, 'r', encoding='utf-8') as file:
        return json.load(file)

def convert_word_to_num(word):
    """
    Convert a word representation of a number to its integer form.

    Args:
        word (str): The word to convert (e.g., "zero", "one").

    Returns:
        int or None: Corresponding integer or None if unknown.
    """
    return word_to_num.get(word.lower(), None)

def extract_constraints_template_onlyAllowed(description, feature_key):
    """
    Extract constraints for `template.spec.restartPolicy` based on descriptions.

    Args:
        description (str): Natural language description.
        feature_key (str): Feature name key.

    Returns:
        str: UVL constraint string.

    Raises:
        ValueError: If expected patterns are not matched.
    """

    template_spec_policy_pattern01 = re.compile(r'(?<=The only allowed template.spec.restartPolicy value is\s)\"([A-Za-z]+)\"', re.IGNORECASE) # In brackets add the double quotation marks as well.
    template_spec_policies_pattern02 = re.compile(r'\"([A-Za-z]+)\"') ## Expression that captures the values enclosed in quotation marks: case 2
    feature_with_spec = f"{feature_key}_spec_restartPolicy"
    
    if 'value is' in description:
        policy_match = template_spec_policy_pattern01.search(description)
        if not policy_match:
            raise ValueError(f"No se encontró un valor único en la descripción: {description}")
        policy_Always = f"{feature_with_spec}_{policy_match.group(1)}"
        policy_OnFailure = f"{feature_with_spec}_OnFailure"
        policy_Never = f"{feature_with_spec}_Never" 
        return f"({policy_Always} => {feature_key}) & (!{policy_Never}) & (!{policy_OnFailure})"

    elif 'values are' in description: ### Case in which there are 2 possible values for template.spec.restartPolicy: Never and OnFailure
        policies_match = template_spec_policies_pattern02.findall(description) # The values of the case are obtained from the descriptions
        policies_Always = f"{feature_with_spec}_Always"
        if len(policies_match) < 2:
            raise ValueError(f"Se esperaban al menos dos valores en la descripción: {description}")
        allowed_policies = [f"{feature_with_spec}_{policy}" for policy in policies_match]
        return f"({' | '.join(allowed_policies)}  => {feature_key}) & (!{policies_Always})" ## New case correct
    
    # If none of the cases are met
    raise ValueError(f"Descripción inesperada para {feature_key}: {description}")

def extract_constraints_string_oneOf(description, feature_key):
    """
    Extract constraints for string fields that indicate exclusive selection.

    Args:
        description (str): Description indicating a one-of rule.
        feature_key (str): Feature identifier.

    Returns:
        str: UVL constraint.
    """

    uvl_rule = ""
    feature_without_lastProperty = feature_key.rsplit('_', 1)[0]

    if 'indicates which one of' in description: # The description of the kind is used to have the feature string of the types that can be the other fields. It is interpreted as meaning that only one of the fields can be selected (10).
        print("No SE EJECUTA?")
        kind_authentication_group = f"{feature_without_lastProperty}_group"
        kind_authentication_serviceAccount = f"{feature_without_lastProperty}_serviceAccount"
        kind_authentication_User = f"{feature_without_lastProperty}_user"

        uvl_rule += f"({feature_key} == 'Group' => {kind_authentication_group})" \
        f" | ({feature_key} == 'ServiceAccount' => {kind_authentication_serviceAccount})" \
        f" | ({feature_key} == 'User' => {kind_authentication_User})" \
        f" & !({kind_authentication_group} & {kind_authentication_serviceAccount})" \
        f" & !({kind_authentication_serviceAccount} & {kind_authentication_User})" \
        f" & !({kind_authentication_group} & {kind_authentication_User})" 
        # Conditions are added so that only one can be caught at a time. It is not entirely clear from the description. But it can be assumed

    if uvl_rule is not None:
        return uvl_rule.strip() # Return restrictions and remove blank lines
    else:
        return "El conjunto esta vacio"
def extract_constraints_multiple_conditions(description, feature_key):
    """
    Extract logical constraints involving multiple conditions.

    Args:
        description (str): Feature description.
        feature_key (str): Feature identifier.

    Returns:
        str: UVL constraint.
    """

    conditions_pattern = re.compile(r'\b(Approved|Denied|Failed)\b')
    type_notbe_pattern = re.compile(r'(?<=conditions may not be\s)\"([A-Za-z]+)\"\s+or\s+\"([A-Za-z]+)\"')

    uvl_rule = ""
    feature_without_lastProperty = feature_key.rsplit('_', 1)[0]
    if 'conditions may not be' in description: # (4)
        type_match = type_notbe_pattern.search(description)
        type01 = type_match.group(1)    
        type02 = type_match.group(2)
        types_notbe = f"!{feature_key}_{type01} & !{feature_key}_{type02}"
        conditions_match = conditions_pattern.findall(description)
        uvl_rule += f"{feature_without_lastProperty} => ({feature_without_lastProperty}_type_{conditions_match[0]} | {feature_without_lastProperty}_type_{conditions_match[1]} | {feature_without_lastProperty}_type_{conditions_match[2]}) => {types_notbe}"
    elif 'Details about a waiting' in description: # Only one of the descriptions will be processed and the other features will be introduced statically. As there are 3 values and there is no description with these, the other 2 will be added manually...
        # Function that defines the status of a container with 3 possible options. Only one can be selected (21)
        container_state01 = f"{feature_without_lastProperty}_running"
        container_state02 = f"{feature_without_lastProperty}_terminated"
        uvl_rule += f"{feature_without_lastProperty} => ({feature_key} => !{container_state01} & !{container_state02})" \
        f" & ({container_state01} => !{feature_key} & !{container_state02})" \
        f" & ({container_state02} => !{feature_key} & !{container_state01})"
        uvl_rule += f"& (!{container_state01} & !{container_state02} => {feature_key})" # Default rule, if no other is selected, default waiting... is selected.
    elif 'TCPSocket is NOT' in description: ## Restrictions without pattern, not defined in the descriptions of the sub-features involved (175)
        """ New group based on description: no pattern, main description: lifecycleHandler defines a specific action to be taken in a lifecycle hook. One and only one of the fields, except TCPSocket must be specified. """
        action_lifecycle_exec = f"{feature_without_lastProperty}_exec"
        action_lifecycle_httpGet =f"{feature_without_lastProperty}_httpGet"
        action_lifecycle_sleep = f"{feature_without_lastProperty}_sleep"
        
        uvl_rule += f"{feature_without_lastProperty} => ({action_lifecycle_exec} | {action_lifecycle_httpGet} | {action_lifecycle_sleep})" \
        f" & !({action_lifecycle_exec} & {action_lifecycle_httpGet})" \
        f" & !({action_lifecycle_exec} & {action_lifecycle_sleep})" \
        f" & !({action_lifecycle_httpGet} & {action_lifecycle_sleep})" \
        f" & !{feature_key}"

    if uvl_rule is not None:
        return uvl_rule.strip()
    else:
        return "El conjunto esta vacio"

def extract_constraints_primary_or(description, feature_key):
    """
    Extract constraints that express "Exactly one of" or similar logic.

    Args:
        description (str): Description of mutual exclusive logic.
        feature_key (str): Feature identifier.

    Returns:
        str: UVL constraint.
    """

    uvl_rule = ""
    feature_without_lastProperty = feature_key.rsplit('_', 1)[0]

    if 'non-resource access request' in description: ## Exactly one of 
        resourceAtr01 = f"{feature_without_lastProperty}_resourceAttributes"
        uvl_rule = f"{feature_without_lastProperty} => ({feature_key} | {resourceAtr01}) & !({feature_key} & {resourceAtr01})"
    elif 'succeededIndexes specifies' in description: ## Each rule must have at least one of the
        resourceAtr02 = f"{feature_without_lastProperty}_succeededCount" ## (9)
        uvl_rule = f"{feature_without_lastProperty} => {feature_key} | {resourceAtr02}"
    elif 'Represents the requirement on the container' in description: ## Adición de un grupo con una regla "compleja" en la que tiene que haber un feature activo pero no se pueden los 2 a la vez (9)
        resourceAtr03 = f"{feature_without_lastProperty}_onPodConditions" ## One of onExitCodes and onPodConditions, but not both,
        uvl_rule = f"{feature_without_lastProperty} => ({feature_key} | {resourceAtr03}) & !({feature_key} & {resourceAtr03})"
    elif 'ResourceClaim object in the same namespace as this pod' in description: ## Se convierte y asume que "ClaimSource describes a reference to a ResourceClaim.\n\nExactly one of these fields should be set.", se pasa a feature1 XOR feature2
        resourceAtr04 = f"{feature_without_lastProperty}_resourceClaimTemplateName" ## ()
        uvl_rule = f"{feature_without_lastProperty} => ({feature_key} | {resourceAtr04}) & !({feature_key} & {resourceAtr04})"
    elif 'datasetUUID is' in description: ## Represents a Flocker volume mounted by the Flocker agent. One and only one of datasetName and datasetUUID should be set. (37)
        flocker_volume_datasetName = f"{feature_without_lastProperty}_datasetName"
        uvl_rule = f"{feature_without_lastProperty} => ({feature_key} | {flocker_volume_datasetName}) & !({feature_key} & {flocker_volume_datasetName})"

    if uvl_rule is not None:
        return uvl_rule
    else:
        return "El conjunto esta vacio"

def extract_constraints_least_one(description, feature_key):
    """
    Extract constraints requiring at least one feature to be enabled.

    Args:
        description (str): Feature description with least one condition.
        feature_key (str): Feature identifier.

    Returns:
        str: UVL constraint.
    """

    least_one_pattern01 = re.compile(r'(?<=a least one of\s)(\w+)\s+or\s+(\w+)', re.IGNORECASE) #  Expresión regular para obtener los 2 valores precedidos por "a least one of" y separados por un "or"
    exactly_least_one_pattern02 = re.compile(r'(?<=Exactly one of\s)`(\w+)`\s+or\s+`(\w+)`', re.IGNORECASE) #  Expresión regular para los valores precedidos por "Exactly..." y que se encuentren bajo comillas invertidas separados por un "or" (8, url, service)
    at_least_one_pattern01 = re.compile(r'(?<=At least one of\s)`(\w+)`\s+and\s+`(\w+)`', re.IGNORECASE) #  Expresión regular para los valores precedidos por "At..." y que se encuentran como en el anterior, bajo comillas invertidas y separadas por un "and"

    uvl_rule = ""

    feature_without_lastProperty = feature_key.rsplit('_', 1)[0]
    a_least_match01 = least_one_pattern01.search(description)
    exactly_match01 = exactly_least_one_pattern02.search(description)
    at_least_match01 = at_least_one_pattern01.search(description)

    if a_least_match01: ## If there is a match with the first expression, the defined rule/constraint is added.
        value01 = a_least_match01.group(1)
        value02 = a_least_match01.group(2)

        uvl_rule = f"{feature_without_lastProperty} => {feature_without_lastProperty}_{value01} | {feature_without_lastProperty}_{value02}"
    elif exactly_match01: ## If there is a match with the second expression the defined constraint is added
        print("Comprobacion02", exactly_match01)
        value01 = exactly_match01.group(1)
        value02 = exactly_match01.group(2)

        uvl_rule = f"{feature_without_lastProperty} => ({feature_without_lastProperty}_{value01} | {feature_without_lastProperty}_{value02}) & !({feature_without_lastProperty}_{value01} & {feature_without_lastProperty}_{value02})"
    elif at_least_match01: ## If there is a match with the third expression the defined constraint is added
        value01 = at_least_match01.group(1)
        value02 = at_least_match01.group(2)

        uvl_rule = f"{feature_without_lastProperty} => {feature_without_lastProperty}_{value01} | {feature_without_lastProperty}_{value02}"

    if uvl_rule is not None:
        return uvl_rule
    else:
        return "El conjunto esta vacio"


def extract_constraints_operator(description, feature_key):
    """
    Extract constraints based on operator usage (e.g., Exists, In).

    Args:
        description (str): Description involving operators.
        feature_key (str): Feature identifier.

    Returns:
        str: UVL constraint.
    """
    # Regular expression for "Requires (X, Y) when feature is up" ## The order of feature selection or not is defined by the expressions "non-empty, empty. If there is any variation it will be taken into account for the selection".
    operator_is_pattern01 = re.compile(r'If the operator is\s+(\w+)\s+or\s+(\w+)', re.IGNORECASE) #  Expresión regular para obtener todos los pares (X,Y) de las descripcciones con "If the operator is"
    operator_if_pattern02 = re.compile(r'If the operator is\s+(\w+)') # Expresion para las restricciones que solo tienen un único valor

    uvl_rule = ""
    feature_without_lastProperty = feature_key.rsplit('_', 1)[0]
    operator_match01 = operator_is_pattern01.findall(description)

    print("Operator 01",operator_match01)

    # Initialize the variables to store the values of the restrictions
    required_value = None

    if operator_match01 and 'is Exists,' not in description:
        # Capture the property and the value of "Required when".
        type_property01 = operator_match01[0] # Captures the first values of the pair "In or NotIn"
        type_property02 = operator_match01[1] # Captures the first values of the pair "Exists or DoesNotExist".
        print("Required 01", type_property01)
        print("Required 02", type_property02)

        uvl_rule = f"({feature_without_lastProperty}_operator_{type_property01[0]} | {feature_without_lastProperty}_operator_{type_property01[1]} => {feature_key}) | ({feature_without_lastProperty}_operator_{type_property02[0]} |{feature_without_lastProperty}_operator_{type_property02[1]} => !{feature_key})"
        if('the operator is Gt or Lt' in description):
            type_property05 = operator_match01[2]
            uvl_rule += f"| ({feature_without_lastProperty}_operator_{type_property05[0]} |{feature_without_lastProperty}_operator_{type_property05[1]} => {feature_key})"

    elif 'is Exists' in description: ## Case in which there is only one value and a different capture is used (32 descriptions).
        print(f"CASO ERRONEO DE VALIDACION")
        operator_match02 = operator_if_pattern02.search(description)
        print(f"MATCHES: {operator_match02}")
        required_value = operator_match02.group(1)
        print(f"REQUIRED: {required_value}")
        uvl_rule = f"{feature_without_lastProperty}_operator_{required_value} => !{feature_key}"

    if uvl_rule is not None:
        return uvl_rule
    else:
        return "El conjunto esta vacio"

def extract_constraints_os_name(description, feature_key):
    """
    Extract OS-specific constraints for Windows/Linux settings.

    Args:
        description (str): Description referring to `spec.os.name`.
        feature_key (str): Feature identifier.

    Returns:
        str: UVL constraint.
    """

    osName_pattern = re.compile(r'(?<=Note that this field cannot be set when spec.os.name is\s)([a-zA-Z\s,]+)(?=\.)', re.IGNORECASE) # re.compile(r'\`([A-Za-z]+)\`')
    uvl_rule =""
    osName_match = osName_pattern.search(description)
    path_osName = "os_name"
    print("Los SO son: ",osName_match)
    list_anothers = ['_v1_Container_securityContext_', '_v1_EphemeralContainer_securityContext_', '_PodSecurityContext_', '_v1_SecurityContext_']

    if osName_match and '_template_spec_' in feature_key: # Depending on which group the feature_os_name belongs to, the main group of 1247 features is different.
        match = re.search(r'^(.*?_template_spec)', feature_key)

        if match:
            feature_without0 = match.group(1)
            print("EL FEATURE DEL MATCH ES ",feature_without0)
        else:
            print("ERROR EN EL MATCH DE feature_key")

        name_obtained = osName_match.group(1) # Obtain the name of the patron

        uvl_rule = f"{feature_without0}_{path_osName}_{name_obtained} => !{feature_key}"

    elif osName_match and '_Pod_spec_' in feature_key: ## Case of second group, _Pod_spec_, 43 features
        match = re.search(r'^(.*?_Pod_spec)', feature_key)

        if match:
            feature_without0 = match.group(1)
            print("EL FEATURE DEL MATCH ES ",feature_without0)
        else:
            print("ERROR EN EL SEGUNDO MATCH DE feature_key")

        name_obtained = osName_match.group(1)
        uvl_rule = f"{feature_without0}_{path_osName}_{name_obtained} => !{feature_key}"


    elif osName_match and '_PodList_items_spec_' in feature_key: ## Case of third group, _PodList_items_spec_, 43 features
        
        match = re.search(r'^(.*?_PodList_items_spec)', feature_key) #(r'^(.*?_template_spec)')
        feature_without0 = match.group(1)
        name_obtained = osName_match.group(1) # Obtain the name of the obtained pattern
        uvl_rule = f"{feature_without0}_{path_osName}_{name_obtained} => !{feature_key}"

    elif osName_match and '_core_v1_PodSpec_' in feature_key: ## Case of fouth group, _core_v1_PodSpec, 43 features

        match = re.search(r'^(.*?_core_v1_PodSpec)', feature_key) #(r'^(.*?_template_spec)')

        feature_without0 = match.group(1)
        name_obtained = osName_match.group(1)
        uvl_rule = f"{feature_without0}_{path_osName}_{name_obtained} => !{feature_key}"

    elif osName_match and '_PodTemplateSpec_spec_' in feature_key: # Case of the fifth group, _PodTemplateSpec_spec_, 43 features

        match = re.search(r'^(.*?_PodTemplateSpec_spec)', feature_key) #(r'^(.*?_template_spec)')
        feature_without0 = match.group(1)
        name_obtained = osName_match.group(1)
        uvl_rule = f"{feature_without0}_{path_osName}_{name_obtained} => !{feature_key}"

    elif osName_match and any(pattern in feature_key for pattern in list_anothers): ## Case of group without spec feature: general group

        predefined_feature_os = "io_k8s_api_core_v1_PodSpec_os_name"        
        name_obtained = osName_match.group(1)
        uvl_rule = f"{predefined_feature_os}_{name_obtained} => !{feature_key}"

    if uvl_rule is not None:
        return uvl_rule
    else:
        print("UVL RULE ESTA VACÍO")


def extract_constraints_mutualy_exclusive(description, feature_key):
    """
    Extract mutual exclusion constraints between two features.

    Args:
        description (str): Natural language description.
        feature_key (str): Feature base name.

    Returns:
        str: UVL mutual exclusion constraint.
    """
    # For this case there are 12 descriptors that are not accessed because it is not necessary to have the same ref in each pair, processing one is equivalent to the 2.
    
    exclusive_pattern = re.compile(r'\`([A-Za-z]+)\`')
    uvl_rule =""
    exclusive_match = exclusive_pattern.findall(description)
    feature_without_lastProperty = feature_key.rsplit('_', 1)[0]

    if exclusive_match:
        type_property01 = exclusive_match[0] # There are repeated values but only the first 2 values are accessible.
        type_property02 = exclusive_match[1]
        uvl_rule = f"({feature_without_lastProperty}_{type_property01} => !{feature_without_lastProperty}_{type_property02}) & ({feature_without_lastProperty}_{type_property02} => !{feature_without_lastProperty}_{type_property01})"
    
    if uvl_rule is not None:
        return uvl_rule
    else:
        print("UVL RULE ESTA VACÍO")

## Function to convert constraints strings and requires 
def extract_constraints_if(description, feature_key): 
    """
    Extract "only if" or "must be set if" type constraints.

    Args:
        description (str): Description indicating conditional rules.
        feature_key (str): Feature name.

    Returns:
        str: UVL constraint.
    """

    only_if_pattern = re.compile(r'\"([A-Za-z]+)\"')

    uvl_rule =""
    if_match = only_if_pattern.search(description)
    feature_without_lastProperty = feature_key.rsplit('_', 1)[0]

    if if_match and 'exempt' not in feature_key: # It deals with desciptions with the pattern "Must be set if type is".
        value_obtained = if_match.group(1)
        if 'Must be set if type is' in description or 'Must be set if and only if type' in description or 'may be non-empty only if' in description: ## Agregado nuevo conjunto may be non-empty only if (10)
            uvl_rule = f"{feature_without_lastProperty}_type_{value_obtained} <=> {feature_key}"
        else:
            ## Division between the types if the feature can only be accessed if the type is the concrete one
            uvl_rule = f"{feature_without_lastProperty}_type_{value_obtained} => {feature_key}"

    elif 'exempt' in feature_key: ### Treat descriptions with the pattern "This field MUST be empty if:"
        exempt_match = only_if_pattern.findall(description)
        type_property01 = exempt_match[0] # Limited
        type_property02 = exempt_match[1] # Exempt
        uvl_rule = f"({feature_without_lastProperty}_type_{type_property01} => !{feature_key}) | ({feature_without_lastProperty}_type_{type_property02} => {feature_key})" ### Aqui se especifican los 2 casos
        
    if uvl_rule is not None:
        return uvl_rule
    else:
        return "No hay ninguna coincidencia con los patrones y descripciones"

def extract_constraints_required_when(description, feature_key):
    # Regular expression for "Required when X is set to Y".
    required_when_pattern = re.compile(r'Required when\s+(\w+)\s+is\s+set\s+to\s+"([^"]+)"', re.IGNORECASE)
    # Regular expression for "Must be unset when X is set to Y"
    must_be_unset_pattern = re.compile(r'must be unset when\s+(\w+)\s+is\s+set\s+to\s+"([^"]+)"', re.IGNORECASE)
    # Regular expression for "Required when `X` is set to `Y`"
    required_when_pattern_strategy = re.compile(r'Required when\s+`(\w+)`\s+is\s+set\s+to\s+`?\"?([^\"`]+)\"?`?', re.IGNORECASE)

    uvl_rule = ""
    feature_without_lastProperty = feature_key.rsplit('_', 1)[0]
    # Search matches for "Required when".
    required_match = required_when_pattern.search(description)
    unset_match = must_be_unset_pattern.search(description)
    when_match = required_when_pattern_strategy.search(description)

    # Initializing the variables to store the values of the constraintss
    required_property, required_value = None, None
    unset_property, unset_value = None, None

    if  required_match and unset_match:
        # Capture the property and the value of "Required when".
        required_property = required_match.group(1)
        required_value = required_match.group(2)
        uvl_rule = f"{feature_without_lastProperty}_{required_property}_{required_value} => {feature_key}"
        
        unset_property = unset_match.group(1)
        unset_value = unset_match.group(2)
        uvl_rule += f" & !({feature_without_lastProperty}_{unset_property}_{unset_value})"

    elif when_match and not required_match and not unset_match:
        value_property = when_match.group(1)  # Capture the property (strategy o scope)
        value_default = when_match.group(2)  # Capture the value (Webhook o Namespace)
        # Adjust feature_key according to your format
        feature_without_lastProperty = feature_key.rsplit('_', 1)[0]
        # Generates the UVL rule for "Required when".
        uvl_rule = f"{feature_without_lastProperty}_{value_property}_{value_default} => {feature_key}"

    if uvl_rule is not None:
        return uvl_rule
    else:
        return "El conjunto esta vacio"


def extract_minimum_value(description, feature_key):
    """
    Extract minimum value constraints from descriptions.

    Args:
        description (str): Feature description with value hints.
        feature_key (str): Feature identifier.

    Returns:
        str: UVL constraint.

    Raises:
        ValueError: If no matching pattern is found.
    """

    value_minimum_pattern = re.compile(r'(?<=Minimum value is\s)(\d+)')
    value_text_pattern = re.compile(r'(?<=minimum valid value for expirationSeconds is\s)(\d+)')
    in_the_range_pattern = re.compile(r'(?<=in the range\s)(\d+)-(\d+)')

    uvl_rule =""
    minimum_match = value_minimum_pattern.search(description)
    minimum_text_match = value_text_pattern.search(description)
    range_match = in_the_range_pattern.search(description)


    if minimum_match: ## (1295)
        uvl_rule = f"{feature_key} > {minimum_match.group(1)}"
    elif not minimum_match and "Value must be non-negative" in description: ## (36)
        uvl_rule = f"{feature_key} > 0"
    elif minimum_text_match: ## (3)
        print(f"El minimo tiene que ser 600: ", minimum_text_match)
        uvl_rule = f"{feature_key} > {minimum_text_match.group(1)}"
    elif range_match: ## (92)
        print(f"LOS RANGE MATCH SON: {range_match.group(2)}")
        uvl_rule = f"{feature_key} > {range_match.group(1)} & {feature_key} < {range_match.group(2)}"
        
    if uvl_rule is not None:
        return uvl_rule ## 1426
    # If none of the cases are met
    raise ValueError(f"Descripción inesperada para {feature_key}: {description}")

def extract_bounds(description):
    """
    Extract numeric bounds (min, max) and type flags from description.

    Args:
        description (str): Description to parse.

    Returns:
        tuple: (min_bound, max_bound, is_port_number, is_other_number)
    """

    min_bound = None
    max_bound = None
    is_port_number = False
    is_other_number = False

    # Expressions to detect intervals of the form "0 < x < 65536", "1-65535 inclusive", y "Number must be in the range 1 to 65535"    range_pattern = re.compile(r'(\d+)\s*<\s*\w+\s*<\s*(\d+)')
    range_pattern = re.compile(r'(\d+)\s*<\s*\w+\s*<\s*(\d+)')
    inclusive_range_pattern = re.compile(r'(\d+)\s*-\s*(\d+)\s*\(inclusive\)')
    range_text_pattern = re.compile(r'Number\s+must\s+be\s+in\s+the\s+range\s+(\d+)\s+to\s+(\d+)', re.IGNORECASE)
    must_be = re.compile(r'must be greater than(?: or equal to)? (\w+)',re.IGNORECASE) ## Special case in which it can be equal to zero (?: or equal to)?
    less_than_pattern = re.compile(r'less than or equal to (\d+)', re.IGNORECASE)
    # Addition restriction with words: must be between
    between_text_pattern = re.compile(r'must\s+be\s+between\s+(\d+)\s+and\s+(\d+)', re.IGNORECASE)
    ## Minimum value is

    # Detect if the description mentions valid ports
    if "valid port number" in description.lower():
        is_port_number = True

    # Translate numeric words to whole numbers within the description
    description = description.lower()
    for word, num in word_to_num.items():
        description = description.replace(word, str(num))  # Replace words with their numeric equivalents

    # Detect ranges with "< x <" (e.g. 0 < x < 65536)
    range_match = range_pattern.search(description)
    if range_match:
        min_bound = int(range_match.group(1))
        max_bound = int(range_match.group(2))
        return min_bound, max_bound, is_port_number, is_other_number

    # Detect ranges with "1-65535 inclusive"
    inclusive_match = inclusive_range_pattern.search(description)
    if inclusive_match:
        min_bound = int(inclusive_match.group(1))
        max_bound = int(inclusive_match.group(2))
        return min_bound, max_bound, is_port_number, is_other_number

    # Detect ranges of the form "Number must be in the range 1 to 65535"
    range_text_match = range_text_pattern.search(description)
    if range_text_match:
        min_bound = int(range_text_match.group(1))
        max_bound = int(range_text_match.group(2))
        return min_bound, max_bound, is_port_number, is_other_number
        
    # Detect ranges of the form "must be between 0 and 100" y "...1 and 30". The range 1-30 is seconds and the range 0-100 represents priority "levels". Total: 22 restric
    between_text_match = between_text_pattern.search(description) 
    if between_text_match:
        min_bound = int(between_text_match.group(1))
        max_bound = int(between_text_match.group(2))
        is_other_number = True
        return min_bound, max_bound, is_port_number, is_other_number

    # Detectar expresiones simples de "greater than" o "less than"
    greater_than_match = must_be.search(description)
    less_than_match = less_than_pattern.search(description)

    if greater_than_match:
        # Convert if it is a numeric word
        min_bound = int(greater_than_match.group(1)) if greater_than_match.group(1).isdigit() else convert_word_to_num(greater_than_match.group(1))
        is_other_number = True

    if less_than_match:
        # Convert if it is a numeric word
        max_bound = int(less_than_match.group(1)) if less_than_match.group(1).isdigit() else convert_word_to_num(less_than_match.group(1))
        max_bound = max_bound + 1 # To take into account the equal to 
        is_other_number = True

    return min_bound, max_bound, is_port_number, is_other_number
    
def convert_to_uvl_constraints(feature_key, description, type_data):
    """
    Convert a feature description and type into a UVL constraint.

    Args:
        feature_key (str): The feature key name.
        description (str): Natural language description.
        type_data (str): Data type ("Boolean", "Integer", etc.).

    Returns:
        str or None: UVL constraint or None if not matched.
    """

    global count
    
    # Check if the description is a list
    if isinstance(description, list):
        description = " ".join(
            " ".join(sublist) if isinstance(sublist, list) else str(sublist) 
            for sublist in description
        )
    elif not isinstance(description, str):
        # If not a string, omit the description
        print(f"No hay descripcion de texto para: {feature_key}")
        return None

    uvl_rule = None  # Initialize as None for descriptions without valid rules
    # Extract limits if present
    min_bound, max_bound, is_port_number, is_other_number = extract_bounds(description)
    # Adjust patterns to generate valid UVL syntax according to data type
    if type_data == "Boolean" or type_data == "boolean":
        if "Number must be in the range" in description:
            feature_without_lastProperty = feature_key.rsplit('_', 1)[0]
            uvl_rule = f"{feature_without_lastProperty} => ({feature_key}_asInteger > 1 & {feature_key}_asInteger < 65535) | ({feature_key}_asString == 'IANA_SVC_NAME')" ## Ver como añadir ese formato
        elif "required when" in description or 'Required when' in description:
            const = extract_constraints_required_when(description, feature_key)
            uvl_rule = const
        elif "only if type" in description or "Must be set if type is" in description or "must be non-empty if and only if" in description or "field MUST be empty if" in description or "may be non-empty only if" in description: ## Agregado nuevo conjunto 15/11: Queue (10) 
            first_constraint = extract_constraints_if(description, feature_key)
            uvl_rule = first_constraint
        elif "selector can be used to match multiple param objects based on their labels" in description:
            constraint = extract_constraints_mutualy_exclusive(description, feature_key)
            print("Restricciones", constraint)
            uvl_rule = constraint
        elif "Note that this field cannot be" in description:
            constraint = extract_constraints_os_name(description, feature_key)
            uvl_rule = constraint
        elif "If the operator is" in description: ## add in description
            constraint = extract_constraints_operator(description, feature_key)
            uvl_rule = constraint
        elif "a least one of" in description or "Exactly one of" in description or "At least one of" in description:
            constraint = extract_constraints_least_one(description, feature_key)
            uvl_rule = constraint
        elif "resource access request" in description or "succeededIndexes specifies" in description or "Represents the requirement on the container" in description or "ResourceClaim object in the same namespace as this pod" in description or "datasetUUID is" in description:
            uvl_rule = extract_constraints_primary_or(description, feature_key)
        elif "conditions may not be" in description or "Details about a waiting" in description or "TCPSocket is NOT" in description:
            constraint = extract_constraints_multiple_conditions(description, feature_key)
            uvl_rule = constraint
        elif "template.spec.restartPolicy" in description in description:
            uvl_rule = extract_constraints_template_onlyAllowed(description, feature_key)
    elif type_data == "Integer" or type_data == "integer":
        if is_port_number:
            # If it is a port number, make sure to use the port limits
            min_bound = 1 if min_bound is None else min_bound
            max_bound = 65535 if max_bound is None else max_bound
            uvl_rule = f"{feature_key} > {min_bound} & {feature_key} < {max_bound}"
        elif min_bound is not None and max_bound is not None:
            uvl_rule = f"{feature_key} > {min_bound} & {feature_key} < {max_bound}"
        elif min_bound is not None:
            uvl_rule = f"{feature_key} > {min_bound}"
        elif max_bound is not None:
            uvl_rule = f"{feature_key} < {max_bound}"
        elif "Minimum value is" in description or "Value must be non-negative" in description or "minimum valid value for" in description or "in the range" in description:
            uvl_rule = extract_minimum_value(description, feature_key)
            if "in the range" in description:
                print("LA REGLA ES, ", uvl_rule)
    elif type_data == "" or type_data == "string":
        if 'conditions may not be' in description:
            constraint = extract_constraints_multiple_conditions(description, feature_key)
            print("It does?")
            uvl_rule = constraint
        elif 'indicates which one of' in description:
            constraint = extract_constraints_string_oneOf(description, feature_key)
            print("It works?")
            uvl_rule = constraint

    if uvl_rule is None: # If there is no match, we increment the invalid rules counter.
        count += 1

    return uvl_rule

# Routes of files
json_file_path = '../../resources/model_generation/descriptions_01.json'
output_file_path = '../../resources/model_generation/all_restrictions.txt'

def generar_constraintsDef(json_file_path):
    """
    Generate UVL constraints from a JSON file and return them.

    Args:
        json_file_path (str): Path to the JSON with feature metadata.

    Returns:
        list: List of UVL constraint strings.
    """

    global count
    features = load_json_features(json_file_path)
    uvl_rules = []

    if 'restrictions' in features:
        for restriction in features['restrictions']:
            if isinstance(restriction, dict) and 'feature_name' in restriction and 'description' in restriction and 'type_data' in restriction:
                feature_key = restriction['feature_name']
                desc = restriction['description']
                type_data = restriction['type_data']
                uvl_rule = convert_to_uvl_constraints(feature_key, desc, type_data)
                if uvl_rule:
                    uvl_rules.append(uvl_rule)
            else:
                print(f"Formato inesperado en la restricción: {restriction}")
    else:
        print("Error. Restricciones vacías o nulas")

    print(f"Hay {count} descripciones que no se pudieron transformar en restricciones UVL.")
    return uvl_rules

if __name__ == "__main__":
    # Main manual for get a file in txt with all the constraints uvl
    restrictions = generar_constraintsDef(json_file_path)

    # Save the constraints in the file
    with open(output_file_path, 'w', encoding='utf-8') as f:
        for rule in restrictions:
            f.write(f"{rule}\n")
            print(rule)
    print(f"UVL output saved to {output_file_path}")