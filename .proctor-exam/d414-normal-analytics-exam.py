"""Actual build_analytics normal reader, stopping only the expensive compute boundary."""
from pathlib import Path
import json,sys
from unittest.mock import patch
W=Path(sys.argv[1]).resolve();fixture=json.loads(Path(sys.argv[2]).read_bytes());out=Path(sys.argv[3]).resolve();out.mkdir(parents=True,exist_ok=False)
sys.path[:0]=[str(W),str(W/'tools'),*[str(p) for p in (W/'packages').glob('*/src')]]
import portfolio_release_builder as release
recipe_path=Path(fixture['recipe']);recipe=json.loads(recipe_path.read_bytes());modes=[]
class StopBeforeCompute(Exception):pass
def trace(frame,event,arg):
    if event=='return' and frame.f_globals.get('__name__')==release.__name__ and frame.f_code.co_name=='_current_release_plan_specs' and isinstance(arg,list):
        modes.append(frame.f_locals['plan']['policies']['strategy_evidence_mode'])
def stop(*args,**kwargs):raise StopBeforeCompute()
sys.setprofile(trace)
try:
    with patch.object(release,'_build_base_analytics',side_effect=stop):
        release.build_analytics(recipe_path,Path(recipe['decomposition_plan_bundle']['path']),out/'never-computed')
except StopBeforeCompute:
    assert modes==['LIVE_ONLY','LIVE_PLUS_BACKTEST'],modes
finally:sys.setprofile(None)
assert modes==['LIVE_ONLY','LIVE_PLUS_BACKTEST'],modes
result={'status':'PASS_ACTUAL_NORMAL_ANALYTICS_BOTH_PLAN_PREREADS','modes':modes,'fits':0,'financial_acceptance':False}
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps(result))
