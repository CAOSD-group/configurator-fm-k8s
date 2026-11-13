#!/usr/bin/env python3
"""RQ3 v4: observación de variabilidad, validación opcional y filtrado LLM.

Reuses metadata_parser01.MetadataMapper and optionally evaluation_manifests03
for SAT. No configuration completion, remediation, or YAML sanitization.

V4: preserves v3 atomic checkpoints and adds independent schema/SAT validation.
With --filter-invalid, only mapped documents passing ALL requested checks
contribute to the observational statistics; excluded rows stay in rq3_manifests.csv.

All array instances are counted for descriptive RQ3 statistics. Optional SAT
uses only the FIRST element of each list in each YAML document; it is a
representative Boolean projection, NOT an assessment of every array instance.

Requires UVL and its matching descriptions/serialization sidecars. If a schema
or GVK is unsupported, keep an error/status row rather than silently dropping it.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import importlib.util
import os
import shutil
import tempfile
import signal
import json
from pathlib import Path
import sys
import time
from typing import Any

import yaml


def load_py(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, str(path.resolve()))
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Cannot import {path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def write_row(writer, f, data):
    # Buffered CSV writes: avoid an OS flush for each of thousands of choices.
    writer.writerow(data)


def attribute_value(feature, key):
    for attr in feature.get_attributes():
        if attr.name == key:
            return attr.get_default_value()
    return None


def model_index(uvl_path: Path):
    """Read the entire FM ONCE (no per-resource UVL parsing)."""
    from flamapy.metamodels.fm_metamodel.transformations import UVLReader
    from flamapy.metamodels.fm_metamodel.operations import FMVariationPoints
    fm = UVLReader(str(uvl_path)).transform()
    by_name = {f.name: f for f in fm.get_features()}
    groups = {}
    for vp in FMVariationPoints().execute(fm).get_result():
        for relation_index, rel in enumerate(vp.get_relations()):
            if rel.is_alternative():
                children = tuple(c.name for c in rel.children)
                marked = tuple(c.name for c in rel.children if attribute_value(c, 'default') is not None
                               or any(a.name == 'default' for a in c.get_attributes()))
                groups[(vp.name, relation_index)] = (children, marked)
    scalars = {}
    for feature in by_name.values():
        dv = attribute_value(feature, 'defaultValue')
        if dv is not None:
            scalars[feature.name] = dv
    return fm, by_name, groups, scalars


def indexes_for_root(root, groups, scalar_defaults):
    prefix = root + '_'
    gs = defaultdict(list)
    for (name, index), (children, defaults) in groups.items():
        if name == root or name.startswith(prefix):
            gs[name].append((index, set(children), defaults))
    defaults = {k:v for k,v in scalar_defaults.items() if k==root or k.startswith(prefix)}
    return gs, defaults


def make_cached_mapper(mapper_cls):
    """Avoid re-indexing all serialization entries for every document of same GVK."""
    class CachedMapper(mapper_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._rq3_root_cache = {}
        def _activate_root(self, root):
            if root not in self._rq3_root_cache:
                super()._activate_root(root)
                self._rq3_root_cache[root] = (self._path_index, self._active_entries)
            else:
                self.last_root_feature = root
                self._path_index, self._active_entries = self._rq3_root_cache[root]
    return CachedMapper


def walk_yaml_paths(value, normalized='', concrete=''):
    """Yield actual YAML paths and schema-pattern paths, preserving list indices."""
    if normalized:
        yield normalized, concrete, value
    if isinstance(value, dict):
        for k, child in value.items():
            if not isinstance(k, str):
                continue
            n = f'{normalized}.{k}' if normalized else k
            p = f'{concrete}.{k}' if concrete else k
            yield from walk_yaml_paths(child, n, p)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from walk_yaml_paths(child, normalized + '[]', concrete + f'[{i}]')


def substitute_array_indices(pattern, indices):
    out = pattern
    for index in indices:
        if '[]' not in out:
            break
        out = out.replace('[]', f'[{index}]', 1)
    return out


def first_instance_only(obj):
    """Representative array projection for SAT only; never use for RQ3 counts."""
    if isinstance(obj, list):
        return [first_instance_only(obj[0])] if obj else []
    if isinstance(obj, dict):
        return {key: first_instance_only(value) for key, value in obj.items()}
    return obj


def extract_observations(mapped, group_by_name, defaults, doc_id, kind, version, vp_paths):
    """Find alternative children in *every* mapped collection instance."""
    alternatives = []
    scalars = []

    def walk(obj, list_indexes=(), trace=()):
        if isinstance(obj, list):
            for index, item in enumerate(obj):
                walk(item, list_indexes + (index,), trace[:-1] + (trace[-1] + f'[{index}]',) if trace else (f'[{index}]',))
            return
        if not isinstance(obj, dict):
            return
        for name, value in obj.items():
            here = trace + (name,)
            if name in group_by_name:
                for relation_index, children, marked in group_by_name[name]:
                    if isinstance(value, dict):
                        chosen = [child for child in children if child in value]
                        if chosen:
                            status = 'mapped_unique' if len(chosen) == 1 else 'ambiguous_multiple'
                            vp_path = vp_paths.get(name, '')
                            instance_path = substitute_array_indices(vp_path, list_indexes) if vp_path else '/'.join(here)
                            for child in chosen:
                                alternatives.append(dict(document_id=doc_id, kind=kind,
                                    api_version=version, vp_feature=name, vp_path=vp_path,
                                    relation_index=relation_index, instance_path=instance_path,
                                    selected_variant=child, default_variant=marked[0] if len(marked)==1 else '',
                                    has_unique_default=(len(marked)==1),
                                    matches_default=(child==marked[0]) if len(marked)==1 and status=='mapped_unique' else '',
                                    observed_explicitly=True, mapping_status=status))
            if name in defaults and not isinstance(value, (dict, list)):
                default = defaults[name]
                match = type(default) is type(value) and default == value
                scalars.append(dict(document_id=doc_id, kind=kind, api_version=version,
                    feature=name, instance_path='/'.join(here), yaml_value=json.dumps(value, default=str),
                    default_value=json.dumps(default, default=str),
                    comparison_status='explicit_equal' if match else 'explicit_different'))
            if isinstance(value, (dict, list)):
                walk(value, list_indexes, here)
    walk(mapped.get('config', {}))
    return alternatives, scalars


def explicit_vp_opportunities(yaml_occurrences, vp_paths):
    """Count context-addressable VP occurrences without completing any FM.

    An opportunity is present when its YAML parent object exists, even if the
    particular VP property is absent. Every array-item object is an independent
    context. Nested VPs are NOT treated as omitted if their parent is absent.
    This is a structural/opportunity denominator, not SAT-derived applicability.
    Unknown/mapping-only paths are deliberately not counted.
    """
    parent_objects = defaultdict(list)
    visible_paths = set()
    for normalized, concrete, value in yaml_occurrences:
        visible_paths.add(concrete)
        if isinstance(value, dict):
            parent_objects[normalized].append(concrete)
    parent_objects[''].append('')
    results = []
    unaddressable = []
    for vp, path in vp_paths.items():
        if not path or '{}' in path or path.endswith('[]'):
            unaddressable.append(vp)
            continue
        parent, sep, leaf = path.rpartition('.')
        if not leaf or '[]' in leaf or '{}' in leaf:
            unaddressable.append(vp)
            continue
        for concrete_parent in parent_objects.get(parent, ()):
            concrete = f'{concrete_parent}.{leaf}' if concrete_parent else leaf
            results.append((vp, concrete, concrete in visible_paths))
    return results, unaddressable


def is_numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def mapped_metadata_anomalies(mapped, metadata_by_feature, raw_patterns):
    """Conservative diagnostic for metadata path inconsistencies.

    Only inspect leaf alternative/value features; complex dynamic maps and
    special computed helpers may not have an exact path correspondence.
    """
    problems = []
    def walk(x):
        if isinstance(x, list):
            for y in x: walk(y)
        elif isinstance(x, dict):
            for feature, value in x.items():
                if not isinstance(value, (dict, list)):
                    entries = metadata_by_feature.get(feature, ())
                    value_entries = [e for e in entries if e.get('serializationRole') in ('value', 'property')]
                    if value_entries and all(e.get('configPath') not in raw_patterns for e in value_entries):
                        problems.append({'feature':feature, 'expected_paths':[e.get('configPath') for e in value_entries]})
                else:
                    walk(value)
    walk(mapped.get('config', {}))
    return problems


MANIFEST_COLS = ['document_id','source_type','source_path','document_index','content_sha256','source_repo',
                 'kind','api_version','root_feature','status','error','mapping_status',
                 'n_unmapped','n_ignored','n_mapping_path_anomalies','mapping_path_anomalies',
                 'n_yaml_paths','n_metadata_matched_paths','n_scalar_vp_candidates',
                 'n_scalar_vp_candidates_mapped','n_scalar_vp_candidates_unmapped',
                 'n_scalar_vp_candidate_mapping_pct','n_applicable_vp_occurrences',
                 'n_explicit_vp_occurrences','n_omitted_vp_occurrences',
                 'n_explicit_unmapped_vp_occurrences','n_vps_without_addressable_path',
                 'n_observed_vps',
                 'n_variant_observations','n_unique_variant_observations',
                 'n_ambiguous_variant_observations','n_scalar_comparisons',
                 'n_comparable_default_selections','n_matching_default_selections',
                 'n_nondefault_selections','n_numeric_default_comparisons',
                 'n_numeric_default_matches','n_numeric_overrides','sat_status',
                 'sat_detail','schema_validation_status','schema_valid',
                 'schema_validation_detail','analysis_included','validation_exclusion_reason',
                 'time_mapping_s']
VARIANT_COLS = ['document_id','source_type','kind','api_version','vp_feature','vp_path','relation_index',
                'instance_path','selected_variant','default_variant','has_unique_default',
                'matches_default','observed_explicitly','mapping_status']
SCALAR_COLS = ['document_id','source_type','kind','api_version','feature','instance_path','yaml_value',
               'default_value','comparison_status']



# The three append-only CSVs are the source of truth. A tiny atomic JSON
# checkpoint records their last committed BYTE lengths and the next file index.
# On resume the tail after those lengths is truncated, then the global counters
# are rebuilt in one streaming pass (no unbounded in-memory document cache).
CHECKPOINT_VERSION = 2
STREAM_FILES = ('rq3_manifests.csv', 'rq3_variant_observations.csv', 'rq3_scalar_defaults.csv')


def run_signature(args, files, manifest_dir):
    """Catch accidental mixing of datasets, model revisions, and SAT modes."""
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(manifest_dir).as_posix().encode('utf-8'))
        digest.update(b'\n')
    def file_sig(path):
        p = path.resolve()
        st = p.stat()
        return {'path': str(p), 'bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}
    return {'manifest_dir': str(manifest_dir), 'files': len(files),
            'file_list_sha256': digest.hexdigest(),
            'source_type': args.source_type, 'validation': args.validation,
            'filter_invalid': args.filter_invalid,
            'kubeconform_k8s_version': args.kubeconform_k8s_version if args.validation in ('schema','both') else None,
            'kubeconform_schema_location': args.kubeconform_schema_location if args.validation in ('schema','both') else None,
            'kubeconform_timeout': args.kubeconform_timeout if args.validation in ('schema','both') else None,
            'first_document_only': args.first_document_only,
            'first_folder_is_repo': args.first_folder_is_repo,
            'uvl': file_sig(args.uvl), 'metadata': file_sig(args.metadata),
            'descriptions': file_sig(args.descriptions), 'mapper': file_sig(args.mapper),
            'evaluator': file_sig(args.evaluator) if args.validation!='none' else None}


def save_checkpoint(checkpoint_path, signature, next_index, handles, completed=False):
    """Flush all three CSVs BEFORE atomically advancing the progress pointer."""
    offsets = {}
    for name, handle in zip(STREAM_FILES, handles):
        handle.flush()
        os.fsync(handle.fileno())
        offsets[name] = os.fstat(handle.fileno()).st_size
    payload = {'version': CHECKPOINT_VERSION, 'signature': signature,
               'next_file_index': next_index, 'csv_byte_offsets': offsets,
               'complete': completed}
    temp = checkpoint_path.with_name(checkpoint_path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as h:
        json.dump(payload, h, ensure_ascii=False, indent=2)
        h.flush()
        os.fsync(h.fileno())
    os.replace(temp, checkpoint_path)


def recover_checkpoint(checkpoint_path, signature, output):
    if not checkpoint_path.exists():
        raise ValueError('No checkpoint found; --resume requires a prior v3 run in --output-dir')
    checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    if checkpoint.get('version') != CHECKPOINT_VERSION or checkpoint.get('signature') != signature:
        raise ValueError('Resume refused: changed dataset/file list, model, metadata, or relevant options. '
                         'Use the original inputs or a NEW output directory.')
    for name in STREAM_FILES:
        p = output / name
        end = checkpoint['csv_byte_offsets'][name]
        if not p.is_file() or p.stat().st_size < end:
            raise ValueError(f'CSV shorter than committed checkpoint: {p}; cannot safely resume')
        with p.open('r+b') as f:
            f.truncate(end)   # discard an interrupted/uncommitted tail, if present
    return int(checkpoint['next_file_index'])


def int_or_zero(value):
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def restore_counters(output, totals, source_hashes, by_resource, by_vp, by_variant, table_by_kind):
    """Rebuild global aggregates from committed rows on resume only."""
    root_for_document = {}
    with (output / STREAM_FILES[0]).open('r', encoding='utf-8-sig', newline='') as h:
        for row in csv.DictReader(h):
            if row.get('document_index'):
                totals['documents'] += 1
            status = row.get('status')
            if status == 'evaluated':
                totals['mapped_documents'] += 1
                totals['included_documents'] += 1
            elif status == 'excluded_validation':
                totals['excluded_validation'] += 1
                if row.get('mapping_status') in ('mapped','mapped_with_unmapped','path_anomaly'):
                    totals['mapped_documents'] += 1
            elif status:
                totals[status] += 1
            sha = row.get('content_sha256')
            if sha:
                source_hashes[sha] += 1
            if status != 'evaluated':
                continue
            root, kind, version = row['root_feature'], row['kind'], row['api_version']
            root_for_document[row['document_id']] = root
            resources = by_resource[(version, kind, root)]
            resources['mapped_documents'] += 1
            for metric in ('n_applicable_vp_occurrences','n_explicit_vp_occurrences',
                           'n_omitted_vp_occurrences','n_explicit_unmapped_vp_occurrences',
                           'n_comparable_default_selections','n_nondefault_selections',
                           'n_numeric_default_comparisons','n_numeric_default_matches',
                           'n_numeric_overrides'):
                resources[metric] += int_or_zero(row.get(metric))
            resources['scalar_vp_candidates'] += int_or_zero(row.get('n_scalar_vp_candidates'))
            resources['scalar_vp_candidates_mapped'] += int_or_zero(row.get('n_scalar_vp_candidates_mapped'))
            resources['unique_choices'] += int_or_zero(row.get('n_unique_variant_observations'))
            resources['default_comparable'] += int_or_zero(row.get('n_comparable_default_selections'))
            resources['default_matches'] += int_or_zero(row.get('n_matching_default_selections'))
            if kind in ('Pod', 'Deployment'):
                table = table_by_kind[kind]
                table['mapped_documents'] += 1
                for metric in ('n_applicable_vp_occurrences','n_explicit_vp_occurrences',
                               'n_omitted_vp_occurrences','n_explicit_unmapped_vp_occurrences',
                               'n_vps_without_addressable_path',
                               'n_comparable_default_selections','n_matching_default_selections',
                               'n_nondefault_selections','n_numeric_default_comparisons',
                               'n_numeric_default_matches','n_numeric_overrides'):
                    table[metric] += int_or_zero(row.get(metric))
    with (output / STREAM_FILES[1]).open('r', encoding='utf-8-sig', newline='') as h:
        for row in csv.DictReader(h):
            if row.get('mapping_status') != 'mapped_unique':
                continue
            root = root_for_document.get(row['document_id'])
            if not root:
                continue
            vp, variant = row['vp_feature'], row['selected_variant']
            by_vp[(root, vp)]['observations'] += 1
            by_variant[(root, vp, variant)] += 1
            if row.get('has_unique_default') == 'True':
                by_vp[(root, vp)]['default_comparable'] += 1
                if row.get('matches_default') == 'True':
                    by_vp[(root, vp)]['default_matches'] += 1


def run(args):
    module = load_py(args.mapper, 'rq3_metadata_parser')
    CachedMapper = make_cached_mapper(module.MetadataMapper)
    mapper = CachedMapper(str(args.descriptions), str(args.metadata), strict=False)
    fm, features, groups, scalar_defaults = model_index(args.uvl)
    evaluator = load_py(args.evaluator, 'rq3_reused_evaluator') if args.validation != 'none' else None
    sat_validator = evaluator.FMSATValidator(fm) if args.validation in ('sat','both') else None
    roots_cache = {}
    entries_by_feature = defaultdict(list)
    for entry in mapper.serialization_entries:
        if isinstance(entry, dict) and isinstance(entry.get('feature'), str):
            entries_by_feature[entry['feature']].append(entry)
    manifest_dir = args.manifest_dir.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    files = sorted([*manifest_dir.rglob('*.yaml'), *manifest_dir.rglob('*.yml')])
    if args.limit_files:
        files = files[:args.limit_files]
    if not files:
        raise ValueError(f'No .yaml/.yml files found in {manifest_dir}')
    signature = run_signature(args, files, manifest_dir)
    checkpoint_path = output / 'rq3_checkpoint.json'
    existing = [output / name for name in STREAM_FILES] + [checkpoint_path]
    if args.resume:
        start_index = recover_checkpoint(checkpoint_path, signature, output)
    else:
        start_index = 0
        if not args.overwrite and any(p.exists() for p in existing):
            raise ValueError('The output directory already has RQ3 results. Use --resume '
                             'for a v3 checkpoint, --overwrite to discard them, or a new output directory.')
    totals=Counter()
    source_hashes = Counter()
    by_resource = defaultdict(Counter)
    by_vp = defaultdict(Counter)
    by_variant = Counter()
    table_by_kind = defaultdict(Counter)
    if args.resume:
        restore_counters(output, totals, source_hashes, by_resource, by_vp, by_variant, table_by_kind)
        print(f'Resuming from file {start_index}/{len(files)}: '
              f'{totals["mapped_documents"]} committed mapped documents',flush=True)
    mode, encoding = ('a', 'utf-8') if args.resume else ('w', 'utf-8-sig')
    stop_request = {'stop': False}
    def request_stop(signum, frame):
        stop_request['stop'] = True
        print('\nInterrupt requested; finishing current file and checkpointing.', flush=True)
    try:
        old_sigint = signal.signal(signal.SIGINT, request_stop)
    except ValueError:
        old_sigint = None  # Not in main thread; resume still works after hard kill.
    last_done_index = start_index
    with (output/'rq3_manifests.csv').open(mode,encoding=encoding,newline='') as fman, \
         (output/'rq3_variant_observations.csv').open(mode,encoding=encoding,newline='') as fvar, \
         (output/'rq3_scalar_defaults.csv').open(mode,encoding=encoding,newline='') as fscalar:
        wman=csv.DictWriter(fman,fieldnames=MANIFEST_COLS,extrasaction='ignore')
        wvar=csv.DictWriter(fvar,fieldnames=VARIANT_COLS,extrasaction='ignore')
        wscalar=csv.DictWriter(fscalar,fieldnames=SCALAR_COLS,extrasaction='ignore')
        if not args.resume:
            wman.writeheader();wvar.writeheader();wscalar.writeheader()
        try:
            for i, path in enumerate(files[start_index:],start_index + 1):
                if stop_request["stop"] or (args.stop_after_files and i > args.stop_after_files):
                    break
                try:
                    rel=path.relative_to(manifest_dir).as_posix()
                    repo = rel.split('/')[0] if args.first_folder_is_repo and '/' in rel else ''
                    try:
                        contents = path.read_text(encoding='utf-8-sig')
                        docs = list(yaml.safe_load_all(contents))
                    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as e:
                        row=dict(document_id=rel+'#parse',source_type=args.source_type,source_path=rel,document_index='',
                                 source_repo=repo,status='parse_error',error=f'{type(e).__name__}: {e}'[:1000],
                                 mapping_status='not_run',sat_status='not_run')
                        write_row(wman,fman,row)
                        totals['parse_error']+=1
                        continue
                    if args.first_document_only:
                        docs = docs[:1]
                    for di, doc in enumerate(docs, 1):
                        document_id=rel+f'#doc{di}'
                        row=dict(document_id=document_id,source_type=args.source_type,source_path=rel,document_index=di,
                                 source_repo=repo,status='started',sat_status='not_run',
                                 schema_validation_status='not_run',analysis_included=False)
                        totals['documents']+=1
                        if not isinstance(doc, dict):
                            row.update(status='invalid_document',error='Expected a mapping/object; null documents are not analyzed',
                                       mapping_status='not_run')
                            write_row(wman,fman,row);totals['invalid_document']+=1
                            continue
                        row['kind']=doc.get('kind','')
                        row['api_version']=doc.get('apiVersion','')
                        try:
                            canonical=json.dumps(doc,sort_keys=True,ensure_ascii=False,default=str)
                        except (TypeError,ValueError) as exc:
                            row.update(status='invalid_document',mapping_status='not_run',
                                       error=f'Unserializable YAML keys: {exc}'[:400])
                            write_row(wman,fman,row);totals['invalid_document']+=1
                            continue
                        raw_sha=hashlib.sha256(canonical.encode('utf-8')).hexdigest()
                        row['content_sha256']=raw_sha
                        source_hashes[raw_sha]+=1
                        if not isinstance(row['kind'],str) or not isinstance(row['api_version'],str):
                            row.update(status='unsupported_gvk',mapping_status='unsupported_gvk',
                                       error='kind/apiVersion must be strings')
                            write_row(wman,fman,row);totals['unsupported_gvk']+=1
                            continue
                        try:
                            root=mapper.resolve_root_feature(row['api_version'],row['kind'])
                        except Exception as e:
                            row.update(status='unsupported_gvk',mapping_status='unsupported_gvk',error=str(e)[:500])
                            write_row(wman,fman,row);totals['unsupported_gvk']+=1
                            continue
                        row['root_feature']=root
                        if args.validation in ('schema','both'):
                            temp_path = None
                            try:
                                # One document per validation: multiple YAML resources can have distinct outcomes.
                                with tempfile.NamedTemporaryFile('w', suffix='.yaml', encoding='utf-8',
                                                                 delete=False) as tmp:
                                    temp_path=Path(tmp.name)
                                    yaml.safe_dump(doc,tmp,allow_unicode=True,sort_keys=False)
                                try:
                                    schema_result=evaluator.kubeconform_check(
                                        temp_path,args.kubeconform_k8s_version,
                                        args.kubeconform_timeout,args.kubeconform_schema_location,
                                        args.kubeconform_cache_dir)
                                finally:
                                    temp_path.unlink(missing_ok=True)
                                row['schema_validation_status']=schema_result.get('schema_validation_status','unavailable')
                                row['schema_valid']=schema_result.get('schema_valid','')
                                row['schema_validation_detail']=schema_result.get('schema_validation_detail','')
                            except Exception as e:
                                row['schema_validation_status']='unavailable'
                                row['schema_validation_detail']=f'{type(e).__name__}: {e}'[:1000]
                                if temp_path is not None:
                                    temp_path.unlink(missing_ok=True)
                            if args.filter_invalid and row['schema_validation_status']!='passed':
                                row.update(status='excluded_validation',mapping_status='not_run_filtered',
                                           analysis_included=False,
                                           validation_exclusion_reason='schema_'+row['schema_validation_status'])
                                totals['excluded_validation']+=1
                                write_row(wman,fman,row)
                                continue
                        if root not in roots_cache:
                            gs,ds=indexes_for_root(root,groups,scalar_defaults)
                            vp_paths={}
                            for name, relations in gs.items():
                                candidates=[e.get('configPath','') for e in entries_by_feature.get(name,())
                                            if e.get('serializationRole') in ('property','structural')]
                                # Some synthetic alternative parents have no property entry;
                                # their children may nonetheless share the YAML value path.
                                if not any(candidates):
                                    for _, children, _ in relations:
                                        candidates.extend(e.get('configPath','') for child in children
                                            for e in entries_by_feature.get(child,())
                                            if e.get('serializationRole') == 'value')
                                vp_paths[name]=next((p for p in candidates if p), '')
                            # Explicit scalar VP opportunities with independent
                            # YAML denominators; structural groups are out of scope.
                            scalar_candidate_paths=defaultdict(set)
                            for vp, group_relations in gs.items():
                                vp_path=vp_paths.get(vp,'')
                                if not vp_path:
                                    continue
                                for _,children,_ in group_relations:
                                    if any(e.get('serializationRole') == 'value' and
                                           e.get('configPath') == vp_path
                                           for child in children for e in entries_by_feature.get(child,())):
                                        scalar_candidate_paths[vp_path].add(vp)
                            roots_cache[root]=(gs,ds,vp_paths,scalar_candidate_paths)
                        gs, ds, vp_paths, scalar_candidate_paths=roots_cache[root]
                        yaml_occurrences=list(walk_yaml_paths(doc))
                        raw_paths={p for p,_,_ in yaml_occurrences}
                        row['n_yaml_paths']=len(raw_paths)
                        row['n_metadata_matched_paths']=sum(p in mapper._path_index for p in raw_paths) if mapper.last_root_feature==root else ''
                        tic=time.perf_counter()
                        try:
                            mapped=mapper.transform_manifest(doc)
                            report=mapper.mapping_report()
                            # Root-index is activated by transform_manifest().
                            row['n_metadata_matched_paths']=sum(p in mapper._path_index for p in raw_paths)
                            row['n_unmapped']=len(report.get('unmapped',[]))
                            row['n_ignored']=len(report.get('ignored',[]))
                            anomalies=mapped_metadata_anomalies(mapped,entries_by_feature,raw_paths)
                            row['n_mapping_path_anomalies']=len(anomalies)
                            row['mapping_path_anomalies']=json.dumps(anomalies[:5],ensure_ascii=False)
                            row['mapping_status']='mapped_with_unmapped' if report.get('unmapped') else 'mapped'
                            if anomalies:
                                row['mapping_status']='path_anomaly'
                            obs,scalar_obs=extract_observations(mapped,gs,ds,document_id,
                                               row['kind'],row['api_version'],vp_paths)
                            for observation in obs:
                                observation['source_type'] = args.source_type
                            for observation in scalar_obs:
                                observation['source_type'] = args.source_type
                            opportunities, unaddressable=explicit_vp_opportunities(yaml_occurrences,vp_paths)
                            row['n_applicable_vp_occurrences']=len(opportunities)
                            row['n_explicit_vp_occurrences']=sum(is_explicit for _,_,is_explicit in opportunities)
                            row['n_omitted_vp_occurrences']=sum(not is_explicit for _,_,is_explicit in opportunities)
                            row['n_vps_without_addressable_path']=len(unaddressable)
                            row['n_variant_observations']=len(obs)
                            row['n_unique_variant_observations']=sum(o['mapping_status']=='mapped_unique' for o in obs)
                            row['n_ambiguous_variant_observations']=sum(o['mapping_status']!='mapped_unique' for o in obs)
                            row['n_observed_vps']=len({o['vp_feature'] for o in obs if o['mapping_status']=='mapped_unique'})
                            row['n_scalar_comparisons']=len(scalar_obs)
                            candidates={(vp,concrete_path)
                                        for normalized_path,concrete_path,value in yaml_occurrences
                                        if not isinstance(value,(dict,list))
                                        for vp in scalar_candidate_paths.get(normalized_path,())}
                            mapped_observations={(o['vp_feature'],o['instance_path'])
                                                 for o in obs if o['mapping_status']=='mapped_unique'}
                            row['n_explicit_unmapped_vp_occurrences']=sum(
                                is_explicit and (vp, concrete) not in mapped_observations
                                for vp, concrete, is_explicit in opportunities)
                            n_mapped=len(candidates & mapped_observations)
                            comparable=[o for o in obs if o['mapping_status']=='mapped_unique'
                                        and o['has_unique_default']]
                            row['n_comparable_default_selections']=len(comparable)
                            row['n_matching_default_selections']=sum(o['matches_default'] is True for o in comparable)
                            row['n_nondefault_selections']=sum(o['matches_default'] is False for o in comparable)
                            numeric=[o for o in scalar_obs
                                     if is_numeric(json.loads(o['default_value']))
                                     and is_numeric(json.loads(o['yaml_value']))]
                            row['n_numeric_default_comparisons']=len(numeric)
                            row['n_numeric_default_matches']=sum(o['comparison_status']=='explicit_equal' for o in numeric)
                            row['n_numeric_overrides']=sum(o['comparison_status']=='explicit_different' for o in numeric)
                            row['n_scalar_vp_candidates']=len(candidates)
                            row['n_scalar_vp_candidates_mapped']=n_mapped
                            row['n_scalar_vp_candidates_unmapped']=len(candidates)-n_mapped
                            row['n_scalar_vp_candidate_mapping_pct']=round(100*n_mapped/len(candidates),3) if candidates else ''
                            if args.validation in ('sat','both'):
                                try:
                                    first=first_instance_only(mapped)
                                    hypot,conflicts,_=evaluator.mapped_config_decisions(first,root)
                                    if conflicts:
                                        row['sat_status']='unavailable_conflicts_first_projection'
                                        row['sat_detail']=';'.join(sorted(conflicts)[:5])
                                    else:
                                        model_proxy={'features':features}
                                        sat_hypotheses,_=evaluator.project_yaml_hypotheses_for_boolean_sat(hypot,model_proxy)
                                        result,detail=sat_validator.check(sat_hypotheses)
                                        row['sat_status']='passed_first_projection' if result is True else ('rejected_first_projection' if result is False else 'unavailable')
                                        row['sat_detail']=detail
                                except Exception as e:
                                    row['sat_status']='unavailable'
                                    row['sat_detail']=f'{type(e).__name__}: {e}'[:1000]
                            failed_checks=[]
                            if args.validation in ('schema','both') and row['schema_validation_status']!='passed':
                                failed_checks.append('schema_'+row['schema_validation_status'])
                            if args.validation in ('sat','both') and row['sat_status']!='passed_first_projection':
                                failed_checks.append('sat_'+row['sat_status'])
                            include=not (args.filter_invalid and failed_checks)
                            row['analysis_included']=include
                            row['validation_exclusion_reason']=';'.join(failed_checks) if not include else ''
                            row['status']='evaluated' if include else 'excluded_validation'
                            totals['mapped_documents']+=1
                            if not include:
                                totals['excluded_validation']+=1
                            else:
                                totals['included_documents']+=1
                                # Only accepted documents contribute to the source-of-truth
                                # observation CSVs and aggregated CAiSE metrics.
                                for observation in obs:
                                    write_row(wvar,fvar,observation)
                                    if observation['mapping_status']=='mapped_unique':
                                        by_vp[(root,observation['vp_feature'])]['observations']+=1
                                        by_variant[(root,observation['vp_feature'],observation['selected_variant'])]+=1
                                        if observation['has_unique_default']:
                                            by_vp[(root,observation['vp_feature'])]['default_comparable']+=1
                                            if observation['matches_default']:
                                                by_vp[(root,observation['vp_feature'])]['default_matches']+=1
                                for observation in scalar_obs:
                                    write_row(wscalar,fscalar,observation)
                                if row['kind'] in ('Deployment','Pod'):
                                    table_count=table_by_kind[row['kind']]
                                    table_count['mapped_documents']+=1
                                    for key in ('n_applicable_vp_occurrences','n_explicit_vp_occurrences',
                                                'n_omitted_vp_occurrences','n_explicit_unmapped_vp_occurrences',
                                                'n_vps_without_addressable_path',
                                                'n_comparable_default_selections','n_matching_default_selections',
                                                'n_nondefault_selections','n_numeric_default_comparisons',
                                                'n_numeric_default_matches','n_numeric_overrides'):
                                        table_count[key]+=row.get(key,0)
                                resources=by_resource[(row['api_version'],row['kind'],root)]
                                resources['mapped_documents']+=1
                                for key in ('n_applicable_vp_occurrences','n_explicit_vp_occurrences',
                                            'n_omitted_vp_occurrences','n_explicit_unmapped_vp_occurrences',
                                            'n_comparable_default_selections','n_nondefault_selections',
                                            'n_numeric_default_comparisons','n_numeric_default_matches',
                                            'n_numeric_overrides'):
                                    resources[key]+=row[key]
                                resources['scalar_vp_candidates']+=len(candidates)
                                resources['scalar_vp_candidates_mapped']+=n_mapped
                                resources['unique_choices']+=row['n_unique_variant_observations']
                                resources['default_comparable']+=sum(o['has_unique_default'] and o['mapping_status']=='mapped_unique' for o in obs)
                                resources['default_matches']+=sum(o['matches_default'] is True for o in obs)
                        except Exception as e:
                            row.update(status='mapping_error',mapping_status='mapping_error',error=f'{type(e).__name__}: {e}'[:1000])
                            totals['mapping_error']+=1
                        finally:
                            row['time_mapping_s']=round(time.perf_counter()-tic,6)
                            write_row(wman,fman,row)
                    if i%100 == 0:
                        for handle in (fman,fvar,fscalar): handle.flush()
                        print(f'[{i}/{len(files)}] files, {totals["mapped_documents"]} documents mapped',flush=True)
                finally:
                    # All three CSVs must be checkpointed together. A file that
                    # raises an exception before completing is NOT committed.
                    if sys.exc_info()[0] is None:
                        last_done_index = i
                        if (i % args.checkpoint_every == 0 or stop_request['stop']
                                or (args.stop_after_files and i >= args.stop_after_files)):
                            save_checkpoint(checkpoint_path, signature, i,
                                            (fman, fvar, fscalar), completed=False)
                if args.stop_after_files and i >= args.stop_after_files:
                    stop_request['stop'] = True
                    break
        finally:
            # On a successful stop, seal the last completed file. On an
            # unexpected exception, keep the last PREVIOUS checkpoint and let
            # resume truncate any incomplete CSV tails.
            if sys.exc_info()[0] is None:
                save_checkpoint(checkpoint_path, signature, last_done_index,
                                (fman, fvar, fscalar),
                                completed=(last_done_index == len(files)))
            if sat_validator is not None:
                sat_validator.close()
            if old_sigint is not None:
                signal.signal(signal.SIGINT, old_sigint)
    # Restore model VP denominators for kinds whose documents were skipped
    # during resume (the original caches are built lazily per resource).
    for _, _, root in by_resource:
        if root not in roots_cache:
            gs, ds = indexes_for_root(root, groups, scalar_defaults)
            roots_cache[root] = (gs, ds, {}, {})
    with (output/'rq3_by_vp.csv').open('w',encoding='utf-8-sig',newline='') as f:
        cols=['root_feature','vp_feature','n_explicit_unique_choices','n_comparable_defaults','n_matching_defaults','default_alignment_pct']
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
        for (root,vp),v in sorted(by_vp.items()):
            n=v['default_comparable']; matched=v['default_matches']
            w.writerow(dict(root_feature=root,vp_feature=vp,n_explicit_unique_choices=v['observations'],
                            n_comparable_defaults=n,n_matching_defaults=matched,
                            default_alignment_pct=round(100*matched/n,3) if n else ''))
    with (output/'rq3_variant_frequencies.csv').open('w',encoding='utf-8-sig',newline='') as f:
        cols=['root_feature','vp_feature','selected_variant','count']
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
        for (root,vp,variant),count in sorted(by_variant.items()):
            w.writerow(dict(root_feature=root,vp_feature=vp,selected_variant=variant,count=count))
    with (output/'rq3_by_resource.csv').open('w',encoding='utf-8-sig',newline='') as f:
        cols=['api_version','kind','root_feature','mapped_documents','n_model_vps','n_observed_vps',
              'n_applicable_vp_occurrences','n_explicit_vp_occurrences','n_omitted_vp_occurrences',
              'n_explicit_unmapped_vp_occurrences','explicit_rate_pct','omission_rate_pct',
              'n_numeric_default_comparisons','n_numeric_default_matches','n_numeric_overrides',
              'observed_vp_coverage_pct','n_scalar_vp_candidates','n_scalar_vp_candidates_mapped',
              'scalar_vp_candidate_mapping_pct','n_unique_choices','n_comparable_defaults','n_matching_defaults',
              'default_alignment_pct']
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
        for (version,kind,root),counts in sorted(by_resource.items()):
            model_n=len(roots_cache[root][0])
            observed_n=len({vp for r,vp in by_vp if r==root})
            applicable=counts['n_applicable_vp_occurrences']
            n=counts['default_comparable']
            w.writerow(dict(api_version=version,kind=kind,root_feature=root,
                mapped_documents=counts['mapped_documents'],n_model_vps=model_n,n_observed_vps=observed_n,
                n_applicable_vp_occurrences=applicable,
                n_explicit_vp_occurrences=counts['n_explicit_vp_occurrences'],
                n_omitted_vp_occurrences=counts['n_omitted_vp_occurrences'],
                n_explicit_unmapped_vp_occurrences=counts['n_explicit_unmapped_vp_occurrences'],
                explicit_rate_pct=round(100*counts['n_explicit_vp_occurrences']/applicable,3) if applicable else '',
                omission_rate_pct=round(100*counts['n_omitted_vp_occurrences']/applicable,3) if applicable else '',
                n_numeric_default_comparisons=counts['n_numeric_default_comparisons'],
                n_numeric_default_matches=counts['n_numeric_default_matches'],
                n_numeric_overrides=counts['n_numeric_overrides'],
                observed_vp_coverage_pct=round(100*observed_n/model_n,3) if model_n else '',
                n_scalar_vp_candidates=counts['scalar_vp_candidates'],
                n_scalar_vp_candidates_mapped=counts['scalar_vp_candidates_mapped'],
                scalar_vp_candidate_mapping_pct=(
                    round(100*counts['scalar_vp_candidates_mapped']/counts['scalar_vp_candidates'],3)
                    if counts['scalar_vp_candidates'] else ''),
                n_unique_choices=counts['unique_choices'],n_comparable_defaults=n,
                n_matching_defaults=counts['default_matches'],
                default_alignment_pct=round(100*counts['default_matches']/n,3) if n else ''))
    # Publication-ready source x resource rates; each input run uses one source_type.
    # Manifest shares are relative to the mapped Pod + Deployment documents in this source.
    with (output/'rq3_table_metrics.csv').open('w',encoding='utf-8-sig',newline='') as f:
        cols=['source_type','kind','n_manifests','n_source_manifests',
              'manifest_share_pct','n_applicable_vps','n_explicit_vps','explicit_rate_pct',
              'n_omitted_vps','omission_rate_pct','n_explicit_unmapped_vps',
              'n_comparable_defaults','n_default_matches','default_match_pct',
              'n_nondefault_selections','alternative_selection_pct',
              'n_numeric_eligible','n_numeric_matches','numeric_match_pct',
              'n_numeric_overrides','numeric_override_pct','n_unaddressable_vp_definitions']
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
        n_total=sum(table_by_kind[k]['mapped_documents'] for k in ('Deployment','Pod'))
        for kind in ('Deployment','Pod'):
            counts=table_by_kind[kind]
            n=counts['mapped_documents']
            vp=counts['n_applicable_vp_occurrences']
            defs=counts['n_comparable_default_selections']
            numeric=counts['n_numeric_default_comparisons']
            def rate(num, den): return round(100*num/den,3) if den else ''
            w.writerow(dict(source_type=args.source_type,kind=kind,n_manifests=n,
                n_source_manifests=n_total,manifest_share_pct=rate(n,n_total),
                n_applicable_vps=vp,n_explicit_vps=counts['n_explicit_vp_occurrences'],
                explicit_rate_pct=rate(counts['n_explicit_vp_occurrences'],vp),
                n_omitted_vps=counts['n_omitted_vp_occurrences'],
                omission_rate_pct=rate(counts['n_omitted_vp_occurrences'],vp),
                n_explicit_unmapped_vps=counts['n_explicit_unmapped_vp_occurrences'],
                n_comparable_defaults=defs,n_default_matches=counts['n_matching_default_selections'],
                default_match_pct=rate(counts['n_matching_default_selections'],defs),
                n_nondefault_selections=counts['n_nondefault_selections'],
                alternative_selection_pct=rate(counts['n_nondefault_selections'],defs),
                n_numeric_eligible=numeric,n_numeric_matches=counts['n_numeric_default_matches'],
                numeric_match_pct=rate(counts['n_numeric_default_matches'],numeric),
                n_numeric_overrides=counts['n_numeric_overrides'],
                numeric_override_pct=rate(counts['n_numeric_overrides'],numeric),
                n_unaddressable_vp_definitions=counts['n_vps_without_addressable_path']))
    schema_counts=Counter()
    sat_counts=Counter()
    exclusion_reasons=Counter()
    with (output/'rq3_manifests.csv').open('r',encoding='utf-8-sig',newline='') as scan:
        for r in csv.DictReader(scan):
            if r.get('document_index'):
                schema_counts[r.get('schema_validation_status') or 'not_run']+=1
                sat_counts[r.get('sat_status') or 'not_run']+=1
                if r.get('status')=='excluded_validation':
                    exclusion_reasons[r.get('validation_exclusion_reason') or 'unspecified']+=1
    summary=dict(validation=args.validation,filter_invalid=args.filter_invalid,
            schema_validation_counts=dict(schema_counts),sat_validation_counts=dict(sat_counts),
            exclusion_reasons=dict(exclusion_reasons),
            files=len(files),files_completed=last_done_index,
            files_remaining=len(files)-last_done_index,run_complete=(last_done_index == len(files)),
            documents=totals['documents'],source_type=args.source_type,status_counts=dict(totals),
            unique_manifest_contents=len(source_hashes),duplicate_content_groups=sum(n>1 for n in source_hashes.values()),
            evaluated_roots=list(roots_cache),
            sat_mode='first_element_per_array_per_document' if args.validation in ('sat','both') else 'not_run',
            mapping_notes='Explicit variants only; no omitted default is counted as selected. '
                          'Array occurrences counted independently for frequencies. SAT uses first array item only. '
                          'n_metadata_matched_paths is diagnostic path coverage. '
                          'Scalar VP candidate mapping only covers explicit scalar YAML fields with a modeled '
                          'value-alternative metadata path; structural and array-valued choices are out of scope. '
                          'Unrecognized choices outside this scope remain mapper diagnostics.',
            input_files=dict(uvl=str(args.uvl),mapper=str(args.mapper),
                             descriptions=str(args.descriptions),metadata=str(args.metadata)))
    (output/'rq3_summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(f'RQ3 {"complete" if last_done_index == len(files) else "partial checkpoint"}: '
          f'{last_done_index}/{len(files)} files; {totals["mapped_documents"]} mapped documents; output: {output}',
          flush=True)


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--manifest-dir',required=True,type=Path)
    ap.add_argument('--source-type',choices=('Real-World','LLM'),required=True,
                    help='Label for the source group in the CAiSE comparison table')
    ap.add_argument('--uvl',required=True,type=Path)
    ap.add_argument('--mapper',required=True,type=Path)
    ap.add_argument('--metadata',required=True,type=Path)
    ap.add_argument('--descriptions',required=True,type=Path)
    ap.add_argument('--output-dir',required=True,type=Path)
    ap.add_argument('--validation',choices=('none','schema','sat','both'),default='none',
                    help='Validation checks to execute; schema=Kubeconform, sat=one representative FM projection')
    ap.add_argument('--filter-invalid',action='store_true',
                    help='Exclude every document not PASSING all requested checks from RQ3 metrics; keep rejection row')
    ap.add_argument('--sat',action='store_true',help='Backward-compatible alias adding SAT validation')
    ap.add_argument('--kubeconform-k8s-version',
                    help='Required when schema is selected. Schema version (e.g. 1.37.0)')
    ap.add_argument('--kubeconform-schema-location',help='Optional custom Kubeconform schema location')
    ap.add_argument('--kubeconform-timeout',type=int,default=45)
    ap.add_argument('--kubeconform-cache-dir',type=Path,
                    help='Persistent schema cache; recommended for multiple documents')
    ap.add_argument('--evaluator',type=Path,default=Path(__file__).with_name('evaluation_manifests03.py'))
    ap.add_argument('--first-document-only',action='store_true',help='Limit EACH YAML FILE to its first document (not recommended for coverage)')
    ap.add_argument('--first-folder-is-repo',action='store_true',help='Only use if the first directory under manifest-dir really identifies a repository')
    ap.add_argument('--limit-files',type=int,default=0)
    ap.add_argument('--resume',action='store_true',help='Continue after the last v4 checkpoint; never duplicate committed files')
    ap.add_argument('--overwrite',action='store_true',help='Start fresh and discard existing RQ3 outputs in this directory')
    ap.add_argument('--checkpoint-every',type=int,default=200,help='Atomic CSV checkpoint every N source files (default 200)')
    ap.add_argument('--stop-after-files',type=int,default=0,help='Graceful partial run after this absolute file index, resumable later')
    a=ap.parse_args(argv)
    if a.sat:
        a.validation = 'sat' if a.validation=='none' else ('both' if a.validation=='schema' else a.validation)
    if a.filter_invalid and a.validation=='none':
        ap.error('--filter-invalid requires --validation schema, sat, or both')
    if a.validation in ('schema','both'):
        if not a.kubeconform_k8s_version:
            ap.error('--validation schema/both requires --kubeconform-k8s-version')
        if shutil.which('kubeconform') is None:
            ap.error('kubeconform not found in PATH; install it before using --validation schema/both')
        if a.kubeconform_timeout<=0:
            ap.error('--kubeconform-timeout must be > 0')
    if a.checkpoint_every < 1 or a.stop_after_files < 0:
        ap.error('--checkpoint-every must be >= 1 and --stop-after-files >= 0')
    if a.resume and a.overwrite:
        ap.error('--resume and --overwrite are mutually exclusive')
    for p in (a.manifest_dir,a.uvl,a.mapper,a.metadata,a.descriptions):
        if not p.exists():
            ap.error(f'Missing path: {p}')
    if a.validation!='none' and not a.evaluator.is_file():
        ap.error(f'--validation requires evaluation_manifests03.py at {a.evaluator}')
    run(a)


if __name__=='__main__':
    main()