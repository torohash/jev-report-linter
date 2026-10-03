#!/usr/bin/env python3
"""Jevの動作確認スクリプト（Issue #1）。

使い方:
  python3 scripts/jev_check.py ping        疎通確認
  python3 scripts/jev_check.py billing     質問数と入力トークン数の関係
  python3 scripts/jev_check.py examples    rules.yaml の match / no_match 例の判定精度
  python3 scripts/jev_check.py scale       1リクエストの質問数を増やしたときの応答時間と上限
  python3 scripts/jev_check.py context     報告全文を state に入れ、対象の1文を指して問う形の確認
"""
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
PRICE_PER_MTOK = 0.042


def api_key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("TYPESAFE_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(".env に TYPESAFE_API_KEY がありません")


def ask(state, questions, timeout=120):
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read())
            status = res.status
    except urllib.error.HTTPError as e:
        data = {"error": e.read().decode(errors="replace")[:500]}
        status = e.code
    return status, time.monotonic() - started, data


def load_rules():
    return yaml.safe_load((ROOT / "rules" / "rules.yaml").read_text())["rules"]


def split_sentences(text: str):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        buf = ""
        for ch in line:
            buf += ch
            if ch in "。！？":
                out.append(buf.strip())
                buf = ""
        if buf.strip():
            out.append(buf.strip())
    return out


def cmd_ping():
    status, sec, data = ask(
        "テストは未実行です。",
        {"q": {"type": "noul", "instructions": "この文は、未確認や未確定といった表現を使っているか。"}},
    )
    print(json.dumps({"status": status, "sec": round(sec, 3), "data": data}, ensure_ascii=False, indent=2))


def cmd_billing():
    rules = load_rules()
    state = {"sentence": "例外が起きたときは、安全側に倒す。"}
    for n in (1, 2, 10, len(rules)):
        qs = {r["id"]: {"type": "noul", "instructions": r["question"]} for r in rules[:n]}
        status, sec, data = ask(state, qs)
        usage = data.get("usage", {})
        print(f"questions={n:3d} status={status} sec={sec:.3f} input_tokens={usage.get('input_tokens')}")


def cmd_examples():
    rules = load_rules()
    rows = []
    for r in rules:
        for expected, key in ((True, "match"), (False, "no_match")):
            for ex in r.get(key, []) or []:
                status, sec, data = ask(
                    {"sentence": ex},
                    {"q": {"type": "noul", "instructions": r["question"]}},
                )
                noul = data.get("answers", {}).get("q", {}).get("noul")
                rows.append({"rule": r["id"], "expected": expected, "noul": noul, "sec": round(sec, 3), "text": ex, "status": status})
                print(f"{r['id']:28s} expected={'Y' if expected else 'N'} noul={noul} {ex[:40]}")
    ok = [x for x in rows if x["noul"] is not None]
    for th in (0.3, 0.5, 0.7):
        correct = sum((x["noul"] >= th) == x["expected"] for x in ok)
        tp = sum(x["noul"] >= th and x["expected"] for x in ok)
        pos = sum(x["expected"] for x in ok)
        tn = sum(x["noul"] < th and not x["expected"] for x in ok)
        neg = len(ok) - pos
        print(f"threshold={th}: 正解 {correct}/{len(ok)}  該当例 {tp}/{pos}  非該当例 {tn}/{neg}")
    secs = sorted(x["sec"] for x in rows)
    print(f"応答時間 中央値={secs[len(secs)//2]:.3f}s 最大={secs[-1]:.3f}s 件数={len(rows)}")
    (ROOT / "scripts" / "out").mkdir(exist_ok=True)
    (ROOT / "scripts" / "out" / "examples.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))


def report_questions(sentences, rules):
    qs = {}
    for i in range(len(sentences)):
        for r in rules:
            qs[f"{i}:{r['id']}"] = {
                "type": "noul",
                "instructions": "対象の文は `sentences[%d]` である。%s" % (i, r["question"]),
            }
    return qs


def cmd_scale():
    rules = load_rules()
    sentences = split_sentences((ROOT / "scripts" / "sample_report.md").read_text())
    print(f"文の数={len(sentences)} ルール数={len(rules)}")
    for n in (1, 2, 5, 10, len(sentences)):
        n = min(n, len(sentences))
        qs = report_questions(sentences[:n], rules)
        status, sec, data = ask({"sentences": sentences}, qs, timeout=300)
        usage = data.get("usage", {})
        tok = usage.get("input_tokens")
        cost = f"${tok / 1e6 * PRICE_PER_MTOK:.5f}" if tok else "-"
        err = data.get("error", "")[:200]
        print(f"文={n:2d} 質問={len(qs):4d} status={status} sec={sec:.2f} input_tokens={tok} 費用={cost} {err}")
        if n == len(sentences):
            (ROOT / "scripts" / "out").mkdir(exist_ok=True)
            (ROOT / "scripts" / "out" / "scale_full.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
            if status == 200:
                show_hits(sentences, data)
            break


def show_hits(sentences, data, th=0.5):
    hits = {}
    for key, ans in data["answers"].items():
        i, rule = key.split(":", 1)
        if ans["noul"] >= th:
            hits.setdefault(int(i), []).append(f"{rule}={ans['noul']:.2f}")
    for i, s in enumerate(sentences):
        print(f"[{i}] {s}\n     -> {', '.join(hits.get(i, [])) or '該当なし'}")


def cmd_context():
    """文脈が要るルールを、報告全文つきと1文だけの両方で問う。"""
    rules = {r["id"]: r for r in load_rules()}
    sentences = split_sentences((ROOT / "scripts" / "sample_report.md").read_text())
    targets = ["sentence-end-repetition", "connective-mismatch", "demonstrative", "announce-only", "unconfirmed", "investigated-unresolved"]
    full = {
        f"{i}:{rid}": {"type": "noul", "instructions": "対象の文は `sentences[%d]` である。%s" % (i, rules[rid]["question"])}
        for i in range(len(sentences))
        for rid in targets
    }
    status, sec, data = ask({"sentences": sentences}, full)
    print(f"報告全文つき: status={status} sec={sec:.2f}")
    for i, s in enumerate(sentences):
        single = {rid: {"type": "noul", "instructions": rules[rid]["question"]} for rid in targets}
        _, _, d1 = ask({"sentence": s}, single)
        cells = []
        for rid in targets:
            a = data.get("answers", {}).get(f"{i}:{rid}", {}).get("noul")
            b = d1.get("answers", {}).get(rid, {}).get("noul")
            cells.append(f"{rid}={a:.2f}/{b:.2f}")
        print(f"[{i}] {s}\n     全文つき/1文のみ: {'  '.join(cells)}")


if __name__ == "__main__":
    cmds = {"ping": cmd_ping, "billing": cmd_billing, "examples": cmd_examples, "scale": cmd_scale, "context": cmd_context}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(__doc__)
    cmds[sys.argv[1]]()
