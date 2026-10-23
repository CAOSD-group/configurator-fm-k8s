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
   * ``--expand-to-leaves``: starting from the features of the initial partial configuration,
     descend to the leaves (optional, abstract and key-less features included), always
     preferring the ``default`` features.
4. Minimize the configuration (MaxSAT) and repeat steps 2-3 until a fixed point is reached.
5. Render the manifest from the ``configKey`` / ``configValue`` attributes. With
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

    python main_k8s_corregido.py model.uvl partial_config.json \\
        -o out/deployment.yaml --variability-comments

Requires Python 3.9+ and the ``flamapy``, ``flamapy-fm`` and ``flamapy-sat`` packages.
"""

import argparse
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


def complete_and_minimize(fm: FeatureModel,
                          sat_model: PySATModel,
                          partial_config: Configuration,
                          expand_to_leaves: bool = False) -> Configuration:
    """Alternate "complete with defaults" and "minimize" until a fixed point is reached.

    The minimization can only add the features forced by the constraints; the values of the
    given configuration and the ``defaultValue`` ones are never replaced (see
    ``merge_values``). The loop therefore ends as soon as the minimization adds nothing.

    Returns:
        The complete configuration (it has the same selected features as its minimization).

    Raises:
        ValueError: If no valid configuration extends ``partial_config``.
    """
    scope = set(selected_items(partial_config))  # fixed: do not grow with each iteration
    logger.info('🧭 Expanding from %d given feature(s) (%s): %s', len(scope),
                'down to the leaves' if expand_to_leaves else 'defaults and config values',
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


def render_manifest(fm: FeatureModel,
                    config: Configuration,
                    comments: Optional[VariabilityComments] = None) -> str:
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
        if value is None and not child_lines and feature.is_boolean():
            value = True  # selected boolean flag with nothing else to show
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
    return '\n'.join(lines) + '\n' if lines else ''


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
                        help='Explore the model down to the leaves from the features of the '
                             'given configuration, including optional and key-less features '
                             '(large manifests). By default only the defaults and the '
                             'config values of the given features are applied.')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show debug information.')
    return parser


def run(feature_model: Path,
        config_files: list[str],
        output: Path,
        variability_comments: bool,
        expand_to_leaves: bool = False) -> None:
    """Generate the manifest and write it to ``output``."""
    with log_step(f'Reading feature model {feature_model}', '📖'):
        fm = UVLReader(str(feature_model)).transform()
    logger.info('🌳 Feature model loaded: %d features', len(fm.get_features()))

    with log_step('Translating the feature model to SAT', '🔧'):
        sat_model = FmToPysat(fm).transform()

    with log_step(f'Reading partial configuration ({len(config_files)} file(s))', '📥'):
        partial_config = load_configurations_from_file(config_files)
    logger.info('📝 Partial configuration: %d selected features',
                len(selected_items(partial_config)))
    logger.debug('🔍 Partial configuration: %s', partial_config)

    with log_step('Generating the full configuration', '⚙️'):
        full_config = complete_and_minimize(fm, sat_model, partial_config, expand_to_leaves)
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

    with log_step('Rendering the manifest', '🧾'):
        manifest = render_manifest(fm, full_config, comments)

    with log_step(f'Writing {output}', '💾'):
        write_manifest(manifest, output)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(message)s')
    try:
        run(args.feature_model, args.configuration, args.output, args.variability_comments,
            args.expand_to_leaves)
    except (ValueError, OSError, FlamaException) as exc:
        logger.error('🚫 %s', exc)
        return 1
    logger.info('🎉 Manifest written to %s', args.output)
    return 0


if __name__ == '__main__':
    sys.exit(main())