---
name: piggyback
description: "Reconcile mid-plan ideas against the settled product plan."
version: 0.1.1
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [planning, change-control, scope-control]
---

# Piggyback

Reconciles every idea added to a settled plan while planning is still in
progress. The plan of record is a contract the human confirms; each new idea is
checked against it, shrunk to the need underneath it, then folded into an
updated plan or parked with a revisit condition.

This is not a scope guard. Scope guards stop an *agent* drifting off a plan.
Piggyback runs when the **human** adds ideas on purpose, and its output is a
reconciled plan — not a verdict.

Pure instructions. No runtime, no hooks, no dependencies.

## When to Use

- The human adds a feature, refactor, or idea to a plan whose decisions are
  already settled ("also add…", "what if we…", "while we're in here…").
- Planning has been running long enough that ideas now arrive on top of earlier
  ideas, and the end goal is getting hard to state in one sentence.
- Several individually reasonable additions may together exceed the time or
  effort the plan was allowed.
- An existing spec or plan document exists and the human keeps changing it.

Don't use for:

- Initial planning, before any decisions are settled — that is what a planning
  skill is for. Piggyback needs a baseline to reconcile against.
- A plan the agent is drafting on its own with no human injecting ideas.
- Enforcement. This is an invoked workflow: it fires when loaded and followed.
  See Pitfalls.

## Prerequisites

`PIGGYBACK.md` at the project root, created from `templates/PIGGYBACK.md`, with
the CONTRACT block filled and the file committed. The human confirms the
contract; an agent may draft it.

If the project already has a plan of record — a constitution, mission, or
steering document — do not duplicate it. Fill `source:` in the contract with
that file's path and keep one source of truth.

## Setup (once per project)

1. Copy `templates/PIGGYBACK.md` to the project root as `PIGGYBACK.md`.
2. Draft the CONTRACT from the existing plan. At least three non-goals, each
   with a reason; an appetite. Non-goals are what make drift detectable.
3. Show it to the human for confirmation. Record `rev 1` and the date.
4. Record each already-settled decision in DECISIONS with a `because:` naming
   the contract field it serves and a `reopen-when:` condition.
5. Commit.

Done when the human has confirmed the contract and `git status` is clean.

## Procedure

Run on every idea, before it enters the plan.

### 1. Normalize

- Split bundled ideas. "Add a blog and also comments" is two ideas with an
  interaction; assess them separately, then together.
- Record the underlying **need** separately from the proposed **mechanism**.
  Accounts are a mechanism; recovering a lost draft is the need.
- Distinguish ideation from instruction. "What if we added accounts?" is not
  authorization to change the plan. Capture it; do not apply it.

### 2. Collision check

Compare against DECISIONS and non-goals. Run this every time, regardless of
what step 3 concludes.

- Collides with a decision whose `because:` cites a contract field → the idea
  changes what the thing is.
- Collides with a decision that cites no contract field → **decision reopen**.
  Compare the idea against that decision's `because:` and `reopen-when:` before
  treating the collision as a problem.
- Collides with a non-goal → say which non-goal and what breaks.

A collision triggers reconciliation, not automatic rejection.

### 3. Contract diff

Draft the contract as it would read if the idea shipped. Then list the
obligations the idea adds: data, security, content, support, maintenance.

| Result | Classification |
|---|---|
| Contract field changed | changes what the thing is |
| No field changed, no new obligation | implementation detail |
| No field changed, new obligation | implementation detail — and the receipt names the obligation |

The third row is the one most often missed. A blog leaves every contract field
untouched and still adds a weekly content obligation.

### 4. Shrink

Before any disposition, ask: what is the smallest mechanism that meets the need
*inside the current contract*, and what obligations does that mechanism add?

Then pre-mortem: *it is three months later and this was a mistake. Why?* Record
the answer if it is not "no reason".

### 5. Disposition

| Disposition | Meaning | Required |
|---|---|---|
| FOLD | Enters the plan | exact accepted variant |
| PARK | Good, not now | `revisit-when:` — a falsifiable condition, or it is a graveyard entry |
| DROP | Rejected | **Human-only.** The agent recommends; it never applies DROP itself |
| NEEDS-INPUT | Not yet understood | temporary only; resolve before any terminal disposition |

The agent decides how. For anything that changes what the thing is, the agent
recommends and the **human disposes**.

Rejecting a mechanism while accepting its need is two dispositions, not a
hedge. Give them linked IDs.

## Output

Two layers. The receipt is for the record; the note is what you actually say to
the human. **Never collapse them into one line** — measured 2026-10-14: one-line
output discarded the need, the smallest mechanism and the obligations, which is
the entire value, and scored worse than plain planning on every axis.

**Always write the receipt** (one line, to PIGGYBACK.md):

```
pb: PB-015 how → FOLD (+obligation: weekly content)
```

**Then say the thing that matters.** Give the need, the smallest mechanism that
meets it, and what the idea costs. If the cheapest version already covers the
need, say so — that is usually the most useful sentence in the reply.

Routine fold, say it short:

> PB-015 folded. It adds a weekly content obligation — you now owe a send every
> release, plus unsubscribe handling. A `/changelog` page fed from your release
> notes covers the need with no standing duty. Taking the newsletter?

Identity change — say it fully, then wait:

> ⚑ PB-014 adds "registered users" to WHO — that is non-goal 3, and it reverses
> D-03 (localStorage persistence, because: non-goal 3).
>
> The need is "don't lose work". Export/import a project file covers that with
> no server, no auth, no password resets, no deletion requests. Accounts would
> add retained user data, backups, and a privacy policy — on a plan with an
> eight-week appetite and no email infrastructure today.
>
> Recommend SHRINK → export/import. Want accounts anyway? That is a contract
> change, rev bump, and most of these decisions get re-audited.

Prose is not decoration here. A refusal the human cannot evaluate is a refusal
they will override next turn.

## Maintaining the plan

**Cumulative check.** At plan lock, at each phase boundary, and after a material
batch of additions: re-diff the contract against *all* folds since the last
checkpoint and weigh them against the appetite. If work went in, something comes
out — name what moved later or got cut, or name the evidence that it all fits.
Ten individually fine folds can double the launch without changing the stated
purpose; a per-idea gate cannot see that.

**Essence edits.** Every contract change is shown as a before/after diff naming
what got broader, narrower, removed, invalidated. Wording that only broadens is
drift, not a refinement — flag it. The human confirms; `rev` increments.

**Re-audit.** On a confirmed FOLD, scan every decision briefly for relevance,
then deeply review the ones whose `because:` cites a changed field, marking each
KEEP or REVISIT. Do this before the new plan is accepted, not after. Three or
more contract rewrites in one session means stop gating and run a short session
to re-settle the contract instead.

## Independent review

Optional, and scaled to consequence. Spawn a second pass only for one-way doors:
a contract field changes, persistent user data or auth is introduced, a stack
decision is reversed, or the build has already started. Keep that pass blind to
the first recommendation.

Otherwise self-review, labelled as such. Findings are objections or hypotheses
unless backed by identifiable evidence — a different provider reading the same
framing can reproduce the same assumptions, and disagreement is not validation.

If the harness cannot spawn a second pass, say so. Do not simulate one.

## Pitfalls

- **Silent folds.** A fold with no visible receipt is how cumulative drift wins.
- **Sycophancy.** The likelier failure is waving an identity change through as an
  implementation detail to avoid interrupting the human. If the receipt would be
  embarrassing to show, it is not a routine fold.
- **Essence laundering.** "A focused drawing tool" → "a creative platform" makes
  everything fit. A broadening-only rewrite is the symptom.
- **Thresholds presented as rules.** "Three rewrites" and "five folds" are
  prompts to look, not boundaries. Material impact and plan boundaries decide.
- **Claiming enforcement.** A root markdown file is not guaranteed to be
  discovered, loaded, or followed by every harness. Claim support only for
  harnesses actually exercised.
- **Duplicating the plan of record.** If a constitution or mission file exists,
  point at it; do not fork it.
- **Missing `because:` links.** A decision with no link cannot be re-audited
  automatically, so scan all decisions briefly rather than trusting the links.
- **Concurrent writers.** One state writer at a time. Check `rev` before
  applying a disposition; two agents can otherwise reconcile against different
  baselines.

## Verification

The gate worked if, at plan lock, you can answer all four:

1. The contract reads as one product, in one sentence, without "and also".
2. Every fold since the last checkpoint is accounted for against the appetite,
   with anything displaced named.
3. Every decision touching a changed field is marked KEEP or REVISIT.
4. At least one idea was shrunk, parked, or dropped — and that call is recorded
   with its reason.

A gate that disposes of nothing is not working. `examples/scenarios.md` holds
the regression cases; run them when the procedure changes.
