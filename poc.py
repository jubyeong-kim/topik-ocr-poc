"""
TOPIK 손글씨 답안 OCR PoC

이미지(답안 사진) → OCR → 텍스트 → 정확도 리포트

  python poc.py gen        합성 답안 샘플 생성 (손글씨 사진이 없어도 실행 가능)
  python poc.py run        샘플에 OCR 실행 후 정확도 리포트 출력
  python poc.py selfcheck  지표 계산 로직 self-test
"""

import argparse
import json
import random
import re
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")  # easyocr/torch 의 pin_memory 경고 억제

ROOT = Path(__file__).parent
DEFAULT_DATA = ROOT / "data" / "samples"

# Windows 콘솔 기본 인코딩(cp949)에서 한글이 깨지는 것 방지
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# TOPIK II 쓰기 51/52번 답안 문장 (실제 모범답안 기반)
# 51번=격식체(~습니다), 52번=문어체(~ㄴ다/다)
SAMPLE_ANSWERS = [
    ("51-1-a", "다시 경험하고 싶습니다"),
    ("51-1-b", "알려 주시기 바랍니다"),
    ("51-2-a", "양해해 주시기 바랍니다"),
    ("51-2-b", "협조해 주시기 바랍니다"),
    ("51-3-a", "주차장을 이용하실 수 없습니다"),
    ("51-3-b", "참석해 주시면 감사하겠습니다"),
    ("52-1-a", "동물을 깜짝 놀라게 한다"),
    ("52-1-b", "자신을 보호하는 방법이라고 한다"),
    ("52-2-a", "전달되는 방식이 다르기 때문이다"),
    ("52-2-b", "공기를 통해서뿐만 아니라"),
    ("52-3-a", "건강에 도움이 되는 것으로 나타났다"),
    ("52-3-b", "환경 보호에 중요한 역할을 한다"),
]

# 필기체에 가까운 순서로 배치 (궁서체가 손글씨와 가장 유사)
FONT_CANDIDATES = [
    ("C:/Windows/Fonts/batang.ttc", 2, "Gungsuh"),
    ("C:/Windows/Fonts/batang.ttc", 3, "GungsuhChe"),
    ("C:/Windows/Fonts/batang.ttc", 0, "Batang"),
    ("C:/Windows/Fonts/gulim.ttc", 0, "Gulim"),
    ("C:/Windows/Fonts/malgun.ttf", 0, "MalgunGothic"),
]


def load_fonts(size):
    """사용 가능한 한글 폰트만 골라 로드."""
    from PIL import ImageFont

    fonts = []
    for path, index, label in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        try:
            fonts.append((ImageFont.truetype(path, size, index=index), label))
        except Exception:
            continue
    if not fonts:
        sys.exit("한글 폰트를 찾지 못했습니다. FONT_CANDIDATES 경로를 확인하세요.")
    return fonts


def gen(outdir: Path, seed: int = 42):
    """답안 문장을 이미지로 렌더링 + 촬영 열화(회전/노이즈/블러)를 흉내낸다.

    실제 손글씨 사진이 없어도 파이프라인을 끝까지 돌리고 정답(ground truth)을
    자동 확보하기 위한 합성 샘플. 실제 사진은 data/real/ 에 넣고 run 하면 된다.
    """
    from PIL import Image, ImageDraw, ImageFilter

    rnd = random.Random(seed)  # 시드 고정 → 누가 실행해도 같은 샘플
    outdir.mkdir(parents=True, exist_ok=True)
    fonts = load_fonts(size=52)
    labels = {}

    for sid, text in SAMPLE_ANSWERS:
        font, font_label = fonts[rnd.randrange(len(fonts))]

        # 텍스트 크기에 맞춰 여백 있는 캔버스 생성
        tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
        left, top, right, bottom = tmp.textbbox((0, 0), text, font=font)
        w, h = right - left, bottom - top
        img = Image.new("L", (w + 80, h + 60), color=245)
        ImageDraw.Draw(img).text((40 - left, 30 - top), text, fill=30, font=font)

        # 촬영 열화 흉내: 미세 회전 → 블러 → 노이즈 합성
        img = img.rotate(rnd.uniform(-1.8, 1.8), expand=True, fillcolor=245)
        img = img.filter(ImageFilter.GaussianBlur(rnd.uniform(0.3, 0.8)))
        noise = Image.effect_noise(img.size, rnd.uniform(6, 14))
        img = Image.blend(img, noise.convert("L"), alpha=0.12)

        fname = f"{sid}.png"
        img.convert("RGB").save(outdir / fname)
        labels[fname] = {"text": text, "font": font_label}

    (outdir / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"샘플 {len(labels)}건 생성 → {outdir}")
    print(f"정답 라벨 → {outdir / 'labels.json'}")


# ── OCR 후처리: 한국어 문법 규칙 기반 교정 ──────────────────────────
# OCR 오류의 대부분이 "종성(받침) 오인식"에 몰려 있다. 아래 규칙은 관측된
# 오답을 보고 짜맞춘 것이 아니라, 한국어에 존재하지 않는 형태를 근거로 한다.
JONG_M, JONG_B = 16, 17  # 한글 종성 인덱스: ㅁ=16, ㅂ=17


def _fix_mnida(text: str) -> str:
    """한국어에 '-ㅁ니다' 어미는 없다 → 종성 ㅁ + '니다' 를 ㅂ 으로 교정.

    (바람니다 → 바랍니다). '습니다'는 이미 종성이 ㅂ이라 영향 없음.
    """
    chars = list(text)
    for i in range(len(chars) - 2):
        c = chars[i]
        if chars[i + 1] == "니" and chars[i + 2] == "다" and "가" <= c <= "힣":
            if (ord(c) - 0xAC00) % 28 == JONG_M:
                chars[i] = chr(ord(c) + (JONG_B - JONG_M))
    return "".join(chars)


def postcorrect(text: str) -> str:
    """OCR 원문 → 한국어 규칙 교정본."""
    text = _fix_mnida(text)
    # 어절 끝 '올'은 조사가 아니다 → 목적격 조사 '을' (동물올 → 동물을)
    text = re.sub(r"([가-힣])올(?=\s|$)", r"\1을", text)
    # 한국어에 '-켓습니다' 어미는 없다 (감사하켓습니다 → 감사하겠습니다)
    text = text.replace("켓습니다", "겠습니다")
    return text


def edit_distance(a: str, b: str) -> int:
    """레벤슈타인 거리 (CER 계산용)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def norm(s: str) -> str:
    """공백 정규화 (OCR은 띄어쓰기를 자주 흘림)."""
    return " ".join(s.split())


def cer(ref: str, hyp: str) -> float:
    """문자 오류율. 0.0 = 완벽, 1.0 = 전부 틀림."""
    ref, hyp = norm(ref), norm(hyp)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


def run(datadir: Path, outfile: Path):
    """OCR 실행 → 샘플별 결과와 집계 지표를 마크다운 리포트로 저장."""
    labels_path = datadir / "labels.json"
    if not labels_path.exists():
        sys.exit(f"{labels_path} 가 없습니다. 먼저 `python poc.py gen` 을 실행하세요.")

    try:
        import easyocr
    except ImportError:
        sys.exit("easyocr 미설치. `pip install -r requirements.txt` 후 다시 실행하세요.")

    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    print(f"OCR 엔진 로딩 중... (최초 1회 모델 다운로드)")
    reader = easyocr.Reader(["ko"], gpu=False)

    rows = []
    for fname, meta in sorted(labels.items()):
        ref = meta["text"] if isinstance(meta, dict) else meta
        img_path = datadir / fname
        if not img_path.exists():
            print(f"  건너뜀 (파일 없음): {fname}")
            continue

        # easyocr 는 영역별로 쪼개 반환 → 좌→우 순으로 이어붙임
        t0 = time.perf_counter()
        raw = norm(" ".join(reader.readtext(str(img_path), detail=0)))
        elapsed = time.perf_counter() - t0
        fixed = norm(postcorrect(raw))
        rows.append(
            {
                "file": fname,
                "font": meta.get("font", "-") if isinstance(meta, dict) else "-",
                "ref": ref,
                "raw": raw,
                "hyp": fixed,
                "sec": elapsed,
                "cer_raw": cer(ref, raw),
                "cer": cer(ref, fixed),
                "ok_raw": ref.replace(" ", "") == raw.replace(" ", ""),
                "exact": norm(ref) == fixed,
                "exact_nospace": ref.replace(" ", "") == fixed.replace(" ", ""),
            }
        )
        r = rows[-1]
        print(f"  [{'O' if r['ok_raw'] else 'X'}→{'O' if r['exact_nospace'] else 'X'}] {fname}: {fixed!r}")

    if not rows:
        sys.exit("처리할 이미지가 없습니다.")

    n = len(rows)
    cer_raw = sum(r["cer_raw"] for r in rows) / n
    avg_cer = sum(r["cer"] for r in rows) / n
    ok_raw = sum(r["ok_raw"] for r in rows)
    exact = sum(r["exact"] for r in rows)
    exact_ns = sum(r["exact_nospace"] for r in rows)

    lines = [
        "# OCR 정확도 리포트",
        "",
        f"대상: `{datadir}` ({n}건)",
        "",
        "## 요약 — 후처리 전/후 비교",
        "",
        "| 지표 | OCR 원본 | 후처리 적용 | 목표 |",
        "|------|----------|-------------|------|",
        f"| 평균 CER (낮을수록 좋음) | {cer_raw:.3f} | **{avg_cer:.3f}** | 0.15 이하 |",
        f"| 공백 무시 일치율 | {ok_raw}/{n} ({ok_raw / n:.0%}) | **{exact_ns}/{n} ({exact_ns / n:.0%})** | 80% 이상 |",
        f"| 완전 일치율 | - | {exact}/{n} ({exact / n:.0%}) | - |",
        "",
        f"이미지 1장당 평균 처리 시간: **{sum(r['sec'] for r in rows) / n:.2f}초** "
        f"(CPU 전용, 모델 로딩 제외)",
        "",
        "## 샘플별 결과",
        "",
        "| 파일 | 폰트 | 정답 | OCR 원본 | 후처리 후 | CER | 일치 |",
        "|------|------|------|----------|-----------|-----|------|",
    ]
    for r in rows:
        changed = r["raw"] != r["hyp"]
        lines.append(
            f"| {r['file']} | {r['font']} | {r['ref']} | {r['raw'] or '(빈 결과)'} "
            f"| {r['hyp'] if changed else '(동일)'} | {r['cer']:.2f} "
            f"| {'O' if r['exact_nospace'] else 'X'} |"
        )

    fixed_by_post = [r for r in rows if not r["ok_raw"] and r["exact_nospace"]]
    if fixed_by_post:
        lines += ["", "## 후처리가 살려낸 사례", ""]
        for r in fixed_by_post:
            lines.append(f"- `{r['file']}`: `{r['raw']}` → `{r['hyp']}`")

    fails = [r for r in rows if not r["exact_nospace"]]
    if fails:
        lines += ["", "## 남은 실패 사례", ""]
        for r in fails:
            lines.append(f"- `{r['file']}` ({r['font']}): 정답 `{r['ref']}` → 결과 `{r['hyp']}`")

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n평균 CER  {cer_raw:.3f} → {avg_cer:.3f}")
    print(f"일치율    {ok_raw}/{n} → {exact_ns}/{n}  (후처리 전 → 후)")
    print(f"리포트 → {outfile}")


def selfcheck():
    """지표 계산 로직 검증."""
    assert edit_distance("가나다", "가나다") == 0
    assert edit_distance("가나다", "가나") == 1
    assert edit_distance("", "가") == 1
    assert cer("가나다라", "가나다라") == 0.0
    assert cer("가나다라", "가나다") == 0.25  # 4자 중 1자 누락
    # 띄어쓰기는 TOPIK 채점 대상이므로 CER에 포함한다 (공백 무시 비교는 exact_nospace 지표로 별도 집계)
    assert cer("가 나", "가나") == 1 / 3
    assert cer("가  나", "가 나") == 0.0, "연속 공백만 정규화되어야 함"
    assert cer("", "") == 0.0
    assert cer("가나", "") == 1.0

    # 후처리: 고쳐야 할 것을 고치는가
    assert postcorrect("바람니다") == "바랍니다"
    assert postcorrect("양해해 주시기 바람니다") == "양해해 주시기 바랍니다"
    assert postcorrect("동물올 본다") == "동물을 본다"
    assert postcorrect("자신올") == "자신을"
    assert postcorrect("감사하켓습니다") == "감사하겠습니다"

    # 후처리: 멀쩡한 것을 망가뜨리지 않는가 (과교정 방지)
    assert postcorrect("감사합니다") == "감사합니다"
    assert postcorrect("없습니다") == "없습니다"
    assert postcorrect("서울에 간다") == "서울에 간다", "'울'은 '올'이 아니므로 무관"
    assert postcorrect("올해는 춥다") == "올해는 춥다", "어절 끝이 아닌 '올'은 유지"
    assert postcorrect("한다") == "한다"
    print("selfcheck 통과")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="TOPIK 손글씨 답안 OCR PoC")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="합성 답안 샘플 생성")
    g.add_argument("--out", type=Path, default=DEFAULT_DATA)
    g.add_argument("--seed", type=int, default=42)

    r = sub.add_parser("run", help="OCR 실행 및 정확도 리포트")
    r.add_argument("--data", type=Path, default=DEFAULT_DATA)
    r.add_argument("--out", type=Path, default=ROOT / "results" / "report.md")

    sub.add_parser("selfcheck", help="지표 계산 self-test")

    a = p.parse_args()
    if a.cmd == "gen":
        gen(a.out, a.seed)
    elif a.cmd == "run":
        run(a.data, a.out)
    else:
        selfcheck()
