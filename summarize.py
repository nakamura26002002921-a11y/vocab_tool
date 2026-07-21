"""
summarize.py
------------
scraping.py で得た生データ（wikitextのテンプレート記法除去やHTML構造の細部までは
完全にクリーンアップされていない、多少ノイズを含む可能性のあるテキスト）を、
Ollama経由でLLM（デフォルト: Vikhr-Nemo-12B-Instruct、ロシア語に強いモデル）に渡し、
構造化されたCSV1行分のデータ（Word, POS, Gender, Aspect, PairedVerb,
Meanings_RU, Collocations_RU, Examples_RU, Accent）に変換するモジュール。

【設計方針: パース精度の一部をLLMに委譲する】
scraping.py 側の正規表現パーサは、MediaWikiテンプレート（{{семантика|...}}等）の
複雑な折り返しパターンを100%決定論的に処理しようとすると際限なくコストが増える。
一方、ロシア語に強いLLMは「テンプレート除去の残骸らしきゴミを無視する」
「意味の区切りを正しく判断する」といった言語理解が要る作業を、正規表現よりずっと
頑健にこなせる。そのため、scraping.py側では過度に厳密なクリーンアップを行わず、
多少ノイズ・記法の残骸が残っていてもよいテキストを渡し、意味的な整形・ノイズ除去は
プロンプトで明示的にLLM側の仕事として指示している（下記 USER_PROMPT_TEMPLATE の
「Handling noisy input」セクション）。ただし「ソースにない事実を作らない」という
ハルシネーション禁止の原則は変えない。ノイズの除去・整形は許容するが、情報の捏造は
許容しない、という線引き。

- ハルシネーション防止のため temperature=0.0 を厳守。
- プロンプトの指示文は英語（モデルの指示追従性能を最大化するため）。
- 意味・例文等はロシア語原文のまま維持し、日本語訳は含めない。
- LLM出力はJSONオブジェクト1つを要求し、Ollamaネイティブ /api/chat の
  format パラメータ（JSON Schema）でデコーディング自体を制約することで、
  「指示を守らずクォート抜けCSVを返す」ような出力崩れを構造的に防ぐ。
  （OpenAI互換の /v1/chat/completions + response_format はバックエンドによって
  無視されるケースがあるため使用しない）
- キャッシュキー: (word, prompt_hash)。プロンプト内容が変わると別キャッシュとして扱われる。

【使用モデルについての注意】
Ollamaで`hf.co/...`形式のGGUFを直接pullする場合、GGUF内のtokenizer.chat_template
メタデータが正しく埋め込まれていないと、Ollamaが誤ったプロンプトフォーマットを
使ってしまい、モデルが学習時と異なる入力を受け取って出力が破綻する
（文字化けやフォーマット崩れ）ことがある。IlyaGusev/saiga2_13b_gguf（2023年当時の
変換）はこの問題が起きたため、chat_templateメタデータが正しく設定されている
bartowski/Vikhr-Nemo-12B-Instruct-R-21-09-24-GGUF に切り替えた。
"""

from __future__ import annotations

import hashlib
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
    "You extract and normalize facts explicitly present in the provided source data. "
    "The source data comes from an automated scraping/parsing pipeline and MAY contain "
    "noise: leftover MediaWiki template syntax (e.g. stray braces, pipe-separated "
    "parameter fragments like 'синонимы=' or 'антонимы=-'), broken punctuation from "
    "template removal (stray leading/trailing commas), HTML tag remnants, or duplicated "
    "fragments. Using your knowledge of Russian, you should recognize and silently ignore "
    "such noise, and reconstruct the intended clean meaning/example/collocation text. "
    "You NEVER invent, guess, or hallucinate facts that are not present in the source data "
    "in some form — cleaning up noise and formatting is allowed, inventing new content is not. "
    "If a field cannot be determined from the source data, you MUST use an empty string for it. "
    "You always respond with a single valid JSON object in the exact format requested, "
    "with no extra commentary, no markdown, no code fences, and no explanations."
)

USER_PROMPT_TEMPLATE = """\
# Task
Convert the following raw dictionary data about the Russian word "{word}" into \
a single JSON object with EXACTLY these 9 keys, in this exact order:

Word, POS, Gender, Aspect, PairedVerb, Meanings_RU, Collocations_RU, Examples_RU, Accent

# Handling noisy input
The source data below was produced by an automated scraper and is NOT guaranteed to be \
perfectly clean. It may contain:
- Leftover MediaWiki template fragments (e.g. stray "{{{{", "}}}}", "|синонимы=", \
"|антонимы=-", parameter names without values).
- Stray leading/trailing commas, semicolons, or whitespace left over from template removal.
- HTML tags or entities that were not fully stripped.
- Section text that runs together without clear boundaries.
When you encounter such noise, use your understanding of Russian and of dictionary structure \
to silently discard the noise and reconstruct the clean, intended text. For example, a meaning \
entry like ",  конфликтный разговор, ссора" should become "конфликтный разговор, ссора" \
(the leading comma/space is noise, not part of the meaning). An entry that is only noise \
(e.g. just "," or an empty template fragment) should be discarded entirely, not included as \
an empty or near-empty item.
This noise-cleaning applies to formatting and boundaries ONLY — never add information, \
translations, or details that are not derivable from the source text itself.

# Field definitions
- Word: the headword in Cyrillic, exactly as given.
- POS: part of speech (e.g. noun, verb, adjective, adverb). Use English terms.
- Gender: grammatical gender for nouns only (masculine/feminine/neuter). Leave empty string if not a noun or unknown.
- Aspect: verbal aspect for verbs only (perfective/imperfective). Leave empty string if not a verb or unknown.
- PairedVerb: the paired perfective/imperfective verb in Cyrillic, if mentioned in the source. Leave empty string if unknown.
- Meanings_RU: the word's distinct meaning(s), written in Russian (Cyrillic) ONLY, taken from the source. \
Each meaning should be a clean, complete phrase with no leftover markup or stray punctuation. \
Separate multiple meanings with " / ". Do NOT translate to Japanese or English.
- Collocations_RU: common collocations or set phrases in Russian (Cyrillic) ONLY, taken from the source. \
Separate multiple items with " / ". Leave empty string if none found in the source.
- Examples_RU: example sentences in Russian (Cyrillic) ONLY, taken verbatim (aside from noise removal) \
from the source. Separate multiple examples with " / ". Leave empty string if none found in the source.
- Accent: stress/accent information if present in the source (e.g. which syllable/vowel is stressed). \
Leave empty string if not present in the source.

# Strict rules
- Use ONLY the information in the "Source data" section below. Do not use outside knowledge.
- If a field is not present in the source data, use an empty string "" as its value — do not guess.
- Every value MUST be a plain JSON string (never a nested object, array, or number).
- Do NOT include any Japanese text anywhere in the output.
- Do NOT include any explanation, preamble, or markdown code fences.
- Output MUST be exactly one JSON object and nothing else — no text before or after it.

# Source data (raw extracted content — may include scraping/parsing noise as described above; \
use your judgment to extract only the genuine, relevant facts)
{source_data}

# Output (one JSON object only, e.g. {{"Word": "...", "POS": "...", ...}}):
"""

def build_prompt(word: str, source_data: dict) -> tuple[str, str]:
    """system, user の2つのプロンプト文字列を返す。"""
    source_json = json.dumps(source_data, ensure_ascii=False, indent=2)
    user_prompt = USER_PROMPT_TEMPLATE.format(word=word, source_data=source_json)
    return SYSTEM_PROMPT, user_prompt


def compute_prompt_hash(system_prompt: str, user_prompt: str, model: str) -> str:
    payload = f"{model}\n{system_prompt}\n{user_prompt}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Ollama の /api/chat エンドポイントに渡す JSON Schema（format パラメータ）。
# これによりデコーディング自体がこのスキーマに従うよう制約され、
# モデルが指示を「破る」形での出力崩れ（例: クォート抜けCSV）を構造的に防げる。
# 参考: https://docs.ollama.com/api/openai-compatibility の response_format は
# 実装によって無視されることがあるため、Ollamaネイティブの /api/chat + format を使う。
_RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {field: {"type": "string"} for field in CSV_FIELDS},
    "required": CSV_FIELDS,
}


# ---------------------------------------------------------------------------
# Ollama呼び出し
# ---------------------------------------------------------------------------
def call_ollama(system_prompt: str, user_prompt: str, llm_config: dict) -> Optional[str]:
    """Ollamaネイティブ API (/api/chat) を、format パラメータでJSON Schemaを
    指定して呼び出す。これによりモデルの出力トークンがスキーマに適合する
    JSONのみに制約される（llama.cppのgrammar-constrained decodingを利用）。
    """
    url = f"{llm_config['base_url']}/api/chat"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": llm_config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "format": _RESPONSE_JSON_SCHEMA,
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
                "summarize: LLM call failed (attempt %d/%d) model=%s error=%s",
                attempt, max_retries, llm_config["model"], e,
            )
            if attempt < max_retries:
                time.sleep(2 * attempt)

    return None


# ---------------------------------------------------------------------------
# LLM出力のパース
# ---------------------------------------------------------------------------
def _extract_json_object(text: str) -> str:
    """テキストから最初の { に対応する } までの範囲を波括弧の深さを数えて
    切り出す。モデルが前後に余計な説明文を付けてきた場合の保険。
    見つからなければ元のテキストをそのまま返す（後段のjson.loadsで失敗させる）。
    """
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


def _stringify(value) -> str:
    """LLMが文字列であるべき値を誤って配列やオブジェクトで返した場合の保険。
    リストは " / " 区切りの文字列に、それ以外の非文字列はそのまま str() する。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " / ".join(_stringify(v) for v in value if _stringify(v))
    return str(value).strip()


def parse_json_object(word: str, llm_output: str) -> dict:
    """LLMが返したJSONオブジェクトをパースしてdictに変換する。
    パースに失敗した場合は Word 以外を空欄にしたdictを返す（呼び出し側でエラー扱い可能）。
    """
    result = {field: "" for field in CSV_FIELDS}
    result["Word"] = word  # Wordは常に入力単語で上書き（LLMの表記揺れ対策）

    cleaned = strip_code_fences(llm_output)
    json_text = _extract_json_object(cleaned)

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.warning("summarize: JSON parse failed word=%s error=%s raw=%s", word, e, llm_output[:300])
        return result

    if not isinstance(parsed, dict):
        logger.warning("summarize: JSON output is not an object word=%s raw=%s", word, llm_output[:300])
        return result

    for field in CSV_FIELDS[1:]:  # Word以外
        if field in parsed:
            result[field] = _stringify(parsed[field])

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

    fields = parse_json_object(word, llm_output)
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
