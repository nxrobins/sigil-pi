"""Build exact native inputs and retain every check; no deployment promotion."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import time

from prepare import verify, sha
from guards import Deadline, inventory, verify_inventory

parser = argparse.ArgumentParser()
parser.add_argument('source', type=Path)
parser.add_argument('sigil', type=Path)
parser.add_argument('output', type=Path)
args = parser.parse_args()
root, sigil, out = args.source.resolve(), args.sigil.resolve(), args.output.resolve()
out.mkdir(parents=True, exist_ok=False)
assert os.environ['RUNNER_ENVIRONMENT'] == 'github-hosted'
assert os.environ['RUNNER_ARCH'] == 'X64' and platform.machine() == 'x86_64' and platform.system() == 'Linux'
verify(root)
assert subprocess.check_output(['git','-C',str(sigil),'rev-parse','HEAD'],text=True).strip() == '8277a1d92d599df89e6b4391fc70fd0fa534d696'
env = dict(os.environ, CARGO_BUILD_JOBS='2', PYTHONDONTWRITEBYTECODE='1')
rows=[]
budget=Deadline(90*60,float(os.environ['NATIVE_DIAG_DEADLINE_MONOTONIC']))

def run(name, command, cwd, environment):
    began=time.monotonic()
    with (out/(name+'.log')).open('x') as log:
        try:
            code=subprocess.run(command,cwd=cwd,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=budget.remaining(3600)).returncode
        except subprocess.TimeoutExpired:
            code=124
    rows.append(dict(name=name,command=command,exit_code=code,elapsed_seconds=time.monotonic()-began))
    (out/'build-progress.json').write_text(json.dumps(rows,indent=2)+'\n')
    assert code==0,name

report=dict(passed=False,product_qualified=False,commit=os.environ['GITHUB_SHA'],runner_arch=os.environ['RUNNER_ARCH'],platform=platform.platform(),rows=rows)
try:
    run('runtime-patch',['python',str(root/'scripts/apply_http_runtime_patch.py'),str(sigil),'--mode','apply','--manifest',str(root/'runtime-patches/http-budget-v4.json')],root,env)
    sigil_inventory=inventory(sigil)
    (out/'sigil-source-inventory.json').write_text(json.dumps(sigil_inventory,indent=2)+'\n')
    forge_env=dict(env,CARGO_TARGET_DIR=str(out/'compiler-target'))
    run('compiler',['cargo','build','--locked','--release','-p','sigil-mcp','--features','sigil-mcp/solver'],sigil,forge_env)
    native_env=dict(env,CARGO_TARGET_DIR=str(out/'native-target'))
    for crate in ['worker','store','service']:
        cwd=root/'native'/crate
        for name,command in [('format',['cargo','fmt','--check']),('clippy',['cargo','clippy','--locked','--all-targets','--','-D','warnings']),('test',['cargo','test','--locked','--','--test-threads=1'])]:
            run(crate+'-'+name,command,cwd,native_env)
    run('host-release',['cargo','build','--locked','--release','--bin','sigil-application-host'],root/'native/service',native_env)
    eval_env=dict(env,CARGO_TARGET_DIR=str(out/'evaluator-target'))
    for name,command in [('format',['cargo','fmt','--check']),('clippy',['cargo','clippy','--locked','--all-targets','--','-D','warnings']),('release',['cargo','build','--locked','--release'])]:
        run('evaluator-'+name,command,root/'native/evaluator',eval_env)
    # Exercise the actual release executable under the unchanged worker file
    # admission ceiling. Do not substitute an unoptimized development binary.
    evaluator_path=out/'evaluator-target/release/sigil-fixed-evaluator'
    info=evaluator_path.stat()
    report['evaluator_test_artifact']=dict(profile='release',path=str(evaluator_path),
        bytes=info.st_size,mode=oct(info.st_mode & 0o7777),sha256=sha(evaluator_path))
    (out/'evaluator-test-artifact.json').write_text(json.dumps(report['evaluator_test_artifact'],indent=2)+'\n')
    assert 0 < info.st_size <= 256*1024*1024 and not info.st_mode & 0o6022
    run('evaluator-test',['cargo','test','--locked','--release','--','--test-threads=1'],root/'native/evaluator',eval_env)
    assert sha(evaluator_path)==report['evaluator_test_artifact']['sha256'], 'tested release executable changed'
    binaries={
        'host':out/'native-target/release/sigil-application-host',
        'runtime':out/'compiler-target/release/sigil-mcp',
        'evaluator':out/'evaluator-target/release/sigil-fixed-evaluator',
    }
    for path in binaries.values():
        header=path.read_bytes()[:20]
        assert header[:5]==b'\x7fELF\x02' and header[18:20]==b'\x3e\x00'
    report['binaries']={k:dict(path=str(p),sha256=sha(p)) for k,p in binaries.items()}
    report['stdlib']={str(p.relative_to(sigil)):sha(p) for p in (sigil/'stdlib').rglob('*') if p.is_file()}
    report['passed']=True
except BaseException as error:
    report['error']=type(error).__name__+': '+str(error)
finally:
    try:
        verify(root)
        assert 'sigil_inventory' in locals(), 'post-patch inventory absent'
        verify_inventory(sigil,sigil_inventory)
        report['source_unchanged']=True
        report['sigil_source_unchanged']=True
    except BaseException as error:
        report['passed']=False
        report['source_guard_error']=type(error).__name__+': '+str(error)
    (out/'build-result.json').write_text(json.dumps(report,indent=2)+'\n')
raise SystemExit(0 if report['passed'] else 1)
