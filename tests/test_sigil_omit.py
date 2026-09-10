"""Build-only omission guards; independent compiler checks live separately."""

import hashlib

import pytest

from conftest import PI_ROOT, SIGIL_ROOT
from scripts.readiness_entry_base import APPROVED, prepare_entry_base
from scripts.sigil_omit import compact_operator_layout, omit_unreferenced_functions


LEAF = "fn leaf() -> i64 { return 1; }"
PARENT = "fn parent() -> i64 { return leaf(); }"
ENTRY = "pub fn tool_main(p: i64, n: i64) -> i64 { return 0; }"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("limit,bytes_expected", [(2, 55994), (9223372036854775807, 56012)])
def test_only_the_five_exact_reviewed_definitions_are_removed(limit, bytes_expected):
    built = prepare_entry_base(PI_ROOT, SIGIL_ROOT, request_limit=limit)
    assert built == prepare_entry_base(PI_ROOT, SIGIL_ROOT, request_limit=limit)
    assert len(built.text.encode()) == bytes_expected
    assert sum(row.end - row.start for row in built.omissions) == 2717
    assert len(built.original.encode()) - len(built.layout_only.encode()) == 4898
    assert len(built.original.encode()) - bytes_expected == 7355
    assert {row.name: row.sha256 for row in built.omissions} == APPROVED
    assert len(built.input_hashes) == 22
    for row in built.omissions:
        assert digest(built.original[row.start:row.end]) == row.sha256
    assert built.text.count("pub fn tool_main(") == 1
    assert "AH6\\n" in built.text and "HC6\\n" in built.text


def test_unused_dependency_chain_is_removed_without_reordering_retained_bytes():
    before = "module sample;\n" + LEAF + "\n" + PARENT + "\n" + ENTRY
    result = omit_unreferenced_functions(before, {"leaf": digest(LEAF), "parent": digest(PARENT)})
    assert result.text == "module sample;\n\n\n" + ENTRY
    assert [item.name for item in result.removed] == ["leaf", "parent"]


@pytest.mark.parametrize("reference", ["leaf()", "module_name::leaf()", "leaf"])
def test_any_remaining_identifier_reference_refuses_an_omission(reference):
    before = LEAF + ENTRY.replace("return 0", "return " + reference)
    with pytest.raises(ValueError, match="still has a reference"):
        omit_unreferenced_functions(before, {"leaf": digest(LEAF)})


def test_definition_text_in_a_string_is_preserved_and_is_not_a_reference():
    entry = ENTRY.replace("return 0", 'let x = "leaf fn fake() { \\\"nested\\\" } café"; return 0')
    assert omit_unreferenced_functions(LEAF + entry, {"leaf": digest(LEAF)}).text == entry


def test_alloc_effect_braces_are_not_mistaken_for_the_function_body():
    leaf = "fn leaf() -> i64 ! { Alloc } { if true { return 1; } return 0; }"
    assert omit_unreferenced_functions(leaf + ENTRY, {"leaf": digest(leaf)}).text == ENTRY


@pytest.mark.parametrize("extra", ["// comment", "/* comment */", 'f"interpolation"', "'a'", "café"])
def test_unsupported_lexical_forms_are_refused(extra):
    with pytest.raises(ValueError):
        omit_unreferenced_functions(LEAF + ENTRY + extra, {"leaf": digest(LEAF)})


@pytest.mark.parametrize("before,approved", [
    (LEAF + ENTRY, {}),
    (LEAF + ENTRY, {"tool_main": digest(ENTRY)}),
    (LEAF + ENTRY, {"leaf": "0" * 64}),
    (ENTRY, {"leaf": digest(LEAF)}),
    (LEAF + LEAF + ENTRY, {"leaf": digest(LEAF)}),
    (LEAF + ENTRY.replace("pub ", ""), {"leaf": digest(LEAF)}),
    (LEAF + ENTRY.replace("tool_main", "tool_main_alternate"), {"leaf": digest(LEAF)}),
    (LEAF + ENTRY + '"unfinished', {"leaf": digest(LEAF)}),
    (LEAF + ENTRY + "{", {"leaf": digest(LEAF)}),
])
def test_missing_changed_ambiguous_or_entry_definitions_are_not_omitted(before, approved):
    with pytest.raises(ValueError):
        omit_unreferenced_functions(before, approved)


@pytest.mark.parametrize("before,after", [
    ("a + b", "a+b"),
    ("a < = b", "a< =b"),
    ("a - - b", "a- -b"),
    ('x = f "quoted"', 'x=f "quoted"'),
    ('x = "value + label"', 'x= "value + label"'),
    ('"literal" + value', '"literal" +value'),
    ('x = "\\\"quoted café + value\\\""', 'x= "\\\"quoted café + value\\\""'),
    ("i64 @Internal", "i64@Internal"),
    ("namespace :: field", "namespace::field"),
    ('f "text"', 'f "text"'),
    ("123 456", "123 456"),
    ("1 . 5", "1 . 5"),
    ("x / * y", "x/ *y"),
    ('"// private-looking text"', '"// private-looking text"'),
])
def test_operator_layout_preserves_strings_words_numeric_pieces_and_distinct_operators(before, after):
    assert compact_operator_layout(before) == after
    assert compact_operator_layout(after) == after


@pytest.mark.parametrize("raw", ["// comment", "/* comment */", 'f"interpolation"', "'a'", "café"])
def test_operator_layout_refuses_unsupported_syntax(raw):
    with pytest.raises(ValueError):
        compact_operator_layout(raw)
