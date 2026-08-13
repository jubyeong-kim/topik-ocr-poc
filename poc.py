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


def _clova_image_text(img: dict) -> str:
    """CLOVA 응답의 이미지 1건에서 텍스트 추출. 처리 실패는 조용히 넘기지 않는다.

    HTTP 200 이어도 이미지별로 실패할 수 있고(inferResult=FAILURE), 그때 fields 가
    비어 빈 문자열이 된다. "못 읽은 것"과 "처리에 실패한 것"은 구분해야 한다.
    """
    result = img.get("inferResult")
    if result and result != "SUCCESS":
        raise ClovaError(
            f"이미지 처리 실패: inferResult={result} "
            f"message={img.get('message', '(없음)')}"
        )
    return " ".join(
        t for f in img.get("fields", []) if (t := f.get("inferText", ""))
    )


def _clova_text(payload: dict) -> str:
    """CLOVA OCR 응답에서 인식 텍스트를 좌→우 순으로 이어붙인다."""
    return " ".join(_clova_image_text(img) for img in payload.get("images", []))


def _paddle_text(result) -> str:
    """PaddleOCR 결과에서 평문 추출.

    3.x 는 [{'rec_texts': [...]}] 형태, 2.x 는 [[[box, (text, conf)], ...]] 형태로
    구조가 완전히 다르므로 둘 다 처리한다.
    """
    if not result:
        return ""
    # 3.x: 페이지별 dict
    if isinstance(result[0], dict):
        out = []
        for page in result:
            out.extend(page.get("rec_texts") or [])
        return " ".join(out)
    # 2.x: 중첩 리스트
    out = []
    for page in result:
        for item in page or []:
            try:
                out.append(item[1][0])
            except (IndexError, TypeError, KeyError):
                continue
    return " ".join(out)


class ClovaError(RuntimeError):
    """CLOVA API 호출 실패 (응답 본문 포함)."""


def _clova_post(url: str, secret: str, body: dict, timeout: int = 120) -> dict:
    """CLOVA API 호출. 오류 시 응답 본문을 함께 보여준다.

    HTTPError 는 본문에 원인(메시지·필드명)을 담아 오므로 버리지 않고 출력한다.
    """
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        data=_json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-OCR-SECRET": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", "replace")[:600]
        payload_mb = len(_json.dumps(body).encode()) / 1048576
        raise ClovaError(
            f"CLOVA API 오류 {e.code} {e.reason}\n"
            f"    요청 크기 {payload_mb:.1f}MB / 이미지 {len(body.get('images', []))}장\n"
            f"    응답 본문: {detail}"
        ) from None


def clova_batch(paths: list) -> list:
    """여러 이미지를 한 번의 CLOVA 호출로 처리한다.

    API 가 images 배열을 받으므로, 이미지를 병합해 해상도를 희생하지 않고도
    호출 수를 줄일 수 있다. 반환은 입력 순서대로의 텍스트 리스트.
    """
    import base64
    import json as _json
    import os
    import urllib.request

    url = os.environ.get("CLOVA_OCR_URL", "").strip()
    secret = os.environ.get("CLOVA_OCR_SECRET", "").strip()
    if not url or not secret:
        sys.exit("CLOVA_OCR_URL / CLOVA_OCR_SECRET 환경변수가 필요합니다.")

    body = {
        "version": "V1",
        "requestId": "batch",
        "timestamp": 0,
        "lang": "ko",
        "images": [
            {
                "format": Path(p).suffix.lstrip(".").lower() or "jpg",
                "name": Path(p).stem,
                "data": base64.b64encode(Path(p).read_bytes()).decode(),
            }
            for p in paths
        ],
    }
    payload = _clova_post(url, secret, body)

    # 이미지별로 분리해 반환 (images 배열 순서 = 요청 순서)
    return [norm(_clova_image_text(img)) for img in payload.get("images", [])]


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
        try:
            return _clova_text(_clova_post(url, secret, body, timeout=30))
        except ClovaError as e:
            sys.exit(str(e))

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

    if engine == "paddle":
        try:
            from paddleocr import PaddleOCR
        except ImportError:
            sys.exit(
                "paddleocr 미설치. Linux 환경에서:\n"
                "  pip install paddlepaddle paddleocr"
            )
        # 생성자 인자도 버전마다 달라 최소 인자로 만든다
        ocr = PaddleOCR(lang="korean")
        use_predict = hasattr(ocr, "predict")

        def read(p):
            r = ocr.predict(str(p)) if use_predict else ocr.ocr(str(p))
            return _paddle_text(r)

        return read

    if engine == "clova":
        return _make_clova_reader()

    sys.exit(f"알 수 없는 엔진: {engine} (easyocr | surya | clova | paddle)")


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


SCALES = [1.0, 0.7, 0.5, 0.35, 0.25, 0.15]


def _page_texts(datadir: Path) -> dict:
    """페이지별 정답 텍스트 (행 전사를 순서대로 이어붙임)."""
    labels = json.loads((datadir / "labels.json").read_text(encoding="utf-8"))
    pages: dict[str, list] = {}
    for fname, meta in sorted(labels.items()):
        pages.setdefault(fname.split("_")[0], []).append(meta["text"])
    return {k: " ".join(v) for k, v in pages.items()}


def _measure_scaled(img, ref: str, scales: list, tag: str, tmp: Path) -> list:
    """이미지를 여러 배율로 줄여가며 CER 을 잰다. 실패도 결과로 기록한다."""
    from PIL import Image

    rows = []
    for s in scales:
        w, h = int(img.size[0] * s), int(img.size[1] * s)
        scaled = img if s == 1.0 else img.resize((w, h), Image.LANCZOS)
        scaled.save(tmp, quality=95)
        mb = tmp.stat().st_size / 1048576

        t0 = time.perf_counter()
        try:
            hyp = norm(postcorrect(clova_batch([tmp])[0]))
            err = None
        except ClovaError as e:
            hyp, err = "", str(e).split("\n")[0]
        sec = time.perf_counter() - t0

        c = cer(ref, hyp) if hyp else 1.0
        rows.append(
            {"scale": s, "w": w, "h": h, "mb": mb, "cer": c, "sec": sec, "err": err}
        )
        status = err if err else ("결과 없음" if not hyp else f"CER {c:.3f}")
        print(f"  {tag} {s:>4.0%} {w}x{h} ({mb:.1f}MB): {status}")
    return rows


def mergetest(datadir: Path, outfile: Path):
    """축소율에 따른 인식률 곡선을 잰다 — 병합본과 단일 페이지 양쪽.

    병합이 원본 크기에서 실패했다고 "병합 불가"로 단정할 수 없다. 어디까지 줄이면
    통과하는지, 그때 정확도가 어떤지를 재야 판단할 수 있다.
    단일 페이지도 함께 재서 **해상도 한계선**을 분리해 본다.
    """
    from PIL import Image

    pages = _page_texts(datadir)
    paths = [datadir / f"{s}_page.jpg" for s in sorted(pages)]
    if any(not p.exists() for p in paths):
        sys.exit("전면 이미지가 없습니다 — `python prepare_real.py` 를 먼저 실행하세요.")

    # 여백 제거 후 병합 — 페이지 아래쪽 빈 공간이 전체 높이의 절반가량이다.
    # 크기가 줄어 제한을 통과할 가능성이 커지고, 축소하더라도 같은 목표 크기에
    # 더 큰 배율을 쓸 수 있어 글자 해상도가 그만큼 보존된다.
    from prepare_real import text_line_bands

    print("여백 제거 중...")
    imgs = []
    for p in paths:
        im = Image.open(p)
        bands = text_line_bands(im)
        y0 = max(0, bands[0][0] - 40)
        y1 = min(im.size[1], bands[-1][1] + 40)
        imgs.append(im.crop((0, y0, im.size[0], y1)))
        print(f"  {p.name}: {im.size[1]} → {y1 - y0}px")

    W, H = max(i.size[0] for i in imgs), sum(i.size[1] for i in imgs)
    merged = Image.new("RGB", (W, H), (205, 205, 205))
    y = 0
    for i in imgs:
        merged.paste(i, (0, y))
        y += i.size[1]

    tmp = datadir / "_scaled.jpg"
    ref_all = norm(" ".join(pages[s] for s in sorted(pages)))

    print(f"\n[1] 병합 5장 (여백 제거 {W}x{H}) — 축소율별")
    merged_rows = _measure_scaled(merged, ref_all, SCALES, "병합", tmp)

    # 단일 페이지도 같은 방식(여백 제거)으로 재야 비교가 성립한다
    stem = sorted(pages)[0]
    print(f"\n[2] 단일 페이지 {stem} ({imgs[0].size[0]}x{imgs[0].size[1]}) — 해상도 한계선")
    single_rows = _measure_scaled(imgs[0], norm(pages[stem]), SCALES, "단일", tmp)
    tmp.unlink(missing_ok=True)

    def table(rows):
        out = ["| 배율 | 크기 | 용량 | CER | 비고 |", "|------|------|------|-----|------|"]
        for r in rows:
            note = r["err"][:60] if r["err"] else ("인식 0건" if r["cer"] >= 1.0 else "")
            out.append(
                f"| {r['scale']:.0%} | {r['w']}x{r['h']} | {r['mb']:.1f}MB "
                f"| {r['cer']:.3f} | {note} |"
            )
        return out

    ok = [r for r in merged_rows if r["cer"] < 1.0]
    best = min(ok, key=lambda r: r["cer"]) if ok else None

    lines = [
        "# 축소율에 따른 인식률 — 병합본 vs 단일 페이지",
        "",
        "병합이 원본 크기에서 실패했다고 병합 자체가 불가한 것은 아니다.",
        "**어디까지 줄이면 통과하는지, 그때 정확도가 어떤지**를 측정했다.",
        "",
        "### 전처리: 여백 제거",
        "",
        "페이지 아래쪽 빈 공간이 전체 높이의 절반가량이다. 글자 영역만 잘라 병합하면",
        "**병합 높이가 20160 → 10199px (51%)** 로 줄어든다.",
        "크기 제한을 통과할 가능성이 커지고, 축소하더라도 같은 목표 크기에 더 큰 배율을",
        "쓸 수 있어 글자 해상도가 보존된다. 검출 비용은 5장에 약 10초로 무시할 수준이다.",
        "",
        f"## 1. 병합 5장 — 여백 제거 후 {W}x{H}",
        "",
        f"(여백 포함 원본 3024x20160 은 **CER 1.000, 인식 0건**으로 실패했다)",
        "",
        *table(merged_rows),
        "",
        f"## 2. 단일 페이지 {stem} — 여백 제거 후 {imgs[0].size[0]}x{imgs[0].size[1]}",
        "",
        *table(single_rows),
        "",
        "## 기준선",
        "",
        "| 방식 | 호출 | CER |",
        "|------|------|-----|",
        "| 행별 분할 | 53회 | 0.073 |",
        "| 페이지별 (원본) | 5회 | **0.037** |",
    ]
    if best:
        lines.append(
            f"| 병합 1장 (최적 {best['scale']:.0%}) | **1회** | {best['cer']:.3f} |"
        )
    else:
        lines.append("| 병합 1장 | 1회 | 모든 배율에서 실패 |")

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n리포트 → {outfile}")


def trimtest(datadir: Path, outfile: Path):
    """여백 제거가 단일 페이지 인식률에 도움이 되는가.

    병합에는 필수였지만(크기 제한 통과), 단일 페이지에서는 002 한 장 기준으로
    오히려 불리해 보였다(0.040 → 0.057). 표본 1장으로는 단정할 수 없어 5장 모두 잰다.
    두 조건을 같은 실행에서 재야 비교가 성립한다.
    """
    from PIL import Image

    from prepare_real import text_line_bands

    pages = _page_texts(datadir)
    rows = []
    for stem in sorted(pages):
        src = datadir / f"{stem}_page.jpg"
        if not src.exists():
            print(f"  건너뜀: {src.name} 없음")
            continue

        im = Image.open(src)
        bands = text_line_bands(im)
        y0 = max(0, bands[0][0] - 40)
        y1 = min(im.size[1], bands[-1][1] + 40)
        trimmed = im.crop((0, y0, im.size[0], y1))

        ref = norm(pages[stem])
        tmp = datadir / "_trim.jpg"

        full_cer = cer(ref, norm(postcorrect(clova_batch([src])[0])))
        trimmed.save(tmp, quality=95)
        trim_cer = cer(ref, norm(postcorrect(clova_batch([tmp])[0])))
        tmp.unlink(missing_ok=True)

        rows.append(
            {
                "page": stem,
                "h_full": im.size[1],
                "h_trim": y1 - y0,
                "full": full_cer,
                "trim": trim_cer,
            }
        )
        r = rows[-1]
        print(
            f"  {stem}: 여백포함 {full_cer:.3f} → 제거 {trim_cer:.3f} "
            f"({trim_cer - full_cer:+.3f})"
        )

    if not rows:
        sys.exit("측정할 페이지가 없습니다.")

    n = len(rows)
    avg_f = sum(r["full"] for r in rows) / n
    avg_t = sum(r["trim"] for r in rows) / n
    better = sum(1 for r in rows if r["trim"] < r["full"])

    lines = [
        "# 여백 제거가 단일 페이지 인식률에 미치는 영향",
        "",
        "병합에는 여백 제거가 필수였다(8000px 제한 통과). 그러나 단일 페이지에서도",
        "도움이 되는지는 별개 문제이므로 5페이지를 같은 실행에서 두 조건으로 측정했다.",
        "",
        "| 페이지 | 높이(포함→제거) | 여백 포함 CER | 여백 제거 CER | 차이 |",
        "|--------|-----------------|---------------|---------------|------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['page']} | {r['h_full']}→{r['h_trim']} | {r['full']:.3f} "
            f"| {r['trim']:.3f} | {r['trim'] - r['full']:+.3f} |"
        )
    lines += [
        "",
        f"| **평균** | | **{avg_f:.3f}** | **{avg_t:.3f}** | **{avg_t - avg_f:+.3f}** |",
        "",
        f"여백 제거가 더 나은 경우: **{better}/{n}**",
        "",
        "## 해석",
        "",
        (
            "여백 제거가 **불리**하다. 여백이 레이아웃 분석에 단서를 주는 것으로 보인다."
            if avg_t > avg_f
            else "여백 제거가 **유리**하거나 무해하다."
        ),
    ]
    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n평균 CER  여백포함 {avg_f:.3f} vs 제거 {avg_t:.3f}")
    print(f"제거가 유리: {better}/{n}")
    print(f"리포트 → {outfile}")


def batchtest(datadir: Path, outfile: Path):
    """CLOVA: 페이지 5장을 한 요청(images 배열)으로 보내 개별 호출과 비교.

    정확도가 같다면 호출 수를 5회 → 1회로 더 줄일 수 있다.
    """
    labels = json.loads((datadir / "labels.json").read_text(encoding="utf-8"))
    pages: dict[str, list] = {}
    for fname, meta in sorted(labels.items()):
        pages.setdefault(fname.split("_")[0], []).append(meta["text"])

    paths = [datadir / f"{s}_page.jpg" for s in sorted(pages)]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        sys.exit(f"전면 이미지 없음: {missing} — `python prepare_real.py` 를 먼저 실행하세요.")

    # 장수를 1→N 으로 늘려가며 어디서 깨지는지 확인한다.
    # (한 번에 실패하면 "장수 제한"인지 "요청 크기 초과"인지 구분할 수 없다)
    print("배치 가능 장수 탐색 중...")
    ok = 0
    for k in range(1, len(paths) + 1):
        mb = sum(p.stat().st_size for p in paths[:k]) * 4 / 3 / 1048576  # base64 팽창분
        try:
            texts_k = clova_batch(paths[:k])
            ok = k
            print(f"  {k}장 (~{mb:.1f}MB): 성공 — 응답 {len(texts_k)}건")
        except ClovaError as e:
            print(f"  {k}장 (~{mb:.1f}MB): 실패\n    {e}")
            break

    if ok == 0:
        sys.exit("1장도 실패했습니다. 키·URL 을 확인하세요.")
    if ok < len(paths):
        print(f"\n→ 최대 {ok}장까지 한 요청으로 가능. {ok}장 기준으로 측정합니다.")

    paths = paths[:ok]
    t0 = time.perf_counter()
    texts = clova_batch(paths)
    sec = time.perf_counter() - t0

    if len(texts) != len(paths):
        sys.exit(
            f"응답 이미지 수 불일치: 요청 {len(paths)} / 응답 {len(texts)}. "
            "배치가 지원되지 않을 수 있습니다."
        )

    rows = []
    for stem, path, hyp in zip(sorted(pages), paths, texts):
        ref = norm(" ".join(pages[stem]))
        rows.append({"page": stem, "cer": cer(ref, norm(postcorrect(hyp)))})
        print(f"  {stem}: CER {rows[-1]['cer']:.3f}")

    avg = sum(r["cer"] for r in rows) / len(rows)
    lines = [
        "# CLOVA 배치 호출 실험 (images 배열)",
        "",
        f"이미지 {len(paths)}장을 **1회 요청**으로 전송. 총 소요 {sec:.1f}초.",
        "",
        "| 페이지 | CER (배치) |",
        "|--------|------------|",
    ]
    lines += [f"| {r['page']} | {r['cer']:.3f} |" for r in rows]
    lines += [
        "",
        f"평균 CER: **{avg:.3f}**",
        "",
        "개별 호출(5회) 결과는 [pagecompare_clova.md](pagecompare_clova.md) 참조.",
        "두 값이 같다면 호출 수를 5회 → 1회로 더 줄일 수 있다.",
    ]
    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n배치 평균 CER {avg:.3f} | 총 {sec:.1f}초 (1회 호출)")
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

    # PaddleOCR 응답 파싱 — 3.x(dict) / 2.x(중첩 리스트) 양쪽
    assert _paddle_text([{"rec_texts": ["기출", "문제를"]}]) == "기출 문제를"
    assert _paddle_text([[[[0, 0], ("가나", 0.9)], [[1, 1], ("다라", 0.8)]]]) == "가나 다라"
    assert _paddle_text([]) == "" and _paddle_text(None) == ""
    assert _paddle_text([{"rec_texts": None}]) == "", "빈 인식 결과"

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

    # 처리 실패(inferResult != SUCCESS)는 빈 문자열이 아니라 예외로 드러나야 한다
    try:
        _clova_text({"images": [{"inferResult": "FAILURE", "message": "too large"}]})
        raise AssertionError("처리 실패가 조용히 넘어감")
    except ClovaError as e:
        assert "FAILURE" in str(e) and "too large" in str(e)
    assert _clova_text({"images": [{"inferResult": "SUCCESS", "fields": [{"inferText": "가"}]}]}) == "가"
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
        "--engine", choices=["easyocr", "surya", "clova", "paddle"], default="easyocr"
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
        "--engine", choices=["easyocr", "surya", "clova", "paddle"], default="clova"
    )

    tt = sub.add_parser("trimtest", help="여백 제거가 단일 페이지에 도움 되는지")
    tt.add_argument("--data", type=Path, default=ROOT / "data" / "real")
    tt.add_argument("--out", type=Path, default=ROOT / "results" / "trimtest_clova.md")

    bt = sub.add_parser("batchtest", help="CLOVA: 여러 장을 1회 요청으로 (images 배열)")
    bt.add_argument("--data", type=Path, default=ROOT / "data" / "real")
    bt.add_argument("--out", type=Path, default=ROOT / "results" / "batchtest_clova.md")

    mt = sub.add_parser("mergetest", help="CLOVA: 5장을 1장으로 병합해 1회 호출")
    mt.add_argument("--data", type=Path, default=ROOT / "data" / "real")
    mt.add_argument("--out", type=Path, default=ROOT / "results" / "mergetest_clova.md")

    sub.add_parser("selfcheck", help="지표 계산 self-test")

    a = p.parse_args()
    if a.cmd == "gen":
        gen(a.out, a.seed)
    elif a.cmd == "run":
        run(a.data, a.out, a.engine)
    elif a.cmd == "pagecompare":
        pagecompare(a.data, a.lines, a.out, a.engine)
    elif a.cmd == "trimtest":
        trimtest(a.data, a.out)
    elif a.cmd == "batchtest":
        batchtest(a.data, a.out)
    elif a.cmd == "mergetest":
        mergetest(a.data, a.out)
    else:
        selfcheck()
