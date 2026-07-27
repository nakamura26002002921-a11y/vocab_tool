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

from common import ensure_db_initialized, get_connection, load_config

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


def load_summary_from_db(db_path: str, word: str) -> dict | None:
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


def make_error_row(word: str, message: str) -> dict:
    """エラー発生時のCSV行を作る。メタデータ以外の項目は空欄のまま、
    POS列に "ERROR: ..." を記録して原因を追跡できるようにする。"""
    row = {field: "" for field in CSV_HEADER}
    row["Word"] = word
    row["POS"] = f"ERROR: {message}"
    return row


def main():
    parser = argparse.ArgumentParser(
        description="scraping.py / summarize.py が保存したDBの内容をCSVに整形して出力するツール"
    )
    parser.add_argument("--startidx", type=int, default=1, help="開始行 (1始まり)")
    parser.add_argument("--endidx", type=int, default=100, help="終了行")
    parser.add_argument("--input", type=str, default="words.txt", help="入力ファイル名")
    parser.add_argument("--output", type=str, default=None, help="出力CSVファイル名（省略時は config.json の設定値）")

    args = parser.parse_args()

    # 設定とDBの初期化
    cfg = load_config()
    db_path = cfg["database"]["path"]
    ensure_db_initialized(db_path)

    output_path = args.output or cfg["pipeline"]["output_file"]
    use_bom = cfg["pipeline"]["csv_bom"]

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

    # DBから該当単語の要約結果を取り出してCSV行を組み立てる
    rows = []
    found_count = 0
    for i in range(start, end):
        word = lines[i]
        print(f"[{i + 1}/{len(lines)}] 処理中: {word}")

        summary = load_summary_from_db(db_path, word)
        if summary is None:
            rows.append(make_error_row(word, "DBに要約結果が見つかりません（未処理またはsummarize失敗の可能性）"))
        else:
            rows.append(summary)
            found_count += 1

    # CSVとして書き出し
    write_csv(rows, output_path, use_bom=use_bom)
    print(f"完了: {len(rows)}件中{found_count}件をDBから取得し、'{output_path}' に書き出しました。")


if __name__ == "__main__":
    main()
