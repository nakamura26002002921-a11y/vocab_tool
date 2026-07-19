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
# プロンプトテンプレート（Saiga 2 13B向け：ロシア語で厳格に指示）
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Ты — профессиональный лексикограф русского языка. "
    "Твоя задача — извлечь факты из предоставленных исходных данных и сформировать СТРОГО ОДНУ строку в формате CSV.\n"
    "АБСОЛЮТНЫЕ ПРАВИЛА (НАРУШЕНИЕ = ОШИБКА):\n"
    "1. Выводи ТОЛЬКО одну строку данных CSV. Никаких заголовков, никаких пояснений, никаких комментариев после строки.\n"
    "2. НЕ используй markdown-разметку (никаких ```csv или ```).\n"
    "3. Часть речи (POS) пиши на русском языке (например: существительное, глагол, прилагательное, наречие, междометие, предлог, союз, частица, местоимение).\n"
    "4. Род (Gender): мужской, женский или средний (только для существительных, иначе оставь поле пустым).\n"
    "5. Вид (Aspect): совершенный или несовершенный (только для глаголов, иначе оставь поле пустым).\n"
    "6. Значения, Коллокации и Примеры должны быть ТОЛЬКО на русском языке (кириллица). Разделяй множественные элементы символом ' | '.\n"
    "7. Если информации нет в исходных данных, оставляй поле ПУСТЫМ. НЕ пиши 'не указано', 'нет', 'unknown' или прочерк.\n"
    "8. НЕ используй свои внешние знания для заполнения полей. Используй ТОЛЬКО информацию из раздела 'Исходные данные'.\n"
    "9. Порядок столбцов строго: Слово,Часть_речи,Род,Вид,Парный_глагол,Значения,Коллокации,Примеры,Ударение.\n"
    "10. Каждое поле должно быть заключено в двойные кавычки, поля разделены запятыми."
)

USER_PROMPT_TEMPLATE = """\
Слово: "{word}"

Исходные данные (JSON):
{source_data}

Сформируй и выведи СТРОГО ОДНУ CSV-строку согласно правилам выше.
Пример идеального вывода:
"спасибо","междометие","","","выражение благодарности | благодарность","большое спасибо","Спасибо за помощь. | Спасибо за цветы.","спаси́бо"
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


import argparse
from common import ensure_db_initialized, load_config
from scraping import scrape_word

def main():
    parser = argparse.ArgumentParser(description="単語リストをスクレイピングするツール")
    parser.add_argument("--startidx", type=int, default=1, help="開始行 (1始まり)")
    parser.add_argument("--endidx", type=int, default=100, help="終了行")
    parser.add_argument("--input", type=str, default="words.txt", help="入力ファイル名")
    
    args = parser.parse_args()

    # 設定とDBの初期化
    cfg = load_config()
    ensure_db_initialized(cfg["database"]["path"])

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

    # 処理実行
    for i in range(start, end):
        word = lines[i]
        print(f"[{i + 1}/{len(lines)}] 処理中: {word}")
        
        try:
            scraped = scrape_word(word, cfg)
            summary = summarize_word(word, scraped, cfg)
            # 必要に応じて結果の保存処理などをここに記述
        except Exception as e:
            print(f"エラー発生 ({word}): {e}")

if __name__ == "__main__":
    main()
