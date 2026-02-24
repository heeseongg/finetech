# 금융 공시/뉴스 분석 Streamlit 앱

단일 Streamlit 앱에서 종목별 `주가`, `수급`, `전자공시`, `뉴스`, `투자 분석 리포트`를 확인하는 프로젝트입니다.

## 핵심 기능

### 1) 주가 차트 탭
- KIS API 일봉 데이터 조회
- 주가 시계열 차트 표시
- 최근 일봉 표 표시
- 표 컬럼: `일자`, `종가`, `등락폭`, `전일대비`, `거래량`
- 상승/하락 값 색상 표시(상승 빨강, 하락 파랑)

### 2) 외국인/기관 수급 탭
- KIS API 수급 데이터 조회
- 외국인/개인/기관 순매수 추이 차트(가능한 컬럼 자동 표시)
- 최근 수급 표 표시
- 양수/음수 색상 표시(양수 빨강, 음수 파랑)

### 3) 투자 분석 리포트 탭
- 뉴스 + 수급 데이터를 기반으로 요약 리포트 생성
- `시장 심리(%)`, `AI 의견`, `신뢰도` 표시
- 최근 7일 감성 지수 추이(막대 차트)
- 핵심 뉴스 요약 포인트(긍정/부정) 표시

참고:
- 이 탭의 시장 심리/의견은 OpenAI가 아니라 내부 규칙 기반 계산입니다.

### 4) 전자공시 탭
- OpenDART 최근 공시 조회(최근 1년)
- 주요 보고서/기타 보고서 분리
- 보고서명 클릭 시 DART 원문 이동
- OpenAI API 키가 있으면 주요 보고서 요약 생성 가능

### 5) 뉴스 탭
- 네이버 금융 주요뉴스 수집
- 뉴스 제목 클릭 시 기사 링크 이동
- 제목이 비정상(링크 문자열)인 경우 `기사보기`로 대체
- OpenAI API 키가 있으면 최신 뉴스 요약 생성 가능

## 데이터 소스

- 종목 리스트: `FinanceDataReader`
- 주가/수급: `한국투자증권(KIS) Open API`
- 공시: `OpenDART`
- 뉴스: `네이버 금융 주요뉴스 페이지 파싱`

## 실행 방법

### 1) 가상환경 생성
```bash
python -m venv venv
```

### 2) 가상환경 활성화
- Windows PowerShell
```powershell
.\venv\Scripts\Activate.ps1
```
- Git Bash / bash
```bash
source venv/Scripts/activate
```

### 3) 패키지 설치
```bash
pip install -r requirements.txt
```

### 4) `.env` 작성
프로젝트 루트에 `.env` 파일을 만들고 아래 값을 입력합니다.

```env
OPENDART_API_KEY=your_opendart_key
OPENAI_API_KEY=your_openai_key
KIS_APP_KEY=your_kis_app_key
KIS_APP_SECRET=your_kis_app_secret
KIS_ENV=real
```

### 5) 앱 실행
```bash
streamlit run main.py
```

접속: `http://localhost:8501`

## 환경변수 설명

- `OPENDART_API_KEY`: 전자공시 조회에 필요
- `OPENAI_API_KEY`: 공시/뉴스 요약 생성에 필요(조회만 할 때는 선택)
- `KIS_APP_KEY`, `KIS_APP_SECRET`: 주가/수급 조회에 필요
- `KIS_ENV`: `real` 또는 `demo` (미입력 시 기본 `real`)

참고:
- 코드에서 `KIS_APPKEY`, `KIS_APPSECRET` 이름도 함께 지원합니다.

## 프로젝트 구조

- `main.py`: 앱 진입점, API 호출/데이터 가공 로직
- `tabs/price_chart.py`: 주가 탭 UI
- `tabs/flow.py`: 수급 탭 UI
- `tabs/report.py`: 투자 분석 리포트 탭 UI
- `tabs/disclosure.py`: 전자공시 탭 UI
- `tabs/news.py`: 뉴스 탭 UI
- `requirements.txt`: 의존성 목록

## 주의사항

- OpenAI 요약 호출 시 `429 insufficient_quota`가 발생하면 API 사용량/결제 상태를 확인해야 합니다.
- 뉴스 수집은 웹 페이지 구조 변경 시 동작이 달라질 수 있습니다.
