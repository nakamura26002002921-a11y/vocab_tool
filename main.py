"""
main.py
-------
ロシア語単語帳自動生成システムの統合実行スクリプト。

パイプライン: scraping.py -> summarize.py -> formatter.py

- config.json からモデル名・トークン数・温度・並列数などの設定を読み込む。
- concurrent.futures.ThreadPoolExecutor で単語ごとに並列処理する。
- 失敗した単語はログ（logs/errors.log と DBのrun_errorsテーブル）に記録し、
  CSVには pipeline.on_error の設定に応じて ERROR 行として残す（デフォルト）か、
  除外する。
- SQLiteキャッシュ（dictionary.db）により、既に処理済みの単語はスクレイピング・
  LLM呼び出しの両方をスキップできる。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import time

from common import ensure_db_initialized, load_config, record_error, setup_logger
from formatter import make_error_row, write_csv
from scraping import ScrapingError, scrape_word
from summarize import SummarizeError, summarize_word


def process_word(word: str, config: dict, logger) -> dict:
    """1単語分のパイプライン（scraping -> summarize）を実行し、
    formatter.py が期待するCSV行dictを返す。失敗時はERROR行を返す。"""
    db_path = config["database"]["path"]

    try:
        scraped = scrape_word(word, config)
    except ScrapingError as e:
        logger.error("word=%s stage=scraping error=%s", word, e)
        record_error(db_path, word, "scraping", str(e))
        return make_error_row(word, f"scraping failed: {e}")
    except Exception as e:  # noqa: BLE001
        logger.error("word=%s stage=scraping unexpected_error=%s", word, e)
        record_error(db_path, word, "scraping", f"unexpected: {e}")
        return make_error_row(word, f"scraping unexpected error: {e}")

    try:
        summary = summarize_word(word, scraped, config)
    except SummarizeError as e:
        logger.error("word=%s stage=summarize error=%s", word, e)
        record_error(db_path, word, "summarize", str(e))
        return make_error_row(word, f"summarize failed: {e}")
    except Exception as e:  # noqa: BLE001
        logger.error("word=%s stage=summarize unexpected_error=%s", word, e)
        record_error(db_path, word, "summarize", f"unexpected: {e}")
        return make_error_row(word, f"summarize unexpected error: {e}")

    logger.info("word=%s completed", word)
    return summary


def is_error_row(row: dict) -> bool:
    return isinstance(row.get("POS"), str) and row["POS"].startswith("ERROR:")


def run_pipeline(words: list[str], config: dict, logger) -> tuple[list[dict], int]:
    max_workers = config["pipeline"]["max_workers"]
    on_error = config["pipeline"]["on_error"]  # "keep_as_error_row" | "exclude"

    results_by_word: dict[str, dict] = {}
    error_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_word = {
            executor.submit(process_word, word, config, logger): word for word in words
        }
        for future in concurrent.futures.as_completed(future_to_word):
            word = future_to_word[future]
            try:
                row = future.result()
            except Exception as e:  # noqa: BLE001
                # process_word内部で捕捉しきれなかった予期しない例外への最終防御
                logger.error("word=%s stage=pipeline fatal_error=%s", word, e)
                record_error(config["database"]["path"], word, "pipeline", f"fatal: {e}")
                row = make_error_row(word, f"fatal pipeline error: {e}")

            if is_error_row(row):
                error_count += 1

            results_by_word[word] = row

    # 入力順を維持してCSVに書き出す
    ordered_rows = [results_by_word[w] for w in words]

    if on_error == "exclude":
        ordered_rows = [r for r in ordered_rows if not is_error_row(r)]

    return ordered_rows, error_count


def main():
    parser = argparse.ArgumentParser(
        description="ロシア語単語帳自動生成システム (Scraping[XPath] -> Summarize[Saiga2 13B] -> CSV出力)"
    )
    parser.add_argument("--input", help="入力ファイル（1行1単語）。省略時は config.json の pipeline.input_file")
    parser.add_argument("--output", help="出力CSVファイル。省略時は config.json の pipeline.output_file")
    parser.add_argument("--config", default="config.json", help="設定ファイルパス")
    parser.add_argument("--startidx", type=int, default=1, help="開始インデックス（1始まり）")
    parser.add_argument("--endidx", type=int, default=None, help="終了インデックス（1始まり、省略時は末尾まで）")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logger(config["pipeline"]["log_file"])

    input_path = args.input or config["pipeline"]["input_file"]
    output_path = args.output or config["pipeline"]["output_file"]

    if not os.path.exists(input_path):
        logger.error("入力ファイルが見つかりません: %s", input_path)
        sys.exit(1)

    db_path = config["database"]["path"]
    ensure_db_initialized(db_path)

    with open(input_path, "r", encoding="utf-8-sig") as f:
        words = [line.strip() for line in f if line.strip()]

    if not words:
        logger.error("入力ファイルに単語がありません: %s", input_path)
        sys.exit(1)

    if args.startidx < 1 or args.startidx > len(words):
        logger.error("--startidx が範囲外です (1〜%d)", len(words))
        sys.exit(1)

    start = args.startidx - 1
    end = args.endidx if args.endidx is not None else len(words)
    end = min(end, len(words))
    target_words = words[start:end]

    logger.info(
        "処理開始: 単語数=%d model=%s max_workers=%d",
        len(target_words), config["llm"]["model"], config["pipeline"]["max_workers"],
    )

    start_time = time.time()
    rows, error_count = run_pipeline(target_words, config, logger)
    elapsed = time.time() - start_time

    write_csv(rows, output_path, use_bom=config["pipeline"]["csv_bom"])

    logger.info(
        "処理完了: 出力=%s 成功=%d 失敗=%d 所要時間=%.1f秒",
        output_path, len(rows) - error_count, error_count, elapsed,
    )

    if error_count:
        print(f"\n⚠ {error_count} 件の単語で処理に失敗しました。詳細は {config['pipeline']['log_file']} を確認してください。")
    print(f"✅ 出力先: {output_path}")


if __name__ == "__main__":
    main()
