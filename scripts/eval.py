#!/usr/bin/env python3
"""corpus/ の報告でJevルールを測る。試しの計測用で、本体の実装ではない。

  python3 scripts/eval.py run      Jevに聞き、値を scripts/out/scores.json に保存する（3回）
  python3 scripts/eval.py report   保存した値を corpus/labels/ の正解と突き合わせて集計する
  python3 scripts/eval.py run-per-unit / report-per-unit   単位1つにつき1リクエストで聞く形（比較用、1回）
  python3 scripts/eval.py bands [ファイル名]   measurements/ に保存した値を、明瞭なNo・不確か・明瞭なYesに分けて数える（Jevを呼ばない）

Jevへの渡し方:
- 報告1件につき、報告全文と単位の配列を state に1回だけ入れ、その報告への質問をまとめて載せる。
- 質問は rule.yml の applies と excludes の原文だけで作る。足す文は「対象の文は、次の記述に当たる。」と
  「次の記述に当たる場合は、該当しない。」の2つだけである。
- match を持つルールは、match に当たった単位だけを聞く。
- subject が paragraph のルールは段落に、document のルールは報告全体に聞く。足す文の「対象の文」は「対象の段落」「対象の報告」になる。
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


# 判定の対象の種類ごとの、質問での呼び方
NOUN = {"paragraph": "段落", "document": "報告"}


def targets(p):
    """報告1件から、判定の対象を (対象ID, 種類, 文面, 含まれる単位の添字) の列で返す。

    単位は添字、段落は `p番号`、報告全体は `doc` を対象IDにする。
    段落は報告全文を空行で区切った塊で、文を2つ以上含むものだけを対象にする。"""
    out = [(str(i), unit_kind(u), u, [i]) for i, u in enumerate(p["units"])]
    blocks = [b for b in re.split(r"\n\s*\n", p["text"]) if b.strip()]
    inside, pos = {}, 0
    for i, u in enumerate(p["units"]):
        for b in range(pos, len(blocks)):
            if u in blocks[b]:
                inside.setdefault(b, []).append(i)
                pos = b
                break
    for b, idx in inside.items():
        if sum(unit_kind(p["units"][i]) == "sentence" for i in idx) >= 2:
            out.append((f"p{b}", "paragraph", blocks[b], idx))
    out.append(("doc", "document", p["text"], list(range(len(p["units"])))))
    return out


def truth(p, label):
    """対象ごとの正解を、ラベルのファイル1件から作る。

    段落と報告全体は、ラベルの `targets` に付いたルールを正解にする。
    `targets` がないファイルでは、含まれる単位のどれかに付いていれば該当とする。"""
    units = [set(x["rules"]) for x in label["labels"]]
    own = label.get("targets", {})
    return {tid: (kind, set(own[tid]["rules"]) if tid in own else set().union(*(units[i] for i in idx))) for tid, kind, _, idx in targets(p)}


def matches(rule, unit, kind=None):
    if (kind or unit_kind(unit)) not in rule["subject"]:
        return False
    m = rule.get("match") or {}
    if "regex" in m:
        return re.search(m["regex"], unit) is not None
    if "regex_any" in m:
        return any(re.search(rx, unit) for rx in m["regex_any"])
    return True


def question(rule, target, kind="sentence"):
    noun = NOUN.get(kind, "文")
    q = {
        "type": "noul",
        "instructions": {f"対象の{noun}": target, "判定": f"対象の{noun}は、次の記述に当たる。", "記述": [a["text"] for a in rule["applies"]]},
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
    """per_unit=False: 報告1件を state に1回入れ、対象を `units[i]`（段落は `paragraphs[対象ID]`、報告全体は `report`）で指す。
    per_unit=True: 対象1つにつき1リクエストにし、対象の文面そのものを state に入れる。報告全体が対象のときは `report` を指す。"""
    rules = load_rules()
    jobs = []
    for path in sorted((ROOT / "corpus" / "reports").glob("*.json")):
        p = json.loads(path.read_text())
        if per_unit:
            for tid, kind, text, _ in targets(p):
                doc = kind == "document"
                items = [(f"{p['id']}|{tid}|{r['id']}", question(r, "`report`" if doc else "`target`", kind)) for r in rules if matches(r, text, kind)]
                if items:
                    jobs.append(({"report": p["text"]} if doc else {"report": p["text"], "target": text}, items))
            continue
        ref = {"paragraph": "`paragraphs[{}]`", "document": "`report`"}
        items = [(f"{p['id']}|{tid}|{r['id']}", question(r, ref.get(kind, "`units[{}]`").format(tid), kind))
                 for tid, kind, text, _ in targets(p) for r in rules if matches(r, text, kind)]
        state = {"report": p["text"], "units": p["units"], "paragraphs": {tid: text for tid, kind, text, _ in targets(p) if kind == "paragraph"}}
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
    subject = {r["id"]: r["subject"] for r in load_rules()}
    mean = {k: sum(v) / len(v) for k, v in data["scores"].items()}
    spread = sorted(max(v) - min(v) for v in data["scores"].values())
    reports, labels, reasons, kinds, texts = {}, {}, {}, {}, {}
    for path in sorted((ROOT / "corpus" / "reports").glob("*.json")):
        p = json.loads(path.read_text())
        reports[p["id"]] = p
        label = json.loads((ROOT / "corpus" / "labels" / path.name).read_text())
        for x in label["labels"]:
            reasons[(p["id"], str(x["unit"]))] = x.get("reasons", {})
        for tid, x in label.get("targets", {}).items():
            reasons[(p["id"], tid)] = x.get("reasons", {})
        for tid, (kind, rules) in truth(p, label).items():
            labels[(p["id"], tid)], kinds[(p["id"], tid)] = rules, kind
        for tid, _, text, _ in targets(p):
            texts[(p["id"], tid)] = text
    units = list(labels)
    passes = data["passes"]
    tok = sum(p["input_tokens"] for p in passes) / len(passes)
    print(f"報告={len(reports)} 単位={sum(len(p['units']) for p in reports.values())} Jevルール={len(rule_ids)} 判定={len(mean)}")
    print(f"1回あたり: リクエスト={passes[0]['requests']} 入力トークン={tok:.0f} 費用=${tok / 1e6 * PRICE_PER_MTOK:.4f} 報告1件あたり={tok / len(reports):.0f}トークン")
    print(f"3回の値のぶれ（最大−最小）: 中央値={spread[len(spread) // 2]:.3f} 95%点={spread[int(len(spread) * 0.95)]:.3f} 最大={spread[-1]:.3f}")
    print()
    print(f"{'rule':28s} {'聞いた':>5s} {'正解':>4s} {'絞込漏れ':>5s} {'しきい値':>6s} {'検出':>4s} {'正検出':>4s} {'誤検出':>4s} {'見逃し':>4s}   該当の値の範囲  非該当の最大")
    total = [0, 0, 0, 0, 0]
    detail = []
    for rid in rule_ids:
        asked = {(k.split("|")[0], k.split("|")[1]): v for k, v in mean.items() if k.split("|")[2] == rid}
        positives = [u for u in units if rid in labels[u] and kinds[u] in subject[rid]]
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
                               "text": texts[u], "reason": reasons.get(u, {}).get(rid)})
        for u in missed_by_match:
            detail.append({"rule": rid, "kind": "見逃し（絞り込みに当たらず）", "value": None, "threshold": best, "report": u[0], "unit": u[1],
                           "text": texts[u], "reason": reasons.get(u, {}).get(rid)})
    print(f"\n合計: 正解の該当数={total[0]} 検出={total[1]} 正検出={total[2]} 誤検出={total[3]} 見逃し={total[4]}")
    print(f"しきい値は全ルール共通で {THRESHOLD}。結果を見てルールごとに選ぶことはしない。")
    lines = ["# 正解とJevの判定が食い違った箇所", ""]
    for d in sorted(detail, key=lambda d: (d["rule"], d["kind"], -(d["value"] or 0))):
        lines += [f"## {d['rule']}  {d['kind']}  値 {d['value']} / しきい値 {d['threshold']}",
                  f"- 場所: corpus/reports/{d['report']}.json の {d['unit']}（数字は units の添字、p番号は段落、doc は報告全体）",
                  f"- 対象: {d['text']}"] + ([f"- 正解の根拠: {d['reason']}"] if d["reason"] else []) + [""]
    (OUT / "disagreements.md").write_text("\n".join(lines))
    print(f"食い違いの一覧: scripts/out/disagreements.md（{len(detail)}件）")


LOW, HIGH = 0.3, 0.7


def cmd_bands(name="per-unit-issue6-40.json"):
    """Jevの値を、明瞭なNo（LOW未満）・不確か・明瞭なYes（HIGH以上）に分けて数える。

    measurements/ に保存した値を読むので、Jevを呼ばずに再現できる。
    明瞭な答えが正解ラベルと食い違う文は scripts/out/clear_disagreements.md に書き出す。"""
    scores = json.loads((ROOT / "measurements" / name).read_text())["scores"]
    rules = {r["id"]: r for r in load_rules()}
    reports, labels, tr, tg = {}, {}, {}, {}
    for path in sorted((ROOT / "corpus" / "reports").glob("*.json")):
        p = json.loads(path.read_text())
        reports[p["id"]] = p
        label = json.loads((ROOT / "corpus" / "labels" / path.name).read_text())
        labels[p["id"]] = {x["unit"]: x for x in label["labels"]}
        tr[p["id"]] = truth(p, label)
        tg[p["id"]] = {tid: (kind, text) for tid, kind, text, _ in targets(p)}
    by = {}
    for k, v in scores.items():
        pid, tid, rid = k.split("|")
        by.setdefault(rid, []).append((sum(v) / len(v), pid, tid, rid in tr[pid][tid][1]))
    print(f"明瞭なNo: {LOW}未満 / 不確か: {LOW}以上{HIGH}未満 / 明瞭なYes: {HIGH}以上。数字は対象（文・段落・報告）の数。")
    print(f"{'rule':28s} {'聞いた':>5s} {'明瞭No':>5s} {'不確か':>5s} {'明瞭Yes':>5s} | 明瞭Yesで正解該当 / 非該当 | 明瞭Noで正解該当 | 不確かで正解該当 | 正解のうち未質問")
    lines = ["# Jevが明瞭に答えたのに、正解ラベルと食い違う文", "",
             f"明瞭なNoは{LOW}未満、明瞭なYesは{HIGH}以上。値はJevが返した確率。", ""]
    for rid in sorted(rules, key=lambda r: -sum(LOW <= x[0] < HIGH for x in by.get(r, []))):
        xs = by.get(rid, [])
        lo = [x for x in xs if x[0] < LOW]
        mid = [x for x in xs if LOW <= x[0] < HIGH]
        hi = [x for x in xs if x[0] >= HIGH]
        asked = {(x[1], x[2]) for x in xs}
        unasked = sum(rid in rs and kind in rules[rid]["subject"] and (pid, tid) not in asked for pid, ts in tr.items() for tid, (kind, rs) in ts.items())
        hy = sum(x[3] for x in hi)
        print(f"{rid:28s} {len(xs):5d} {len(lo):6d} {len(mid):6d} {len(hi):6d} | {hy:8d} / {len(hi) - hy:<8d} | {sum(x[3] for x in lo):8d} | {sum(x[3] for x in mid):8d} | {unasked:6d}")
        for kind, group in (("明瞭なYes・正解は該当なし", [x for x in hi if not x[3]]), ("明瞭なNo・正解は該当", [x for x in lo if x[3]])):
            if not group:
                continue
            r = rules[rid]
            lines += [f"## {rid}  {kind}  {len(group)}件", "",
                      "該当の記述: " + " / ".join(a["text"] for a in r["applies"])]
            if r.get("excludes"):
                lines.append("除外の記述: " + " / ".join(e["text"] for e in r["excludes"]))
            lines.append("")
            for v, pid, tid, _ in sorted(group, key=lambda x: (x[0], x[1], int(x[2]) if x[2].isdigit() else -1), reverse=True):
                if not tid.isdigit():
                    lines += [f"### {v:.2f}  corpus/reports/{pid}.json {tid}（{NOUN[tg[pid][tid][0]]}）", f"- **対象の{NOUN[tg[pid][tid][0]]}: {tg[pid][tid][1]}**", ""]
                    continue
                units, i = reports[pid]["units"], int(tid)
                lines += [f"### {v:.2f}  corpus/reports/{pid}.json units[{i}]",
                          f"- 前の文: {units[i - 1] if i else '（なし）'}",
                          f"- **対象の文: {units[i]}**",
                          f"- 次の文: {units[i + 1] if i + 1 < len(units) else '（なし）'}"]
                reason = labels[pid][i].get("reasons", {}).get(rid)
                if reason:
                    lines.append(f"- 正解ラベルの根拠: {reason}")
                lines.append("")
    OUT.mkdir(exist_ok=True)
    (OUT / "clear_disagreements.md").write_text("\n".join(lines))
    print("\n食い違いの一覧: scripts/out/clear_disagreements.md")


if __name__ == "__main__":
    {"run": cmd_run, "run-per-unit": lambda: cmd_run(True), "report": cmd_report, "report-per-unit": lambda: cmd_report("scores_per_unit.json"), "bands": lambda: cmd_bands(*sys.argv[2:3])}[sys.argv[1]]()
