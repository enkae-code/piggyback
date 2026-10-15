#!/usr/bin/env python3
"""Drift alarm: check added lines against the locked rules in a contract file.

Fires when an added line reuses the vocabulary of a locked rule (a non-goal,
a locked decision). It is an alarm, not a verdict — a human still reads the
finding and judges.

Ground truth: the one real catch from the 2026-10-14 Piggyback validation —
HollowPrismOS v0.4.0 (commit e976593) embedded authenticated webviews against
a locked "never an embedded authenticated inbox". That shape is the
self-test fixture.

ponytail: term co-occurrence per ADDED LINE, threshold 2 terms. No semantic
matching, so "webview" vs "embedded" is a miss and touching the forbidden
surface to fix it still fires. Hunk-level matching was tried first and
measured worse (4-7 findings per ordinary commit). All limits measured on
real commits, documented in the README, not assumed.
"""

import argparse
import re
import subprocess
import sys

_STOPWORDS = (
    "a an the and or not never no is are be been in on for to of with as at by "
    "it its this that only appears appear always must should can may will would "
    "from into than then when where which who what how all any each one per via "
    "if so but does do has have had"
)
STOPWORDS = frozenset(_STOPWORDS.split())
HEADING = re.compile(r"non-?goal|locked|direction|contract|constraint", re.IGNORECASE)
LIST_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(\S.*\S)\s*$")
LOCKED_DECISION = re.compile(r"D-\d+.*\blocked\b", re.IGNORECASE)
PROHIBITION = re.compile(
    r"(?<![\w-])(?:never|not|no|without|must|only|reserved|forbidden|avoid)"
    r"(?![\w-])",
    re.IGNORECASE,
)


def terms(text):
    """Significant terms: lowercase, hyphenated compounds kept whole,
    stopwords and 1-2 char tokens dropped."""
    seen = []
    for tok in re.findall(r"[a-z]+(?:-[a-z]+)*", text.lower()):
        if len(tok) > 2 and tok not in STOPWORDS and tok not in seen:
            seen.append(tok)
    return seen


def rules_from_text(text):
    """Rules = prohibition-flavored list items under a heading naming
    non-goals/locks (soft-wrapped items are joined), plus locked D-nn
    decision lines anywhere in the file. Identity statements without a
    prohibition marker are excluded — measured on real commits, they fire on
    every change to the surface they describe. Rules with <2 terms are
    dropped: one shared word is noise, not drift."""
    rules, in_section, cur = [], False, None

    def flush():
        nonlocal cur
        if cur:
            rules.append(cur)
            cur = None

    for line in text.splitlines():
        stripped = line.strip()
        if HEADING.search(line) and len(line) < 120:
            flush()
            in_section = True
            continue
        if line.startswith("#"):
            flush()
            in_section = bool(HEADING.search(line))
            continue
        if in_section and stripped:
            m = LIST_ITEM.match(line)
            if m:
                flush()
                cur = m.group(1)
            elif cur is not None:
                cur += " " + stripped
        elif not stripped:
            flush()
    flush()
    locked = [l.strip() for l in text.splitlines() if LOCKED_DECISION.search(l)]
    rules = [
        r
        for r in dict.fromkeys(rules + locked)
        if PROHIBITION.search(r) or LOCKED_DECISION.search(r)
    ]
    return [r for r in rules if len(terms(r)) >= 2]


def diff_lines(since=None):
    """(path, line-number, text) for every added line from `git diff` (working
    tree vs HEAD by default; --since accepts anything git diff accepts,
    e.g. A..B)."""
    cmd = ["git", "diff"] + (since.split() if since else ["HEAD"])
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("piggyback: git not found — run inside a git repository")
    if proc.returncode != 0:
        sys.exit(f"piggyback: git diff failed:\n{proc.stderr.strip()}")
    added, path, lineno = [], "?", 0
    for line in proc.stdout.splitlines():
        if line.startswith("diff --git"):
            path = line.split(" b/")[-1]
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            added.append((path, lineno, line[1:].strip()))
            lineno += 1
        elif line.startswith(" "):
            lineno += 1
    return added


def drift_in_line(rule, text, min_terms):
    """Rule terms appearing in one added line, or None below threshold.

    Line-level, not hunk-level: a real drift signature (a config line naming
    the forbidden surface) co-occurs tightly, while generic project
    vocabulary spreads across the hunk. Measured: hunk-level matching fired
    4-7 findings per ordinary commit; the known violation is single-line.
    """
    hits = [
        t for t in terms(rule) if re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE)
    ]
    return hits if len(hits) >= min_terms else None


def self_test():
    """One runnable check: the real violation shape fires; a clean line stays
    quiet."""
    contract = (
        "## Direction (locked)\n\n"
        "- Zoho mail appears as a read-only projection, never an embedded "
        "authenticated inbox.\n"
    )
    violation = '  { label: "Zoho Mail", url: "https://mail.zoho.com" },'
    clean = "  const x = 1;"
    rules = rules_from_text(contract)
    assert drift_in_line(rules[0], violation, 2), "missed the real violation"
    assert drift_in_line(rules[0], clean, 2) is None, "cried wolf on clean"
    print("self-test: ok — fires on the real violation shape, quiet on clean")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "contract", nargs="?", help="contract file (PIGGYBACK.md, Overview.md, ...)"
    )
    parser.add_argument(
        "--since", help="revision or range for git diff (default: HEAD)"
    )
    parser.add_argument(
        "--min-terms", type=int, default=2, help="terms per hunk to fire (default 2)"
    )
    parser.add_argument(
        "--self-test", action="store_true", help="run the embedded check"
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.contract:
        parser.error("contract file required")

    with open(args.contract, encoding="utf-8") as fh:
        rules = rules_from_text(fh.read())
    findings = []
    for rule in rules:
        for path, lineno, text in diff_lines(args.since):
            hits = drift_in_line(rule, text, args.min_terms)
            if hits:
                findings.append((path, lineno, hits, rule, text))
    for path, lineno, hits, rule, text in findings:
        print(f"DRIFT  {path}:{lineno} — terms: {', '.join(hits)}")
        print(f"       {text[:90]}")
        print(f"       rule: {rule[:100]}")
    if findings:
        print(
            f"\npiggyback: {len(findings)} finding(s) — alarm, not verdict; read the rule"
        )
        return 1
    print(f"piggyback: quiet ({len(rules)} rules checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
