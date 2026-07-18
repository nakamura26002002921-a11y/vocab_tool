import argparse
import json
import os
import csv
import time
import sys
import re
import requests


REQUIRED_HEADERS = [
    "【コアイメージ】",
    "【イメージ・語源】",
    "【よくあるセット",
    "【例文】",
    "【類語・言い換え】",
    "【自分用メモ】",
]

JP_CHAR_RE = re.compile(r'[\u3040-\u30FF\u4E00-\u9FFF]')
ASCII_LETTER_RE = re.compile(r'[A-Za-z]')


def load_config():
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

    raw_url = config['llm']['base_url']
    if raw_url.startswith("${") and ":-" in raw_url:
        inner = raw_url[2:-1]
        var_name, default_url = inner.split(":-", 1)
        base_url = os.environ.get(var_name, default_url)
    else:
        base_url = raw_url

    config['llm']['base_url'] = base_url.rstrip('/')

    config.setdefault('pipeline', {})
    config['pipeline'].setdefault('num_candidates', 3)
    config['pipeline'].setdefault('generate_temperature', 0.7)
    config['pipeline'].setdefault('refine_temperature', 0.2)
    return config


def strip_code_fences(text):
    text = re.sub(r'^```(?:text|markdown)?\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*```$', '', text, flags=re.MULTILINE)
    return text.strip()


def call_llm(messages, temperature, config):
    """Ollama(OpenAI互換API)への1回の呼び出し。成功時は文字列、失敗時はNoneを返す"""
    url = f"{config['llm']['base_url']}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": config['llm']['model'],
        "messages": messages,
        "temperature": temperature,
        "stream": False
    }

    max_retries = max(1, config['llm']['max_retries'])
    timeout = config['llm']['timeout_seconds']

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            choices = result.get('choices')
            if not choices:
                raise ValueError(f"応答に choices が含まれていません: {result}")
            content = choices[0].get('message', {}).get('content', '').strip()
            if not content:
                raise ValueError("応答内容が空でした")
            return content
        except Exception as e:
            print(f"    [!] エラー (試行 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2)

    return None


# ---------------------------------------------------------------------------
# 1. Generate（モンテカルロ）: 候補をnum_candidates個生成する
# ---------------------------------------------------------------------------
def generate_candidates(word, prompt_template, config):
    prompt = prompt_template.replace("{word}", word)
    n = config['pipeline']['num_candidates']
    temperature = config['pipeline']['generate_temperature']

    candidates = []
    for i in range(n):
        print(f"    Generate {i + 1}/{n} (temperature={temperature})...")
        content = call_llm(
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            config=config
        )
        if content:
            candidates.append(strip_code_fences(content))
        else:
            print(f"    -> 候補{i + 1}の生成に失敗しました（スキップ）")

    return candidates


# ---------------------------------------------------------------------------
# 2. Select（Python）: フォーマットと言語純度でスコアリングし、最良の候補を選ぶ
# ---------------------------------------------------------------------------
def score_candidate(text):
    score = 0
    reasons = []

    # --- フォーマットチェック ---
    if text.count("====") >= 2:
        score += 2
    else:
        reasons.append("区切り線(====)が不足")

    for header in REQUIRED_HEADERS:
        if header in text:
            score += 1
        else:
            reasons.append(f"見出し欠落: {header}")

    if "```" in text:
        score -= 5
        reasons.append("マークダウンのコードブロックが残存")

    # 挨拶や余計な前置き（「承知しました」「以下に」等）が先頭にあると減点
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if not first_line.startswith("=") and not re.match(r'^[A-Za-z].*\s*/.*/', first_line):
        # 先頭が単語行(====か発音記号行)でない場合は減点
        if any(greeting in first_line for greeting in ["承知", "以下", "です", "ます"]):
            score -= 2
            reasons.append("先頭に余計な前置き文がある")

    # --- 言語純度チェック ---
    # 例文セクション: 英語部分にアルファベットが十分含まれているか
    example_match = re.search(r'【例文】(.*?)【', text, re.DOTALL)
    if example_match:
        example_text = example_match.group(1)
        ascii_ratio = len(ASCII_LETTER_RE.findall(example_text)) / max(len(example_text), 1)
        if ascii_ratio > 0.15:
            score += 2
        else:
            reasons.append("例文セクションに英語が少ない/欠落")
    else:
        reasons.append("例文セクションが見つからない")

    # 自分用メモ・イメージ語源セクション: 日本語が十分含まれているか
    for section_name in ["【イメージ・語源】", "【自分用メモ】"]:
        pattern = re.escape(section_name) + r'(.*?)(【|====|$)'
        m = re.search(pattern, text, re.DOTALL)
        if m:
            section_text = m.group(1)
            jp_ratio = len(JP_CHAR_RE.findall(section_text)) / max(len(section_text), 1)
            if jp_ratio > 0.2:
                score += 2
            else:
                reasons.append(f"{section_name}の日本語比率が低い")

    # 極端に短い/長いものは減点（フォーマット崩れの疑い）
    length = len(text)
    if length < 150:
        score -= 3
        reasons.append("全体が短すぎる")
    elif length > 2000:
        score -= 1
        reasons.append("全体が長すぎる")

    return score, reasons


def select_best(candidates):
    if not candidates:
        return None, []

    scored = []
    for idx, c in enumerate(candidates):
        s, reasons = score_candidate(c)
        scored.append((s, idx, c, reasons))
        print(f"    候補{idx + 1}: score={s}  ({'; '.join(reasons) if reasons else '問題なし'})")

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_idx, best_text, best_reasons = scored[0]
    print(f"    -> 候補{best_idx + 1}を選択 (score={best_score})")
    return best_text, scored


# ---------------------------------------------------------------------------
# 3. Refine（CoT）: 選ばれた候補をLLMに渡し、思考プロセスで修正させ最終版を得る
# ---------------------------------------------------------------------------
def refine_candidate(word, candidate_text, refine_prompt_template, config):
    prompt = refine_prompt_template.replace("{candidate}", candidate_text)
    temperature = config['pipeline']['refine_temperature']

    print(f"    Refine中 (temperature={temperature})...")
    content = call_llm(
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        config=config
    )

    if not content:
        print("    -> Refineに失敗。Selectで選ばれた候補をそのまま使用します。")
        return candidate_text

    content = strip_code_fences(content)

    marker = "### FINAL ###"
    if marker in content:
        final_text = content.split(marker, 1)[1].strip()
    else:
        # マーカーが見つからない場合は、最後の==== ブロックを探す
        blocks = re.findall(r'=+.*?=+', content, re.DOTALL)
        if blocks:
            final_text = blocks[-1].strip()
        else:
            print("    -> FINALマーカーが見つからず、Refine結果全体を採用します。")
            final_text = content

    final_text = strip_code_fences(final_text)
    return final_text if final_text else candidate_text


def process_word(word, prompt_template, refine_prompt_template, config):
    # 1. Generate
    candidates = generate_candidates(word, prompt_template, config)
    if not candidates:
        return "ERROR: 候補の生成にすべて失敗しました。"

    # 2. Select
    best_text, scored = select_best(candidates)
    if best_text is None:
        return "ERROR: 候補の選択に失敗しました。"

    # 3. Refine
    final_text = refine_candidate(word, best_text, refine_prompt_template, config)
    return final_text


def main():
    parser = argparse.ArgumentParser(description="単語帳自動生成スクリプト (Generate-Select-Refine)")
    parser.add_argument('--input', required=True, help='入力ファイル (例: words.txt)')
    parser.add_argument('--output', required=True, help='出力ファイル (例: output.csv)')
    parser.add_argument('--startidx', type=int, default=1, help='開始インデックス (1始まり)')
    parser.add_argument('--endidx', type=int, default=None, help='終了インデックス (1始まり, 省略時は末尾まで)')
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"[エラー] 入力ファイルが見つかりません: {args.input}")
        sys.exit(1)

    config = load_config()
    print(f"LLM Model: {config['llm']['model']}")
    print(f"Base URL: {config['llm']['base_url']}")
    print(f"Pipeline: Generate({config['pipeline']['num_candidates']}candidates) -> Select -> Refine")

    with open(args.input, 'r', encoding='utf-8-sig') as f:
        words = [line.strip() for line in f if line.strip()]

    if not words:
        print("[エラー] 入力ファイルに単語がありません。")
        sys.exit(1)

    if args.startidx < 1 or args.startidx > len(words):
        print(f"[エラー] --startidx が範囲外です (1〜{len(words)})")
        sys.exit(1)

    start = args.startidx - 1
    end = args.endidx if args.endidx is not None else len(words)
    end = min(end, len(words))
    target_words = words[start:end]

    print(f"処理対象: {len(target_words)} 語 ({args.startidx} 番目 〜 {start + len(target_words)} 番目)")

    for path, label in [('prompt.txt', 'Generate用プロンプト'), ('refine_prompt.txt', 'Refine用プロンプト')]:
        if not os.path.exists(path):
            print(f"[エラー] {path} ({label}) が見つかりません。")
            sys.exit(1)

    with open('prompt.txt', 'r', encoding='utf-8') as f:
        prompt_template = f.read()
    with open('refine_prompt.txt', 'r', encoding='utf-8') as f:
        refine_prompt_template = f.read()

    output_dir = os.path.dirname(os.path.abspath(args.output))
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    error_count = 0

    with open(args.output, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['単語', '生成した単語帳'])
        f.flush()

        try:
            for i, word in enumerate(target_words, start=args.startidx):
                print(f"[{i}/{len(words)}] 処理中: {word}")
                card_content = process_word(word, prompt_template, refine_prompt_template, config)
                writer.writerow([word, card_content])
                f.flush()

                if card_content.startswith("ERROR:"):
                    error_count += 1
                    print("  -> 失敗")
                else:
                    print("  -> 完了")
        except KeyboardInterrupt:
            print("\n[中断] ユーザーにより処理が中断されました。ここまでの結果は保存されています。")
            sys.exit(1)

    print(f"\n✅ 処理が完了しました。出力先: {args.output}")
    if error_count:
        print(f"⚠ {error_count} 件の単語で生成に失敗しました。CSV内の 'ERROR:' 行を確認してください。")


if __name__ == "__main__":
    main()
