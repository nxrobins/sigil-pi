//! Test-only comparison through the exact pinned compiler's real lexer.
//! Layout provenance is data, never a bypass of executable source admission.
use sigil_compiler::{lexer, source::SourceFile};
use std::fs::File;
use std::io::Read;

fn tokens(path: &str) -> Vec<lexer::TokenKind> {
    let mut raw = String::new();
    File::open(path)
        .unwrap()
        .take(1_048_577)
        .read_to_string(&mut raw)
        .unwrap();
    assert!(raw.len() <= 1_048_576, "test input bound");
    let source = SourceFile::new("layout-test", raw);
    let (tokens, diagnostics) = lexer::lex(&source);
    assert!(diagnostics.is_empty(), "{diagnostics:?}");
    tokens.into_iter().map(|token| token.kind).collect()
}

fn main() {
    let args: Vec<_> = std::env::args().collect();
    assert_eq!(args.len(), 3);
    let before = tokens(&args[1]);
    let after = tokens(&args[2]);
    assert_eq!(before, after, "layout changed a compiler token or literal");
    println!("{} identical tokens; zero lexer diagnostics", before.len());
}
