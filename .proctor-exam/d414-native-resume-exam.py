"""Public-main resume control using an actual produced ledger's exact bytes.

Only stage subprocess is instrumented; native readers and artifact checks stay real.
The positive ledger child copies an already produced immutable native generation;
the next child stops deliberately. This is resume component proof, not a new run.
"""
from pathlib import Path
import contextlib,hashlib,io,json,shutil,subprocess,sys
from unittest.mock import patch
W=Path(sys.argv[1]).resolve();fixture=json.loads(Path(sys.argv[2]).read_bytes());out=Path(sys.argv[3]).resolve();out.mkdir(parents=True,exist_ok=False)
sys.path[:0]=[str(W),str(W/'tools'),*[str(p) for p in (W/'packages').glob('*/src')]]
import portfolio_pipeline_materialize as m
R=Path('C:/Dev/worktrees/quant-engine-completion')
source_result=R/'.pt/source/current-native-release-v13/proof/stage-results/ledger.json'
assert hashlib.sha256(source_result.read_bytes()).hexdigest()=='1d805dc6c0e89ef2a9553a4406942cb8c10909bf15b15359727e7b0727a205b3'
ledger=json.loads(source_result.read_bytes());source_root=Path(ledger['root']);source_generation=source_root/'generations'/ledger['generation_id']
source_catalog=source_generation/'provider-ledger.json'
import portfolio_release_builder as release
verified_id,verified_catalog=release._validated_reusable_provider_ledger(source_generation)
assert verified_id==ledger['generation_id'] and verified_catalog.read_bytes()==source_catalog.read_bytes()
ledger['catalog_sha256']=hashlib.sha256(source_catalog.read_bytes()).hexdigest()
baseline_path=R/'.pt/source/current-native-release-v13/proof/stage-results/baseline.json'
assert hashlib.sha256(baseline_path.read_bytes()).hexdigest()=='e445341186077f5438bea254418c864e47f08c8c1270009a26a177cd278e4f0d'
baseline=json.loads(baseline_path.read_bytes());baseline_source=Path(baseline['root'])
baseline_catalog=baseline_source/'generations'/baseline['generation_id']/'portfolio-baseline.json'
assert hashlib.sha256(baseline_catalog.read_bytes()).hexdigest()==baseline['catalog_sha256']
workspace=out/'workspace';(workspace/'dataPortfolio').mkdir(parents=True);cache=out/'cache';cache.mkdir();run=out/'run'
argv=['attended','--repo-root',str(W),'--python',sys.executable,'--workspace-root',str(workspace),'--output-root',str(run),'--retained-cache-root',str(cache),'--requested-as-of',fixture['requested_as_of'],'--source-run',fixture['source_run'],'--workbook-runtime',fixture['runtime'],'--execute']
real_run=subprocess.run;calls=[];install=True
def boundary(command,*args,**kwargs):
    command=[str(v) for v in command]
    if any(Path(v).name=='portfolio_release_builder.py' for v in command):
        role=command[command.index('--role')+1];calls.append(role)
        if role=='ledger' and install:
            dst=run/'stages/ledger';shutil.copytree(source_root,dst)
            result={**ledger,'root':str(dst)};(run/'stage-results').mkdir(exist_ok=True)
            (run/'stage-results/ledger.json').write_text(json.dumps(result),encoding='utf-8')
            return subprocess.CompletedProcess(command,0,stdout='EXACT_NATIVE_LEDGER_COPY_COMPONENT\n',stderr='')
        if role=='baseline' and install:
            dst=run/'stages/baseline';shutil.copytree(baseline_source,dst)
            result={**baseline,'root':str(dst),'spec':str(dst/'config/portfolio-baseline-spec.json')}
            (run/'stage-results/baseline.json').write_text(json.dumps(result),encoding='utf-8')
            return subprocess.CompletedProcess(command,0,stdout='EXACT_NATIVE_BASELINE_COPY_COMPONENT\n',stderr='')
        return subprocess.CompletedProcess(command,97,stdout='',stderr='D414_RESUME_EXAM_ATTRIBUTION_STOP\n')
    assert command == ['ver'] or Path(command[0]).stem.lower() in ('git','git.cmd','git.exe') or ('-I' in command and '-c' in command and not any('portfolio_' in v for v in command)),repr(command)
    return real_run(command,*args,**kwargs)
def invoke(label,extra):
    calls.clear();stdout=io.StringIO();stderr=io.StringIO()
    with patch.object(m.subprocess,'run',side_effect=boundary),contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
        try:code=m.main(argv+extra)
        except SystemExit as e:code=e.code
    result={'code':code,'stage_calls':list(calls),'stdout':stdout.getvalue(),'stderr':stderr.getvalue()}
    (out/(label+'.json')).write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');return result
initial=invoke('initial-native-ledger',[])
assert initial['code']==97 and initial['stage_calls']==['ledger','baseline','attribution'],initial
install=False
resumed=invoke('valid-native-resume',['--resume'])
assert resumed['code']==97 and resumed['stage_calls']==['attribution'],resumed
copied_baseline=run/'stages/baseline/generations'/baseline['generation_id']/'portfolio-baseline.json'
original_baseline=copied_baseline.read_bytes();copied_baseline.write_bytes(original_baseline+b'\n')
bad_baseline=invoke('corrupt-native-baseline',['--resume'])
assert bad_baseline['code'] not in (None,0) and not bad_baseline['stage_calls'],'CORRUPT_NATIVE_BASELINE_WAS_SKIPPED_AS_VERIFIED'
copied_baseline.write_bytes(original_baseline)
assert baseline_catalog.read_bytes()==original_baseline,'ORIGINAL_BASELINE_CHANGED'
catalog=run/'stages/ledger/generations'/ledger['generation_id']/'provider-ledger.json'
content=catalog.read_bytes();catalog.write_bytes(content+b'\n')
assert hashlib.sha256(catalog.read_bytes()).hexdigest()!=ledger['catalog_sha256']
corrupt=invoke('corrupt-native-artifact',['--resume'])
assert corrupt['code'] not in (None,0) and not corrupt['stage_calls'],'CORRUPT_NATIVE_ARTIFACT_WAS_SKIPPED_AS_VERIFIED'
assert source_catalog.read_bytes()==content,'ORIGINAL_NATIVE_SOURCE_CHANGED'
(out/'result.json').write_text(json.dumps({'status':'PASS_NATIVE_COMPLETED_LEDGER_AND_BASELINE_RESUME_COMPONENT','actual_source_generation':ledger['generation_id'],'valid_resume_child':resumed['stage_calls'],'corrupt_resume_children':corrupt['stage_calls'],'financial_run_acceptance':False},indent=2)+'\n',encoding='utf-8')
print('PASS_NATIVE_COMPLETED_LEDGER_AND_BASELINE_RESUME_COMPONENT')
