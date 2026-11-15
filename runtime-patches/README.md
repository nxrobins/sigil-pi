# Local SIGIL dependency

`SIGIL_REV` names local runtime commit `09939979163f7bcca5443b1060c93b0eed4ddb28`, based on
published commit `8277a1d92d599df89e6b4391fc70fd0fa534d696`. Its crates tree is
`1e87daf4a0ae98ceddd13bd26c41143a9b8d87cf`; the stdlib and other build inputs are unchanged.
This runtime commit must be published in the configured SIGIL repository before hosted CI
can fetch the app's new pin. No release or remote CI result is claimed by local testing.

`http-budget.bundle` preserves the exact candidate commit (requires the published base).
In a SIGIL clone containing that base, run:

```sh
git fetch /absolute/path/to/runtime-patches/http-budget.bundle refs/heads/codex/pi-http-budget
git checkout 09939979163f7bcca5443b1060c93b0eed4ddb28
```

`0001-bounded-provider-http.patch` preserves the complete dependency change for review.
It grants an explicit `http_timeout_ms` in 1..60000, advertises
`x-sigil-http-budget: single-request-v1`, applies one total deadline across headers and body,
and refuses redirects for explicitly budgeted requests. Requests without the grant keep
the existing defaults. Runtime tests cover slow headers, body dribbling, redirects, expired
budgets, invalid grants, interruptions and the MCP capability schema.

To reproduce from the published base, apply the patch in a clean SIGIL checkout with
`git am /absolute/path/to/0001-bounded-provider-http.patch`. This reproduces the source tree;
a new committer timestamp changes the commit id. Either fetch the exact candidate commit
from its local clone/bundle, or deliberately update `SIGIL_REV` to the newly created commit
before running `SIGIL_ROOT=/absolute/path/to/SIGIL ./ci.sh`. The tree and all build-input
checks remain mandatory.
