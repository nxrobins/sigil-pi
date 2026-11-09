"""Native profile14 qualification with an immutable, longer-lived workload."""
from pathlib import Path
import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from prepare import verify, sha
from guards import Deadline, verify_inventory

parser=argparse.ArgumentParser()
for name in ['source','sigil','build','state','output']:
    parser.add_argument(name,type=Path)
args=parser.parse_args()
root,sigil,build,state,out=[getattr(args,n).resolve() for n in ['source','sigil','build','state','output']]
out.mkdir(parents=True,exist_ok=False);state.mkdir(parents=True,exist_ok=False)
assert os.environ['RUNNER_ENVIRONMENT']=='github-hosted'
assert os.environ['RUNNER_ARCH']=='X64' and platform.machine()=='x86_64' and platform.system()=='Linux'
verify(root)
report=json.loads((build/'build-result.json').read_text())
assert report['passed'] and report['source_unchanged'] and report['sigil_source_unchanged']
sigil_inventory=json.loads((build/'sigil-source-inventory.json').read_text())
verify_inventory(sigil,sigil_inventory)
binaries=report['binaries']
for value in binaries.values(): assert sha(Path(value['path']))==value['sha256']
host,runtime,evaluator=[binaries[k]['path'] for k in ['host','runtime','evaluator']]
sys.path.insert(0,str(root/'tests'))
from profile14_evidence import validate_reports
criteria=json.loads((root/'config/profile14-linux-qualification.json').read_text())
assert criteria['budgets']['qualification_minutes']==240
filesystem=subprocess.check_output(['stat','-f','-c','%T',str(state)],text=True).strip()
assert filesystem in {'ext2/ext3','ext4','xfs','btrfs'},filesystem
metrics=out/'service-metrics'
env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PI_REQUIRE_TOOLCHAIN='1',
    PI_FORGE_BIN=runtime,PI_STDLIB_DIR=str(sigil),PI_GENERATION_EVALUATOR=evaluator,
    PI_CAPACITY_HOST=host,HYPOTHESIS_STORAGE_DIRECTORY=str(state/'hypothesis'),
    PI_QUALIFICATION_METRICS_DIR=str(metrics))
pytest=[sys.executable,'-m','pytest','-q','-p','no:cacheprovider','-o','addopts=']
checks=[
 ('compiled',15,['tests/test_generation_facts.py']),
 ('admission',3,['tests/test_profile14_qualification.py','-k','helper_admission']),
 ('functional',11,['tests/test_profile14_qualification.py','-k','not helper_admission and not long_lived']),
 ('endurance',1,['tests/test_profile14_qualification.py::test_long_lived_same_state']),
]
record=dict(passed=False,product_qualified=False,source_manifest_sha256=sha(Path(__file__).with_name('source-manifest.json')),
    criteria_sha256=sha(root/'config/profile14-linux-qualification.json'),binaries=binaries,
    source_unchanged=False,state_filesystem=filesystem,platform=platform.platform(),
    execution_backend='GitHub hosted Ubuntu24.04 X64; sampled actual child ELF identities',
    state_scope='Fresh independent state; then 512 uninterrupted turns plus 64 after reopening the same state and authority',rows=[])
monitor=None;monitor_log=None
started=time.monotonic()
budget=Deadline(240*60,float(os.environ['NATIVE_DIAG_DEADLINE_MONOTONIC']))
try:
    for name,count,selectors in checks:
        if name=='functional':
            monitor_log=(out/'monitor.log').open('x')
            monitor=subprocess.Popen([sys.executable,str(Path(__file__).with_name('monitor.py')),str(build),str(out)],stdout=monitor_log,stderr=subprocess.STDOUT)
        command=pytest+selectors+['--basetemp',str(state/name),'--junitxml',str(out/(name+'-junit.xml'))]
        began=time.monotonic()
        with (out/(name+'.log')).open('x') as log:
            proc=subprocess.Popen(command,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                try: code=proc.wait(timeout=budget.remaining(14400))
                except subprocess.TimeoutExpired: code=124
            finally:
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait(timeout=10)
        record['rows'].append(dict(check=name,command=command,expected_tests=count,exit_code=code,elapsed_seconds=time.monotonic()-began))
        (out/'stage-progress.json').write_text(json.dumps(record['rows'],indent=2)+'\n')
        print(json.dumps(record['rows'][-1]),flush=True)
        assert code==0,name
        suites=ET.parse(out/(name+'-junit.xml')).getroot().findall('testsuite')
        assert sum(int(s.attrib['tests']) for s in suites)==count
        assert all(int(s.attrib[k])==0 for s in suites for k in ['failures','errors','skipped'])
    record['passed']=True
except BaseException as error:
    record['error']=type(error).__name__+': '+str(error)
finally:
    if monitor is not None:
        monitor.terminate()
        try: monitor.wait(timeout=15)
        except subprocess.TimeoutExpired: monitor.kill();monitor.wait()
        monitor_log.close()
    try:
        # Copy observations only; never upload database, authority, credentials or workspaces.
        names={'owner-facts-report.json','helper-failure.json','two-tenant-report.json',
            'concurrent-drain-report.json','long-progress.json','long-performance.json'}
        for path in state.rglob('*.json'):
            if path.name in names:
                target=out/'observations'/path.relative_to(state)
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
        performance=list((out/'observations').rglob('long-performance.json'))
        assert len(performance)==1
        assert monitor is not None and monitor.returncode==0
        record['qualification']=validate_reports(sorted(metrics.glob('open-*.json')),performance[0],out/'process-summary.json',out/'process-samples.jsonl')
    except BaseException as error:
        record['passed']=False;record['evidence_error']=type(error).__name__+': '+str(error)
    try:
        verify(root);verify_inventory(sigil,sigil_inventory)
        for value in binaries.values(): assert sha(Path(value['path']))==value['sha256']
        record.update(source_unchanged=True,sigil_source_unchanged=True)
    except BaseException as error:
        record['passed']=False;record['guard_error']=type(error).__name__+': '+str(error)
    record['elapsed_seconds']=time.monotonic()-started
    (out/'result.json').write_text(json.dumps(record,indent=2)+'\n')
raise SystemExit(0 if record['passed'] else 1)
