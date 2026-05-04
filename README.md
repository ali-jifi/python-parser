# Dock Closure Remark Parser

Pulls start/end times and dates out of the messy `remarks` column in our dock
closure CSV so we have something usable for downstream analysis instead of a
bunch of free-text notes.

## Why this exists

The remarks are written by hand by whoever is on shift, and everyone writes
them differently. Same idea, dozens of formats:

- `0700-1700 daylight hours   05-22 thru 05-24`
- `May 21st 0500 until further notice per john doe`
- `Closed Wednesday ,May 28, at 0700 hours through 1900 hours`
- `0700 29th - 1700 1st`
- `closed 7/10 0700 thru 7/13 2330`

I needed start/end pairs out of all of them, and there was no way a single
regex was going to cover this, so I broke it into stages where each stage
only has to be good at one thing.

## How it works (and why)

### 1. Clean the text first

Before trying to find any numbers, the parser throws out the obvious junk.
Lots of remarks tack on a "cancel closure per John Doe 1630 05-21" or
"dock open per X" at the end, and if you don't strip that, you'll grab
*those* numbers instead of the real closure window.

So clauses that start with noise words (`cancelled`, `fn`, `dock open`,
`all clear`, `reopened`, etc.) get dropped, and trailing noise after the real
info gets chopped off. 

Important catch: if *every* clause looks like noise,
keep them all. Some signal is better than nothing.

### 2. Pull out tokens with positions

I grab every time and every day from the cleaned text and remember where each
one sits in the string. Position matters because pairing depends on reading
order later.

Time can be `0700`, `07:00`, `7:00 pm`, or `8am`, all four show up in the
data, so the time regex has three alternatives to cover them. Days can be
`05-22`, `5/22`, or `29th`.

The tricky one is ordinal days like `2nd`. If you accept every `\d+(st|nd|rd|th)`
you'll happily turn "2nd POC notified" into day 2. So ordinals are only kept
if there's a month, date, time, or another ordinal day within ~30 characters
to anchor them. That kills a ton of false positives without losing the legit
"May 28th" / "27th-28th" cases.

### 3. Pair them into start/end

Two strategies, and the parser picks based on what it sees:

- **Connector-based** - if there's a `thru` / `through` / `to` / `till` /
  `until` with tokens on both sides, split there. Whatever's on the left is
  the start, whatever's on the right is the end. This is the most reliable
  signal when it's available.
- **Count-based fallback** - when there's no connector, pair by how many
  days and times we found. `2 days + 2 times` → first pair is start, last
  pair is end. `2 days + 1 time` → the lone time goes to whichever day it's
  closer to in the text. `1 day + 2 times` → both times share the day. And
  so on for the smaller cases.

There's also a special case for `0700-1700 daylight hours 05-22 thru 05-24`
where the time range is fully on the left of the connector, both times get
spread across the two days instead of pairing the second time with the
second day.

### 4. Score it

Confidence is just "how many of the four fields did we get?", start day,
start time, end day, end time. Starts at 0.4, each filled field adds a bit:

- **0.95** - all four fields filled. Almost always right.
- **0.85** - three of four. Usually still correct, just missing one piece.
- **0.7** - two of four. Day-only, time-only, or partial entries; worth a
  human eyeball.

This isn't a probabilistic model, it's just a "more info = more trust"
heuristic. Good enough to sort the output by and triage from.

### 5. Write the result

Output is `docks_parsed.csv` with `remark_start`, `remark_end`, and
`parse_confidence` next to the original `remarks` column. Newlines inside
remarks get collapsed so each row stays on one line in the CSV.

## How I built it

Iteratively. Run it, sort by confidence, look at the low-confidence rows and
the weird high-confidence ones, figure out what tripped it up, tweak a regex
or add a special case, run it again. Most of the noise-prefix words and the
ordinal-day anchor logic came out of looking at rows that confidently parsed
the *wrong* numbers.

## Usage

```
pip install pandas
python parse.py
```

Reads `dock closure history (vessel close).csv` from the current directory,
writes `docks_parsed.csv` next to it.

## Files

- `parse.py` - the parser
- `dock closure history (vessel close).csv` - input
- `docks_parsed.csv` - output

## Why

I know this is a simple project and not really worth making open source or public but hopefully someone finds this useful!