-- ============================================================
-- dictionary.db 初期化スクリプト
-- ロシア語単語帳自動生成システム用のキャッシュ/中間データテーブル
-- ============================================================

PRAGMA journal_mode = WAL;   -- 並列書き込み時の競合を減らす

-- ----------------------------------------------------------
-- raw_data: scraping.py が取得した生データのキャッシュ
--   キー: (word, source_url)
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_data (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    word        TEXT NOT NULL,
    source_url  TEXT NOT NULL,
    raw_html    TEXT,             -- 取得した生wikitext（デバッグ・再パース用途、任意。カラム名は互換性のため raw_html のまま）
    extracted   TEXT NOT NULL,    -- XPathで抽出したテキストをJSON文字列化したもの
    fetched_at  TEXT NOT NULL,    -- ISO8601形式のタイムスタンプ
    status      TEXT NOT NULL DEFAULT 'ok',  -- 'ok' / 'not_found' / 'error'
    UNIQUE(word, source_url)
);

CREATE INDEX IF NOT EXISTS idx_raw_data_word ON raw_data(word);

-- ----------------------------------------------------------
-- summaries: summarize.py がLLMで構造化した結果のキャッシュ
--   キー: (word, prompt_hash)
--   prompt_hash はプロンプトテンプレート＋raw_dataの内容のハッシュ値。
--   プロンプトやraw_dataが変わった場合は別キャッシュとして扱われ、
--   古いキャッシュを誤って再利用しない。
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    word            TEXT NOT NULL,
    prompt_hash     TEXT NOT NULL,
    model           TEXT NOT NULL,
    pos             TEXT,             -- 品詞
    gender          TEXT,             -- 性（名詞のみ）
    aspect          TEXT,             -- 体（動詞のみ）
    paired_verb     TEXT,             -- 完了体/不完了体のペア動詞
    meanings_ru     TEXT,             -- 意味（ロシア語原文）
    collocations_ru TEXT,             -- コロケーション（ロシア語原文）
    examples_ru     TEXT,             -- 例文（ロシア語原文）
    accent          TEXT,             -- アクセント位置情報
    raw_llm_output  TEXT,             -- LLMの生出力（デバッグ用）
    created_at      TEXT NOT NULL,
    UNIQUE(word, prompt_hash)
);

CREATE INDEX IF NOT EXISTS idx_summaries_word ON summaries(word);

-- ----------------------------------------------------------
-- run_errors: パイプライン実行時のエラーログ（DB上にも残す）
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    word        TEXT NOT NULL,
    stage       TEXT NOT NULL,    -- 'scraping' / 'summarize' / 'formatter'
    message     TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
