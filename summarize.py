"""
summarize.py
------------
scraping.py で得た生データを、Ollama経由でSaiga2 13Bに渡し、
構造化されたCSV1行分のデータ（Word, POS, Gender, Aspect, PairedVerb,
Meanings_RU, Collocations_RU, Examples_RU, Accent）に変換するモジュール。

- ハルシネーション防止のため temperature=0.0 を厳守。
- プロンプトの指示文は英語（Saigaの性能最大化のため）。
- 意味・例文等はロシア語原文のまま維持し、日本語訳は含めない。
- 出力形式はCSV1行（ヘッダなし、パイプ区切り "|" をフィールド内部の区切りに使う）を強制する。
- キャッシュキー: (word, prompt_hash)。プロンプト内容が変わると別キャッシュとして扱われる。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from typing import Optional

import requests

from common import get_connection, now_iso, setup_logger, strip_code_fences

logger = setup_logger("logs/errors.log")

CSV_FIELDS = [
    "Word", "POS", "Gender", "Aspect", "PairedVerb",
    "Meanings_RU", "Collocations_RU", "Examples_RU", "Accent",
]

# ---------------------------------------------------------------------------
# プロンプトテンプレート（英語で厳格に指示。ロシア語データはそのまま埋め込む）
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a precise Russian-language lexicographer assistant. "
    "You extract ONLY facts explicitly present in the provided source data. "
    "You NEVER invent, guess, or hallucinate information. "
    "If a field cannot be determined from the source data, you MUST leave it EMPTY. "
    "You always respond in the exact CSV format requested, with no extra commentary, "
    "no markdown, no code fences, and no explanations."
)

USER_PROMPT_TEMPLATE = """\
# Task
Convert the following raw dictionary data about the Russian word "{word}" into \
EXACTLY ONE CSV row with these 9 fields, in this exact order, separated by commas, \
with each field wrapped in double quotes:

Word,POS,Gender,Aspect,PairedVerb,Meanings_RU,Collocations_RU,Examples_RU,Accent

# Field definitions
- Word: the headword in Cyrillic, exactly as given.
- POS: part of speech (e.g. noun, verb, adjective, adverb). Use English terms.
- Gender: grammatical gender for nouns only (masculine/feminine/neuter). Leave empty if not a noun or unknown.
- Aspect: verbal aspect for verbs only (perfective/imperfective). Leave empty if not a verb or unknown.
- PairedVerb: the paired perfective/imperfective verb in Cyrillic, if mentioned in the source. Leave empty if unknown.
- Meanings_RU: the word's meaning(s), written in Russian (Cyrillic) ONLY, taken from the source. \
Separate multiple meanings with " / ". Do NOT translate to Japanese or English.
- Collocations_RU: common collocations or set phrases in Russian (Cyrillic) ONLY, taken from the source. \
Separate multiple items with " / ". Leave empty if none found in the source.
- Examples_RU: example sentences in Russian (Cyrillic) ONLY, taken verbatim from the source. \
Separate multiple examples with " / ". Leave empty if none found in the source.
- Accent: stress/accent information if present in the source (e.g. which syllable/vowel is stressed). \
Leave empty if not present in the source.

# Strict rules
- Use ONLY the information in the "Source data" section below. Do not use outside knowledge.
- If a field is not present in the source data, leave that field as an empty string "" — do not guess.
- Do NOT include any Japanese text anywhere in the output.
- Do NOT include any explanation, preamble, or markdown code fences.
- Output MUST be exactly one CSV line: 9 comma-separated, double-quoted fields, and nothing else.

# Source data (raw extracted content, may include noise; use only relevant facts)
{source_data}

# Output (one CSV line only):
"""


def build_prompt(word: str, source_data: dict) -> tuple[str, str]:
    """system, user の2つのプロンプト文字列を返す。"""
    source_json = json.dumps(source_data, ensure_ascii=False, indent=2)
    user_prompt = USER_PROMPT_TEMPLATE.format(word=word, source_data=source_json)
    return SYSTEM_PROMPT, user_prompt


def compute_prompt_hash(system_prompt: str, user_prompt: str, model: str) -> str:
    payload = f"{model}\n{system_prompt}\n{user_prompt}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Ollama呼び出し
# ---------------------------------------------------------------------------
def call_ollama(system_prompt: str, user_prompt: str, llm_config: dict) -> Optional[str]:
    url = f"{llm_config['base_url']}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": llm_config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": llm_config.get("temperature", 0.0),
        "max_tokens": llm_config.get("max_tokens", 1024),
        "stream": False,
    }

    max_retries = max(1, llm_config.get("max_retries", 2))
    timeout = llm_config.get("timeout_seconds", 120)

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            result = resp.json()
            choices = result.get("choices")
            if not choices:
                raise ValueError(f"No choices in response: {result}")
            content = choices[0].get("message", {}).get("content", "").strip()
            if not content:
                raise ValueError("Empty content in response")
            return content
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "summarize: LLM call failed (attempt %d/%d) model=%s error=%s",
                attempt, max_retries, llm_config["model"], e,
            )
            if attempt < max_retries:
                time.sleep(2 * attempt)

    return None


# ---------------------------------------------------------------------------
# LLM出力のパース
# ---------------------------------------------------------------------------
def parse_csv_line(word: str, llm_output: str) -> dict:
    """LLMが返したCSV1行をパースしてdictに変換する。
    パースに失敗した場合は Word 以外を空欄にしたdictを返す（呼び出し側でエラー扱い可能）。
    """
    cleaned = strip_code_fences(llm_output)
    # 複数行返ってきた場合は、カンマを含む最初の妥当そうな行を採用する
    candidate_line = cleaned.splitlines()[0] if cleaned.splitlines() else cleaned

    try:
        reader = csv.reader(io.StringIO(candidate_line), skipinitialspace=True)
        row = next(reader)
    except Exception as e:  # noqa: BLE001
        logger.warning("summarize: CSV parse failed word=%s error=%s raw=%s", word, e, llm_output[:200])
        row = []

    result = {field: "" for field in CSV_FIELDS}
    result["Word"] = word  # Wordは常に入力単語で上書き（LLMの表記揺れ対策）

    if len(row) >= len(CSV_FIELDS):
        for field, value in zip(CSV_FIELDS[1:], row[1:len(CSV_FIELDS)]):
            result[field] = value.strip()
    else:
        logger.warning(
            "summarize: unexpected field count word=%s expected=%d got=%d raw=%s",
            word, len(CSV_FIELDS), len(row), llm_output[:200],
        )

    return result


# ---------------------------------------------------------------------------
# キャッシュ付き要約
# ---------------------------------------------------------------------------
def _load_from_cache(db_path: str, word: str, prompt_hash: str) -> Optional[dict]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM summaries WHERE word = ? AND prompt_hash = ?",
            (word, prompt_hash),
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


def _save_to_cache(db_path: str, word: str, prompt_hash: str, model: str,
                    fields: dict, raw_output: str) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO summaries (
                word, prompt_hash, model, pos, gender, aspect, paired_verb,
                meanings_ru, collocations_ru, examples_ru, accent, raw_llm_output, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(word, prompt_hash) DO UPDATE SET
                pos = excluded.pos,
                gender = excluded.gender,
                aspect = excluded.aspect,
                paired_verb = excluded.paired_verb,
                meanings_ru = excluded.meanings_ru,
                collocations_ru = excluded.collocations_ru,
                examples_ru = excluded.examples_ru,
                accent = excluded.accent,
                raw_llm_output = excluded.raw_llm_output,
                created_at = excluded.created_at
            """,
            (
                word, prompt_hash, model,
                fields["POS"], fields["Gender"], fields["Aspect"], fields["PairedVerb"],
                fields["Meanings_RU"], fields["Collocations_RU"], fields["Examples_RU"], fields["Accent"],
                raw_output, now_iso(),
            ),
        )
        conn.commit()


class SummarizeError(Exception):
    pass


def summarize_word(word: str, scraped_data: dict, config: dict) -> dict:
    """1単語分の生データをLLMで構造化する。キャッシュがあれば再利用する。
    戻り値: CSV_FIELDS をキーに持つdict。
    失敗時は SummarizeError を送出する。
    """
    db_path = config["database"]["path"]
    llm_config = config["llm"]

    system_prompt, user_prompt = build_prompt(word, scraped_data.get("sources", scraped_data))
    prompt_hash = compute_prompt_hash(system_prompt, user_prompt, llm_config["model"])

    cached = _load_from_cache(db_path, word, prompt_hash)
    if cached is not None:
        logger.info("summarize: cache hit word=%s", word)
        return cached

    llm_output = call_ollama(system_prompt, user_prompt, llm_config)
    if llm_output is None:
        raise SummarizeError(f"LLM呼び出しに失敗しました（word={word}, model={llm_config['model']}）")

    fields = parse_csv_line(word, llm_output)
    _save_to_cache(db_path, word, prompt_hash, llm_config["model"], fields, llm_output)

    return fields


if __name__ == "__main__":
    from common import ensure_db_initialized, load_config
    from scraping import scrape_word

    cfg = load_config()
    ensure_db_initialized(cfg["database"]["path"])
    test_word = "спасибо"
    scraped = scrape_word(test_word, cfg)
    summary = summarize_word(test_word, scraped, cfg)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
