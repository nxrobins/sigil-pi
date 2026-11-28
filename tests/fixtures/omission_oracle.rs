//! Independent test-only function-span and token checks through the pinned compiler.
//! This never emits an admitted artifact or replaces verification/execution gates.
use sigil_compiler::{ast, lexer, parser, source::SourceFile, span::Span};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;

const OMITTED: [&str; 5] = [
    "parse_field",
    "key_matches",
    "bytes_eq",
    "store_quoted_size",
    "action_key",
];

fn source(path: &str) -> SourceFile {
    let mut raw = String::new();
    File::open(path)
        .unwrap()
        .take(1_048_577)
        .read_to_string(&mut raw)
        .unwrap();
    assert!(raw.len() <= 1_048_576, "test provenance input bound");
    SourceFile::new("omission-test", raw)
}

fn parsed(source: &SourceFile) -> (BTreeMap<String, Span>, Vec<lexer::Token>) {
    let (program, diagnostics) = parser::parse(source);
    assert!(diagnostics.is_empty(), "{diagnostics:?}");
    let mut functions = BTreeMap::new();
    let mut entries = 0;
    for module in program.modules {
        for item in module.items {
            match item {
                ast::Item::FnDef(function) => {
                    if function.name.contains("tool_main") {
                        assert_eq!(function.name, "tool_main");
                        assert_eq!(function.visibility, ast::Visibility::Public);
                        entries += 1;
                    }
                    assert!(functions.insert(function.name, function.span).is_none());
                }
                ast::Item::UseDecl(_) | ast::Item::EffectDecl(_) => {}
                _ => panic!("unsupported item in the reviewed fixed-entry recipe"),
            }
        }
    }
    assert_eq!(entries, 1);
    let (tokens, diagnostics) = lexer::lex(source);
    assert!(diagnostics.is_empty(), "{diagnostics:?}");
    (functions, tokens)
}

fn main() {
    let args: Vec<_> = std::env::args().collect();
    assert!(args.len() == 3 || (args.len() == 4 && args[3] == "--layout-only"));
    let (before_functions, mut retained) = parsed(&source(&args[1]));
    let (after_functions, after) = parsed(&source(&args[2]));
    let mut remaining: BTreeSet<_> = if args.len() == 4 {
        BTreeSet::new()
    } else {
        OMITTED.into_iter().collect()
    };
    let mut removed = BTreeMap::new();
    while !remaining.is_empty() {
        let eligible: Vec<_> = remaining
            .iter()
            .filter(|name| {
                retained
                    .iter()
                    .filter(|token| matches!(&token.kind, lexer::TokenKind::Ident(value) if value == **name))
                    .count()
                    == 1
            })
            .copied()
            .collect();
        assert!(
            !eligible.is_empty(),
            "an omitted function still has a reference"
        );
        for name in eligible {
            let span = *before_functions.get(name).expect("missing whole function");
            retained.retain(|token| {
                let inside = token.span.start >= span.start && token.span.start < span.end;
                assert!(
                    !inside || token.span.end <= span.end,
                    "partial token removal"
                );
                !inside
            });
            removed.insert(name, span);
            remaining.remove(name);
            assert!(!after_functions.contains_key(name));
        }
    }
    assert_eq!(
        before_functions.len() - removed.len(),
        after_functions.len()
    );
    let retained: Vec<_> = retained.into_iter().map(|token| token.kind).collect();
    let after: Vec<_> = after.into_iter().map(|token| token.kind).collect();
    assert_eq!(
        retained, after,
        "a retained compiler token or literal changed"
    );
    for (name, span) in removed {
        println!("removed {name} {} {}", span.start, span.end);
    }
    println!(
        "{} retained tokens; zero parser/lexer diagnostics",
        after.len()
    );
}
