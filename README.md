# Dock Closure Remark Parser

Pulls start/end times and dates out of the messy `remarks` column in our dock
closure CSV so we have something usable for downstream analysis instead of a
bunch of free-text notes.

## Why this exists

The remarks are written by hand by whoever is on shift, and everyone writes
them differently. Same idea, dozens of formats:

- `0700-1700 daylight hours   05-22 thru 05-24`
- `May 21st 0500 until further notice per John Doe`
- `Closed Wednesday ,May 28, at 0700 hours through 1900 hours`
- `0700 29th - 1700 1st`
- `closed 7/10 0700 thru 7/13 2330`
- `Closed June 20th 2019 from 0700 until 2000`
- `From 0800 to 1200 on Tuesday, May 28, 2024`
- `0700 to 1700 daily 5/12 thru 5/16`
- `Closed for a 4 hour window for Thursday 12/18 (8am-12pm)`

I needed start/end pairs out of all of them, and there was no way a single
regex was going to cover this, so I broke it into stages where each stage
only has to be good at one thing.

## Results

Across 834 non-empty remarks:

| Confidence | Rows | %     |
|-----------:|-----:|------:|
| **0.95**   | 691  | **82.9%** |
| 0.85       |   9  |  1.1% |
| 0.80       |   9  |  1.1% |
| 0.75       |  29  |  3.5% |
| 0.70       |  24  |  2.9% |
| 0.60       |   7  |  0.8% |
| 0.50       |  10  |  1.2% |
| 0.00       |  55  |  6.6% |

Average confidence: **0.862**.

That's after iterating from a much rougher first cut: the original parser
landed **71.1%** at 0.95 with **9.0%** completely unparsed (0.0). The current
version is at 82.9% / 6.6% — a **+11.8 percentage-point gain** in fully-parsed
rows (+98 rows promoted), and a **−2.4 pp drop** in completely-unparsed rows
(−20 rows recovered). The remaining ~17% breaks down as roughly:

- ~7% genuinely incomplete remarks (`till further notice`, `Maintenance fender
  chains POC ...`, single-time observations) — no parse fix can help these
- ~10% structural edge cases (dates with internal whitespace like `2/ 9`,
  multi-day ranges without a connector word, malformed phone-number-adjacent
  digit runs)

## How it works (and why)

### 1. Clean the text first

Before trying to find any numbers, the parser throws out the obvious junk.
Lots of remarks tack on a "cancel closure per John Doe 1630 05-21" or
"dock open per X" at the end, and if you don't strip that, you'll grab
*those* numbers instead of the real closure window.

So clauses that start with noise words (`cancelled`, `fn`, `dock open`,
`all clear`, `reopened`, etc.) get dropped, and trailing noise after the real
info gets chopped off.

Important catch: if *every* clause looks like noise, keep them all. Some
signal is better than nothing.

### 2. Pull out tokens with positions

Grab every time and every day from the cleaned text and remember where each
one sits in the string. Position matters because pairing depends on reading
order later.

Time can be `0700`, `07:00`, `7:00 pm`, `8am`, or `2400` (midnight end-of-day).
Five forms show up in the data, so the time regex has three alternatives plus
a `(?!\d)` trailing guard so `1800hrs` parses but `12345` doesn't pretend to
be `12:34`. Days can come from `05-22`, `5/22`, `29th`, or `May 28[, 2024]`.

Two boring-but-important guards on the token extractor:

- **Phone-number guard**: a regex up front detects `XXX-XXX-XXXX` style numbers
  (e.g. `555-555-1234`) and marks those spans as consumed *before* time/date
  matching runs. Without this, a phone like `555-555-1234` would get its
  trailing `1234` parsed as time `12:34`. This alone fixed several rows that
  had been silently mangled.
- **Ordinal-day anchor**: ordinals like `2nd` are only kept if there's a
  month, date, time, or another ordinal day within ~30 characters to anchor
  them. That kills `2nd POC notified` from becoming day 2 without losing the
  legit `May 28th` / `27th-28th` cases.

### 3. Pair them into start/end

Two strategies, and the parser picks based on what it sees:

- **Connector-based** - if there's a `thru` / `through` / `to` / `till` /
  `until` with tokens on both sides, split there. Whatever's on the left is
  the start, whatever's on the right is the end. This is the most reliable
  signal when it's available. The connector regex allows the connector to
  attach directly after a digit (e.g. `0700to 1700` works) since the data
  has plenty of typos with missing spaces.
- **Count-based fallback** - when there's no connector, pair by how many
  days and times we found. `2 days + 2 times` → first pair is start, last
  pair is end. `2 days + 1 time` → the lone time goes to whichever day it's
  closer to in the text. `1 day + 2 times` → both times share the day. And
  so on for the smaller cases.

There's also a special case for `0700-1700 daylight hours 05-22 thru 05-24`
where the time range is fully on the left of the connector, both times get
spread across the two days instead of pairing the second time with the
second day. The symmetric case (`0700 to 1700 daily 5/12 thru 5/16`) is
handled too — when left has a time range and right has a day range, the
times bracket the day range.

### 4. Same-day propagation (and why it has guards)

A common pattern: `Closed June 20th 2019 from 0700 until 2000`. The connector
split gives a start with a day (`D20`) and an end with just a time (`20:00`).
That's clearly a same-day closure. So after pairing, if exactly one side is
missing a day and both times are present, the known day gets copied across.

But naïve propagation breaks on multi-day ranges. Two guards:

- Don't propagate if the populated side has more than one day token (it's a
  date range, not a same-day closure).
- Don't propagate if the empty side contains a date-like pattern that just
  failed to parse (e.g. `2/ 9` with whitespace inside — a real date, just
  not in our regex).

This pair of guards moved 60+ rows from 0.85 to 0.95 without introducing
single-day collapses on real ranges.

### 5. Score it

Confidence is just "how many of the four fields did we get?" — start day,
start time, end day, end time. Starts at 0.4, each filled field adds a bit:

- **0.95** - all four fields filled. Almost always right.
- **0.85** - three of four. Usually still correct, just missing one piece.
- **0.7**  - two of four. Day-only, time-only, or partial entries; worth a
  human eyeball.

This isn't a probabilistic model — just a "more info = more trust"
heuristic. Good enough to sort the output by and triage from.

### 6. Write the result

Output is `docks_parsed.csv` with `remark_start`, `remark_end`, and
`parse_confidence` next to the original `remarks` column. Newlines inside
remarks get collapsed so each row stays on one line in the CSV.

The CLI also prints a confidence histogram and the average confidence at the
end of every run, so regressions in future tweaks are obvious immediately.

## How I built it

Iteratively. Run it, sort by confidence, look at the low-confidence rows and
the weird high-confidence ones, figure out what tripped it up, tweak a regex
or add a special case, run it again. Most of the noise-prefix words, the
ordinal-day anchor, the phone-number guard, and the propagation guards came
out of looking at rows that confidently parsed the *wrong* numbers.

A few of the bigger lifts along the way:

- **Phone-number guard + 4-digit time `(?!\d)` boundary**: cleaned up rows
  where digit runs in phone numbers (e.g. `555-555-1234`) or compound tokens
  (`1800hrs`) had been silently misread or dropped.
- **Month-name + day extraction** (`May 28`, `July 13-14th`, `April 8 -11`):
  picked up dozens of rows where the date was written longhand instead of
  numerically.
- **`2400` as a valid time**: end-of-day timestamps stopped being silently
  dropped.
- **Combined time-range + day-range pairing** (`0700 to 1700 daily 5/12 thru
  5/16`): connector logic now prefers connectors with day tokens on both
  sides, picking the day-range as the spine and applying the time-range
  across it.
- **Same-day propagation with guards**: closed the gap between "found
  three of four fields" and "found all four" without breaking actual ranges.

## Usage

```
pip install pandas
python parse.py
```

Reads `input.csv` from the current directory,
writes `output.csv` next to it. Prints the confidence histogram and
average score at the end in the CLI.

## Files

- `parse.py` - the parser
- `input.csv` - input
- `output.csv` - output

## Why

I know this is a simple project and not really worth making open source or
public but hopefully someone finds this useful!

> Note: Developed and documented by humans with the aid of AI-powered systems; all code reviewed and tested by the author (a human).
