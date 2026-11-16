# Froggle

A daily word game. Bridge two words with a linking word to get the frog across the river.
- **Lean too far toward the far bank:** the frog falls in and loses a life (3 per game).
- **Lean toward the start bank:** the frog lands safely but needs more hops.

Open `froggle.html` to play today's puzzle. Add `?date=2026-11-17` to the address to play another day as practice, including Draft days.

## Planning puzzles: `content/Froggle Schedule.xlsx`

Share this workbook with your partner through OneDrive and edit it together in Excel, desktop or web. The **How to use** sheet inside has the full instructions.

- **Days sheet:** one row per date, already filled in 6 months ahead.
  - Type up to 6 bank words; each pair of neighbouring banks is one crossing.
  - Set Status to **Draft** while you work and **Ready** when it should go live.
- **Words sheet:** one row per curated word. Date + Crossing picks the crossing, and the Pair column fills itself in so you can check it.

| Type | What it does |
|---|---|
| **Perfect link** | Crosses in one hop, always gold. The whole crossing is calibrated around it. |
| **Extra word** | Accepts or moves a word. Position runs from 0 (start bank) to 1 (far bank). Use it for words the dictionary is missing. |
| **Banned** | Never grows a lily pad. |

**Bonus:** any word with a Bonus is a **fly**. Crossings without one get an automatic fly on their strongest perfect link.

## Building

```
py pipeline/build.py
```

What the build does:
- **Reads the workbook** and lists any mistakes by sheet and row ("Words row 14 (nanny): Position must be a number from 0 to 1").
- **Only rebuilds days that changed,** so planning months ahead stays fast.
- **Freezes days once their date has passed,** so published scores stay comparable. `--force` overrides this.
- **Warns about gaps:** any of the next 14 days without a Ready puzzle.
- **Writes `pipeline/reports/overview.md`:** every upcoming day, its status and chain, and any problems.
- **Writes `pipeline/reports/<date>.md` for each day:** perfect links, quality tiers, flies, and where your curated words landed.

**Setup:**
- **Install the libraries once:** `py -m pip install -r pipeline/requirements.txt`
- **First build:** it downloads ConceptNet Numberbatch (~325 MB) into `pipeline/cache/`.
- **Settings:** game rules and scoring knobs live in `content/settings.json`.

## Publishing to GitHub Pages

The game is served from `docs/` at https://jbroefitzpatrick.github.io/froggle/. After building new days:

```
py pipeline/build.py
py pipeline/make_site.py      # copies the game + Ready days into docs/
git add -A
git commit -m "Publish puzzles"
git push
```

**What's kept out of the repo:** the shared workbook, the reports and the model cache are git-ignored, so planning notes stay private. The published `docs/data/` files do contain every Ready day, including future ones.

## Beta hosting (claude.ai)

The beta is also hosted at https://claude.ai/artifact/Y5VHZh191qoYf92ZaMnxp3 and is private until shared from its Share menu.

- **Beta numbering:** while `"beta": true` is set in `content/settings.json`, puzzles are numbered 1B, 2B, and so on.
- **Previewing a day:** add `#2026-11-17` to the link.
- **Updating after new days are built:**
  1. Run `py pipeline/make_hosted.py`, which writes `dist/froggle.html`.
  2. Republish `dist/froggle.html` together with the `data/` files to the same link.

## Scoring

| Part | Points |
|---|---|
| Base points for a crossing (1 / 2 / 3 / 4+ hops) | 10 / 6 / 4 / 2 |
| Link quality: crossing × (1 + 0.5 × average quality of the pads on the path) | up to +50% |
| Fly eaten 🪰 | the fly's Bonus |
| Each life left at the end | +2 |

- **Link quality** (0–1) ranks a word's relatedness against the other words landing in the same band of the river. Gold is 0.8 and up, silver 0.4 and up, bronze below that.
- **Ranges:** a one-hop crossing scores 10–15 and a two-hop crossing 6–9, so better links never make extra hops worth it.

## Folder layout

```
content/Froggle Schedule.xlsx  the shared puzzle plan (you + partner edit this)
content/settings.json          game rules + scoring knobs
pipeline/build.py              workbook → data/
pipeline/schedule.py           reads + validates the workbook
pipeline/new_schedule.py       creates a fresh workbook (won't overwrite)
pipeline/reports/              overview.md + one report per day
data/                          generated; the game loads this
```
