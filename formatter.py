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
"""

from __future__ import annotations

import csv
import os
from typing import Iterable

CSV_HEADER = [
    "Word",            # 見出し語（キリル文字）
    "POS",             # 品詞（メタデータ、英語表記）
    "Gender",          # 性（メタデータ、名詞のみ）
    "Aspect",          # 体（メタデータ、動詞のみ）
    "PairedVerb",      # ペア動詞（ロシア語）
    "Meanings_RU",     # 意味（ロシア語原文）
    "Collocations_RU", # コロケーション（ロシア語原文）
    "Examples_RU",     # 例文（ロシア語原文）
    "Accent",          # アクセント情報
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


if __name__ == "__main__":
    # 単体テスト用
    sample_rows = [
        {
            "Word": "спасибо", "POS": "interjection", "Gender": "", "Aspect": "",
            "PairedVerb": "", "Meanings_RU": "выражение благодарности",
            "Collocations_RU": "большое спасибо", "Examples_RU": "Спасибо за помощь.",
            "Accent": "спаси́бо",
        },
        make_error_row("несуществующееслово", "Extract/Summarizeステップに失敗しました"),
    ]
    write_csv(sample_rows, "vocab_test.csv")
    print("wrote vocab_test.csv")
