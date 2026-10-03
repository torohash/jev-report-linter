# 計測の記録

Jevが返した値の生データと、そこから作った一覧を置く。Jevを呼ばずに集計を再現するためのもの。

| ファイル | 中身 |
|---|---|
| `per-unit-eabe402.json` | コミット eabe402 のルールと聞き方で、報告30件（620単位）にJevルール35本を掛けたときの値。キーは `報告ID\|単位の添字\|ルールID`、値はJevが返した確率 |
| `clear-disagreements-eabe402.md` | Jevが明瞭に答えた（0.3未満、または0.7以上）のに、正解ラベルと食い違った文の一覧。ルールの記述、前後の文、正解ラベルの根拠つき |

## 聞き方（eabe402）

- 単位1つにつき1リクエスト。`state` に報告全文（`report`）と対象の文（`target`）を入れる
- 質問はルール1本につき1つ。`rule.yml` の `applies` と `excludes` の原文だけで作る
- `match` を持つルールは、`match` に当たった単位だけを聞く。`subject` に含まれない種類の単位は聞かない
- リクエストを組み立てているのは `scripts/eval.py` の `question()` と `cmd_run()`

## 再現

```bash
# Jevを呼ばずに、保存した値から集計する
python3 scripts/eval.py bands

# Jevに聞き直す（.env に TYPESAFE_API_KEY が要る。1回あたり約177万トークン、約7セント）
python3 scripts/eval.py run-per-unit
python3 scripts/eval.py report-per-unit
```
