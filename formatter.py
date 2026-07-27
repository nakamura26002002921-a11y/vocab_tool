"""
formatter.py
------------
summarize.py で得た構造化データ（dictのリスト）を、最終的なCSVファイル
（デフォルト: vocab.csv）として書き出すモジュール。

- 文字コード: UTF-8 with BOM（Excel対応、config.jsonの pipeline.csv_bom で切替可）。
- カラム構成: メタデータ項目（Word, POS, Gender, Aspect, PairedVerb）と
  ロシア語原文項目（Meanings_RU, Collocations_RU, Examples_RU）、
  発音情報（Accent）を明確に分離したヘッダとする。
- エラー行（word のみでその他が "ERROR: ..." のもの）もそのまま1行として書き出す。

単体実行時は argparse による範囲指定付きのパイプライン実行が可能。
"""

from __future__ import annotations

import argparse
import csv
import os
from typing import Iterable

from common import ensure_db_initialized, load_config

CSV_HEADER = [
    "Word",           # 見出し語（キリル文字）
    "POS",            # 品詞（メタデータ、英語表記）
    "Gender",         # 性（メタデータ、名詞のみ）
    "Aspect",         # 体（メタデータ、動詞のみ）
    "PairedVerb",     # ペア動詞（ロシア語）
    "Meanings_RU",    # 意味（ロシア語原文）
    "Collocations_RU",# コロケーション（ロシア語原文）
    "Examples_RU",    # 例文（ロシア語原文）
    "Accent",         # アクセント情報
]


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


if __name__ == "__main__":
    main()
