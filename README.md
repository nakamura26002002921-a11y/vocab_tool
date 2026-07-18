# 単語帳自動生成スクリプト (Generate-Select-Refine版)

Ollama（ローカルLLM）を使って英単語の単語帳（フラッシュカード）をCSVで自動生成します。
1単語ごとに以下の3ステップを実行します。

1. **Generate（モンテカルロ）**: `prompt.txt` を使い、temperatureを効かせて候補を`num_candidates`個（デフォルト3）生成し、多様性を確保する。
2. **Select（Python）**: 生成された各候補を以下の観点でスコアリングし、最も点数の高い候補を1つ選ぶ。
   - フォーマット: `====`区切り線・全見出し(【コアイメージ】等)の有無、マークダウンコードブロックの残存、冒頭の余計な前置き文
   - 言語純度: 【例文】セクションにアルファベットが十分含まれているか、【イメージ・語源】【自分用メモ】セクションに日本語が十分含まれているか
   - 長さ: 極端に短い/長い場合は減点（フォーマット崩れの疑い）
3. **Refine（CoT）**: `refine_prompt.txt` を使い、選ばれた候補をLLMに渡す。LLMはまず【思考プロセス】として問題点を洗い出し、`### FINAL ###` という区切り行の後に完成版のみを出力する。スクリプトはこの区切り以降を抽出して最終出力とする。

## 事前準備

```bash
pip install -r requirements.txt
```

Ollamaを起動し、モデルをダウンロードしておいてください。

```bash
ollama pull qwen2.5:14b
```

## 実行方法

```bash
python3 main.py --input words.txt --output output.csv --startidx 1 --endidx 5
```

- `--input`: 単語が1行ずつ書かれたテキストファイル
- `--output`: 出力CSVファイル名（utf-8-sig、Excelでもそのまま開けます）
- `--startidx`: 開始行（1始まり、省略時は1）
- `--endidx`: 終了行（1始まり、省略時は末尾まで）

## config.json の pipeline 設定

```json
"pipeline": {
  "num_candidates": 3,          // Generateで作る候補数
  "generate_temperature": 0.7,  // Generate時のtemperature（多様性重視で高め）
  "refine_temperature": 0.2     // Refine時のtemperature（安定重視で低め）
}
```

候補数や各温度は自由に調整できます。候補数を増やすほど多様性が増しますが、その分LLM呼び出し回数（≒処理時間）も増えます。

## ファイル構成

- `main.py`: Generate → Select → Refine のパイプライン本体
- `config.json`: LLM接続設定 + パイプライン設定
- `prompt.txt`: Generateステップで使うプロンプト
- `refine_prompt.txt`: Refineステップで使うプロンプト（CoT形式）
- `words.txt`: サンプル単語リスト

## 処理の流れ（1単語あたり）

```
[Generate] prompt.txt を temperature=0.7 で3回呼び出し → 候補A, B, C
    ↓
[Select]   各候補をスコアリング（フォーマット + 言語純度）→ 最高得点の候補を選択
    ↓
[Refine]   refine_prompt.txt に選ばれた候補を渡す
           → LLMが思考プロセスで問題点を洗い出し、### FINAL ### 以降に完成版を出力
           → 完成版のみを抽出してCSVに書き込み
```

途中でエラーが出た候補はSelectステップで自動的にスコアが下がるため選ばれにくくなります。
全候補の生成に失敗した場合や選択・Refineに失敗した場合は `ERROR:` から始まる文字列がCSVに記録され、処理は次の単語に進みます（途中終了しません）。
