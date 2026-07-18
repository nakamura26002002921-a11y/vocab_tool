# ロシア語単語帳自動生成システム (XPath scraping + Saiga2 13B + SQLite cache)

Web上のロシア語辞書サイトからXPathで情報を取得し、Ollama経由のSaiga2 13Bで
構造化データ（CSV）に変換するシステムです。

## パイプライン

```
words.txt
   │
   ▼
[scraping.py]   requests + lxml(XPath) で辞書サイトから情報抽出
                 → SQLite raw_data テーブルにキャッシュ (キー: word, source_url)
   │
   ▼
[summarize.py]  Ollama (Saiga2 13B, temperature=0.0) でCSV1行に構造化
                 → SQLite summaries テーブルにキャッシュ (キー: word, prompt_hash)
   │
   ▼
[formatter.py]  vocab.csv として出力 (UTF-8 with BOM)
   │
   ▲
[main.py]       ThreadPoolExecutor で単語ごとに並列実行、config.json 読込、
                 エラーハンドリング (logs/errors.log + DB run_errors テーブル)
```

## セットアップ

```bash
pip install -r requirements.txt
```

Ollamaを起動し、モデルをダウンロードしておいてください。

```bash
ollama pull hf.co/IlyaGusev/saiga2_13b_gguf:model-q4_K.gguf
```

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
| `llm.model` | Ollamaモデル名（デフォルト: Saiga2 13B） |
| `llm.temperature` | 生成温度（ハルシネーション防止のため `0.0` 推奨） |
| `llm.max_tokens` | 最大トークン数（デフォルト `1024`） |
| `scraping.sources` | XPath辞書ソースのリスト。複数サイトを自由に追加可能 |
| `pipeline.max_workers` | 並列実行数 |
| `pipeline.on_error` | `keep_as_error_row`（エラーもCSVに残す）または `exclude`（除外） |

### XPathソースの追加方法

`config.json` の `scraping.sources` に以下の形式でオブジェクトを追加します。

```json
{
  "name": "my_source",
  "url_template": "https://example.com/dict/{word}",
  "enabled": true,
  "xpath_map": {
    "meaning_list": "//div[@class='meaning']//li"
  },
  "timeout_seconds": 15,
  "max_retries": 2,
  "request_delay_seconds": 1.0
}
```

`xpath_map` の各キーは任意の名前で、対応するXPath式が抽出するノードの
テキストがリストとして `summarize.py` に渡されます。

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

## キャッシュ戦略

- **Scraping**: `(word, source_url)` キーでキャッシュ。`status='ok'` または
  `'not_found'`（恒久的な404等）のみキャッシュし、一時的なネットワークエラー等
  (`status='error'`) はキャッシュせず次回再試行します。
- **Summarize**: `(word, prompt_hash)` キーでキャッシュ。`prompt_hash` は
  プロンプトテンプレート＋スクレイピング結果＋モデル名から算出されるため、
  プロンプトやスクレイピング結果が変わると自動的に再生成されます。

## エラーハンドリング

各単語の各ステップ（scraping / summarize）で例外を捕捉し、
`logs/errors.log` と SQLite の `run_errors` テーブルの両方に記録します。
`pipeline.on_error` が `keep_as_error_row`（デフォルト）の場合、
失敗した単語は `POS` 列に `ERROR: ...` と記載された行としてCSVに残ります。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `common.py` | 設定読込・DB接続・ロギングの共通ユーティリティ |
| `scraping.py` | XPathスクレイピング + raw_dataキャッシュ |
| `summarize.py` | Ollama(Saiga2 13B)呼び出し + summariesキャッシュ |
| `formatter.py` | vocab.csv 出力 |
| `main.py` | 統合・並列実行エントリポイント |
| `init_db.sql` | SQLiteテーブル定義 |
| `config.json` | 設定ファイル |
| `words.txt` | 入力単語リストのサンプル |
