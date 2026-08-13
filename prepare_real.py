"""
실제 손글씨 샘플 준비 (Nexdata 공개 데모 이미지)

A4 손글씨 페이지를 행 단위로 잘라 TOPIK 51/52번 답안(한 문장)과 같은 조건으로 만든다.
데모 이미지에는 어노테이션 박스가 초록색으로 인쇄되어 있어, 이를 이용해 행 위치를
자동 검출하고 잘라낸 뒤 박스 자체는 배경색으로 지운다.

  python prepare_real.py              전체 이미지 처리
  python prepare_real.py --detect     행 검출 결과만 확인 (자르지 않음)

정답(ground truth)은 사람이 눈으로 읽어 TRANSCRIPTS 에 적어 둔 것을 사용한다.
전사에는 따옴표·문장부호를 **원문 그대로** 포함한다.

주의: 원본 이미지는 상업 라이선스라 저장소에 커밋하지 않는다 (.gitignore).
      아래 SOURCE_BASE 에서 직접 내려받아야 한다.
"""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops

SOURCE_BASE = (
    "https://raw.githubusercontent.com/Nexdata-AI/"
    "5711-Images-Korean-Handwriting-OCR-data/main"
)
REAL_DIR = Path(__file__).parent / "data" / "real"

# 사람이 눈으로 읽은 전사 (따옴표·문장부호 포함, 원문 그대로)
# 자동 검출된 행 순서(위→아래)와 1:1 대응해야 한다.
TRANSCRIPTS: dict[str, list[str]] = {
    "010_demo.jpg": [
        "학원 선생님이 아무렇지 않게 작년 기출 문제와 재작년 기출 문제를 나누어 주셨다.",
        "복사 된 기출 문제의 아래쪽에는 '이 시험문제의 저작권은 OO중학교에 있습",
        "니다. 저작권법에 의해 보호받는 저작물이므로 전재와 복제는 금지되며, 이를 어",
        "길시 저작권법에 의거처벌될 수 있습니다.'라고 적혀 있었다. 나는 생각했다.",
        "어... 이거 복사하면 안된다는데 이렇게 복사해서 써도 되는건가?' 그리고",
        '나는 학원 선생님께 질문했다. "선생님, 시험지 복제 금지라는데 풀어도 되요?"',
        '그러자 선생님은 늘 이렇게 해왔다는 듯이 "뭔 상관이야, 다른 학원도 다 똑',
        '같아. 그럼 너는 불법이니까 안 풀거야?"라고 하셨다. 그러자 내 마음은',
        "두개로 갈라졌다. '내 시험을 위하는 건데 어차피 다른 학원도 다 푼다니까",
        "나도 풀어도 되지'와 '아무리 그래도 법을 위반하는 행위는 하면 안 돼'는",
        "계속해서 나를 갈등하게 만들었고. 내 결론은 풀어도 되겠지였다. 처",
        "음 문제를 받고 풀기 시작할 때는 양심에 찔렸다. 하지만 양심의 가",
        "책도 오래가지 못했다. 나는 누구보다 문제를 열심히 풀고 있었고 좀",
        "전까지 하던 고민은 이미 저 멀리 사라져 버렸다.",
    ],
    "009_demo.jpg": [
        '"헐, 어떻게 기말고사 1주일 남음" 친구들과 곧 다가올 기말고사에 대해 떠들',
        '고 있었다. "야, 그거 알아? 기출 문제 나왔데" 친구 한 명이 말했다. 그러자 우리는',
        "쏜살같이 기출 문제가 나왔다는 곳으로 갔다. 기출 문제를 읽다 보니 너무 많아",
        "쉬는 시간이 끝났고 우리는 교실로 돌아왔다. 종례후 우리는 다 함께 학원으로",
        "향했다. 학원에서는 기말 대비로 바빴고 아이들에게 시험보고나면 점수 말하",
        "고 시험지를 가지고 오라 했대. 처음에는 이해가 가지 않았다. '왜 시험지를",
        "가지고 오라하지?' 의문이 있었지만 그의문은 학원수 업이 시작하고 얼마",
        "지나지 않아 풀렸다.",
    ],
    "008_demo.jpg": [
        "소프트웨어 산업이나 콘텐츠에서 창작은 AI를 활용하는 주요 분야 중 하나",
        "이지만 대륙법을 따르는 우리나라는 인간이 만든 창작물을 기준으로 저작권",
        "의 개념이 마련되어 있어 현행법상 AI 저작물에 대한 명확한 규정이 없습",
        "니다. 인공지능과 예술 산업의 발전을위해서, AI 저작권, 더 나아가 저작",
        '권법까지, 우리 모두가 주의를 기울여 살펴봐야 할 때가 찾아왔습니다."',
        "가속화되는 기술의 발전과 인간의 생활 양식의 변화는 우리가 알고 있듯이,",
        "인간의 일이 계속될 수없는 것을 넘어서는 경쟁의 역사에서 어떤 본",
        '질적인 특이성에 접근하는 모습에 다가가게 한다."는 존 폰 노이만의',
        "말, 한번 쯤 새겨보는 건 어떨까요?",
    ],
    "007_demo.jpg": [
        "1990년대부터 2022년까지, 저작권 침해의 역사는 인간이 창작을 시작한 그 순간",
        # '폼출할'로 전사했다가 확대 판독으로 정정 — ㅁ 받침이 없고 ㅍ 구조가 뚜렷하다
        "부터 시작되었습니다. 다양한 생각을 표출할 수 있는 것이 기술의 발전이라 하지만, 단순히",
        "저작물이 대량 생산되고, 대량 소비된다고 하여 문화의 총량이 확대되고, 향상 발전",
        "된 다고 보기에는 힘들다는 것이죠. 우리는 저작 권법의 본질을 고려하고, 그것이 비칠 부정",
        "적인 영향을 고려할 필요가 있는 것입니다.",
        "AI가 미술, 음악, 작문 등 창작 영역에 발을 들이면서 AI의 창작물에 대한",
        "저작권에대한 논의는 지금까지 이어지고 있습니다. 과연 AI가 저작권 취득을 위해",
        "자연인 혹은 법 인으로서 권리를 가질 수 있을까요? 또, 창작 AI가 학습 과정에",
        "서 실제 작가의 예술작품을 사용했을 경우 이에 대한 저작권은 어떻게 해야",
        "할가요?",
    ],
    "002_demo.jpg": [
        "기출문제를 다 푼 뒤 선생님이 기말고사를 볼 때 볼펜으로 풀지 말고 연필로 연하게",
        "풀어서 가져오라고 하셨다. 그 이유는 다름아닌 이번에 본 기말고사 문제지에 답을 푼 흔",
        "적을 없애고 다음 2학년에게 나누어 주어 기출 문제를 풀어 볼 수 있게 하기 위해",
        "서 이였다. 나는 학교 저작권 수업 시간 중에 들었던 말이 생각났다. ' \"학원에서",
        "기출문제를 함부로 복제해서 쓰다가 저작권자에게 걸려서 신고를 받은 적이 있어",
        "요. 즉 학원에서 기출문제를 복제하는 것도 저작권법 위반입니다.\"라고 저작권",
        "선생님이 알려 주셨는데 기출문제을 학원에 주면 안 되지않을까?' 그래서",
        "나는 학원 선생님께 \"안 가져오면 안돼요?\" 하고 물어봤다. 그러자 \"그럼",
        "되겠니? 너도 작년 선배들이 가져온 문제 풀었잖아. 근데 너는 안가져",
        "오겠다고? 그건 도둑놈 심보야.\" 하셨다. 나는 할 말을 잃었다. 다 맞는",
        "말이여서 반박할 수 없었다. 나는 아무말 없이 친구들과 집으로 향했",
        "다.",
    ],
}


def text_line_bands(
    im: Image.Image, min_height: int = 30, min_dark: int = 2
) -> list[tuple[int, int]]:
    """글자(어두운 픽셀)의 가로 투영으로 행 구간 [(y0, y1), ...] 을 검출한다.

    어노테이션 박스로 검출하면 박스가 기울어져 위아래 행의 세로 구간이 겹칠 때
    두 행이 하나로 병합된다. 글자는 행 사이에 확실한 여백이 있어 더 안정적이다.
    """
    gray = remove_annotation_boxes(im).convert("L")
    w, h = gray.size
    px = gray.load()
    step = max(1, w // 500)  # 가로는 듬성듬성 훑어도 충분하다

    is_text = [
        sum(1 for x in range(0, w, step) if px[x, y] < 110) >= min_dark
        for y in range(h)
    ]

    bands, start = [], None
    for y, flag in enumerate(is_text):
        if flag and start is None:
            start = y
        elif not flag and start is not None:
            if y - start >= min_height:
                bands.append((start, y))
            start = None
    if start is not None and h - start >= min_height:
        bands.append((start, h))
    return bands


def remove_annotation_boxes(im: Image.Image) -> Image.Image:
    """초록 어노테이션 박스를 배경색으로 지운다 (OCR 교란 요인 제거).

    조건은 g > r+40 and g > b+40. 픽셀 단위 파이썬 루프 대신 채널 연산으로 처리한다
    (전면 페이지는 1200만 픽셀이라 루프로는 수 분이 걸린다).
    """
    im = im.convert("RGB")
    r, g, b = im.split()
    # ImageChops.subtract 는 0 에서 클리핑되므로 g-r, g-b 가 40 초과인 화소만 남는다
    over_r = ImageChops.subtract(g, r).point(lambda v: 255 if v > 40 else 0)
    over_b = ImageChops.subtract(g, b).point(lambda v: 255 if v > 40 else 0)
    mask = ImageChops.logical_and(over_r.convert("1"), over_b.convert("1"))
    im.paste((205, 205, 205), mask=mask)
    return im


def process(name: str, texts: list[str], labels: dict, detect_only: bool) -> None:
    src = REAL_DIR / name
    if not src.exists():
        print(f"  건너뜀 — {src} 없음. 내려받기:")
        print(f'    curl -L -o "{src}" {SOURCE_BASE}/{name}')
        return

    im = Image.open(src)
    bands = text_line_bands(im)
    stem = name.split("_")[0]
    print(f"  {name}: 행 {len(bands)}개 검출 / 전사 {len(texts)}개")

    if len(bands) != len(texts):
        print(f"    ⚠️ 개수 불일치 → 건너뜀 (전사를 검출 행 수에 맞춰 주세요)")
        return
    if detect_only:
        return

    width = im.size[0]
    for i, ((y0, y1), text) in enumerate(zip(bands, texts), 1):
        pad = 12
        crop = im.crop((0, max(0, y0 - pad), width, min(im.size[1], y1 + pad)))
        fname = f"{stem}_line{i:02d}.png"
        remove_annotation_boxes(crop).save(REAL_DIR / fname)
        labels[fname] = {"text": text, "font": f"실제손글씨-{stem}"}

    # 페이지 통째 비교(pagecompare)용 — 행 크롭과 동일하게 박스를 지운 전면 이미지.
    # PNG 로 두면 8~9MB 라 base64 전송 시 API 크기 제한에 걸린다 → 고품질 JPEG.
    remove_annotation_boxes(im).save(REAL_DIR / f"{stem}_page.jpg", quality=95)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", action="store_true", help="행 검출 결과만 확인")
    a = ap.parse_args()

    labels: dict = {}
    for name, texts in TRANSCRIPTS.items():
        process(name, texts, labels, a.detect)

    if a.detect:
        return
    if not labels:
        raise SystemExit("생성된 행이 없습니다.")

    (REAL_DIR / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    writers = {v["font"] for v in labels.values()}
    print(f"\n행 이미지 {len(labels)}건 / 필체 {len(writers)}종 → {REAL_DIR}")
    print("측정: python poc.py run --data data/real")


if __name__ == "__main__":
    main()
