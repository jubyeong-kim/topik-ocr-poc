"""
실제 손글씨 샘플 준비 스크립트 (Nexdata 공개 데모 이미지 002_demo.jpg 기준)

A4 손글씨 페이지를 행 단위로 잘라 TOPIK 51/52번 답안(한 문장)과 같은 조건으로 만들고,
사람이 눈으로 읽은 전사를 정답(ground truth)으로 저장한다.

  python prepare_real.py

주의: 원본 이미지는 상업 라이선스라 저장소에 커밋하지 않는다 (.gitignore 처리).
      실행하려면 아래 SOURCE_URL 에서 직접 내려받아야 한다.
"""

from pathlib import Path

from PIL import Image

SOURCE_URL = (
    "https://raw.githubusercontent.com/Nexdata-AI/"
    "5711-Images-Korean-Handwriting-OCR-data/main/002_demo.jpg"
)
REAL_DIR = Path(__file__).parent / "data" / "real"
SRC = REAL_DIR / "002_demo.jpg"

# (y 시작, y 끝, 사람이 읽은 전사) — 원본 3024x4032 좌표 기준
LINES = [
    (244, 362, "기출문제를 다 푼 뒤 선생님이 기말고사를 볼 때 볼펜으로 풀지 말고 연필로 연하게"),
    (400, 550, "풀어서 가져오라고 하셨다. 그 이유는 다름아닌 이번에 본 기말고사 문제지에 답을 푼 흔"),
    (590, 753, "적을 없애고 다음 2학년에게 나누어 주어 기출 문제를 풀어 볼 수 있게 하기 위해"),
    (793, 942, "서 이였다. 나는 학교 저작권 수업 시간 중에 들었던 말이 생각났다. 학원에서"),
    (982, 1102, "기출문제를 함부로 복제해서 쓰다가 저작권자에게 걸려서 신고를 받은 적이 있어"),
    (1156, 1305, "요. 즉 학원에서 기출문제를 복제하는 것도 저작권법 위반입니다. 라고 저작권"),
    (1344, 1493, "선생님이 알려 주셨는데 기출문제을 학원에 주면 안 되지않을까? 그래서"),
    (1547, 1696, "나는 학원 선생님께 안 가져오면 안돼요? 하고 물어봤다. 그러자 그럼"),
    (1729, 1849, "되겠니? 너도 작년 선배들이 가져온 문제 풀었잖아. 근데 너는 안가져"),
    (1903, 2045, "오겠다고? 그건 도둑놈 심보야. 하셨다. 나는 할 말을 잃었다. 다 맞는"),
    (2084, 2233, "말이여서 반박할 수 없었다. 나는 아무말 없이 친구들과 집으로 향했"),
]


def remove_annotation_boxes(im: Image.Image) -> Image.Image:
    """데모 이미지에 인쇄된 초록색 어노테이션 박스를 배경색으로 지운다.

    박스가 글자와 겹쳐 OCR을 방해하는 교란 변수이므로, '손글씨 난이도'만
    측정하려면 제거해야 한다. 초록 성분이 적/청보다 뚜렷이 큰 픽셀만 지운다.
    """
    im = im.convert("RGB")
    px = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if g > r + 40 and g > b + 40:
                px[x, y] = (205, 205, 205)  # 종이 배경색
    return im


def main():
    if not SRC.exists():
        raise SystemExit(
            f"{SRC} 가 없습니다.\n다음 명령으로 먼저 내려받으세요:\n"
            f'  curl -L -o "{SRC}" {SOURCE_URL}'
        )

    im = Image.open(SRC)
    width = im.size[0]
    labels = {}

    for i, (y0, y1, text) in enumerate(LINES, 1):
        fname = f"line{i:02d}.png"
        crop = remove_annotation_boxes(im.crop((60, y0, width - 60, y1)))
        crop.save(REAL_DIR / fname)
        labels[fname] = {"text": text, "font": "실제손글씨"}

    import json

    (REAL_DIR / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"실제 손글씨 행 이미지 {len(labels)}건 생성 → {REAL_DIR}")
    print("이제 `python poc.py run --data data/real` 로 측정하세요.")


if __name__ == "__main__":
    main()
