#!/usr/bin/env python3
"""scripts/problems（実際の報告）と scripts/answers（正解ラベル）で、ルールごとの判定精度を測る。

正解ラベルはLLMが報告を読んで付けたもので、このスクリプトは正解を作らない。

Jevには1文につき1リクエストを送る。state には報告全文と、対象の文の位置（前後の文、冒頭か末尾か）
を入れ、文章全体の構造の中で対象の文を判定できるようにする。

使い方:
  python3 scripts/eval.py            全問を実行して集計を出す
  python3 scripts/eval.py --dump     文ごとの判定値も出す
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from jev_check import PRICE_PER_MTOK, ROOT, ask, load_rules

THRESHOLDS = (0.5, 0.6, 0.7, 0.8)
PREFIX = (
    "報告の全文は `report` にある。対象の文は `target_sentence` で、"
    "報告の中での位置は `position`、直前の文は `previous_sentences`、直後の文は `next_sentence` にある。"
)


def build_state(problem, i):
    units = problem["units"]
    return {
        "report": problem["text"],
        "position": {
            "index": i + 1,
            "total": len(units),
            "is_first_sentence": i == 0,
            "is_last_sentence": i == len(units) - 1,
        },
        "previous_sentences": units[max(0, i - 2):i],
        "target_sentence": units[i],
        "next_sentence": units[i + 1] if i + 1 < len(units) else None,
    }


def judge(problem, i, rules):
    questions = {r["id"]: {"type": "noul", "instructions": PREFIX + r["question"]} for r in rules}
    status, sec, data = ask(build_state(problem, i), questions)
    if status != 200:
        raise SystemExit(f"{problem['id']}[{i}] status={status} {data}")
    return sec, data["usage"]["input_tokens"], {k: v["noul"] for k, v in data["answers"].items()}


def load_jobs(rule_ids):
    jobs = []
    for path in sorted((ROOT / "scripts" / "problems").glob("*.json")):
        problem = json.loads(path.read_text())
        answer_path = ROOT / "scripts" / "answers" / path.name
        if not answer_path.exists():
            print(f"{problem['id']}: 正解がないので飛ばす", file=sys.stderr)
            continue
        labels = {x["unit"]: x["rules"] for x in json.loads(answer_path.read_text())["labels"]}
        if sorted(labels) != list(range(len(problem["units"]))):
            raise SystemExit(f"{problem['id']}: 正解の unit が問題の units と対応していません")
        unknown = {r for rs in labels.values() for r in rs} - set(rule_ids)
        if unknown:
            raise SystemExit(f"{problem['id']}: 未定義のルール: {unknown}")
        for i in range(len(problem["units"])):
            jobs.append({"problem": problem, "i": i, "expected": set(labels[i])})
    return jobs


def main():
    dump = "--dump" in sys.argv
    rules = load_rules()
    rule_ids = [r["id"] for r in rules]
    jobs = load_jobs(rule_ids)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: judge(j["problem"], j["i"], rules), jobs))

    secs = sorted(r[0] for r in results)
    tokens = sum(r[1] for r in results)
    problems = len({j["problem"]["id"] for j in jobs})
    print(f"問題数={problems} 文数={len(jobs)} ルール数={len(rules)} 判定数={len(jobs) * len(rules)}")
    print(f"応答時間 中央値={secs[len(secs) // 2]:.2f}s 最大={secs[-1]:.2f}s  入力トークン合計={tokens} 費用=${tokens / 1e6 * PRICE_PER_MTOK:.4f}")

    if dump:
        for j, (_, _, nouls) in zip(jobs, results):
            hits = sorted(((v, k) for k, v in nouls.items() if v >= 0.5 or k in j["expected"]), reverse=True)
            marks = [f"{k}={v:.2f}{'*' if k in j['expected'] else ''}" for v, k in hits]
            print(f"[{j['problem']['id']}:{j['i']}] {j['problem']['units'][j['i']][:80]}\n    {', '.join(marks) or '-'}")
        print("（* は正解で該当とされたルール）")

    print()
    print(f"{'rule':30s} {'該当':>4s} {'非該当':>5s}  該当の値(最小-中央-最大)  非該当の最大  " + "  ".join(f"@{t} 拾/誤" for t in THRESHOLDS))
    totals = {t: [0, 0, 0, 0] for t in THRESHOLDS}
    for rid in rule_ids:
        pos = sorted(r[2][rid] for j, r in zip(jobs, results) if rid in j["expected"])
        neg = [r[2][rid] for j, r in zip(jobs, results) if rid not in j["expected"]]
        cells = []
        for t in THRESHOLDS:
            tp = sum(p >= t for p in pos)
            fp = sum(n >= t for n in neg)
            totals[t][0] += tp
            totals[t][1] += len(pos)
            totals[t][2] += fp
            totals[t][3] += len(neg)
            cells.append(f"{tp}/{len(pos)} {fp:3d}")
        pos_range = f"{pos[0]:.2f}-{pos[len(pos) // 2]:.2f}-{pos[-1]:.2f}" if pos else "-"
        print(f"{rid:30s} {len(pos):4d} {len(neg):5d}  {pos_range:>22s}  {max(neg):10.2f}  " + "  ".join(f"{c:>9s}" for c in cells))
    print()
    for t in THRESHOLDS:
        tp, p, fp, n = totals[t]
        print(f"しきい値{t}: 該当を拾えた {tp}/{p}  誤検出 {fp}/{n}")

    out = ROOT / "scripts" / "out"
    out.mkdir(exist_ok=True)
    (out / "eval.json").write_text(json.dumps(
        [{"problem": j["problem"]["id"], "unit": j["i"], "text": j["problem"]["units"][j["i"]], "expected": sorted(j["expected"]), "nouls": r[2]} for j, r in zip(jobs, results)],
        ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
