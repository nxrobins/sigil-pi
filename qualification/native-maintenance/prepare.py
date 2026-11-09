"""Verify and unpack the explicit source inventory, never repository work state."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile

HERE = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(root):
    manifest = json.loads((HERE / 'source-manifest.json').read_text())
    actual = {str(p.relative_to(root)): sha(p) for p in root.rglob('*') if p.is_file()}
    assert actual == manifest['files'], 'source inventory changed'
    assert not any(p.is_symlink() for p in root.rglob('*'))


def prepare(root):
    inputs = json.loads((HERE / 'inputs.json').read_text())
    assert sha(HERE / 'source-manifest.json') == inputs['manifest_sha256']
    assert sha(HERE / 'source.tar.gz') == inputs['archive_sha256']
    manifest = json.loads((HERE / 'source-manifest.json').read_text())
    assert inputs['bytes'] == sum(manifest['sizes'].values()) < 32 * 1024 * 1024
    assert not root.exists()
    with tarfile.open(HERE / 'source.tar.gz', 'r:gz') as archive:
        members = archive.getmembers()
        assert len(members) == inputs['files'] == len(manifest['files'])
        assert {m.name for m in members} == set(manifest['files'])
        for member in members:
            path = PurePosixPath(member.name)
            assert member.isfile() and not path.is_absolute() and '..' not in path.parts
            assert member.size == manifest['sizes'][member.name]
        root.mkdir(parents=True)
        for member in members:
            target = root / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = archive.extractfile(member).read()
            assert hashlib.sha256(raw).hexdigest() == manifest['files'][member.name]
            with target.open('xb') as stream:
                stream.write(raw)
            target.chmod(0o600)
    verify(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        verify(args.source)
    else:
        prepare(args.source)
