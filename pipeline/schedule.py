"""Read the shared puzzle schedule workbook (content/Froggle Schedule.xlsx).

Sheets:
  Days   one row per date: Date | # | Status | Bank 1..Bank 6 | Author | Notes
  Words  one row per curated word: Date | Crossing | (Pair) | Word | Type | Bonus | Position | Note | Added by

Word types:
  Perfect link  pinned to the middle of the river (crosses in one hop), always gold,
                and used to calibrate the midpoint for the whole crossing
  Extra word    accept/adjust a word: give it a Position (0 = start bank, 1 = far bank)
                or just a Bonus. Use it for words missing from the dictionary.
  Banned        never grows a lily pad
Any word with a Bonus is a fly.
"""
import datetime as dt
import shutil
import tempfile
from pathlib import Path

from openpyxl import load_workbook

SCHEDULE = Path(__file__).resolve().parent.parent / "content" / "Froggle Schedule.xlsx"
TYPES = {"perfect link": "perfect", "perfect": "perfect", "extra word": "extra", "extra": "extra",
         "": "extra", "fly": "extra", "bonus": "extra", "banned": "ban", "ban": "ban"}
BANK_COLS = [f"Bank {i}" for i in range(1, 7)]


def _norm(s) -> str:
    return " ".join(str(s).lower().strip().split()) if s is not None else ""


def _as_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str) and v.strip():
        return dt.date.fromisoformat(v.strip())
    return None


def _rows(ws):
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(it)]
    for n, row in enumerate(it, start=2):
        yield n, dict(zip(header, row))


def read_schedule(path: Path = SCHEDULE):
    """Return (days, problems).

    days: {"YYYY-MM-DD": {"status": "ready"|"draft", "author", "rounds": [...]}}
          where each round is {"start", "end", "calibrate", "answers", "banned"}
    problems: list of human-readable strings ("Days row 7: ...")
    """
    # Copy first: Excel can hold a lock on the synced file while it's open
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "schedule.xlsx"
        shutil.copyfile(path, copy)
        wb = load_workbook(copy, data_only=True, read_only=True)
        days, problems = {}, []

        for n, r in _rows(wb["Days"]):
            try:
                date = _as_date(r.get("Date"))
            except ValueError:
                problems.append(f"Days row {n}: date {r.get('Date')!r} isn't a valid date")
                continue
            banks = [str(r.get(c)).strip() if r.get(c) not in (None, "") else "" for c in BANK_COLS]
            if not date or not any(banks):
                continue  # empty or not yet filled in
            filled = banks[: max(i for i, b in enumerate(banks) if b) + 1]
            if "" in filled:
                problems.append(f"Days row {n} ({date}): bank words have a gap. Fill banks left to right.")
                continue
            if len(filled) < 2:
                problems.append(f"Days row {n} ({date}): needs at least 2 bank words")
                continue
            if len({_norm(b) for b in filled}) < len(filled):
                problems.append(f"Days row {n} ({date}): the same bank word appears twice")
            key = date.isoformat()
            if key in days:
                problems.append(f"Days row {n}: {key} is listed twice; using the first row")
                continue
            days[key] = {
                "status": "ready" if _norm(r.get("Status")) == "ready" else "draft",
                "author": r.get("Author") or "",
                "rounds": [{"start": a, "end": b, "calibrate": [], "answers": {}, "banned": []}
                           for a, b in zip(filled, filled[1:])],
            }

        for n, r in _rows(wb["Words"]):
            word = _norm(r.get("Word"))
            if not word:
                continue
            try:
                date = _as_date(r.get("Date"))
            except ValueError:
                date = None
            if not date or date.isoformat() not in days:
                shown = date.isoformat() if date else (r.get("Date") or "(blank)")
                problems.append(f"Words row {n} ({word}): {shown} has no bank words on the Days sheet")
                continue
            day = days[date.isoformat()]
            try:
                ci = int(r.get("Crossing"))
                rnd = day["rounds"][ci - 1]
                assert ci >= 1
            except (TypeError, ValueError, IndexError, AssertionError):
                problems.append(f"Words row {n} ({word}): crossing {r.get('Crossing')!r} doesn't exist on {date} "
                                f"(1–{len(day['rounds'])})")
                continue
            kind = TYPES.get(_norm(r.get("Type")))
            if kind is None:
                problems.append(f"Words row {n} ({word}): unknown type {r.get('Type')!r}")
                continue
            banks = [_norm(rnd["start"]), _norm(rnd["end"])]
            if any(b in word or (len(word) >= 3 and word in b) for b in banks):
                problems.append(f"Words row {n} ({word}): contains a bank word, so players can't use it")
                continue
            if kind == "ban":
                rnd["banned"].append(word)
                continue
            spec = {}
            for col, key, lo, hi in (("Bonus", "bonus", 0, 50), ("Position", "position", 0, 1)):
                v = r.get(col)
                if v in (None, ""):
                    continue
                try:
                    v = float(v)
                    assert lo <= v <= hi
                except (TypeError, ValueError, AssertionError):
                    problems.append(f"Words row {n} ({word}): {col} must be a number from {lo} to {hi}")
                    continue
                spec[key] = int(v) if key == "bonus" else v
            if r.get("Note"):
                spec["note"] = str(r["Note"]).strip()
            if kind == "perfect":
                spec.update(position=0.5, quality=1.0)
                rnd["calibrate"].append(word)
            if word in rnd["answers"]:
                problems.append(f"Words row {n} ({word}): listed twice for this crossing; later row wins")
            rnd["answers"][word] = spec
        wb.close()
    return days, problems
