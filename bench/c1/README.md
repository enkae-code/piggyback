# C1 pin-accuracy harness

Run from the repository root, in order. Evidence goes to a store outside the repo
(default `/home/brodey/projects/piggyback-evidence/c1/<run-id>/`).

1. **Commit, then register.** `manifest` refuses a dirty worktree. It locks the commit, the sha256
   of the harness files and `pairs.json`, the spot-check seed and sample, and the Chromium version of
   the bundled browser (read by launching it). Nothing is captured before it.
   `python -m bench.c1.manifest --store STORE --run RUN --seed N`
2. **Capture.** Only the IDs you name are captured; nothing is substituted. Refused without the
   manifest, when HEAD is not the manifest commit, or when the tree is dirty. Each try gets its own
   `attempt-N/` folder, never overwritten; a pair that already has attempts is never captured again.
   `python -m bench.c1.capture --run RUN C1-01 C1-02 ... C1-20`
   A side is `attempt-N/{fingerprint.json, screenshot.png, evidence.json}`; the receipt is in
   `fingerprint.json`. A failed try gets `failure.json` (class, status, final URL, screenshot when
   taken). A bot-wall page is kept and flagged `suspected_challenge` with the matched text. A crash
   still writes `pair.json` with status `failed`.
3. **Decide.** Write `eligibility.json` in the run folder: a list of `{pair_id, decision, reason,
   decided_by, decided_at_utc}`, `decision` is `keep` or `replace`. Required for every failed or
   `suspected_challenge` pair, primary or reserve, even when no replacement uses it. `keep` scores the
   pair as the site; `replace` needs an eligible failure (`http_status`, `cross_host_redirect`) for a
   failed pair; a `suspected_challenge` primary may be replaced. A timeout or `network` failure is
   never eligible. A replacement reserve that failed or is `suspected_challenge` needs its own
   decision. `replace` moves on to the next unused reserve in row order; `keep` leaves a failed
   reserve unresolved (a suspected one is scored as the site). An unused failed or
   `suspected_challenge` reserve takes `keep`; `replace` on an unused reserve is refused.
4. **Capture reserves.** Only needed for a `replace` decision. Reserves are used in row order R1, R2.
   `python -m bench.c1.capture --run RUN R1 R2`
5. **Lock targets.** Refuses until step 3 and 4 are complete. Locks only the retained set (kept
   primaries and the chosen reserves) and writes `targets.json` and `targets.lock.json`.
   `python -m bench.c1.targets --store STORE --run RUN`
6. **Score.** Refuses unless the manifest and targets lock match, each receipt's Chromium equals the
   manifest, and the decisions are complete. Writes `pins.csv`, `pin_properties.csv`, `anchors.csv`,
   `pair_summary.csv` and `verdict.json`. The first scoring is final: `verdict.json` is never
   replaced, so a later experiment needs a new run id.
   `python -m bench.c1.score --store STORE --run RUN`

Tests: `PYTHONPATH=. python -m unittest tests.test_bench_c1 tests.test_bench_c1_round4 tests.test_bench_c1_round6 tests.test_bench_c1_shadow` (no browser).
The browser test `tests.test_bench_c1_browser` needs Playwright and runs on the host.

Verdict is `pass`, `fail`, or `incomplete` (any unresolved pair).

Known limits (shadow DOM, capture side): the flat-tree order changes any DOM-order choice for slotted
nodes (first body, nav, card-group member, tie-breaks). Unslotted light nodes (text or child
elements) are missing, so a host's own text and card content can differ. Exact parity needs
`measure.py` to capture light-DOM order and the omitted unslotted nodes. Left as is: 2 of 72
captured pages had any `<slot>`.

Results of the published runs: [RESULTS.md](RESULTS.md).
