#!/usr/bin/env python3
"""corpus/ の報告でJevルールを測る。試しの計測用で、本体の実装ではない。

  python3 scripts/eval.py run      Jevに聞き、値を scripts/out/scores.json に保存する（3回）
  python3 scripts/eval.py report   保存した値を corpus/labels/ の正解と突き合わせて集計する
  python3 scripts/eval.py run-per-unit / report-per-unit   単位1つにつき1リクエストで聞く形（比較用、1回）

Jevへの渡し方:
- 報告1件につき、報告全文と単位の配列を state に1回だけ入れ、その報告への質問をまとめて載せる。
- 質問は rule.yml の applies と excludes の原文だけで作る。足す文は「対象の文は、次の記述に当たる。」と
  「次の記述に当たる場合は、該当しない。」の2つだけである。
- match を持つルールは、match に当たった単位だけを聞く。
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import yaml

from jev_check import PRICE_PER_MTOK, ROOT, ask

PASSES = 3
# 全ルール共通のしきい値。Jevの値がこれ以上なら検出とする。結果を見てルールごとに変えない。
THRESHOLD = 0.5
CHUNK = 150
# 静的に移すかどうかの判断待ち
SKIP = {"long-sentence"}
OUT = ROOT / "scripts" / "out"


def load_rules():
    rules = [yaml.safe_load(p.read_text()) for p in sorted((ROOT / "rules").glob("*/rule.yml"))]
    return [r for r in rules if r["detector"] == "jev" and r["id"] not in SKIP]


def unit_kind(unit):
    if unit.startswith("#"):
        return "heading"
    if re.match(r"^([-*+]|\d+\.)\s", unit):
        return "list-item"
    return "sentence"


def matches(rule, unit):
    if unit_kind(unit) not in rule["subject"]:
        return False
    m = rule.get("match") or {}
    if "regex" in m:
        return re.search(m["regex"], unit) is not None
    if "regex_any" in m:
        return any(re.search(rx, unit) for rx in m["regex_any"])
    return True


def question(rule, target):
    q = {
        "type": "noul",
        "instructions": {"対象の文": target, "判定": "対象の文は、次の記述に当たる。", "記述": [a["text"] for a in rule["applies"]]},
    }
    if rule.get("excludes"):
        q["criteria"] = {"false": {"判定": "次の記述に当たる場合は、該当しない。", "記述": [e["text"] for e in rule["excludes"]]}}
    return q


def ask_chunk(state, items):
    """items は (キー, 質問) の列。トークン上限を超えたら半分に割って聞き直す。"""
    status, sec, data = ask(state, {f"q{n}": q for n, (_, q) in enumerate(items)})
    if status == 400 and "max_tokens_exceeded" in str(data) and len(items) > 1:
        half = len(items) // 2
        a, b = ask_chunk(state, items[:half]), ask_chunk(state, items[half:])
        return {**a[0], **b[0]}, a[1] + b[1], a[2] + b[2], a[3] + b[3]
    if status != 200:
        raise SystemExit(f"status={status} {data}")
    values = {items[n][0]: data["answers"][f"q{n}"]["noul"] for n in range(len(items))}
    return values, data["usage"]["input_tokens"], 1, [sec]


def cmd_run(per_unit=False):
    """per_unit=False: 報告1件を state に1回入れ、対象の文を `units[i]` で指す。
    per_unit=True: 単位1つにつき1リクエストにし、対象の文そのものを state に入れる。"""
    rules = load_rules()
    jobs = []
    for path in sorted((ROOT / "corpus" / "reports").glob("*.json")):
        p = json.loads(path.read_text())
        if per_unit:
            for i, u in enumerate(p["units"]):
                items = [(f"{p['id']}|{i}|{r['id']}", question(r, "`target`")) for r in rules if matches(r, u)]
                if items:
                    jobs.append(({"report": p["text"], "target": u}, items))
            continue
        items = [(f"{p['id']}|{i}|{r['id']}", question(r, f"`units[{i}]`")) for i, u in enumerate(p["units"]) for r in rules if matches(r, u)]
        state = {"report": p["text"], "units": p["units"]}
        jobs += [(state, items[k:k + CHUNK]) for k in range(0, len(items), CHUNK)]
    scores, passes = {}, []
    for n in range(1 if per_unit else PASSES):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda j: ask_chunk(*j), jobs))
        tokens = sum(r[1] for r in results)
        reqs = sum(r[2] for r in results)
        secs = sorted(s for r in results for s in r[3])
        for values, *_ in results:
            for k, v in values.items():
                scores.setdefault(k, []).append(v)
        passes.append({"requests": reqs, "input_tokens": tokens, "median_sec": secs[len(secs) // 2], "max_sec": secs[-1]})
        print(f"{n + 1}回目: リクエスト={reqs} 入力トークン={tokens} 費用=${tokens / 1e6 * PRICE_PER_MTOK:.4f} 応答 中央値={secs[len(secs) // 2]:.2f}s 最大={secs[-1]:.2f}s")
    OUT.mkdir(exist_ok=True)
    (OUT / ("scores_per_unit.json" if per_unit else "scores.json")).write_text(json.dumps({"passes": passes, "rules": [r["id"] for r in rules], "scores": scores}, ensure_ascii=False))
    print(f"判定数={len(scores)}（1回あたり）")


def cmd_report(name="scores.json"):
    data = json.loads((OUT / name).read_text())
    rule_ids = data["rules"]
    mean = {k: sum(v) / len(v) for k, v in data["scores"].items()}
    spread = sorted(max(v) - min(v) for v in data["scores"].values())
    reports, labels, reasons = {}, {}, {}
    for path in sorted((ROOT / "corpus" / "reports").glob("*.json")):
        p = json.loads(path.read_text())
        reports[p["id"]] = p
        for x in json.loads((ROOT / "corpus" / "labels" / path.name).read_text())["labels"]:
            labels[(p["id"], x["unit"])] = set(x["rules"])
            reasons[(p["id"], x["unit"])] = x.get("reasons", {})
    units = list(labels)
    passes = data["passes"]
    tok = sum(p["input_tokens"] for p in passes) / len(passes)
    print(f"報告={len(reports)} 単位={len(units)} Jevルール={len(rule_ids)} 判定={len(mean)}")
    print(f"1回あたり: リクエスト={passes[0]['requests']} 入力トークン={tok:.0f} 費用=${tok / 1e6 * PRICE_PER_MTOK:.4f} 報告1件あたり={tok / len(reports):.0f}トークン")
    print(f"3回の値のぶれ（最大−最小）: 中央値={spread[len(spread) // 2]:.3f} 95%点={spread[int(len(spread) * 0.95)]:.3f} 最大={spread[-1]:.3f}")
    print()
    print(f"{'rule':28s} {'聞いた':>5s} {'正解':>4s} {'絞込漏れ':>5s} {'しきい値':>6s} {'検出':>4s} {'正検出':>4s} {'誤検出':>4s} {'見逃し':>4s}   該当の値の範囲  非該当の最大")
    total = [0, 0, 0, 0, 0]
    detail = []
    for rid in rule_ids:
        asked = {(k.split("|")[0], int(k.split("|")[1])): v for k, v in mean.items() if k.split("|")[2] == rid}
        positives = [u for u in units if rid in labels[u]]
        missed_by_match = [u for u in positives if u not in asked]
        pos = sorted(asked[u] for u in positives if u in asked)
        neg = sorted((v for u, v in asked.items() if rid not in labels[u]), reverse=True)

        def count(t):
            tp, fp = sum(v >= t for v in pos), sum(v >= t for v in neg)
            return tp, fp, len(positives) - tp

        best = THRESHOLD
        tp, fp, fn = count(best)
        for i, v in enumerate((len(positives), tp + fp, tp, fp, fn)):
            total[i] += v
        rng = f"{pos[0]:.2f}-{pos[-1]:.2f}" if pos else "-"
        th = f"{best:.2f}"
        print(f"{rid:28s} {len(asked):5d} {len(positives):4d} {len(missed_by_match):5d} {th:>8s} {tp + fp:4d} {tp:5d} {fp:5d} {fn:5d}   {rng:>12s}  {neg[0] if neg else 0:.2f}")
        for u, v in asked.items():
            hit = v >= best
            if hit != (rid in labels[u]):
                detail.append({"rule": rid, "kind": "誤検出" if hit else "見逃し", "value": round(v, 2), "threshold": best, "report": u[0], "unit": u[1],
                               "text": reports[u[0]]["units"][u[1]], "reason": reasons[u].get(rid)})
        for u in missed_by_match:
            detail.append({"rule": rid, "kind": "見逃し（絞り込みに当たらず）", "value": None, "threshold": best, "report": u[0], "unit": u[1],
                           "text": reports[u[0]]["units"][u[1]], "reason": reasons[u].get(rid)})
    print(f"\n合計: 正解の該当数={total[0]} 検出={total[1]} 正検出={total[2]} 誤検出={total[3]} 見逃し={total[4]}")
    print(f"しきい値は全ルール共通で {THRESHOLD}。結果を見てルールごとに選ぶことはしない。")
    lines = ["# 正解とJevの判定が食い違った箇所", ""]
    for d in sorted(detail, key=lambda d: (d["rule"], d["kind"], -(d["value"] or 0))):
        lines += [f"## {d['rule']}  {d['kind']}  値 {d['value']} / しきい値 {d['threshold']}",
                  f"- 場所: corpus/reports/{d['report']}.json の units[{d['unit']}]",
                  f"- 対象: {d['text']}"] + ([f"- 正解の根拠: {d['reason']}"] if d["reason"] else []) + [""]
    (OUT / "disagreements.md").write_text("\n".join(lines))
    print(f"食い違いの一覧: scripts/out/disagreements.md（{len(detail)}件）")


if __name__ == "__main__":
    {"run": cmd_run, "run-per-unit": lambda: cmd_run(True), "report": cmd_report, "report-per-unit": lambda: cmd_report("scores_per_unit.json")}[sys.argv[1]]()
