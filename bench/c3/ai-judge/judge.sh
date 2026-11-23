#!/bin/sh
# One blind AI-judge call per task/order via Codex CLI (GPT sees the images natively).
# Usage: judge.sh T01 o1   -> out/T01-o1.jsonl (event log) and out/T01-o1.txt (final answer)
set -eu
J=$(dirname "$(readlink -f "$0")")
T=$1 O=$2 D="$J/blind/$1/$2"
REQ=$(python3 -c "import json,sys;print({t['id']:t['build_request'] for t in json.load(open('$HOME/projects/piggyback/bench/c3/tasks.json'))['tasks']}[sys.argv[1]])" "$T")
mkdir -p "$J/out"
PROMPT="Do not run any commands or read any files; use only the five attached images.
Image 1 is the ORIGINAL web page. Images 2-5 are page1, page2, page3, page4: web pages built for this request: \"$REQ\"
You are told nothing about how they were made; do not guess.
Pair 1: page1 (image 2) vs page2 (image 3). Pair 2: page3 (image 4) vs page4 (image 5).
For each pair pick the page that looks more like the ORIGINAL's design direction: colours, typefaces, spacing, layout and mood, as it would apply to the requested content. Do not judge which page you like more, which has more content, or which is more complete. You must pick one per pair.
Reply with one JSON object only: {\"original_background\": \"<colour in a few words>\", \"original_heading_type\": \"<a few words>\", \"pair1\": \"page1|page2\", \"pair1_reason\": \"<one line>\", \"pair2\": \"page3|page4\", \"pair2_reason\": \"<one line>\"}"
cd "$D"
timeout 600 codex exec --skip-git-repo-check --ephemeral -s read-only -m gpt-6.2-sol --json \
  -o "$J/out/$T-$O.txt" -i original.png -i page1.png -i page2.png -i page3.png -i page4.png \
  -- "$PROMPT" > "$J/out/$T-$O.jsonl" </dev/null
