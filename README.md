# ロシア語単語帳自動生成システム

複数のロシア語辞書・翻訳サイトから単語データを取得し、Ollama経由のLLM（ロシア語に強い
Vikhr-Nemo-12B-Instruct）で構造化データ（CSV）に変換するシステムです。

## パイプライン

```
words.txt
   │
   ▼
[scraping.py]   ソースごとに2方式で取得・抽出
                 - mediawiki_wikitext方式: ru.wiktionary等のMediaWikiサイトから
                   action=parse&prop=wikitext で生wiki記法を取得し、正規表現で
                   セクション・テンプレートを解釈
                 - html方式: multitran・reverso等の一般サイトから通常HTMLを取得し、
                   config.json の field_map（正規表現の複数フォールバック）で抽出
                 → SQLite raw_data テーブルにキャッシュ (キー: word, source_url)
   │
   ▼
[summarize.py]  Ollama (Vikhr-Nemo-12B-Instruct, temperature=0.0) でJSONオブジェクト
                 1つに構造化。ノイズ込みの生データをそのまま渡し、ノイズの除去・
                 意味の切り分けはLLMの言語理解に委ねる（詳細は下記「設計方針」）。
                 → SQLite summaries テーブルにキャッシュ (キー: word, prompt_hash)
   │
   ▼
[formatter.py]  vocab.csv として出力 (UTF-8 with BOM)
   │
   ▲
[main.py]       ThreadPoolExecutor で単語ごとに並列実行、config.json 読込、
                 エラーハンドリング (logs/errors.log + DB run_errors テーブル)
```

## 設計方針: パース精度の一部をLLMに委譲する

`scraping.py` の正規表現パーサだけで、MediaWikiテンプレート（`{{семантика|...}}` 等）の
あらゆる折り返しパターンや表記揺れを100%決定論的に処理しようとすると、際限なく
パーサのメンテナンスコストが増えていく（実際に、テンプレート引数の残骸が意味の
定義文に混入するバグが起きた）。

一方、ロシア語に強いLLM（Vikhr-Nemo-12B-Instruct）は「テンプレート除去の残骸らしき
ゴミを無視する」「意味の区切りを正しく判断する」といった言語理解が要る作業を、
正規表現よりずっと頑健にこなせる。

そこで本システムでは、`scraping.py` 側では過度に厳密なクリーンアップを追い求めず、
多少ノイズ・記法の残骸が残っていてもよいテキストを抽出し、意味的な整形・ノイズ除去は
`summarize.py` のプロンプトで明示的にLLM側の仕事として指示している
（`USER_PROMPT_TEMPLATE` 内の "Handling noisy input" セクション）。

ただし **「ソースにない事実を作らない」というハルシネーション禁止の原則は変えない**。
ノイズの除去・整形（フォーマットの正規化）は許容するが、情報の捏造・翻訳の追加などは
禁止、という線引きをプロンプトで明示している。

## セットアップ

```bash
pip install -r requirements.txt
```

Ollamaを起動し、モデルをダウンロードしておいてください。

```bash
ollama pull hf.co/bartowski/Vikhr-Nemo-12B-Instruct-R-21-09-24-GGUF:Q4_K_M
```

> **なぜVikhr-Nemoか**: 当初 `IlyaGusev/saiga2_13b_gguf` を使用していたが、GGUF内の
> `tokenizer.chat_template` メタデータが正しく埋め込まれておらず、Ollamaが誤った
> プロンプトフォーマットでモデルを呼び出してしまい、出力が破綻する問題
> （文字化け・フォーマット崩れ）が発生した。`chat_template` が正しく設定されている
> `bartowski/Vikhr-Nemo-12B-Instruct-R-21-09-24-GGUF`（ロシア語特化、Mistral-Nemo
> ベース、12B）に切り替えて解消した。

DBは初回実行時に `main.py` が自動的に `init_db.sql` を使って作成します
（手動で作る場合は `sqlite3 dictionary.db < init_db.sql`）。

## 実行方法

```bash
python main.py --input words.txt --output vocab.csv
```

`--startidx` / `--endidx` で処理範囲を指定できます（1始まり）。

```bash
python main.py --input words.txt --output vocab.csv --startidx 3 --endidx 10
```

## config.json の主な設定項目

| キー | 説明 |
|---|---|
| `database.path` | SQLiteファイルパス（デフォルト: `dictionary.db`） |
| `llm.model` | Ollamaモデル名（デフォルト: Vikhr-Nemo-12B-Instruct） |
| `llm.temperature` | 生成温度（ハルシネーション防止のため `0.0` 推奨） |
| `llm.max_tokens` | 最大トークン数（JSON出力はCSVよりキー名の分長くなるため `1536` 推奨） |
| `scraping.sources` | 辞書・翻訳サイトソースのリスト。複数サイトを自由に追加可能 |
| `pipeline.max_workers` | 並列実行数 |
| `pipeline.on_error` | `keep_as_error_row`（エラーもCSVに残す）または `exclude`（除外） |

### スクレイピングソースの追加方法

`config.json` の `scraping.sources` に以下の形式でオブジェクトを追加します。
`source_type` によって取得・抽出方式が変わります。

**mediawiki_wikitext方式**（MediaWiki上の辞書サイト向け。ru.wiktionary等）:

```json
{
  "name": "ru_wiktionary",
  "source_type": "mediawiki_wikitext",
  "url_template": "https://ru.wiktionary.org/wiki/{word}",
  "api_url_template": "https://ru.wiktionary.org/w/api.php",
  "enabled": true,
  "field_map": {
    "meaning_list": "meaning_definitions",
    "example_list": "examples",
    "collocation_list": "list:Фразеологизмы",
    "synonyms": "list:Синонимы",
    "etymology": "etymology"
  },
  "timeout_seconds": 15,
  "max_retries": 2,
  "request_delay_seconds": 1.0
}
```

**html方式**（それ以外の一般的な辞書・翻訳サイト向け。デフォルト）:

```json
{
  "name": "multitran",
  "source_type": "html",
  "url_template": "https://www.multitran.com/m.exe?s={word}&l1=2&l2=9",
  "enabled": true,
  "field_map": {
    "translations": [
      "<td[^>]*class=\"[^\"]*subj[^\"]*\"[^>]*>.*?</td>\\s*<td[^>]*>\\s*(?:<a[^>]*>)?([^<]+)(?:</a>)?"
    ]
  },
  "timeout_seconds": 15,
  "max_retries": 2,
  "request_delay_seconds": 1.5
}
```

`field_map` の値は正規表現の文字列またはリスト（先頭からフォールバックで試行）で、
マッチした内容がテキストのリストとして `summarize.py` に渡される。
サイトのHTML構造は変更されやすいため、複数パターンを指定してフォールバックさせる
ことで多少の耐性を持たせている。抽出結果には多少のノイズが残ることを許容し、
最終的な意味の整形はLLM側（`summarize.py`）に委ねる設計。

## 出力CSVのカラム

| カラム | 内容 | 言語 |
|---|---|---|
| Word | 見出し語 | ロシア語（キリル文字） |
| POS | 品詞 | 英語（メタデータ） |
| Gender | 性（名詞のみ） | 英語（メタデータ） |
| Aspect | 体（動詞のみ） | 英語（メタデータ） |
| PairedVerb | ペア動詞 | ロシア語 |
| Meanings_RU | 意味 | ロシア語原文 |
| Collocations_RU | コロケーション | ロシア語原文 |
| Examples_RU | 例文 | ロシア語原文 |
| Accent | アクセント情報 | ロシア語（記号付き） |

日本語訳は含まれません（言語混在によるノイズ防止のため）。

## LLM呼び出しの仕組み

`summarize.py` はOllamaネイティブの `/api/chat` エンドポイントを、`format` パラメータに
JSON Schemaを指定して呼び出す。これによりデコーディング自体が指定スキーマに従う
JSONのみに制約され（llama.cppのgrammar-constrained decoding）、モデルが指示を
「破る」形での出力崩れ（例: クォート抜けのCSV行でフィールドがズレる）を構造的に
防いでいる。

（OpenAI互換の `/v1/chat/completions` の `response_format` はバックエンド実装によって
無視されることがあるため、あえてOllamaネイティブAPIを使用している。）

LLM出力はJSONとしてパースした後、`Word`列は常に入力単語で上書きし、値が誤って
配列で返ってきた場合は `" / "` 区切りの文字列に変換するなど、軽い後処理を行っている
（`parse_json_object` 関数）。

## キャッシュ戦略

- **Scraping**: `(word, source_url)` キーでキャッシュ。`status='ok'` または
  `'not_found'`（恒久的な404等）のみキャッシュし、一時的なネットワークエラー等
  (`status='error'`) はキャッシュせず次回再試行します。
- **Summarize**: `(word, prompt_hash)` キーでキャッシュ。`prompt_hash` は
  プロンプトテンプレート＋スクレイピング結果＋モデル名から算出されるため、
  プロンプトやスクレイピング結果、使用モデルが変わると自動的に再生成されます。

## エラーハンドリング

各単語の各ステップ（scraping / summarize）で例外を捕捉し、
`logs/errors.log` と SQLite の `run_errors` テーブルの両方に記録します。
`pipeline.on_error` が `keep_as_error_row`（デフォルト）の場合、
失敗した単語は `POS` 列に `ERROR: ...` と記載された行としてCSVに残ります。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `common.py` | 設定読込・DB接続・ロギングの共通ユーティリティ |
| `scraping.py` | wikitext方式/HTML方式のスクレイピング + raw_dataキャッシュ |
| `summarize.py` | Ollama(Vikhr-Nemo-12B-Instruct)呼び出し + summariesキャッシュ |
| `formatter.py` | vocab.csv 出力 |
| `main.py` | 統合・並列実行エントリポイント |
| `init_db.sql` | SQLiteテーブル定義 |
| `config.json` | 設定ファイル |
| `words.txt` | 入力単語リストのサンプル |
