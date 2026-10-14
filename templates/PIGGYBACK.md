# PIGGYBACK

One product. One plan. Copy this file to your project root, fill the CONTRACT,
commit it. Everything below the contract is maintained *in place* — never
append a session diary, replace stale rows.

Run the gate on every idea before it enters the plan:
**1. Normalize** — split bundled ideas; record the need separately from the
proposed mechanism. Brainstorming is not instruction.
**2. Collision check** — against decisions and non-goals. Every time, always.
**3. Contract diff** — draft the contract as it would read if the idea shipped,
plus the obligations it adds (data, security, content, support).
**4. Shrink** — smallest mechanism that meets the need, inside the contract.
**5. Disposition** — FOLD / PARK / DROP. NEEDS-INPUT until the idea is understood.

---

## CONTRACT

```
rev 4 | confirmed 2026-10-14 | source: PIGGYBACK.md | constitution.md
```
<!-- source: name the file this was confirmed in, or where the plan of record lives.
     rev increments on every human-confirmed change; "confirmed" is a date, not a hope. -->

**one-liner**
<!-- what it is, for whom, in one sentence -->

**core-loop**
<!-- the thing a user does that makes this worth building -->

**signature**
<!-- the detail that makes it recognisably this thing and not a generic one -->

**non-goals** — at least three, each with a reason. This is what makes drift
detectable; without it, "fits the essence" means nothing.
1. <!-- not X, because Y -->
2.
3.

**appetite**
<!-- time or effort allowance; what gets displaced if this grows -->

---

## DECISIONS

```
D-01 <rule> — because: <contract field it serves> — reopen-when: <falsifiable condition> — locked|provisional
```

- **D-01** _example_ — No database; drafts are JSON files under `data/` —
  because: deploys-anywhere — reopen-when: multi-instance writes or concurrent
  access is required — provisional

A lock without a rationale is fossilized authority. A collision with a locked
decision triggers reconciliation, not automatic suspicion.

---

## PARKED

```
PB-nn <idea> — need: <the underlying problem> — revisit-when: <falsifiable condition>
```

<!-- A parked item with no revisit-when is a graveyard entry. -->

---

## LOG

Last ~20 dispositions. IDs only; older history lives in git.

```
pb: PB-014 what  → SHRINK → recovery link        (adds "registered users" to WHO)
pb: PB-015 how   → FOLD   (+obligation: weekly content)
pb: PB-016 parked → revisit-when: real users hit the 5-field limit
```

**Cumulative check** — at plan lock, at each phase boundary, and after a
material batch: re-diff the contract against *all* folds since the last
checkpoint and weigh them against the appetite. If work went in, something
comes out — name what moved later or got cut, or name the evidence it all fits.

**Essence edits** — always shown as a before/after diff naming what got
broader / narrower / removed / invalidated. Wording that only broadens is
drift, not a refinement. On a confirmed FOLD, re-audit every decision briefly,
then deeply the ones whose `because:` cites a changed field: KEEP or REVISIT.
Three or more contract rewrites in one session → stop gating, run a short
essence session.
