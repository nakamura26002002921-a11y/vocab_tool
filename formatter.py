"""
formatter.py
------------
scraping.py / summarize.py が dictionary.db に保存した構造化データ（ロシア語原文）を、
最終的なCSVファイル（デフォルト: vocab.csv）として書き出すモジュール。

【日本語解説の生成方法（ハルシネーション対策）】
ロシア語に強いローカルLLM（Vikhr-Nemo等）に直接「ロシア語→日本語」の翻訳をさせると、
語彙力の薄い言語方向のため事実と異なる訳語をハルシネーションしやすい。
そのため、このモジュールでは2段階方式を取る。

  1. 機械翻訳（Google翻訳, deep-translator経由）でロシア語原文(*_RU)を
     日本語へ直訳する。事実関係はGoogle翻訳エンジンに委ね、LLMには翻訳させない。
  2. ローカルLLM（config.jsonのllm設定と同じモデル）には「機械翻訳結果を、
     ロシア語原文を参考にしつつ自然な日本語表現に整形する」ことだけを許可する。
     新しい情報を追加すること・項目を統合/削除することは禁止し、
     出力の" / "区切り項目数が入力と一致しない、または明らかに劣化している場合は
     機械翻訳の結果をそのまま採用する（LLM整形は "あってもなくても情報の正しさが
     変わらない" 範囲に限定するフェイルセーフ）。

- 文字コード: UTF-8 with BOM（Excel対応、config.jsonの pipeline.csv_bom で切替可）。
- カラム構成: メタデータ項目（Word, POS, Gender, Aspect, PairedVerb）、
  ロシア語原文とその日本語訳を項目ごとに隣接させた
  （Meanings_RU/Meanings_JA, Collocations_RU/Collocations_JA, Examples_RU/Examples_JA）、
  発音情報（Accent）というヘッダとする。
- エラー行（word のみでその他が "ERROR: ..." のもの）もそのまま1行として書き出す。
- 日本語訳（機械翻訳＋LLM整形の結果）は translations_ja テーブルにキャッシュし、
  ロシア語原文が変わらない限り再翻訳・再整形しない（速度・コスト対策）。

単体実行時は argparse による範囲指定付きのパイプライン実行が可能。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from typing import Iterable, Optional

import requests

from common import ensure_db_initialized, get_connection, load_config, now_iso, setup_logger

logger = setup_logger("logs/errors.log")

CSV_HEADER = [
    "Word",              # 見出し語（キリル文字）
    "POS",               # 品詞（メタデータ、英語表記）
    "Gender",            # 性（メタデータ、名詞のみ）
    "Aspect",            # 体（メタデータ、動詞のみ）
    "PairedVerb",        # ペア動詞（ロシア語）
    "Meanings_RU",       # 意味（ロシア語原文）
    "Meanings_JA",       # 意味（日本語訳: 機械翻訳+LLM整形）
    "Collocations_RU",   # コロケーション（ロシア語原文）
    "Collocations_JA",   # コロケーション（日本語訳）
    "Examples_RU",       # 例文（ロシア語原文）
    "Examples_JA",       # 例文（日本語訳）
    "Accent",            # アクセント情報
]

# " / " は summarize.py が複数項目を連結する際に使っている区切り文字と揃える
ITEM_SEP = " / "


# ---------------------------------------------------------------------------
# CSV書き出し
# ---------------------------------------------------------------------------
def write_csv(rows: Iterable[dict], output_path: str, use_bom: bool = True) -> None:
    """rows: CSV_HEADER のキーを持つdictのイテラブル。
    エラー行は {"Word": word, "POS": "ERROR: ...", 他は空} の形で渡されることを想定。
    """
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    encoding = "utf-8-sig" if use_bom else "utf-8"
    with open(output_path, "w", newline="", encoding=encoding) as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            safe_row = {field: row.get(field, "") for field in CSV_HEADER}
            writer.writerow(safe_row)


def make_error_row(word: str, message: str) -> dict:
    """エラー発生時のCSV行を作る。メタデータ以外の項目は空欄のまま、
    POS列に "ERROR: ..." を記録して原因を追跡できるようにする。"""
    row = {field: "" for field in CSV_HEADER}
    row["Word"] = word
    row["POS"] = f"ERROR: {message}"
    return row


# ---------------------------------------------------------------------------
# summaries テーブル（ロシア語原文）の読み込み
# ---------------------------------------------------------------------------
def load_summary_from_db(db_path: str, word: str) -> Optional[dict]:
    """summaries テーブルから、指定した単語の最新レコード（created_at が最大のもの）を取得する。
    同じ単語で prompt_hash が異なる（=再要約された）レコードが複数存在する場合があるため、
    最新のものだけを採用する。見つからなければ None を返す。"""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM summaries
            WHERE word = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (word,),
        ).fetchone()
    if row is None:
        return None
    return {
        "Word": row["word"],
        "POS": row["pos"] or "",
        "Gender": row["gender"] or "",
        "Aspect": row["aspect"] or "",
        "PairedVerb": row["paired_verb"] or "",
        "Meanings_RU": row["meanings_ru"] or "",
        "Collocations_RU": row["collocations_ru"] or "",
        "Examples_RU": row["examples_ru"] or "",
        "Accent": row["accent"] or "",
    }


# ---------------------------------------------------------------------------
# 日本語訳キャッシュ（translations_ja テーブル）
# ---------------------------------------------------------------------------
def ensure_translation_cache_table(db_path: str) -> None:
    """translations_ja テーブルが無ければ作成する（init_db.sqlは変更しない、自己完結の追加テーブル）。"""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS translations_ja (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                word            TEXT NOT NULL,
                source_hash     TEXT NOT NULL,  -- RU原文(Meanings/Collocations/Examples)のハッシュ。原文が変われば再翻訳。
                meanings_ja     TEXT,
                collocations_ja TEXT,
                examples_ja     TEXT,
                mt_engine       TEXT,           -- 機械翻訳エンジン名（例: 'google'）
                polish_model    TEXT,           -- LLM整形に使ったモデル名（未整形なら空文字）
                created_at      TEXT NOT NULL,
                UNIQUE(word, source_hash)
            )
            """
        )
        conn.commit()


def compute_source_hash(meanings_ru: str, collocations_ru: str, examples_ru: str) -> str:
    payload = "\x1f".join([meanings_ru or "", collocations_ru or "", examples_ru or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_translation_from_cache(db_path: str, word: str, source_hash: str) -> Optional[dict]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT meanings_ja, collocations_ja, examples_ja
            FROM translations_ja
            WHERE word = ? AND source_hash = ?
            """,
            (word, source_hash),
        ).fetchone()
    if row is None:
        return None
    return {
        "Meanings_JA": row["meanings_ja"] or "",
        "Collocations_JA": row["collocations_ja"] or "",
        "Examples_JA": row["examples_ja"] or "",
    }


def save_translation_to_cache(
    db_path: str, word: str, source_hash: str, ja_fields: dict, mt_engine: str, polish_model: str
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO translations_ja (
                word, source_hash, meanings_ja, collocations_ja, examples_ja,
                mt_engine, polish_model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(word, source_hash) DO UPDATE SET
                meanings_ja = excluded.meanings_ja,
                collocations_ja = excluded.collocations_ja,
                examples_ja = excluded.examples_ja,
                mt_engine = excluded.mt_engine,
                polish_model = excluded.polish_model,
                created_at = excluded.created_at
            """,
            (
                word, source_hash,
                ja_fields["Meanings_JA"], ja_fields["Collocations_JA"], ja_fields["Examples_JA"],
                mt_engine, polish_model, now_iso(),
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 1段階目: 機械翻訳（Google翻訳）
# ---------------------------------------------------------------------------
def _get_translator_class():
    """deep-translator は任意依存のため、未インストール時は分かりやすいエラーにする。"""
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator
    except ImportError as e:
        raise RuntimeError(
            "deep-translator がインストールされていません。"
            "`pip install deep-translator` を実行するか、"
            "config.json の translation.enabled を false にしてください。"
        ) from e


def build_translator(source_lang: str = "ru", target_lang: str = "ja"):
    GoogleTranslator = _get_translator_class()
    return GoogleTranslator(source=source_lang, target=target_lang)


def _mt_translate_one(text: str, translator, max_retries: int = 3, delay: float = 1.0) -> str:
    """1つの短いフレーズ・文をGoogle翻訳にかける。失敗時はリトライし、最終的に例外を送出する。"""
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


def machine_translate_field(text: str, translator, delay: float = 1.0) -> str:
    """" / " 区切りの項目ごとに翻訳し、再び " / " で結合する。
    項目単位で訳すことで、複数の意味・例文が1つの塊に潰れて訳されるのを防ぐ。"""
    if not text or not text.strip():
        return ""
    items = [t.strip() for t in text.split(ITEM_SEP) if t.strip()]
    translated_items = []
    for item in items:
        translated_items.append(_mt_translate_one(item, translator, delay=delay))
        time.sleep(delay)  # 無料の機械翻訳エンドポイントへの配慮（レート制限回避）
    return ITEM_SEP.join(t for t in translated_items if t)


def machine_translate_fields(ru_fields: dict, translator, delay: float = 1.0) -> dict:
    return {
        "Meanings_JA": machine_translate_field(ru_fields.get("Meanings_RU", ""), translator, delay),
        "Collocations_JA": machine_translate_field(ru_fields.get("Collocations_RU", ""), translator, delay),
        "Examples_JA": machine_translate_field(ru_fields.get("Examples_RU", ""), translator, delay),
    }


# ---------------------------------------------------------------------------
# 2段階目: ローカルLLMによる日本語の整形（翻訳はさせない、文章の自然さだけ直す）
# ---------------------------------------------------------------------------
POLISH_SYSTEM_PROMPT = (
    "You are a meticulous Japanese editor working on a Russian-Japanese vocabulary dictionary "
    "for Japanese learners of Russian. "
    "You receive (1) the original Russian text and (2) a raw machine translation of it into Japanese. "
    "Your ONLY job is to rewrite the raw machine translation into natural, dictionary-appropriate "
    "Japanese: fix awkward phrasing, unnatural word order, and literal-translation artifacts. "
    "You MUST NOT add any information, nuance, or detail that is not already present in the machine "
    "translation or the Russian original — you are polishing wording, not translating or explaining. "
    "You MUST preserve the exact number of ' / '-separated items in each field, in the same order; "
    "never merge, split, add, or drop items. "
    "If a raw machine-translated item is empty, garbled, or you cannot faithfully improve it, "
    "output it unchanged rather than inventing new content. "
    "You always respond with a single valid JSON object in the exact format requested, "
    "with no extra commentary, no markdown, no code fences, and no explanations."
)

POLISH_USER_TEMPLATE = """\
# Task
Polish the raw machine-translated Japanese text below for the Russian word "{word}", so that it reads \
as natural, dictionary-style Japanese for learners of Russian. Use the Russian original ONLY as a \
reference to catch mistranslations — do not add anything beyond what the machine translation / Russian \
original conveys.

Return a single JSON object with EXACTLY these 3 keys, in this order:
Meanings_JA, Collocations_JA, Examples_JA

# Russian original (reference only, do not translate from scratch)
Meanings_RU: {meanings_ru}
Collocations_RU: {collocations_ru}
Examples_RU: {examples_ru}

# Raw machine translation (this is what you must polish)
Meanings_JA (raw): {meanings_ja_mt}
Collocations_JA (raw): {collocations_ja_mt}
Examples_JA (raw): {examples_ja_mt}

# Strict rules
- Each field's output must have exactly the same number of " / "-separated items as its raw input.
- Only fix naturalness of the Japanese wording; do not add facts not present in the raw translation.
- If a raw field is empty (""), output an empty string "" for it — do not fabricate content.
- Output MUST be exactly one JSON object and nothing else — no text before or after it.

# Output (one JSON object only, e.g. {{"Meanings_JA": "...", "Collocations_JA": "...", "Examples_JA": "..."}}):
"""

_POLISH_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "Meanings_JA": {"type": "string"},
        "Collocations_JA": {"type": "string"},
        "Examples_JA": {"type": "string"},
    },
    "required": ["Meanings_JA", "Collocations_JA", "Examples_JA"],
}


def _extract_json_object(text: str) -> str:
    """テキストから最初の { に対応する } までを波括弧の深さを数えて切り出す
    （モデルが前後に余計な説明文を付けてきた場合の保険）。"""
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def _call_ollama_polish(system_prompt: str, user_prompt: str, llm_config: dict) -> Optional[str]:
    """summarize.py の call_ollama と同じ Ollama /api/chat 呼び出しパターンだが、
    整形用のJSON Schema（3フィールド）を使う点だけが異なる。"""
    url = f"{llm_config['base_url']}/api/chat"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": llm_config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "format": _POLISH_JSON_SCHEMA,
        "options": {
            "temperature": llm_config.get("temperature", 0.0),
            "num_predict": llm_config.get("max_tokens", 1024),
        },
        "stream": False,
    }

    max_retries = max(1, llm_config.get("max_retries", 2))
    timeout = llm_config.get("timeout_seconds", 120)

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            result = resp.json()
            content = result.get("message", {}).get("content", "").strip()
            if not content:
                raise ValueError(f"Empty content in response: {result}")
            return content
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "formatter: LLM整形呼び出しに失敗 (attempt %d/%d) model=%s error=%s",
                attempt, max_retries, llm_config["model"], e,
            )
            if attempt < max_retries:
                time.sleep(2 * attempt)

    return None


def polish_japanese_with_llm(word: str, ru_fields: dict, mt_fields: dict, cfg: dict) -> dict:
    """機械翻訳結果(mt_fields)をローカルLLMで自然な日本語に整形する。
    失敗した場合や項目数が壊れた場合は、フィールドごとに機械翻訳の結果へフォールバックする。"""
    llm_config = cfg["llm"]

    user_prompt = POLISH_USER_TEMPLATE.format(
        word=word,
        meanings_ru=ru_fields.get("Meanings_RU", ""),
        collocations_ru=ru_fields.get("Collocations_RU", ""),
        examples_ru=ru_fields.get("Examples_RU", ""),
        meanings_ja_mt=mt_fields.get("Meanings_JA", ""),
        collocations_ja_mt=mt_fields.get("Collocations_JA", ""),
        examples_ja_mt=mt_fields.get("Examples_JA", ""),
    )

    content = _call_ollama_polish(POLISH_SYSTEM_PROMPT, user_prompt, llm_config)
    result = dict(mt_fields)  # デフォルトは機械翻訳のまま（フェイルセーフ）
    if content is None:
        return result

    try:
        parsed = json.loads(_extract_json_object(content))
    except json.JSONDecodeError as e:
        logger.warning("formatter: LLM整形出力のJSONパースに失敗 word=%s error=%s", word, e)
        return result

    for key in ("Meanings_JA", "Collocations_JA", "Examples_JA"):
        polished_value = parsed.get(key)
        if not isinstance(polished_value, str):
            continue
        polished_value = polished_value.strip()
        raw_value = mt_fields.get(key, "")
        # 項目数（" / "区切り）が機械翻訳と一致しない場合は、統合/欠落のリスクがあるため
        # 機械翻訳の結果をそのまま採用する（ハルシネーション対策のフェイルセーフ）
        raw_count = len([t for t in raw_value.split(ITEM_SEP) if t.strip()])
        polished_count = len([t for t in polished_value.split(ITEM_SEP) if t.strip()])
        if raw_value and raw_count != polished_count:
            logger.warning(
                "formatter: LLM整形で項目数不一致のため機械翻訳結果を採用 word=%s field=%s raw=%d polished=%d",
                word, key, raw_count, polished_count,
            )
            continue
        if polished_value or not raw_value:
            result[key] = polished_value

    return result


# ---------------------------------------------------------------------------
# オーケストレーション: 機械翻訳 → LLM整形 → キャッシュ
# ---------------------------------------------------------------------------
def translate_and_polish(
    word: str, ru_fields: dict, cfg: dict, translator, translation_cfg: dict, only_mt: bool = False
) -> dict:
    """機械翻訳→(オプションで)LLM整形までを行う。

    only_mt=True の場合、LLM整形を完全にスキップし、Google翻訳の結果をそのまま採用する
    （config.json の translation.polish_with_llm 設定より優先される、呼び出し側の明示的な指定）。
    """
    db_path = cfg["database"]["path"]
    delay = translation_cfg.get("request_delay_seconds", 1.0)
    polish_enabled = translation_cfg.get("polish_with_llm", True) and not only_mt

    source_hash = compute_source_hash(
        ru_fields.get("Meanings_RU", ""),
        ru_fields.get("Collocations_RU", ""),
        ru_fields.get("Examples_RU", ""),
    )

    cached = load_translation_from_cache(db_path, word, source_hash)
    if cached is not None:
        return cached

    mt_fields = machine_translate_fields(ru_fields, translator, delay=delay)

    ja_fields = dict(mt_fields)
    polish_model = ""
    if polish_enabled and any(mt_fields.values()):
        try:
            ja_fields = polish_japanese_with_llm(word, ru_fields, mt_fields, cfg)
            polish_model = cfg["llm"]["model"]
        except Exception as e:  # noqa: BLE001
            logger.warning("formatter: LLM整形処理で例外発生 word=%s error=%s（機械翻訳結果をそのまま使用）", word, e)
            ja_fields = dict(mt_fields)

    save_translation_to_cache(db_path, word, source_hash, ja_fields, mt_engine="google", polish_model=polish_model)
    return ja_fields


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
             "（LLM整形の妥当性を検証したい場合や、LLM(Ollama)サーバを起動していない場合に使用）",
    )

    args = parser.parse_args()

    # 設定とDBの初期化
    cfg = load_config()
    db_path = cfg["database"]["path"]
    ensure_db_initialized(db_path)

    translation_cfg = cfg.get("translation", {
        "enabled": True,
        "source_lang": "ru",
        "target_lang": "ja",
        "request_delay_seconds": 1.0,
        "polish_with_llm": True,
    })
    translate_enabled = translation_cfg.get("enabled", True) and not args.no_translate

    output_path = args.output or cfg["pipeline"]["output_file"]
    use_bom = cfg["pipeline"]["csv_bom"]

    translator = None
    if translate_enabled:
        ensure_translation_cache_table(db_path)
        try:
            translator = build_translator(
                translation_cfg.get("source_lang", "ru"),
                translation_cfg.get("target_lang", "ja"),
            )
        except RuntimeError as e:
            print(f"警告: {e}\n日本語訳なし（ロシア語原文のみ）で続行します。")
            translate_enabled = False

    if translate_enabled:
        mode = "Google翻訳のみ（LLM整形スキップ / --onlyMT）" if args.onlyMT else "Google翻訳 + LLM整形"
        print(f"日本語訳モード: {mode}")

    # ファイルの読み込み
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"エラー: ファイル '{args.input}' が見つかりません。")
        return

    # 指定範囲のインデックス調整 (1始まりを0始まりのリストインデックスへ)
    start = max(0, args.startidx - 1)
    end = min(len(lines), args.endidx)

    # DBから該当単語の要約結果を取り出し、必要なら日本語訳を付けてCSV行を組み立てる
    rows = []
    found_count = 0
    translated_count = 0
    for i in range(start, end):
        word = lines[i]
        print(f"[{i + 1}/{len(lines)}] 処理中: {word}")

        ru_fields = load_summary_from_db(db_path, word)
        if ru_fields is None:
            rows.append(make_error_row(word, "DBに要約結果が見つかりません（未処理またはsummarize失敗の可能性）"))
            continue

        found_count += 1
        row = dict(ru_fields)
        row["Meanings_JA"] = ""
        row["Collocations_JA"] = ""
        row["Examples_JA"] = ""

        if translate_enabled:
            try:
                ja_fields = translate_and_polish(
                    word, ru_fields, cfg, translator, translation_cfg, only_mt=args.onlyMT
                )
                row.update(ja_fields)
                translated_count += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("formatter: 翻訳処理に失敗 word=%s error=%s", word, e)
                row["POS"] = (row.get("POS") or "") + f" [JA翻訳エラー: {e}]"

        rows.append(row)

    # CSVとして書き出し
    write_csv(rows, output_path, use_bom=use_bom)
    print(
        f"完了: {len(rows)}件中 {found_count}件をDBから取得（うち {translated_count}件に日本語訳を付与）、"
        f"'{output_path}' に書き出しました。"
    )


if __name__ == "__main__":
    main()
