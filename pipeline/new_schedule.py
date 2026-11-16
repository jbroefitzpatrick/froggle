"""Create the shared schedule workbook: content/Froggle Schedule.xlsx

    py pipeline/new_schedule.py [--days 180]

Refuses to overwrite an existing workbook. If content/days/*.json files exist
(the old format), their puzzles are copied in.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from schedule import SCHEDULE

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "content" / "settings.json"
OLD_DAYS = ROOT / "content" / "days"

GREEN, PALE, YELLOW, GREY = "1E7B45", "E8F5EC", "FFF4C2", "EEEEEE"
HEAD = dict(font=Font(bold=True, color="FFFFFF"), fill=PatternFill("solid", fgColor=GREEN),
            alignment=Alignment(vertical="center"))
WORD_ROWS = 2000

HOW_TO = [
    ("Froggle puzzle schedule", "title"),
    ("Edit this workbook together (OneDrive / Excel for the web). The build reads it directly.", None),
    ("", None),
    ("1. Days sheet: one row per date", "h"),
    ("Fill Bank 1 to Bank 6 left to right. Bank 1 → Bank 2 is crossing 1, Bank 2 → Bank 3 is crossing 2, and so on.", None),
    ("Fewer banks = fewer crossings (minimum 2 banks). Leave a row empty if the day isn't planned yet.", None),
    ("Status: Draft while you're working on it, Ready when it should go live. Only Ready days are served as the daily puzzle.", None),
    ("", None),
    ("2. Words sheet: one row per curated word", "h"),
    ("Date + Crossing (1–5) say which crossing the word belongs to. The Pair column fills itself in so you can check.", None),
    ("Type:", None),
    ("   Perfect link: crosses in one hop, always gold. The whole crossing is calibrated around these words.", None),
    ("   Extra word: accept or adjust a word. Use Position (0 = start bank … 0.5 = middle … 1 = far bank),", None),
    ("        e.g. to add a word the dictionary is missing, or move one the model got wrong.", None),
    ("   Banned: never grows a lily pad (obscure, off-topic or unfair words).", None),
    ("Bonus: points for eating the fly. Any word with a Bonus is a fly. Aim for 1–2 flies per crossing.", None),
    ("        If a crossing has no fly, the build puts one on its strongest perfect link automatically.", None),
    ("Words can't contain a bank word (e.g. 'sandcastle' when a bank is 'sand'). The build will warn you.", None),
    ("", None),
    ("3. Build", "h"),
    ("Run:  py pipeline/build.py   It reports any mistakes in this workbook by sheet and row,", None),
    ("and writes pipeline/reports/<date>.md showing each crossing's perfect links, tiers and flies.", None),
    ("Days whose date has passed are frozen and never rebuilt, so published scores stay fair.", None),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180, help="how many dated rows to pre-fill")
    args = ap.parse_args()
    if SCHEDULE.exists():
        sys.exit(f"{SCHEDULE} already exists; not overwriting.")

    settings = json.loads(SETTINGS.read_text(encoding="utf8"))
    launch = dt.date.fromisoformat(settings["launch_date"])
    old = {p.stem: json.loads(p.read_text(encoding="utf8")) for p in sorted(OLD_DAYS.glob("*.json"))}

    wb = Workbook()
    # ---------------------------------------------------------------- How to use
    ws = wb.active
    ws.title = "How to use"
    ws.column_dimensions["A"].width = 120
    for i, (text, style) in enumerate(HOW_TO, start=1):
        c = ws.cell(i, 1, text)
        if style == "title":
            c.font = Font(bold=True, size=16, color=GREEN)
        elif style == "h":
            c.font = Font(bold=True, size=12, color=GREEN)
    ws.sheet_view.showGridLines = False

    # ---------------------------------------------------------------- Days
    days = wb.create_sheet("Days")
    cols = ["Date", "#", "Status"] + [f"Bank {i}" for i in range(1, 7)] + ["Author", "Notes"]
    widths = [13, 6, 10] + [15] * 6 + [12, 40]
    days.append(cols)
    for i, w in enumerate(widths, start=1):
        days.column_dimensions[chr(64 + i)].width = w
    for c in days[1]:
        for k, v in HEAD.items():
            setattr(c, k, v)
    for i in range(args.days):
        d = launch + dt.timedelta(days=i)
        row = i + 2
        days.cell(row, 1, d).number_format = "ddd yyyy-mm-dd"
        days.cell(row, 2, f"=A{row}-DATE({launch.year},{launch.month},{launch.day})+1")
        if d.isoformat() in old:
            rounds = old[d.isoformat()]["rounds"]
            banks = [rounds[0]["start"]] + [r["end"] for r in rounds]
            for j, b in enumerate(banks[:6]):
                days.cell(row, 4 + j, b)
            days.cell(row, 3, "Ready")
    days.column_dimensions["A"].width = 16
    days.freeze_panes = "D2"
    days.auto_filter.ref = f"A1:K{args.days + 1}"
    dv = DataValidation(type="list", formula1='"Draft,Ready"', allow_blank=True)
    days.add_data_validation(dv)
    dv.add(f"C2:C{args.days + 400}")
    rng = f"A2:K{args.days + 400}"
    days.conditional_formatting.add(rng, FormulaRule(formula=['$C2="Ready"'], fill=PatternFill("solid", fgColor=PALE)))
    days.conditional_formatting.add(rng, FormulaRule(formula=['$C2="Draft"'], fill=PatternFill("solid", fgColor=YELLOW)))
    days.conditional_formatting.add(rng, FormulaRule(formula=['AND($A2<>"",$A2<TODAY())'], font=Font(color="999999")))

    # ---------------------------------------------------------------- Words
    words = wb.create_sheet("Words")
    cols = ["Date", "Crossing", "Pair", "Word", "Type", "Bonus", "Position", "Note", "Added by"]
    widths = [16, 10, 24, 18, 14, 8, 10, 36, 12]
    words.append(cols)
    for i, w in enumerate(widths, start=1):
        words.column_dimensions[chr(64 + i)].width = w
    for c in words[1]:
        for k, v in HEAD.items():
            setattr(c, k, v)

    entries = []
    for date, day in old.items():
        for ci, r in enumerate(day["rounds"], start=1):
            cal = set(r.get("calibrate", []))
            for w, s in r.get("answers", {}).items():
                perfect = w in cal
                kind = "Perfect link" if perfect else "Extra word"
                pos = None if perfect else s.get("position")
                entries.append((dt.date.fromisoformat(date), ci, w, kind, s.get("bonus"), pos, s.get("note")))
            for w in r.get("banned", []):
                entries.append((dt.date.fromisoformat(date), ci, w, "Banned", None, None, None))

    for row in range(2, WORD_ROWS + 2):
        words.cell(row, 1).number_format = "ddd yyyy-mm-dd"
        words.cell(row, 3, f'=IF(OR(A{row}="",B{row}=""),"",IFERROR(INDEX(Days!$D:$I,MATCH(A{row},Days!$A:$A,0),B{row})'
                           f'&" → "&INDEX(Days!$D:$I,MATCH(A{row},Days!$A:$A,0),B{row}+1),"⚠ no such day"))')
        words.cell(row, 3).font = Font(color="476B55", italic=True)
    for row, e in enumerate(entries, start=2):
        for col, v in zip((1, 2, 4, 5, 6, 7, 8), e):
            if v is not None:
                words.cell(row, col, v)
    words.freeze_panes = "A2"
    words.auto_filter.ref = f"A1:I{WORD_ROWS + 1}"
    for formula, ref, kw in (
        ('"Perfect link,Extra word,Banned"', "E", dict(type="list")),
        (None, "B", dict(type="whole", operator="between", formula1="1", formula2="5")),
        (None, "F", dict(type="whole", operator="between", formula1="0", formula2="50")),
        (None, "G", dict(type="decimal", operator="between", formula1="0", formula2="1")),
    ):
        dv = DataValidation(formula1=formula, allow_blank=True, **kw) if formula else DataValidation(allow_blank=True, **kw)
        dv.error = {"E": "Pick Perfect link, Extra word or Banned", "B": "Crossing is 1 to 5",
                    "F": "Bonus is a whole number 0–50", "G": "Position is 0 (start bank) to 1 (far bank)"}[ref]
        dv.showErrorMessage = True
        words.add_data_validation(dv)
        dv.add(f"{ref}2:{ref}{WORD_ROWS + 1}")
    dv = DataValidation(type="date", operator="greaterThan", formula1=f"DATE({launch.year},{launch.month},{launch.day})-1", allow_blank=True)
    dv.error, dv.showErrorMessage = "Enter a date, e.g. 2026-12-01", True
    words.add_data_validation(dv)
    dv.add(f"A2:A{WORD_ROWS + 1}")
    words.conditional_formatting.add(f"A2:I{WORD_ROWS + 1}",
                                     FormulaRule(formula=['$E2="Banned"'], font=Font(color="999999", strike=True)))
    words.conditional_formatting.add(f"A2:I{WORD_ROWS + 1}",
                                     FormulaRule(formula=['AND($F2<>"",$F2>0)'], fill=PatternFill("solid", fgColor=YELLOW)))

    wb.active = 1  # open on Days
    SCHEDULE.parent.mkdir(parents=True, exist_ok=True)
    wb.save(SCHEDULE)
    print(f"Created {SCHEDULE.relative_to(ROOT)} with {args.days} dated rows; imported {len(old)} day(s), {len(entries)} word(s).")


if __name__ == "__main__":
    main()
