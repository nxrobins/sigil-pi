"""Fresh uninterrupted maintenance checks on a Linux local filesystem."""
from pathlib import Path
import json
import os
import platform
import argparse
import shutil
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
out.mkdir(parents=True,exist_ok=False)
state.mkdir(parents=True,exist_ok=False)
assert os.environ['RUNNER_ENVIRONMENT']=='github-hosted'
assert os.environ['RUNNER_ARCH']=='X64' and platform.machine()=='x86_64'
verify(root)
report=json.loads((build/'build-result.json').read_text())
assert report['passed'] and report['source_unchanged'] and report['sigil_source_unchanged']
sigil_inventory=json.loads((build/'sigil-source-inventory.json').read_text())
verify_inventory(sigil,sigil_inventory)
binaries=report['binaries']
for value in binaries.values():
    assert sha(Path(value['path']))==value['sha256']
host,runtime,evaluator=[binaries[k]['path'] for k in ['host','runtime','evaluator']]
sys.path.insert(0,str(root/'tests'))
from maintenance_evidence import TIMING_REPORTS, validate_monitor, validate_timings
filesystem=subprocess.check_output(['stat','-f','-c','%T',str(state)],text=True).strip()
assert filesystem in {'ext2/ext3','ext4','xfs','btrfs'},filesystem
env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PI_REQUIRE_TOOLCHAIN='1',
    PI_FORGE_BIN=runtime, PI_STDLIB_DIR=str(sigil), PI_GENERATION_EVALUATOR=evaluator,
    PI_CAPACITY_HOST=host, HYPOTHESIS_STORAGE_DIRECTORY=str(state/'hypothesis'))
python = sys.executable
pytest = [python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-o', 'addopts=']
checks = [
    ('two-tenant', pytest + ['tests/test_generation_maintenance_tenants.py',
        'tests/test_generation_maintenance_concurrent.py', '--basetemp', str(state/'two-tenant')]),
    ('endurance', pytest + ['tests/test_generation_maintenance_service.py::test_actual_handoff_reuses_slots_and_preserves_history_and_dedup[beyond_128]',
        '--basetemp', str(state/'endurance')]),
]
record = dict(passed=False, product_qualified=False, source_manifest_sha256=sha(Path(__file__).with_name('source-manifest.json')),
    host_sha256=sha(Path(host)), runtime_sha256=sha(Path(runtime)), evaluator_sha256=sha(Path(evaluator)),
    source_unchanged=False, state_filesystem=filesystem, platform=platform.platform(),
    execution_backend='GitHub hosted Ubuntu24.04 X64 runner; actual child ELF identities independently observed',
    state_scope='Fresh independent state on runner-local filesystem; no prior database or authority copied',
    stdlib_files={str(p.relative_to(sigil)): sha(p) for p in (sigil/'stdlib').rglob('*') if p.is_file()},
    rows=[])
monitor_log = (out / 'monitor.log').open('x')
monitor = subprocess.Popen([python, str(Path(__file__).with_name('monitor.py')), str(build), str(out)], stdout=monitor_log, stderr=subprocess.STDOUT)
started = time.monotonic()
budget=Deadline(120*60,float(os.environ['NATIVE_DIAG_DEADLINE_MONOTONIC']))
try:
    for name, command in checks:
        command = command + ['--junitxml', str(out / (name + '-junit.xml'))]
        began = time.monotonic()
        with (out / f'{name}.log').open('x') as log:
            try:
                code = subprocess.run(command, cwd=root, env=env, stdout=log,
                    stderr=subprocess.STDOUT, timeout=budget.remaining(7200)).returncode
            except subprocess.TimeoutExpired:
                code = 124
        record['rows'].append(dict(check=name, command=command, exit_code=code,
                                   elapsed_seconds=time.monotonic() - began))
        (out / 'stage-progress.json').write_text(json.dumps(record['rows'], indent=2) + '\n')
        print(json.dumps(record['rows'][-1]), flush=True)
        assert code == 0, name
        suites=ET.parse(out / (name + '-junit.xml')).getroot().findall('testsuite')
        assert sum(int(s.attrib['tests']) for s in suites) == (4 if name == 'two-tenant' else 1)
        assert all(int(s.attrib[key]) == 0 for s in suites for key in ['failures','errors','skipped'])
    record['passed'] = True
except BaseException as error:
    record['error'] = type(error).__name__ + ': ' + str(error)
finally:
    try:
        # Copy only observation reports. The sole authority and live state stay in
        # this disposable volume and are never included in host evidence copies.
        names = set(TIMING_REPORTS) | {'handoff-metrics.json', 'maintenance-report.json', 'progress.json', 'two-tenant-report.json', 'concurrent-drain-report.json'}
        metrics = []
        for path in state.rglob('*.json'):
            if path.name in names:
                target = out / 'observations' / path.relative_to(state)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                if path.name in TIMING_REPORTS:
                    metrics.append(target)
        try:
            validate_timings(metrics)
            record['all_nine_opens_measured_within_bounds'] = True
        except (AssertionError, KeyError, TypeError, ValueError) as error:
            record['passed'] = False
            record['timing_error'] = type(error).__name__ + ': ' + str(error)
        verify(root)
        verify_inventory(sigil,sigil_inventory)
        for value in binaries.values():
            assert sha(Path(value['path']))==value['sha256']
        record.update(source_unchanged=True, sigil_source_unchanged=True, elapsed_seconds=time.monotonic() - started)
    except BaseException as error:
        record['passed']=False
        record['final_observation_error']=type(error).__name__+': '+str(error)
    monitor.terminate()
    try:
        monitor.wait(timeout=10)
    except subprocess.TimeoutExpired:
        monitor.kill()
        monitor.wait()
    monitor_log.close()
    try:
        record['process_observation'] = validate_monitor(monitor.returncode,
            out / 'process-summary.json', out / 'process-samples.jsonl')
        assert record['process_observation']['observed_roles'] == ['evaluator','host','runtime']
        assert not record['process_observation']['identity_errors']
        assert not record['process_observation']['unverified_instances']
        assert record['process_observation']['observed_host_identities']==9
    except (AssertionError, OSError, KeyError, TypeError, ValueError) as error:
        record['passed'] = False
        record['monitor_error'] = type(error).__name__ + ': ' + str(error)
    (out / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
raise SystemExit(0 if record['passed'] else 1)
