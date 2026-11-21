# C1 pin-accuracy results

C1 checks whether Piggyback's anchor pins point at the right elements on real site pairs. It has two bars, and both must pass:

- **Accuracy:** at least 90% of shown anchor pins are correct.
- **Coverage:** on average at least 3.00 of the 5 anchor slots (heading, body, CTA, nav, card) are pinned per pair.

The run uses 20 public reference/build pairs at 1440x900, one shot per run. The first score of each run is final, and neither run was rerun.

## Runs

| Run | Set | Locked (UTC) | Manifest commit | Chromium | Pins correct / shown | Avg pinned anchors | Verdict |
|---|---|---|---|---|---|---|---|
| `c1-gate-20261119` | Set 1 | 2026-11-20 | `42ea27b` | 156.0.8078.4 | 55 / 56 (need 51) | 2.60 (need 3.00) | fail |
| `c1-gate-20261121` | Set 2, 20 new pairs | 2026-11-21 | `22159bd` | 156.0.8078.4 | 58 / 62 (need 56) | 2.75 (need 3.00) | fail |

Accuracy passed in both runs. Both runs failed on anchor count: 52 of 100 slots pinned in run 1 and 55 of 100 in run 2.

## Per-slot dispositions

Each cell shows the pinned count. The other dispositions are listed beside it.

| Slot | Run 1 pinned | Run 1 other | Run 2 pinned | Run 2 other |
|---|---|---|---|---|
| heading | 18 | partly visible 2 | 11 | not found 8, no pin 1 |
| body | 8 | not found 12 | 15 | no pin 1, not found 4 |
| cta | 14 | partly visible 1, no pin 1, not found 4 | 14 | no pin 4, not found 2 |
| nav | 12 | no pin 5, not found 3 | 15 | no pin 5 |
| card | 0 | partly visible 5, not found 6, no pin 9 | 0 | no pin 18, partly visible 1, not found 1 |

## Why the card slot scores 0

The card slot scored 0 pins in both runs. This is a documented limitation of the current harness, not a finding about the sites:

- The card overlay counts only when the card group sits fully inside the first 900px screen on both pages. Card grids sit below that fold on nearly every site in the set. In run 2, 29 of the 39 sides with a card had it below the fold, and none of the 19 pairs with a card on both sides had it fully in view on both.
- The card pin compares the card container's type. That container usually inherits the page's base type, so the comparison rarely finds a card-specific difference.
- Heading and body rows marked *not found* were pages that truly lack that anchor.

These results must not be read as complete difference detection. The anchor floor guarantees volume of pins, not recall: a pin set can meet the count and still miss differences the page has.

No human spot-check was done, because both verdicts fail on a count, not on a judgment.

## Per-pair results: `c1-gate-20261119`

| Pair | Reference | Build | Shown pins | Correct pins | Anchors pinned |
|---|---|---|---|---|---|
| C1-01 | https://stripe.com/ | https://linear.app/ | 3 | 3 | 2 |
| C1-02 | https://linear.app/ | https://vercel.com/ | 1 | 1 | 1 |
| C1-03 | https://vercel.com/ | https://docs.python.org/3/ | 3 | 3 | 3 |
| C1-04 | https://docs.python.org/3/ | https://developer.mozilla.org/ | 4 | 3 | 4 |
| C1-05 | https://www.shopify.com/ | https://www.ikea.com/ | 3 | 3 | 2 |
| C1-06 | https://www.apple.com/ | https://www.patagonia.com/ | 2 | 2 | 2 |
| C1-07 | https://www.bbc.com/ | https://www.theguardian.com/ | 2 | 2 | 1 |
| C1-08 | https://www.npr.org/ | https://www.metmuseum.org/ | 3 | 3 | 3 |
| C1-09 | https://www.gov.uk/ | https://www.usa.gov/ | 4 | 4 | 4 |
| C1-10 | https://www.figma.com/ | https://github.com/ | 2 | 2 | 2 |
| C1-11 | https://www.notion.com/ | https://www.mozilla.org/ | 3 | 3 | 3 |
| C1-12 | https://www.airbnb.com/ | https://www.ikea.com/ | 3 | 3 | 3 |
| C1-13 | https://www.nationalgeographic.com/ | https://www.bbc.com/ | 3 | 3 | 2 |
| C1-14 | https://www.nasa.gov/ | https://www.gov.uk/ | 4 | 4 | 4 |
| C1-15 | https://www.starbucks.com/ | https://www.chipotle.com/ | 2 | 2 | 2 |
| C1-16 | https://www.sweetgreen.com/ | https://www.apple.com/ | 3 | 3 | 3 |
| C1-17 | https://www.mozilla.org/ | https://vercel.com/ | 2 | 2 | 2 |
| C1-18 | https://www.metmuseum.org/ | https://www.airbnb.com/ | 3 | 3 | 3 |
| C1-19 | https://www.usa.gov/ | https://www.npr.org/ | 4 | 4 | 4 |
| C1-20 | https://www.patagonia.com/ | https://www.figma.com/ | 2 | 2 | 2 |

## Per-pair results: `c1-gate-20261121`

| Pair | Reference | Build | Shown pins | Correct pins | Anchors pinned |
|---|---|---|---|---|---|
| C1-01 | https://buffer.com/ | https://www.todoist.com/ | 5 | 5 | 4 |
| C1-02 | https://bun.com/ | https://deno.com/ | 4 | 3 | 4 |
| C1-03 | https://docs.deno.com/ | https://pkg.go.dev/ | 0 | 0 | 0 |
| C1-04 | https://www.muji.us/ | https://www.llbean.com/ | 2 | 2 | 2 |
| C1-05 | https://www.propublica.org/ | https://www.rockpapershotgun.com/ | 2 | 2 | 1 |
| C1-06 | https://www.guggenheim.org/ | https://www.getty.edu/ | 2 | 2 | 2 |
| C1-07 | https://www.census.gov/ | https://www.irs.gov/ | 4 | 4 | 4 |
| C1-08 | https://www.noodles.com/ | https://www.wagamama.com/ | 3 | 1 | 2 |
| C1-09 | https://bandcamp.com/ | https://www.abebooks.com/ | 4 | 4 | 3 |
| C1-10 | https://www.gimp.org/ | https://kde.org/ | 3 | 3 | 3 |
| C1-11 | https://www.loom.com/ | https://www.census.gov/ | 4 | 4 | 4 |
| C1-12 | https://bun.com/ | https://www.irs.gov/ | 4 | 4 | 4 |
| C1-13 | https://www.llbean.com/ | https://www.propublica.org/ | 4 | 4 | 3 |
| C1-14 | https://www.noodles.com/ | https://pkg.go.dev/ | 2 | 2 | 2 |
| C1-15 | https://www.getty.edu/ | https://bandcamp.com/ | 4 | 4 | 3 |
| C1-16 | https://www.openstreetmap.org/ | https://www.todoist.com/ | 4 | 4 | 4 |
| C1-17 | https://www.wagamama.com/ | https://www.muji.us/ | 3 | 2 | 3 |
| C1-18 | https://www.gimp.org/ | https://www.guggenheim.org/ | 2 | 2 | 2 |
| C1-19 | https://www.rockpapershotgun.com/ | https://go.dev/ | 3 | 3 | 2 |
| C1-20 | https://1password.com/ | https://www.irs.gov/ | 3 | 3 | 3 |

## Re-run

Follow [bench/c1/README.md](README.md). Evidence is written outside the repository.
