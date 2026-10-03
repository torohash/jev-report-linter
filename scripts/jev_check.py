#!/usr/bin/env python3
"""JevのAPIを呼ぶ最小の部品。`python3 scripts/jev_check.py` で疎通確認をする。"""
import json
import pathlib
import time
import urllib.error
import urllib.request

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
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
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


if __name__ == "__main__":
    status, sec, data = ask(
        "テストは未実行です。",
        {"q": {"type": "noul", "instructions": "This sentence says that something has not been confirmed."}},
    )
    print(json.dumps({"status": status, "sec": round(sec, 3), "data": data}, ensure_ascii=False, indent=2))
