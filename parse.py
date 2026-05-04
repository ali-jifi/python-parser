import pandas as pd
import re

# ---- HOW IT WORKS ----
# 1. split the remark into clauses, drop ones that start with admin noise like "cancelled..." or "dock open per Y", and trim trailing noise off the survivors
# 2. pull out every time and every day in the cleaned text along with their character positions; ordinal days like "2nd" are only kept if anchored by a nearby month/date/time/sibling-day so "2nd POC" doesn't become day 2
# 3. pair start/end days and times by splitting on connector words like "thru"/"to"/"until" when present, otherwise pair tokens by count and text order
# 4. assign a confidence score based on how many of the four fields (start day, start time, end day, end time) got filled, and format output as "D22 07:00", "D22", or "07:00" depending on what was available
# 5. apply the parser to every row and write the results to a new csv
# 6. (not shown) manually review the results, especially low-confidence ones, to identify common failure modes and refine the regexes/logic as needed
# 7. iterate until satisfied with results

# scoring 
# 0.95 - all four fields filled (start day + start time + end day + end time), essentially always right when the cleaning logic isn't fooled
# 0.85 - three of four fields, with one of the days missing (a time was extracted but couldn't be tied to a date), usually still correct, just incomplete
# 0.7 - two of four fields, often "day only" or "time only" with no closing bracket, rows where the parser saw something but couldn't form a real window; cancellation notes, partial entries, or single-time observations

# load source csv and drop any rows where 'remarks' is missing or null
df = pd.read_csv("input.csv")
df = df[df["remarks"].notna() & df["remarks"].astype(str).str.strip().ne("")].reset_index(drop=True)

# ---- regex building blocks ----
# "time" can be written as 0700, 07:00, 7:00, "7:00 PM", or "8am"/"12pm"
# three alts: HH:MM (with optional am/pm), HHMM, or bare hour with am/pm
TIME_RE = re.compile(
    r"\b(?:"
    r"(?P<h_full>[01]?\d|2[0-3]):(?P<mn_full>[0-5]\d)\s*(?P<ampm_full>am|pm)?"
    r"|(?P<h_mil>[01]\d|2[0-4])(?P<mn_mil>[0-5]\d)"
    r"|(?P<h_bare>1[0-2]|[1-9])\s*(?P<ampm_bare>am|pm)"
    r")(?!\d)",
    re.IGNORECASE,
)

# phone numbers must be detected so their digit segments aren't read as times
PHONE_RE = re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b|\(\d{3}\)\s*\d{3}[-.\s]?\d{4}")

# "date" like 05-22 or 5/22 (year is optional and ignored)
# extract only the DAY component
DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-]\d{2,4})?\b")

# bare day with an ordinal suffix: 29th, 1st, 22nd, 3rd
DAY_RE  = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)

# month names, used to "anchor" ordinal days like "May 28th"
_MONTH_INNER = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
MONTH_RE = re.compile(rf"\b(?:{_MONTH_INNER})\b", re.IGNORECASE)
MONTH_DAY_RE = re.compile(
    rf"\b(?:{_MONTH_INNER})\.?\s+(?P<d1>\d{{1,2}})(?:\s*(?:st|nd|rd|th))?"
    rf"(?:\s*(?:[-–]|to|thru|through)\s*(?P<d2>\d{{1,2}})(?:\s*(?:st|nd|rd|th))?)?"
    rf"(?:\s*,?\s*(?:19|20)\d{{2}})?",
    re.IGNORECASE,
)

# words that link a start to an end: "5/22 thru 5/24", "0700 to 1700"
CONNECTOR_RE = re.compile(r"(?:(?<=\d)|\b)(?:thru|through|to|till|until)\b", re.IGNORECASE)

# loose date pattern (allows whitespace around separator, e.g. "2/ 9")
# used to detect an unparsed date hint on the empty side of a connector,
# so we don't propagate a same-day guess across what's actually a range
LOOSE_DATE_RE = re.compile(r"\b\d{1,2}\s*[/-]\s*\d{1,2}\b")

# clauses that start with these words are usually admin noise, not closure info
# stuff like "cancelled X", "finished @ 1545", "dock open per Y"
NOISE_PREFIX_RE = re.compile(
    r"^\s*(?:cancel(?:led|lation)?\b|fin(?:ished)?\b|fn\b|all\s+clear\b|"
    r"dock\s+(?:open|clear|reopen)\b|reopen(?:ed)?\b|released?\b|ok\s+for\b|"
    r"open(?:ed)?\s+(?:@|per|by|at)\b|clear(?:ed)?\s+(?:@|per|by|at)\b)",
    re.IGNORECASE,
)

# same idea but for noise  AFTER the real closure info,
# e.g. "...05-22 thru 05-24 Cancel closure per John Doe 1630 05-21".
TRAILING_NOISE_RE = re.compile(
    r"\s+(?:cancel(?:led|lation)?\b|fn\b|fin(?:ished)?\b|all\s+clear\b|"
    r"dock\s+(?:open|clear|reopen)\b|reopen(?:ed)?\b|released?\s+the\s+dock\b)",
    re.IGNORECASE,
)

HAS_DIGIT_RE = re.compile(r"\d")



# ---- step 1: clean the remark text before we try to extract numbers ----
def clean_remark(text):
    # split on sentence breaks and also on boundary right before "closed"/"closure" to isolate actual closure clause from preceding cancellation notes
    clauses = [c.strip().rstrip(".,") for c in re.split(r"\.\s+|\s+(?=closed\b|closure\b)", text, flags=re.IGNORECASE)]
    clauses = [c for c in clauses if c]
    if not clauses:
        return ""

    # for each clause, record whether it has any digits (i.e. usable info) and whether it starts with a noise word
    classified = [(c, bool(HAS_DIGIT_RE.search(c)), bool(NOISE_PREFIX_RE.match(c))) for c in clauses]

    # if at least one non-noise clause carries digits, throw the noise clauses away, otherwise keep everything, better some signal than none
    has_clean_tokens = any(has and not noise for _, has, noise in classified)
    chosen = [c for c, _, noise in classified if not noise] if has_clean_tokens else clauses

    # Within each surviving clause, lop off any trailing noise tail.
    out = []
    for c in chosen:
        m = TRAILING_NOISE_RE.search(c)
        if m and HAS_DIGIT_RE.search(c[:m.start()]):
            c = c[:m.start()].rstrip()
        if c:
            out.append(c)
    return " ".join(out)


# ---- step 2: pull every time and every day out of the cleaned text ----
def extract_tokens(text):
    # each token is (kind, position, value), kind is "d" for day or "t" for time
    # track used spans so "8/12th" isn't read as both a date and an ordinal day
    tokens = []
    used = []

    # block phone-number digit runs from being read as times/dates
    for m in PHONE_RE.finditer(text):
        used.append((m.start(), m.end()))

    def overlaps(s, e):
        return any(us < e and ue > s for us, ue in used)

    # anchors for ordinal-day filtering: months, dates, times all count
    # this stops "2nd POC" from being treated as day 2
    anchor_spans = []
    for m in MONTH_RE.finditer(text):
        anchor_spans.append((m.start(), m.end()))
    for m in MONTH_DAY_RE.finditer(text):
        anchor_spans.append((m.start(), m.end()))
    for m in DATE_RE.finditer(text):
        anchor_spans.append((m.start(), m.end()))
    for m in TIME_RE.finditer(text):
        anchor_spans.append((m.start(), m.end()))
    day_starts = [m.start() for m in DAY_RE.finditer(text)]

    def day_anchored(pos, window=30):
        # near a date/month/time?
        for s, e in anchor_spans:
            if s - window <= pos <= e + window:
                return True
        # near another ordinal day? (so "27th-28th" self-validates)
        for d in day_starts:
            if d != pos and abs(d - pos) <= window:
                return True
        return False

    # month-name + day: "May 28", "May 28th", "May 28 thru 30", "May 28-30, 2025"
    for m in MONTH_DAY_RE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        d1 = int(m.group("d1"))
        if not (1 <= d1 <= 31):
            continue
        tokens.append(("D", m.start(), d1))
        used.append((m.start(), m.end()))
        d2_str = m.group("d2")
        if d2_str is not None:
            d2 = int(d2_str)
            if 1 <= d2 <= 31:
                tokens.append(("D", m.start("d2"), d2))

    # dates first: 05-22 → day 22 (mm/dd or dd/mm based on which side is a valid month)
    for m in DATE_RE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= 12 and 1 <= b <= 31:
            day = b
        elif 1 <= b <= 12 and 1 <= a <= 31:
            day = a
        else:
            continue
        tokens.append(("D", m.start(), day))
        used.append((m.start(), m.end()))

    # ordinal-suffix days: 29th, 1st (only if anchored by a real date/month/time/sibling)
    for m in DAY_RE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        if not day_anchored(m.start()):
            continue
        d = int(m.group(1))
        if 1 <= d <= 31:
            tokens.append(("D", m.start(), d))
            used.append((m.start(), m.end()))

    # times: 0700, 07:00, 7:00 pm, or 8am/12pm
    for m in TIME_RE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        if m.group("h_full") is not None:
            h = int(m.group("h_full"))
            mn = int(m.group("mn_full"))
            ampm = (m.group("ampm_full") or "").lower()
        elif m.group("h_mil") is not None:
            h = int(m.group("h_mil"))
            mn = int(m.group("mn_mil"))
            if h == 24 and mn != 0:
                continue
            ampm = ""
        else:
            h = int(m.group("h_bare"))
            mn = 0
            ampm = m.group("ampm_bare").lower()
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        tokens.append(("T", m.start(), (h, mn)))
        used.append((m.start(), m.end()))

    # sort by position so token order matches reading order
    tokens.sort(key=lambda x: x[1])
    return tokens


# ---- step 3a: pair tokens into (start, end) when there is no connector word ----
def pair_tokens(tokens):
    days = [t for t in tokens if t[0] == "D"]
    times = [t for t in tokens if t[0] == "T"]
    n_d, n_t = len(days), len(times)

    if n_d == 0 and n_t == 0:
        return None, None, None, None

    # 2+ of each: assume "d1 t1 ... d2 t2" or "t1-t2 d1-d2", pair by text order
    if n_d >= 2 and n_t >= 2:
        return days[0][2], times[0][2], days[-1][2], times[-1][2]

    # 2 days, 1 time ("closed 5/12 to 5/14 1900"), give the lone time to whichever day it sits closer to
    if n_d == 2 and n_t == 1:
        t = times[0]
        if abs(t[1] - days[0][1]) <= abs(t[1] - days[1][1]):
            return days[0][2], t[2], days[1][2], None
        return days[0][2], None, days[1][2], t[2]

    # 1 day, 2 times ("closed 11/6 0730-1200"), same day for both
    if n_d == 1 and n_t == 2:
        d = days[0][2]
        return d, times[0][2], d, times[1][2]

    # everything else, best-effort fill
    if n_d == 1 and n_t == 1:
        return days[0][2], times[0][2], None, None
    if n_d == 1 and n_t == 0:
        return days[0][2], None, None, None
    if n_d == 0 and n_t == 1:
        return None, times[0][2], None, None
    if n_d == 0 and n_t >= 2:
        return None, times[0][2], None, times[-1][2]
    if n_d >= 2 and n_t == 0:
        return days[0][2], None, days[-1][2], None
    return None, None, None, None


# ---- step 3b: pair tokens when we found a connector (thru/to/till/until) ----
def split_pair(left, right):
    # tokens on the left side belong to start, tokens on the right belong to end
    l = extract_tokens(left)
    r = extract_tokens(right)
    l_days = [t for t in l if t[0] == "D"]
    l_times = [t for t in l if t[0] == "T"]
    r_days = [t for t in r if t[0] == "D"]
    r_times = [t for t in r if t[0] == "T"]

    # take the last day/time on the left (closest to connector) and first on the right
    sd = l_days[-1][2] if l_days else None
    st = l_times[-1][2] if l_times else None
    ed = r_days[0][2] if r_days else None
    et = r_times[0][2] if r_times else None

    # special case: "0700-1700 daylight hours 05-22 thru 05-24"
    # left has a time range, right has only the second day, so the range spans both days
    if len(l_times) >= 2 and not r_times:
        st = l_times[0][2]
        et = l_times[-1][2]

    # symmetric case: "0700 to 1700 daily 5/12 thru 5/16"
    # left has time range, right has day range; pair both
    if len(l_times) >= 2 and len(r_days) >= 2 and not r_times:
        sd = r_days[0][2]
        st = l_times[0][2]
        ed = r_days[-1][2]
        et = l_times[-1][2]

    # same-day propagation: only one day was given but both times are present,
    # so the closure is same-day; copy the known day into the empty slot.
    # guards: don't propagate if the empty side has an unparsed date hint
    # (suggests a real range we missed) or if the populated side has multiple days
    if st is not None and et is not None:
        if sd is None and ed is not None and len(r_days) == 1 and not LOOSE_DATE_RE.search(left):
            sd = ed
        elif ed is None and sd is not None and len(l_days) == 1 and not LOOSE_DATE_RE.search(right):
            ed = sd

    return sd, st, ed, et


# ---- step 4: top-level function applied to every row's remark ----
def parse_remark(remark):
    if not isinstance(remark, str) or not remark.strip():
        return None, None, 0.0

    cleaned = clean_remark(remark)
    if not cleaned:
        return None, None, 0.0

    sd = st = ed = et = None
    matched = False

    # prefer connector-based split: prefer a connector with day tokens on BOTH sides;
    # fall back to any connector with tokens on both sides
    day_bracketed = None
    any_bracketed = None
    for c in CONNECTOR_RE.finditer(cleaned):
        left = cleaned[:c.start()]
        right = cleaned[c.end():]
        l_toks = extract_tokens(left)
        r_toks = extract_tokens(right)
        if not (l_toks and r_toks):
            continue
        if any_bracketed is None:
            any_bracketed = (left, right)
        l_has_day = any(t[0] == "D" for t in l_toks)
        r_has_day = any(t[0] == "D" for t in r_toks)
        if l_has_day and r_has_day and day_bracketed is None:
            day_bracketed = (left, right)
            break

    chosen = day_bracketed if day_bracketed is not None else any_bracketed
    if chosen is not None:
        sd, st, ed, et = split_pair(chosen[0], chosen[1])
        matched = True

    # no usable connector, fall back to count-based pairing
    if not matched:
        sd, st, ed, et = pair_tokens(extract_tokens(cleaned))

    if sd is None and st is None and ed is None and et is None:
        return None, None, 0.0

    # confidence: more fields filled in means higher score
    confidence = 0.4
    if st is not None: confidence += 0.2
    if et is not None: confidence += 0.15
    if sd is not None: confidence += 0.1
    if ed is not None: confidence += 0.1

    # format helper, output looks like "d22 07:00", "07:00", or "d22"
    def fmt(d, t):
        if d is not None and t is not None:
            return f"D{d:02d} {t[0]:02d}:{t[1]:02d}"
        if t is not None:
            return f"{t[0]:02d}:{t[1]:02d}"
        if d is not None:
            return f"D{d:02d}"
        return None

    s, e = fmt(sd, st), fmt(ed, et)
    if s is None and e is None:
        return None, None, 0.0
    return s, e, round(min(confidence, 0.99), 2)


# ---- run the parser over every row and write the result ----
results = df["remarks"].apply(parse_remark)
df["remark_start"] = [r[0] for r in results]
df["remark_end"] = [r[1] for r in results]
df["parse_confidence"] = [r[2] for r in results]

# keep only the columns that matter, confidence last
OUT_COLS = ["visit_number", "status_type_name", "remark_start", "remark_end", "remarks", "parse_confidence"]
out = df[[c for c in OUT_COLS if c in df.columns]].copy()

# collapse newlines/extra whitespace inside remarks so each row is one line
if "remarks" in out.columns:
    out["remarks"] = out["remarks"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()

out.to_csv("output.csv", index=False)

# print confirmation message
print(f"parsing complete, rows {len(out)}, results written to docks_parsed.csv")

# confidence distribution + average score
counts = out["parse_confidence"].value_counts().sort_index(ascending=False)
total = len(out)
max_count = counts.max() if len(counts) else 1
bar_width = 30
print("\nconfidence distribution:")
for score, n in counts.items():
    bar = "#" * max(1, round(n / max_count * bar_width))
    pct = n / total * 100
    print(f"  {score:>4}  {bar:<{bar_width}}  {n:>4}  ({pct:>5.1f}%)")
print(f"\naverage confidence: {out['parse_confidence'].mean():.3f}")
