"""Bounded diagnostic provenance checks, independent of product authority."""
import hashlib
import math
import subprocess
import time


def inventory(root):
    names=subprocess.check_output(['git','-C',str(root),'ls-files','--cached','--others','--exclude-standard','-z']).decode().split('\0')
    result={}
    for name in filter(None,names):
        path=root/name
        assert path.is_file() and not path.is_symlink(), 'nonregular SIGIL input: '+name
        result[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    assert result
    return result


def verify_inventory(root, expected):
    assert inventory(root)==expected, 'patched SIGIL inventory changed'


class Deadline:
    def __init__(self,seconds,total_until):
        assert math.isfinite(seconds) and seconds > 0 and math.isfinite(total_until)
        self.until=min(time.monotonic()+seconds,total_until)

    def remaining(self, ceiling):
        remaining=self.until-time.monotonic()
        assert remaining>0, 'aggregate diagnostic budget exhausted'
        return min(ceiling,remaining)


class ExecutableIdentities:
    def __init__(self, expected):
        self.expected=expected
        self.receipts={}
        self.errors=[]
        self.seen=set()

    def observe(self, pid, start_ticks, path, read_bytes):
        key=(pid,start_ticks,path)
        self.seen.add(key)
        if key in self.receipts:
            return
        role,expected=self.expected[path]
        raw=read_bytes()
        actual=hashlib.sha256(raw).hexdigest()
        valid=raw[:5]==b'\x7fELF\x02' and raw[18:20]==b'\x3e\x00' and actual==expected
        receipt=dict(pid=pid,start_ticks=start_ticks,path=path,role=role,sha256=actual,native_amd64=valid)
        self.receipts[key]=receipt
        if not valid:
            self.errors.append(receipt)

    def roles(self):
        return sorted({r['role'] for r in self.receipts.values() if r['native_amd64']})
