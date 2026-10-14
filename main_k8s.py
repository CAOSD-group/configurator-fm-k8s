import argparse
import pathlib

#from uvengine import UVEngine

from flamapy.metamodels.configuration_metamodel.models import Configuration
from flamapy.metamodels.fm_metamodel.models import FeatureModel, Feature
from flamapy.metamodels.fm_metamodel.operations import FMFeatureAncestors
from flamapy.metamodels.fm_metamodel.transformations import UVLReader
from flamapy.metamodels.sharpsat_metamodel.transformations import FmToSharpSAT
from flamapy.metamodels.sharpsat_metamodel.operations import SharpSATSampling
from flamapy.metamodels.pysat_metamodel.models import PySATModel
from flamapy.metamodels.pysat_metamodel.transformations import FmToPysat
from flamapy.metamodels.pysat_metamodel.operations import PySATSampling, PySATSatisfiableConfiguration, PySATMinimumConfiguration



def check_config_sat(sat_model: PySATModel, config: Configuration) -> bool:
    """Check if a given configuration is satisfiable with respect to the SAT model.
    
    Args:
        sat_model (PySATModel): The PySAT model to check against.
        config (Configuration): The configuration to check.
    
    Returns:
        bool: True if the configuration is satisfiable, False otherwise.
    """
    sat_config_op = PySATSatisfiableConfiguration()
    sat_config_op.set_configuration(config)
    return sat_config_op.execute(sat_model).get_result()


def get_feature_value(feature: Feature, config: Configuration) -> Any:
    value = None
    if feature.name in config.elements:
        value = config.get_value(feature.name)
    if value is None:
        attr = next((attr for attr in feature.get_attributes() if attr.name == 'default'), None)
        if attr is not None:
            if attr.get_default_value() is not None:
                value = attr.get_default_value()
            else:
                value = True
    return value



def generate_default_configuration(fm: FeatureModel, sat_model: PySATModel, partial_config: Configuration) -> Configuration:
    """Generate a default configuration based on the feature model and a partial configuration.
    
    This function fills in the undecided elements of the partial configuration with default values
    from the feature model, resulting in a complete configuration.
    
    Args:
        fm (FeatureModel): The feature model to reference for default values.
        partial_config (Configuration): The partial configuration to complete.
    
    Returns:
        Configuration: A complete configuration with all elements decided.
    """
    elements: dict[Any, Any] = partial_config.elements.copy()
    features = []
    # Bottom-up approach: for each selected feature, ensure all its ancestors are also selected
    for feature_name in partial_config.get_selected_elements():
        feature = fm.get_feature_by_name(feature_name)
        features.append(feature)
        if feature is None:
            raise ValueError(f"Feature '{feature_name}' not found in the feature model.")
        ancestors_op = FMFeatureAncestors()
        ancestors_op.set_feature(feature)
        ancestors_features = ancestors_op.execute(fm).get_result()
        for ancestor in ancestors_features:
            if ancestor.name not in elements:
                features.append(ancestor)
                if ancestor.is_boolean():
                    elements[ancestor.name] = True
                else:
                    elements[ancestor.name] = next((attr.get_default_value() for attr in ancestor.get_attributes() if attr.name == 'default'), None)
    # Top-down approach: for each feature in the feature model, if it is not in the configuration, set its default value
    while features:
        feature = features.pop()
        for relation in feature.get_relations():
            if relation.is_mandatory():
                value = get_feature_value(relation.children[0], partial_config)
                value = True if value is None or not value else value
                elements[relation.children[0].name] = value
                if not check_config_sat(sat_model, Configuration(elements)):
                    elements.pop(relation.children[0].name, None)
                else:
                    features.append(relation.children[0])
            elif relation.is_optional():
                value = get_feature_value(relation.children[0], partial_config)
                if value is not None and value:
                    elements[relation.children[0].name] = value
                    if not check_config_sat(sat_model, Configuration(elements)):
                        elements.pop(relation.children[0].name, None)
                    else:
                        features.append(relation.children[0])
            elif relation.is_alternative():
                for child in relation.children:
                    if any(attr.name == 'default' for attr in child.get_attributes()):
                        value = get_feature_value(child, partial_config)
                        value = True if value is None or not value else value
                        elements[child.name] = value
                        if not check_config_sat(sat_model, Configuration(elements)):
                            elements.pop(child.name, None)
                        else:
                            features.append(child)
                        break
            elif relation.is_or() or relation.is_cardinal():
                for child in relation.children:
                    if any(attr.name == 'default' for attr in child.get_attributes()):
                        value = get_feature_value(child, partial_config)
                        value = True if value is None or not value else value
                        elements[child.name] = value
                        if not check_config_sat(sat_model, Configuration(elements)):
                            elements.pop(child.name, None)
                        else:
                            features.append(child)
    complete_config = Configuration(elements)
    complete_config.set_full(True)
    return complete_config


def generate_manifest(fm: FeatureModel, partial_config: Configuration) -> str:
    # Features agrupadas por su nivel en la jerarquía del feature model
    levels_features: dict[int, list[Feature]] = {}
    for feature_name in partial_config.get_selected_elements():
        feature = fm.get_feature_by_name(feature_name)
        if feature is None:
            raise ValueError(f"Feature '{feature_name}' not found in the feature model.")
        ancestors_op = FMFeatureAncestors()
        ancestors_op.set_feature(feature)
        level = len(ancestors_op.execute(fm).get_result()) - 1
        levels_features.setdefault(level, []).append(feature)

    # Cada entrada: (nivel, tiene_valor, línea)
    config_entries: list[tuple[int, bool, str]] = []
    for level, features in levels_features.items():
        for feature in features:
            key = next((attr.get_default_value() for attr in feature.get_attributes()
                        if attr.name == 'configKey'), None)
            if not feature.is_boolean():
                value = partial_config.get_value(feature.name)
            else:
                value = next((attr.get_default_value() for attr in feature.get_attributes()
                              if attr.name == 'configValue'), None)

            if value is None:
                for child in levels_features.get(level + 1, []):
                    if child.get_parent() == feature:
                        child_key, child_value = get_config_key_value(child)
                        if child_key is None and child_value is not None:
                            value = child_value
                            break

            value = value if value is not None else ''

            if key is not None:
                has_value = value != ''
                line = f"{'  ' * (level - 1)}{key}: {value}"
                config_entries.append((level, has_value, line))

    # Orden: nivel ascendente; dentro del mismo nivel, primero las que tienen
    # valor (not has_value = False -> va antes) y después las que solo tienen key
    config_entries.sort(key=lambda e: (e[0], not e[1]))

    return "\n".join(line for _, _, line in config_entries)


def get_config_key_value(feature: Feature) -> tuple[str, Any]:
    """Get the configuration key and value for a given feature from its attributes."""
    key = next((attr.get_default_value() for attr in feature.get_attributes() if attr.name == 'configKey'), None)
    value = next((attr.get_default_value() for attr in feature.get_attributes() if attr.name == 'configValue'), None)
    return key, value


def merge_values(min_config: Configuration, full_config: Configuration) -> Configuration:
    """Devuelve min_config conservando los valores no booleanos de full_config."""
    elements = {}
    for name, min_value in min_config.elements.items():
        original = full_config.elements.get(name)
        # Si la feature tenía un valor no booleano en full_config, se mantiene
        if original is not None and not isinstance(original, bool):
            elements[name] = original
        else:
            elements[name] = min_value
    return Configuration(elements)


def main() -> None:
    fm = UVLReader('case_studies/kubernetes/feature_model/KubernetesFM.uvl').transform()
    print(f'Features: {len(fm.get_features())}')

    my_config = Configuration({'io_k8s_api_apps_v1_Deployment_spec_strategy_type': True,
                               'io_k8s_api_apps_v1_Deployment_spec_replicas': 3,
                               })
    print(my_config)
    pysat_model = FmToPysat(fm).transform()

    full_config = Configuration({})
    min_config = Configuration(my_config.elements.copy())
    while full_config != min_config:
        full_config = generate_default_configuration(fm, pysat_model, min_config)
        print(f'Full config: {len(full_config.get_selected_elements())} elements')
        min_config_op = PySATMinimumConfiguration()
        min_config_op.set_partial_configuration(full_config)
        min_config = min_config_op.execute(pysat_model).get_result()
        min_config = merge_values(min_config, full_config)
        print(f'Min config: {len(min_config.get_selected_elements())} elements')
    
    print("=== Full Configuration ===")
    print(full_config)
    print(f'Satisfiable full_config: {check_config_sat(pysat_model, full_config)}')

    print("=== Artifact Configuration ===")
    artifact_config = generate_manifest(fm, full_config)
    print(artifact_config)

    raise
    # Minimum configuration
    min_config_op = PySATMinimumConfiguration()
    min_config_op.set_partial_configuration(full_config)
    min_config = min_config_op.execute(pysat_model).get_result()
    print("=== Minimum Configuration ===")
    print(min_config)
    print(f'Satisfiable min full config: {check_config_sat(pysat_model, min_config)}')

    full_config = generate_default_configuration(fm, pysat_model, min_config)
    print("=== Full Configuration ===")
    print(full_config)
    print(f'Satisfiable full_config: {check_config_sat(pysat_model, full_config)}')

    # Minimum configuration
    min_config_op = PySATMinimumConfiguration()
    min_config_op.set_partial_configuration(full_config)
    min_config = min_config_op.execute(pysat_model).get_result()
    print("=== Minimum Configuration ===")
    print(min_config)
    print(f'Satisfiable min full config: {check_config_sat(pysat_model, min_config)}')

    

    # sampling_op = PySATSampling()
    # sampling_op.set_sample_size(1)
    # sampling_op.set_partial_configuration(my_config)
    # rnd_configs = sampling_op.execute(pysat_model).get_result()
    # print(rnd_configs)

    print("=== Artifact Configuration ===")
    artifact_config = generate_manifest(fm, full_config)
    print(artifact_config)


if __name__ == '__main__':
    main()

