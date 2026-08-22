"""Minimal deterministic SIGIL stdlib composition used by the runtime host."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


MODULE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
HASH_HEX_LEN = 24


@dataclass(frozen=True)
class ComposedSource:
    text: str
    stdlib_hash: str
    modules_included: tuple[str, ...]
    stdlib_line_offset: int = 0


def _normalize(raw):
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized = "\n".join(line.rstrip() for line in lines)
    return normalized if normalized.endswith("\n") else normalized + "\n"


@lru_cache(maxsize=64)
def _read(path):
    return _normalize(Path(path).read_text(encoding="utf-8"))


def compose_with_stdlib(source, stdlib_modules, repo_root):
    for name in stdlib_modules:
        if not MODULE_RE.fullmatch(name):
            raise ValueError(f"invalid SIGIL stdlib module name: {name!r}")
    modules = tuple(sorted(set(stdlib_modules)))
    if not modules:
        return ComposedSource(source, "0" * HASH_HEX_LEN, ())
    root = Path(repo_root) / "stdlib" / "sigil"
    parts = []
    for name in modules:
        path = root / f"{name}.sigil"
        if not path.is_file():
            raise ValueError(f"SIGIL stdlib module {name!r} not found at {path}")
        parts.append(_read(str(path)))
    stdlib = "".join(parts)
    return ComposedSource(
        stdlib + source,
        hashlib.sha256(stdlib.encode()).hexdigest()[:HASH_HEX_LEN],
        modules,
        stdlib.count("\n"),
    )

