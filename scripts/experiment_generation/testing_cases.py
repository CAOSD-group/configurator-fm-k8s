#!/usr/bin/env python3
"""Regression tests for final E2-B generator, runner and nil converter.

Works without FlamaPy/PySAT; exercises actual extracted functions plus a
fake end-to-end runner. A REAL UVL integration test is still needed.
"""
from __future__ import annotations

import ast
import csv
import importlib.util
import json
import logging
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


# Embedded regression cases from earlier pilots: no auxiliary files needed.
REGRESSION_NIL_SOURCE = '"""Tests for null/nil absence semantics. Can run without FlamaPy installed."""\nimport ast\nimport logging\nimport re\nfrom pathlib import Path\nfrom types import SimpleNamespace\nfrom typing import Any, Collection, Iterator, Optional\n\nHERE = Path(__file__).resolve().parent\n\nclass Attr:\n    def __init__(self, name, value):\n        self.name, self.value = name, value\n    def get_default_value(self):\n        return self.value\n\nclass Relation:\n    def __init__(self, kind, children):\n        self.kind, self.children = kind, children\n    def is_optional(self): return self.kind == \'optional\'\n    def is_mandatory(self): return self.kind == \'mandatory\'\n    def is_alternative(self): return self.kind == \'alternative\'\n\nclass Feature:\n    def __init__(self, name, feature_type=\'BOOLEAN\', abstract=False, **attrs):\n        self.name = name\n        self.feature_type = feature_type\n        self.is_abstract = abstract\n        self.parent = None\n        self.children = []\n        self.relations = []\n        self.attrs = attrs\n    def get_attributes(self): return [Attr(k, v) for k,v in self.attrs.items()]\n    def get_parent(self): return self.parent\n    def get_children(self): return self.children\n    def get_relations(self): return self.relations\n    def is_boolean(self): return self.feature_type.upper() == \'BOOLEAN\'\n    def add_relation(self, kind, *children):\n        self.relations.append(Relation(kind, list(children)))\n        for c in children:\n            self.children.append(c)\n            c.parent = self\n        return self\n\nclass Configuration:\n    def __init__(self, elements): self.elements = dict(elements)\n    def set_full(self, full): self.full = full\n\nclass FeatureModel:\n    def __init__(self, root):\n        self.root = root\n        self.nodes = {}\n        def visit(n):\n            self.nodes[n.name] = n\n            for ch in n.children: visit(ch)\n        visit(root)\n    def get_feature_by_name(self, name):return self.nodes.get(name)\n\nclass Checker:\n    def is_satisfiable(self, decisions):return True\n\nsource = (HERE / \'main_k8s05_e2b_final.py\').read_text(encoding=\'utf8\')\nkeep = {\'_get_attr\',\'is_absence_default\',\'has_absence_default\', \'is_default\',\n        \'get_default_value\', \'get_config_key_value\',\'has_descendant_config_value\',\n        \'shows_value\',\'iter_ancestors\',\'is_in_scope\',\'get_feature\',\'is_selected\',\n        \'selected_items\',\'value_for\',\'generate_default_configuration\'}\nnode=ast.parse(source)\nfunctions=[n for n in node.body if isinstance(n,ast.FunctionDef) and n.name in keep]\nassert len(functions)==len(keep),keep - {n.name for n in functions}\nctx={\'Feature\':Feature,\'FeatureModel\':FeatureModel,\'Configuration\':Configuration,\n     \'SatChecker\':Checker,\'Any\':Any,\'Collection\':Collection,\'Optional\':Optional,\n     \'Iterator\':Iterator,\'logger\':logging.getLogger(\'test\')}\nexec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),\n             \'nilfix_excerpt\',\'exec\'),ctx)\n\n# Legacy oneOf: a false default, written as \'nil\' on the asString alternative.\nroot=Feature(\'root\')\nvol=Feature(\'emptyDir\')\nlimit=Feature(\'sizeLimit\', abstract=True, serializationKind=\'union\')\nqstr=Feature(\'sizeLimit_asString\',feature_type=\'STRING\',configKey=\'sizeLimit\',\n             serializationRole=\'value\',default=None,defaultValue=\'nil\',\n             defaultValueSource=\'description\')\nqnum=Feature(\'sizeLimit_asNumber\',feature_type=\'REAL\',configKey=\'sizeLimit\',\n             serializationRole=\'value\')\nroot.add_relation(\'optional\',vol)\nvol.add_relation(\'optional\',limit)\nlimit.add_relation(\'alternative\',qstr,qnum)\nfm=FeatureModel(root)\n\nassert ctx[\'is_absence_default\'](qstr)\nassert ctx[\'has_absence_default\'](limit)\nassert not ctx[\'is_default\'](qstr)\nassert ctx[\'get_default_value\'](qstr) is None\n\n# EmptyDir selected, sizeLimit not requested: must remain absent.\nresult=ctx[\'generate_default_configuration\'](fm,Checker(),Configuration({\'emptyDir\':True}))\nassert \'sizeLimit\' not in result.elements, result.elements\nassert \'sizeLimit_asString\' not in result.elements, result.elements\nprint(\'PASS: legacy nil default does not activate optional abstract sizeLimit\')\n\n# Explicit sizeLimit selection is preserved; selected branch should be solver\'s choice.\nresult2=ctx[\'generate_default_configuration\'](fm,Checker(),\n    Configuration({\'sizeLimit\':True, \'sizeLimit_asString\':True}))\nassert result2.elements[\'sizeLimit\'] is True\nassert result2.elements[\'sizeLimit_asString\'] is True\nprint(\'PASS: explicitly requested sizeLimit is preserved\')\n\n# New UVL with explicit absenceDefault at parent.\nlimnew=Feature(\'limitNew\', abstract=True, serializationKind=\'union\',absenceDefault=True,\n               absenceDefaultSource=\'description\')\nvol.add_relation(\'optional\',limnew)\nfm2=FeatureModel(root)\nassert ctx[\'has_absence_default\'](limnew)\nresult3=ctx[\'generate_default_configuration\'](fm2,Checker(),Configuration({\'emptyDir\':True}))\nassert \'limitNew\' not in result3.elements\nprint(\'PASS: new absenceDefault marker suppresses implicit optional union\')\n\n# Other actual default branches remain unchanged.\nchild=Feature(\'typeDefault\',default=None,defaultValue=\'Always\',defaultValueSource=\'description\')\nassert ctx[\'is_default\'](child)\nassert ctx[\'get_default_value\'](child)==\'Always\'\nprint(\'PASS: real scalar defaults retain their behavior\')\n\n# Converter extraction must distinguish an absent default from a scalar default.\nconverter_source=(HERE/\'convert04_final.py\').read_text(encoding=\'utf8\')\ncon_tree=ast.parse(converter_source)\nfound=[n for n in ast.walk(con_tree) if isinstance(n,ast.FunctionDef) and n.name in\n       {\'extract_documented_default\',\'union_default_type\'}]\nassert len(found)==2\n# Only functions; union_default_type has @staticmethod and can be called as a plain function.\nns={\'re\':re}\nexec(compile(ast.fix_missing_locations(ast.Module(body=found,type_ignores=[])),\n             \'convert_excerpt\',\'exec\'),ns)\nd=ns[\'extract_documented_default\'](object(),\n   \'The default is nil which means that the limit is undefined.\')\nassert d[\'semantics\']==\'absence\',d\nassert ns[\'union_default_type\'](d,[{\'type\':\'string\'},{\'type\':\'number\'}]) is None\nregular=ns[\'extract_documented_default\'](object(), \'Defaults to 25%.\')\nassert regular[\'semantics\']==\'documented\' and regular[\'value\']==\'25%\'\nassert ns[\'union_default_type\'](regular,[{\'type\':\'string\'},{\'type\':\'integer\'}])==\'string\'\nprint(\'PASS: converter nil is absence; real union defaults still recognized\')\nprint(\'ALL NULL/NIL PATCH TESTS PASSED\')\n'
REGRESSION_TYPED_SOURCE = '"""Dependency-free unit checks for the E2-B union/array-alternative patch.\nDoes not replace integration testing with real FlamaPy and Kubernetes UVL.\n"""\nimport ast\nimport copy\nimport json\nimport logging\nimport typing\nfrom pathlib import Path\n\nHERE=Path(__file__).resolve().parent\n\nclass A:\n    def __init__(self,name,value):self.name=name;self.value=value\n    def get_default_value(self):return self.value\nclass Relation:\n    def __init__(self,children,alternative=False):self.children=children;self.alt=alternative\n    def is_alternative(self):return self.alt\nclass Feature:\n    def __init__(self,name,typ=\'BOOLEAN\', **attrs):\n        self.name=name;self.feature_type=typ;self.attrs=attrs;self.parent=None;self.children=[];self.relations=[]\n        self.is_abstract=False\n    def add(self,child,alternative=False):\n        child.parent=self\n        self.children.append(child)\n        self.relations.append(Relation([child],alternative))\n        return child\n    def get_parent(self):return self.parent\n    def get_children(self):return self.children\n    def get_relations(self):return self.relations\n    def get_attributes(self):return [A(k,v) for k,v in self.attrs.items()]\n    def is_boolean(self):return self.feature_type.upper()==\'BOOLEAN\'\nclass Config:\n    def __init__(self,**elements):self.elements=elements\n    def get_value(self,name):return self.elements.get(name)\n\nsrc=(HERE/\'main_k8s05_e2b_final.py\').read_text(encoding=\'utf-8\')\nastree=ast.parse(src)\nnames={\'_get_attr\',\'is_absence_default\',\'get_doc\',\'get_config_key_value\',\'get_serialization_attr\',\n       \'get_default_value\',\'typed_union_witness\',\'_child_config_value\',\'manifest_value\',\n       \'_yaml_shape\',\'_value_from_feature\',\'_check_payload_feature\',\n       \'_serialize_collection\',\'_merge_fields\',\'_collection_item\',\'_belongs_to\'}\nns={\'Feature\':Feature,\'FeatureModel\':object,\'Configuration\':Config,\'Any\':typing.Any,\n    \'Optional\':typing.Optional,\'logger\':logging.getLogger(\'test\')}\nfuncs=[n for n in astree.body if isinstance(n,ast.FunctionDef) and n.name in names]\nassert len(funcs)==len(names),names-{n.name for n in funcs}\nexec(compile(ast.fix_missing_locations(ast.Module(body=funcs,type_ignores=[])), \'gen_excerpt\', \'exec\'),ns)\n\nport=Feature(\'example_port\',serializationKind=\'union\',schemaRef=\'#/definitions/...IntOrString\')\nnum=port.add(Feature(\'example_port_asInteger\',typ=\'INTEGER\',configKey=\'port\',serializationRole=\'value\',doc=\'Number must be in the range 1 to 65535. Name must be an IANA_SVC_NAME.\'),alternative=True)\nstring=port.add(Feature(\'example_port_asString\',typ=\'STRING\',configKey=\'port\',serializationRole=\'value\',doc=\'Number must be in the range 1 to 65535. Name must be an IANA_SVC_NAME.\'),alternative=True)\nport.relations=[Relation([num,string],alternative=True)]\nfeatures={n.name:n for n in (port,num,string)}\nassert ns[\'_value_from_feature\'](port,{num.name:True},features)=={\'port\':8080}\nassert ns[\'_value_from_feature\'](port,{string.name:True},features)=={\'port\':\'http\'}\nassert ns[\'_value_from_feature\'](port,{num.name:9090},features)=={\'port\':9090}\nassert ns[\'manifest_value\'](num,Config(**{num.name:True}),{num.name:True})==8080\nprint(\'PASS IntOrString: asInteger -> 8080, asString -> http; explicit 9090 preserved\')\n\nqty=Feature(\'example_sizeLimit\',serializationKind=\'union\',schemaRef=\'#/definitions/resource.Quantity\')\nqstr=qty.add(Feature(\'example_sizeLimit_asString\',typ=\'STRING\',configKey=\'sizeLimit\',serializationRole=\'value\',defaultValue=\'nil\'),alternative=True)\nqnum=qty.add(Feature(\'example_sizeLimit_asNumber\',typ=\'REAL\',configKey=\'sizeLimit\',serializationRole=\'value\'),alternative=True)\nqty.relations=[Relation([qstr,qnum],alternative=True)]\nfeatures.update({n.name:n for n in (qty,qstr,qnum)})\nassert ns[\'_value_from_feature\'](qty,{qstr.name:True},features)=={\'sizeLimit\':\'1Gi\'}\nassert ns[\'_value_from_feature\'](qty,{qnum.name:True},features)=={\'sizeLimit\':1.0}\nprint(\'PASS Quantity: string witness 1Gi (not nil), numeric witness 1.0\')\n\nbm=Feature(\'example_bindMountOptions\',configKey=\'bindMountOptions\',serializationKind=\'array\',schema_type=\'array\')\nnoexec=bm.add(Feature(\'example_bindMountOptions_noexec\',configValue=\'noexec\',serializationRole=\'value\'),alternative=True)\nnodev=bm.add(Feature(\'example_bindMountOptions_nodev\',configValue=\'nodev\',serializationRole=\'value\'),alternative=True)\nbm.relations=[Relation([noexec,nodev],alternative=True)]\nfeatures.update({n.name:n for n in (bm,noexec,nodev)})\nassert ns[\'_value_from_feature\'](bm,[{noexec.name:True}],features)==[\'noexec\']\nprint(\'PASS bindMountOptions: array item represented as a scalar string\')\n\n# Extract the real runner function, exercising both features (no FlamaPy required).\nrsource=(HERE/\'generate_manifests_timed_v5_final.py\').read_text(encoding=\'utf8\')\nrfunc=[n for n in ast.parse(rsource).body if isinstance(n,ast.FunctionDef) and n.name==\'materialize_selected_array_alternatives\']\nassert len(rfunc)==1\nrunner={\'copy\':copy,\'ValueError\':ValueError}\nexec(compile(ast.fix_missing_locations(ast.Module(body=rfunc,type_ignores=[])),\'runner_excerpt\',\'exec\'),runner)\n\ncont=Feature(\'example_containers\',serializationKind=\'array\',schema_type=\'array\')\nvm=cont.add(Feature(\'example_volumeMounts\',configKey=\'volumeMounts\',serializationKind=\'array\',schema_type=\'array\'))\n# under an array, bindMountOptions is a scalar-list field (not object).\nvm.add(bm)\nraw={cont.name:[{vm.name:[{bm.name:True}]}]}\nfull={noexec.name:True}\nres,count=runner[\'materialize_selected_array_alternatives\'](raw,features|{cont.name:cont,vm.name:vm},full)\nassert count==1,res\nbmchoice=res[cont.name][0][vm.name][0][bm.name]\nassert isinstance(bmchoice,list),bmchoice\nassert ns[\'_value_from_feature\'](cont,res[cont.name],features|{cont.name:cont,vm.name:vm})==[\n  {\'volumeMounts\':[{\'bindMountOptions\':[\'noexec\']}]}]\n# Serialize through array serializer in a simplified key shape.\nassert ns[\'_value_from_feature\'](bm,bmchoice,features)==[\'noexec\']\nprint(\'PASS nested array: runner writes list-shaped scalar alternative\')\n\n# All union alternatives in an array are still represented as feature selection;\n# the generator resolves them using the selected typed child.\nhttp=Feature(\'example_httpGet\',configKey=\'httpGet\')\nhttp.add(port)\ncont.add(http)\nraw2={cont.name:[{http.name:{port.name:True}}]}\nres,count=runner[\'materialize_selected_array_alternatives\'](raw2,features|{cont.name:cont,http.name:http},{string.name:True})\nassert count==1\nmapped=ns[\'_value_from_feature\'](cont,res[cont.name],features|{cont.name:cont,http.name:http})\nassert mapped==[{\'httpGet\':{\'port\':\'http\'}}],mapped\nprint(\'PASS nested union: asString selected -> port: http\')\n\nprint(\'ALL E2-B synthetic checks passed\')\n'

def test_existing_regressions():
    for filename, source in (
        ('test_nil_default_patch.py', REGRESSION_NIL_SOURCE),
        ('test_e2b_typed_nilfix.py', REGRESSION_TYPED_SOURCE)):
        exec(compile(source, str(HERE / filename), 'exec'),
             {'__file__': str(HERE / filename), '__name__': '__test__'})


def runner_module():
    path = HERE / 'generate_manifests_timed_v5_final.py'
    spec = importlib.util.spec_from_file_location('runner_final_for_tests', path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_timing_formulas(r):
    row = dict(status='generated', time_partial_read_s=1, time_sat_precheck_s=2,
        time_completion_total_s=3, time_resolve_array_points_s=4, time_sat_forced_array_s=5,
        time_render_yaml_s=.2, time_write_yaml_s=.02, time_build_comments_s=.3,
        time_render_documented_s=.4, time_write_documented_s=.03,
        time_write_full_config_s=.1, time_case_total_s=16.5)
    r.derived_timings(row, documented=True)
    assert row['time_common_s']==15
    assert row['time_plain_manifest_total_s']==15.22
    assert row['time_documented_manifest_total_s']==15.73
    assert row['time_both_manifests_total_s']==15.95
    assert row['time_case_unattributed_s']==.45, row
    no_doc=dict(row, time_documented_manifest_total_s='', time_both_manifests_total_s='')
    r.derived_timings(no_doc, documented=False)
    assert no_doc['time_documented_manifest_total_s']==''
    assert no_doc['time_both_manifests_total_s']==''
    print('PASS timing: shared completion, separate I/O, no double-counting')


def test_unsat_diagnosis(r):
    class Checker:
        def is_satisfiable(self, d):
            return not (d.get('unreachable') is True or
                        (d.get('point_conditional') and d.get('base')))
    class SAT:
        variables = dict(unreachable=1, point_conditional=2, base=3)
    checker=Checker()
    assert r.diagnose_unsat_point(checker, SAT(), {'point_feature':'unreachable'}) == (
        False,'point_unreachable_in_boolean_fm')
    assert r.diagnose_unsat_point(checker, SAT(), {'point_feature':'point_conditional'}) == (
        True,'partial_conflict_with_other_decisions')
    assert r.diagnose_unsat_point(checker, SAT(), {'point_feature':'missing'}) == (
        '','point_not_in_sat_model')
    print('PASS UNSAT diagnosis: unreachable vs partial combination vs unknown')


FAKE_GENERATOR = r'''
import json
from pathlib import Path
class Feature:
    def __init__(self,name,parent=None): self.name=name;self.parent=parent
    def get_parent(self): return self.parent
    def get_relations(self): return []
class FM:
    def __init__(self):
        self.root=Feature('root')
        self.nodes={n:Feature(n,self.root) for n in ('good','unreachable','point_conditional','base')}
        self.nodes['root']=self.root
    def get_features(self): return list(self.nodes.values())
    def get_feature_by_name(self,name): return self.nodes.get(name)
class UVLReader:
    def __init__(self,path): self.path=path
    def transform(self): return FM()
class SATModel:
    def __init__(self): self.variables={n:i for i,n in enumerate(['root','good','unreachable','point_conditional','base'],1)}
class FmToPysat:
    def __init__(self,fm): self.fm=fm
    def transform(self): return SATModel()
class SatChecker:
    def __init__(self,sm): pass
    def is_satisfiable(self,d):
        return not (d.get('unreachable') is True or
                    (d.get('point_conditional') is True and d.get('base') is True))
    def close(self): pass
class PySATMinimumConfiguration:
    def execute(self,*a,**kw): return self
class Config:
    def __init__(self,e):self.elements=e
class SyntheticLogger:
    pass

def generate_default_configuration(*a,**kw): return Config({'root':True})
def complete_and_minimize(fm,sat,partial,expand_to_leaves=False):
    return Config(dict(partial.elements,root=True))
def load_configurations_with_collections(fm,paths):
    return Config(json.loads(Path(paths[0]).read_text(encoding='utf8'))),{}
def render_manifest(fm,full,comments,**kw):
    return 'apiVersion: v1\nkind: Pod\n' if comments is None else 'apiVersion: v1\nkind: Pod\n# documented\\n'
def write_manifest(text,path):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding='utf8')
def selected_items(c):return {k:v for k,v in c.elements.items() if v}
def _sat_forced_array_features(*args,**kw):return []
def get_serialization_attr(*args,**kw):return None
def build_variability_comments(fm,full,partial):return {'note':'document'}
'''


def test_runner_e2e(r):
    from argparse import Namespace
    with tempfile.TemporaryDirectory(prefix='e2b_final_') as temp:
        base=Path(temp)
        src=base/'fake_gen.py';src.write_text(FAKE_GENERATOR,encoding='utf8')
        uvl=base/'model.uvl';uvl.write_text('fake',encoding='utf8')
        partial=base/'inputs';partial.mkdir()
        (partial/'00001_good.json').write_text(json.dumps({'good':True}),encoding='utf8')
        (partial/'00002_unreachable.json').write_text(json.dumps({'unreachable':True}),encoding='utf8')
        (partial/'00003_conflict.json').write_text(json.dumps({'point_conditional':True,'base':True}),encoding='utf8')
        plan=base/'plan.csv'
        with plan.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=['case_id','point_feature','status'])
            writer.writeheader()
            for name,point in [('00001_good','good'),('00002_unreachable','unreachable'),
                               ('00003_conflict','point_conditional')]:
                writer.writerow(dict(case_id=name,point_feature=point,status='planned'))
        out=base/'run'
        args=Namespace(partial_dir=partial,uvl=uvl,generator=src,root_feature='root',
                       output_dir=out,plan_csv=plan,variability_comments=True,
                       resume=False,limit=0)
        assert r.run(args)==0
        with (out/'generation_times.csv').open(encoding='utf-8-sig',newline='') as f:
            rows=list(csv.DictReader(f))
        assert [x['status'] for x in rows]==['generated','unsat','unsat'],rows
        assert rows[1]['unsat_diagnosis']=='point_unreachable_in_boolean_fm',rows[1]
        assert rows[2]['unsat_diagnosis']=='partial_conflict_with_other_decisions',rows[2]
        first=rows[0]
        assert all(float(first[k]) >= 0 for k in (
            'time_common_s','time_plain_manifest_total_s','time_documented_manifest_total_s',
            'time_both_manifests_total_s','time_case_total_s','time_write_yaml_s'))
        assert abs(float(first['time_write_s'])-float(first['time_write_yaml_s'])-
                   float(first['time_write_full_config_s']))<0.00001
        assert (out/'manifests'/'00001_good.yaml').is_file()
        import yaml
        assert yaml.safe_load((out/'manifests'/'00001_good.yaml').read_text(encoding='utf8')) == {
            'apiVersion': 'v1', 'kind': 'Pod'}
        assert (out/'documented_manifests'/'00001_good.yaml').is_file()
        assert (out/'full_configs'/'00001_good.json').is_file()
        summary=json.loads((out/'generation_summary.json').read_text(encoding='utf8'))
        assert summary['status_counts']=={'generated':1,'unsat':2},summary
        assert summary['unsat_diagnosis_counts']=={
            'point_unreachable_in_boolean_fm':1,'partial_conflict_with_other_decisions':1}
        assert len(rows)==3
        print('PASS end-to-end mock run: artifacts, timing, UNSAT classification, CSV summary')


if __name__ == '__main__':
    test_existing_regressions()
    mod=runner_module()
    test_timing_formulas(mod)
    test_unsat_diagnosis(mod)
    test_runner_e2e(mod)
    print('ALL FINAL E2-B LOCAL TESTS PASSED')
