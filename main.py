import datetime
import os
import re

import FinanceDataReader as fdr
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from tabs.disclosure import render_disclosure_tab
from tabs.flow import render_flow_tab
from tabs.news import render_news_tab
from tabs.price_chart import render_price_chart_tab
from tabs.report import render_report_tab

load_dotenv()

KIS_BASE_URLS = {
    "real": "https://openapi.koreainvestment.com:9443",
    "demo": "https://openapivts.koreainvestment.com:29443",
}


def _get_env_first(*keys):
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


def _normalize_kis_env(kis_env):
    env = str(kis_env or "real").strip().lower()
    alias = {"prod": "real", "vps": "demo", "paper": "demo"}
    return alias.get(env, env if env in KIS_BASE_URLS else "real")


def _to_dataframe(value):
    if isinstance(value, list):
        return pd.DataFrame(value)
    if isinstance(value, dict):
        return pd.DataFrame([value])
    return pd.DataFrame()


def _to_numeric_series(series):
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def _to_numeric_value(value):
    num = _to_numeric_series(pd.Series([value])).iloc[0]
    if pd.isna(num):
        return None
    return float(num)


def _find_first_matching_col(columns, patterns):
    lowered = {c: str(c).lower() for c in columns}
    for pattern in patterns:
        p = pattern.lower()
        for col, low in lowered.items():
            if p in low:
                return col
    return None


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


def _is_url_like_text(text):
    t = (text or "").strip().lower()
    return t.startswith(("http://", "https://", "www.")) or "finance.naver.com/news/" in t


def _extract_clean_summary_text(summary_node):
    if not summary_node:
        return ""
    node = BeautifulSoup(str(summary_node), "html.parser")
    for tag in node.select("span"):
        tag.decompose()
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _clean_news_title(raw_title, summary_node):
    title = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    if title and not _is_url_like_text(title):
        return title
    summary_text = _extract_clean_summary_text(summary_node)
    if summary_text and not _is_url_like_text(summary_text):
        return summary_text
    return "기사보기"


@st.cache_data(ttl=180)
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

            summary_node = li.select_one("dd.articleSummary")
            title = _clean_news_title(a.get_text(" ", strip=True), summary_node)
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


POSITIVE_NEWS_KEYWORDS = [
    "상승",
    "호재",
    "기대",
    "개선",
    "성장",
    "확대",
    "흑자",
    "강세",
    "회복",
    "상향",
    "급등",
    "수혜",
    "사상 최대",
    "돌파",
]

NEGATIVE_NEWS_KEYWORDS = [
    "하락",
    "악재",
    "우려",
    "불확실성",
    "둔화",
    "적자",
    "약세",
    "축소",
    "급락",
    "하향",
    "리스크",
    "부진",
    "부담",
    "경고",
    "충격",
]

STOCK_NAME_ALIASES = {
    "삼성전자": ["삼성전자", "삼성", "반도체"],
    "sk하이닉스": ["sk하이닉스", "하이닉스", "반도체"],
    "현대차": ["현대차", "현대자동차", "완성차", "자동차"],
    "기아": ["기아", "완성차", "자동차"],
    "lg에너지솔루션": ["lg에너지솔루션", "배터리", "2차전지"],
}


def _news_sentiment_score(text):
    t = str(text or "").lower()
    pos = sum(1 for w in POSITIVE_NEWS_KEYWORDS if w in t)
    neg = sum(1 for w in NEGATIVE_NEWS_KEYWORDS if w in t)
    raw = pos - neg
    if raw > 0:
        return min(raw, 3) / 3
    if raw < 0:
        return max(raw, -3) / 3
    return 0.0


def _stock_keywords(stock_name):
    norm = str(stock_name or "").strip()
    base = [norm, norm.replace(" ", "")]
    alias = STOCK_NAME_ALIASES.get(norm.lower(), [])
    for word in alias:
        base.append(str(word).strip())
    seen = set()
    out = []
    for token in base:
        low = token.lower()
        if token and low not in seen:
            seen.add(low)
            out.append(token)
    return out


def _flow_signal_text(flow_df):
    if flow_df.empty or {"외국인순매수", "기관순매수"}.issubset(flow_df.columns) is False:
        return None, 0.0

    latest = flow_df.dropna(subset=["외국인순매수", "기관순매수"]).tail(1)
    if latest.empty:
        return None, 0.0

    frgn = float(latest.iloc[0]["외국인순매수"])
    inst = float(latest.iloc[0]["기관순매수"])
    denom = abs(frgn) + abs(inst)
    flow_score = 0.0 if denom == 0 else max(-1.0, min(1.0, (frgn + inst) / denom))

    if frgn > 0 and inst > 0:
        return "외국인·기관 동반 순매수", flow_score
    if frgn > 0:
        return "외국인 순매수", flow_score
    if inst > 0:
        return "기관 순매수", flow_score
    if frgn < 0 and inst < 0:
        return "외국인·기관 동반 순매도", flow_score
    return "수급 혼조", flow_score


def build_investment_report(stock_name, news_rows, flow_df):
    news_df = pd.DataFrame(news_rows)
    if news_df.empty:
        return {
            "summary": "분석 가능한 뉴스가 부족합니다. 잠시 후 다시 시도해 주세요.",
            "sentiment_pct": 50,
            "sentiment_label": "Neutral",
            "opinion": "관망",
            "confidence": 50,
            "trend_df": pd.DataFrame(columns=["일자", "감성지수"]),
            "positive_points": [],
            "negative_points": [],
            "news_count": 0,
        }

    news_df["제목"] = news_df["제목"].astype(str).str.strip()
    news_df["일시"] = pd.to_datetime(news_df["일시"], errors="coerce")
    keywords = [k.lower() for k in _stock_keywords(stock_name)]

    related_mask = news_df["제목"].str.lower().apply(lambda t: any(k in t for k in keywords))
    related_df = news_df[related_mask].copy()
    if related_df.empty:
        related_df = news_df.copy()

    related_df["sent_score"] = related_df["제목"].apply(_news_sentiment_score)
    news_score = float(related_df["sent_score"].mean()) if not related_df.empty else 0.0

    flow_text, flow_score = _flow_signal_text(flow_df)
    combined_score = (news_score * 0.75) + (flow_score * 0.25)
    sentiment_pct = int(round(max(0.0, min(100.0, 50 + combined_score * 50))))

    if sentiment_pct >= 63:
        sentiment_label = "Bullish"
        opinion = "매수 우위"
        signal_text = "긍정적인 신호"
    elif sentiment_pct <= 37:
        sentiment_label = "Bearish"
        opinion = "매도 우위"
        signal_text = "부정적인 신호"
    else:
        sentiment_label = "Neutral"
        opinion = "관망"
        signal_text = "중립 신호"

    data_bonus = min(10, int(len(related_df) * 1.5))
    confidence = int(max(45, min(95, 55 + abs(sentiment_pct - 50) * 0.6 + data_bonus + (5 if flow_text else 0))))

    if flow_text:
        first_sentence = f"최근 {flow_text}와 뉴스 흐름에서 {signal_text}가 포착되었습니다."
    else:
        first_sentence = f"최근 뉴스 흐름에서 {signal_text}가 포착되었습니다."

    risk_words = ["금리", "환율", "인플레이션", "침체", "불확실성", "리스크"]
    risk_exists = related_df["제목"].str.contains("|".join(risk_words), case=False, regex=True).any()
    if risk_exists:
        second_sentence = "다만 거시 환경 리스크와 단기 변동성에는 유의가 필요합니다."
    else:
        second_sentence = "다만 단기 변동성 확대 가능성은 함께 점검해야 합니다."

    trend_df = related_df.dropna(subset=["일시"]).copy()
    if not trend_df.empty:
        trend_df["일자"] = trend_df["일시"].dt.normalize()
        trend_df = trend_df.groupby("일자", as_index=False)["sent_score"].mean()
        trend_df["감성지수"] = (50 + trend_df["sent_score"] * 50).clip(0, 100)
        trend_df = trend_df[["일자", "감성지수"]].sort_values("일자")
        if len(trend_df) > 7:
            trend_df = trend_df.tail(7)
        elif len(trend_df) < 7 and not trend_df.empty:
            end_day = trend_df["일자"].max()
            full_days = pd.date_range(end=end_day, periods=7, freq="D")
            trend_df = (
                pd.DataFrame({"일자": full_days})
                .merge(trend_df, on="일자", how="left")
                .fillna({"감성지수": 50})
            )

    def _to_point_records(df_slice):
        records = []
        for _, row in df_slice.iterrows():
            title = str(row.get("제목", "")).strip()
            if not title:
                continue
            link = str(row.get("기사링크", "")).strip()
            item = {"text": title}
            if link.startswith("http://") or link.startswith("https://"):
                item["link"] = link
            records.append(item)
        return records

    pos_points = _to_point_records(
        related_df[related_df["sent_score"] > 0]
        .sort_values(["sent_score", "일시"], ascending=[False, False])
        .head(2)
    )
    neg_points = _to_point_records(
        related_df[related_df["sent_score"] < 0]
        .sort_values(["sent_score", "일시"], ascending=[True, False])
        .head(2)
    )

    if not pos_points and not related_df.empty:
        pos_points = _to_point_records(related_df.sort_values("일시", ascending=False).head(1))

    return {
        "summary": f"{first_sentence} {second_sentence}",
        "sentiment_pct": sentiment_pct,
        "sentiment_label": sentiment_label,
        "opinion": opinion,
        "confidence": confidence,
        "trend_df": trend_df if isinstance(trend_df, pd.DataFrame) else pd.DataFrame(columns=["일자", "감성지수"]),
        "positive_points": pos_points,
        "negative_points": neg_points,
        "news_count": int(len(related_df)),
    }


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


@st.cache_data(ttl=3300, show_spinner=False)
def _get_kis_access_token(app_key, app_secret, kis_env):
    env = _normalize_kis_env(kis_env)
    base_url = KIS_BASE_URLS.get(env, KIS_BASE_URLS["real"])
    url = f"{base_url}/oauth2/tokenP"
    payload = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "charset": "UTF-8",
    }
    res = requests.post(url, json=payload, headers=headers, timeout=15)
    res.raise_for_status()
    body = res.json()
    token = str(body.get("access_token", "")).strip()
    if not token:
        msg = str(body.get("msg1", "KIS access token 발급 실패")).strip()
        raise RuntimeError(msg)
    return token


def _kis_get(app_key, app_secret, kis_env, api_url, tr_id, params):
    env = _normalize_kis_env(kis_env)
    base_url = KIS_BASE_URLS.get(env, KIS_BASE_URLS["real"])
    access_token = _get_kis_access_token(app_key, app_secret, env)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "charset": "UTF-8",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
    }
    res = requests.get(f"{base_url}{api_url}", headers=headers, params=params, timeout=15)
    res.raise_for_status()
    body = res.json()
    rt_cd = str(body.get("rt_cd", "0")).strip()
    if rt_cd and rt_cd != "0":
        msg_cd = str(body.get("msg_cd", "")).strip()
        msg = str(body.get("msg1", "KIS API 호출 실패")).strip()
        raise RuntimeError(f"{msg_cd} {msg}".strip())
    return body


@st.cache_data(ttl=3600, show_spinner=False)
def get_kis_stock_industry(stock_code, app_key, app_secret, kis_env):
    body = _kis_get(
        app_key,
        app_secret,
        kis_env,
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
        },
    )
    output = body.get("output")
    if not isinstance(output, dict):
        return ""

    for key in ("bstp_kor_isnm", "idst_nm", "sector_name", "industry_name"):
        value = str(output.get(key, "")).strip()
        if value and value.lower() != "nan":
            return value

    industry_col = _find_first_matching_col(output.keys(), ["industry", "sector", "업종"])
    if not industry_col:
        return ""
    value = str(output.get(industry_col, "")).strip()
    if value and value.lower() != "nan":
        return value
    return ""


@st.cache_data(ttl=2, show_spinner=False)
def get_kis_realtime_price(stock_code, app_key, app_secret, kis_env):
    body = _kis_get(
        app_key,
        app_secret,
        kis_env,
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
        },
    )
    output = body.get("output")
    if not isinstance(output, dict):
        return {}

    price = _to_numeric_value(output.get("stck_prpr"))
    change = _to_numeric_value(output.get("prdy_vrss"))
    change_rate = _to_numeric_value(output.get("prdy_ctrt"))

    date_raw = str(output.get("stck_bsop_date", "")).strip()
    time_raw = str(output.get("stck_cntg_hour", "")).strip()
    date_fmt = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}" if len(date_raw) == 8 and date_raw.isdigit() else ""
    time_fmt = f"{time_raw[:2]}:{time_raw[2:4]}:{time_raw[4:]}" if len(time_raw) == 6 and time_raw.isdigit() else ""
    asof = " ".join(x for x in [date_fmt, time_fmt] if x)

    return {
        "price": price,
        "change": change,
        "change_rate": change_rate,
        "asof": asof,
    }


@st.cache_data(ttl=300, show_spinner=False)
def get_kis_daily_ohlcv(stock_code, app_key, app_secret, kis_env, days=90):
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=max(days * 3, 180))
    body = _kis_get(
        app_key,
        app_secret,
        kis_env,
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        "FHKST03010100",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "1",
        },
    )

    df = _to_dataframe(body.get("output2"))
    if df.empty:
        return df

    rename_map = {
        "stck_bsop_date": "일자",
        "stck_oprc": "시가",
        "stck_hgpr": "고가",
        "stck_lwpr": "저가",
        "stck_clpr": "종가",
        "acml_vol": "거래량",
        "prdy_vrss": "전일대비",
    }
    df = df.rename(columns=rename_map)

    if "일자" in df.columns:
        df["일자"] = pd.to_datetime(df["일자"], format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=["일자"]).sort_values("일자")

    for col in ["시가", "고가", "저가", "종가", "거래량", "전일대비"]:
        if col in df.columns:
            df[col] = _to_numeric_series(df[col])

    return df.reset_index(drop=True)


@st.cache_data(ttl=300, show_spinner=False)
def get_kis_investor_flow(stock_code, app_key, app_secret, kis_env):
    body = _kis_get(
        app_key,
        app_secret,
        kis_env,
        "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily",
        "FHPTJ04160001",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": datetime.date.today().strftime("%Y%m%d"),
            "FID_ORG_ADJ_PRC": "",
            "FID_ETC_CLS_CODE": "",
        },
    )

    df1 = _to_dataframe(body.get("output1"))
    df2 = _to_dataframe(body.get("output2"))
    df = df2 if len(df2) >= len(df1) else df1
    if df.empty:
        return df

    date_col = _find_first_matching_col(
        df.columns,
        ["stck_bsop_date", "bsop_date", "trd_dd", "date", "dt"],
    )
    if date_col:
        df = df.rename(columns={date_col: "일자"})
        df["일자"] = pd.to_datetime(df["일자"], format="%Y%m%d", errors="coerce")

    frgn_col = _find_first_matching_col(
        df.columns,
        ["frgn_ntby_qty", "frgn_ntby_tr_pbmn", "frgn_ntby_amt", "frgn_ntby", "frgn_net"],
    )
    prsn_col = _find_first_matching_col(
        df.columns,
        [
            "prsn_ntby_qty",
            "prsn_ntby_tr_pbmn",
            "prsn_ntby_amt",
            "prsn_ntby",
            "prsn_net",
            "indv_ntby_qty",
            "indv_ntby_amt",
            "indv_net",
        ],
    )
    orgn_col = _find_first_matching_col(
        df.columns,
        ["orgn_ntby_qty", "orgn_ntby_tr_pbmn", "orgn_ntby_amt", "orgn_ntby", "orgn_net"],
    )

    rename_map = {}
    if frgn_col:
        rename_map[frgn_col] = "외국인순매수"
    if prsn_col:
        rename_map[prsn_col] = "개인순매수"
    if orgn_col:
        rename_map[orgn_col] = "기관순매수"
    if rename_map:
        df = df.rename(columns=rename_map)

    for col in ["외국인순매수", "개인순매수", "기관순매수"]:
        if col in df.columns:
            df[col] = _to_numeric_series(df[col])

    if "일자" in df.columns:
        df = df.dropna(subset=["일자"]).sort_values("일자")

    return df.reset_index(drop=True)


def app():
    st.set_page_config(page_title="SSAFY 금융 데이터 분석 GPT")
    st.title("SSAFY Project II")

    stock_name = ""
    stock_code = ""
    stock_industry = ""
    with st.sidebar:
        st.header("사용자 설정")
        df_list = get_stock_list("KOSPI")

        stock = None
        if not df_list.empty:
            stock = st.selectbox("📌 종목 선정", (f"{nm}({cd})" for cd, nm in zip(list(df_list["Code"]), list(df_list["Name"]))))
        else:
            st.warning("선택 가능한 종목이 없습니다.")

        st.subheader("API Key (.env)")
        opendart_api = os.getenv("OPENDART_API_KEY", "").strip()
        openai_api = os.getenv("OPENAI_API_KEY", "").strip()
        kis_app_key = _get_env_first("KIS_APP_KEY", "KIS_APPKEY")
        kis_app_secret = _get_env_first("KIS_APP_SECRET", "KIS_APPSECRET")
        kis_env = _normalize_kis_env(_get_env_first("KIS_ENV"))

        if opendart_api:
            st.success("OpenDart API Key 로드 완료", icon="✅")
        else:
            st.warning("OPENDART_API_KEY가 .env에 없습니다.", icon="⚠️")

        if openai_api:
            st.success("OpenAI API Key 로드 완료", icon="✅")
        else:
            st.warning("OPENAI_API_KEY가 .env에 없습니다.", icon="⚠️")

        if kis_app_key and kis_app_secret:
            st.success(f"KIS API Key 로드 완료 ({kis_env})", icon="✅")
        else:
            st.warning("KIS_APP_KEY / KIS_APP_SECRET가 .env에 없습니다.", icon="⚠️")

        if stock:
            stock_name = stock.split("(")[0]
            stock_code = stock.split("(")[-1][:-1]
            if kis_app_key and kis_app_secret:
                try:
                    stock_industry = get_kis_stock_industry(stock_code, kis_app_key, kis_app_secret, kis_env)
                except Exception:
                    stock_industry = ""

    if not stock_name:
        st.info("좌측에서 종목을 선택하세요.")
        return

    st.divider()
    st.title(f"📌 {stock_name} ({stock_code})")
    st.caption(f" {stock_industry or '정보 없음'}")

    tab_price_chart, tab_flow, tab_report, tab_disclosure, tab_news = st.tabs(
        ["📈 주가 차트", "💹 외국인/기관 수급", "🧾 투자 분석 리포트", "🗂️ 전자공시", "📰 뉴스"]
    )

    with tab_price_chart:
        if not kis_app_key or not kis_app_secret:
            st.warning("KIS 주가 조회를 위해 .env에 KIS_APP_KEY, KIS_APP_SECRET를 설정하세요.")
        else:
            render_price_chart_tab(
                stock_name=stock_name,
                stock_code=stock_code,
                stock_industry=stock_industry,
                kis_app_key=kis_app_key,
                kis_app_secret=kis_app_secret,
                kis_env=kis_env,
                get_kis_daily_ohlcv=get_kis_daily_ohlcv,
                get_kis_realtime_price=get_kis_realtime_price,
            )

    with tab_flow:
        if not kis_app_key or not kis_app_secret:
            st.warning("외국인/기관 수급 조회를 위해 .env에 KIS_APP_KEY, KIS_APP_SECRET를 설정하세요.")
        else:
            render_flow_tab(
                stock_name=stock_name,
                stock_code=stock_code,
                kis_app_key=kis_app_key,
                kis_app_secret=kis_app_secret,
                kis_env=kis_env,
                get_kis_investor_flow=get_kis_investor_flow,
            )

    with tab_report:
        render_report_tab(
            stock_name=stock_name,
            stock_code=stock_code,
            kis_app_key=kis_app_key,
            kis_app_secret=kis_app_secret,
            kis_env=kis_env,
            collect_mainnews_latest=collect_mainnews_latest,
            get_kis_investor_flow=get_kis_investor_flow,
            build_investment_report=build_investment_report,
        )

    with tab_disclosure:
        if not opendart_api:
            st.warning("전자공시 조회를 위해 .env에 OPENDART_API_KEY를 설정하세요.")
        else:
            render_disclosure_tab(
                stock_name=stock_name,
                stock_code=stock_code,
                opendart_api=opendart_api,
                openai_api_key=openai_api,
                summarize_major_disclosures=summarize_major_disclosures,
                is_openai_quota_error=_is_openai_quota_error,
            )

    with tab_news:
        render_news_tab(
            stock_name=stock_name,
            stock_code=stock_code,
            openai_api_key=openai_api,
            collect_mainnews_latest=collect_mainnews_latest,
            summarize_latest_news=summarize_latest_news,
            is_openai_quota_error=_is_openai_quota_error,
        )


app()
