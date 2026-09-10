"""Layout transformation boundaries; compilation is checked separately."""

import pytest

from request_layout import compact_layout


@pytest.mark.parametrize("source,expected", [
    ("  fn x ( a: i64 , b: i64 ) -> i64 { return a + b ; } \n", "fn x(a: i64,b: i64)-> i64{return a + b;}"),
    ("f   \"not interpolated\" ;", 'f "not interpolated";'),
    ('namef "string" ;', 'namef "string";'),
    ('namef"string" ;', 'namef"string";'),
    ("a +  + b / / c < < d - > e : : name", "a + + b / / c < < d - > e : : name"),
    ("first\r\n\tsecond \n third", "first second third"),
    (" ", ""),
    ('{ "a ; ( ) \\" // f\\" \\n 😀" ; }', '{"a ; ( ) \\" // f\\" \\n 😀";}'),
    ('"literal\n\n  spacing" ;', '"literal\n\n  spacing";'),
])
def test_only_ascii_layout_changes_and_literals_remain_exact(source, expected):
    assert compact_layout(source) == expected
    assert compact_layout(expected) == expected


@pytest.mark.parametrize("source", ['f"hello {x}"', 'return f"{call(\"x\")}";',
    "// line", "/* block */", "'x'", '"unterminated', '"trailing\\'])
def test_unsupported_or_incomplete_lexical_forms_are_not_rewritten(source):
    with pytest.raises(ValueError):
        compact_layout(source)


def test_non_ascii_whitespace_is_not_silently_normalized_into_valid_source():
    assert compact_layout("{\u00a0name\u00a0}") == "{\u00a0name\u00a0}"
