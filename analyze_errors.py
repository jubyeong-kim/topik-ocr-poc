"""
OCR 오류 유형 분석 — 어떤 글자가 왜 틀리는가

정답과 인식 결과를 정렬해 치환/삽입/삭제를 뽑고, 한글은 자모로 분해해
초성·중성·종성 중 어디가 틀렸는지까지 본다. API 호출 없이 기존 결과만 사용한다.

  python analyze_errors.py results/report_real_clova.json
"""

import json
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
JONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"


def decompose(ch: str):
    """한글 음절 → (초성, 중성, 종성). 한글이 아니면 None."""
    if not ("가" <= ch <= "힣"):
        return None
    code = ord(ch) - 0xAC00
    return CHO[code // 588], JUNG[(code % 588) // 28], JONG[code % 28]


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "results/report_real_clova.json")
    rows = json.loads(src.read_text(encoding="utf-8"))

    subs = Counter()  # 글자 치환 쌍
    jamo_slot = Counter()  # 초성/중성/종성 중 어디가 틀렸나
    jamo_pairs = Counter()  # 자모 혼동 쌍
    op_kind = Counter()  # 치환/삽입/삭제
    n_lines_with_err = 0

    for r in rows:
        # 공백은 별도 문제로 이미 분석했으므로 제외하고 글자만 본다
        a = r["ref"].replace(" ", "")
        b = r["hyp"].replace(" ", "")
        if a == b:
            continue
        n_lines_with_err += 1

        for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b).get_opcodes():
            if tag == "equal":
                continue
            op_kind[tag] += max(i2 - i1, j2 - j1)
            if tag != "replace":
                continue
            # 길이가 같은 치환만 1:1 대응으로 본다
            if i2 - i1 != j2 - j1:
                continue
            for x, y in zip(a[i1:i2], b[j1:j2]):
                subs[(x, y)] += 1
                dx, dy = decompose(x), decompose(y)
                if not dx or not dy:
                    jamo_slot["한글 아님"] += 1
                    continue
                diff = [k for k in range(3) if dx[k] != dy[k]]
                if len(diff) == 1:
                    slot = ["초성", "중성", "종성"][diff[0]]
                    jamo_slot[slot] += 1
                    jamo_pairs[(slot, dx[diff[0]], dy[diff[0]])] += 1
                elif diff:
                    jamo_slot["2개 이상"] += 1

    total_sub = sum(subs.values())
    total_err = sum(op_kind.values())
    per_line = total_err / max(n_lines_with_err, 1)
    out = [
        "# OCR 오류 유형 분석 (CLOVA · 실제 손글씨 53행)",
        "",
        f"글자가 틀린 행: **{n_lines_with_err}/{len(rows)}** (띄어쓰기 차이는 제외)",
        "",
        "## 1. 연산 종류",
        "",
        "| 종류 | 글자 수 |",
        "|------|---------|",
    ]
    label = {"replace": "치환(다른 글자로 읽음)", "insert": "삽입(없는 글자 추가)", "delete": "삭제(글자 누락)"}
    for k, v in op_kind.most_common():
        out.append(f"| {label.get(k, k)} | {v} |")

    out += [
        "",
        "## 2. 치환 오류는 자모 어디에서 나는가",
        "",
        f"1:1 치환 {total_sub}건 분해",
        "",
        "| 위치 | 건수 | 비율 |",
        "|------|------|------|",
    ]
    for k, v in jamo_slot.most_common():
        out.append(f"| {k} | {v} | {v / max(total_sub, 1):.0%} |")

    out += ["", "## 3. 가장 잦은 글자 혼동", "", "| 정답 → 결과 | 횟수 |", "|-------------|------|"]
    for (x, y), c in subs.most_common(15):
        out.append(f"| {x} → {y} | {c} |")

    out += ["", "## 4. 자모 단위 혼동 (한 곳만 다른 경우)", "", "| 위치 | 정답 → 결과 | 횟수 |", "|------|-------------|------|"]
    for (slot, x, y), c in jamo_pairs.most_common(15):
        out.append(f"| {slot} | {x or '없음'} → {y or '없음'} | {c} |")

    out += [
        "",
        "## 5. 오류는 행마다 겹쳐 있다 — 부분 교정의 한계",
        "",
        f"글자 오류 총 **{total_err}건**이 **{n_lines_with_err}행**에 분포한다 "
        f"→ 오류 행 1개당 평균 **{per_line:.1f}건**.",
        "",
        "즉 대부분의 오류 행에는 원인이 **둘 이상** 있다.",
        "가장 잦은 혼동 하나를 완벽히 고쳐도 그 행은 여전히 틀린 채로 남는다.",
        "",
        "> **후처리로 일치율을 올리기 어려운 이유가 여기에 있다.**",
        "> CER 은 조금 내려가겠지만, 완전일치는 한 행의 모든 오류를 동시에 고쳐야 달성된다.",
    ]

    dst = Path("results/error_analysis.md")
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")

    print(f"글자 오류 행: {n_lines_with_err}/{len(rows)}")
    print("연산:", dict(op_kind))
    print("자모 위치:", dict(jamo_slot))
    print("잦은 혼동:", [f"{x}→{y}({c})" for (x, y), c in subs.most_common(8)])
    print(f"리포트 → {dst}")


if __name__ == "__main__":
    main()
