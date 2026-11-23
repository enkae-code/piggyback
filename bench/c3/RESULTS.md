# C3 results

Registered run `c3-gate-20261122`, store `/home/brodey/projects/piggyback-evidence/c3/c3-gate-20261122/`.
Locked 2026-11-22 (manifest `locked_at` 2026-11-22T21:21:50+00:00, Piggyback commit `7c52234`).

- Model: claude-opus-5-5, via Claude Code 2.1.325, context window 1000000, max output 128000.
- Design skill: Impeccable 4.8.2, commit `035c8f3`.
- Rater seed: 20261122.

## Generation

- 40 pages (10 tasks x 4 arms). All 40 `run.json` have status `pass`, `model_conforms` true and
  `gate_eligible` true. All 40 transcripts are kept.
- Permission denials: 5, all for the `Glob` tool (none for `Read`, `Write`, `Edit` or `Grep`).
  Three targeted `/usr/share/fonts`; two had no path recorded. Denied as designed; no page was
  affected by the denial itself.
  - T03 skill-plus-piggyback, T05 skill-plus-piggyback, T09 skill-plus-piggyback: `/usr/share/fonts`
  - T09 piggyback-brief: no path recorded
  - T10 skill-plus-piggyback: no path recorded

## Correction turns

Counted from each `pages/TASK/ARM/run.json` `turns`, not from the summary in the request.
Every page passed the checklist after at most one correction turn.

| Task | piggyback-brief | screenshot | skill-alone | skill-plus-piggyback |
|---|---|---|---|---|
| T01 | 1 | 0 | 0 | 0 |
| T02 | 0 | 0 | 1 | 0 |
| T03 | 0 | 1 | 1 | 0 |
| T04 | 1 | 1 | 1 | 1 |
| T05 | 0 | 1 | 0 | 0 |
| T06 | 1 | 0 | 0 | 0 |
| T07 | 1 | 1 | 0 | 1 |
| T08 | 0 | 0 | 0 | 0 |
| T09 | 0 | 0 | 1 | 0 |
| T10 | 1 | 1 | 1 | 1 |
| **Total** | **5** | **5** | **5** | **3** |

## Gate verdict: FAILED

The gate needs two conditions. The turn condition fails: the Piggyback brief total (5) is not
strictly lower than the screenshot total (5). The gate fails on that alone, whatever the ratings
show.

Observation: every page passed the fixed checklist within 0 to 1 correction turns, so the turn
count did not separate the arms. The first-attempt rule is unchanged: this result stands and is
not re-run.

## Works-with pair

Reported beside the gate. It decides nothing. Turn totals: skill-alone 5, skill-plus-piggyback 3.

## Ratings

Not run. Human rating was cancelled on 2026-11-22 before any submission: the gate had already
failed on turns, so ratings could not change the verdict, and the project does not depend on
recruiting people. The rating site and form were removed.

## Unofficial AI-judge check (decides nothing)

Run after the gate failed, under rules fixed before any judging: [`ai-judge/PROTOCOL.md`](ai-judge/PROTOCOL.md).
The judge was OpenAI gpt-6.2-sol through Codex CLI (images attached directly, no tools), a
different vendor from the builder. It saw the starter screenshot and the four above-the-fold
renders under neutral names, twice per task with each pair's order swapped. A pair counts only
when both judgements agree.

| Pair | Piggyback side wins | Ties | Other side wins |
|---|---|---|---|
| Piggyback brief vs screenshot | 0 | 0 | 10 |
| Skill + brief vs skill alone | 7 | 1 | 2 |

The judge picked the first-shown page 10 of 20 times in pair 1 and 9 of 20 in pair 2, so position
did not drive the picks. One caveat favours the screenshot arm: the judge's reference image is the
same screenshot that arm was given.

Read the reasons in [`ai-judge/results.json`](ai-judge/results.json): the brief carried the
original's colours and type, while the screenshot builds also copied its layout (for example
ink-and-rule's reversed headline word, ruled spec sidebar and black callout).

Machine measure: Piggyback's own gap check between each starter and page, share of comparable
concepts that match ([`ai-judge/measure.json`](ai-judge/measure.json)). This metric is the one
the brief is written from, so it favours the brief arm.

| Arm | Mean match |
|---|---|
| Piggyback brief | 0.589 |
| Screenshot | 0.592 |
| Skill alone | 0.565 |
| Skill + brief | 0.689 |

Brief vs screenshot: 3 wins, 4 ties, 3 losses. Skill + brief vs skill alone: 8 wins, 2 losses.

Summary: on its own, the brief did not beat a screenshot, even on Piggyback's own metric. Added to
a design skill, it helped on both measures.

## Kill rule

The gate failed, so the kill rule applies: stop feature work. Keep only the build check (defaults
detected plus re-run) if testers actually used it; otherwise stop. Taste sign-off is pending until
Brodey signs off.
