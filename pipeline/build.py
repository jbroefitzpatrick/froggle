"""Froggle offline scoring pipeline.

Reads content/settings.json and the shared schedule workbook
(content/Froggle Schedule.xlsx), scores a vocabulary of common English words
against each crossing's two banks, and writes data/days/<date>.js,
data/vocab.js and data/index.js for the game to load.

    py pipeline/build.py                  # every changed day from today on, plus anything unpublished
    py pipeline/build.py --date 2026-11-20
    py pipeline/build.py --force          # also rebuild published past days (changes their scores!)

Unchanged days are skipped, so building months ahead stays quick.

Steps:
  1. Vocabulary: the most common English words (wordfreq) that ConceptNet
     Numberbatch also knows, minus stopwords. Curated answers are always added.
  2. Relatedness: cosine similarity to each bank from two models, z-scored so
     they're comparable, then blended:
       - ConceptNet Numberbatch: good at thematic association (frog -> prince)
       - Model2Vec: good at general meaning, and handles any phrase
  3. Placement by rank: each word's lean (toward start vs. end bank) is ranked,
     and the ranks are spread across the river so every puzzle has about
     `perfect_count` words in the perfect zone. Difficulty stays consistent
     whatever the raw model scores look like.
  4. Link quality: each word's relatedness is ranked against the words that
     land in the same band of the river (0 = weakest, 1 = strongest). This
     drives the score multiplier and the bronze/silver/gold tiers.
  5. Flies: curated bonus answers, or the strongest perfect link if none.
  6. Curated overrides from the Words sheet: fixed positions, bonus points, notes,
     calibration words that define the midpoint, and banned words.
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
from wordfreq import top_n_list

from schedule import SCHEDULE, read_schedule

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "pipeline" / "cache"
SETTINGS = ROOT / "content" / "settings.json"
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "pipeline" / "reports"
HASHES = DATA_DIR / "build_hashes.json"
NB_GZ = CACHE / "numberbatch-en-19.08.txt.gz"
NB_URL = "https://conceptnet.s3.amazonaws.com/downloads/2019/numberbatch/numberbatch-en-19.08.txt.gz"
NB_CACHE = CACHE / "nb_cache.npz"
NB_MISSING = CACHE / "nb_missing.json"

STOPWORDS = set("""
a about above after again against all also am an and any are aren as at be because been before
being below between both but by can cannot could did didn do does doesn doing don down during each
few for from further had hadn has hasn have haven having he her here hers herself him himself his how
i if in into is isn it its itself just let ll me more most mustn my myself no nor not now of off on
once only or other ought our ours ourselves out over own re same shan she should shouldn so some such
than that the their theirs them themselves then there these they this those through to too under
until up ve very was wasn we were weren what when where which while who whom why will with won would
wouldn you your yours yourself yourselves yes yeah okay ok its im youre dont thats cant ive
""".split())


def norm(term: str) -> str:
    return " ".join(term.lower().strip().split())


def nb_key(term: str) -> str:
    return norm(term).replace(" ", "_")


def uses_bank_word(word: str, banks: list[str]) -> bool:
    """Same rule the game enforces: no bank word inside the guess, or vice versa."""
    for b in banks:
        if b in word or (len(word) >= 3 and word in b):
            return True
    return False


# ---------------------------------------------------------------- Numberbatch
def load_numberbatch(terms: set[str]) -> dict[str, np.ndarray]:
    """Return {term: unit vector} for the terms Numberbatch knows, using a local cache."""
    cached: dict[str, np.ndarray] = {}
    if NB_CACHE.exists():
        z = np.load(NB_CACHE, allow_pickle=False)
        cached = dict(zip(z["terms"].tolist(), z["vecs"]))
    missing = set(json.loads(NB_MISSING.read_text())) if NB_MISSING.exists() else set()

    todo = {nb_key(t) for t in terms} - set(cached) - missing
    if todo:
        if not NB_GZ.exists():
            print(f"Downloading Numberbatch (~325 MB) to {NB_GZ} ...")
            CACHE.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(NB_URL, NB_GZ)
        print(f"Scanning Numberbatch for {len(todo)} uncached terms ...")
        t0 = time.time()
        with gzip.open(NB_GZ, "rt", encoding="utf8") as f:
            next(f)  # header: rows dims
            for line in f:
                key, _, rest = line.partition(" ")
                if key in todo:
                    v = np.array(rest.split(), dtype=np.float32)
                    cached[key] = v / (np.linalg.norm(v) or 1)
        missing |= todo - set(cached)
        np.savez(NB_CACHE, terms=np.array(list(cached)), vecs=np.stack(list(cached.values())))
        NB_MISSING.write_text(json.dumps(sorted(missing)))
        print(f"  done in {time.time() - t0:.0f}s")
    return {t: cached[nb_key(t)] for t in terms if nb_key(t) in cached}


# ------------------------------------------------------------------- Model2Vec
def load_model2vec(name: str):
    from model2vec import StaticModel
    return StaticModel.from_pretrained(name)


def encode(model, terms: list[str]) -> np.ndarray:
    v = model.encode(terms)
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


# ----------------------------------------------------------------- scoring
def zscore(sims: np.ndarray, ref: np.ndarray) -> np.ndarray:
    return (sims - ref.mean()) / (ref.std() or 1)


def term_matrices(terms, nb, m2v, nb_dims):
    """Model2Vec + Numberbatch vectors for a list of terms (computed once for the vocabulary)."""
    return {
        "m2v": encode(m2v, terms) if terms else np.zeros((0, encode(m2v, ["a"]).shape[1]), np.float32),
        "nb": (np.stack([nb.get(t, np.zeros(nb_dims, np.float32)) for t in terms])
               if terms else np.zeros((0, nb_dims), np.float32)),
        "has": np.array([t in nb for t in terms], dtype=bool),
    }


def relatedness(anchor, V, X, nb, m2v, weights):
    """Blended, z-scored relatedness of every term (vocabulary V, then extras X) to one bank word.

    The vocabulary is the reference distribution; extras are scored on the same scale.
    """
    a_m = encode(m2v, [anchor])[0]
    n_v = len(V["has"])
    m = np.concatenate([V["m2v"] @ a_m, X["m2v"] @ a_m])
    zm = zscore(m, m[:n_v])
    a_nb = nb.get(anchor)
    if a_nb is None:
        rel = zm
    else:
        n = np.concatenate([V["nb"] @ a_nb, X["nb"] @ a_nb])
        has = np.concatenate([V["has"], X["has"]])
        zn = zscore(n, n[:n_v][V["has"]])
        rel = np.where(has, weights["numberbatch"] * zn + weights["model2vec"] * zm, zm)
    return zscore(rel, rel[:n_v])


def place(lean, valid, strength, perfect_count, min_strength, slack, reach):
    """Map lean (-1 = start bank, +1 = far bank) to a river position by rank.

    The `perfect_count` words closest to the centre land inside the perfect zone
    (0.5 +/- slack), but only words related to BOTH banks by at least
    `min_strength` qualify. Weaker words that happen to sit centrally get
    nudged just outside the zone. Everything else spreads evenly toward the banks.
    """
    pos = np.full(lean.shape, np.nan)
    idx = np.flatnonzero(valid)
    if len(idx) == 0:
        return pos
    d = lean[idx]
    key = np.abs(d)
    strong = strength[idx] >= min_strength
    if strong.any():
        k_strong = min(perfect_count, int(strong.sum()))
        d_k = np.sort(key[strong])[k_strong - 1]
        weak_central = ~strong & (key <= d_k)
        key = np.where(weak_central, d_k + 1e-6 + key * 1e-3, key)
    order = np.argsort(key, kind="stable")
    ranks = np.empty(len(idx))
    ranks[order] = np.arange(1, len(idx) + 1)
    k = min(perfect_count, len(idx))
    edge = slack / reach  # fraction of a half-river that the perfect zone covers
    n = len(idx)
    f = np.where(
        ranks <= k,
        edge * (ranks - 0.5) / k,
        edge + (1 - edge) * (ranks - k) / max(n - k, 1),
    )
    sign = np.where(d >= 0, 1.0, -1.0)  # weak words keep their real side
    pos[idx] = np.clip(0.5 + sign * 0.5 * f, 0.02, 0.98)
    return pos


# ------------------------------------------------------------- link quality
def link_quality(pos, total, valid, edges):
    """Rank each word's total relatedness against the other words that land in
    the same band of the river. 0 = weakest in its band, 1 = strongest.
    Comparing within a band means a perfect-zone word competes with other
    perfect-zone words, and a near-bank word with other near-bank words."""
    q = np.full(pos.shape, np.nan)
    idx = np.flatnonzero(valid)
    band = np.searchsorted(edges, pos[idx], side="right") - 1
    for bnd in np.unique(band):
        members = idx[band == bnd]
        order = np.argsort(total[members], kind="stable")
        ranks = np.empty(len(members))
        ranks[order] = np.arange(len(members))
        q[members] = (ranks + 0.5) / len(members)
    return q


def tier_of(q, tiers):
    return "gold" if q >= tiers["gold"] else "silver" if q >= tiers["silver"] else "bronze"


# -------------------------------------------------------------------- build
def score_round(r, vocab, vocab_set, V, nb, m2v, game, sc):
    start, end = norm(r["start"]), norm(r["end"])
    banks = [start, end]
    answers = {norm(k): v for k, v in r.get("answers", {}).items()}
    banned = {norm(w) for w in r.get("banned", [])}
    calibrate = [norm(w) for w in r.get("calibrate", [])]

    extra_terms = [t for t in dict.fromkeys(list(answers) + calibrate) if t not in vocab_set]
    terms = vocab + extra_terms
    X = term_matrices(extra_terms, nb, m2v, V["nb"].shape[1])
    a = relatedness(start, V, X, nb, m2v, sc["weights"])
    b = relatedness(end, V, X, nb, m2v, sc["weights"])
    ap, bp = np.maximum(a, 0), np.maximum(b, 0)
    lean = (bp - ap) / (ap + bp + 1e-9)

    idx_of = {t: i for i, t in enumerate(terms)}
    known = [lean[idx_of[t]] for t in calibrate if t in idx_of]
    center = float(np.median(known)) if known else 0.0

    eligible = np.array([not uses_bank_word(t, banks) and t not in banned for t in terms])
    valid = eligible & (np.maximum(a, b) >= sc["valid_z"])
    for t in answers:
        if t in idx_of and eligible[idx_of[t]]:
            valid[idx_of[t]] = True

    strength = np.minimum(a, b)
    pos = place(lean - center, valid, strength, sc["perfect_count"], sc["perfect_min_strength"],
                game["slack"], game["reach"])
    for t, spec in answers.items():
        if "position" in spec and t in idx_of:
            pos[idx_of[t]] = float(spec["position"])

    lo, hi = 0.5 - game["slack"], 0.5 + game["slack"]
    bands = sc["quality_bands"]
    edges = np.unique(np.array([0.0] + [e for e in bands if e < lo] + [lo, hi]
                               + [e for e in bands if e > hi] + [1.0001]))
    q = link_quality(pos, ap + bp, valid, edges)
    for t, spec in answers.items():
        if "quality" in spec and t in idx_of:
            q[idx_of[t]] = float(spec["quality"])

    words = {terms[i]: [round(float(pos[i]), 3), round(float(q[i]), 2)] for i in np.flatnonzero(valid)}
    perfect = [i for i in np.flatnonzero(valid) if lo <= pos[i] <= hi]
    perfect.sort(key=lambda i: (terms[i] not in answers, -strength[i]))
    best = [terms[i] for i in perfect[:5]]

    # Flies: curated answers with a bonus, else the strongest perfect links
    flies = {t: {"bonus": s["bonus"], **({"note": s["note"]} if s.get("note") else {})}
             for t, s in answers.items() if s.get("bonus") and t in words}
    auto = []
    if not flies:
        for i in perfect[: sc["auto_flies"]]:
            flies[terms[i]] = {"bonus": sc["auto_fly_bonus"]}
            auto.append(terms[i])

    rnd = {"start": r["start"], "end": r["end"], "words": words, "flies": flies, "best": best}

    # report
    fly_txt = ", ".join(f"`{t}` +{s['bonus']}" + (" (auto)" if t in auto else "") for t, s in flies.items())
    rep = [f"## {r['start']} → {r['end']}", "",
           f"- valid words: {len(words)}  ·  perfect zone: {len(perfect)}  ·  centre shift: {center:+.3f}",
           f"- flies: {fly_txt or 'none'}", ""]
    weak = [i for i in perfect if strength[i] < 1.0 and terms[i] not in answers]
    if len(perfect) - len(weak) < 3:
        rep.append("> ⚠️ Fewer than 3 strong perfect links. Consider curating answers or changing the bank words.\n")
    rep += ["| word | rel. start (z) | rel. end (z) | position | quality |", "|---|---|---|---|---|"]
    for i in perfect:
        rep.append(f"| **{terms[i]}** | {a[i]:.2f} | {b[i]:.2f} | {pos[i]:.2f} | {q[i]:.2f} {tier_of(q[i], game['tiers'])} |")
    rep += ["", "Sample across the river:", "", "| word | rel. start | rel. end | position | quality |", "|---|---|---|---|---|"]
    valid_idx = sorted(np.flatnonzero(valid), key=lambda i: pos[i])
    for i in valid_idx[:: max(1, len(valid_idx) // 15)]:
        rep.append(f"| {terms[i]} | {a[i]:.2f} | {b[i]:.2f} | {pos[i]:.2f} | {q[i]:.2f} {tier_of(q[i], game['tiers'])} |")
    if answers:
        rep += ["", "Curated answers:", ""]
        for t, s in answers.items():
            where = (f"position {words[t][0]:.2f}, quality {words[t][1]:.2f}" if t in words
                     else "not allowed (uses a bank word or banned)")
            rep.append(f"- `{t}` → {where}  bonus {s.get('bonus', 0)}  {s.get('note', '')}")
    rep.append("")
    print(f"  {r['start']} -> {r['end']}: {len(words)} valid · flies: {', '.join(flies)} · best: {', '.join(best)}")
    return rnd, rep


def write_js(path: Path, assign: str, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// Generated by pipeline/build.py. Edit content/, not this file.\n"
                    f"{assign} = " + json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + ";\n",
                    encoding="utf8")


def day_hash(day, settings) -> str:
    blob = json.dumps({"rounds": day["rounds"], "game": settings["game"], "scoring": settings["scoring"],
                       "beta": settings.get("beta", False), "launch": settings["launch_date"]},
                      sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", action="append", help="only build this day (repeatable)")
    parser.add_argument("--force", action="store_true", help="rebuild past days even if already published")
    args = parser.parse_args()

    settings = json.loads(SETTINGS.read_text(encoding="utf8"))
    game, sc = settings["game"], settings["scoring"]
    launch = dt.date.fromisoformat(settings["launch_date"])
    today = dt.date.today()

    print(f"Reading {SCHEDULE.relative_to(ROOT)} ...")
    days, problems = read_schedule()
    for p in problems:
        print(f"  ⚠ {p}")
    hashes = json.loads(HASHES.read_text()) if HASHES.exists() else {}

    todo = []
    for date, day in sorted(days.items()):
        d = dt.date.fromisoformat(date)
        out = DATA_DIR / "days" / f"{date}.js"
        h = day_hash(day, settings)
        if args.date and date not in args.date:
            continue
        if d < launch:
            problems.append(f"{date} is before the launch date {launch}; skipped")
            continue
        if d < today and out.exists() and not args.force:
            if hashes.get(date) != h:
                print(f"  ⚠ {date} changed in the workbook but is already published; keeping the published version")
            continue
        if out.exists() and hashes.get(date) == h and not args.force:
            continue  # unchanged
        todo.append((d, date, day, out, h))

    # Future days removed from the workbook: unpublish them
    for f in (DATA_DIR / "days").glob("*.js"):
        if f.stem not in days and dt.date.fromisoformat(f.stem) >= today:
            f.unlink()
            hashes.pop(f.stem, None)
            print(f"Removed {f.stem}: no longer in the workbook")

    print(f"{len(days)} day(s) in the workbook, {len(todo)} to build")
    if todo or not (DATA_DIR / "vocab.js").exists():
        raw = [w for w in top_n_list("en", int(sc["vocab_size"] * 1.6))
               if re.fullmatch(r"[a-z]{3,}", w) and w not in STOPWORDS]
        extras = set()
        for _, _, day, _, _ in todo:
            for r in day["rounds"]:
                extras |= {norm(r["start"]), norm(r["end"])} | set(r["answers"]) | set(r["calibrate"])
        nb = load_numberbatch(set(raw) | extras)
        vocab = [w for w in raw if w in nb][: sc["vocab_size"]]
        vocab_set = set(vocab)
        print(f"Vocabulary: {len(vocab)} words")
        write_js(DATA_DIR / "vocab.js", "window.FROGGLE_VOCAB", " ".join(vocab))

    if todo:
        print(f"Loading Model2Vec {sc['model2vec_model']} ...")
        m2v = load_model2vec(sc["model2vec_model"])
        V = term_matrices(vocab, nb, m2v, next(iter(nb.values())).shape[0])
    for d, date, day, out, h in todo:
        number = (d - launch).days + 1
        print(f"Day {date} (#{number}, {day['status']}):")
        rounds, report = [], [f"# Froggle {date} (#{number}) · {day['status']}", ""]
        for r in day["rounds"]:
            rnd, rep = score_round(r, vocab, vocab_set, V, nb, m2v, game, sc)
            rounds.append(rnd)
            report += rep
        label = f"{number}B" if settings.get("beta") else str(number)  # "1B" while in beta
        day_obj = {"date": date, "number": number, "label": label, "status": day["status"],
                   "built": time.strftime("%Y-%m-%d %H:%M"), "game": game, "rounds": rounds}
        write_js(out, f'(window.FROGGLE_DAYS = window.FROGGLE_DAYS || {{}})["{date}"]', day_obj)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / f"{date}.md").write_text("\n".join(report), encoding="utf8")
        hashes[date] = h

    HASHES.write_text(json.dumps(hashes, indent=1, sort_keys=True))
    built = sorted(p.stem for p in (DATA_DIR / "days").glob("*.js"))
    # Ready days go live; past days no longer in the workbook stay published
    ready = [d for d in built if days.get(d, {}).get("status") == "ready"
             or (d not in days and d < today.isoformat())]
    drafts = [d for d in built if d not in ready]
    write_js(DATA_DIR / "index.js", "window.FROGGLE_INDEX",
             {"launch": settings["launch_date"], "dates": ready, "drafts": drafts})
    write_overview(days, built, ready, problems, launch, today)

    print(f"Published (Ready): {len(ready)} · Drafts: {len(drafts)}")
    upcoming = [(today + dt.timedelta(days=i)).isoformat() for i in range(14)]
    gaps = [d for d in upcoming if d not in ready]
    if gaps:
        print(f"  ⚠ No Ready puzzle yet for: {', '.join(gaps[:7])}{' …' if len(gaps) > 7 else ''}")
    if problems:
        print(f"{len(problems)} problem(s) in the workbook, listed above and in pipeline/reports/overview.md")
        return 1
    return 0


def write_overview(days, built, ready, problems, launch, today):
    lines = ["# Froggle schedule overview", "", f"Generated {time.strftime('%Y-%m-%d %H:%M')}", ""]
    if problems:
        lines += ["## ⚠ Problems in the workbook", ""] + [f"- {p}" for p in problems] + [""]
    lines += ["## Days", "", "| date | # | status | crossings | flies | report |", "|---|---|---|---|---|---|"]
    for date, day in sorted(days.items()):
        if date < today.isoformat():
            continue
        chain = " → ".join([day["rounds"][0]["start"]] + [r["end"] for r in day["rounds"]])
        flies = sum(1 for r in day["rounds"] for s in r["answers"].values() if s.get("bonus"))
        status = "✅ ready" if date in ready else ("📝 draft" if date in built else "not built")
        num = (dt.date.fromisoformat(date) - launch).days + 1
        lines.append(f"| {date} | {num} | {status} | {chain} | {flies} curated | [{date}.md]({date}.md) |")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "overview.md").write_text("\n".join(lines) + "\n", encoding="utf8")


if __name__ == "__main__":
    sys.exit(main())
