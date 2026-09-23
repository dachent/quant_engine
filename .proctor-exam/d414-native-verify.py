"""Candidate component verifier driver. Logs retained; no financial acceptance.

CHECKOUT [SCRATCH_PARENT]; uses normal candidate-code fixture reconstruction.
Each invocation creates a unique normal-ACL directory, including runner retries.
Parent must seal this driver, fixture, exam, pins, and frozen auxiliary inputs.
"""
from pathlib import Path
import json,subprocess,sys,uuid
HERE=Path(__file__).resolve().parent
checkout=Path(sys.argv[1]).resolve()
parent=Path(sys.argv[2]).resolve() if len(sys.argv)>2 else checkout/'.scratch'
parent.mkdir(parents=True,exist_ok=True)
output=parent/('e-'+uuid.uuid4().hex[:8])
output.mkdir(exist_ok=False)
print('scratch='+str(output),flush=True)
commands=[
 [sys.executable,'-I','-B',str(HERE/'d414-native-fixture.py'),'--checkout',str(checkout),'--output',str(output/'f'),'--rebuild'],
 [sys.executable,'-I','-B',str(HERE/'d414-native-exam.py'),'--checkout',str(checkout),'--fixture',str(output/'f/fixture-result.json'),'--output',str(output/'exam'),'--candidate'],
 [sys.executable,'-I','-B',str(HERE/'d414-native-resume-exam.py'),str(checkout),str(output/'f/fixture-result.json'),str(output/'resume-exam')],
 [sys.executable,'-I','-B',str(HERE/'d414-normal-analytics-exam.py'),str(checkout),str(output/'f/fixture-result.json'),str(output/'normal-analytics')],
]
results=[]
for index,argv in enumerate(commands):
 log=output/('step-'+str(index)+'.log')
 with log.open('w',encoding='utf-8') as stream:process=subprocess.run(argv,stdout=stream,stderr=subprocess.STDOUT)
 results.append(dict(argv=argv,exit=process.returncode,log=str(log)))
 (output/'results.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
 print(json.dumps(results[-1]),flush=True)
 if process.returncode:raise SystemExit(process.returncode)
print('PASS_D414_COMPONENT_ONLY_NATIVE_COMPLETION_REMAINS_SEPARATE',flush=True)
