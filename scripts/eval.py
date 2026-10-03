#!/usr/bin/env python3
"""corpus/ の報告と正解ラベルで、Jevルール（rules/*/rule.yml の detector: jev）を測る。

- 対象の絞り込み（subject と match）はこのスクリプトが決め、絞った対象だけをJevに聞く。
- state は rule.yml の state に従う。bare は対象の文だけ、located は報告全文と位置を渡す。
- 正解ラベルはLLMが付けたもので、このスクリプトは正解を作らない。

使い方:
  python3 scripts/eval.py           測って集計を出す
  python3 scripts/eval.py --fit     集計に加え、しきい値を rule.yml に書き、実測を baseline.json に残す
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import yaml

from jev_check import PRICE_PER_MTOK, ROOT, ask

TASK = "Judge only the text identified below, and only for the statement given -- other problems with it are not your concern here."


def load_rules():
    rules = [yaml.safe_load(p.read_text()) for p in sorted((ROOT / "rules").glob("*/rule.yml"))]
    return [r for r in rules if r["detector"] == "jev"]


def unit_kind(unit: str) -> str:
    if unit.startswith("#"):
        return "heading"
    if re.match(r"^([-*+]|\d+\.)\s", unit):
        return "list-item"
    return "sentence"


def paragraph_ids(problem):
    """各 unit が何番目の段落にあるかを返す。空行と見出しで段落を区切る。"""
    ids, para, pos = [], 0, 0
    units = problem["units"]
    for block in re.split(r"\n\s*\n", problem["text"]):
        para += 1
        while pos < len(units) and units[pos] in block and len(ids) == pos:
            ids.append(para)
            block = block.split(units[pos], 1)[1]
            pos += 1
    ids += [para] * (len(units) - len(ids))
    return ids


def matched(rule, problem, i, paras, kinds):
    """対象なら、モデルに渡す一致箇所（なければ空文字）を返す。対象外なら None。"""
    if kinds[i] not in rule["subject"]:
        return None
    m = rule.get("match") or {}
    if "regex" in m:
        hit = re.search(m["regex"], problem["units"][i])
        return hit.group(0) if hit else None
    if m.get("position") == "first":
        first = next((k for k, kind in enumerate(kinds) if kind == "sentence"), None)
        return "" if i == first else None
    if m.get("position") == "last-paragraph":
        return "" if paras[i] == paras[-1] else None
    return ""


def build_state(rule, problem, i):
    units = problem["units"]
    if rule["state"] == "bare":
        return {"text": units[i]}
    return {
        "report": problem["text"],
        "position": {"index": i + 1, "total": len(units), "is_first": i == 0, "is_last": i == len(units) - 1},
        "previous_sentences": units[max(0, i - 2):i],
        "text": units[i],
        "next_sentence": units[i + 1] if i + 1 < len(units) else None,
    }


def build_question(rule, hit):
    instructions = {"task": TASK, "statement": rule["ask"], "text_to_judge": "`text`"}
    if rule.get("note"):
        instructions["also"] = rule["note"]
    if hit:
        instructions["matched"] = hit
    return {"type": "noul", "instructions": instructions, "criteria": {"true": rule["criteria"]["true"], "false": rule["criteria"]["false"]}}


def run(requests):
    def one(req):
        status, sec, data = ask(req["state"], req["questions"])
        if status != 200:
            raise SystemExit(f"status={status} {data}")
        return sec, data["usage"]["input_tokens"], {k: v["noul"] for k, v in data["answers"].items()}

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, requests))


def main():
    fit = "--fit" in sys.argv
    rules = load_rules()
    by_id = {r["id"]: r for r in rules}
    problems = []
    for path in sorted((ROOT / "corpus" / "reports").glob("*.json")):
        problem = json.loads(path.read_text())
        labels = json.loads((ROOT / "corpus" / "labels" / path.name).read_text())["labels"]
        problem["expected"] = [set(x["rules"]) for x in labels]
        problems.append(problem)

    # 1巡目: after を持たないルール。対象ごと・state ごとに1リクエスト。
    scores, missed, requests, keys = {}, {r["id"]: 0 for r in rules}, [], []
    for p in problems:
        kinds = [unit_kind(u) for u in p["units"]]
        paras = paragraph_ids(p)
        p["kinds"], p["paras"] = kinds, paras
        for i in range(len(p["units"])):
            for arm in ("bare", "located"):
                qs = {}
                for r in rules:
                    if r["state"] != arm or (r.get("match") or {}).get("after"):
                        continue
                    hit = matched(r, p, i, paras, kinds)
                    if hit is None:
                        missed[r["id"]] += r["id"] in p["expected"][i]
                        continue
                    qs[r["id"]] = build_question(r, hit)
                if qs:
                    rule0 = next(r for r in rules if r["state"] == arm)
                    requests.append({"state": build_state(rule0, p, i), "questions": qs})
                    keys.append((p["id"], i))
    results = run(requests)
    for key, (_, _, nouls) in zip(keys, results):
        for rid, v in nouls.items():
            scores[(key[0], key[1], rid)] = v

    # 2巡目: after のルールは、先行ルールが0.5以上を返した対象にだけ聞く。
    requests2, keys2 = [], []
    for r in rules:
        dep = (r.get("match") or {}).get("after")
        if not dep:
            continue
        for p in problems:
            for i in range(len(p["units"])):
                if scores.get((p["id"], i, dep), 0) >= 0.5 and p["kinds"][i] in r["subject"]:
                    requests2.append({"state": build_state(r, p, i), "questions": {r["id"]: build_question(r, "")}})
                    keys2.append((p["id"], i))
                else:
                    missed[r["id"]] += r["id"] in p["expected"][i]
    results2 = run(requests2)
    for key, (_, _, nouls) in zip(keys2, results2):
        for rid, v in nouls.items():
            scores[(key[0], key[1], rid)] = v

    all_results = results + results2
    secs = sorted(x[0] for x in all_results)
    tokens = sum(x[1] for x in all_results)
    units = sum(len(p["units"]) for p in problems)
    print(f"報告={len(problems)} 単位={units} Jevルール={len(rules)} リクエスト={len(all_results)} 判定={len(scores)}")
    print(f"応答時間 中央値={secs[len(secs) // 2]:.2f}s 最大={secs[-1]:.2f}s  入力トークン={tokens} 費用=${tokens / 1e6 * PRICE_PER_MTOK:.4f} 報告1件あたり=${tokens / 1e6 * PRICE_PER_MTOK / len(problems):.5f}")
    print()
    print(f"{'rule':28s} {'聞いた':>5s} {'該当':>4s} {'絞込漏れ':>6s}  {'該当の値':>16s} {'非該当の最大':>8s}  {'しきい値':>6s}  拾えた  誤検出")
    text_of = {(p["id"], i): p["units"][i] for p in problems for i in range(len(p["units"]))}
    expected = {(p["id"], i): p["expected"][i] for p in problems for i in range(len(p["units"]))}
    total_tp = total_pos = total_fp = 0
    for r in rules:
        rid = r["id"]
        asked = [(k, v) for k, v in scores.items() if k[2] == rid]
        pos = sorted(v for k, v in asked if rid in expected[(k[0], k[1])])
        neg = sorted((v for k, v in asked if rid not in expected[(k[0], k[1])]), reverse=True)
        neg_max = neg[0] if neg else 0.0
        separable = bool(pos) and pos[0] > neg_max
        if separable:
            threshold, how = round((pos[0] + neg_max) / 2, 2), "隙間の中点"
        elif pos:
            # 分かれないときはF1が最大になる点を探し、精度か再現率が足りなければ無効にする
            def f1(t):
                tp, fp = sum(p >= t for p in pos), sum(n >= t for n in neg)
                return 2 * tp / (2 * tp + fp + (len(pos) - tp))
            best = max(sorted(set(pos)), key=f1)
            tp, fp = sum(p >= best for p in pos), sum(n >= best for n in neg)
            if tp / (tp + fp) >= 0.7 and tp / len(pos) >= 0.5:
                threshold, how = best, "分離不可（F1最大）"
            else:
                threshold, how = None, "分離不可（無効）"
        else:
            # 該当例がないので、問題なしの最大値より上に仮に置く
            threshold, how = round(max(0.6, neg_max + 0.1), 2), "該当例なし（仮）"
        tp = sum(p >= threshold for p in pos) if threshold is not None else 0
        fp = sum(n >= threshold for n in neg) if threshold is not None else 0
        total_tp += tp
        total_pos += len(pos) + missed[rid]
        total_fp += fp
        pos_range = f"{pos[0]:.2f}-{pos[-1]:.2f}" if pos else "-"
        th = f"{threshold:.2f}" if threshold is not None else "-"
        print(f"{rid:28s} {len(asked):5d} {len(pos):4d} {missed[rid]:6d}  {pos_range:>16s} {neg_max:10.2f}  {th:>6s}  {tp}/{len(pos) + missed[rid]:<4d}  {fp:3d}  {how}")
        if fit:
            top_neg = sorted(((v, text_of[(k[0], k[1])][:120]) for k, v in asked if rid not in expected[(k[0], k[1])]), reverse=True)[:5]
            base = {
                "fitted_on": "corpus/ 30 reports, single pass",
                "asked": len(asked), "positives": len(pos), "missed_by_matcher": missed[rid],
                "positive_scores": pos, "clean_max": neg_max, "threshold": threshold, "fit": how,
                "caught": tp, "false_positives": fp,
                "top_clean": [{"score": v, "text": t} for v, t in top_neg],
            }
            d = ROOT / "rules" / rid
            (d / "baseline.json").write_text(json.dumps(base, ensure_ascii=False, indent=1) + "\n")
            yml = (d / "rule.yml").read_text()
            yml = re.sub(r"^threshold: .*$", f"threshold: {threshold if threshold is not None else 'null'}  # {how}", yml, flags=re.M)
            (d / "rule.yml").write_text(yml)
    print(f"\n合計: 該当を拾えた {total_tp}/{total_pos}  誤検出 {total_fp}")


if __name__ == "__main__":
    main()
