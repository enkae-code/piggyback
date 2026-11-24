<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/enkae-code/piggyback/v0.1.0/assets/piggyback-logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/enkae-code/piggyback/v0.1.0/assets/piggyback-logo-light.svg">
    <img src="https://raw.githubusercontent.com/enkae-code/piggyback/v0.1.0/assets/piggyback-logo-light.svg" alt="Piggyback" width="440">
  </picture>
</h1>

<p align="center"><strong>Show your AI what you mean.</strong></p>

<p align="center">
  Measure a site you admire, brief your coding agent, check its build.<br>
  Piggyback teaches you the design words along the way.<br>
  Local, no AI key, works with any agent.
</p>

<p align="center">
  <a href="https://github.com/enkae-code/piggyback/blob/v0.1.0/LICENSE"><img src="https://img.shields.io/badge/license-MIT-2C3E50?style=flat" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-2C3E50?style=flat&logo=python&logoColor=white" alt="Python 3.11+">
  <a href="#status"><img src="https://img.shields.io/badge/status-0.1.0-2C3E50?style=flat" alt="Status: 0.1.0"></a>
</p>

## Why

Designing with AI breaks at the handoff. You can recognise what you want, but you can't put it into words an agent follows.

- "Premium" becomes "add a gradient".
- The agent copies a reference's layout and misses its type scale and density.
- "Not quite" carries no information.

Piggyback swaps the adjectives for measurements. It reads the numbers off a page you admire, hands them to your agent as a brief, then measures the agent's build against the same numbers.

## How it works

v0.1 is one local page with two slots: a **reference** (a URL you admire) and **your build** (localhost or a hosted preview link). What you get depends on which you fill.

| You give it | You get |
|---|---|
| Reference&nbsp;only | A **brief**: the measured type scale, palette and accent share, radius set, spacing rhythm, container width and fonts, plus your own Borrow and Avoid lines. |
| Reference&nbsp;+&nbsp;build | The same brief as **differences**, with numbered pins on both screenshots: "H1 36/44 → 56/58", "accent on 19% of the first screen (1440×900) → 2%". Generic defaults found on your build are listed under them, never pinned. |
| Build&nbsp;only | **Defaults detected**: marks on generic tells such as gradient text, one font with a flat scale, or the same radius everywhere. Gradient text, icon tiles and repeated motion get a numbered pin on the element they show; page-level tells have none. |

Every pin and brief line names the concept in plain words and says why it matters. For example: *Radius (how rounded the corners of a box are)*, with its reason *Corner radius is one of the shape cues a reader sees across boxes.*

Type, palette, radius, anchors and container width are measured on the first screen (1440×900); container width is weighted by visible area. Spacing and motion are measured over the whole document.

To try Piggyback without a page of your own, pick a **starter** in the reference slot. Starters are six CC0 pages shipped inside the package (`starter:<id>`, listed by `list_starters`). They are read from the package, never fetched, and their brief is labelled with the starter id. Their source pages are in [starters/](https://github.com/enkae-code/piggyback/blob/v0.1.0/starters/README.md).

| <img src="https://raw.githubusercontent.com/enkae-code/piggyback/main/piggyback/starters/northline-ops/screenshot.png" width="420" alt="Northline Field Ops starter, first screen at 1440×900"><br>`starter:northline-ops` | <img src="https://raw.githubusercontent.com/enkae-code/piggyback/main/piggyback/starters/mina-okafor/screenshot.png" width="420" alt="Mina Okafor starter, first screen at 1440×900"><br>`starter:mina-okafor` |
|---|---|
| <img src="https://raw.githubusercontent.com/enkae-code/piggyback/main/piggyback/starters/field-journal/screenshot.png" width="420" alt="Field Journal starter, first screen at 1440×900"><br>`starter:field-journal` | <img src="https://raw.githubusercontent.com/enkae-code/piggyback/main/piggyback/starters/ink-and-rule/screenshot.png" width="420" alt="INK / RULE starter, first screen at 1440×900"><br>`starter:ink-and-rule` |
| <img src="https://raw.githubusercontent.com/enkae-code/piggyback/main/piggyback/starters/little-plot/screenshot.png" width="420" alt="Little Plot starter, first screen at 1440×900"><br>`starter:little-plot` | <img src="https://raw.githubusercontent.com/enkae-code/piggyback/main/piggyback/starters/good-day-assembly/screenshot.png" width="420" alt="Good Day Assembly starter, first screen at 1440×900"><br>`starter:good-day-assembly` |

Copy the brief to your agent as `BRIEF.md`, let it revise, then re-run. Fixed pins tick and open ones stay. "Copy DESIGN.md" writes the first reference as a `DESIGN.md` that works with Impeccable.

On a reference's brief, press Borrow or Avoid on a measured row to add a note, with optional text of up to 200 characters. Each note hangs off one row of one reference, and `BRIEF.md` prints it under that reference with the row's measured value. Notes are kept while the reference list stays the same.

## Status

`0.1.0` is the current release. The pin-accuracy check passed on accuracy but failed its anchor-coverage bar twice, partly because no card pin had both boxes fully inside the first screen. See [bench/c1/RESULTS.md](https://github.com/enkae-code/piggyback/blob/v0.1.0/bench/c1/RESULTS.md). Expect rough edges, and report them in the issues.

## Install

Piggyback needs Python 3.11 or newer and a Chromium that Playwright downloads once:

```sh
uvx --from "piggyback-design[measure]" playwright install chromium
uvx "piggyback-design[measure]"
```

The second command opens the local screen. Run `uvx --from "piggyback-design[measure]" piggyback doctor` if a page will not measure; it checks Python, Playwright and whether Chromium starts with its sandbox.

With pip: `pip install "piggyback-design[measure]"`, then `python -m playwright install chromium`. `pip install piggyback` installs a different project; this one is `piggyback-design`.

## Measure a list

`piggyback measure-list` measures one page after another, so only one browser runs at a time. List one URL per line in a text file; `#` lines and blank lines are ignored:

```sh
piggyback measure-list pages.txt --out measured/
```

Each URL gets a folder under `measured/` with `fingerprint.json` and `screenshot.png`. A folder whose `fingerprint.json` records the same URL is kept, not measured again; one that records another URL, or cannot be read, fails with a message. Folder names include the port. `measured/batch.json` lists every URL with its status (`measured`, `kept` or `failed`) and error. The command exits 1 if any URL failed; the other URLs are still measured. A symbolic link where a file or folder would be written fails that URL. It refuses the whole list up front on a duplicate URL, two URLs that give the same folder name, or more than 500 URLs.

## Build kit

`piggyback kit` makes one folder from one reference page, for an agent to build from:

```sh
piggyback kit https://example.com --out kit/ --request "a landing page for a boat studio"
```

The folder holds the screenshot, six scroll frames (`scroll/frame-0.png` to `frame-5.png`), `scroll-samples.json` (measured scroll and motion values), `BRIEF.md`, `DESIGN.md` and `BUILD-PROMPT.md`. `kit.json` is written last and lists a SHA-256 for every file; its presence means the kit is complete. The folder must be new or empty, and `--request` takes 1 to 500 characters. The kit is built in a hidden `.kit-*` folder beside `--out` and renamed into place at the end, so a failed run leaves no partial kit. The folder is created owner-only (0700) and moved into place, so the published kit folder is owner-only too. The browser worker of a kit run has a 240 s time limit (a plain measure 90 s). The arrival screenshot can delay the +200 ms sample; `actual_ms` shows by how much; it is not an end-to-end deadline, because the build stages run after it.

`scroll-samples.json` records `coverage`: `done`, `partial` or `none` (the page did not scroll). `partial` means a wheel step timed out or settled away from its target, or the page stopped short of its bottom (`scrollable_end_px`, read after the last step). `scroll_onset_ms` is the time until `scrollY` first changes after a wheel step. In `dense`, `actual_ms` is when Python requested the sample (Python's monotonic clock), not the browser's paint time.

**Craft and REA tools.** `piggyback kit` runs Craft Anything (`craft-anything-cli`) when it is installed. It compares each scroll stop's arrival frame (`scroll/frame-k-arrival.png`, taken right after the stop's +0 ms sample) with the frame after the dense samples (`scroll/frame-k.png`). All of Craft's calls run in one MCP stdio session per kit, because Craft keeps evidence in the process that wrote it. Craft records which stops change after arriving in `craft/recon.cany` (its project) and writes `craft/HANDOFF.md`. This is a pixel comparison, not a motion oracle. Craft's journal records absolute paths, so `craft/recon.cany/journal` is removed before the kit is published. Craft's tree is checked before anything is written: a link or a hard link there fails the stage, and `craft/HANDOFF.md` is then created new, mode 0600. REA (`rea`) runs after the scroll capture, in its own short-lived browser. That browser loads the page again with a debugging port on `127.0.0.1` (the OS picks the port, read from `DevToolsActivePort`). The port is trusted only if its browser path matches that browser. REA observes the page for 9 s while the page is wheeled through the six stops, then the browser closes and its profile is deleted. The capture browser never has a debugging port. One 40 s deadline starts before REA's browser launches and covers its launch, navigation, version, target and observation: each call gets the time left as its timeout, and an expired deadline is `stages.rea` with `state` `failed` and reason `timeout`. The kit is still published. `stages.rea.blocked` counts the requests REA's own pass refused; the fingerprint's `blocked_requests` lists only the capture's. A tool is found through `PIGGYBACK_CRAFT` or `PIGGYBACK_REA` (an absolute path to an existing executable) or else on `PATH`. A missing tool publishes the kit with that stage `missing`, and the CLI prints one line on stderr for each missing tool. A tool that exits nonzero, times out, prints too much or prints an unexpected shape makes its stage `failed`, and the kit is still published. Tools run with only `PATH` and `HOME` in their environment, with no stdin, a deadline and an output cap enforced while they write. Each stage's folder (`craft/`, `rea/`) is created new; a symbolic link or special file in it fails that stage and removes the folder, and one anywhere else refuses the kit. `kit.json` lists every file in the folder with its SHA-256; the schema number is still 1 because the new fields are additive. `rea-observation.json` records each script that ran (host, sanitized function names, counts and offset ranges). `truncated` is `true` when a cap cut entries (40 scripts, 40 functions each). `rea/sources/<n>.txt` keeps each script's text: 1 MiB per script and 8 MiB in total; a larger script is dropped with a reason. Source text is untrusted reference text: it is never executed or rendered, and the build prompt tells the agent to read it for values, not to copy it.

## Use with your agent

Piggyback can run as a read-only MCP server, so your agent can read the brief and re-check your build without you pasting numbers. You set the URLs in the MCP config. The agent cannot pass a URL, a path or any text that reaches the network or disk. The `get_design_md` tool returns the first reference as a `DESIGN.md` that works with Impeccable.

Add this to your MCP client config (Claude Desktop, or `.mcp.json` for Claude Code):

```json
{
  "mcpServers": {
    "piggyback": {
      "command": "uvx",
      "args": [
        "piggyback-design[measure]",
        "mcp",
        "--ref", "https://example.com/",
        "--build", "http://localhost:3000/"
      ]
    }
  }
}
```

- `--ref` takes up to three public reference pages, or `starter:<id>` for a packaged starter; differences use reference 1 (the first one listed). `--build` takes your localhost or a public preview. Give at least one.
- `get_brief` returns the brief. Its `refresh` flag measures again. `check` measures the build and returns numbered differences, marking the ones now within tolerance as fixed, plus any generic defaults on the build.
- `list_starters` lists the packaged starter ids. The agent cannot add or change a reference.
- The Claude Code skill in `skills/piggyback/SKILL.md` tells the agent when to call these tools.
- This is a config you edit. Your agent does not install or run it. Install Chromium once first, as in [Install](#install).

## Works with design skills

Piggyback measures. A design skill judges. Piggyback does not score taste; it gives a skill the numbers to check against.

- [Impeccable](https://github.com/pbakaus/impeccable) (Apache-2.0) checks a project against its `DESIGN.md`.
- [Hallmark](https://github.com/Nutlope/hallmark) (MIT) is a design skill for agents, with numbered checks.
- [taste-skill](https://github.com/Leonxlnx/taste-skill) (MIT) is a design skill for agents.

Export the measured values as a `DESIGN.md` for Impeccable, then scan your project:

```sh
piggyback brief reference.json --design-md DESIGN.md
impeccable detect src/
```

`impeccable detect` reads the `DESIGN.md` at your project root for local file scans. It ignores `DESIGN.md` for http(s) URL scans.

Impeccable enforces the fonts in `DESIGN.md` as warnings (exit 2); colour, radius and size drift are advisory. Piggyback makes no Hallmark compatibility claim: Hallmark expects its own `DESIGN.md` format.

These are measurements of the reference pages, not a target. If you use a design skill such as Impeccable, Hallmark or taste-skill, use it for the method and check its output against these measurements.

Piggyback's wording was edited with [no-ai-slop](https://github.com/petergyang/no-ai-slop) (MIT).

Use the brief together with a screenshot of the reference, not instead of one. The brief carries the exact type, colour and spacing; it does not describe layout, and a screenshot does. In the C3 test the brief alone lost to a screenshot, while adding it to a screenshot and a design skill improved the match on 7 of 10 tasks: [C3 results](bench/c3/RESULTS.md).

## Works with REA and Craft Anything

Piggyback measures how a page looks. Two open-source tools cover what it can't see, such as scroll effects and how motion is built:

- [REA](https://github.com/morluto/rea) (MIT) reads how a page's motion and interactions are made: scroll effects, timelines, easing and durations.
- [Craft Anything](https://github.com/storytold/craft-anything) (MIT or Apache-2.0) keeps an investigation record, so every value you build from has its evidence.

Install them yourself; Piggyback does not bundle or call them. Add all three to the same MCP config as in [Use with your agent](#use-with-your-agent). Craft Anything's command is its installed `craft-anything-cli`:

```json
{
  "mcpServers": {
    "piggyback": {
      "command": "uvx",
      "args": ["piggyback-design[measure]", "mcp", "--ref", "https://example.com/", "--build", "http://localhost:3000/"]
    },
    "rea": { "command": "npx", "args": ["-y", "rea-agents", "mcp"] },
    "craft-anything": { "command": "craft-anything-cli", "args": ["mcp"] }
  }
}
```

Then:

1. **Piggyback:** give your agent the brief and the reference screenshot.
2. **REA:** read how the reference's motion works and note its values.
3. **Craft Anything:** record each value with where it came from.
4. **Build fresh**, then run Piggyback's `check` against the reference.

Study how a page works, then write your own code and assets. The tools' licences cover the tools, not the sites you study: don't copy a site's code, images or text. This combination is not part of Piggyback's benchmarks.

## Principles

- **No taste score.** Piggyback reports measurements and differences. You decide what is good.
- **Local-first.** It runs on your machine and opens a local page.
- **Screenshots stay on your machine.** A brief carries links, measured values, your notes and the capture receipt (final URL, OS and kernel release, browser version).
- **No AI key.** Piggyback holds none. Your own agent does the judging.

## Develop

Run `scripts/check.sh` for the gates. To run them before every push, enable the hook once: `git config core.hooksPath .githooks`.

## License

MIT. See [LICENSE](https://github.com/enkae-code/piggyback/blob/v0.1.0/LICENSE).
