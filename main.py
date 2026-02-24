import datetime
import os
import re

import FinanceDataReader as fdr
import OpenDartReader
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def _is_openai_quota_error(err):
    msg = str(err).lower()
    code = str(getattr(err, "code", "")).lower()
    return (
        "insufficient_quota" in msg
        or "insufficient_quota" in code
        or "rate limit" in msg
        or getattr(err, "status_code", None) == 429
    )


def extract_refine_text(html_string):
    no_css = re.sub(r"<style.*?</style>", "", html_string, flags=re.DOTALL)
    no_inline_css = re.sub(r"\..*?{.*?}", "", no_css, flags=re.DOTALL)
    no_undesired = re.sub(r'\d{4}[A-Za-z0-9_]*" ADELETETABLE="N">', "", no_inline_css)
    no_tags = re.sub(r"<[^>]+>", " ", no_undesired)
    cleaned = re.sub(r"\s+", " ", no_tags).strip()
    no_square = re.sub("□", "", cleaned)
    return re.sub(r"\\'", "'", no_square)


def fetch_news_article_text(news_url):
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}
    res = requests.get(news_url, headers=headers, timeout=10)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    selectors = [
        "#news_read",
        "#content",
        "#dic_area",
        ".articleCont",
        "#articleBodyContents",
    ]
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            txt = node.get_text(" ", strip=True)
            if txt:
                return txt
    return ""


def summarize_major_disclosures(dart, major_df, stock_name, openai_api_key, top_n):
    if not openai_api_key:
        return None, "요약 생성을 위해 .env에 OPENAI_API_KEY를 설정하세요."
    if "rcept_no" not in major_df.columns or major_df.empty:
        return None, "요약할 주요 보고서가 없습니다."

    selected = major_df.head(top_n)
    docs = []
    total_chars = 0

    for _, row in selected.iterrows():
        rcp_no = str(row.get("rcept_no", "")).strip()
        if not rcp_no:
            continue
        try:
            raw_text = dart.document(rcp_no)
            clean_text = extract_refine_text(raw_text)
        except Exception:
            continue

        report_nm = str(row.get("report_nm", "보고서명 없음"))
        rcept_dt = row.get("rcept_dt")
        if hasattr(rcept_dt, "strftime"):
            rcept_dt = rcept_dt.strftime("%Y-%m-%d")

        snippet = f"[{rcept_dt}] {report_nm}\n{clean_text[:5000]}"
        docs.append(snippet)
        total_chars += len(snippet)
        if total_chars >= 45000:
            break

    if not docs:
        return None, "본문을 불러올 수 있는 주요 보고서가 없습니다."

    client = OpenAI(api_key=openai_api_key)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": "금융 공시 요약 전문가로서 문서 근거 중심의 한국어 요약만 작성하라.",
            },
            {
                "role": "user",
                "content": (
                    f"{stock_name} 최근 주요 보고서 요약을 작성해줘.\n"
                    "형식:\n"
                    "1) 핵심 요약(5줄 이내)\n"
                    "2) 긍정 포인트(최대 3개)\n"
                    "3) 리스크 포인트(최대 3개)\n"
                    "4) 체크할 후속 공시(최대 3개)\n\n"
                    + "\n\n".join(docs)
                ),
            },
        ],
    )
    return resp.choices[0].message.content, None


def summarize_latest_news(news_df, stock_name, openai_api_key, top_n):
    if not openai_api_key:
        return None, "뉴스 요약 생성을 위해 .env에 OPENAI_API_KEY를 설정하세요."
    if news_df.empty:
        return None, "요약할 뉴스가 없습니다."

    selected = news_df.head(top_n)
    docs = []
    total_chars = 0

    for _, row in selected.iterrows():
        title = str(row.get("제목", "제목 없음"))
        date_text = str(row.get("일시", ""))
        press = str(row.get("언론사", ""))
        link = str(row.get("기사링크", ""))

        body = ""
        if link:
            try:
                body = fetch_news_article_text(link)
            except Exception:
                body = ""

        snippet = f"[{date_text}] {title} ({press})\n{body[:2500]}"
        docs.append(snippet)
        total_chars += len(snippet)
        if total_chars >= 35000:
            break

    if not docs:
        return None, "요약 가능한 뉴스 본문이 없습니다."

    client = OpenAI(api_key=openai_api_key)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": "금융 뉴스 요약 전문가로서 기사 근거 중심의 한국어 요약만 작성하라.",
            },
            {
                "role": "user",
                "content": (
                    f"{stock_name} 최근 뉴스 요약을 작성해줘.\n"
                    "형식:\n"
                    "1) 핵심 요약(5줄 이내)\n"
                    "2) 시장 반응 포인트(최대 3개)\n"
                    "3) 리스크/유의 포인트(최대 3개)\n\n"
                    + "\n\n".join(docs)
                ),
            },
        ],
    )
    return resp.choices[0].message.content, None


STOCK_THEME_KEYWORDS = {
    "삼성전자": ["삼성", "반도체", "메모리", "dram", "d램", "nand", "hbm", "파운드리", "램"],
    "sk하이닉스": ["하이닉스", "반도체", "메모리", "dram", "d램", "nand", "hbm", "램"],
    "현대차": ["현대자동차", "자동차", "완성차", "전기차", "배터리", "자율주행"],
    "기아": ["자동차", "완성차", "전기차", "배터리", "자율주행"],
    "lg에너지솔루션": ["배터리", "2차전지", "전기차", "양극재", "음극재"],
    "삼성바이오로직스": ["바이오", "cdmo", "의약품", "제약", "바이오의약품"],
}


def _stock_related_keywords(stock_name):
    base = [stock_name.strip()]
    normalized = stock_name.strip().lower().replace(" ", "")
    for key, words in STOCK_THEME_KEYWORDS.items():
        key_norm = key.lower().replace(" ", "")
        if key_norm in normalized or normalized in key_norm:
            base.extend(words)

    seen = set()
    out = []
    for k in base:
        token = k.strip()
        if not token:
            continue
        low = token.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(token)
    return out


def _contains_keyword(text, keywords):
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords)


def collect_mainnews_by_keywords(keywords, max_pages=5, max_items=20):
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}
    rows = []
    seen_urls = set()

    for page in range(1, max_pages + 1):
        url = f"https://finance.naver.com/news/mainnews.naver?page={page}"
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        items = soup.select("ul.newsList li")
        if not items:
            continue

        for li in items:
            a = li.select_one("dd.articleSubject a")
            if not a:
                continue
            href = a.get("href", "")
            if not href:
                continue

            news_url = "https://finance.naver.com" + href
            if news_url in seen_urls:
                continue

            title = a.get_text(" ", strip=True)
            summary_node = li.select_one("dd.articleSummary")
            summary = summary_node.get_text(" ", strip=True) if summary_node else ""
            press_node = li.select_one("span.press")
            wdate_node = li.select_one("span.wdate")
            press_text = press_node.get_text(strip=True) if press_node else ""
            date_text = wdate_node.get_text(strip=True) if wdate_node else ""

            hit = _contains_keyword(title, keywords) or _contains_keyword(summary, keywords)
            if not hit:
                try:
                    body = fetch_news_article_text(news_url)
                    hit = _contains_keyword(body, keywords)
                except Exception:
                    hit = False

            if not hit:
                continue

            seen_urls.add(news_url)
            rows.append(
                {
                    "일시": date_text,
                    "언론사": press_text,
                    "제목": title,
                    "기사링크": news_url,
                }
            )
            if len(rows) >= max_items:
                return rows
    return rows


def collect_mainnews_latest(max_pages=3, max_items=20):
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}
    rows = []
    seen_urls = set()

    for page in range(1, max_pages + 1):
        url = f"https://finance.naver.com/news/mainnews.naver?page={page}"
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        items = soup.select("ul.newsList li")
        if not items:
            continue

        for li in items:
            a = li.select_one("dd.articleSubject a")
            if not a:
                continue
            href = a.get("href", "")
            if not href:
                continue
            news_url = "https://finance.naver.com" + href
            if news_url in seen_urls:
                continue

            title = a.get_text(" ", strip=True)
            press_node = li.select_one("span.press")
            wdate_node = li.select_one("span.wdate")
            press_text = press_node.get_text(strip=True) if press_node else ""
            date_text = wdate_node.get_text(strip=True) if wdate_node else ""

            seen_urls.add(news_url)
            rows.append(
                {
                    "일시": date_text,
                    "언론사": press_text,
                    "제목": title,
                    "기사링크": news_url,
                }
            )
            if len(rows) >= max_items:
                return rows
    return rows


@st.cache_data(ttl=3600)
def _fetch_stock_list_from_krx(market):
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "http://data.krx.co.kr/"}
    r = requests.post(
        "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
        headers=headers,
        data={"bld": "dbms/comm/finder/finder_stkisu"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("block1", [])
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df = df.rename(columns={"short_code": "Code", "codeName": "Name", "marketEngName": "Market"})
    df["Code"] = df["Code"].astype(str).str.zfill(6)
    if market != "KRX":
        df = df[df["Market"].astype(str).str.upper() == market]
    return df[["Code", "Name", "Market"]].reset_index(drop=True)


@st.cache_data(ttl=3600)
def get_stock_list(market):
    market = str(market).upper()
    for target in ([market] if market == "KRX" else [market, "KRX"]):
        try:
            df = fdr.StockListing(target)
            if df is not None and not df.empty:
                if target == "KRX" and market != "KRX" and "Market" in df.columns:
                    filtered = df[df["Market"].astype(str).str.upper() == market]
                    if not filtered.empty:
                        return filtered.reset_index(drop=True)
                    continue
                return df.reset_index(drop=True)
        except Exception:
            pass
    fallback = _fetch_stock_list_from_krx(market)
    if fallback is not None and not fallback.empty:
        return fallback
    return pd.DataFrame(columns=["Code", "Name"])


def plot_disclosures(stock_name, stock_code, opendart_api, openai_api_key):
    st.header(f"🗂️ {stock_name} 최근 전자공시")
    try:
        dart = OpenDartReader(opendart_api)
        start = (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
        disclosures = dart.list(stock_code, start=start, final=False)
        if disclosures is None or disclosures.empty:
            disclosures = dart.list(stock_name, start=start, final=False)
    except Exception as e:
        st.error(f"전자공시 목록 조회 중 오류가 발생했습니다: {type(e).__name__} - {e}")
        return

    if disclosures is None or disclosures.empty:
        st.info("최근 1년 내 공시가 없습니다.")
        return

    df = disclosures.copy()
    if "rcept_dt" in df.columns:
        df["rcept_dt"] = pd.to_datetime(df["rcept_dt"], format="%Y%m%d", errors="coerce")
        df = df.sort_values("rcept_dt", ascending=False)
    if "rcept_no" in df.columns:
        df["공시보기"] = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + df["rcept_no"].astype(str)

    major_keywords = ["사업보고서", "반기보고서", "분기보고서", "감사보고서"]
    if "report_nm" in df.columns:
        major_mask = df["report_nm"].astype(str).apply(lambda x: any(k in x for k in major_keywords))
    else:
        major_mask = pd.Series([True] * len(df), index=df.index)

    major_df = df[major_mask]
    other_df = df[~major_mask]

    display_cols = [c for c in ["rcept_dt", "report_nm", "flr_nm", "corp_name", "공시보기"] if c in df.columns]
    rename_map = {"rcept_dt": "접수일자", "report_nm": "보고서명", "flr_nm": "제출인", "corp_name": "회사명"}

    tab_major, tab_other = st.tabs(["주요 보고서", "기타 보고서"])

    with tab_major:
        major_view = major_df[display_cols].rename(columns=rename_map).head(30)
        if major_view.empty:
            st.info("주요 보고서가 없습니다.")
        else:
            st.caption(f"최신순 상위 {len(major_view)}건")
            st.dataframe(
                major_view,
                hide_index=True,
                use_container_width=True,
                column_config={"공시보기": st.column_config.LinkColumn("공시보기", display_text="바로가기")},
            )

            st.markdown("#### 주요 보고서 요약")
            summary_n = st.slider(
                "요약 대상 보고서 수",
                1,
                min(5, len(major_df)),
                min(3, len(major_df)),
                key=f"major_n_{stock_code}",
            )
            major_state_key = f"major_summary_text_{stock_code}"

            if st.button("주요 보고서 요약 생성", key=f"major_btn_{stock_code}"):
                with st.spinner("주요 보고서를 요약 중입니다..."):
                    try:
                        text, err = summarize_major_disclosures(dart, major_df, stock_name, openai_api_key, summary_n)
                        if err:
                            st.warning(err)
                        else:
                            st.success(f"최근 주요 보고서 {summary_n}건 요약 완료")
                            st.session_state[major_state_key] = text
                    except Exception as e:
                        if _is_openai_quota_error(e):
                            st.error("OpenAI API 사용 한도를 초과했습니다(429 insufficient_quota).")
                        else:
                            st.error(f"요약 중 오류가 발생했습니다: {type(e).__name__} - {e}")

            if major_state_key in st.session_state:
                st.markdown(st.session_state[major_state_key])

    with tab_other:
        other_view = other_df[display_cols].rename(columns=rename_map).head(30)
        if other_view.empty:
            st.info("기타 보고서가 없습니다.")
        else:
            st.caption(f"최신순 상위 {len(other_view)}건")
            st.dataframe(
                other_view,
                hide_index=True,
                use_container_width=True,
                column_config={"공시보기": st.column_config.LinkColumn("공시보기", display_text="바로가기")},
            )


def plot_latest_news(stock_name, stock_code, openai_api_key, max_items=20):
    st.header(f"📰 {stock_name} 최신 주요뉴스")
    try:
        rows = collect_mainnews_latest(max_pages=5, max_items=max_items)
    except Exception as e:
        st.error(f"뉴스 조회 중 오류가 발생했습니다: {type(e).__name__} - {e}")
        return

    if not rows:
        st.info("표시할 주요뉴스가 없습니다.")
        return

    news_df = pd.DataFrame(rows)
    st.caption(f"최신순 상위 {len(news_df)}건")
    st.dataframe(
        news_df,
        hide_index=True,
        use_container_width=True,
        column_config={"기사링크": st.column_config.LinkColumn("기사링크", display_text="바로가기")},
    )

    st.markdown("#### 최신 뉴스 요약")
    n = st.slider("요약 대상 뉴스 수", 1, min(10, len(news_df)), min(5, len(news_df)), key=f"news_n_{stock_code}")
    news_state_key = f"news_summary_text_{stock_code}"

    if st.button("최신 뉴스 요약 생성", key=f"news_btn_{stock_code}"):
        with st.spinner("최신 뉴스를 요약 중입니다..."):
            try:
                text, err = summarize_latest_news(news_df, stock_name, openai_api_key, n)
                if err:
                    st.warning(err)
                else:
                    st.success(f"최근 뉴스 {n}건 요약 완료")
                    st.session_state[news_state_key] = text
            except Exception as e:
                if _is_openai_quota_error(e):
                    st.error("OpenAI API 사용 한도를 초과했습니다(429 insufficient_quota).")
                else:
                    st.error(f"요약 중 오류가 발생했습니다: {type(e).__name__} - {e}")

    if news_state_key in st.session_state:
        st.markdown(st.session_state[news_state_key])


def app():
    st.set_page_config(page_title="SSAFY 금융 데이터 분석 GPT")
    st.title("SSAFY Project II")
    st.subheader(": 전자공시 / 뉴스 조회 + AI 요약")

    stock_name = ""
    stock_code = ""
    show_disclosure = False
    show_news = False
    with st.sidebar:
        st.header("사용자 설정")
        market = st.selectbox("📌 시장 선정", ("KRX 전체", "KOSPI 코스피", "KOSDAQ 코스닥", "KONEX 코넥스"))
        df_list = get_stock_list(market.split(" ")[0])

        stock = None
        if not df_list.empty:
            stock = st.selectbox("📌 종목 선정", (f"{nm}({cd})" for cd, nm in zip(list(df_list["Code"]), list(df_list["Name"]))))
        else:
            st.warning("선택 가능한 종목이 없습니다.")

        st.subheader("API Key (.env)")
        opendart_api = os.getenv("OPENDART_API_KEY", "").strip()
        openai_api = os.getenv("OPENAI_API_KEY", "").strip()

        if opendart_api:
            st.success("OpenDart API Key 로드 완료", icon="✅")
        else:
            st.warning("OPENDART_API_KEY가 .env에 없습니다.", icon="⚠️")

        if openai_api:
            st.success("OpenAI API Key 로드 완료", icon="✅")
        else:
            st.warning("OPENAI_API_KEY가 .env에 없습니다.", icon="⚠️")

        if stock:
            stock_name = stock.split("(")[0]
            stock_code = stock.split("(")[-1][:-1]
            st.subheader("옵션")
            show_disclosure = st.checkbox("🗂️ 전자공시", value=True)
            show_news = st.checkbox("📰 뉴스", value=True)

    if not stock_name:
        st.info("좌측에서 종목을 선택하세요.")
        return

    st.divider()
    st.title(f"📌 《{stock_name} ({stock_code})》")

    if show_disclosure:
        if not opendart_api:
            st.warning("전자공시 조회를 위해 .env에 OPENDART_API_KEY를 설정하세요.")
        else:
            plot_disclosures(stock_name, stock_code, opendart_api, openai_api)

    if show_news:
        plot_latest_news(stock_name, stock_code, openai_api)


app()
