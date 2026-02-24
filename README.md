# SSAFY Project II - 금융 데이터 분석 + 시각화 + 공시/뉴스 요약

현재 저장소는 **Project II 단일 Streamlit 앱**으로 정리되어 있습니다.

## 실행 방법

```bash
# 1) 가상환경 생성
python -m venv vnev

# 2) 가상환경 활성화
venv\Scripts\activate

# 2) 패키지 설치
pip install -r requirements.txt

# 3) 앱 실행
streamlit run main.py
```

접속 주소: `http://localhost:8501`

## 주요 기능

- 종목/기간 기반 금융 데이터 조회 (FinanceDataReader)
- 퀀트 전략 시각화
  - 삼중창(EMA, MACD)
  - 골든/데드크로스(5/20, 20/60)
- 투자 성과 지표 계산
  - CAGR, VOL, MDD, Sharpe
- 최신 전자공시 조회 (OpenDart)
  - 주요 보고서 / 기타 보고서 분류
  - DART 원문 링크 제공
  - 주요 보고서 요약 (OpenAI API Key 사용)
- 최신 뉴스 조회
  - 종목 뉴스 링크 목록 제공
  - 뉴스 요약 (OpenAI API Key 사용)

## API 키 안내

- `OpenDart API Key`:
  - 전자공시 조회/요약 기능에 필요
- `OpenAI API Key`:
  - 주요 보고서 요약, 뉴스 요약 기능에 필요
  - 단순 조회(차트/공시/뉴스 목록)에는 필수 아님

## 현재 구조

- `main.py`: 앱 진입점 (Project II 코드)
- `DB/vector/Stock`: Project II에서 사용하는 벡터 DB
- `requirements.txt`: 실행 패키지 목록

## 참고

- OpenAI 관련 `429 insufficient_quota` 발생 시, API Billing/Usage 설정을 확인해야 합니다.
- 뉴스 목록은 웹 파싱 기반이라 원본 사이트 구조 변경 시 동작이 바뀔 수 있습니다.
