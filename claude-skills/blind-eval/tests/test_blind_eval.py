"""Тести blind_eval.py: пошук витоків міток, ізоляція пакета, математика розкриття, формати ключа.

Запуск: python -m pytest -q -p no:cacheprovider claude-skills/blind-eval/tests
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blind_eval as be  # noqa: E402

KEY = {
    "e1": {"A": "X", "B": "Y", "C": "Z"},
    "e2": {"A": "Y", "B": "Z", "C": "X"},
    "g1": {"A": "Z", "B": "X", "C": "Y"},
    "g2": {"A": "X", "B": "Y", "C": "Z"},
}


def ans(item, letter, c, a, u, fabs=(), **extra):
    return {"item": item, "letter": letter, "completeness": c, "accuracy": a, "usefulness": u,
            "fabrications": list(fabs), "note": "", **extra}


GRADES = [
    ans("e1", "A", 5, 5, 4), ans("e1", "B", 3, 4, 3, ["f1"]), ans("e1", "C", 4, 4, 4), {"item": "e1", "best": "A"},
    ans("e2", "A", 4, 5, 5), ans("e2", "B", 2, 2, 2, ["f1", "f2"]), ans("e2", "C", 4, 4, 4), {"item": "e2", "best": "tie"},
    ans("g1", "A", 3, 3, 3, honesty=5), ans("g1", "B", 5, 4, 5, honesty=4), ans("g1", "C", 4, 4, 4, ["f"], honesty=2),
    {"item": "g1", "best": "B"},
    ans("g2", "A", 3, 3, 3, honesty=3), ans("g2", "B", 5, 5, 5, honesty=5), ans("g2", "C", 1, 1, 1, ["a", "b", "c"], honesty=1),
    {"item": "g2", "best": "B"},
]


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return path


def load(tmp_path, rows=GRADES, key=KEY):
    kp = tmp_path / "key.json"
    kp.write_text(json.dumps(key), encoding="utf-8")
    warnings = []
    scores, best = be.load_grades([write_jsonl(tmp_path / "g.jsonl", rows)], warnings.append)
    return be.load_key(kp), scores, best, warnings


# ---------------------------------------------------------------- scan / isolate
TEXT = "Answer by GPT-4o.\nThe V2 board runs flash3 firmware; Flash wins. gpt-4omni xflash"


def test_scan_hits_word_boundary_case_insensitive():
    hits = be.scan_text(TEXT, be.label_patterns(["gpt-4o", "flash", "v2"]))
    assert [(line, lbl, got) for line, lbl, got, _ in hits] == [
        (1, "gpt-4o", "GPT-4o"), (2, "flash", "Flash"), (2, "v2", "V2")]


def test_scan_misses_inside_words():
    assert be.scan_text("flash3 xflash gpt-4omni v22 2v2", be.label_patterns(["flash", "gpt-4o", "v2"])) == []


def test_scan_context_is_25_chars_each_side():
    (line, lbl, got, ctx), = be.scan_text(TEXT, be.label_patterns(["v2"]))
    i = TEXT.index("V2")
    assert ctx == TEXT[max(0, i - 25):i + 2 + 25].replace("\n", "⏎")
    assert "⏎" in ctx


def test_isolate_copies_only_the_pack(tmp_path, capsys):
    src = tmp_path / "run"
    src.mkdir()
    (src / "pack.md").write_text("## e1\nA: 5.8 GHz\nB: 5.9 GHz\n", encoding="utf-8")
    (src / "key.json").write_text(json.dumps(KEY), encoding="utf-8")
    out = tmp_path / "iso"
    with pytest.raises(SystemExit) as e:
        be.main(["isolate", "--pack", str(src / "pack.md"), "--out", str(out), "--labels", "X-model,flash"])
    assert e.value.code == 0
    assert sorted(p.name for p in out.iterdir()) == ["pack.md"]
    assert "CLEAN" in capsys.readouterr().out


def test_isolate_reports_leak_and_exits_1(tmp_path, capsys):
    (tmp_path / "run").mkdir()
    pack = tmp_path / "run" / "pack.md"
    pack.write_text("## e1\nA: as Gemini Flash I think…\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        be.main(["isolate", "--pack", str(pack), "--out", str(tmp_path / "iso"), "--labels", "flash", "--labels", "pro"])
    out = capsys.readouterr().out
    assert e.value.code == 1
    assert "LEAK? pack.md:2 [flash] 'Flash'" in out


def test_isolate_refuses_key_in_pack_dir_and_nested_out(tmp_path):
    src = tmp_path / "pack"
    src.mkdir()
    (src / "pack.md").write_text("x", encoding="utf-8")
    (src / "grade_key.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        be.main(["isolate", "--pack", str(src), "--out", str(tmp_path / "iso"), "--labels", "flash"])
    assert "answer key" in str(e.value.code) and not (tmp_path / "iso").exists()
    (src / "grade_key.json").unlink()
    with pytest.raises(SystemExit) as e:
        be.main(["isolate", "--pack", str(src), "--out", str(src / "iso"), "--labels", "flash"])
    assert "inside the pack" in str(e.value.code)


def test_prompt_fills_placeholders(tmp_path, capsys):
    rules = tmp_path / "rules.txt"
    rules.write_text("- 3.8 GHz band names are identifiers\n2) honesty: admits missing data\n", encoding="utf-8")
    be.main(["prompt", "--pack", "/iso/pack.md", "--out", "/iso/g1.jsonl", "--items", "e01–e30",
             "--rules-file", str(rules), "--extra-fields", "honesty"])
    out = capsys.readouterr().out
    assert "`/iso/pack.md`" in out and "`/iso/g1.jsonl`" in out and "items e01–e30" in out
    assert "\n5. 3.8 GHz band names are identifiers\n6. honesty: admits missing data\n" in out
    assert '"usefulness": n, "honesty": n, "fabrications"' in out
    assert '{"item": "<id>", "best": "<letter>"}' in out
    assert "{" + "pack}" not in out


# ---------------------------------------------------------------- key formats
def test_key_formats_are_equivalent(tmp_path):
    forms = [KEY, {"items": KEY, "meta": {"seed": 1}},
             {i: {"letters": v, "source": "s.md"} for i, v in KEY.items()},
             {"items": {i: {"letters": v} for i, v in KEY.items()}}]
    for n, form in enumerate(forms):
        p = tmp_path / f"k{n}.json"
        p.write_text(json.dumps(form), encoding="utf-8")
        assert be.load_key(p) == KEY


def test_key_bad_format_rejected(tmp_path):
    p = tmp_path / "k.json"
    p.write_text(json.dumps({"e1": ["X", "Y"]}), encoding="utf-8")
    with pytest.raises(ValueError):
        be.load_key(p)


# ---------------------------------------------------------------- unblind math
def test_unblind_single_group(tmp_path):
    key, scores, best, warnings = load(tmp_path)
    res = be.summarize(key, scores, best)
    assert warnings == [] and res["fields"] == ["completeness", "accuracy", "usefulness", "honesty"]
    g = res["kinds"]["all"]
    assert g["items"] == 4 and g["ties"] == 1 and g["no_best"] == 0
    x, y, z = (g["labels"][k] for k in "XYZ")
    assert x["means"] == {"completeness": 4.25, "accuracy": 4.0, "usefulness": 4.0, "honesty": 3.5}
    assert y["means"] == {"completeness": 4.0, "accuracy": 4.5, "usefulness": 4.25, "honesty": 3.5}
    assert z["means"]["completeness"] == 2.5 and z["means"]["honesty"] == 3.0
    assert (x["n"], x["fab"], x["fab_answers"], x["wins"]) == (4, 0, 0, 2)
    assert (y["fab"], y["fab_answers"], y["wins"]) == (2, 2, 1)
    assert (z["fab"], z["fab_answers"], z["wins"]) == (5, 2, 0)


def test_unblind_kinds(tmp_path):
    key, scores, best, _ = load(tmp_path)
    res = be.summarize(key, scores, best, kinds=be.parse_kinds("e=extraction,g=generation"))
    ex, gen, allg = res["kinds"]["extraction"], res["kinds"]["generation"], res["kinds"]["all"]
    assert ex["items"] == 2 and ex["ties"] == 1 and gen["ties"] == 0
    assert ex["labels"]["X"]["means"]["completeness"] == 4.5 and ex["labels"]["X"]["wins"] == 1
    assert ex["labels"]["X"]["means"]["honesty"] is None
    assert ex["labels"]["Y"]["fab"] == 1
    assert gen["labels"]["X"]["means"]["honesty"] == 3.5 and gen["labels"]["Y"]["wins"] == 1
    assert allg["labels"]["X"]["wins"] == 2 and allg["items"] == 4


def test_unblind_unmatched_prefix_goes_to_other(tmp_path):
    key, scores, best, _ = load(tmp_path)
    res = be.summarize(key, scores, best, kinds=be.parse_kinds("e=extraction"))
    assert set(res["kinds"]) == {"extraction", "other", "all"}


def test_unblind_strata(tmp_path):
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps([{"item": "e1", "lang": "uk"}, {"id": "e2", "lang": "en"},
                                  {"item": "g1", "lang": "uk"}]), encoding="utf-8")
    key, scores, best, _ = load(tmp_path)
    res = be.summarize(key, scores, best, strata=be.load_strata(sample, "lang"))
    st = res["strata"]
    assert st["uk"]["labels"]["X"]["means"]["completeness"] == 5.0 and st["uk"]["labels"]["X"]["wins"] == 2
    assert st["en"]["ties"] == 1 and st["?"]["labels"]["Y"]["wins"] == 1


def test_unblind_diff(tmp_path):
    key, scores, best, _ = load(tmp_path)
    d = be.summarize(key, scores, best, diff=["X", "Y"])["diff"]["groups"]["all"]
    assert d["pairs"] == 4
    c = d["fields"]["completeness"]
    assert (c["mean"], c["a"], c["b"], c["eq"], c["n"]) == (0.25, 2, 1, 1, 4)
    h = d["fields"]["honesty"]
    assert (h["mean"], h["a"], h["b"], h["n"]) == (0.0, 1, 1, 2)


def test_unblind_warnings_and_render(tmp_path):
    rows = GRADES + [ans("e1", "A", 1, 1, 1), ans("e1", "D", 5, 5, 5), ans("zz", "A", 5, 5, 5)]
    key, scores, best, warnings = load(tmp_path, rows)
    assert any("graded twice" in w for w in warnings)
    res = be.summarize(key, scores, best, warn=warnings.append, diff=["X", "Y"])
    assert any("no such letter" in w for w in warnings) and any("zz" in w for w in warnings)
    assert res["kinds"]["all"]["labels"]["X"]["means"]["completeness"] == 3.25  # e1/A: пізніший рядок (1) переміг
    md = be.render(res)
    assert "| X | 4 | 3.25 |" in md and "# Pairwise: X − Y" in md


def test_unblind_cli_save_to(tmp_path, capsys, monkeypatch):
    kp = tmp_path / "key.json"
    kp.write_text(json.dumps({"items": KEY}), encoding="utf-8")
    gp = write_jsonl(tmp_path / "g.jsonl", GRADES)
    dest = tmp_path / "persist"
    with pytest.raises(SystemExit) as e:  # pytest tmp_path is a temp dir — refused without --force
        be.main(["unblind", "--key", str(kp), "--grades", str(gp), "--save-to", str(dest)])
    assert "scratchpad/temp" in str(e.value.code)
    monkeypatch.setattr(be, "looks_ephemeral", lambda p: False)
    be.main(["unblind", "--key", str(kp), "--grades", str(gp), "--kind-prefix", "e=extraction,g=generation",
             "--save-to", str(dest)])
    assert sorted(p.name for p in dest.iterdir()) == ["g.jsonl", "key.json", "summary.md"]
    summary = (dest / "summary.md").read_text(encoding="utf-8")
    assert "## extraction — 2 item(s), ties: 1" in summary
    assert summary in capsys.readouterr().out


def test_looks_ephemeral():
    assert be.looks_ephemeral("/private/tmp/claude-501/x/scratchpad/eval")
    assert be.looks_ephemeral("/tmp/eval")
    assert not be.looks_ephemeral(str(Path.home() / "Antigravity" / "DDL" / "docs" / "eval"))


def test_kind_longest_prefix_wins_and_order_is_kept():
    kinds = be.parse_kinds("e=extraction,ex=extra")
    assert kinds == [("e", "extraction"), ("ex", "extra")]
    f = be.kind_fn(kinds)
    assert (f("ex1"), f("e1"), f("g1")) == ("extra", "extraction", "other")
