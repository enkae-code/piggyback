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

## Limits of the evidence

n=1 real session, hand-run by the same author (self-interest bias), one model,
compaction was the damaging variable. A session without mid-sequence compaction
might not need any of this. The negative result is the contribution: if you were
considering building this kind of continuous gate, don't — measure first.

## License

MIT
