"""Read-only native Linux test process monitor with actual executable checks.

Select only the application host command in this dedicated test container and
its descendants. Read no argv, environment, descriptors, or application data.
RSS sums may count shared pages repeatedly; cgroup figures are separate.
"""
from pathlib import Path
import argparse
import datetime
import hashlib
import json
import os
import signal
import time
from guards import ExecutableIdentities

parser = argparse.ArgumentParser()
parser.add_argument('build', type=Path)
parser.add_argument('output', type=Path)
args = parser.parse_args()
base = args.output
binaries = json.loads((args.build / 'build-result.json').read_text())['binaries']
by_path = {row['path']: (role, row['sha256']) for role, row in binaries.items()}
identities = ExecutableIdentities(by_path)

output = base / 'process-samples.jsonl'
summary = base / 'process-summary.json'
started = time.monotonic()
stop = False
samples = written = peak_rss = peak_processes = 0
host_instances = set()


def terminate(_signum, _frame):
    global stop
    stop = True


signal.signal(signal.SIGTERM, terminate)
signal.signal(signal.SIGINT, terminate)
with output.open('x') as out:
    while not stop and time.monotonic() - started < 14400 and written < 64 * 1024 * 1024:
        rows = {}
        for p in Path('/proc').iterdir():
            if not p.name.isdigit():
                continue
            try:
                stat = (p / 'stat').read_text()
                fields = stat[stat.rindex(')') + 2:].split()
                status = dict(line.split(':', 1) for line in (p / 'status').read_text().splitlines() if ':' in line)
                rows[int(p.name)] = {
                    'pid': int(p.name), 'ppid': int(fields[1]),
                    'comm': (p / 'comm').read_text().strip(),
                    'exe': os.readlink(p / 'exe'), 'start_ticks': int(fields[19]),
                    'user_ticks': int(fields[11]), 'system_ticks': int(fields[12]),
                    'rss_kib': int(status.get('VmRSS', '0 kB').split()[0]),
                    'threads': int(status['Threads']),
                }
            except (OSError, ValueError, KeyError, IndexError):
                continue
        selected = {
            pid for pid, row in rows.items()
            if row['exe'] == binaries['host']['path']
        }
        roots = sorted(selected)
        host_instances.update((pid, rows[pid]["start_ticks"]) for pid in roots)
        for _ in range(16):
            more = {pid for pid, row in rows.items() if row['ppid'] in selected}
            if more <= selected:
                break
            selected |= more
        if selected:
            for pid in selected:
                row = rows[pid]
                if row['exe'] in by_path:
                    try:
                        identities.observe(pid, row['start_ticks'], row['exe'],
                            lambda pid=pid: Path(f'/proc/{pid}/exe').read_bytes())
                    except OSError:
                        pass  # Exited before observation; verdict requires every observed instance.
            record = {
                'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'elapsed_seconds': time.monotonic() - started,
                'clock_ticks_per_second': os.sysconf('SC_CLK_TCK'),
                'root_pids': roots,
                'processes': [rows[pid] for pid in sorted(selected)],

            }
            line = json.dumps(record) + '\n'
            out.write(line)
            out.flush()
            written += len(line.encode())
            samples += 1
            peak_rss = max(peak_rss, sum(rows[pid]['rss_kib'] for pid in selected))
            peak_processes = max(peak_processes, len(selected))
        time.sleep(1)
result = {
    'stop_reason': 'requested' if stop else ('duration_cap' if time.monotonic()-started >= 14400 else 'byte_cap'),
    'coverage_truncated': not stop,
    'observed_host_instances': len(host_instances),
    'samples': samples, 'bytes': written, 'valid_process_selection_observed': samples > 0,
    'peak_sum_rss_kib': peak_rss, 'peak_processes': peak_processes,
    'raw_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
    'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'scope': 'Actual native AMD64 application host executable and descendants in a dedicated hosted-runner job; RSS sum is not physical memory.',
    'observed_roles': identities.roles(), 'identity_errors': identities.errors,
    'executable_receipts': list(identities.receipts.values()),
    'unverified_instances': sorted(identities.seen - identities.receipts.keys()),
    'observed_host_identities': sum(r['role']=='host' and r['native_amd64'] for r in identities.receipts.values()),
    'monitor_shares_runner': True, 'product_qualified': False,
}
summary.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result), flush=True)
