"""Build-only omission of explicitly reviewed, unreferenced SIGIL helpers.

This is not a general optimizer, reachability proof, compiler or runtime policy.
The caller supplies exact definition hashes for a fixed grantless tool artifact.
Only a complete top-level definition with no remaining identifier occurrence
outside its declaration can be removed. Quoted strings never count as references;
comments, interpolation, character syntax and non-ASCII code are refused.
Every requested omission must succeed. Public tool_main is never removable.
Pinned-parser/token checks and fresh compiler admission remain separate gates.
"""

from collections import Counter
from dataclasses import dataclass
import hashlib
import re


IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


@dataclass(frozen=True)
class Omission:
    name: str
    sha256: str
    start: int
    end: int


@dataclass(frozen=True)
class OmittedSource:
    text: str
    removed: tuple[Omission, ...]


def _tokens(source):
    """Conservative identifier/structure spans, NOT the SIGIL lexer."""
    result, at = [], 0
    while at < len(source):
        if source.startswith(("//", "/*", 'f"'), at) or source[at] == "'":
            raise ValueError("omission requires comment-free code without interpolation or characters")
        if source[at] == '"':
            start = at
            at += 1
            while at < len(source) and source[at] != '"':
                at += 2 if source[at] == "\\" else 1
            if at >= len(source):
                raise ValueError("unterminated quoted literal")
            at += 1
            result.append((None, start, at))
        elif source[at] in " \t\r\n":
            at += 1
        elif not source[at].isascii():
            raise ValueError("non-ASCII code is outside the reviewed recipe")
        else:
            match = IDENTIFIER.match(source, at)
            end = match.end() if match else at + 1
            result.append((source[at:end], at, end))
            at = end
    return result


def _functions(tokens):
    def close(start):
        depth = 1
        for end in range(start + 1, len(tokens)):
            depth += (tokens[end][0] == "{") - (tokens[end][0] == "}")
            if depth == 0:
                return end
        raise ValueError("unbalanced function braces")

    functions, at, depth = {}, 0, 0
    while at < len(tokens):
        word = tokens[at][0]
        if word != "fn" or depth != 0:
            depth += (word == "{") - (word == "}")
            if depth < 0:
                raise ValueError("unbalanced top-level braces")
            at += 1
            continue
        if at + 2 >= len(tokens) or not IDENTIFIER.fullmatch(tokens[at + 1][0] or ""):
            raise ValueError("unsupported function declaration")
        name = tokens[at + 1][0]
        if name in functions:
            raise ValueError("ambiguous unqualified function name")
        start = at - 1 if at and tokens[at - 1][0] == "pub" else at
        body = at + 2
        while body < len(tokens) and tokens[body][0] not in {"{", ";"}:
            body += 1
        if body >= len(tokens) or tokens[body][0] != "{":
            raise ValueError("unsupported function body")
        if tokens[body - 1][0] == "!":
            body = close(body) + 1
            if body >= len(tokens) or tokens[body][0] != "{":
                raise ValueError("unsupported effect/body boundary")
        end = close(body)
        functions[name] = (tokens[start][1], tokens[end][2], start != at)
        at = end + 1
    if depth:
        raise ValueError("unbalanced top-level braces")
    return functions


def compact_operator_layout(source):
    """Remove only word/operator whitespace; never join adjacent operators.

    This does not alter a quoted byte or delete a code token. Whitespace between
    operators is retained (for example `< =` cannot become `<=`), as are spaces
    between words, numeric pieces and a possible interpolation prefix/string.
    The independent pinned lexer is still the authority for equivalence.
    """
    tokens = _tokens(source)
    words = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789"
    operators = "=:+-*/%&|^!<>@"
    pieces, at = [], 0
    for left, right in zip(tokens, tokens[1:]):
        start, end = left[2], right[1]
        before, after = source[start - 1], source[end]
        if end > start and ((before in words and after in operators)
                            or (before in operators and after in words)):
            pieces.append(source[at:start])
            at = end
    pieces.append(source[at:])
    return "".join(pieces)


def omit_unreferenced_functions(source, approved):
    if type(source) is not str or not source or len(source.encode()) > 1_048_576:
        raise ValueError("bounded build-only source required")
    if not approved or any(not IDENTIFIER.fullmatch(name) or "tool_main" in name
                           or not re.fullmatch(r"[0-9a-f]{64}", digest)
                           for name, digest in approved.items()):
        raise ValueError("explicit non-entry definition hashes required")
    original_tokens = _tokens(source)
    functions = _functions(original_tokens)
    if [name for name in functions if "tool_main" in name] != ["tool_main"] \
            or not functions["tool_main"][2]:
        raise ValueError("one exact public tool_main entry required")
    for name, digest in approved.items():
        if name not in functions:
            raise ValueError("approved function is missing")
        start, end, _ = functions[name]
        if hashlib.sha256(source[start:end].encode()).hexdigest() != digest:
            raise ValueError("approved function definition changed")
    pending, removed = set(approved), []
    kept = original_tokens
    while pending:
        counts = Counter(word for word, _, _ in kept if word is not None)
        eligible = sorted(name for name in pending if counts[name] == 1)
        if not eligible:
            raise ValueError("an approved function still has a reference")
        for name in eligible:
            start, end, _ = functions[name]
            kept = [token for token in kept if not start <= token[1] < end]
            removed.append(Omission(name, approved[name], start, end))
            pending.remove(name)
    removed.sort(key=lambda item: item.start)
    pieces, at = [], 0
    for item in removed:
        pieces.append(source[at:item.start])
        at = item.end
    pieces.append(source[at:])
    result = "".join(pieces)
    # Refuse an accidental lexical join at an omission boundary even under this
    # conservative scanner. The independent pinned lexer must also agree.
    if [word for word, _, _ in _tokens(result)] != [word for word, _, _ in kept]:
        raise ValueError("omission joined tokens")
    return OmittedSource(result, tuple(removed))
