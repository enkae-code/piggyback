# Piggyback

**A contract file for mid-plan change control. Retired as a skill.**

This repo started as an agent skill: a five-step gate that reconciled every new
idea against a settled plan. Two measured experiments killed it. What survives
is the one artifact that earned its keep — `templates/PIGGYBACK.md`.

## Why the skill was retired

**Experiment 1 (2026-10-14, synthetic).** Seven human-injected ideas against a
settled drawing-tool plan, three isolated conditions, same model:

| Measure | No contract | Contract only | Full gate |
|---|---|---|---|
| Smaller/cheaper alternatives proposed | **7** | 6 | 2 |
| Ongoing obligations named | **10** | 6 | 3 |
| Collective drift flagged | idea 7 | **idea 3** | only in cumulative check |

The gated run scored worst on every axis. The gate suppressed exactly the
behaviour a plain competent agent does well.

**Experiment 2 (2026-10-14, real session).** A 17-hour build session with 15
idea-bearing turns onto a locked plan, hand-gated against ground truth from
commit history. Findings:

- **One real catch.** The agent had held the locked contract informally in the
  morning; context compaction erased it at midday and it violated the same rule
  at full speed 35 minutes later, silently. The informal check was as good as
  the gate while it lived in context — and context is where it dies.
- **One mechanism earned the artifact.** A contract *file* survives compaction.
  An agent remembering the plan does not. That is durability, not procedure.
- **10 of 15 turns needed nothing.** Bug reports, polish, ops asks — the gate
  emits receipts nobody reads.
- The 0.1.1 fixes held (no engagement suppression; DROP never applied), so
  there was no defect worth another iteration either.

**Conclusion:** the template earns its keep; the procedure subtracts value.
Keep the file, drop the gate.

## What to actually use

Copy `templates/PIGGYBACK.md` to your project root, fill the CONTRACT (with at
least three non-goals, each with a reason), record decisions with a `because:`
and `reopen-when:`, commit it — and **re-read it whenever an idea changes the
mechanism or the surface of the thing**. That last sentence is the whole skill.

Do not run it as a per-idea ceremony. Do not paste a standing trigger into your
harness config. The one hard failure mode it protects against — the plan
vanishing from the agent's head mid-session — is fixed by the file existing and
being re-read, not by a workflow.

## Drift alarm (spike — unvalidated)

`piggyback.py` tests whether the one real catch from the validation was
machine-detectable: it checks added lines against the prohibition-flavored
rules in a contract file and reports lines that reuse a rule's vocabulary.

    python3 piggyback.py CONTRACT [--since A..B] [--min-terms N]
    python3 piggyback.py --self-test

**Measured on the HollowPrismOS contract and four real commits:**

| Commit | Real drift? | Findings | Caught? |
|---|---|---|---|
| e976593 v0.4.0 — embedded webviews | yes | 2 | **yes** (`deck.ts:103`, terms zoho+mail) |
| 12ec041 v0.3.0 — compliant `embed: false` link | no | 4 | no (near-miss: same words as the violation) |
| c7b5954 — the isolation fix | no | 3 | no (touches the forbidden surface to fix it) |
| 7660178 — unrelated token split | no | 2 | no (architecture-vocabulary noise) |

Recall is 1/1 on the one known violation; precision on ordinary commits is
poor. The noise comes from rules whose vocabulary *is* the project's
vocabulary — "live data from a VPS dashboard feed" matches every commit that
touches the feed. There is no semantic layer: "webview" does not match
"embedded", `embed: false` does not differ from `embed: true`, and fixing a
violation still fires on the surface being fixed.

**Verdict: a human-reviewed diagnostic, not a gate.** n=1, one contract, one
author — the same limits as the validation itself. Use it to surface
candidate drift for a human to read; do not wire it into CI on this
evidence. Hunk-level matching was tried first and measured worse (4–7
findings per ordinary commit); line-level is what shipped.

## Limits of the evidence

n=1 real session, hand-run by the same author (self-interest bias), one model,
compaction was the damaging variable. A session without mid-sequence compaction
might not need any of this. The negative result is the contribution: if you were
considering building this kind of continuous gate, don't — measure first.

## License

MIT
