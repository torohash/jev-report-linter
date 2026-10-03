# 計測の記録

Jevが返した値の生データと、そこから作った一覧を置く。Jevを呼ばずに集計を再現するためのもの。

| ファイル | 中身 |
|---|---|
| `per-unit-eabe402.json` | コミット eabe402 のルールと聞き方で、報告30件（620単位）にJevルール35本を掛けたときの値。キーは `報告ID\|単位の添字\|ルールID`、値はJevが返した確率 |
| `clear-disagreements-eabe402.md` | Jevが明瞭に答えた（0.3未満、または0.7以上）のに、正解ラベルと食い違った文の一覧。ルールの記述、前後の文、正解ラベルの根拠つき |
| `per-unit-issue6.json` | #6 で3本のルールの聞き方を変えたあとの値。キーの2つ目は、単位の添字のほかに、段落（`p番号`）と報告全体（`doc`）がある |
| `clear-disagreements-issue6.md` | `per-unit-issue6.json` から作った、同じ形の一覧 |
| `per-unit-issue6-40.json` | 自作の報告10件（p31〜p40）を足した40件（790単位）での値。`direct-translation` に該当の記述を4つ足したあとのルールで測った |
| `clear-disagreements-issue6-40.md` | `per-unit-issue6-40.json` から作った、同じ形の一覧 |

## 聞き方（eabe402）

- 単位1つにつき1リクエスト。`state` に報告全文（`report`）と対象の文（`target`）を入れる
- 質問はルール1本につき1つ。`rule.yml` の `applies` と `excludes` の原文だけで作る
- `match` を持つルールは、`match` に当たった単位だけを聞く。`subject` に含まれない種類の単位は聞かない
- リクエストを組み立てているのは `scripts/eval.py` の `question()` と `cmd_run()`

## 聞き方（#6 での変更）

eabe402 から、次の3本だけを変えた。ほかの32本は同じ。

| ルール | 変更 |
|---|---|
| `abstract-only` | 文1つずつではなく、報告全体に1回聞く |
| `paragraph-topic` | 文1つずつではなく、段落に聞く。該当の記述を、同じ出典の「1つの段落に無関係な話題が混ざったり」に変えた |
| `passive-agent` | 「れる」「られる」の形を含む文だけに聞く |

30件のファイル2つを集計すると、「正解のうち未質問」の列に p31〜p40 のラベルが数えられる。30件の時点の数字ではない。

`per-unit-eabe402.json` は eabe402 のルールで測った値である。いまの `rules/` と合わせて集計すると、上の3本は記述や対象が食い違う。

## 再現

```bash
# Jevを呼ばずに、保存した値から集計する（ファイル名を省くと per-unit-issue6-40.json）
python3 scripts/eval.py bands
python3 scripts/eval.py bands per-unit-eabe402.json

# Jevに聞き直す（.env に TYPESAFE_API_KEY が要る。40件で1回あたり約220万トークン、約9セント）
python3 scripts/eval.py run-per-unit
python3 scripts/eval.py report-per-unit
```
