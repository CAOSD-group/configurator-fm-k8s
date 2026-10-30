#!/usr/bin/env python3
"""Generate a YAML manifest from a UVL feature model and a partial configuration.

The script takes a feature model (``.uvl``) and one or more partial configurations
(``.json`` / ``.uvl.json``), completes the configuration with the defaults declared in the
model, and writes the resulting manifest to a ``.yaml`` file. Optionally, the manifest is
annotated with comments describing the variants of each variation point.

Pipeline
--------
1. Load the feature model and merge the partial configuration files.
2. Complete the configuration upwards: every selected feature gets all its ancestors.
3. Complete the configuration downwards. Every selection is validated with a SAT solver, so
   the configuration never violates the constraints of the model.

   * Compact mode (default): the defaults of the model are applied (mandatory children,
     abstract optional children and ``default`` children) and, below the features of the
     initial partial configuration, the optional features that show a value (``configKey``
     plus a ``configValue``) are added. The leaves are not systematically reached.
   * ``--expand-to-leaves``: descend to the leaves of the selected features. With
     ``--expand-scope resource`` the traversal starts at the resource-definition
     feature (e.g. Pod) inferred from those features instead, without expanding
     unrelated Kubernetes resource definitions.
4. Minimize the configuration (MaxSAT) and repeat steps 2-3 until a fixed point is reached.
5. Render the manifest from the ``configKey`` / ``configValue`` attributes. Arrays
   and dynamic maps are rebuilt from structured JSON using UVL serialization metadata.
   Optional ``--collection-mode skeleton`` creates one *synthetic example* for
   uninstantiated arrays of objects in --expand-to-leaves mode. It is a schema
   inspection aid, not a deployable or necessarily Kubernetes-valid manifest.
   With
   ``--variability-comments`` the variation points are annotated: a block with the selected
   and alternative variants for alternative groups, and an inline description for optional
   and or variants.
6. Write the manifest to a YAML file.

Feature attributes understood by the script
-------------------------------------------
``configKey``
    Key emitted in the manifest for the feature. Features without it produce no line.
``configValue``
    Value emitted for a boolean feature. A child feature without ``configKey`` but with
    ``configValue`` propagates its value to the key of its parent (e.g. the selected variant
    of an alternative group). The search goes through abstract children recursively, so a
    feature with a ``configKey`` but no value takes the ``configValue`` of the next
    non-abstract descendant. A selected boolean feature with a ``configKey`` and no value at
    all is written as ``key: true``; if it was explicitly deselected in the given
    configuration it is written as ``key: false``.
``default``
    Flag (no value). Marks the preferred feature: an optional feature that is activated, or
    the child chosen by default in an alternative/or group. Default features are always tried
    before their siblings.
``defaultValue``
    Default value of a non-boolean feature (number or string).
``doc``
    Free-text documentation, used for the variability comments (see
    ``--variability-comments``): alternative groups get a block listing the selected and the
    alternative variants; optional, or and cardinality variants get their description as an
    inline comment on their own line.

Usage
-----
.. code-block:: console

    python main_k8s05.py model.uvl partial_config.json \\
        -o out/deployment.yaml --variability-comments

Requires Python 3.9+ and the ``flamapy``, ``flamapy-fm`` and ``flamapy-sat`` packages.
"""

import argparse
import json

import yaml
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Collection, Iterator, NamedTuple, Optional, Sequence

from pysat.solvers import Solver

from flamapy.core.exceptions import FlamaException
from flamapy.metamodels.configuration_metamodel.models import Configuration
from flamapy.metamodels.configuration_metamodel.transformations import (
    ConfigurationJSONReader,
    UVLSJSONReader,
)
from flamapy.metamodels.fm_metamodel.models import Feature, FeatureModel
from flamapy.metamodels.fm_metamodel.operations import FMVariationPoints
from flamapy.metamodels.fm_metamodel.transformations import UVLReader
from flamapy.metamodels.pysat_metamodel.models import PySATModel
from flamapy.metamodels.pysat_metamodel.operations import PySATMinimumConfiguration
from flamapy.metamodels.pysat_metamodel.transformations import FmToPysat

logger = logging.getLogger(__name__)


@contextmanager
def log_step(message: str, start_icon: str = '⏳', done_icon: str = '✅') -> Iterator[None]:
    """Log the start and the end (with elapsed time) of a potentially slow operation."""
    logger.info('%s %s...', start_icon, message)
    start = time.perf_counter()
    try:
        yield
    except Exception:
        logger.error('❌ %s failed', message)
        raise
    logger.info('%s %s done (%.2fs)', done_icon, message, time.perf_counter() - start)


# ---------------------------------------------------------------------------
# Feature attribute helpers
# ---------------------------------------------------------------------------
def _get_attr(feature: Feature, name: str) -> Any:
    """Return the attribute of ``feature`` called ``name``, or ``None`` if it has none."""
    return next((a for a in feature.get_attributes() if a.name == name), None)


def is_default(feature: Feature) -> bool:
    """Return whether the feature is selected by default (it carries the ``default`` flag)."""
    return _get_attr(feature, 'default') is not None


def get_default_value(feature: Feature) -> Any:
    """Return the default value of a non-boolean feature, or ``None`` if it has none."""
    # The legacy format ``default XX`` is still accepted as a default value.
    for name in ('defaultValue', 'default'):
        attr = _get_attr(feature, name)
        if attr is not None and attr.get_default_value() is not None:
            return attr.get_default_value()
    return None


def get_config_key_value(feature: Feature) -> tuple[Optional[str], Any]:
    """Return the ``(configKey, configValue)`` pair of a feature (``None`` when missing)."""
    key = _get_attr(feature, 'configKey')
    value = _get_attr(feature, 'configValue')
    return (key.get_default_value() if key else None,
            value.get_default_value() if value else None)


def has_descendant_config_value(feature: Feature) -> bool:
    """Return whether a child, or a sub-child reached through abstract children, has a
    ``configValue``."""
    for child in feature.get_children():
        if get_config_key_value(child)[1] is not None:
            return True
        if child.is_abstract and has_descendant_config_value(child):
            return True
    return False


def shows_value(feature: Feature) -> bool:
    """Return whether the feature has a ``configKey`` and some ``configValue`` to show.

    The value is its own ``configValue`` or one found in its descendants (see
    ``has_descendant_config_value``).
    """
    key, value = get_config_key_value(feature)
    return key is not None and (value is not None or has_descendant_config_value(feature))


def iter_ancestors(feature: Feature) -> Iterator[Feature]:
    """Yield the ancestors of a feature, from its parent up to the root."""
    current = feature.get_parent()
    while current is not None:
        yield current
        current = current.get_parent()


def is_in_scope(feature: Feature, scope: Collection[str]) -> bool:
    """Return whether ``feature`` is one of the ``scope`` features or lies below one of them."""
    current: Optional[Feature] = feature
    while current is not None:
        if current.name in scope:
            return True
        current = current.get_parent()
    return False


def get_doc(feature: Feature) -> Optional[str]:
    """Return the ``doc`` attribute of a feature collapsed into a single line, if any."""
    attr = _get_attr(feature, 'doc')
    if attr is None or attr.get_default_value() is None:
        return None
    # Documentation ends up inside YAML comments: newlines would break them.
    return ' '.join(str(attr.get_default_value()).split()) or None


def get_feature(fm: FeatureModel, name: str) -> Feature:
    """Return the feature called ``name`` or raise ``ValueError`` if it does not exist."""
    feature = fm.get_feature_by_name(name)
    if feature is None:
        raise ValueError(f"Feature '{name}' not found in the feature model.")
    return feature


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def load_configurations_from_file(configs_path: list[str]) -> Configuration:
    """Read and merge partial configurations from JSON files.

    Files are merged in order, so later files override earlier ones.

    Supported formats:
        * ``*.uvl.json``: UVLS JSON configuration.
        * ``*.json``: plain JSON configuration. If it actually is a UVLS configuration
          (it has a top-level ``config`` key) it is read as such.

    Raises:
        ValueError: If a file has an unsupported extension.
    """
    elements: dict[Any, Any] = {}
    for filepath in configs_path:
        if filepath.endswith('.uvl.json'):
            config = UVLSJSONReader(filepath).transform()
        elif filepath.endswith('.json'):
            config = ConfigurationJSONReader(filepath).transform()
            if 'config' in config.elements:
                # UVLS configuration saved with a plain ``.json`` extension.
                config = UVLSJSONReader(filepath).transform()
        else:
            raise ValueError(f'Unsupported configuration file format: {filepath}')
        elements.update(config.elements)
    return Configuration(elements)


def get_serialization_attr(feature: Feature, name: str) -> Optional[str]:
    """Read UVL serialization metadata already generated by convert04.py."""
    attr = _get_attr(feature, name)
    return attr.get_default_value() if attr is not None else None


def load_configurations_with_collections(
    fm: FeatureModel, config_paths: list[str]
) -> tuple[Configuration, dict[str, Any]]:
    """Load ordinary UVL configurations and preserve repeated array/map entries.

    UVLS configurations generated by MetadataMapper can contain arrays as lists of
    dictionaries indexed by UVL feature names. FlamaPy's boolean SAT model has no
    per-occurrence variables, so list/map entries are kept separately for rendering.
    They are not passed as scalar values to the SAT solver.

    Ordinary configurations are still delegated to the original FlamaPy readers.
    """
    features = {feature.name: feature for feature in fm.get_features()}
    # The presence of a collection payload selects the new reader for all files.
    def has_structures(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        for name, child in value.items():
            feature = features.get(name)
            if (feature is not None and
                    get_serialization_attr(feature, 'serializationKind') in {'array', 'map'} and
                    isinstance(child, (dict, list))):
                return True
            if has_structures(child):
                return True
        return False

    payloads: list[dict[str, Any]] = []
    for path in config_paths:
        if not path.endswith('.json'):
            raise ValueError(f'Unsupported configuration file format: {path}')
        with open(path, encoding='utf-8') as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            raise ValueError(f'The configuration file must contain a JSON object: {path}')
        root = payload.get('config', payload)
        if not isinstance(root, dict):
            raise ValueError(f'Expected an object in the config field of {path}')
        payloads.append(root)

    if not any(has_structures(data) for data in payloads):
        return load_configurations_from_file(config_paths), {}

    elements: dict[str, Any] = {}
    collections: dict[str, Any] = {}

    def flatten(data: dict[str, Any], context: Optional[Feature] = None) -> None:
        for name, value in data.items():
            feature = features.get(name)
            if feature is None:
                raise ValueError(f'Unknown feature in configuration JSON: {name}')
            if context is not None:
                parent = feature.get_parent()
                valid = False
                while parent is not None:
                    if parent.name == context.name:
                        valid = True
                        break
                    parent = parent.get_parent()
                if not valid:
                    raise ValueError(f'Feature {name} is not a descendant of {context.name}')
            kind = get_serialization_attr(feature, 'serializationKind')
            if kind in {'map', 'array'} and isinstance(value, (dict, list)):
                elements[name] = True
                collections[name] = value
                # Entries are kept verbatim. SAT doesn't model repeated instances.
                continue
            if isinstance(value, dict):
                elements[name] = True
                flatten(value, feature)
            elif isinstance(value, list):
                raise ValueError(f'Unexpected list in {name}: missing serializationKind')
            else:
                elements[name] = value

    for data in payloads:
        flatten(data)

    return Configuration(elements), collections


def is_selected(value: Any) -> bool:
    """Return whether a configuration value means "selected" (``0`` counts as a value)."""
    return value is not None and value is not False


def selected_items(config: Configuration) -> dict[str, Any]:
    """Return the ``{feature name: value}`` entries of the selected features."""
    return {name: value for name, value in config.elements.items() if is_selected(value)}


def value_for(feature: Feature, config: Configuration) -> Any:
    """Return the value to assign to a feature when it is selected.

    Priority: value given in ``config`` > ``defaultValue`` (non-boolean features) > ``True``.
    """
    value = config.elements.get(feature.name)
    if is_selected(value) and (feature.is_boolean() or value is not True):
        return value
    if not feature.is_boolean():
        default = get_default_value(feature)
        if default is not None:
            return default
    return True


def to_boolean_config(config: Configuration) -> Configuration:
    """Return a boolean view of a configuration: selected features are ``True``.

    SAT operations only understand booleans and use the truthiness of the values, so a
    non-boolean feature with value ``0`` or ``''`` would be taken as deselected. This view
    avoids that.
    """
    return Configuration({name: is_selected(value) for name, value in config.elements.items()})


class SatChecker:
    """Incremental satisfiability checker for (partial) configurations.

    The clauses of the model are loaded once and every check uses solver assumptions, which
    is much faster than building a new solver for each check. A feature is assumed selected
    when its value is not ``None``/``False`` (so ``0`` counts as selected) and deselected
    otherwise; features not decided in the configuration are left free.
    """

    def __init__(self, sat_model: PySATModel) -> None:
        self._variables = sat_model.variables
        self._solver = Solver(name='glucose3')
        for clause in sat_model.get_all_clauses():
            self._solver.add_clause(clause)

    def is_satisfiable(self, elements: dict[Any, Any]) -> bool:
        """Return whether the model has a valid configuration extending ``elements``."""
        assumptions = []
        for name, value in elements.items():
            variable = self._variables.get(name)
            if variable is None:  # feature unknown to the SAT model
                return False
            assumptions.append(variable if is_selected(value) else -variable)
        return self._solver.solve(assumptions=assumptions)

    def close(self) -> None:
        """Release the solver."""
        self._solver.delete()

    def __enter__(self) -> 'SatChecker':
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def merge_values(min_config: Configuration, full_config: Configuration) -> Configuration:
    """Return ``full_config`` plus the features that the minimization had to add.

    Priority of the values: given configuration > ``defaultValue`` > minimization. Since
    ``full_config`` is imposed as hard constraints on the minimization, the minimization can
    only *add* features forced by the constraints (as plain ``True``): it never replaces
    values (e.g. ``replicas: 3``) nor removes or deselects any feature of ``full_config``.
    """
    elements = dict(full_config.elements)
    for name, value in min_config.elements.items():
        if name not in elements:
            elements[name] = value
    return Configuration(elements)


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------
def generate_default_configuration(fm: FeatureModel,
                                   checker: SatChecker,
                                   partial_config: Configuration,
                                   scope: Optional[Collection[str]] = None,
                                   expand_to_leaves: bool = False) -> Configuration:
    """Complete a partial configuration with the default values of the feature model.

    1. Bottom-up: every selected feature gets all its ancestors selected.
    2. Top-down: for every selected feature its children are visited, trying the ``default``
       ones first.

       * Compact mode (``expand_to_leaves=False``): mandatory children, abstract optional
         children and ``default`` children are selected (one child at most in an
         alternative group). Additionally, inside the ``scope`` the optional children that
         show a value in the manifest (``shows_value``) are selected.
       * Leaves mode (``expand_to_leaves=True``): inside the ``scope`` the descent reaches the
         leaves, whatever the kind of feature (optional, abstract, with or without
         ``configKey``/``configValue``): all children of mandatory, optional and or/cardinality
         groups, and one child of each alternative group (the ``default`` one, or else the
         first one that keeps the configuration satisfiable). Outside the ``scope`` the
         compact rules apply.

    Every selection is validated with the SAT solver and discarded if it makes the
    configuration unsatisfiable. Explicit decisions in ``partial_config`` (including
    deselections) are never overridden.

    Args:
        scope: Names of the features from which the model is explored further (these
            features and everything below them). Defaults to the features selected in
            ``partial_config``.
        expand_to_leaves: Descend to the leaves inside the ``scope`` (see above).
    """
    if scope is None:
        scope = set(selected_items(partial_config))
    elements: dict[Any, Any] = partial_config.elements.copy()
    pending: list[Feature] = []

    def try_select(feature: Feature) -> bool:
        """Select an undecided feature if the configuration stays satisfiable.

        Returns whether the feature ends up selected.
        """
        if feature.name in elements:
            return is_selected(elements[feature.name])
        elements[feature.name] = value_for(feature, partial_config)
        if checker.is_satisfiable(elements):
            pending.append(feature)
            return True
        del elements[feature.name]
        return False

    # Bottom-up (an empty partial configuration starts from the root feature).
    for name in list(selected_items(partial_config)) or [fm.root.name]:
        feature = get_feature(fm, name)
        elements[name] = value_for(feature, partial_config)
        pending.append(feature)
        for ancestor in iter_ancestors(feature):
            if ancestor.name not in elements:
                elements[ancestor.name] = value_for(ancestor, partial_config)
                pending.append(ancestor)

    # Top-down
    while pending:
        feature = pending.pop()
        in_scope = is_in_scope(feature, scope)
        deep = in_scope and expand_to_leaves
        for relation in feature.get_relations():
            children = sorted(relation.children, key=lambda c: not is_default(c))  # defaults first
            if relation.is_mandatory():
                for child in children:
                    try_select(child)
            elif relation.is_optional():
                for child in children:
                    if (deep or child.is_abstract or is_default(child)
                            or (in_scope and shows_value(child))):
                        try_select(child)
            elif relation.is_alternative():
                if any(is_selected(elements.get(c.name)) for c in children):
                    continue  # the xor group is already decided
                for child in children:
                    if (deep or is_default(child)) and try_select(child):
                        break  # xor: a single child
            else:  # or / cardinality
                for child in children:
                    if deep or is_default(child):
                        try_select(child)

    complete_config = Configuration(elements)
    complete_config.set_full(True)
    return complete_config


def resolve_expansion_scope(fm: FeatureModel,
                            partial_config: Configuration,
                            mode: str = 'selected') -> set[str]:
    """Return starting features for downward expansion.

    selected: exactly those initially selected in the partial config (original mode).
    resource: nearest child of the global Kubernetes root containing all selected
              features, e.g. io_k8s_api_core_v1_Pod. This avoids expanding all 81k
              features from the global root, and avoids scope anchored at leaf values.

    Requiring one resource guards against accidentally expanding the entire FM or
    mixing unrelated roots in a single Kubernetes manifest.
    """
    selected = set(selected_items(partial_config))
    if mode == 'selected':
        return selected
    if mode != 'resource':
        raise ValueError(f'Unknown expansion scope: {mode}')
    if not selected:
        raise ValueError('--expand-scope resource requires at least one selected feature')

    resources: set[str] = set()
    for name in selected:
        feature = get_feature(fm, name)
        if feature is fm.root or feature.name == fm.root.name:
            raise ValueError('Cannot use the global FM root with --expand-scope resource')
        current = feature
        while current.get_parent() is not None and current.get_parent().name != fm.root.name:
            current = current.get_parent()
        if current.get_parent() is None:
            raise ValueError(f'Feature {name!r} is not under the global FM root')
        resources.add(current.name)

    if len(resources) != 1:
        raise ValueError('All selected features must belong to a single resource for '
                         f'--expand-scope resource; got: {", ".join(sorted(resources))}')
    return resources


def complete_and_minimize(fm: FeatureModel,
                          sat_model: PySATModel,
                          partial_config: Configuration,
                          expand_to_leaves: bool = False,
                          expand_scope: str = 'selected') -> Configuration:
    """Alternate "complete with defaults" and "minimize" until a fixed point is reached.

    The minimization can only add the features forced by the constraints; the values of the
    given configuration and the ``defaultValue`` ones are never replaced (see
    ``merge_values``). The loop therefore ends as soon as the minimization adds nothing.

    Returns:
        The complete configuration (it has the same selected features as its minimization).

    Raises:
        ValueError: If no valid configuration extends ``partial_config``.
    """
    # Keep the scope fixed across minimization iterations. The original behavior
    # is preserved unless the user opts into resource-root expansion explicitly.
    scope = (resolve_expansion_scope(fm, partial_config, expand_scope)
             if expand_to_leaves else set(selected_items(partial_config)))
    logger.info('🧭 Expanding from %d scope feature(s) (%s, scope=%s): %s', len(scope),
                'down to the leaves' if expand_to_leaves else 'defaults and config values',
                expand_scope if expand_to_leaves else 'selected',
                ', '.join(sorted(scope)[:10]) + (' ...' if len(scope) > 10 else ''))
    if fm.root.name in scope:
        logger.warning('⚠️  The root feature is in the initial configuration: the whole model '
                       'is in the expansion scope.')

    min_config = Configuration(partial_config.elements.copy())
    previous: Optional[dict[str, Any]] = None
    iteration = 0
    with SatChecker(sat_model) as checker:
        while True:
            iteration += 1
            logger.info('🔁 Iteration %d', iteration)
            with log_step('Completing the configuration (ancestors, defaults, expansion)', '🧩'):
                full_config = generate_default_configuration(fm, checker, min_config, scope,
                                                             expand_to_leaves)

            with log_step('Computing the minimum configuration (MaxSAT)', '📉'):
                min_op = PySATMinimumConfiguration()
                min_op.set_partial_configuration(to_boolean_config(full_config))
                result = min_op.execute(sat_model).get_result()
                if result is None:
                    raise ValueError('The partial configuration is not satisfiable.')
                ignored = set(selected_items(full_config)) - set(selected_items(result))
                if ignored:
                    logger.warning('⚠️  The minimization ignored %d selected feature(s) '
                                   '(unknown to the SAT model?): %s', len(ignored),
                                   ', '.join(sorted(ignored)[:10]))
                min_config = merge_values(result, full_config)

            full_selected, min_selected = selected_items(full_config), selected_items(min_config)
            logger.info('📊 Full config: %d features | Min config: %d features',
                        len(full_selected), len(min_selected))
            if full_selected == min_selected:
                logger.info('🎯 Fixed point reached after %d iteration(s)', iteration)
                return full_config
            if min_selected == previous:  # safety net: no progress between iterations
                logger.warning('⚠️  No progress between iterations; stopping with the last '
                               'complete configuration.')
                return full_config
            previous = min_selected


# ---------------------------------------------------------------------------
# Variability comments
# ---------------------------------------------------------------------------
def format_value(value: Any) -> str:
    """Format a configuration value as a YAML scalar (booleans in lower case)."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def describe_variant(variant: Feature) -> str:
    """Return a human readable label for a variant, followed by its ``doc`` if any.

    A variant without ``configKey`` is a plain value (e.g. ``RollingUpdate``): its label is
    its ``configValue`` (or its name). A variant with a ``configKey`` adds a line to the
    manifest: its label is ``key`` or ``key: value``.
    """
    key, config_value = get_config_key_value(variant)
    if key is None:
        label = format_value(config_value) if config_value is not None else variant.name
    elif config_value is not None:
        label = f'{key}: {format_value(config_value)}'
    else:
        label = key
    doc = get_doc(variant)
    return f'{label} ({doc})' if doc else label


def _find_anchor(feature: Feature) -> Optional[Feature]:
    """Return the closest feature (itself or an ancestor) that produces a manifest line."""
    current: Optional[Feature] = feature
    while current is not None:
        if get_config_key_value(current)[0] is not None:
            return current
        current = current.get_parent()
    return None


class VariabilityComments(NamedTuple):
    """Comments to add to the manifest, indexed by feature name."""

    blocks: dict[str, list[str]]  # lines placed before the line of the feature (no leading '# ')
    inline: dict[str, str]  # description placed at the end of the line of the feature


def build_variability_comments(fm: FeatureModel,
                               full_config: Configuration,
                               scope: Optional[Collection[str]] = None) -> VariabilityComments:
    """Describe the variants of the variation points present in the full configuration.

    A variation point is a feature with at least one non-mandatory child (see
    ``FMVariationPoints``). The text comes from the ``doc`` attribute of the features:

    * Alternative groups: a block with the variation point and its variants, the selected
      ones (according to ``full_config``) first, placed before the line of the variation
      point (or of its closest ancestor with a ``configKey``).
    * Optional, or and cardinality groups: no block; the description of each selected
      variant is added as an inline comment at the end of its own line.

    If ``scope`` (the names of the given features) is provided, only the variation points
    that are one of those features or lie below them, plus the parents of those features, are
    described.
    """
    selected = selected_items(full_config)
    scope_parents: set[str] = set()
    for name in scope or ():
        parent = get_feature(fm, name).get_parent()
        if parent is not None:
            scope_parents.add(parent.name)
    blocks: dict[str, list[str]] = {}
    inline: dict[str, str] = {}
    for vp in FMVariationPoints().execute(fm).get_result():
        if vp.name not in selected:
            continue
        if scope is not None and vp.name not in scope_parents and not is_in_scope(vp, scope):
            continue
        for relation in vp.get_relations():
            if relation.is_mandatory():
                continue
            if relation.is_alternative():
                anchor = _find_anchor(vp)
                if anchor is None:
                    logger.debug("Variation point '%s' has no configKey in its ancestry; "
                                 "its comment is skipped.", vp.name)
                    continue
                block = [f'[Variability Point: {get_doc(vp) or vp.name}]']
                for variant in sorted(relation.children, key=lambda v: v.name not in selected):
                    kind = 'Selected' if variant.name in selected else 'Alternative'
                    block.append(f'- {kind} Variant: {describe_variant(variant)}')
                blocks.setdefault(anchor.name, []).extend(block)
            else:  # optional, or, cardinality
                for variant in relation.children:
                    doc = get_doc(variant)
                    if doc and variant.name in selected:
                        inline[variant.name] = doc
    return VariabilityComments(blocks, inline)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def manifest_value(feature: Feature, config: Configuration, selected: dict[str, Any]) -> Any:
    """Return the value emitted next to the key of ``feature`` (``None`` for key-only lines).

    Boolean features use their ``configValue``. Non-boolean features use the value in the
    configuration (given by the user or ``defaultValue``); if they were just selected without
    a real value (a bare ``True``), their ``configValue`` is used instead.
    If there is still no value, the ``configValue`` of a selected child without ``configKey``
    is used, searching recursively through selected abstract children.
    """
    _, config_value = get_config_key_value(feature)
    if feature.is_boolean():
        value = config_value
    else:
        value = config.get_value(feature.name)
        if value is True:  # placeholder: selected, but no real value assigned
            value = config_value
    if value is None:
        value = _child_config_value(feature, selected)
    return value


def _child_config_value(feature: Feature, selected: dict[str, Any]) -> Any:
    """Find the ``configValue`` of a selected keyless child (through abstract children)."""
    for child in feature.get_children():
        if child.name not in selected:
            continue
        child_key, child_value = get_config_key_value(child)
        if child_key is not None:
            continue
        if child_value is not None:
            return child_value
        if child.is_abstract:
            nested = _child_config_value(child, selected)
            if nested is not None:
                return nested
    return None


def _yaml_shape(feature: Feature) -> Optional[str]:
    """Return the YAML container shape represented by a selected UVL feature.

    Feature-selection booleans are not necessarily YAML boolean values. In
    particular, object-valued $ref properties with no selected children must
    render as empty objects, not as ``true``.
    """
    kind = get_serialization_attr(feature, 'serializationKind')
    if kind in {'object', 'map', 'array'}:
        return kind
    if kind in {'scalar', 'union'}:
        return None

    schema_type = (get_serialization_attr(feature, 'schema_type')
                   or get_serialization_attr(feature, 'schemaType'))
    if schema_type in {'object', 'array'}:
        return schema_type
    if schema_type in {'string', 'integer', 'number', 'boolean', 'null'}:
        return None

    # The converter may represent object $refs without serializationKind.
    if get_serialization_attr(feature, 'schemaRef') and feature.get_children():
        return 'object'
    return None


def _materialize_required_values(
    data: dict[str, Any], fm: FeatureModel, config: Configuration
) -> tuple[dict[str, Any], list[str]]:
    """Materialize missing *selected* typed values and mandatory descendants.

    Policy is based on the UVL relation, schema_type, defaultValue, and bounds.
    Generated values are experimental witnesses, NOT Kubernetes defaults and
    NOT proof that the manifest satisfies all domain-specific constraints.
    A dynamic map never receives an invented map key. Required fields with an
    unknown scalar type are reported, not silently converted into ``true``.
    """
    import re

    selected = selected_items(config)
    report: list[str] = []
    reserved: dict[tuple[str, ...], set[Any]] = {}

    def attr(feature: Feature, *names: str) -> Any:
        for name in names:
            result = get_serialization_attr(feature, name)
            if result is not None:
                return result
        return None

    def value_type(feature: Feature) -> str:
        typ = str(attr(feature, 'schema_type', 'schemaType') or '').lower()
        if typ:
            return typ
        # The existing UVL converter turns a few scalar fields into Boolean
        # selection nodes with a mandatory, keyless String/Integer helper.
        # Preserve their original scalar type when building a witness.
        for child in feature.get_children():
            child_key, _ = get_config_key_value(child)
            if child_key is None and child.name in selected:
                if child.name.endswith('_nameStr'):
                    return 'string'
                if child.name.endswith('_valueInt'):
                    return 'integer'
        return ''

    def as_number(value: Any) -> Optional[float]:
        try:
            if value is None or isinstance(value, bool):
                return None
            return float(value)
        except (ValueError, TypeError):
            return None

    def slug(text: str) -> str:
        return re.sub(r'[^a-z0-9-]+', '-', text.lower().replace('_', '-')).strip('-') or 'value'

    def bounds(feature: Feature) -> tuple[Optional[float], Optional[float]]:
        low = as_number(attr(feature, 'minimum', 'schemaMinimum', 'minValue'))
        high = as_number(attr(feature, 'maximum', 'schemaMaximum', 'maxValue'))
        doc = get_doc(feature) or ''
        if low is None:
            m = re.search(r'\b(?:minimum value is|must be greater than or equal to)\s*(-?\d+(?:\.\d+)?)', doc, re.I)
            if m:
                low = float(m.group(1))
        if high is None:
            m = re.search(r'\b(?:maximum value is|must be less than or equal to)\s*(-?\d+(?:\.\d+)?)', doc, re.I)
            if m:
                high = float(m.group(1))
        return low, high

    def witness(feature: Feature, key: str, location: tuple[str, ...],
                array_context: Optional[tuple[str, ...]], unique_key: bool = False) -> Any:
        typ = value_type(feature)
        configured = None
        for candidate in (manifest_value(feature, config, selected),
                          config.elements.get(feature.name), get_default_value(feature)):
            if (candidate is not None and not isinstance(candidate, bool)
                    and ((typ == 'string' and isinstance(candidate, str))
                         or (typ == 'integer' and isinstance(candidate, int))
                         or (typ == 'number' and isinstance(candidate, (int, float))))):
                configured = candidate
                break
        if configured is not None and not unique_key:
            return configured
        if typ == 'boolean':
            result = False
        elif typ in {'integer', 'number'}:
            minimum, maximum = bounds(feature)
            number = max(1.0, minimum) if minimum is not None else 1.0
            if typ == 'integer':
                import math
                number = math.ceil(number)
            if maximum is not None and number > maximum:
                # If the schema permits values below 1, use the upper limit.
                number = int(maximum) if typ == 'integer' else maximum
            if minimum is not None and number < minimum:
                raise ValueError(f'No valid numeric witness in bounds for {feature.name}')
            result = int(number) if typ == 'integer' else float(number)
        elif typ == 'string':
            doc = (get_doc(feature) or '').upper()
            # Format hints such as DNS_LABEL constrain the witness; they are not
            # values to be copied verbatim into the manifest.
            base_key = (array_context[-1] if array_context and key == 'name' else key)
            base_key = base_key[:-1] if key == 'name' and base_key.endswith('s') else base_key
            prefix = 'fm-' + slug(base_key)
            if 'DNS_LABEL' in doc or 'DNS SUBDOMAIN' in doc or key == 'name':
                prefix = prefix[:52].rstrip('-')
            result = prefix
            min_length = attr(feature, 'minLength', 'schemaMinLength')
            max_length = attr(feature, 'maxLength', 'schemaMaxLength')
            if min_length is not None:
                result = result.ljust(int(min_length), 'x')
            if max_length is not None and len(result) > int(max_length):
                result = result[:int(max_length)].rstrip('-')
            if not result:
                raise ValueError(f'Cannot satisfy string length for {feature.name}')
        else:
            return None  # Unsupported types remain unresolved, never written as True.

        # Uniqueness for collection keys is driven by x-kubernetes-list-map-keys
        # (or the documented uniqueness rule for names), not a fixed array kind.
        if unique_key and array_context is not None:
            namespace = array_context[:-1] if key == 'name' else array_context
            token = namespace + (key,)
            taken = reserved.setdefault(token, set())
            result = configured if configured is not None else result
            if typ == 'string':
                base = str(result)
                suffix = 1
                if configured is None or result in taken:
                    while f'{base}-{suffix}' in taken:
                        suffix += 1
                    result = f'{base}-{suffix}'
            elif typ in {'integer', 'number'}:
                minimum, maximum = bounds(feature)
                while result in taken:
                    result += 1
                    if maximum is not None and result > maximum:
                        raise ValueError(f'No unique numeric witness within bounds for {feature.name}')
            taken.add(result)
            if configured is not None and result == configured:
                return result
        report.append(f'{".".join(location)}: generated {typ} witness {result!r} ({feature.name})')
        return result

    def collection_minimum(feature: Feature) -> int:
        minimum = attr(feature, 'minItems', 'schemaMinItems', 'min_items')
        if minimum is not None:
            return max(0, int(minimum))
        doc = (get_doc(feature) or '').lower()
        # Explicit natural-language minimum. Do not infer one from [0..*].
        if 'there must be at least one' in doc:
            return 1
        return 0

    def pre_reserve(node: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(child, list):
                    collection_path = path + (str(key),)
                    for item in child:
                        if isinstance(item, dict):
                            for item_key, value in item.items():
                                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                                    scope = path if item_key == 'name' else collection_path
                                    reserved.setdefault(scope + (str(item_key),), set()).add(value)
                        pre_reserve(item, collection_path)
                else:
                    pre_reserve(child, path + (str(key),))

    pre_reserve(data)

    def materialize_array(feature: Feature, items: list[Any],
                          path: tuple[str, ...]) -> None:
        minimum = collection_minimum(feature)
        # Only object items can be generated from child-feature requirements.
        if minimum > len(items) and feature.get_children():
            for _ in range(minimum - len(items)):
                items.append({})
                report.append(f'{".".join(path)}: added required array item ({feature.name})')
        for i, item in enumerate(items):
            if isinstance(item, dict):
                walk(feature, item, path + (str(i),), path)

    def is_unique_collection_key(parent_feature: Feature, child: Feature, key: str,
                                 array_context: Optional[tuple[str, ...]]) -> bool:
        if array_context is None:
            return False
        list_keys = attr(parent_feature, 'x_kubernetes_list_map_keys',
                         'xKubernetesListMapKeys')
        if isinstance(list_keys, str):
            try:
                list_keys = json.loads(list_keys)
            except ValueError:
                list_keys = []
        if isinstance(list_keys, list) and key in list_keys:
            return True
        # Name is a conventional list identity; the schema's list-map-key
        # metadata takes precedence for other identity fields.
        return key == 'name'

    def walk(parent_feature: Feature, obj: dict[str, Any], path: tuple[str, ...],
             array_context: Optional[tuple[str, ...]]) -> None:
        for relation in parent_feature.get_relations():
            required = relation.is_mandatory()
            for child in relation.children:
                # The model's selection determines optional properties; mandatory
                # children are structurally required when the parent is present.
                key, config_value = get_config_key_value(child)
                if not required:
                    if array_context is not None:
                        # A global UVL selection does not imply that an optional
                        # field occurs in EVERY array item.
                        if key is None or key not in obj:
                            continue
                    elif child.name not in selected and (key is None or key not in obj):
                        continue
                if key is None:
                    # Abstract and keyless grouping features have no YAML node.
                    if required or child.name in selected:
                        walk(child, obj, path, array_context)
                    continue
                kind = _yaml_shape(child)
                cpath = path + (str(key),)
                if kind == 'array':
                    if key not in obj and (required or child.name in selected):
                        obj[key] = []
                    if isinstance(obj.get(key), list):
                        materialize_array(child, obj[key], cpath)
                    continue
                if kind in {'object', 'map'}:
                    if key not in obj and (required or child.name in selected):
                        obj[key] = {}
                    if kind == 'object' and isinstance(obj.get(key), dict):
                        walk(child, obj[key], cpath, array_context)
                    # A map has unconstrained keys; never fabricate entries.
                    continue
                typ = value_type(child)
                value = obj.get(key)
                missing = key not in obj or value is None or (value is True and typ != 'boolean')
                if missing and (required or child.name in selected):
                    if config_value is not None:
                        obj[key] = config_value
                    else:
                        generated = witness(child, key, cpath, array_context,
                                            is_unique_collection_key(parent_feature, child, key,
                                                                     array_context))
                        if generated is not None:
                            obj[key] = generated
                        elif required:
                            logger.warning('⚠️ Cannot materialize required property %s '
                                           '(missing concrete value/type in UVL)', child.name)
                # Don't overwrite any concrete values or selected enum alternatives.

    walk(fm.root, data, (), None)
    return data, report


def _belongs_to(feature: Feature, ancestor: Feature) -> bool:
    current: Optional[Feature] = feature
    while current is not None:
        if current.name == ancestor.name:
            return True
        current = current.get_parent()
    return False


def _serialize_collection(
    feature: Feature, payload: Any, features: dict[str, Feature]
) -> Any:
    """Translate UVL-feature-shaped list/map entries to actual YAML values."""
    kind = get_serialization_attr(feature, 'serializationKind')
    if kind == 'array':
        if not isinstance(payload, list):
            raise ValueError(f'{feature.name} must be an array (JSON list)')
        return [_collection_item(feature, item, features) for item in payload]
    if kind == 'map':
        if isinstance(payload, dict):
            # A plain JSON map is also allowed for manually authored configurations.
            # Its dynamic keys need no reinterpretation.
            return payload
        if not isinstance(payload, list):
            raise ValueError(f'{feature.name} must be a map or array of map entries')
        entries: dict[str, Any] = {}
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f'Map {feature.name} requires object rows')
            map_key: Any = None
            map_value: Any = None
            extra: dict[str, Any] = {}
            for child_name, value in row.items():
                child = _check_payload_feature(child_name, feature, features)
                role = get_serialization_attr(child, 'serializationRole')
                if role == 'mapKey':
                    map_key = value
                elif role == 'mapValue':
                    map_value = _value_from_feature(child, value, features)
                else:
                    _merge_fields(extra, _fields_from_feature(child, value, features))
            if map_key is None:
                raise ValueError(f'Map {feature.name} row is missing mapKey')
            if not isinstance(map_key, str):
                raise ValueError(f'Map {feature.name} key must be a string')
            if map_key in entries:
                raise ValueError(f'Duplicate map key {map_key!r} in {feature.name}')
            entries[map_key] = map_value if map_value is not None else extra
        return entries
    raise ValueError(f'Not a collection feature: {feature.name}')


def _check_payload_feature(name: str, parent: Feature, features: dict[str, Feature]) -> Feature:
    child = features.get(name)
    if child is None or not _belongs_to(child, parent) or child.name == parent.name:
        raise ValueError(f'Unknown/unrelated collection entry {name!r} under {parent.name}')
    return child


def _merge_fields(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if key in target and target[key] != value:
            raise ValueError(f'Conflicting YAML values for key {key!r}')
        target[key] = value


def _value_from_feature(feature: Feature, value: Any, features: dict[str, Feature]) -> Any:
    kind = get_serialization_attr(feature, 'serializationKind')
    if kind in {'array', 'map'} and isinstance(value, (dict, list)):
        return _serialize_collection(feature, value, features)
    if isinstance(value, dict):
        fields: dict[str, Any] = {}
        scalar = None
        for child_name, sub_value in value.items():
            child = _check_payload_feature(child_name, feature, features)
            sub_key, _ = get_config_key_value(child)
            sub = _value_from_feature(child, sub_value, features)
            if sub_key is None and not isinstance(sub, dict):
                if scalar is not None and scalar != sub:
                    raise ValueError(f'Conflicting values in {feature.name}')
                scalar = sub
            else:
                _merge_fields(fields, {sub_key: sub} if sub_key is not None else sub)
        if scalar is not None and fields:
            raise ValueError(f'A feature has both scalar and nested values: {feature.name}')
        return scalar if scalar is not None else fields
    if isinstance(value, list):
        raise ValueError(f'List without serializationKind on {feature.name}')
    _, config_value = get_config_key_value(feature)
    if value is True and config_value is not None:
        return config_value
    # A UVL selection flag does not mean a Kubernetes object is boolean.
    if value is True:
        shape = _yaml_shape(feature)
        if shape in {'object', 'map'}:
            return {}
        if shape == 'array':
            return []
    return value


def _fields_from_feature(feature: Feature, value: Any, features: dict[str, Feature]) -> dict[str, Any]:
    key, _ = get_config_key_value(feature)
    output = _value_from_feature(feature, value, features)
    if key is None:
        if not isinstance(output, dict):
            raise ValueError(f'Keyless feature {feature.name} cannot emit a mapping entry')
        return output
    return {key: output}


def _collection_item(parent: Feature, item: Any, features: dict[str, Feature]) -> Any:
    if not isinstance(item, dict):
        return item
    fields: dict[str, Any] = {}
    scalar: Any = None
    for name, value in item.items():
        child = _check_payload_feature(name, parent, features)
        key, _ = get_config_key_value(child)
        rendered = _value_from_feature(child, value, features)
        if key is not None:
            _merge_fields(fields, {key: rendered})
        elif isinstance(rendered, dict):
            _merge_fields(fields, rendered)
        else:
            if scalar is not None and scalar != rendered:
                raise ValueError(f'Ambiguous scalar item in {parent.name}')
            scalar = rendered
    if fields and scalar is not None:
        raise ValueError(f'Array item mixes scalar and properties in {parent.name}')
    return fields if fields else scalar if scalar is not None else {}


def _manifest_key_path(feature: Feature) -> list[str]:
    ancestors = list(iter_ancestors(feature))
    path = []
    for node in reversed(ancestors):
        key, _ = get_config_key_value(node)
        if key is not None:
            path.append(key)
    key, _ = get_config_key_value(feature)
    if key is not None:
        path.append(key)
    return path


def _sat_forced_array_features(
    fm: FeatureModel, sat_model: PySATModel, partial_config: Configuration,
    full_config: Configuration, collections: Optional[dict[str, Any]] = None,
    checker: Optional[SatChecker] = None
) -> set[str]:
    """Return selected array descendants that are entailed by the input choices.

    A Boolean SAT probe ``assumptions + NOT feature`` must be UNSAT. This
    separates implications from MaxSAT/default optional selections.

    Choices explicitly present inside collection items can also be assumed,
    provided the *union* is compatible with the global Boolean SAT model.
    This matters for point-only experiments whose enum child is resolved in
    the render-time payload. Such a union is not a per-instance SAT encoding.
    """
    from contextlib import nullcontext

    selected = selected_items(full_config)
    decided = dict(partial_config.elements)
    features = {feature.name: feature for feature in fm.get_features()}
    candidates: list[str] = []
    for name in selected:
        if name in decided or name not in sat_model.variables:
            continue
        current = features.get(name)
        if current is None:
            continue
        current = current.get_parent()
        while current is not None:
            if get_serialization_attr(current, 'serializationKind') == 'array':
                candidates.append(name)
                break
            current = current.get_parent()
    if not candidates:
        return set()

    extra: dict[str, bool] = {}
    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for name, child in value.items():
                if name in selected and name in sat_model.variables and is_selected(child):
                    extra[name] = True
                visit(child)
    for value in (collections or {}).values():
        visit(value)

    context = nullcontext(checker) if checker is not None else SatChecker(sat_model)
    with context as sat:
        hypotheses = {**decided, **{n: True for n in extra if n not in decided}}
        if extra and not sat.is_satisfiable(hypotheses):
            logger.warning('⚠️  Explicit array choices cannot be represented together '
                           'by global SAT variables; checking only top-level partial')
            hypotheses = decided
        forced = {name for name in candidates
                  if not sat.is_satisfiable({**hypotheses, name: False})}
    logger.info('🔗 SAT-forced array descendants: %d of %d selected candidates',
                len(forced), len(candidates))
    return forced


def _merge_forced_array_fields(
    data: dict[str, Any], fm: FeatureModel, config: Configuration,
    collections: dict[str, Any], forced_names: Collection[str]
) -> list[str]:
    """Place SAT-forced fields in unambiguous existing array occurrences only.

    A Boolean FM has one variable per schema feature, not per list element.
    A forced field is therefore assigned to the sole array item, or to the
    unique item most specifically associated with its explicit UVL branch.
    Ambiguous cases are reported, never broadcast to all occurrences.
    """
    features = {feature.name: feature for feature in fm.get_features()}
    selected = selected_items(config)
    report: list[str] = []

    def explicit_names(value: Any) -> set[str]:
        names: set[str] = set()
        if isinstance(value, dict):
            for name, child in value.items():
                if name in features:
                    names.add(name)
                names.update(explicit_names(child))
        elif isinstance(value, list):
            for child in value:
                names.update(explicit_names(child))
        return names

    def branch(feature: Feature, ancestor: Feature) -> tuple[str, ...]:
        result = []
        cursor: Optional[Feature] = feature
        while cursor is not None and cursor.name != ancestor.name:
            result.append(cursor.name)
            cursor = cursor.get_parent()
        return tuple(reversed(result)) if cursor is not None else ()

    def best_item(feature: Feature, owner: Feature, raw_items: Any,
                  count: int) -> Optional[int]:
        if count == 1:
            return 0
        if not isinstance(raw_items, list) or len(raw_items) != count:
            return None
        desired = branch(feature, owner)
        scores = []
        for raw in raw_items:
            best = 0
            for name in explicit_names(raw):
                member = features.get(name)
                if member is None or not _belongs_to(member, owner):
                    continue
                candidate = branch(member, owner)
                common = 0
                for left, right in zip(desired, candidate):
                    if left != right:
                        break
                    common += 1
                best = max(best, common)
            scores.append(best)
        top = max(scores, default=0)
        return scores.index(top) if top > 0 and scores.count(top) == 1 else None

    # Only the outermost arrays have a directly addressable path in the
    # manifest; nested arrays require per-occurrence identity, not inferred here.
    arrays = [f for f in features.values()
              if f.name in selected and
              get_serialization_attr(f, 'serializationKind') == 'array']
    outermost = []
    for array in arrays:
        if not any(_belongs_to(array, other) and other is not array for other in arrays):
            outermost.append(array)

    for owner in outermost:
        path = _manifest_key_path(owner)
        node: Any = data
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if not isinstance(node, list) or not node:
            continue
        raw_items = collections.get(owner.name)
        for name in sorted(forced_names):
            feature = features.get(name)
            if feature is None or not _belongs_to(feature, owner) or feature is owner:
                continue
            chain = branch(feature, owner)
            nodes = [features[n] for n in chain]
            if any(get_serialization_attr(n, 'serializationKind') in {'array', 'map'}
                   for n in nodes):
                report.append(f'{name}: deferred (nested collection or dynamic map)')
                continue
            # Structural group/enum leaves without a configKey refer to their
            # nearest keyed parent. The concrete enum value comes from the
            # complete selected configuration.
            keyed = [n for n in nodes if get_config_key_value(n)[0] is not None]
            if not keyed:
                continue
            keys = [get_config_key_value(n)[0] for n in keyed]
            index = best_item(feature, owner, raw_items, len(node))
            if index is None:
                report.append(f'{name}: ambiguous among {len(node)} array occurrences')
                continue
            target = node[index]
            if not isinstance(target, dict):
                report.append(f'{name}: array item {index} is not an object')
                continue
            for key in keys[:-1]:
                value = target.get(key)
                if value is None and key not in target:
                    target[key] = {}
                    value = target[key]
                if not isinstance(value, dict):
                    report.append(f'{name}: cannot descend into non-object {key!r}')
                    target = None
                    break
                target = value
            if target is None:
                continue
            key = keys[-1]
            leaf = keyed[-1]
            typ = str(get_serialization_attr(leaf, 'schema_type') or '').lower()
            if (key in target and target[key] is not None and
                    (target[key] is not True or typ == 'boolean')):
                continue  # Preserve explicit concrete values, including Boolean true.
            value = manifest_value(leaf, config, selected)
            shape = _yaml_shape(leaf)
            if shape in {'object', 'map'}:
                value = {} if key not in target else target[key]
            elif shape == 'array':
                value = [] if key not in target else target[key]
            elif value is True and typ != 'boolean':
                value = None  # typed witness materializer resolves this
            if key not in target or target[key] is None or target[key] is True:
                target[key] = value
                report.append(f'{name}: merged into {owner.name}[{index}].' + '.'.join(keys))
    return report


def _apply_collections(manifest: str, fm: FeatureModel,
                       config: Configuration, collections: dict[str, Any],
                       expand_to_leaves: bool = False,
                       collection_mode: str = 'empty',
                       forced_array_features: Optional[Collection[str]] = None) -> str:
    """Reconstruct arrays/maps and retain selected descendants in expanded mode.

    The text renderer supplies a one-item *template* from the complete UVL
    selection, while the input JSON supplies collection occurrences and explicit
    values. Previously we replaced the former with the latter, losing every
    descendant added by --expand-to-leaves. Merge both, recursively, giving
    supplied values precedence. No new array/map entries are fabricated when
    the input contains none unless the model gives an explicit minimum
    cardinality or an unambiguous minimum in the description.
    """
    import copy

    if collection_mode not in {'empty', 'skeleton'}:
        raise ValueError(f'Unknown collection mode: {collection_mode}')
    if collection_mode == 'skeleton' and not expand_to_leaves:
        raise ValueError('--collection-mode skeleton requires --expand-to-leaves')

    data = yaml.safe_load(manifest) if manifest.strip() else None
    if not isinstance(data, dict):
        raise ValueError('The base manifest is not a YAML mapping; cannot place collections')
    features = {feature.name: feature for feature in fm.get_features()}
    selected = selected_items(config)
    selected_collections = {
        name: get_serialization_attr(features[name], 'serializationKind')
        for name in selected if name in features and
        get_serialization_attr(features[name], 'serializationKind') in {'array', 'map'}
    }
    kinds_by_path = {
        tuple(_manifest_key_path(features[name])): kind
        for name, kind in selected_collections.items()
    }
    missing = object()
    stats = {'synthetic_array_items': 0, 'empty_arrays': 0, 'empty_maps': 0}

    def complete(template: Any, explicit: Any, path: tuple[str, ...]) -> Any:
        kind = kinds_by_path.get(path)
        if kind == 'array':
            if explicit is missing:
                # SAT selects the array feature, not its number of occurrences.
                # An opt-in *inspection* mode creates a representative object
                # using only selected fields in the one-item UVL template.
                # Pass each CHILD path so we cannot recurse infinitely at the
                # same array node. Never synthesize scalars or dynamic map keys.
                if (collection_mode == 'skeleton' and expand_to_leaves
                        and isinstance(template, dict) and template):
                    example = {
                        key: complete(value, missing, path + (str(key),))
                        for key, value in template.items()
                    }
                    stats['synthetic_array_items'] += 1
                    return [example]
                stats['empty_arrays'] += 1
                return []
            if isinstance(explicit, list):
                if expand_to_leaves and isinstance(template, dict):
                    return [complete(template, item, path) if isinstance(item, dict)
                            else item for item in explicit]
                return explicit
        elif kind == 'map':
            if explicit is missing:
                stats['empty_maps'] += 1
                return {}
            # Dynamic entries must come from user input. No generated mapKey
            # values can be inferred from a Boolean feature selection.
            return explicit

        if explicit is missing:
            if not isinstance(template, dict):
                return copy.deepcopy(template)
            return {k: complete(v, missing, path + (str(k),))
                    for k, v in template.items()}

        if expand_to_leaves and isinstance(template, dict) and isinstance(explicit, dict):
            # Keep explicit user keys first (e.g. container name/image) and
            # append only additional selected fields from the UVL template.
            merged: dict[Any, Any] = {}
            for k, value in explicit.items():
                merged[k] = (complete(template[k], value, path + (str(k),))
                             if k in template else value)
            for k, value in template.items():
                if k not in merged:
                    merged[k] = complete(value, missing, path + (str(k),))
            return merged
        return explicit

    # Do not process a nested collection as an independent YAML path: it lives
    # inside each occurrence of the outer collection. The recursive completion
    # above reconstructs it using the corresponding nested UVL template.
    top_collections = []
    for name in selected_collections:
        parent = features[name].get_parent()
        while parent is not None and parent.name not in selected_collections:
            parent = parent.get_parent()
        if parent is None:
            top_collections.append(name)

    for name in sorted(top_collections, key=lambda n: len(_manifest_key_path(features[n]))):
        feature = features[name]
        path = _manifest_key_path(feature)
        if not path:
            continue
        parent: Any = data
        for key in path[:-1]:
            if not isinstance(parent, dict):
                parent = None
                break
            parent = parent.get(key)
        if not isinstance(parent, dict):
            continue
        explicit = (_serialize_collection(feature, collections[name], features)
                    if name in collections else missing)
        parent[path[-1]] = complete(parent.get(path[-1]), explicit, tuple(path))

    # Complete selected mandatory typed properties using the same policy for
    # every object and array item. No Kubernetes resource-specific field names.
    data, witness_report = _materialize_required_values(data, fm, config)
    if forced_array_features:
        forced_report = _merge_forced_array_fields(
            data, fm, config, collections, forced_array_features)
        for item in forced_report:
            if 'ambiguous' in item or 'deferred' in item or 'cannot descend' in item:
                logger.warning('⚠️  Array field not safely placed: %s', item)
            else:
                logger.debug('🔗 %s', item)
        # Populate only missing typed values and mandatory descendants of
        # newly merged fields; no optional feature is selected by this pass.
        data, new_witnesses = _materialize_required_values(data, fm, config)
        witness_report.extend(new_witnesses)
    if witness_report:
        logger.info('🧩 Typed witness materialization: %d field(s) or item(s)',
                    len(witness_report))
        for entry in witness_report:
            logger.debug('  %s', entry)

    logger.info('🧺 Collections: %d selected array/map features, %d explicitly defined; '
                '%d synthetic array items, %d empty arrays, %d empty maps',
                len(selected_collections), len(collections),
                stats['synthetic_array_items'], stats['empty_arrays'], stats['empty_maps'])

    # Repeated template values must not introduce YAML anchors/aliases; each
    # occurrence should remain an independent, readable Kubernetes object.
    class NoAliasesSafeDumper(yaml.SafeDumper):
        def ignore_aliases(self, data: Any) -> bool:
            return True

    return yaml.dump(data, Dumper=NoAliasesSafeDumper, sort_keys=False,
                     allow_unicode=True, default_flow_style=False)


def _selected_collection_feature_names(collections: dict[str, Any]) -> set[str]:
    """Names explicitly present in repeated collection entries (outside SAT)."""
    names: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for name, child in value.items():
                names.add(name)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for payload in collections.values():
        walk(payload)
    return names


def _collection_inline_comments(fm: FeatureModel,
                                collections: dict[str, Any],
                                full_config: Configuration) -> dict[str, str]:
    """Document explicit and SAT-expanded fields inside selected collections.

    Repeated array/map elements are kept outside SAT. Include docs for supplied
    fields and for model-selected descendants inside collections, regardless of
    mandatory/optional relation. Only paths emitted in the YAML are annotated.
    """
    present = _selected_collection_feature_names(collections)
    present.update(collections)  # Include the arrays/maps themselves, e.g. containers.
    # Also document fields introduced by --expand-to-leaves inside a collection.
    # Only paths that actually exist in the serialized YAML will be annotated.
    selected = selected_items(full_config)
    container_features = [f for f in fm.get_features()
                          if f.name in selected and
                          get_serialization_attr(f, 'serializationKind') in {'array', 'map'}]
    for feature in fm.get_features():
        if feature.name in selected and any(_belongs_to(feature, coll)
                                             for coll in container_features):
            present.add(feature.name)
    inline: dict[str, str] = {}
    for feature in fm.get_features():
        if feature.name not in present:
            continue
        # Synthetic keyless mapKey/mapValue/arrayItem features do not have their
        # own YAML property line. Their documentation should not be attached
        # misleadingly to the nearest ancestor's key.
        key, _ = get_config_key_value(feature)
        if key is None:
            continue
        doc = get_doc(feature)
        if doc:
            inline[feature.name] = doc
    return inline


def _restore_variability_comments(manifest: str, fm: FeatureModel,
                                  comments: VariabilityComments,
                                  collections: dict[str, Any],
                                  full_config: Configuration) -> str:
    """Attach model comments after PyYAML serializes arrays/maps.

    PyYAML's safe_load/safe_dump deliberately discards comments. We locate mapping
    keys using its parsed YAML node positions, rather than matching key names or
    indentation with regular expressions. Paths are mapped to UVL ``configKey``
    paths; list indexes are ignored so item properties can be annotated too.

    This changes only comment text, never the YAML data structure. It also avoids
    any extra dependency (ruamel.yaml is not required).
    """
    extra_inline = _collection_inline_comments(fm, collections, full_config)
    wanted = set(comments.blocks) | set(comments.inline) | set(extra_inline)
    if not wanted or not manifest.strip():
        return manifest

    # Multiple selected synthetic features can refer to the same output key;
    # retain all distinct annotations on that YAML path.
    paths: dict[tuple[str, ...], list[Feature]] = {}
    for feature in fm.get_features():
        if feature.name not in wanted:
            continue
        path = tuple(_manifest_key_path(feature))
        if path:
            paths.setdefault(path, []).append(feature)

    node = yaml.compose(manifest)
    if node is None:
        return manifest

    # Positions are those of the *final* serialized YAML, with '-' and dynamic
    # map keys already incorporated.
    annotations: dict[int, tuple[list[str], list[str]]] = {}

    def visit(current: Any, path: tuple[Any, ...] = ()) -> None:
        if isinstance(current, yaml.nodes.SequenceNode):
            for index, item in enumerate(current.value):
                visit(item, path + (index,))
        elif isinstance(current, yaml.nodes.MappingNode):
            for key_node, value_node in current.value:
                if not isinstance(key_node, yaml.nodes.ScalarNode):
                    continue
                key_path = path + (key_node.value,)
                normalized = tuple(p for p in key_path if not isinstance(p, int))
                matches = paths.get(normalized, [])
                blocks: list[str] = []
                inline: list[str] = []
                for feature in matches:
                    for block in comments.blocks.get(feature.name, []):
                        if block not in blocks:
                            blocks.append(block)
                    doc = comments.inline.get(feature.name) or extra_inline.get(feature.name)
                    if doc and doc not in inline:
                        inline.append(doc)
                if blocks or inline:
                    annotations[key_node.start_mark.line] = (blocks, inline)
                visit(value_node, key_path)

    visit(node)
    if not annotations:
        return manifest

    lines = manifest.splitlines()
    result: list[str] = []
    for index, line in enumerate(lines):
        annotation = annotations.get(index)
        if annotation:
            blocks, inline = annotation
            # A line can start with '- name:' in a YAML sequence; the comment
            # uses the actual line indentation, not the column of 'name'.
            indent = line[:len(line) - len(line.lstrip(' '))]
            result.extend(f'{indent}# {text}' for text in blocks)
            if inline:
                line += '  # ' + ' | '.join(inline)
        result.append(line)
    return '\n'.join(result) + ('\n' if manifest.endswith('\n') else '')

def render_manifest(fm: FeatureModel,
                    config: Configuration,
                    comments: Optional[VariabilityComments] = None,
                    collections: Optional[dict[str, Any]] = None,
                    expand_to_leaves: bool = False,
                    collection_mode: str = 'empty',
                    forced_array_features: Optional[Collection[str]] = None) -> str:
    """Render the YAML manifest of a complete configuration.

    The feature tree is traversed depth-first so that the YAML nesting follows the model.
    Among sibling features, those with a value (``key: value``) come first and those with
    only a key (``key:``) come last. Indentation is two spaces per emitted YAML
    mapping key; features without ``configKey`` (abstract or concrete) do not add
    a YAML indentation level.

    A selected boolean feature with a ``configKey`` that has no value of its own, none to
    take from its children and no nested lines is written as ``key: true``. A boolean
    feature with a ``configKey`` and no ``configValue`` that was explicitly deselected in the
    configuration is written as ``key: false``. Undecided features are omitted.
    """
    requested_comments = comments is not None
    comments = comments or VariabilityComments({}, {})
    selected = selected_items(config)

    def is_deselected_flag(feature: Feature) -> bool:
        key, config_value = get_config_key_value(feature)
        return (key is not None and config_value is None and feature.is_boolean()
                and config.elements.get(feature.name) is False)

    def visit(feature: Feature, depth: int) -> tuple[list[str], bool]:
        """Render a feature and its subtree; also return whether its line has a value.

        ``depth`` counts YAML mapping keys in the ancestry, not UVL tree levels.
        A feature without ``configKey`` is transparent for indentation, even when
        it is a concrete (non-abstract) feature.
        """
        key, _ = get_config_key_value(feature)
        indent = '  ' * depth
        if feature.name not in selected:  # explicitly deselected boolean flag
            return [f'{indent}{key}: false'], True
        child_depth = depth + (1 if key is not None else 0)
        blocks = [visit(child, child_depth) for child in feature.get_children()
                  if child.name in selected or is_deselected_flag(child)]
        # Stable sort: siblings with a value first, key-only siblings last.
        child_lines = [line for lines, _ in sorted(blocks, key=lambda b: not b[1])
                       for line in lines]
        if key is None:
            return child_lines, False
        value = manifest_value(feature, config, selected)
        if value is None and not child_lines:
            shape = _yaml_shape(feature)
            if shape in {'object', 'map'}:
                value = {}
            elif shape == 'array':
                value = []
            elif feature.is_boolean():
                value = True  # only an actual scalar flag should become true
        text = format_value(value)
        lines = [f'{indent}# {comment}' for comment in comments.blocks.get(feature.name, [])]
        line = f'{indent}{key}:' + (f' {text}' if text else '')
        description = comments.inline.get(feature.name)
        if description:
            line += f'  # {description}'
        lines.append(line)
        return lines + child_lines, value is not None

    if fm.root.name not in selected:
        return ''
    lines, _ = visit(fm.root, 0)
    manifest = '\n'.join(lines) + '\n' if lines else ''
    # For complex collections the legacy text renderer is first used to preserve
    # existing defaults/selection behavior; the collection nodes are then
    # reconstructed with the explicit UVL serialization metadata.
    has_collections = any(get_serialization_attr(f, 'serializationKind') in {'array', 'map'}
                          and f.name in selected for f in fm.get_features())
    if has_collections:
        rendered = _apply_collections(manifest, fm, config, collections or {},
                                      expand_to_leaves=expand_to_leaves,
                                      collection_mode=collection_mode,
                                      forced_array_features=forced_array_features)
        if requested_comments:
            rendered = _restore_variability_comments(rendered, fm, comments,
                                                     collections or {}, config)
        return rendered
    if manifest.strip():
        structured = yaml.safe_load(manifest)
        if isinstance(structured, dict):
            structured, witness_report = _materialize_required_values(structured, fm, config)
            if witness_report:
                logger.info('🧩 Typed witness materialization: %d field(s) or item(s)',
                            len(witness_report))
                rendered = yaml.safe_dump(structured, sort_keys=False, allow_unicode=True)
                if requested_comments:
                    rendered = _restore_variability_comments(rendered, fm, comments,
                                                             collections or {}, config)
                return rendered
    return manifest


def write_manifest(manifest: str, output: Path) -> None:
    """Write the manifest to ``output``, creating parent directories if needed."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(manifest, encoding='utf-8')


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """Build the command line argument parser."""
    parser = argparse.ArgumentParser(
        description='Generate a YAML manifest from a UVL feature model and a partial '
                    'configuration.')
    parser.add_argument('feature_model', type=Path,
                        help='Path to the feature model (.uvl).')
    parser.add_argument('configuration', nargs='+',
                        help='One or more partial configuration files (.json or .uvl.json). '
                             'Later files override earlier ones.')
    parser.add_argument('-o', '--output', type=Path, default=Path('manifest.yaml'),
                        help='Output YAML file (default: manifest.yaml).')
    parser.add_argument('--variability-comments', action=argparse.BooleanOptionalAction,
                        default=False,
                        help='Add comments (from the "doc" attribute) about the variation points: '
                             'selected/alternative variants for alternative groups and an '
                             'inline description for optional/or variants. Covers the given '
                             'features, everything below them and their parents, or every '
                             'variation point with --expand-to-leaves (default: disabled).')
    parser.add_argument('--expand-to-leaves', action=argparse.BooleanOptionalAction,
                        default=False,
                        help='Explore model descendants from selected features (large manifests). '
                             'For explicitly supplied arrays, add model-selected fields to '
                             'each item; arrays/maps without entries remain empty. '
                             'Defaults/config values only when disabled.')
    parser.add_argument('--collection-mode', choices=('empty', 'skeleton'), default='empty',
                        help='empty (default): selected arrays without entries become []. '
                             'skeleton: with --expand-to-leaves, materialize one SYNTHETIC '
                             'representative item for each otherwise empty array of objects, '
                             'including nested ones. Intended for model inspection; not a '
                             'validity/deployment mode. Maps remain {} without keys.')
    parser.add_argument('--expand-scope', choices=('selected', 'resource'), default='selected',
                        help='With --expand-to-leaves: selected (original behavior, expand '
                             'only below explicitly selected features) or resource (expand '
                             'the complete selected Kubernetes resource subtree, not the '
                             'whole global FM). Default: selected.')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show debug information.')
    return parser


def run(feature_model: Path,
        config_files: list[str],
        output: Path,
        variability_comments: bool,
        expand_to_leaves: bool = False,
        expand_scope: str = 'selected',
        collection_mode: str = 'empty') -> None:
    """Generate the manifest and write it to ``output``."""
    with log_step(f'Reading feature model {feature_model}', '📖'):
        fm = UVLReader(str(feature_model)).transform()
    logger.info('🌳 Feature model loaded: %d features', len(fm.get_features()))

    with log_step('Translating the feature model to SAT', '🔧'):
        sat_model = FmToPysat(fm).transform()

    with log_step(f'Reading partial configuration ({len(config_files)} file(s))', '📥'):
        partial_config, collections = load_configurations_with_collections(fm, config_files)
    logger.info('📝 Partial configuration: %d selected features',
                len(selected_items(partial_config)))
    logger.debug('🔍 Partial configuration: %s', partial_config)

    with log_step('Generating the full configuration', '⚙️'):
        full_config = complete_and_minimize(fm, sat_model, partial_config, expand_to_leaves,
                                            expand_scope=expand_scope)
    logger.debug('🔍 Full configuration: %s', full_config)

    comments = None
    if variability_comments:
        with log_step('Building variability comments', '💬'):
            # Leaves mode: every variation point; compact mode: those of the given features,
            # of everything below them, and of their parents.
            scope = None if expand_to_leaves else set(selected_items(partial_config))
            comments = build_variability_comments(fm, full_config, scope)
        logger.info('💬 %d variation point block(s) and %d inline description(s)',
                    len(comments.blocks), len(comments.inline))

    if collection_mode == 'skeleton':
        logger.warning('⚠️  Skeleton mode creates synthetic array items for inspection; '
                       'do not deploy this manifest without completing and validating it.')
    # The completed FM may also contain arbitrary/default optional choices.
    # Only add array descendants demonstrably implied by SAT to an occurrence.
    forced_array_features = _sat_forced_array_features(
        fm, sat_model, partial_config, full_config, collections=collections)
    with log_step('Rendering the manifest', '🧾'):
        manifest = render_manifest(fm, full_config, comments, collections=collections,
                                   expand_to_leaves=expand_to_leaves,
                                   collection_mode=collection_mode,
                                   forced_array_features=forced_array_features)

    with log_step(f'Writing {output}', '💾'):
        write_manifest(manifest, output)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(message)s')
    try:
        run(args.feature_model, args.configuration, args.output, args.variability_comments,
            args.expand_to_leaves, args.expand_scope, args.collection_mode)
    except (ValueError, OSError, FlamaException) as exc:
        logger.error('🚫 %s', exc)
        return 1
    logger.info('🎉 Manifest written to %s', args.output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
