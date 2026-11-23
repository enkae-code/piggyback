"""Build the blind C3 rating site and its private key. The key stays in the store.

Usage: python -m bench.c3.rate --store STORE --run RUN

Each generated page is copied as its index.html only, with HTML comments stripped, to
rate/p/TOKEN/index.html. The token key goes to STORE/RUN/rating-key.json. Refuses when the site
folder or the key already exists.
"""

import argparse
import html
import json
import random
import re
import secrets
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE / "rate"
ARMS = ["piggyback-brief", "screenshot", "skill-alone", "skill-plus-piggyback"]
PAIRS = {1: ("piggyback-brief", "screenshot"), 2: ("skill-alone", "skill-plus-piggyback")}
PAIR_KINDS = {"1": "gate", "2": "works-with"}
PACKETS = {f"P{n}": [f"T{t:02d}" for t in range(1, 6)] for n in (1, 2, 3)}
PACKETS.update({f"P{n}": [f"T{t:02d}" for t in range(6, 11)] for n in (4, 5, 6)})
ARM_WORDS = ["brief", "screenshot", "piggyback", "impeccable", "skill", "DESIGN.md", "measured"]
LIVE = "https://enkae-code.github.io/piggyback/starters/{}/"
COMMENT = re.compile(r"<!--.*?-->", re.S)
CSS = """body{font-family:system-ui,sans-serif;margin:1rem;max-width:1200px;line-height:1.5}
.pair{display:flex;flex-wrap:wrap;gap:1.5rem}
.frame{width:504px;height:315px;overflow:hidden;border:1px solid #777}
.frame iframe{width:1440px;height:900px;border:0;transform:scale(.35);transform-origin:0 0}"""


def new_token(taken):
    while True:
        token = secrets.token_hex(6)
        if token not in taken:
            return token


def scan(text, token):
    hits = []
    for word in ARM_WORDS:
        for m in re.finditer(re.escape(word), text, re.I):
            context = text[max(0, m.start() - 60) : m.end() + 60]
            hits.append(f"{token} [{word}] ...{' '.join(context.split())}...")
    return hits


def packet_entries(seed, packet, task_ids, cell):
    entries = []
    for tid in task_ids:
        order = [1, 2]
        random.Random(f"{seed}:{packet}:{tid}:order").shuffle(order)
        for pair in order:
            a, b = PAIRS[pair]
            if random.Random(f"{seed}:{packet}:{tid}:{pair}").random() < 0.5:
                a, b = b, a
            entries.append(
                {
                    "item": len(entries) + 1,
                    "task": tid,
                    "pair": pair,
                    "A": a,
                    "B": b,
                    "A_token": cell[(tid, a)],
                    "B_token": cell[(tid, b)],
                }
            )
    return entries


def packet_html(packet, entries, tasks):
    esc = html.escape
    parts = []
    for tid in dict.fromkeys(e["task"] for e in entries):
        task = tasks[tid]
        starter = task["starter_id"]
        parts.append(f'<section>\n<h2 id="{tid}">{tid}: {esc(task["reference"])}</h2>')
        parts.append(f"<p><strong>Task:</strong> {esc(task['build_request'])}</p>")
        parts.append(
            f'<p><a href="{LIVE.format(starter)}"><img src="ref/{starter}.png" width="720" '
            f'alt="Reference screenshot for {tid}. Opens the live original site."></a></p>'
        )
        for e in entries:
            if e["task"] != tid:
                continue
            n = e["item"]
            parts.append(f'<h3 id="item-{n}">Item {n}: {tid} pair {e["pair"]}</h3>')
            parts.append('<div class="pair">')
            for side in ("A", "B"):
                src = f"p/{e[side + '_token']}/index.html"
                parts.append(
                    f"<figure><figcaption>Page {side}</figcaption>"
                    f'<div class="frame"><iframe src="{src}" title="Item {n} Page {side}" '
                    f'sandbox="allow-scripts" loading="lazy"></iframe></div>'
                    f'<p><a href="{src}" target="_blank" rel="noopener">'
                    f"Open Page {side} full size</a></p></figure>"
                )
            parts.append("</div>")
        parts.append("</section>")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Packet {packet}: C3 rating</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
        f"<main>\n<h1>Packet {packet}</h1>\n"
        "<p>Ten items. For each item, pick Page A or Page B: the page that looks more like the "
        "original's design direction for that task. Then submit one issue with your ten picks "
        '(<a href="index.html">instructions</a>).</p>\n'
        + "\n".join(parts)
        + "\n</main>\n</body>\n</html>\n"
    )


INDEX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>C3 blind design rating</title>
<style>body{font-family:system-ui,sans-serif;margin:1rem;max-width:760px;line-height:1.5}</style>
</head>
<body>
<main>
<h1>C3 blind design rating</h1>
<p>Thanks for rating. This takes about 15 minutes.</p>
<ol>
<li>Check the <a href="https://github.com/enkae-code/piggyback/issues?q=label%3Ac3-rating">issues
labelled c3-rating</a> and pick a packet (P1 to P6) with fewer than three submissions.</li>
<li>You must not have built any of these pages.</li>
<li>For each item, judge only which page looks more like the original's design direction for
the task. Do not judge which you like more, or which is more complete.</li>
<li>Submit one <a href="https://github.com/enkae-code/piggyback/issues/new?template=c3-rating.yml">
C3 rating issue</a> with your packet and ten picks. Rate once.</li>
</ol>
<h2>Packets</h2>
<ul>
{links}
</ul>
</main>
</body>
</html>
"""

README = """# C3 rating site

Generated by `python -m bench.c3.rate`. Each page is copied as its `index.html` only, with HTML
comments stripped. Other files from the generated run are not published, and the token key is kept
outside the repository.
"""


def build(store, run, site=SITE):
    run_dir = Path(store) / run
    key_path = run_dir / "rating-key.json"
    if site.exists() or key_path.exists():
        raise SystemExit(f"refusing: {site} or {key_path} already exists")
    seed = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["rater_seed"]
    tasks = {t["id"]: t for t in json.loads((HERE / "tasks.json").read_text())["tasks"]}

    tokens, cell, hits = {}, {}, []
    for tid in tasks:
        for arm in ARMS:
            src = run_dir / "pages" / tid / arm / "project" / "index.html"
            text = COMMENT.sub("", src.read_text(encoding="utf-8"))
            token = new_token(tokens)
            dest = site / "p" / token / "index.html"
            dest.parent.mkdir(parents=True)
            dest.write_text(text, encoding="utf-8")
            tokens[token] = {"task": tid, "arm": arm}
            cell[(tid, arm)] = token
            hits += scan(text, token)

    ref = site / "ref"
    ref.mkdir()
    for starter in sorted({t["starter_id"] for t in tasks.values()}):
        shutil.copyfile(run_dir / "inputs" / starter / "screenshot.png", ref / f"{starter}.png")

    packets = {p: packet_entries(seed, p, ids, cell) for p, ids in PACKETS.items()}
    for p, entries in packets.items():
        (site / f"{p}.html").write_text(packet_html(p, entries, tasks), encoding="utf-8")
    links = "\n".join(
        f'<li><a href="{p}.html">{p}</a>: {PACKETS[p][0]} to {PACKETS[p][-1]}</li>' for p in PACKETS
    )
    (site / "index.html").write_text(INDEX.replace("{links}", links), encoding="utf-8")
    (site / "README.md").write_text(README, encoding="utf-8")

    key = {
        "run": run,
        "rater_seed": seed,
        "html_comments_stripped": True,
        "pair_kinds": PAIR_KINDS,
        "tokens": tokens,
        "packets": packets,
        "arm_word_hits": len(hits),
    }
    with key_path.open("x", encoding="utf-8") as f:
        json.dump(key, f, indent=2)
    key_path.chmod(0o600)
    return hits


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    hits = build(args.store, args.run)
    for hit in hits:
        print(hit)
    print(f"arm-word hits: {len(hits)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
