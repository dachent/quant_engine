"""Parent-owned native fixture preparation; never fits, publishes, or alters pins.

--rebuild is mandatory for candidate acceptance: materializer is trusted H2 code.
Without it this only prereads the retained native baseline for the baseline RED.
"""
from pathlib import Path
from datetime import date
import argparse, contextlib, hashlib, io, json, subprocess, sys

HERE = Path(__file__).resolve().parent
STAGES = ['ledger', 'baseline', 'attribution', 'analytics', 'report', 'workbook', 'panel-candidate', 'panel']
load = lambda p: json.loads(Path(p).read_bytes())
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p, value):
    Path(p).write_text(json.dumps(value, sort_keys=True, indent=2)+'\n', encoding='utf-8')
def check(value, message):
    if not value: raise AssertionError(message)
def install(checkout):
    sys.path[:0] = [str(checkout), str(checkout/'tools'), *map(str, (checkout/'packages').glob('*/src'))]

def prepare(checkout, output, rebuild):
    checkout, output = checkout.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    pins = load(HERE/'d414-native-pins.json')
    for item in pins['files']:
        p = Path(item['path'])
        check(p.is_file() and not p.is_symlink() and p.stat().st_size == item['size_bytes'] and sha(p) == item['sha256'], 'FROZEN_DRIFT:'+str(p))
    install(checkout)
    import portfolio_pipeline_materialize as materializer
    import portfolio_release_builder as release
    import portfolio_target_series as history
    import portfolio_current_dependence as dependence
    import portfolio_current_decomposition_plan as planner
    head = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
    for args in (['diff','--quiet'], ['diff','--cached','--quiet']):
        check(subprocess.run(['git','-C',str(checkout),*args], capture_output=True).returncode == 0, 'CLEAN_COMMITTED_CANDIDATE_REQUIRED')
    original_recipe_path = Path(pins['recipe'])
    # Pins above establish exact retained metadata. Candidate changes to trusted
    # H2 dependencies require normal reconstruction before deep current-code
    # validation. The newly produced recipe is still read by _recipe below.
    original = load(original_recipe_path) if rebuild else release._recipe(original_recipe_path)
    locator = load(original['dependence_selected_inputs']['path'])
    h1_path = HERE/'d414-native-h1-selection.json'
    h1_pin = next(item for item in pins['files'] if item['path']==pins['h1_selection'])
    check(sha(h1_path)==h1_pin['sha256'], 'SEALED_H1_SELECTION_DRIFT')
    h1 = load(h1_path)
    ledger = Path(locator['provider_ledger_generation_root'])
    contract = Path(locator['financial_input_contract_path'])
    native = materializer._provider_admission_authority(ledger, load(contract), **h1['admission_authority'])
    check(native['admission_gate_status']=='PASS' and native['product_readiness']=='READY_FOR_COMPOSITION', 'ACTUAL_PROVIDER_ADMISSION_FAILED')
    print('PASS_NATIVE_PROVIDER_ADMISSION', flush=True)
    history_root = Path(locator['history_generation_root'])
    reader = dict(provider_admission_receipt_path=Path(locator['provider_admission_receipt_path']), financial_input_contract_path=contract,
                  provider_ledger_manifest_path=ledger/'generation.json', provider_ledger_catalog_path=ledger/'provider-ledger.json',
                  portfolio123_version_evidence_path=Path(locator['portfolio123_version_evidence_path']))
    unchanged = {}
    if rebuild:
        kwargs = {k:Path(v) if k.endswith('_path') and v is not None else v for k,v in h1['builder_kwargs'].items()}
        kwargs.update(repository_sha=head, portfolio123_version_evidence_path=reader['portfolio123_version_evidence_path'])
        artifacts, manifest = history.build_layered_history_generation(**kwargs)
        new_history = history.write_layered_history_generation(output/'history', artifacts, manifest)
        for name, content in artifacts.items():
            if name.endswith('.csv'):
                check(content == (history_root/name).read_bytes(), 'ECONOMIC_HISTORY_CHANGED:'+name)
                unchanged[name] = hashlib.sha256(content).hexdigest()
        check(len(unchanged)==5, 'FIVE_ECONOMIC_HISTORY_FILES_REQUIRED')
        history_root = new_history
        locator['history_generation_root'] = str(history_root)
    for mode in ('LIVE_ONLY','LIVE_PLUS_BACKTEST'):
        history.verify_layered_history_generation(history_root, contract, require_mode=mode, **reader)
    selected = output/'selected-inputs.json'; write(selected, locator)
    policy = checkout/'contracts/portfolio-dependence-policy-v1.json'
    if rebuild:
        built = dependence.write_source(output/'dependence', contract_path=contract, policy_path=policy,
            history_generation_root=history_root, allocation_generation_root=Path(locator['allocation_generation_root']),
            evaluated_on=date.fromisoformat(original['as_of_date']), selected_inputs_path=selected)
        dependence_root = Path(built['destination'])
    else:
        dependence_root = Path(original['correlation_source_root']['path'])
    dependence.verify_packaged_source(dependence_root, contract_path=contract, policy_path=policy, selected_inputs_path=selected)
    print('PASS_NATIVE_HISTORY_BOTH_MODES_AND_SELECTED_DEPENDENCE', flush=True)
    plans = {}
    if rebuild:
        for mode in ('LIVE_ONLY', 'LIVE_PLUS_BACKTEST'):
            invocation = load(HERE/('native-plan-v13-'+mode.lower()+'-invocation.json'))['argv']
            args = invocation[invocation.index('tools/portfolio_current_decomposition_plan.py')+1:]
            plan_output = output/('l' if mode == 'LIVE_ONLY' else 'h')
            for option, value in (('--layered-history-generation',history_root), ('--output-root',plan_output)):
                args[args.index(option)+1] = str(value)
            with (output/('compile-'+mode.lower()+'.log')).open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log):
                check(planner.main(args)==0, 'NATIVE_PLAN_COMPILATION_FAILED:'+mode)
            dirs = list(plan_output.iterdir()); check(len(dirs)==1, 'PLAN_OUTPUT_NOT_UNIQUE')
            plans[mode] = dirs[0]
        recipe_path = output/'input-recipe.json'
        kwargs = dict(provider_ledger_spec=None, provider_ledger_generation=ledger,
            as_of_date=original['as_of_date'], pipeline_id=original['pipeline_id'], release_commit=head, output=recipe_path,
            correlation_source_root=dependence_root, dependence_selected_inputs_path=selected,
            decomposition_plan_bundle=plans['LIVE_ONLY'], hybrid_decomposition_plan_bundle=plans['LIVE_PLUS_BACKTEST'],
            epoch_manifest_generation_root=Path(original['epoch_manifest_generation']['runtime_root']),
            epoch_manifest_generation_id=original['epoch_manifest_generation']['generation_id'],
            strategy_attribution_generation_id=original['strategy_attribution_root']['generation_id'])
        for key in ('baseline_spec','base_factor_file','implementation_reconciliation','implementation_return_attribution','strategy_attribution_root','legacy_panel'):
            kwargs[key] = Path(original[key]['path'])
        release.create_recipe(**kwargs)
    else:
        recipe_path = original_recipe_path
    recipe = release._recipe(recipe_path)
    check(recipe['schema_version']==10, 'SCHEMA10_REQUIRED')
    release._history_contract_path(recipe)
    materializer._epoch_manifest_generation_definition(recipe)
    runtime = Path(pins['runtime'])
    resolved_runtime = release._resolve_workbook_runtime(runtime)
    print('PASS_NATIVE_SCHEMA10_EPOCH_DEPENDENCE_AND_RUNTIME_PREREADS', flush=True)
    extension = dict(schema_version=1, release_recipe=materializer._file_definition(recipe_path),
        history_generation=materializer._directory_definition(history_root),
        allocation_generation=materializer._directory_definition(Path(locator['allocation_generation_root'])),
        financial_input_contract=materializer._file_definition(contract), required_stages=STAGES, predecessor_spec=None)
    # Initial selection is distinct from legacy acquire()/authority() post-proof payload.
    # No QA output, financial-result placeholder, post-proof recipe, or PASS flag is inserted.
    artifacts = []
    for key, definition in recipe.items():
        if isinstance(definition, dict) and 'path' in definition:
            p = Path(definition['path'])
            artifacts.append({'type':'DIRECTORY' if p.is_dir() else 'FILE', **(materializer._directory_definition(p) if p.is_dir() else materializer._file_definition(p))})
    source = dict(schema_version=1, as_of_date=recipe['as_of_date'], release_commit=head,
                  artifacts=sorted(artifacts,key=lambda item:item['path']), attended=extension)
    source_path=output/'source-run.json'; write(source_path, source)
    result = dict(status='PASS_NATIVE_PREFLIGHT_FIXTURE', candidate_code_rebuilt=rebuild, candidate_commit=head,
        source_run=str(source_path), source_run_sha256=sha(source_path), recipe=str(recipe_path), recipe_sha256=sha(recipe_path),
        runtime=str(runtime), requested_as_of=recipe['as_of_date'], history_generation=str(history_root),
        identical_economic_history=unchanged, resolved_runtime_identity=resolved_runtime.identity,
        remaining=['Actual attended implementation', 'Governed HWM/capital result selections when produced',
                   'Verified committed predecessor positive and hidden-predecessor negative',
                   'Successful completed-stage resume/corruption proof', 'Full native financial/workbook/panel and reuse acceptance'])
    write(output/'fixture-result.json', result)
    print(json.dumps({'status':result['status'],'fixture':str(output/'fixture-result.json'),'rebuild':rebuild}),flush=True)
    return result

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--checkout',type=Path,required=True); ap.add_argument('--output',type=Path,required=True); ap.add_argument('--rebuild',action='store_true')
    a=ap.parse_args(); prepare(a.checkout,a.output,a.rebuild)
