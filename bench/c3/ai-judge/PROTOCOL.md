# C3 AI-judge check (unofficial; fixed before any judging, 2026-11-22)

Unofficial evidence only. The preregistered C3 gate already FAILED on the turn condition; nothing here changes that.

Judge: OpenAI gpt-6.2-sol via Codex CLI (`judge.sh`: images attached natively with `-i`, read-only sandbox, no tools allowed), a different vendor from the builder (Claude Opus 5.5). Model identity and zero tool use are checked on every result. (Amended 2026-11-23 before any pick: the first route, the `reviewer2` profile, could not see the attached pixels and fell back to a Claude vision tool, so it made no picks and was dropped.)

Blind input per task: the frozen 1440x900 starter screenshot (the same image the screenshot arm received) and four 1440x900 above-the-fold renders named page1-page4. Pair 1 = page1 vs page2 (Piggyback brief vs screenshot), pair 2 = page3 vs page4 (skill alone vs skill + brief). The judge is told nothing about how pages were built. The task's build request is given.

Position control: every task is judged twice by separate judges, with each pair's order swapped (o1, o2; order from random.Random("ai-judge:20261122:<task>")). A pair counts as a win only when both judgements pick the same arm; otherwise it is a tie.

Question: which page looks more like the original's design direction (colour, type, spacing, layout, mood) for the requested content; not preference or completeness.

Reading: report wins/ties/losses per pair. "Brief looks closer" needs Piggyback brief to win at least 8 of 10 tasks, mirroring the gate bar; anything else is reported as is.

Machine measure (secondary): Piggyback's own gap check (piggyback.gap) between the starter and each page, share of comparable concepts that match. Bias: this is Piggyback's own metric, the same one its brief is written from, so it favours the brief arm; reported with that label.
