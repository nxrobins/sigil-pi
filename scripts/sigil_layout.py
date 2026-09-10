"""Conservative layout compaction for the explicit comment-free SIGIL recipe.

The pinned lexer treats ()[]{},; as standalone single-character tokens and
ignores ASCII whitespace. Remove whitespace only adjacent to those tokens;
else collapse it to one space. Never change quoted contents or join operators,
identifiers, numbers, or an identifier `f` with a string. Unsupported lexical
forms are refused, not guessed. Compiler admission and source limits still apply.
"""

SPACE = " \t\r\n"
SINGLE = "()[]{},;"


def compact_layout(source):
    out, at = [], 0
    while at < len(source):
        interpolation = source.startswith('f"', at) and (
            at == 0 or source[at - 1] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
        if source.startswith(("//", "/*"), at) or interpolation or source[at] == "'":
            raise ValueError("layout compaction requires comment-free SIGIL without interpolation or character syntax")
        if source[at] == '"':
            start = at
            at += 1
            while at < len(source) and source[at] != '"':
                at += 2 if source[at] == "\\" else 1
            if at >= len(source):
                raise ValueError("unterminated literal in layout input")
            at += 1
            out.append(source[start:at])
        elif source[at] in SPACE:
            start = at
            while at < len(source) and source[at] in SPACE:
                at += 1
            before = source[start - 1] if start else ""
            after = source[at] if at < len(source) else ""
            if before and after and before not in SINGLE and after not in SINGLE:
                out.append(" ")
        else:
            out.append(source[at])
            at += 1
    return "".join(out)
