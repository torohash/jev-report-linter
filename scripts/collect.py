#!/usr/bin/env python3
"""GitHub上の公開Issue/PRから、Claudeが日本語で書いた完了報告を集める。

対象は claude[bot] が投稿した「Claude finished」コメント。1リポジトリ所有者につき1件だけ採る。
結果は scripts/problems/<id>.json に、出典URL・本文・判定単位（units）を保存する。

使い方:
  python3 scripts/collect.py [件数]
"""
import json
import re
import subprocess
import sys
import time

from jev_check import ROOT

QUERIES = [
    '"Claude finished" 完了しました in:comments',
    '"Claude finished" 修正しました in:comments',
    '"Claude finished" 実装しました in:comments',
    '"Claude finished" 原因 in:comments',
    '"Claude finished" 確認しました in:comments',
    '"Claude finished" レビュー in:comments',
]
MIN_CHARS, MAX_CHARS = 300, 2500
MAX_UNITS = 40


def gh(*args):
    out = subprocess.run(["gh", "api", "-X", "GET", *args], capture_output=True, text=True)
    if out.returncode != 0:
        print("gh error:", out.stderr.strip()[:200], file=sys.stderr)
        return None
    return json.loads(out.stdout)


def japanese_ratio(text: str) -> float:
    body = re.sub(r"\s", "", text)
    return len(re.findall(r"[ぁ-んァ-ヶ]", body)) / max(len(body), 1)


def clean(body: str) -> str:
    """ボットが付ける定型のヘッダーとリンクを落とし、報告本文だけを残す。"""
    lines = body.replace("\r\n", "\n").split("\n")
    kept = []
    for line in lines:
        s = line.strip()
        if re.search(r"Claude finished @", s) or re.search(r"\[View job\]|\[View job run\]|\[Create PR", s):
            continue
        if s.startswith("<img") or s.startswith("<!--"):
            continue
        kept.append(line.rstrip())
    text = "\n".join(kept)
    text = re.sub(r"^\s*---\s*$", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_units(text: str):
    """報告を判定の単位に分ける。コードブロックと表は対象外。見出しと箇条書きは1行を1単位、地の文は句点で分ける。"""
    units, in_code = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code or not s or s.startswith("|") or s.startswith(">"):
            continue
        if s.startswith("#") or re.match(r"^([-*+]|\d+\.)\s", s):
            units.append(s)
            continue
        buf = ""
        for ch in s:
            buf += ch
            if ch in "。！？":
                units.append(buf.strip())
                buf = ""
        if buf.strip():
            units.append(buf.strip())
    return units


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    out_dir = ROOT / "scripts" / "problems"
    out_dir.mkdir(exist_ok=True)
    seen_owners, saved = set(), 0
    for q in QUERIES:
        for page in (1, 2):
            res = gh("search/issues", "-f", f"q={q}", "-f", "per_page=30", "-f", f"page={page}", "-f", "sort=created", "-f", "order=desc")
            time.sleep(2.5)
            for item in (res or {}).get("items", []):
                owner = item["repository_url"].split("/")[-2]
                if owner in seen_owners:
                    continue
                comments = gh(item["comments_url"].replace("https://api.github.com/", ""), "-f", "per_page=50") or []
                for c in comments:
                    if c["user"]["login"] != "claude[bot]" or "Claude finished" not in c["body"]:
                        continue
                    text = clean(c["body"])
                    units = split_units(text)
                    if not (MIN_CHARS <= len(text) <= MAX_CHARS) or japanese_ratio(text) < 0.25 or not (6 <= len(units) <= MAX_UNITS):
                        continue
                    saved += 1
                    seen_owners.add(owner)
                    pid = f"p{saved:02d}"
                    (out_dir / f"{pid}.json").write_text(json.dumps(
                        {"id": pid, "source": c["html_url"], "text": text, "units": units}, ensure_ascii=False, indent=1))
                    print(pid, len(units), "units", c["html_url"])
                    break
                if saved >= target:
                    return


if __name__ == "__main__":
    main()
