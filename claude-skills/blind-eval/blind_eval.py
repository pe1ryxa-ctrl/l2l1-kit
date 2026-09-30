#!/usr/bin/env python3
"""blind-eval — сліпе порівняння відповідей моделей: ізоляція пакета оцінювання з пошуком витоків міток,
промпт оцінювача, розкриття ключа й зведення. Лише stdlib, Python >= 3.10.

Підкоманди:
  isolate — скопіювати ЛИШЕ пакет у новий ізольований каталог і перевірити його на витоки міток
  scan    — лише перевірка файла чи каталогу на витоки міток (напр. після правки пакета)
  prompt  — промпт read-only субагента-оцінювача для діапазону елементів
  unblind — ключ + оцінки (jsonl) → середні, вигадки, перемоги, нічиї за мітками; види, страти, попарна різниця

Приклади — у SKILL.md поруч.
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

CTX = 25
KEYISH = re.compile(r"(^|[^a-z])(key|keys|ключ\w*)([^a-z]|$)", re.I)
MAIN_FIELDS = ("completeness", "accuracy", "usefulness")
NOT_SCORES = {"item", "letter", "best"}


def read_text(p):
    return Path(p).read_bytes().decode("utf-8", errors="replace")


def split_list(values):
    out = []
    for v in values or []:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out


# ---------------------------------------------------------------- isolate / scan
def label_patterns(labels):
    """Word-boundary, case-insensitive; lookarounds instead of \\b so labels like 'gpt-4o' or 'v2+' work too."""
    return [(lbl, re.compile(r"(?<!\w)" + re.escape(lbl) + r"(?!\w)", re.I)) for lbl in labels]


def scan_text(text, pats, ctx=CTX):
    """[(line, label, match, context)] — context is ±ctx chars around the hit, newlines shown as ⏎."""
    hits = []
    for lbl, rx in pats:
        for m in rx.finditer(text):
            s, e = max(0, m.start() - ctx), min(len(text), m.end() + ctx)
            hits.append((text.count("\n", 0, m.start()) + 1, lbl, m.group(0), text[s:e].replace("\n", "⏎")))
    return sorted(hits)


def files_under(p):
    p = Path(p)
    return [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file())


def scan_path(path, labels):
    """Prints every hit and suspicious file name; returns the number of findings."""
    pats = label_patterns(labels)
    files = files_under(path)
    findings = 0
    for f in files:
        rel = f.name if Path(path).is_file() else f.relative_to(path)
        if KEYISH.search(f.name):
            print(f"SUSPECT FILE {rel}: the name looks like an answer key — keys never go into the grader's directory")
            findings += 1
        for line, lbl, got, ctx in scan_text(read_text(f), pats):
            print(f"LEAK? {rel}:{line} [{lbl}] '{got}': …{ctx}…")
            findings += 1
    if findings:
        print(f"{findings} finding(s) in {len(files)} file(s). Review each one: a device or product name "
              f"(e.g. 'V2') may be a false positive; a real leak must be removed before grading.")
    else:
        print(f"CLEAN: no label hits in {len(files)} file(s) (labels: {', '.join(labels)})")
    return findings


def cmd_isolate(a):
    labels = split_list(a.labels)
    if not labels:
        sys.exit("--labels is required: model/version names that must not appear in the pack")
    src, out = Path(a.pack).resolve(), Path(a.out).resolve()
    if not src.exists():
        sys.exit(f"no {src}")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        sys.exit(f"--out {out} must be a new or empty directory (isolation: nothing but the pack there)")
    base = src if src.is_dir() else src.parent
    if out == base or base in out.parents:
        sys.exit(f"--out {out} is inside the pack's own directory — choose a separate place")
    keyish = [f for f in files_under(src) if KEYISH.search(f.name)]
    if keyish:
        sys.exit("refused, nothing copied: the pack has files named like an answer key: "
                 + ", ".join(str(f) for f in keyish))
    out.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        shutil.copy2(src, out / src.name)
    else:
        shutil.copytree(src, out, dirs_exist_ok=True)
    print(f"isolated: {src} → {out}")
    sys.exit(1 if scan_path(out, labels) else 0)


def cmd_scan(a):
    labels = split_list(a.labels)
    if not labels:
        sys.exit("--labels is required")
    if not Path(a.path).exists():
        sys.exit(f"no {a.path}")
    sys.exit(1 if scan_path(a.path, labels) else 0)


# ---------------------------------------------------------------- prompt
PROMPT = """You are a blind grader. You compare anonymised answers under letters; you do not know, and must not try to find out, which model or version wrote which answer.

## What you may read
- ONLY the grade pack `{pack}`{sources}.
- NEVER open answer keys, run or output directories, raw model outputs, reports, task files, git history or anything else. Do not search the filesystem for them. If an answer seems to reveal its author (a model or version name), ignore that and mention it in `note`.
- Read-only: create or modify no file except your output file `{out}`.

## Scope
Grade items {items} of the pack: every lettered answer of each item.

## Scores (integers {scale}, higher is better)
- completeness: covers everything the source supports for this item.
- accuracy: every stated fact matches the source; errors and fabrications lower it.
- usefulness: how well the answer serves the item's purpose for its reader.{extra_desc}

## Judgement rules
1. Empty is correct when the source is empty: if the source holds nothing for the item, an empty answer or an explicit "not in the source" is fully correct (top completeness and accuracy); any content invented there is a fabrication.
2. A fabrication is a concrete fact (number, name, identifier, date, claim) that is NOT in the source. An omission is not a fabrication. A faithful translation or paraphrase of the source is not a fabrication; general knowledge stated as if it came from the source is.
3. Identifiers (part and model numbers, versions, frequencies, codes, file names, URLs) are compared exactly, character by character: a near miss is an accuracy error; an identifier absent from the source is a fabrication.
4. Judge each answer against the source, not against the other answers. Length and style are not merit by themselves.{rules}

## Output
Append to `{out}` one JSON object per line (JSON Lines, UTF-8, nothing else in the file):
- per answer: {{"item": "<id>", "letter": "<letter>", "completeness": n, "accuracy": n, "usefulness": n,{extra_json} "fabrications": ["<each fabricated fact, quoted briefly>"], "note": "<one sentence in {lang}>"}}
- per item, after its answers: {{"item": "<id>", "best": "<letter>"}}; use "tie" when no answer is clearly better.
Copy item ids and letters exactly as written in the pack. Finish your reply with one line: how many items and answers you graded. Never state or guess which model wrote an answer.
"""


def cmd_prompt(a):
    extra = split_list(a.extra_fields)
    rules = ""
    if a.rules_file:
        body = read_text(a.rules_file).strip()
        items = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", l).strip() for l in body.splitlines() if l.strip()]
        rules = "".join(f"\n{n}. {r}" for n, r in enumerate(items, start=5))
    print(PROMPT.format(
        pack=a.pack, out=a.out, items=a.items, scale=a.scale, lang=a.lang,
        sources=(f" and the source files it references (under {a.sources})" if a.sources
                 else " and the source files it references"),
        extra_desc="".join(f"\n- {f}: see the domain rules below; same scale." for f in extra),
        extra_json="".join(f' "{f}": n,' for f in extra),
        rules=rules,
    ), end="")


# ---------------------------------------------------------------- unblind
def load_key(path):
    """Tolerant key formats → {item: {letter: label}}:
    {item: {letter: label}} | {"items": {item: {...}}} | {item: {"letters": {letter: label}, ...}}"""
    data = json.loads(read_text(path))
    if isinstance(data, dict) and isinstance(data.get("items"), dict):
        data = data["items"]
    if not isinstance(data, dict):
        raise ValueError("key: expected a JSON object of items")
    key = {}
    for item, v in data.items():
        if isinstance(v, dict) and isinstance(v.get("letters"), dict):
            v = v["letters"]
        if not isinstance(v, dict) or not v or not all(isinstance(x, str) for x in v.values()):
            raise ValueError(f"key: item {item!r}: expected {{letter: label}}")
        key[str(item)] = {str(k): x for k, x in v.items()}
    return key


def load_grades(paths, warn):
    """→ scores {(item, letter): record}, best {item: letter}. Later duplicates win, with a warning."""
    scores, best = {}, {}
    for p in paths:
        for n, line in enumerate(read_text(p).splitlines(), start=1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                warn(f"{p}:{n}: not JSON ({e.msg}) — skipped")
                continue
            if not isinstance(r, dict) or "item" not in r:
                warn(f"{p}:{n}: no 'item' — skipped")
                continue
            item = str(r["item"])
            if "letter" in r:
                k = (item, str(r["letter"]))
                if k in scores:
                    warn(f"{p}:{n}: {item}/{k[1]} graded twice — the later line wins")
                scores[k] = r
            elif "best" in r:
                if item in best:
                    warn(f"{p}:{n}: {item}: 'best' given twice — the later line wins")
                best[item] = str(r["best"])
            else:
                warn(f"{p}:{n}: neither 'letter' nor 'best' — skipped")
    return scores, best


def numeric_fields(scores):
    seen = {f for r in scores.values() for f, v in r.items()
            if f not in NOT_SCORES and isinstance(v, (int, float)) and not isinstance(v, bool)}
    return [f for f in MAIN_FIELDS if f in seen] + sorted(seen - set(MAIN_FIELDS))


def parse_kinds(spec):
    """'e=extraction,g=generation' → [('e', 'extraction'), ('g', 'generation')]; None → no split."""
    if not spec:
        return None
    kinds = []
    for part in spec.split(","):
        prefix, _, name = part.partition("=")
        if not prefix.strip():
            raise ValueError(f"--kind-prefix: bad part {part!r}")
        kinds.append((prefix.strip(), name.strip() or prefix.strip()))
    return kinds


def kind_fn(kinds):
    if not kinds:
        return lambda item: "all"
    longest_first = sorted(kinds, key=lambda k: -len(k[0]))
    return lambda item: next((name for p, name in longest_first if item.startswith(p)), "other")


def load_strata(path, field):
    """{item: stratum}: {item: {field: v}} | {"items": ...} | [{"item"|"id": …, field: v}, …]."""
    data = json.loads(read_text(path))
    if isinstance(data, dict) and isinstance(data.get("items"), (dict, list)):
        data = data["items"]
    out = {}
    if isinstance(data, dict):
        for item, rec in data.items():
            if isinstance(rec, dict) and field in rec:
                out[str(item)] = str(rec[field])
    elif isinstance(data, list):
        for rec in data:
            if isinstance(rec, dict) and field in rec:
                ident = rec.get("item", rec.get("id"))
                if ident is not None:
                    out[str(ident)] = str(rec[field])
    else:
        raise ValueError("strata: expected a JSON object or list")
    return out


def aggregate(key, scores, best, group_of, fields):
    """{group: {"items": n, "ties": n, "no_best": n, "labels": {label: {n, means, fab, fab_answers, wins}}}}"""
    groups = {}

    def g(name):
        return groups.setdefault(name, {"items": set(), "ties": 0, "no_best": 0, "labels": {}, "_sums": {}})

    for (item, letter), r in scores.items():
        if item not in key or letter not in key[item]:
            continue
        label = key[item][letter]
        grp = g(group_of(item))
        grp["items"].add(item)
        st = grp["labels"].setdefault(label, {"n": 0, "fab": 0, "fab_answers": 0, "wins": 0})
        sums = grp["_sums"].setdefault(label, {f: [0.0, 0] for f in fields})
        st["n"] += 1
        fabs = r.get("fabrications") or []
        st["fab"] += len(fabs) if isinstance(fabs, list) else 1
        st["fab_answers"] += 1 if fabs else 0
        for f in fields:
            v = r.get(f)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                sums[f][0] += v
                sums[f][1] += 1
    for item in sorted({i for i, _ in scores} | set(best)):
        if item not in key:
            continue
        grp = g(group_of(item))
        if item not in best:
            if item in grp["items"]:
                grp["no_best"] += 1
            continue
        grp["items"].add(item)
        letter = best[item]
        if letter in key[item]:
            label = key[item][letter]
            grp["labels"].setdefault(label, {"n": 0, "fab": 0, "fab_answers": 0, "wins": 0})["wins"] += 1
        else:
            grp["ties"] += 1
    for grp in groups.values():
        for label, st in grp["labels"].items():
            sums = grp["_sums"].get(label, {})
            st["means"] = {f: (s / c if c else None) for f, (s, c) in sums.items()}
        grp["items"] = len(grp["items"])
        del grp["_sums"]
    return groups


def pair_diff(key, scores, group_of, fields, a_lbl, b_lbl):
    """Per group: for items graded for both labels — mean A−B per field and counts A>B / A<B / equal."""
    by_item = {}
    for (item, letter), r in scores.items():
        lbl = key.get(item, {}).get(letter)
        if lbl in (a_lbl, b_lbl):
            by_item.setdefault(item, {})[lbl] = r
    out = {}
    for item, pair in sorted(by_item.items()):
        if len(pair) < 2:
            continue
        grp = out.setdefault(group_of(item), {"pairs": 0, "fields": {f: {"sum": 0.0, "n": 0, "a": 0, "b": 0, "eq": 0}
                                                                         for f in fields}})
        grp["pairs"] += 1
        for f in fields:
            va, vb = pair[a_lbl].get(f), pair[b_lbl].get(f)
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (va, vb)):
                continue
            d = grp["fields"][f]
            d["sum"] += va - vb
            d["n"] += 1
            d["a" if va > vb else "b" if va < vb else "eq"] += 1
    for grp in out.values():
        for d in grp["fields"].values():
            d["mean"] = d.pop("sum") / d["n"] if d["n"] else None
    return out


def summarize(key, scores, best, kinds=None, strata=None, diff=None, warn=print):
    fields = numeric_fields(scores)
    for (item, letter) in scores:
        if item not in key:
            warn(f"graded item {item} is not in the key — skipped")
        elif letter not in key[item]:
            warn(f"graded {item}/{letter}: no such letter in the key — skipped")
    for item in best:
        if item not in key:
            warn(f"'best' for item {item} that is not in the key — skipped")
    graded = {i for i, _ in scores}
    missing = [i for i in key if i not in graded]
    if missing:
        warn(f"{len(missing)} item(s) of the key have no grades: {', '.join(missing[:10])}"
             + (" …" if len(missing) > 10 else ""))
    by_kind = kind_fn(kinds)
    kind_order = [name for _, name in kinds or []] + ["other", "all"]
    res = {"fields": fields, "kinds": ordered(aggregate(key, scores, best, by_kind, fields), kind_order)}
    if kinds and len(res["kinds"]) > 1:
        res["kinds"]["all"] = aggregate(key, scores, best, lambda i: "all", fields)["all"]
    if strata is not None:
        st = aggregate(key, scores, best, lambda i: strata.get(i, "?"), fields)
        res["strata"] = ordered(st, sorted(k for k in st if k != "?") + ["?"])
    if diff:
        res["diff"] = {"a": diff[0], "b": diff[1], "groups": pair_diff(key, scores, by_kind, fields, *diff)}
        if kinds and len(res["diff"]["groups"]) > 1:
            res["diff"]["groups"]["all"] = pair_diff(key, scores, lambda i: "all", fields, *diff).get("all")
    return res


def ordered(groups, order):
    return {k: groups[k] for k in order if k in groups} | {k: v for k, v in groups.items() if k not in order}


def fmt(x):
    return "—" if x is None else f"{x:.2f}"


def render_groups(groups, fields, title):
    lines = []
    for name, grp in groups.items():
        lines += [f"## {title}{name} — {grp['items']} item(s), ties: {grp['ties']}"
                  + (f", no 'best': {grp['no_best']}" if grp["no_best"] else ""), "",
                  "| label | n | " + " | ".join(fields) + " | fabrications (answers) | wins |",
                  "|---|---:|" + "---:|" * len(fields) + "---:|---:|"]
        for label, st in sorted(grp["labels"].items()):
            means = st.get("means", {})
            lines.append(f"| {label} | {st['n']} | " + " | ".join(fmt(means.get(f)) for f in fields)
                         + f" | {st['fab']} ({st['fab_answers']}) | {st['wins']} |")
        lines.append("")
    return lines


def render(res, strata_field=None):
    fields = res["fields"]
    lines = ["# Blind evaluation — summary", ""]
    lines += render_groups(res["kinds"], fields, "")
    if "strata" in res:
        lines += [f"# Strata by `{strata_field}`", ""] + render_groups(res["strata"], fields, f"{strata_field} = ")
    if "diff" in res:
        d = res["diff"]
        lines += [f"# Pairwise: {d['a']} − {d['b']}", ""]
        for name, grp in d["groups"].items():
            if not grp:
                continue
            lines += [f"## {name} — {grp['pairs']} pair(s)", "",
                      f"| field | mean {d['a']} − {d['b']} | {d['a']} better | {d['b']} better | equal |",
                      "|---|---:|---:|---:|---:|"]
            for f, x in grp["fields"].items():
                mean = "—" if x["mean"] is None else f"{x['mean']:+.2f}"
                lines.append(f"| {f} | {mean} | {x['a']} | {x['b']} | {x['eq']} |")
            lines.append("")
    return "\n".join(lines)


def looks_ephemeral(p):
    p = Path(p).resolve()
    tmp = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
    return "scratchpad" in p.parts or any(t == p or t in p.parents for t in tmp)


def save_to(dest, inputs, summary_md, force):
    dest = Path(dest)
    if looks_ephemeral(dest) and not force:
        sys.exit(f"--save-to {dest} looks like a scratchpad/temp directory, which sessions wipe — "
                 f"choose a persistent place (or pass --force)")
    dest.mkdir(parents=True, exist_ok=True)
    targets, used = [], set()
    for i, src in enumerate(inputs):
        name = Path(src).name
        if name in used:
            name = f"{i}_{name}"
        used.add(name)
        targets.append((Path(src), dest / name))
    targets.append((None, dest / "summary.md"))
    for src, t in targets:
        if t.exists() and not force:
            same = src is not None and t.read_bytes() == src.read_bytes()
            if not same:
                sys.exit(f"refused: {t} exists and differs (pass --force to overwrite)")
    for src, t in targets:
        if src is not None:
            shutil.copyfile(src, t)
    (dest / "summary.md").write_bytes(summary_md.encode("utf-8"))
    print(f"saved: {', '.join(t.name for _, t in targets)} → {dest}")


def cmd_unblind(a):
    warnings = []

    def warn(msg):
        warnings.append(msg)
        print(f"WARNING: {msg}", file=sys.stderr)

    try:
        key = load_key(a.key)
        kinds = parse_kinds(a.kind_prefix)
        strata = load_strata(a.strata, a.strata_field) if a.strata else None
    except (ValueError, json.JSONDecodeError) as e:
        sys.exit(str(e))
    if a.strata and not a.strata_field:
        sys.exit("--strata needs --strata-field")
    diff = None
    if a.diff:
        diff = [x.strip() for x in a.diff.split(",")]
        if len(diff) != 2 or not all(diff):
            sys.exit("--diff expects two labels: A,B")
        labels = {lbl for v in key.values() for lbl in v.values()}
        for lbl in diff:
            if lbl not in labels:
                sys.exit(f"--diff: label {lbl!r} is not in the key (labels: {', '.join(sorted(labels))})")
    scores, best = load_grades(a.grades, warn)
    res = summarize(key, scores, best, kinds, strata, diff, warn)
    md = render(res, a.strata_field)
    if warnings:
        md += "\n# Warnings\n\n" + "\n".join(f"- {w}" for w in warnings) + "\n"
    print(md)
    if a.save_to:
        save_to(a.save_to, [a.key, *a.grades] + ([a.strata] if a.strata else []), md, a.force)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    i = sp.add_parser("isolate", help="copy ONLY the pack into a new isolated dir and scan it for label leaks")
    i.add_argument("--pack", required=True, help="grade pack: a file or a directory (never the key)")
    i.add_argument("--out", required=True, help="new or empty directory for the grader")
    i.add_argument("--labels", action="append", required=True, help="model/version names, comma-separated, repeatable")
    i.set_defaults(fn=cmd_isolate)

    s = sp.add_parser("scan", help="scan a file or directory for label leaks (exit 1 on any finding)")
    s.add_argument("path")
    s.add_argument("--labels", action="append", required=True)
    s.set_defaults(fn=cmd_scan)

    p = sp.add_parser("prompt", help="grader subagent prompt for a range of items")
    p.add_argument("--pack", required=True, help="pack path inside the isolated directory")
    p.add_argument("--out", required=True, help="output .jsonl path the grader appends to")
    p.add_argument("--items", required=True, help="item range as written in the pack, e.g. 'e01–e30'")
    p.add_argument("--sources", help="directory of the source files the pack references")
    p.add_argument("--rules-file", help="domain judgement rules, one per line (appended after the generic ones)")
    p.add_argument("--extra-fields", action="append", help="extra numeric criteria, e.g. honesty (define them in --rules-file)")
    p.add_argument("--scale", default="1–5")
    p.add_argument("--lang", default="Ukrainian", help="language of the grader's notes")
    p.set_defaults(fn=cmd_prompt)

    u = sp.add_parser("unblind", help="key + grades jsonl → per-label means, fabrications, wins, ties")
    u.add_argument("--key", required=True)
    u.add_argument("--grades", nargs="+", required=True, help="one or more grades .jsonl")
    u.add_argument("--kind-prefix", help="split by item id prefix, e.g. 'e=extraction,g=generation' (default: one group)")
    u.add_argument("--strata", help="sample JSON with a per-item field for a per-stratum table")
    u.add_argument("--strata-field")
    u.add_argument("--diff", help="pairwise difference of two labels: A,B")
    u.add_argument("--save-to", help="persistent directory: copies key, grades (and strata) and writes summary.md")
    u.add_argument("--force", action="store_true", help="--save-to: allow a temp path / overwrite differing files")
    u.set_defaults(fn=cmd_unblind)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
