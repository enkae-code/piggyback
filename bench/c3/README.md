# C3 acceptance checklist and prompt assembly

Frozen by `phase-c-sets.md`, section C3. The task rows, the shared instruction and the four arm
blocks are copied verbatim; `tests/fixtures/c3/spec_text.json` holds the frozen text and
`tests/test_bench_c3.py` asserts equality with it.

## Run order

1. Expand the prompts (no model runs here):
   `PYTHONPATH=. python -m bench.c3.prompts OUT/prompts`
   Each file ends with `prompt_text.json` `runtime_default`: the Hermes default runtime line. The
   manifest may replace that value only before the first generation, then the prompts are
   expanded again and frozen.
2. After each build and each correction turn, run the checklist on the page:
   `PYTHONPATH=. python -m bench.c3.checklist PAGE.html --task T01 > OUT/T01-arm-turn0.json`
   `PAGE` is a file path or a loopback `http(s)` URL (`127.0.0.1` or `localhost`). Any other host
   is refused before a browser starts. The JSON report is printed for every outcome.
3. Read `outcome`:
   - `pass` (exit 0): every item passed.
   - `fail` (exit 1): `correction_message` holds the single follow-up turn. Send it, then stop
     once the checklist passes, or after five turns.
   - `no_runnable_output` (exit 1): the page never loaded to a checkable state (navigation
     timeout, load failure, context destroyed). Recorded as a loss, with no correction message.
   - `checker_fault` (exit 2): bad target or task, no bundled Chromium, or an error in the
     checker. Fix the checker. Do not count it as a page result.

Playwright is needed for step 2 (`pip install playwright` and `playwright install chromium`, or
the repo's measure extra). The checker uses only the bundled Chromium (no channel fallback), and
the report records `browser_version` and `browser_executable`. The browser tests,
`tests/test_bench_c3_browser.py`, run on `tests/fixtures/c3/` pages and skip when Playwright is
missing. Hermes runs them on the host.

## Egress

The browser goes through `piggyback.proxy.EgressProxy`, subclassed as `TargetProxy`. Only the
target's own `(host, port)` is connected to. A file target connects nowhere. `route()` checks the
page's own requests as a second layer. `blocked_requests` in the report lists what was refused.
`PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK` is removed while the browser starts, as
`piggyback/measure.py` does, so loopback traffic also goes through the proxy.

## Checklist order and rules

1. **render**: passes when the body has non-empty rendered text (`innerText`, trimmed). Uncaught
   page errors and console errors are recorded in the JSON and never fail on their own. A failed
   render adds the first line of the first page error to its item.
2. **content**: one predicate per content item of the task row (see `tasks.json`, evaluated by
   `predicates.py` on the rendered DOM). Only visible elements count. Kinds:
   - `text`: regex on the body text, case-insensitive.
   - `count`: visible elements matching a class or tag, `>=` min. Class matching is by whole
     word (`signal-strip` has `signal`, `showcase` is not `case`). `exclude_within` skips
     nav, header and footer where the item means content.
   - `follows_headline`: standfirst, a paragraph after the h1 in its header, article or main,
     before any heading.
   - `heading_statement`: a short paragraph (60 words or fewer) under a mission, purpose, about
     or why heading.
   - `control_name`: an accessible name of a button, link, textbox or similar control.
   - `any` and `all` combine kinds. T09 author/date is an author (no `<time>` in the author list)
     and a date.
   Known limit: a paragraph straight after the h1 counts as a standfirst even when it is the body
   opening. The DOM alone cannot tell them apart.
3. **overflow**: at 1440 px, then 390 px, after the page settles (animations finished or 1000 ms,
   plus two frames). Fails if `scrollWidth > clientWidth`, or any rendered element, fixed
   positioned ones included, ends more than 1 px past the viewport edge, unless an ancestor with
   `overflow` hidden or clip clips it.
4. **axe**: axe-core 4.14.1, tags `wcag2a` and `wcag2aa`, at 1440 px. Violations fail. Incomplete
   results are recorded and never fail. An axe error fails closed.

The failure message is fixed text: `The page failed these checklist items:` followed by one
`- ` line per failed item, then `Fix each listed item and return the complete page.` A content
item line carries its rule in parentheses.

## Vendored axe-core

- Package: `axe-core` 4.14.1, from the official npm tarball
  (`npm pack axe-core@4.14.1`, npm integrity `sha512-FHfa+lmNjQZfJz1q8wKqIDwC6DExcHt/pkjNQb09a3NPjCKg2rhu5qb2S4r76ASyFNNtuFooJpZe37OgudVGtQ==`).
- `vendor/axe-core/axe.min.js` sha256: `29348999450cdf2849a8e70b7912fc2da85a849da39c0d0d5394f51d4a9ed95c`
- `vendor/axe-core/LICENSE` (MPL-2.0) sha256: `af175b9d96ee93c21a036152e1b905b0b95304d4ae8c2c921c7609100ba8df7e`
- `vendor/axe-core/LICENSE-3RD-PARTY.txt` sha256: `4f8563870d0fca38bbc3e00b6f670cb7fa9f380ba9f26a7f7d1184a6b18b1653`

## Generation: manifest, then one page at a time

Run from the repository root, in order. Evidence goes to a store outside the repo (default
`/home/brodey/projects/piggyback-evidence/c3/<run-id>/`).

1. **Commit, then register.** `manifest` refuses a dirty worktree, an existing or non-empty run
   folder. It measures
   each starter's `index.html` with the bundled Chromium (1440x900 screenshot and `BRIEF.md`), copies
   the Impeccable `SKILL.md`, and writes the 40 prompts. It locks the commit,
   harness, starter and frozen-input hashes, Chromium and axe-core versions, the Impeccable commit and
   package version, the model, the pinned claude executable (realpath and `--version`), the context
   window (1000000) and max output tokens (128000) as Claude Code 2.1.325 reports them for
   `claude-opus-5-5`, the agent env allowlist (names only), the 40 prompt bodies and their hashes, the
   checker and capture Chromium, a clean Impeccable checkout, and the rater seed. It is written once.
   A store path with a symbolic link on it or above it is refused.
   `python -m bench.c3.manifest --store STORE --run RUN --seed N`
2. **Generate.** One task and one arm per call. Refused without the manifest, when HEAD, the harness,
   the starters, a frozen input or the claude CLI version changed since the lock, when the task or
   arm is unknown, when a folder on the way to `pages/TASK/ARM` is a symlink, or when that page
   folder already exists (the first attempt stands). Each turn runs the pinned claude executable
   with `DISABLE_AUTOUPDATER=1`. The runner also refuses when the pinned CLI is missing or reports
   another version, when the Chromium that runs the checklist differs from the manifest, or when an
   earlier page's `run.json` is not model-conforming. The project folder holds only that arm's
   allowed inputs. The checklist runs after every turn, errors included; up to five correction turns
   follow a runnable failing page. The record is `pages/TASK/ARM/run.json`. `rounds/` holds each
   sent message (`sent-N.txt`), each raw agent output (`agent-N.json`) and `transcript.jsonl` when
   the session file exists (`transcript` is `kept`, `missing`, or `copy_failed: <Error>`).
   A run that used any model other than `claude-opus-5-5` lists it in `model_flag`; `model_evidence`
   is `missing` when a round reported no `modelUsage`. `model_conforms` is false only when a round
   reported a model other than that one, another context window, or another `maxOutputTokens` than
   the manifest's `max_output_tokens`. Such a turn ends the page at once with status
   `model_mismatch`, with no further agent call. The checklist still runs on that turn's page, for
   the record. The page prints `STOP` to stderr, the CLI exits 3, and the next page of the run is
   refused. A round with no usage report is missing evidence, not a model change, and does not halt. `gate_eligible` equals `model_conforms`; a
   `no_output` page still counts for the gate, as the preregistered loss. Each round keeps its `permission_denials` (tool name and target
   path); a run with any prints `FLAG` and sets `audit_flag` to `permission_denials`.
   `python -m bench.c3.run --store STORE --run RUN TASK ARM`
   Run it for each of the 10 tasks and 4 arms (40 pages). Rating comes in a later step.

**Gate** (preregistered, amendment of 2026-11-22). Two-way, on the gate arms only: the Piggyback brief
wins at least 8 of 10 tasks, and its turn total is strictly lower than the screenshot arm's. The works-with
pair (skill alone against skill plus brief) is reported beside it and decides nothing.

**Status scoring** (preregistered in the manifest as `status_scoring`, before any generation).
`pass` scores its actual correction count and is rated. `failed_after_5` and `agent_error` score 6
and are rated as they stand. `checker_fault` scores 6, is rated and flagged. `no_output` and
`runner_crash` score 6 and are a loss, not rated. `model_mismatch` scores 6, is flagged, is not
rated, and halts the gate. A turn that errors but leaves a passing page is
`pass`. `runner_crash` writes `run.json` with the rounds made so far, then re-raises; a crash after
a round on another model is `model_mismatch`.

Interrupted pages: a runner killed before publish leaves `pages/TASK/ARM/` with no `run.json`, and
the next page of the run is refused until it is recorded. Run
`python -m bench.c3.run --run RUN --reconcile TASK ARM`: it rebuilds the rounds from
`rounds/agent-N.json` (no turn runs, `project/` is untouched) and publishes `run.json` with
`reconciled: true`, status `model_mismatch` or `runner_crash`. Reconcile refuses an arm that already
has `run.json` or does not exist. It needs no pinned CLI or checker Chromium. The session id is in
`rounds/session.txt`, written before the first turn. A run holds `<run>/.runner.lock` from admission
to publication; a page or reconcile against a run that another process holds is refused.

Tests: `PYTHONPATH=. python -m unittest tests.test_bench_c3_manifest tests.test_bench_c3_run` (no
browser, no model; the agent call and the checklist are stubbed).

The checklist loads each page from a loopback server (`127.0.0.1`, a free port) that serves only
that arm's project folder, and only for the length of one check. The evidence store beside the
project is not reachable from the page, so `../` and `%2e%2e` requests do not return outside files.
The server is shut down after every check.

## Limits

Axe findings are automated checks. They do not certify accessibility. The content predicates
check that a required item exists, not that it is good.
