#!/usr/bin/env python3
"""corpus/ の報告でJevルールを測る。試しの計測用で、本体の実装ではない。

  python3 scripts/eval.py run      全部の組み立て方でJevに聞き、値を scripts/out/scores_<組み立て>.json に保存する
  python3 scripts/eval.py report   保存した値を corpus/labels/ の正解と突き合わせて集計する

原文（applies / excludes / words）には手を入れない。変えるのは組み立て方だけで、次の3点を組み合わせて測る。

  形   criteria: 1ルール1質問。applies を criteria.true、excludes を criteria.false に入れる。
       atomic:   applies と excludes の記述1つにつき1質問。値はコードで合成する。
                 合成 = applies の平均 × (1 − excludes の最大)
  文脈 bare:    対象の文だけを渡す。
       located: 報告全文と対象の文を渡す。
  言語 ja / en: 足す1文（question）の言語。原文は日本語のまま。

共通: 単位1つにつき1リクエスト。対象の文は `target`、match に当たった箇所は `matched`、
列挙された語は `words` として渡し、question からバッククォートで指す。
"""
import itertools
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import yaml

from jev_check import PRICE_PER_MTOK, ROOT, ask

# 静的に移すかどうかの判断待ち
SKIP = {"long-sentence"}
OUT = ROOT / "scripts" / "out"
CONFIGS = ["-".join(c) for c in itertools.product(("criteria", "atomic"), ("bare", "located"), ("ja", "en"))]
QUESTION = {
    ("ja", False): "`target` は、`description` に当たる。",
    ("ja", True): "`target` の中の `matched` は、`description` に当たる。",
    ("en", False): "`target` fits `description`.",
    ("en", True): "`matched` in `target` fits `description`.",
}


def load_rules():
    rules = [yaml.safe_load(p.read_text()) for p in sorted((ROOT / "rules").glob("*/rule.yml"))]
    return [r for r in rules if r["detector"] == "jev" and r["id"] not in SKIP]


def unit_kind(unit):
    if unit.startswith("#"):
        return "heading"
    if re.match(r"^([-*+]|\d+\.)\s", unit):
        return "list-item"
    return "sentence"


def matched(rule, unit):
    """対象なら、match に当たった箇所（絞り込みがなければ空文字）を返す。対象外なら None。"""
    if unit_kind(unit) not in rule["subject"]:
        return None
    m = rule.get("match") or {}
    for rx in m.get("regex_any", []) + ([m["regex"]] if "regex" in m else []):
        hit = re.search(rx, unit)
        if hit:
            return hit.group(0)
    return None if ("regex" in m or "regex_any" in m) else ""


def base(rule, description, hit, lang):
    q = {"question": QUESTION[(lang, bool(hit))], "description": description}
    if hit:
        q["matched"] = hit
    if rule.get("words"):
        q["words"] = [w["text"] for w in rule["words"]]
    return q


def questions(rule, hit, shape, lang):
    """(枝番, 質問) の列を返す。criteria は1つ、atomic は記述の数だけ。"""
    applies = [a["text"] for a in rule["applies"]]
    excludes = [e["text"] for e in rule.get("excludes", [])]
    if shape == "criteria":
        q = {"type": "noul", "instructions": base(rule, applies, hit, lang), "criteria": {"true": {"what": applies}}}
        if excludes:
            q["criteria"]["false"] = {"what": excludes}
        return [("c", q)]
    return [(f"a{n}", {"type": "noul", "instructions": base(rule, t, hit, lang)}) for n, t in enumerate(applies)] + \
           [(f"e{n}", {"type": "noul", "instructions": base(rule, t, hit, lang)}) for n, t in enumerate(excludes)]


def run_config(config, rules, problems):
    shape, arm, lang = config.split("-")
    jobs = []
    for p in problems:
        for i, u in enumerate(p["units"]):
            qs = {}
            for r in rules:
                hit = matched(r, u)
                if hit is None:
                    continue
                for tag, q in questions(r, hit, shape, lang):
                    qs[f"{p['id']}|{i}|{r['id']}|{tag}"] = q
            if qs:
                state = {"target": u} if arm == "bare" else {"report": p["text"], "target": u}
                jobs.append((state, qs))

    def one(job):
        state, qs = job
        keys = list(qs)
        status, sec, data = ask(state, {f"q{n}": qs[k] for n, k in enumerate(keys)})
        if status != 200:
            raise SystemExit(f"{config} status={status} {data}")
        return {k: data["answers"][f"q{n}"]["noul"] for n, k in enumerate(keys)}, data["usage"]["input_tokens"], sec

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, jobs))
    scores = {k: v for r in results for k, v in r[0].items()}
    tokens = sum(r[1] for r in results)
    secs = sorted(r[2] for r in results)
    meta = {"requests": len(jobs), "questions": len(scores), "input_tokens": tokens, "median_sec": secs[len(secs) // 2], "max_sec": secs[-1]}
    OUT.mkdir(exist_ok=True)
    (OUT / f"scores_{config}.json").write_text(json.dumps({"meta": meta, "scores": scores}, ensure_ascii=False))
    print(f"{config:22s} リクエスト={len(jobs)} 質問={len(scores)} 入力トークン={tokens} 費用=${tokens / 1e6 * PRICE_PER_MTOK:.4f} 応答中央値={secs[len(secs) // 2]:.2f}s")


def load_problems():
    problems = []
    for path in sorted((ROOT / "corpus" / "reports").glob("*.json")):
        p = json.loads(path.read_text())
        labels = json.loads((ROOT / "corpus" / "labels" / path.name).read_text())["labels"]
        p["labels"] = [set(x["rules"]) for x in labels]
        p["reasons"] = [x.get("reasons", {}) for x in labels]
        problems.append(p)
    return problems


def composed(config):
    """(報告, 単位, ルール) ごとの値を返す。atomic は合成する。"""
    raw = json.loads((OUT / f"scores_{config}.json").read_text())
    grouped = {}
    for k, v in raw["scores"].items():
        pid, i, rid, tag = k.split("|")
        grouped.setdefault((pid, int(i), rid), {})[tag] = v
    out = {}
    for key, tags in grouped.items():
        if "c" in tags:
            out[key] = tags["c"]
        else:
            a = [v for t, v in tags.items() if t.startswith("a")]
            e = [v for t, v in tags.items() if t.startswith("e")]
            out[key] = sum(a) / len(a) * (1 - max(e, default=0.0))
    return raw["meta"], out


def evaluate(values, problems, rule_ids):
    """ルールごとに、F1が最大になるしきい値と、そのときの数を返す。"""
    rows = {}
    for rid in rule_ids:
        asked = {(k[0], k[1]): v for k, v in values.items() if k[2] == rid}
        positives = [(p["id"], i) for p in problems for i in range(len(p["units"])) if rid in p["labels"][i]]
        pos = sorted(asked[u] for u in positives if u in asked)
        neg = sorted((v for u, v in asked.items() if u not in set(positives)), reverse=True)
        best, tp, fp = None, 0, 0
        if pos:
            def f1(t):
                a, b = sum(v >= t for v in pos), sum(v >= t for v in neg)
                return 2 * a / (2 * a + b + len(positives) - a)
            best = max(sorted(set(pos)), key=f1)
            tp, fp = sum(v >= best for v in pos), sum(v >= best for v in neg)
        f = 2 * tp / (2 * tp + fp + len(positives) - tp) if tp else 0.0
        rows[rid] = {"asked": len(asked), "positives": len(positives), "unasked": len(positives) - len(pos), "threshold": best,
                     "detected": tp + fp, "tp": tp, "fp": fp, "fn": len(positives) - tp, "f1": f,
                     "pos": pos, "neg_max": neg[0] if neg else 0.0, "asked_values": asked}
    return rows


def cmd_run():
    rules, problems = load_rules(), load_problems()
    for config in CONFIGS:
        run_config(config, rules, problems)


def cmd_report():
    rules, problems = load_rules(), load_problems()
    rule_ids = [r["id"] for r in rules]
    units = sum(len(p["units"]) for p in problems)
    total_pos = sum(len(l & set(rule_ids)) for p in problems for l in p["labels"])
    print(f"報告={len(problems)} 単位={units} Jevルール={len(rule_ids)} 正解の該当数={total_pos}")
    print("しきい値は、ルールごとに、正解と照らして F1 が最大になる値。同じデータで決めて同じデータで数えている。\n")
    results = {}
    print(f"{'組み立て':22s} {'リクエスト':>6s} {'質問':>6s} {'入力トークン':>9s} {'報告1件あたり':>8s}  {'検出':>5s} {'正検出':>4s} {'誤検出':>5s} {'見逃し':>4s}")
    for config in CONFIGS:
        meta, values = composed(config)
        rows = evaluate(values, problems, rule_ids)
        results[config] = rows
        s = {k: sum(r[k] for r in rows.values()) for k in ("detected", "tp", "fp", "fn")}
        print(f"{config:22s} {meta['requests']:8d} {meta['questions']:7d} {meta['input_tokens']:11d} {meta['input_tokens'] // len(problems):10d}  {s['detected']:6d} {s['tp']:5d} {s['fp']:6d} {s['fn']:5d}")

    print("\nルールごとに、F1が最も高かった組み立てを選んだ場合（正解の該当が1個以上あるルール）")
    print(f"{'rule':28s} {'正解':>3s}  {'組み立て':22s} {'しきい値':>5s} {'検出':>4s} {'正検出':>3s} {'誤検出':>3s} {'見逃し':>3s} {'未質問':>3s}  該当の値   非該当の最大")
    tot = {"detected": 0, "tp": 0, "fp": 0, "fn": 0}
    lines = ["# 正解とJevの判定が食い違った箇所（ルールごとに最もよかった組み立て）", ""]
    unit_text = {(p["id"], i): (u, p["reasons"][i]) for p in problems for i, u in enumerate(p["units"])}
    labels = {(p["id"], i): p["labels"][i] for p in problems for i in range(len(p["units"]))}
    for rid in rule_ids:
        best_config = max(CONFIGS, key=lambda c: (results[c][rid]["f1"], -results[c][rid]["fp"]))
        r = results[best_config][rid]
        if not r["positives"]:
            continue
        for k in tot:
            tot[k] += r[k]
        rng = f"{r['pos'][0]:.2f}-{r['pos'][-1]:.2f}" if r["pos"] else "-"
        th = f"{r['threshold']:.2f}" if r["threshold"] is not None else "-"
        print(f"{rid:28s} {r['positives']:4d}  {best_config:22s} {th:>7s} {r['detected']:5d} {r['tp']:5d} {r['fp']:5d} {r['fn']:5d} {r['unasked']:5d}  {rng:>9s}  {r['neg_max']:.2f}")
        for u, v in sorted(r["asked_values"].items(), key=lambda x: -x[1]):
            hit = r["threshold"] is not None and v >= r["threshold"]
            if hit != (rid in labels[u]):
                text, reasons = unit_text[u]
                lines += [f"## {rid}  {'誤検出' if hit else '見逃し'}  値 {v:.2f} / しきい値 {r['threshold']}  ({best_config})",
                          f"- 場所: corpus/reports/{u[0]}.json の units[{u[1]}]", f"- 対象: {text}"] + ([f"- 正解の根拠: {reasons[rid]}"] if rid in reasons else []) + [""]
    print(f"{'合計':28s} {'':4s}  {'':22s} {'':7s} {tot['detected']:5d} {tot['tp']:5d} {tot['fp']:5d} {tot['fn']:5d}")

    print("\n正解の該当が0個のルール: 組み立てごとの、非該当の最大値")
    print(f"{'rule':28s} " + " ".join(f"{c.replace('criteria', 'cri').replace('atomic', 'ato').replace('located', 'loc'):>11s}" for c in CONFIGS))
    for rid in rule_ids:
        if results[CONFIGS[0]][rid]["positives"]:
            continue
        print(f"{rid:28s} " + " ".join(f"{results[c][rid]['neg_max']:11.2f}" for c in CONFIGS))
    (OUT / "disagreements.md").write_text("\n".join(lines))
    print("\n食い違いの一覧: scripts/out/disagreements.md")


if __name__ == "__main__":
    {"run": cmd_run, "report": cmd_report}[sys.argv[1]]()
