import argparse
import csv
import hashlib
import json
import time

import requests
from deep_translator import GoogleTranslator

from common import ensure_db_initialized, get_connection, load_config, now_iso, setup_logger

logger = setup_logger("logs/errors.log")

ITEM_SEP = " / "
CSV_HEADER = [
    "Word", "POS", "Gender", "Aspect", "PairedVerb",
    "Meanings_RU", "Meanings_JA",
    "Collocations_RU", "Collocations_JA",
    "Examples_RU", "Examples_JA",
    "Accent",
]

# --- LLM用プロンプト ---
SYSTEM_PROMPT = (
    "You are a meticulous Japanese editor. Polish the raw machine-translated Japanese text "
    "into natural dictionary-style Japanese. Do not add information. "
    "Keep the exact number of ' / '-separated items. Output valid JSON only."
)

USER_PROMPT_TEMPLATE = """\
# Task
Polish the machine-translated Japanese for the Russian word "{word}".

# Russian original
Meanings_RU: {meanings_ru}
Collocations_RU: {collocations_ru}
Examples_RU: {examples_ru}

# Raw machine translation
Meanings_JA: {meanings_ja_mt}
Collocations_JA: {collocations_ja_mt}
Examples_JA: {examples_ja_mt}

# Output JSON format
{{"Meanings_JA": "...", "Collocations_JA": "...", "Examples_JA": "..."}}
"""


# ---------------------------------------------------------------------------
# DB読み込み
# ---------------------------------------------------------------------------
def get_summary(db_path, word):
    """summaries テーブルから最新1件取得"""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM summaries WHERE word = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (word,),
        ).fetchone()
    if not row:
        return None
    return {
        k: (row[k] or "")
        for k in [
            "word", "pos", "gender", "aspect", "paired_verb",
            "meanings_ru", "collocations_ru", "examples_ru", "accent",
        ]
    }


# ---------------------------------------------------------------------------
# 日本語訳キャッシュ（translations_ja テーブル）
# ---------------------------------------------------------------------------
def ensure_translation_cache_table(db_path):
    """translations_ja テーブルが無ければ作成する。
    Google翻訳結果(mt_*)とLLM整形後の結果(ja_*)を両方保存し、
    ロシア語原文が変わらない限り再翻訳・再整形しない（速度・コスト対策）。"""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS translations_ja (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                word            TEXT NOT NULL,
                source_hash     TEXT NOT NULL,  -- RU原文(Meanings/Collocations/Examples)のハッシュ
                meanings_mt     TEXT,           -- Google翻訳結果（整形前）
                collocations_mt TEXT,
                examples_mt     TEXT,
                meanings_ja     TEXT,           -- 最終採用値（LLM整形後 or MTそのまま）
                collocations_ja TEXT,
                examples_ja     TEXT,
                mt_engine       TEXT,
                polish_model    TEXT,           -- LLM整形に使ったモデル名（未整形なら空文字）
                created_at      TEXT NOT NULL,
                UNIQUE(word, source_hash)
            )
            """
        )
        conn.commit()


def compute_source_hash(meanings_ru, collocations_ru, examples_ru):
    payload = "\x1f".join([meanings_ru or "", collocations_ru or "", examples_ru or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_translation_from_cache(db_path, word, source_hash):
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT meanings_ja, collocations_ja, examples_ja "
            "FROM translations_ja WHERE word = ? AND source_hash = ?",
            (word, source_hash),
        ).fetchone()
    if row is None:
        return None
    return {
        "Meanings_JA": row["meanings_ja"] or "",
        "Collocations_JA": row["collocations_ja"] or "",
        "Examples_JA": row["examples_ja"] or "",
    }


def save_translation_to_cache(db_path, word, source_hash, mt_fields, ja_fields, mt_engine, polish_model):
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO translations_ja (
                word, source_hash,
                meanings_mt, collocations_mt, examples_mt,
                meanings_ja, collocations_ja, examples_ja,
                mt_engine, polish_model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(word, source_hash) DO UPDATE SET
                meanings_mt = excluded.meanings_mt,
                collocations_mt = excluded.collocations_mt,
                examples_mt = excluded.examples_mt,
                meanings_ja = excluded.meanings_ja,
                collocations_ja = excluded.collocations_ja,
                examples_ja = excluded.examples_ja,
                mt_engine = excluded.mt_engine,
                polish_model = excluded.polish_model,
                created_at = excluded.created_at
            """,
            (
                word, source_hash,
                mt_fields["Meanings_JA"], mt_fields["Collocations_JA"], mt_fields["Examples_JA"],
                ja_fields["Meanings_JA"], ja_fields["Collocations_JA"], ja_fields["Examples_JA"],
                mt_engine, polish_model, now_iso(),
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 1段階目: 機械翻訳（Google翻訳）
# ---------------------------------------------------------------------------
def _translate_one(text, translator, max_retries=3, delay=1.0):
    """1項目をGoogle翻訳にかける。失敗時はリトライし、最終的に例外を送出する。"""
    text = text.strip()
    if not text:
        return ""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            result = translator.translate(text)
            return (result or "").strip()
        except Exception as e:  # noqa: BLE001
            last_error = e
            logger.warning(
                "formatter: 機械翻訳に失敗 (attempt %d/%d) text=%r error=%s",
                attempt, max_retries, text[:50], e,
            )
            if attempt < max_retries:
                time.sleep(delay * attempt)
    raise RuntimeError(f"機械翻訳に失敗しました: {text!r}") from last_error


def translate(text, translator, delay=1.0):
    """" / " 区切りの項目ごとに翻訳し、再び " / " で結合する。"""
    if not text:
        return ""
    items = [t.strip() for t in text.split(ITEM_SEP) if t.strip()]
    translated = []
    for item in items:
        translated.append(_translate_one(item, translator, delay=delay))
        time.sleep(delay)  # 無料エンドポイントへの配慮（レート制限回避）
    return ITEM_SEP.join(t for t in translated if t)


# ---------------------------------------------------------------------------
# 2段階目: ローカルLLMによる日本語の整形
# ---------------------------------------------------------------------------
def polish_llm(word, ru, mt, llm_config):
    """Ollamaで日本語を整形する（`llm_config` は config.json の `polish_llm` セクション、
    つまり summarize.py 用モデルとは別の日本語整形専用モデルの設定を想定）。
    失敗時はリトライし、最終的に例外を送出する（呼び出し側でmtへのフォールバックを行う想定）。"""
    prompt = USER_PROMPT_TEMPLATE.format(
        word=word,
        meanings_ru=ru["meanings_ru"], collocations_ru=ru["collocations_ru"], examples_ru=ru["examples_ru"],
        meanings_ja_mt=mt["Meanings_JA"], collocations_ja_mt=mt["Collocations_JA"], examples_ja_mt=mt["Examples_JA"],
    )
    payload = {
        "model": llm_config["model"],
        "think": False,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "format": {
            "type": "object",
            "properties": {
                "Meanings_JA": {"type": "string"},
                "Collocations_JA": {"type": "string"},
                "Examples_JA": {"type": "string"},
            },
            "required": ["Meanings_JA", "Collocations_JA", "Examples_JA"],
        },
        "options": {
            "temperature": llm_config.get("temperature", 0.0),
            "num_predict": llm_config.get("max_tokens", 1024),
        },
    }
    url = f"{llm_config['base_url']}/api/chat"
    max_retries = max(1, llm_config.get("max_retries", 2))
    timeout = llm_config.get("timeout_seconds", 120)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            res = requests.post(url, json=payload, timeout=timeout)
            res.raise_for_status()
            content = res.json()["message"]["content"]
            return json.loads(content)
        except Exception as e:  # noqa: BLE001
            last_error = e
            logger.warning(
                "formatter: LLM整形呼び出しに失敗 (attempt %d/%d) word=%s error=%s",
                attempt, max_retries, word, e,
            )
            if attempt < max_retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"LLM整形に失敗しました: word={word}") from last_error


# ---------------------------------------------------------------------------
# オーケストレーション: 機械翻訳 → (オプションで)LLM整形 → キャッシュ
# ---------------------------------------------------------------------------
def get_polish_llm_config(cfg):
    """日本語整形専用のLLM設定を取得する。
    `polish_llm` セクションが無い古い config.json との互換のため、
    無ければ summarize.py 用の `llm` セクションにフォールバックする。"""
    return cfg.get("polish_llm") or cfg["llm"]


def translate_and_polish(word, ru, cfg, translator, translation_cfg, only_mt=False):
    db_path = cfg["database"]["path"]
    delay = translation_cfg.get("request_delay_seconds", 1.0)
    polish_enabled = translation_cfg.get("polish_with_llm", True) and not only_mt

    source_hash = compute_source_hash(ru["meanings_ru"], ru["collocations_ru"], ru["examples_ru"])

    cached = load_translation_from_cache(db_path, word, source_hash)
    if cached is not None:
        return cached

    mt = {
        "Meanings_JA": translate(ru["meanings_ru"], translator, delay),
        "Collocations_JA": translate(ru["collocations_ru"], translator, delay),
        "Examples_JA": translate(ru["examples_ru"], translator, delay),
    }

    ja = dict(mt)
    polish_model = ""
    if polish_enabled and any(mt.values()):
        polish_llm_config = get_polish_llm_config(cfg)
        try:
            ja = polish_llm(word, ru, mt, polish_llm_config)
            polish_model = polish_llm_config["model"]
        except Exception as e:  # noqa: BLE001
            logger.warning("formatter: LLM整形に失敗、機械翻訳の結果を採用 word=%s error=%s", word, e)
            ja = dict(mt)

    save_translation_to_cache(db_path, word, source_hash, mt, ja, mt_engine="google", polish_model=polish_model)
    return ja


def write_csv(rows, output_file, use_bom=True):
    encoding = "utf-8-sig" if use_bom else "utf-8"
    with open(output_file, "w", newline="", encoding=encoding) as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="scraping.py / summarize.py が保存したDBの内容を、日本語訳付きでCSVに整形して出力するツール"
    )
    parser.add_argument("--startidx", type=int, default=1, help="開始行 (1始まり)")
    parser.add_argument("--endidx", type=int, default=100, help="終了行")
    parser.add_argument("--input", type=str, default="words.txt", help="入力ファイル名")
    parser.add_argument("--output", type=str, default=None, help="出力CSVファイル名（省略時は config.json の設定値）")
    parser.add_argument(
        "--no-translate", action="store_true",
        help="日本語訳を付けず、ロシア語原文のみでCSVを出力する（動作確認・高速テスト用）",
    )
    parser.add_argument(
        "--onlyMT", action="store_true",
        help="日本語訳はGoogle翻訳の結果のみを採用し、LLMによる整形ステップを完全にスキップする"
             "（LLM(Ollama)サーバを起動していない場合や、LLM整形の要否を検証したい場合に使用）",
    )
    args = parser.parse_args()

    cfg = load_config()
    db_path = cfg["database"]["path"]
    ensure_db_initialized(db_path)

    translation_cfg = cfg.get("translation", {
        "enabled": True, "source_lang": "ru", "target_lang": "ja",
        "request_delay_seconds": 1.0, "polish_with_llm": True,
    })
    translate_enabled = translation_cfg.get("enabled", True) and not args.no_translate

    output_file = args.output or cfg["pipeline"]["output_file"]
    use_bom = cfg["pipeline"]["csv_bom"]

    translator = None
    if translate_enabled:
        ensure_translation_cache_table(db_path)
        translator = GoogleTranslator(
            source=translation_cfg.get("source_lang", "ru"),
            target=translation_cfg.get("target_lang", "ja"),
        )
        mode = "Google翻訳のみ（LLM整形スキップ / --onlyMT）" if args.onlyMT else "Google翻訳 + LLM整形"
        print(f"日本語訳モード: {mode}")

    with open(args.input, "r", encoding="utf-8") as f:
        words = [line.strip() for line in f if line.strip()]

    # 範囲指定 (1始まりを0始まりのリストインデックスへ変換)
    start = max(0, args.startidx - 1)
    end = min(len(words), args.endidx)
    target_words = words[start:end]

    rows = []
    for i, word in enumerate(target_words, start=start + 1):
        print(f"[{i}/{len(words)}] 処理中: {word}")

        ru = get_summary(db_path, word)
        if not ru:
            print(f"  -> DBに見つからないためスキップ: {word}")
            continue  # DBになければスキップ（エラー行は出力しない）

        ja = {"Meanings_JA": "", "Collocations_JA": "", "Examples_JA": ""}
        if translate_enabled:
            try:
                ja = translate_and_polish(word, ru, cfg, translator, translation_cfg, only_mt=args.onlyMT)
            except Exception as e:  # noqa: BLE001
                logger.warning("formatter: 翻訳処理に失敗 word=%s error=%s", word, e)

        rows.append({
            "Word": ru["word"], "POS": ru["pos"], "Gender": ru["gender"],
            "Aspect": ru["aspect"], "PairedVerb": ru["paired_verb"],
            "Meanings_RU": ru["meanings_ru"], "Meanings_JA": ja["Meanings_JA"],
            "Collocations_RU": ru["collocations_ru"], "Collocations_JA": ja["Collocations_JA"],
            "Examples_RU": ru["examples_ru"], "Examples_JA": ja["Examples_JA"],
            "Accent": ru["accent"],
        })

    write_csv(rows, output_file, use_bom=use_bom)
    print(f"完了: {len(rows)}件を '{output_file}' に書き出しました。")


if __name__ == "__main__":
    main()
