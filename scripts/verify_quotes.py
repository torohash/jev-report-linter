#!/usr/bin/env python3
"""rules/*/rule.yml の applies・excludes・basis が、出典の原文と一字一句同じかを確かめる。

使い方:
  YOMIYASU_DIR=<yomiyasu を置いたディレクトリ> python3 scripts/verify_quotes.py
"""
import os
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main():
    yomiyasu = os.environ.get("YOMIYASU_DIR")
    if not yomiyasu:
        raise SystemExit("YOMIYASU_DIR を指定してください")
    cache, bad, count = {}, [], 0
    for path in sorted((ROOT / "rules").glob("*/rule.yml")):
        rule = yaml.safe_load(path.read_text())
        for item in rule.get("applies", []) + rule.get("words", []) + rule.get("excludes", []) + rule["basis"]:
            src = item["source"]
            file = ROOT / src if src == "README.md" else pathlib.Path(yomiyasu) / src.removeprefix("yomiyasu/")
            if file not in cache:
                cache[file] = file.read_text()
            count += 1
            if item["text"] not in cache[file]:
                bad.append(f"{rule['id']}: {src} に見つからない: {item['text'][:60]}")
    for line in bad:
        print("NG", line)
    print(f"ルール {len(list((ROOT / 'rules').glob('*/rule.yml')))} 本、引用 {count} 件、不一致 {len(bad)} 件")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
