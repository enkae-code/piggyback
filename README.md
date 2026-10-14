# Piggyback

**Change control for your own ideas.**

Scope guards stop the agent from drifting off a plan. Piggyback stops *you*.
Every idea you add mid-plan is checked against the decisions and non-goals you
already settled, shrunk to the need underneath it, then folded into an updated
plan — or parked with a condition for revisiting it.

Works on top of whatever spec you already have.

## The problem

Planning with an agent, you settle decisions and lock a plan. Then, still in
flow, new ideas arrive — a feature, a refactor, a "what if we also…". You tell
the agent to add each one.

Stacked on top of already-decided ideas, the end goal gets convoluted and loses
clarity. The plan becomes a chronological log of requests instead of one coherent
product. Nobody re-examined whether idea #7 still makes sense given ideas #1–6,
or whether the whole thing is still one product at all.

This is human-driven drift, not agent scope creep. It is a different problem
with a different shape, and the tools that exist solve the agent's version.

## How it works

One file, `PIGGYBACK.md`, holds a contract you confirm. Each new idea runs five
steps: **normalize** (split bundles, separate need from mechanism) →
**collision check** (against decisions and non-goals, every time) → **contract
diff** (what fields change, what obligations appear) → **shrink** (smallest
mechanism that meets the need) → **disposition** (FOLD / PARK / DROP).

Three rules carry most of the value:

- **Decisions record why.** `D-03 No database — because: deploys-anywhere —
  reopen-when: …`. A lock without a rationale is fossilized authority.
- **Non-goals are mandatory.** Without them, "fits the essence" means nothing.
- **Every fold leaves a receipt.** At plan lock, all folds since the last
  checkpoint are re-diffed against the appetite. If work went in, something
  comes out.

Routine folds print one line and move on. Only an identity change interrupts:

```
pb: PB-015 how   → FOLD   (+obligation: weekly content)
```

## Install

Pick a harness. Skills live in different places; all three accept the same
`SKILL.md`.

| Harness | Location |
|---|---|
| Claude Code / Codex / OpenCode (project) | `<project>/.claude/skills/piggyback/` or `.agents/skills/piggyback/` |
| OpenCode (global) | `~/.config/opencode/skills/piggyback/` |
| Hermes Agent | `~/.hermes/skills/piggyback/` |

Copy this repo's contents into that directory — `SKILL.md`'s frontmatter `name`
must match the directory name. Only immediate children are discovered, so no
extra nesting.

Then paste `snippets/AGENTS.md.txt` into the project's existing `AGENTS.md` or
`CLAUDE.md`. **Append it; never overwrite existing instructions.** Without a
standing trigger, ideas arrive while you are in flow and not thinking about
tools, and the gate never fires.

Finally, per project: copy `templates/PIGGYBACK.md` to the project root, fill
the contract, have the human confirm it, commit.

## What it is not

Not enforced. A root markdown file is not guaranteed to be discovered, loaded,
or followed by every harness. It is an invoked workflow: it works when loaded
and followed. Support is claimed only for harnesses actually exercised — see
Evidence.

Not a planner. It needs a settled baseline to reconcile against. For initial
planning, use a planning skill first.

Not a verdict engine. The agent recommends; the human disposes anything that
changes what the thing is.

## Evidence status

**Illustrated, not proved.** Honest state as of 2026-10-14:

- The design was reviewed by three independent models (Hermes, GPT Sol 6.1,
  Claude Opus 5.5) in a blind-critique-then-rebuttal loop. All three rejected the
  original mechanism — a one-question essence gate that tested necessity, not
  identity, and therefore rubber-stamped the drift it was meant to catch. What
  ships here is the replacement they converged on.
- Twelve regression scenarios exist in `examples/scenarios.md`. They have not
  been run against the shipped procedure.
- No comparative measurement exists: missed impacts, unnecessary interruptions,
  or whether plans actually get clearer, measured against ordinary planning.
- Earlier versions were piloted on one already-shipped product, retrospectively,
  by someone who knew the answers. That is a demonstration, not validation.

The drift claim is unproven until someone runs a real idea-stacking sequence —
sequential, human-injected, live — through this. `examples/scenarios.md` §
Comparison protocol is how to check.

## Layout

```
SKILL.md                  the gate: procedure, output formats, authority rules
templates/PIGGYBACK.md    the contract: copy to your project root
snippets/AGENTS.md.txt    the standing trigger, pasted into your harness config
examples/scenarios.md     12 regression cases + comparison protocol
```

## Prior art

The territory is not new. This is the cheap, mid-conversation version of
established practice:

- **Requirements change control** — proposed change, affected requirements,
  dependencies, disposition.
- **Architecture Decision Records** — rationale, consequences, supersession.
  The `because:` / `reopen-when:` fields borrow from this.
- **Truth maintenance / belief revision** — when an assumption changes,
  dependent conclusions get re-checked. This is the conceptual anchor for the
  re-audit step.
- **Shape Up** — fixed time, variable scope, appetite. The appetite field and
  the cumulative check come from here; identity alone does not control workload.
- **Jobs-to-be-Done** — separating the underlying need from a proposed solution.
  This is why the shrink step works.
- **Spec frameworks** (Spec Kit constitutions, Kiro steering, BMAD
  correct-course, Agent OS mission files) — they fix the plan up front. None
  run continuously against human-injected ideas mid-flow.

## License

MIT
