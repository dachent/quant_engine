"""Actual attended main preflight/routing exam; only stage subprocess is stopped.

This is NOT financial, Office, panel, committed-predecessor, or completed-stage
resume acceptance. First ledger child returns 97 deliberately, after preflight.
Parent must review proposed source-run extension and seal before dispatch.
"""
from pathlib import Path
import argparse, contextlib, copy, hashlib, io, json, subprocess, sys
from unittest.mock import patch

load=lambda p:json.loads(Path(p).read_bytes())
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,sort_keys=True,indent=2)+'\n',encoding='utf-8')
def check(ok,msg):
    if not ok:raise AssertionError(msg)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--checkout',type=Path,required=True);ap.add_argument('--fixture',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--candidate',action='store_true');a=ap.parse_args()
    checkout=a.checkout.resolve(); out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    fixture=load(a.fixture);check(fixture['status']=='PASS_NATIVE_PREFLIGHT_FIXTURE','NATIVE_PREREADS_REQUIRED')
    if a.candidate:check(fixture['candidate_code_rebuilt'],'CANDIDATE_REQUIRES_NORMAL_H2_REBUILD')
    check(sha(fixture['source_run'])==fixture['source_run_sha256'] and sha(fixture['recipe'])==fixture['recipe_sha256'],'FIXTURE_DRIFT')
    head=subprocess.check_output(['git','-C',str(checkout),'rev-parse','HEAD'],text=True).strip()
    check(head==fixture['candidate_commit'],'FIXTURE_CODE_COMMIT_DIFFERS')
    sys.path[:0]=[str(checkout),str(checkout/'tools'),*map(str,(checkout/'packages').glob('*/src'))]
    import portfolio_pipeline_materialize as materializer
    source=load(fixture['source_run']); source_path=Path(fixture['source_run'])
    runtime=Path(fixture['runtime']); workspace=out/'workspace';workspace.mkdir();(workspace/'dataPortfolio').mkdir()
    cache=out/'retained-cache';cache.mkdir()
    real_run=subprocess.run; cases={}; captured=[]
    def intercept(argv,*args,**kwargs):
        command=[str(v) for v in argv] if not isinstance(argv,str) else [argv]
        if any(Path(v).name=='portfolio_release_builder.py' for v in command):
            check('stage' in command,'ONLY_EXISTING_RELEASE_STAGE_MAY_EXECUTE')
            captured.append(command)
            check(len(captured)==1,'DOWNSTREAM_STAGE_AFTER_FAILURE')
            return subprocess.CompletedProcess(argv,97,stdout='D414_EXAM_FIRST_STAGE_STOP\n',stderr='D414_EXAM_FIRST_STAGE_STOP\n')
        # Existing native readers may issue git and the configured interpreter
        # metadata probe. Nothing else may start a fit/replay/Office/provider child.
        check(command == ['ver'] or Path(command[0]).stem.lower() in ('git','git.cmd','git.exe') or ('-I' in command and '-c' in command and not any('portfolio_' in x for x in command)), 'UNEXPECTED_CHILD:'+repr(command))
        return real_run(argv,*args,**kwargs)
    def invoke(label, *, selected=source_path, run=None, changes=None, execute=True, resume=False):
        captured.clear();run=run or out/('run-'+label)
        values={'--repo-root':str(checkout),'--python':sys.executable,'--workspace-root':str(workspace),'--output-root':str(run),'--retained-cache-root':str(cache),'--requested-as-of':fixture['requested_as_of'],'--source-run':str(selected),'--workbook-runtime':str(runtime)}
        values.update(changes or {})
        argv=['attended',*[v for pair in values.items() for v in pair]]+(['--execute'] if execute else [])+(['--resume'] if resume else [])
        stdout,stderr=io.StringIO(),io.StringIO()
        with patch.object(materializer.subprocess,'run',side_effect=intercept),contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            try:code=materializer.main(argv)
            except SystemExit as exc:code=exc.code
        result={'argv':argv,'exit':code,'stage_calls':copy.deepcopy(captured),'stdout':stdout.getvalue(),'stderr':stderr.getvalue()}
        write(out/(label+'.json'),result);cases[label]=result
        return result
    planning=invoke('planning',execute=False)
    if "invalid choice: 'attended'" in planning['stderr']:
        write(out/'result.json',{'status':'RED_MISSING_ACTUAL_ATTENDED_ENTRYPOINT','native_preconditions':str(a.fixture),'actual_argv':planning['argv'],'stage_calls':0,'scope':'Actual main invocation with native prevalidated selection; no help-only acceptance'})
        print('RED_MISSING_ACTUAL_ATTENDED_ENTRYPOINT_AFTER_NATIVE_PREREADS',flush=True);return 1
    check(planning['exit']==0 and not planning['stage_calls'],'PLANNING_MUST_VALIDATE_WITHOUT_STAGE')
    def first(label,**kwargs):
        result=invoke(label,**kwargs);check(result['exit'] not in (None,0) and len(result['stage_calls'])==1,'FIRST_CHILD_NOT_REACHED_AND_PROPAGATED:'+label)
        command=result['stage_calls'][0]
        check(Path(command[0]).resolve()==Path(sys.executable).resolve(),'EXPLICIT_PYTHON_NOT_FORWARDED')
        check(str(checkout/'tools/portfolio_release_builder.py') in command,'CANDIDATE_STAGE_ENTRYPOINT_NOT_FORWARDED')
        def option(name):check(name in command,'MISSING_CHILD_OPTION:'+name);return command[command.index(name)+1]
        check(option('--role')=='ledger','FIRST_CHILD_NOT_LEDGER')
        for opt,value in [('--recipe',fixture['recipe']),('--output-root',kwargs.get('run',out/('run-'+label))),('--portfolio-root',workspace/'dataPortfolio'),('--retained-cache-root',cache),('--workbook-runtime',runtime)]:
            check(Path(option(opt)).resolve()==Path(value).resolve(),'CHILD_ROUTING:'+opt)
        return result
    first('first-child')
    # A failed first child leaves no verified completed financial stage. Same
    # identity may retry it; changed request/source/runtime must fail first.
    first('resume-same',run=out/'run-first-child',resume=True)
    def reject(label,**kwargs):
        result=invoke(label,**kwargs);check(result['exit'] not in (None,0) and not result['stage_calls'],'NEGATIVE_REACHED_CHILD_OR_PASSED:'+label)
        check(result['stdout'] or result['stderr'],'NEGATIVE_WITHOUT_DIAGNOSTIC:'+label)
    def variant(label,edit):
        value=copy.deepcopy(source);edit(value);path=out/(label+'-source.json');write(path,value);return path
    def recipe_variant(label,edit):
        value=copy.deepcopy(source); original_recipe=load(fixture['recipe']); changed=copy.deepcopy(original_recipe);edit(changed)
        recipe_path=out/(label+'-recipe.json');write(recipe_path,changed)
        value['attended']['release_recipe']=materializer._file_definition(recipe_path)
        # Update selected artifact definitions coherently: the native reader,
        # not an accidentally stale outer hash, must catch producer misbinding.
        for key,new_definition in changed.items():
            old_definition=original_recipe.get(key)
            if isinstance(new_definition,dict) and isinstance(old_definition,dict) and 'path' in new_definition and new_definition!=old_definition:
                p=Path(new_definition['path'])
                definition={'type':'DIRECTORY' if p.is_dir() else 'FILE',**(materializer._directory_definition(p) if p.is_dir() else materializer._file_definition(p))}
                value['artifacts']=[definition if item['path']==old_definition['path'] else item for item in value['artifacts']]
        path=out/(label+'-source.json');write(path,value);return path
    reject('missing-extension-schema',selected=variant('missing-schema',lambda v:v['attended'].pop('schema_version')))
    reject('invalid-source-schema',selected=variant('bad-schema',lambda v:v['attended'].update(schema_version=99)))
    reject('missing-source',selected=out/'absent-source.json')
    reject('missing-recipe',selected=variant('missing-recipe',lambda v:v['attended']['release_recipe'].update(path=str(out/'absent-recipe.json'))))
    reject('changed-recipe-hash',selected=variant('recipe-hash',lambda v:v['attended']['release_recipe'].update(sha256='0'*64)))
    reject('changed-contract-hash',selected=variant('contract-hash',lambda v:v['attended']['financial_input_contract'].update(sha256='0'*64)))
    reject('misbound-history',selected=variant('history',lambda v:v['attended']['history_generation'].update(inventory_sha256='0'*64)))
    original_recipe=load(fixture['recipe'])
    locator=load(original_recipe['dependence_selected_inputs']['path']);locator['provider_admission_receipt_path']=str(out/'absent-admission-receipt.json')
    missing_receipt_locator=out/'missing-receipt-locator.json';write(missing_receipt_locator,locator)
    reject('missing-admission-receipt',selected=recipe_variant('receipt',lambda v:v.update(dependence_selected_inputs=materializer._file_definition(missing_receipt_locator))))
    reject('wrong-epoch-binding',selected=recipe_variant('epoch',lambda v:v['epoch_manifest_generation'].update(generation_id='0'*64)))
    alternate=Path(load(Path(__file__).parent/'h3-native-selection.json')['alternate_ledger_generation_root'])
    reject('valid-producer-b-bound-as-a',selected=recipe_variant('producer-b',lambda v:v.update(provider_ledger_generation=materializer._directory_definition(alternate))))
    reject('omitted-panel',selected=variant('panel',lambda v:v['attended']['required_stages'].remove('panel')))
    invalid_predecessor=out/'invalid-predecessor.json';write(invalid_predecessor,{'schema_version':1})
    reject('invalid-predecessor',selected=variant('predecessor',lambda v:v['attended'].update(predecessor_spec=materializer._file_definition(invalid_predecessor))))
    occupied=out/'occupied';occupied.mkdir();sentinel=occupied/'sentinel.txt';sentinel.write_text('retain-exactly',encoding='utf-8');before=sha(sentinel)
    reject('occupied-output',run=occupied);check(sha(sentinel)==before,'OCCUPIED_SENTINEL_CHANGED')
    reject('invalid-date',changes={'--requested-as-of':'not-a-date'})
    reject('missing-runtime',changes={'--workbook-runtime':str(out/'absent-runtime.json')})
    invalid_runtime=out/'invalid-runtime.json';write(invalid_runtime,{'schema_version':99})
    reject('invalid-runtime',changes={'--workbook-runtime':str(invalid_runtime)})
    reject('wrong-repo',changes={'--repo-root':str(out/'absent-repo')})
    reject('missing-python',changes={'--python':str(out/'absent-python.exe')})
    reject('overlapping-root',changes={'--output-root':str(workspace/'dataPortfolio')})
    reject('resume-date',run=out/'run-first-child',resume=True,changes={'--requested-as-of':'2026-09-18'})
    source_change=variant('changed-source',lambda v:v.update(as_of_date='2026-09-18'))
    reject('resume-source',run=out/'run-first-child',resume=True,selected=source_change)
    reject('resume-runtime',run=out/'run-first-child',resume=True,changes={'--workbook-runtime':str(invalid_runtime)})
    write(out/'result.json',{'status':'PASS_NATIVE_INITIAL_PREFLIGHT_AND_FIRST_STAGE_ROUTING_ONLY','cases':list(cases),'remaining':fixture['remaining'],'financial_acceptance':False})
    print('PASS_NATIVE_INITIAL_PREFLIGHT_AND_FIRST_STAGE_ROUTING_ONLY',flush=True);return 0
if __name__=='__main__':raise SystemExit(main())
