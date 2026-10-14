# Piggyback — regression scenarios

Each case: setup, the trap, expected impact, acceptable outcomes. Run these when
the procedure changes. Agreement with a scenario's stated expectation is not
validation — the point is to check that the *impact* is surfaced before the
disposition is chosen.

Baseline contract used by all cases:

```
one-liner:  A focused drawing tool for hobby illustrators.
core-loop:  Sketch → refine → export a finished panel.
signature:  The pressure curve that makes line weight feel like a real nib.
non-goals:  1. Not a social network. 2. Not a general photo editor.
            3. Not for teams — single artist, single machine.
appetite:   One person, eight weeks to first exportable panel.
```

## 1. Optional addition that is still an identity change

**Idea:** "Let's add a marketplace so artists can sell brushes."

**Trap:** it is optional — the tool ships fine without it. Every
"would we still ship this?" test routes it to routine fold.

**Expected:** contract diff changes `core-loop` (buy/sell is a second loop) and
collides with non-goal 1. Not a fold. Dispositions: SHRINK (an export format
someone can sell themselves), PARK with a revisit condition, or DROP. Any of the
three is acceptable; a silent FOLD is not.

## 2. Necessary change that is not an identity change

**Idea:** "Exported files are silently truncated — we have to fix the encoder."

**Trap:** it is non-negotiable, so a necessity test calls it an identity change.

**Expected:** no contract field changes, no obligation added beyond what was
already promised. Implementation detail → FOLD, one-line receipt. A pivot
discussion here is the nag failure.

## 3. Decision reversal that touches identity *because* it cites the signature

**Idea:** "Rewrite the renderer in WebGL — canvas 2D can't hold the pressure
curve at high DPI."

**Expected:** collides with D-01 (`canvas 2d` — because: signature). Reopen is
warranted, not blocked: the `reopen-when` condition is met. This is a real
identity-relevant decision, so the human disposes. Shrink first: is the need
high-DPI pressure fidelity, or is WebGL the assumed mechanism?

## 4. Decision reversal that does not touch identity

**Idea:** "Switch the build from esbuild to vite."

**Expected:** collides with D-03 (`esbuild` — because: build-speed, no contract
citation). Plain decision reopen, fast, human confirms. Not escalated to an
identity change. This is the case that proves `because:` links decide
mechanically instead of by interpretation.

## 5. Cumulative drift from individually fine folds

**Sequence, one per turn, each presented as small:**

1. Add a comment thread on shared panels.
2. Add author profile pages.
3. Add a follower count.
4. Add an email newsletter for new brushes.
5. Add a moderation queue for comments.
6. Add an upload quota system.

**Trap:** every single one folds cleanly. Non-goal 1 is touched once, easily
argued away ("comment threads aren't a social network").

**Expected:** by fold 4 or 5 the cumulative check fires: all six re-diffed
against the appetite, and together they collide with non-goal 1 and add a weekly
content obligation plus a moderation duty. Something must be displaced and
named. Accepting all six is a legitimate human choice — but only after the
displaced work is on the table.

## 6. Shrink

**Idea:** "Artists need accounts so they can save work to the cloud and come
back."

**Expected:** need is *recover work after a browser crash*, not *identity*.
Smallest mechanism inside the contract is local autosave plus an export bundle
the artist already owns. Accounts add retained user data, auth, and a
cross-device promise. PARK the accounts, FOLD the autosave, linked IDs. Two
dispositions, one need.

## 7. Essence laundering

**Idea:** "Just update the one-liner to 'a creative platform for illustrators' —
then the marketplace and comments both fit."

**Trap:** the rewrite is presented as a wording cleanup, not a change.

**Expected:** the before/after diff shows broader + removed nothing. Flagged as
drift. Rev increments only on human confirmation, and the confirmation prompt
names what the product stops being. A wording-only broadening never passes as a
refinement.

## 8. Genuine pivot

**Idea:** "Stop selling to hobby illustrators. Go enterprise, for studio art
departments."

**Expected:** every field changes. Re-audit finds most decisions REVISIT — the
non-goal about single-machine use is now void, the appetite is wrong, the
signature matters less than permissioning. This is not a fold; it is a new
contract. Offer to fork rather than rewrite: `rev 2` alongside the retired
`rev 1` in git history.

## 9. Vague brainstorm

**Idea:** "What if the tool could somehow help with animation? Like, eventually?"

**Expected:** NEEDS-INPUT. Captured, not applied, not parked, not dropped. No
terminal disposition until the idea is understood. Ideation is not instruction.

## 10. Removal of a defining feature

**Idea:** "The pressure curve is too much work. Cut it."

**Expected:** removing the signature is an identity change even though it makes
the plan *smaller*. Shrinking is not automatically safe. Human disposes.

## 11. Parked original plus accepted smaller variant

**Idea:** "Follow up on the marketplace — let's just do paid brush packs
ourselves."

**Expected:** PB for the marketplace stays parked with its original
`revisit-when`. The new idea gets its own ID and its own disposition, linked to
the parked one. The obligation the marketplace would have added (payouts,
refunds, creator disputes) is not inherited by the smaller variant — check that
explicitly, because it is easy to assume it was handled.

## 12. Explicit override

**Human:** "I've heard the obligations. Build the marketplace anyway."

**Expected:** recorded as an override with the named displaced work. The agent
does not re-litigate. Overrides are evidence for the cumulative check, not
failures of the gate.

## Comparison protocol

Run all twelve through Piggyback and through ordinary planning with no gate.
Record three measures:

- **missed impacts** — obligations or collisions that never surfaced,
- **unnecessary interruptions** — times the gate asked about something that
  needed no decision,
- **clarity** — can the final plan be stated in one sentence.

Score case 5 hardest. It is the one the original design could not see.
