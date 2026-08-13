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
from html import unescape
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


# ── OCR 엔진 ────────────────────────────────────────────────────────
def _gpu_available() -> bool:
    """GPU가 있으면 쓰고 없으면 CPU (로컬=CPU, Colab T4=GPU 자동 전환)."""
    try:
        import torch

        return torch.cuda.is_available()
    except ImportError:
        return False


def _surya_text(result) -> str:
    """surya 결과에서 평문 추출.

    버전에 따라 blocks(html) 또는 text_lines(text) 형태로 나뉘어 둘 다 처리한다.
    """
    blocks = getattr(result, "blocks", None)
    if blocks:
        raw = " ".join(getattr(b, "html", "") or "" for b in blocks)
        return unescape(re.sub(r"<[^>]+>", " ", raw))
    lines = getattr(result, "text_lines", None) or []
    return " ".join(getattr(ln, "text", "") or "" for ln in lines)


def _clova_text(payload: dict) -> str:
    """CLOVA OCR 응답에서 인식 텍스트를 좌→우 순으로 이어붙인다.

    응답 구조: images[].fields[].inferText
    fields 에는 줄바꿈 여부(lineBreak)가 있으나 우리는 한 줄 이미지만 넣으므로 무시한다.
    """
    out = []
    for img in payload.get("images", []):
        for f in img.get("fields", []):
            t = f.get("inferText", "")
            if t:
                out.append(t)
    return " ".join(out)


def _make_clova_reader():
    """CLOVA OCR(네이버 클라우드) 어댑터.

    키는 환경변수로만 받는다 — 저장소에 절대 커밋하지 않는다.
      CLOVA_OCR_URL     APIGW Invoke URL
      CLOVA_OCR_SECRET  Secret Key
    """
    import base64
    import json as _json
    import os
    import urllib.request

    url = os.environ.get("CLOVA_OCR_URL", "").strip()
    secret = os.environ.get("CLOVA_OCR_SECRET", "").strip()
    if not url or not secret:
        sys.exit(
            "CLOVA 키가 없습니다. 환경변수를 설정하세요:\n"
            '  $env:CLOVA_OCR_URL = "<APIGW Invoke URL>"\n'
            '  $env:CLOVA_OCR_SECRET = "<Secret Key>"'
        )

    def read(path) -> str:
        path = Path(path)
        body = {
            "version": "V1",
            "requestId": path.name,
            "timestamp": 0,
            "lang": "ko",
            "images": [
                {
                    "format": path.suffix.lstrip(".").lower() or "png",
                    "name": path.stem,
                    "data": base64.b64encode(path.read_bytes()).decode(),
                }
            ],
        }
        req = urllib.request.Request(
            url,
            data=_json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-OCR-SECRET": secret},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _clova_text(_json.loads(resp.read().decode()))

    return read


def make_reader(engine: str):
    """엔진 이름 → `이미지경로 -> 텍스트` 함수를 돌려준다."""
    gpu = _gpu_available()
    print(f"OCR 엔진 로딩 중: {engine} ({'GPU' if gpu else 'CPU'})")

    if engine == "easyocr":
        try:
            import easyocr
        except ImportError:
            sys.exit("easyocr 미설치. `pip install easyocr` 후 다시 실행하세요.")
        reader = easyocr.Reader(["ko"], gpu=gpu)
        # 영역별로 쪼개 반환되므로 좌→우 순으로 이어붙임
        return lambda p: " ".join(reader.readtext(str(p), detail=0))

    if engine == "surya":
        # surya 는 언어 지정이 필요 없다 (다국어 자동 인식).
        # 다만 버전에 따라 초기화 방식이 완전히 다르므로 둘 다 지원한다.
        from PIL import Image

        # 0.20+ : 추론 백엔드 매니저 기반 (vLLM/llama.cpp 등 외부 백엔드 필요)
        try:
            from surya.inference import SuryaInferenceManager
            from surya.recognition import RecognitionPredictor

            predictor = RecognitionPredictor(SuryaInferenceManager())
            return lambda p: _surya_text(predictor([Image.open(p)])[0])
        except ImportError:
            pass

        # ~0.17.x : FoundationPredictor + DetectionPredictor 로 인프로세스 추론
        try:
            from surya.detection import DetectionPredictor
            from surya.foundation import FoundationPredictor
            from surya.recognition import RecognitionPredictor
        except ImportError:
            sys.exit("surya 미설치. `pip install surya-ocr` 후 다시 실행하세요.")

        rec = RecognitionPredictor(FoundationPredictor())
        det = DetectionPredictor()
        return lambda p: _surya_text(rec([Image.open(p)], det_predictor=det)[0])

    if engine == "clova":
        return _make_clova_reader()

    sys.exit(f"알 수 없는 엔진: {engine} (easyocr | surya | clova)")


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


def cer_nospace(ref: str, hyp: str) -> float:
    """띄어쓰기를 무시한 문자 오류율.

    TOPIK 은 띄어쓰기도 채점 대상이라 기본 CER 에는 공백을 포함한다.
    다만 OCR 실패가 띄어쓰기에 몰릴 때 '글자 자체를 얼마나 읽었는가'를
    분리해서 보기 위한 보조 지표.
    """
    ref, hyp = ref.replace(" ", ""), hyp.replace(" ", "")
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


SHORT_LINE_CHARS = 10  # 이보다 짧은 행은 CER 이 극단적으로 튀어 대표성이 없다


def _short_line_note(rows: list) -> str:
    """짧은 단편 행을 제외한 CER 을 함께 보고한다.

    "다." 같은 2~3글자 행은 한 글자만 틀려도 CER 이 1.0 이 되어 평균을 왜곡한다.
    TOPIK 답안 행으로서 대표성도 없으므로 제외값을 병기한다 (제외 대상은 명시).
    """
    short = [r for r in rows if len(r["ref"]) < SHORT_LINE_CHARS]
    if not short:
        return f"({SHORT_LINE_CHARS}자 미만 단편 없음)"
    keep = [r for r in rows if len(r["ref"]) >= SHORT_LINE_CHARS]
    if not keep:
        return "(모든 행이 단편이라 보정값을 낼 수 없음)"
    adj = sum(r["cer"] for r in keep) / len(keep)
    names = ", ".join(f"`{r['file']}`({r['ref']})" for r in short)
    return (
        f"**{SHORT_LINE_CHARS}자 미만 단편 {len(short)}건 제외 시 평균 CER: {adj:.3f}** "
        f"({len(keep)}행 기준) — 제외 대상: {names}"
    )


def run(datadir: Path, outfile: Path, engine: str = "easyocr"):
    """OCR 실행 → 샘플별 결과와 집계 지표를 마크다운 리포트로 저장."""
    labels_path = datadir / "labels.json"
    if not labels_path.exists():
        sys.exit(f"{labels_path} 가 없습니다. 먼저 `python poc.py gen` 을 실행하세요.")

    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    reader = make_reader(engine)

    rows = []
    for fname, meta in sorted(labels.items()):
        ref = meta["text"] if isinstance(meta, dict) else meta
        img_path = datadir / fname
        if not img_path.exists():
            print(f"  건너뜀 (파일 없음): {fname}")
            continue

        t0 = time.perf_counter()
        raw = norm(reader(img_path))
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
                "cer_ns": cer_nospace(ref, fixed),
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
    avg_cer_ns = sum(r["cer_ns"] for r in rows) / n
    ok_raw = sum(r["ok_raw"] for r in rows)
    exact = sum(r["exact"] for r in rows)
    exact_ns = sum(r["exact_nospace"] for r in rows)

    # 필체(그룹)별 집계 — 표본이 한 사람에 치우쳤는지, 편차가 얼마인지 보기 위함
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["font"], []).append(r)

    lines = [
        "# OCR 정확도 리포트",
        "",
        f"- 엔진: **{engine}**",
        f"- 대상: `{datadir}` ({n}건)",
        "",
        "## 요약 — 후처리 전/후 비교",
        "",
        "| 지표 | OCR 원본 | 후처리 적용 | 목표 |",
        "|------|----------|-------------|------|",
        f"| 평균 CER (낮을수록 좋음) | {cer_raw:.3f} | **{avg_cer:.3f}** | 0.15 이하 |",
        f"| 공백 무시 일치율 | {ok_raw}/{n} ({ok_raw / n:.0%}) | **{exact_ns}/{n} ({exact_ns / n:.0%})** | 80% 이상 |",
        f"| 완전 일치율 | - | {exact}/{n} ({exact / n:.0%}) | - |",
        f"| 평균 CER (띄어쓰기 무시) | - | {avg_cer_ns:.3f} | 참고 지표 |",
        "",
        _short_line_note(rows),
        "",
        f"이미지 1장당 평균 처리 시간: **{sum(r['sec'] for r in rows) / n:.2f}초** "
        f"({'GPU' if _gpu_available() else 'CPU'}, 모델 로딩 제외)",
        "",
    ]

    if len(groups) > 1:
        lines += [
            "## 필체별 집계",
            "",
            "| 필체 | 행 수 | 평균 CER | CER(공백무시) | 일치 |",
            "|------|-------|----------|---------------|------|",
        ]
        for g, rs in sorted(groups.items()):
            m = len(rs)
            lines.append(
                f"| {g} | {m} | {sum(x['cer'] for x in rs) / m:.3f} "
                f"| {sum(x['cer_ns'] for x in rs) / m:.3f} "
                f"| {sum(x['exact_nospace'] for x in rs)}/{m} |"
            )
        spread = [sum(x["cer"] for x in rs) / len(rs) for rs in groups.values()]
        lines += [
            "",
            f"필체 간 CER 편차: **{min(spread):.3f} ~ {max(spread):.3f}** "
            f"(최대/최소 {max(spread) / max(min(spread), 1e-9):.1f}배)",
            "",
        ]

    lines += [
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

    # 행별 원시 결과를 함께 남긴다 — pagecompare 가 재호출 없이 재사용한다
    outfile.with_suffix(".json").write_text(
        json.dumps(
            [{"file": r["file"], "ref": r["ref"], "hyp": r["hyp"]} for r in rows],
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    print(f"\n평균 CER  {cer_raw:.3f} → {avg_cer:.3f}")
    print(f"일치율    {ok_raw}/{n} → {exact_ns}/{n}  (후처리 전 → 후)")
    print(f"리포트 → {outfile}")


def pagecompare(datadir: Path, lines_json: Path, outfile: Path, engine: str):
    """페이지 통째로 1회 호출 vs 행별 여러 회 호출을 페이지 단위 CER 로 비교.

    행별 결과는 앞선 `run` 이 남긴 JSON 을 재사용하므로 추가 호출이 없다.
    페이지 쪽만 이미지 수만큼(=5회) 호출한다.
    """
    if not lines_json.exists():
        sys.exit(
            f"{lines_json} 가 없습니다.\n"
            f"먼저 행별 측정을 실행하세요: python poc.py run --data {datadir} --engine {engine}"
        )

    rows = json.loads(lines_json.read_text(encoding="utf-8"))
    pages: dict[str, list] = {}
    for r in rows:
        pages.setdefault(r["file"].split("_")[0], []).append(r)

    reader = make_reader(engine)
    out = []
    for stem, rs in sorted(pages.items()):
        img = datadir / f"{stem}_page.jpg"
        if not img.exists():
            print(f"  건너뜀 (전면 이미지 없음): {img.name}")
            continue

        ref = norm(" ".join(r["ref"] for r in rs))
        by_line = norm(" ".join(r["hyp"] for r in rs))

        t0 = time.perf_counter()
        whole = norm(postcorrect(reader(img)))
        sec = time.perf_counter() - t0

        out.append(
            {
                "page": stem,
                "n_lines": len(rs),
                "cer_line": cer(ref, by_line),
                "cer_page": cer(ref, whole),
                "cer_line_ns": cer_nospace(ref, by_line),
                "cer_page_ns": cer_nospace(ref, whole),
                "sec": sec,
                "whole": whole,
                "ref": ref,
            }
        )
        r = out[-1]
        print(
            f"  {stem}: 행별 CER {r['cer_line']:.3f} → 페이지 CER {r['cer_page']:.3f} "
            f"({sec:.1f}초)"
        )

    if not out:
        sys.exit("비교할 페이지가 없습니다. `python prepare_real.py` 를 먼저 실행하세요.")

    n = len(out)
    calls_line = sum(r["n_lines"] for r in out)
    avg_l = sum(r["cer_line"] for r in out) / n
    avg_p = sum(r["cer_page"] for r in out) / n
    avg_l_ns = sum(r["cer_line_ns"] for r in out) / n
    avg_p_ns = sum(r["cer_page_ns"] for r in out) / n

    lines = [
        f"# 페이지 통째 vs 행별 분할 비교 — {engine}",
        "",
        f"같은 정답·같은 엔진으로 **입력 단위만** 바꿔 측정했다. 대상 {n}페이지.",
        "",
        "| 항목 | 행별 분할 | 페이지 통째 |",
        "|------|-----------|-------------|",
        f"| API 호출 수 | {calls_line}회 | **{n}회** ({calls_line / n:.1f}배 절감) |",
        f"| 평균 CER | {avg_l:.3f} | {avg_p:.3f} |",
        f"| 평균 CER (띄어쓰기 무시) | {avg_l_ns:.3f} | {avg_p_ns:.3f} |",
        "",
        "## 페이지별",
        "",
        "| 페이지 | 행 수 | 행별 CER | 페이지 CER | 차이 | 페이지 처리시간 |",
        "|--------|-------|----------|------------|------|-----------------|",
    ]
    for r in out:
        d = r["cer_page"] - r["cer_line"]
        lines.append(
            f"| {r['page']} | {r['n_lines']} | {r['cer_line']:.3f} | {r['cer_page']:.3f} "
            f"| {d:+.3f} | {r['sec']:.1f}초 |"
        )

    better = sum(1 for r in out if r["cer_page"] < r["cer_line"])
    lines += [
        "",
        f"페이지 통째가 더 정확한 경우: **{better}/{n}**",
        "",
        "## 페이지 통째 인식 결과 (원문 대조)",
        "",
    ]
    for r in out:
        lines += [
            f"### {r['page']}",
            "",
            f"- 정답: `{r['ref'][:300]}`",
            f"- 결과: `{r['whole'][:300]}`",
            "",
        ]

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n호출 수  {calls_line}회 → {n}회 ({calls_line / n:.1f}배 절감)")
    print(f"평균 CER {avg_l:.3f} (행별) vs {avg_p:.3f} (페이지)")
    print(f"페이지가 더 정확: {better}/{n}")
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

    # surya 결과 파싱 (엔진 미설치 상태에서도 검증되도록 가짜 객체 사용)
    class _B:
        def __init__(self, html):
            self.html = html

    class _R:
        def __init__(self, blocks=None, text_lines=None):
            self.blocks, self.text_lines = blocks, text_lines

    assert norm(_surya_text(_R(blocks=[_B("<p>안녕</p>"), _B("<p>하세요</p>")]))) == "안녕 하세요"
    assert norm(_surya_text(_R(blocks=[_B("<p>&lt;답안&gt;</p>")]))) == "<답안>", "HTML 엔티티 복원"

    class _L:
        def __init__(self, text):
            self.text = text

    assert norm(_surya_text(_R(text_lines=[_L("가나"), _L("다라")]))) == "가나 다라", "구버전 형태"
    assert norm(_surya_text(_R())) == "", "빈 결과"

    # CLOVA 응답 파싱 (키·네트워크 없이 검증)
    clova = {
        "images": [
            {
                "fields": [
                    {"inferText": "기출문제를", "inferConfidence": 0.99},
                    {"inferText": "다", "inferConfidence": 0.98},
                    {"inferText": "푼", "inferConfidence": 0.97},
                ]
            }
        ]
    }
    assert _clova_text(clova) == "기출문제를 다 푼"
    assert _clova_text({"images": [{"fields": []}]}) == "", "빈 인식 결과"
    assert _clova_text({}) == "", "이미지 없음"
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
    r.add_argument(
        "--engine", choices=["easyocr", "surya", "clova"], default="easyocr"
    )

    pc = sub.add_parser("pagecompare", help="페이지 통째 vs 행별 분할 비교")
    pc.add_argument("--data", type=Path, default=ROOT / "data" / "real")
    pc.add_argument(
        "--lines",
        type=Path,
        required=True,
        help="앞선 run 이 남긴 행별 결과 JSON (예: results/report_real_clova.json)",
    )
    pc.add_argument("--out", type=Path, default=ROOT / "results" / "pagecompare.md")
    pc.add_argument(
        "--engine", choices=["easyocr", "surya", "clova"], default="clova"
    )

    sub.add_parser("selfcheck", help="지표 계산 self-test")

    a = p.parse_args()
    if a.cmd == "gen":
        gen(a.out, a.seed)
    elif a.cmd == "run":
        run(a.data, a.out, a.engine)
    elif a.cmd == "pagecompare":
        pagecompare(a.data, a.lines, a.out, a.engine)
    else:
        selfcheck()
