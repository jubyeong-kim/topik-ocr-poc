# TOPIK 손글씨 답안 OCR PoC

종이에 손으로 쓴 TOPIK 답안을 촬영해 텍스트로 바꾸는 실험. 재타이핑 구간을 OCR 로 대체하는 것이 목표.

- 레포: https://github.com/jubyeong-kim/topik-ocr-poc
- 문제 정의서: `PROBLEM.md` · 상용화 제안서: `NEXT_STEPS.md` · 결과: `README.md`

**이 PoC 의 산출물은 "동작하는 기능"이 아니라 재사용 가능한 평가 파이프라인이다.**
엔진을 갈아끼우고 같은 기준으로 재측정하는 것이 핵심 가치다.

## 실행

```bash
pip install -r requirements.txt
python poc.py gen        # 합성 샘플 12건 + 정답 라벨 생성 (시드 고정)
python poc.py run        # OCR → 정확도 리포트
python poc.py selfcheck  # 지표·후처리·파싱 로직 self-test
python poc.py run --engine surya   # 엔진 교체
```

실제 손글씨: `prepare_real.py` 가 원본을 행 단위로 자르고 정답 라벨을 만든다.
그 뒤 `python poc.py run --data data/real`.

## 구조

```
poc.py            전체 파이프라인 (gen / run / selfcheck)
  make_reader()     엔진명 → 이미지경로를 받아 텍스트를 돌려주는 함수
  postcorrect()     한국어 문법 규칙 기반 OCR 교정
  cer()             문자 오류율 (편집거리 ÷ 정답 길이)
prepare_real.py   실제 손글씨 사진 → 행 크롭 + 사람 전사 라벨
data/samples/     합성 샘플 (커밋됨)
data/real/        실제 육필 (커밋 금지)
results/          측정 리포트
```

## ⚠️ 지켜야 할 원칙

**1. 합성 샘플 결과를 성능 근거로 쓰지 않는다**
폰트 렌더링이라 실제 육필보다 훨씬 쉽다. 실측으로 확인된 격차:
같은 easyocr 로 합성 CER 0.029 / 실제 0.222. **판정은 반드시 `data/real` 로 한다.**

**2. `data/real` 의 이미지를 커밋하지 않는다**
출처(Nexdata 데모)가 상업 라이선스다. `.gitignore` 로 막혀 있으니 풀지 말 것.
재현은 `prepare_real.py` 가 안내하는 다운로드 명령으로 한다.

**3. 후처리 규칙은 관측된 오답을 보고 짜맞추지 않는다**
`postcorrect()` 의 근거는 "한국어에 `-ㅁ니다` 어미는 없다" 같은 **언어 규칙**이다.
테스트 결과에 맞춘 규칙은 과적합이다. 과교정 방지 테스트가 `selfcheck` 에 있으니 유지할 것.

**4. `selfcheck` 는 항상 통과 상태로 둔다**
커밋 전에 실행한다. 실제로 여기서 잘못된 가정을 잡아낸 적이 있다.

## 측정 결과 요약 (실제 육필 11건)

| 엔진 | CER | 일치율 |
|------|-----|--------|
| easyocr | 0.222~0.224 | 0/11 |
| **surya 0.17.1** | **0.092** | 1/11 |

CER 목표(0.15)는 surya 로 달성. 일치율 목표(80%)는 미달 → **완전 자동 채점은 아직 불가**,
초벌 전사 + 사용자 확인 전략은 유망.

지표 해석은 README 의 "지표 읽는 법" 참조. 요약하면 **CER 은 평균, 일치율은 전부 정답이어야 성립**이라
문장이 길수록 같은 CER 에서도 일치율은 급락한다.

## ⚠️ 환경 함정

**surya 버전**
- `0.20+` 는 모든 추론 백엔드가 외부 서버·컨테이너 기반(vLLM=Docker 등) → Colab·로컬에서 실행 불가
- `0.17.1` + `transformers 5.x` → `SuryaDecoderConfig has no attribute pad_token_id`

```bash
pip install "surya-ocr==0.17.1" "transformers<5"
```

**CPU/GPU 미세 차이**: 같은 코드·데이터라도 값이 조금 다르다(실제 CER 0.222 vs 0.224).
엔진을 비교할 때는 **같은 환경에서 잰 값끼리** 비교한다.

**Colab (GPU 필요 시)**: `google-colab-cli` 는 Windows 미지원(`termios`)이라 WSL 에서 사용한다.
`wsl -d Ubuntu -- bash -lc '$HOME/.local/bin/colab new -s <이름> --gpu T4'`

## 다음 우선순위

`NEXT_STEPS.md` 참조. 최우선은 **B 전략 사용성 검증** —
CER 0.092 초안을 고치는 것이 백지 타이핑보다 실제로 빠른지 측정하는 것. 제품 방향 자체의 판정이다.

## 관련 프로젝트

`../topiq-write` — OCR 결과를 최종적으로 연결할 채점 웹앱.
