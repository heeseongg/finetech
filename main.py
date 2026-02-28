import datetime
import json
import os
import re
import time

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from tabs.disclosure import render_disclosure_tab
from tabs.etf_components import render_etf_components_tab
from tabs.flow import render_flow_tab
from tabs.news import render_news_tab
from tabs.price_chart import render_price_chart_tab
from tabs.report import render_report_tab
from tabs.valuation import render_valuation_tab

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
    num = pd.to_numeric(str(value).replace(",", ""), errors="coerce")
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


def summarize_major_disclosures(dart, disclosure_df, stock_name, openai_api_key):
    if not openai_api_key:
        return None, None, "요약 생성을 위해 .env에 OPENAI_API_KEY를 설정하세요."
    if "rcept_no" not in disclosure_df.columns or disclosure_df.empty:
        return None, None, "요약할 공시가 없습니다."

    docs = []
    total_chars = 0

    for _, row in disclosure_df.iterrows():
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
        reason = re.sub(r"\s+", " ", str(row.get("_summary_bucket", "")).strip())
        title_line = f"[{rcept_dt}] {report_nm}"
        if reason:
            title_line += f" ({reason})"

        snippet = f"{title_line}\n{clean_text[:5000]}"
        docs.append(snippet)
        total_chars += len(snippet)
        if total_chars >= 45000:
            break

    if not docs:
        return None, None, "본문을 불러올 수 있는 공시가 없습니다."

    prompt = (
        f"{stock_name} 전자공시 요약을 작성해줘.\n"
        "요약 대상은 최신 정기보고서 1건 + 최근 주요사항/정정공시 + 최신 감사/검토 보고서로 구성되어 있다.\n"
        "출력 규칙:\n"
        "1) 반드시 JSON 객체만 출력한다.\n"
        "2) summary 섹션의 큰 숫자는 억/조 단위로 간결하게 표기한다.\n"
        "3) metrics의 숫자/비율/금액은 문서 원문 값 그대로 쓴다. 없으면 '-'를 넣는다.\n"
        "4) 섹션은 summary(핵심 요약, 긍정 포인트, 리스크 포인트)와 metrics 배열을 포함한다.\n"
        "5) metrics 각 항목은 '항목', '값', '기준기간', '전기/전년 대비', '출처 공시' 키를 가진다.\n"
        "6) 공시 근거가 약하면 추정하지 말고 '-'를 넣는다.\n"
        "JSON 스키마 예시:\n"
        "{\n"
        '  "summary": {\n'
        '    "핵심 요약": ["..."],\n'
        '    "긍정 포인트": ["..."],\n'
        '    "리스크 포인트": ["..."]\n'
        "  },\n"
        '  "metrics": [\n'
        '    {"항목":"...", "값":"...", "기준기간":"...", "전기/전년 대비":"...", "출처 공시":"..."}\n'
        "  ]\n"
        "}\n\n"
        "아래는 공시 원문 발췌:\n"
        + "\n\n".join(docs)
    )

    client = OpenAI(api_key=openai_api_key)
    messages = [
        {
            "role": "system",
            "content": "금융 공시 요약 전문가로서 문서 근거 중심의 한국어 요약만 작성하라.",
        },
        {"role": "user", "content": prompt},
    ]

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=messages,
        )
    except Exception:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            messages=messages,
        )

    content = (resp.choices[0].message.content or "").strip()
    if not content:
        return None, None, "요약 결과를 생성하지 못했습니다."

    parsed = {}
    try:
        parsed = json.loads(content)
    except Exception:
        parsed = {}

    if not isinstance(parsed, dict):
        parsed = {}

    summary_obj = parsed.get("summary", {}) if isinstance(parsed.get("summary"), dict) else {}

    def _format_korean_compact_number(number):
        abs_number = abs(number)
        sign = "-" if number < 0 else ""

        def _fmt(value):
            if value >= 100:
                return f"{value:,.0f}"
            return f"{value:,.1f}".rstrip("0").rstrip(".")

        if abs_number >= 1_000_000_000_000:
            return f"{sign}{_fmt(abs_number / 1_000_000_000_000)}조"
        if abs_number >= 100_000_000:
            return f"{sign}{_fmt(abs_number / 100_000_000)}억"
        return f"{number:,}"

    def _compact_large_numbers_in_text(text):
        pattern = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d{9,})(\s*)(원|주|건)?")

        def _replace(match):
            raw_number = match.group(1)
            spacing = match.group(2) or ""
            unit = match.group(3) or ""
            numeric = int(raw_number.replace(",", ""))
            if numeric < 100_000_000:
                return match.group(0)
            compact = _format_korean_compact_number(numeric)
            return f"{compact}{spacing}{unit}"

        return pattern.sub(_replace, str(text or ""))

    def _to_clean_lines(value, fallback):
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = []

        lines = []
        for item in raw_items:
            line = re.sub(r"\s+", " ", str(item or "")).strip()
            if line:
                lines.append(_compact_large_numbers_in_text(line))
        if not lines and fallback:
            lines = [fallback]
        return lines

    core_lines = _to_clean_lines(summary_obj.get("핵심 요약"), "핵심 요약을 생성하지 못했습니다.")
    positive_lines = _to_clean_lines(summary_obj.get("긍정 포인트"), "문서 근거 기반 긍정 포인트가 부족합니다.")
    risk_lines = _to_clean_lines(summary_obj.get("리스크 포인트"), "문서 근거 기반 리스크 포인트가 부족합니다.")

    summary_text = (
        "#### 핵심 요약\n"
        + "\n".join(f"- {line}" for line in core_lines)
        + "\n\n#### 긍정 포인트\n"
        + "\n".join(f"- {line}" for line in positive_lines)
        + "\n\n#### 리스크 포인트\n"
        + "\n".join(f"- {line}" for line in risk_lines)
    )

    metrics_raw = parsed.get("metrics", [])
    if isinstance(metrics_raw, dict):
        metrics_raw = [metrics_raw]
    if not isinstance(metrics_raw, list):
        metrics_raw = []

    metric_keys = {
        "항목": ["항목", "item", "name"],
        "값": ["값", "value"],
        "기준기간": ["기준기간", "기간", "period"],
        "전기/전년 대비": ["전기/전년 대비", "전기대비", "전년대비", "change"],
        "출처 공시": ["출처 공시", "출처", "source", "보고서"],
    }

    metrics_rows = []
    for row in metrics_raw[:15]:
        if not isinstance(row, dict):
            continue

        normalized = {}
        for target_key, aliases in metric_keys.items():
            value = ""
            for alias in aliases:
                if alias in row:
                    value = row.get(alias)
                    break
            value = re.sub(r"\s+", " ", str(value or "")).strip()
            normalized[target_key] = value if value else "-"

        if normalized["항목"] == "-" and normalized["값"] == "-":
            continue
        metrics_rows.append(normalized)

    if not parsed:
        summary_text = content
        metrics_rows = []

    return summary_text, metrics_rows, None


NEWS_IMPORTANCE_WEIGHTS = {
    "실적": 10,
    "영업이익": 9,
    "매출": 8,
    "순이익": 8,
    "가이던스": 9,
    "수주": 8,
    "계약": 6,
    "배당": 6,
    "자사주": 6,
    "소각": 6,
    "합병": 10,
    "인수": 10,
    "m&a": 10,
    "유상증자": 12,
    "전환사채": 12,
    "신주인수권부사채": 12,
    "cb": 10,
    "bw": 10,
    "감사의견": 11,
    "소송": 10,
    "제재": 10,
    "영업정지": 10,
    "적자": 8,
    "하향": 7,
    "정정": 5,
}

NEWS_CATEGORY_KEYWORDS = {
    "리스크": [
        "유상증자",
        "전환사채",
        "신주인수권부사채",
        "cb",
        "bw",
        "소송",
        "제재",
        "영업정지",
        "감사의견",
        "적자",
        "하향",
        "부진",
        "정정",
    ],
    "실적/펀더멘털": [
        "실적",
        "매출",
        "영업이익",
        "순이익",
        "가이던스",
        "수주",
        "계약",
        "배당",
        "자사주",
        "소각",
    ],
}


def _parse_news_datetime(value):
    text = str(value or "").strip()
    if not text:
        return pd.NaT
    normalized = re.sub(r"\s+", " ", text.replace(".", "-").replace("/", "-")).strip()
    parsed = pd.to_datetime(normalized, errors="coerce")
    if not pd.isna(parsed):
        return parsed

    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) >= 8:
        y, m, d = digits[:4], digits[4:6], digits[6:8]
        hh = digits[8:10] if len(digits) >= 10 else "00"
        mm = digits[10:12] if len(digits) >= 12 else "00"
        parsed = pd.to_datetime(f"{y}-{m}-{d} {hh}:{mm}", errors="coerce")
        if not pd.isna(parsed):
            return parsed
    return pd.NaT


def _news_importance_score(title):
    text = str(title or "")
    lowered = text.lower()
    score = 0.0
    for keyword, weight in NEWS_IMPORTANCE_WEIGHTS.items():
        if keyword.lower() in lowered:
            score += float(weight)
    return score


def _classify_news_category(title):
    lowered = str(title or "").lower()
    for category, keywords in NEWS_CATEGORY_KEYWORDS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            return category
    return "일반/시장"


def _news_topic_key(title):
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", "", str(title or "").lower())
    return normalized[:30]


def select_news_for_summary(news_df, top_n):
    if news_df.empty:
        return news_df.copy()
    if "제목" not in news_df.columns:
        return news_df.head(top_n).copy()

    df = news_df.copy()
    if "기사링크" in df.columns:
        df["기사링크"] = df["기사링크"].fillna("").astype(str).str.strip()
        has_link = df["기사링크"].ne("")
        df_with_link = df[has_link].drop_duplicates(subset=["기사링크"], keep="first")
        df_no_link = df[~has_link].drop_duplicates(subset=["제목"], keep="first")
        df = pd.concat([df_with_link, df_no_link], axis=0)
    else:
        df = df.drop_duplicates(subset=["제목"], keep="first")

    if "일시" in df.columns:
        df["_parsed_dt"] = df["일시"].apply(_parse_news_datetime)
        df = df.sort_values("_parsed_dt", ascending=False, na_position="last")
    else:
        df["_parsed_dt"] = pd.NaT

    pool_size = min(len(df), max(top_n * 8, 40))
    candidates = df.head(pool_size).copy()
    candidates["_importance"] = candidates["제목"].apply(_news_importance_score)
    candidates["_category"] = candidates["제목"].apply(_classify_news_category)
    candidates["_topic_key"] = candidates["제목"].apply(_news_topic_key)

    latest_ts = candidates["_parsed_dt"].dropna().max()
    if pd.isna(latest_ts):
        recency = pd.Series([1.0 - (idx / max(1, len(candidates))) for idx in range(len(candidates))], index=candidates.index)
        candidates["_recency"] = recency.clip(lower=0.0)
    else:
        age_hours = ((latest_ts - candidates["_parsed_dt"]).dt.total_seconds() / 3600).clip(lower=0)
        recency = (1.0 - (age_hours / (24 * 7))).clip(lower=0.0, upper=1.0)
        candidates["_recency"] = recency.fillna(0.2)

    max_importance = float(candidates["_importance"].max() or 0.0)
    if max_importance > 0:
        candidates["_importance_norm"] = candidates["_importance"] / max_importance
    else:
        candidates["_importance_norm"] = 0.0

    candidates["_score"] = 0.6 * candidates["_importance_norm"] + 0.4 * candidates["_recency"]

    ordered = candidates.sort_values(
        by=["_score", "_importance", "_parsed_dt"],
        ascending=[False, False, False],
        na_position="last",
    )

    required_categories = []
    if top_n >= 3:
        required_categories = ["리스크", "실적/펀더멘털", "일반/시장"]
    elif top_n == 2:
        required_categories = ["리스크", "실적/펀더멘털"]

    selected_indices = []
    used_topics = set()

    for category in required_categories:
        for idx, row in ordered.iterrows():
            if row.get("_category") != category or idx in selected_indices:
                continue
            topic_key = row.get("_topic_key", "")
            if topic_key and topic_key in used_topics:
                continue
            selected_indices.append(idx)
            if topic_key:
                used_topics.add(topic_key)
            break
        if len(selected_indices) >= top_n:
            break

    for idx, row in ordered.iterrows():
        if len(selected_indices) >= top_n:
            break
        if idx in selected_indices:
            continue
        topic_key = row.get("_topic_key", "")
        if topic_key and topic_key in used_topics:
            continue
        selected_indices.append(idx)
        if topic_key:
            used_topics.add(topic_key)

    if len(selected_indices) < top_n:
        for idx in ordered.index:
            if len(selected_indices) >= top_n:
                break
            if idx not in selected_indices:
                selected_indices.append(idx)

    selected = ordered.loc[selected_indices].copy()
    return selected


def summarize_latest_news(news_df, stock_name, openai_api_key, top_n):
    if not openai_api_key:
        return None, "뉴스 요약 생성을 위해 .env에 OPENAI_API_KEY를 설정하세요."
    if news_df.empty:
        return None, "요약할 뉴스가 없습니다."

    selected = select_news_for_summary(news_df, top_n)
    if selected.empty:
        return None, "요약할 뉴스를 선별하지 못했습니다."

    docs = []
    total_chars = 0

    for _, row in selected.iterrows():
        title = str(row.get("제목", "제목 없음"))
        date_text = str(row.get("일시", ""))
        press = str(row.get("언론사", ""))
        link = str(row.get("기사링크", ""))
        category = str(row.get("_category", "")).strip()

        body = ""
        if link:
            try:
                body = fetch_news_article_text(link)
            except Exception:
                body = ""

        category_tag = f"[{category}] " if category else ""
        snippet = f"{category_tag}[{date_text}] {title} ({press})\n{body[:2500]}"
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
                    f"{stock_name} 중요도 반영 뉴스 요약을 작성해줘.\n"
                    "아래 뉴스는 최신성, 중요도, 카테고리 다양성을 반영해 선별됐다.\n"
                    "형식:\n"
                    "1) 핵심 요약(5줄 이내)\n"
                    "2) 중요 이벤트 포인트(최대 4개)\n"
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


def _to_absolute_naver_url(href):
    value = str(href or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return "https://finance.naver.com" + value
    return "https://finance.naver.com/" + value.lstrip("/")


@st.cache_data(ttl=180)
def collect_stocknews_by_code(stock_code, max_pages=5, max_items=20):
    code = str(stock_code or "").strip()
    if not code:
        return []

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://finance.naver.com/item/news.naver?code={code}",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    rows = []
    seen_urls = set()

    for page in range(1, max_pages + 1):
        res = requests.get(
            "https://finance.naver.com/item/news_news.naver",
            headers=headers,
            params={"code": code, "page": page, "clusterId": ""},
            timeout=10,
        )
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        anchors = soup.select("table.type5 td.title a")
        if not anchors:
            continue

        for a in anchors:
            href = _to_absolute_naver_url(a.get("href", ""))
            if not href or href in seen_urls:
                continue

            td = a.find_parent("td")
            tr = td.find_parent("tr") if td else None
            press_node = tr.select_one("td.info") if tr else None
            wdate_node = tr.select_one("td.date") if tr else None

            title_text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip() or "기사보기"
            press_text = press_node.get_text(" ", strip=True) if press_node else ""
            date_text = wdate_node.get_text(" ", strip=True) if wdate_node else ""

            seen_urls.add(href)
            rows.append(
                {
                    "일시": date_text,
                    "언론사": press_text,
                    "제목": title_text,
                    "기사링크": href,
                }
            )
            if len(rows) >= max_items:
                return rows
    return rows


POSITIVE_NEWS_WEIGHTS = {
    "상승": 1.0,
    "호재": 1.4,
    "기대": 0.8,
    "개선": 0.9,
    "성장": 1.0,
    "확대": 0.8,
    "흑자": 1.4,
    "강세": 1.1,
    "회복": 1.0,
    "상향": 1.2,
    "급등": 1.5,
    "수혜": 1.2,
    "사상 최대": 1.7,
    "돌파": 1.3,
    "계약 체결": 1.1,
    "수주": 1.2,
    "증가": 0.7,
}

NEGATIVE_NEWS_WEIGHTS = {
    "하락": 1.0,
    "악재": 1.5,
    "우려": 1.1,
    "불확실성": 1.2,
    "둔화": 1.0,
    "적자": 1.4,
    "약세": 1.0,
    "축소": 0.8,
    "급락": 1.6,
    "하향": 1.2,
    "리스크": 1.2,
    "부진": 1.1,
    "부담": 0.9,
    "경고": 1.2,
    "충격": 1.5,
    "감소": 0.7,
}

SENTIMENT_PHRASE_OVERRIDES = {
    "우려 해소": 1.6,
    "적자 축소": 1.3,
    "손실 축소": 1.3,
    "흑자 전환": 1.8,
    "실적 개선": 1.5,
    "실적 부진": -1.6,
    "실적 악화": -1.8,
    "적자 전환": -1.9,
    "목표가 하향": -1.7,
    "목표가 상향": 1.6,
}

GENERIC_NAME_TOKENS = {
    "etf",
    "tr",
    "액티브",
    "합성",
    "선물",
    "인버스",
    "레버리지",
    "증권",
    "채권",
    "회사채",
    "국고채",
}

ETF_BRAND_TOKENS = {
    "kodex",
    "tiger",
    "ace",
    "arirang",
    "kindex",
    "kbstar",
    "hanaro",
    "rise",
    "sol",
    "plus",
    "kosef",
    "1q",
}


def _news_sentiment_score(text):
    t = str(text or "").lower()
    if not t:
        return 0.0

    pos_raw = sum(weight for keyword, weight in POSITIVE_NEWS_WEIGHTS.items() if keyword in t)
    neg_raw = sum(weight for keyword, weight in NEGATIVE_NEWS_WEIGHTS.items() if keyword in t)
    raw = pos_raw - neg_raw

    for phrase, delta in SENTIMENT_PHRASE_OVERRIDES.items():
        if phrase in t:
            raw += float(delta)

    raw = max(-6.0, min(6.0, raw))
    if raw == 0:
        return 0.0
    return raw / 6.0


def _weighted_average(values, weights):
    value_series = pd.to_numeric(values, errors="coerce")
    weight_series = pd.to_numeric(weights, errors="coerce").fillna(0.0)
    valid = value_series.notna() & weight_series.gt(0)
    if not valid.any():
        if value_series.notna().any():
            return float(value_series.dropna().mean())
        return 0.0

    value_series = value_series[valid]
    weight_series = weight_series[valid]
    total_weight = float(weight_series.sum())
    if total_weight <= 0:
        return float(value_series.mean()) if not value_series.empty else 0.0
    return float((value_series * weight_series).sum() / total_weight)


def _prepare_weighted_sentiment_frame(df):
    if df is None or df.empty:
        return pd.DataFrame(columns=["제목", "일시", "기사링크", "sent_score", "news_weight"])

    out = df.copy()
    out["일시"] = pd.to_datetime(out.get("일시"), errors="coerce")
    out["sent_score"] = out["제목"].apply(_news_sentiment_score)
    out["_importance_raw"] = out["제목"].apply(_news_importance_score)
    out["_importance_weight"] = (1.0 + (out["_importance_raw"] / 12.0)).clip(lower=1.0, upper=3.0)

    latest_ts = out["일시"].dropna().max()
    if pd.isna(latest_ts):
        out["_recency_weight"] = 0.35
    else:
        age_hours = ((latest_ts - out["일시"]).dt.total_seconds() / 3600.0).clip(lower=0)
        recency = 0.5 ** (age_hours / 48.0)
        out["_recency_weight"] = recency.fillna(0.35).clip(lower=0.15, upper=1.0)

    out["news_weight"] = (out["_importance_weight"] * out["_recency_weight"]).clip(lower=0.15, upper=3.5)
    return out


def _build_recent_sentiment_trend(scored_df, top_n_days=7):
    if scored_df is None or scored_df.empty or "일시" not in scored_df.columns:
        return pd.DataFrame(columns=["일자", "감성지수"])

    trend_source = scored_df.dropna(subset=["일시"]).copy()
    if trend_source.empty:
        return pd.DataFrame(columns=["일자", "감성지수"])

    trend_source["일자"] = trend_source["일시"].dt.normalize()
    daily_rows = []
    for day, group in trend_source.groupby("일자"):
        day_score = _weighted_average(group["sent_score"], group["news_weight"])
        daily_rows.append(
            {
                "일자": day,
                "감성지수": max(0.0, min(100.0, 50 + (day_score * 50))),
            }
        )

    if not daily_rows:
        return pd.DataFrame(columns=["일자", "감성지수"])

    top_n = max(1, int(top_n_days))
    trend_df = pd.DataFrame(daily_rows).sort_values("일자").tail(top_n).reset_index(drop=True)
    if len(trend_df) >= top_n:
        return trend_df

    end_day = trend_df["일자"].max()
    calendar_df = pd.DataFrame({"일자": pd.date_range(end=end_day, periods=top_n, freq="D").normalize()})
    merged = calendar_df.merge(trend_df, on="일자", how="left")

    filled = []
    prev = 50.0
    for value in merged["감성지수"]:
        if pd.isna(value):
            current = (prev * 0.7) + (50.0 * 0.3)
        else:
            current = float(value)
        current = max(0.0, min(100.0, current))
        filled.append(current)
        prev = current

    merged["감성지수"] = filled
    return merged.reset_index(drop=True)


def _select_related_news(news_df, stock_name, stock_code=""):
    if news_df is None or news_df.empty:
        return pd.DataFrame(columns=news_df.columns if isinstance(news_df, pd.DataFrame) else None)

    base = news_df.copy()
    if "제목" in base.columns:
        base["제목"] = base["제목"].fillna("").astype(str).str.strip()
    else:
        base["제목"] = ""

    code = str(stock_code or "").strip()
    if code and "기사링크" in base.columns:
        link_series = base["기사링크"].fillna("").astype(str)
        code_mask = link_series.str.contains(rf"(?:[?&]code={re.escape(code)}(?:&|$))", case=False, regex=True)
        if code_mask.any():
            return base[code_mask].copy()

    keywords = [k.lower() for k in _stock_keywords(stock_name)]
    if keywords:
        title_mask = base["제목"].str.lower().apply(lambda t: any(k in t for k in keywords))
        if title_mask.any():
            return base[title_mask].copy()

    return pd.DataFrame(columns=base.columns)


def _recent_coverage_days(scored_df):
    if scored_df is None or scored_df.empty or "일시" not in scored_df.columns:
        return 0
    today = pd.Timestamp(datetime.date.today())
    start_day = today - pd.Timedelta(days=6)
    dated = scored_df.dropna(subset=["일시"]).copy()
    if dated.empty:
        return 0
    days = dated["일시"].dt.normalize()
    covered = days[days.between(start_day.normalize(), today.normalize(), inclusive="both")]
    return int(covered.dropna().nunique())


def _align_bonus(news_score, flow_score, has_flow):
    if not has_flow:
        return 0
    gap = abs(float(news_score) - float(flow_score))
    bonus = 6 - int(round(gap * 4))
    return max(0, bonus)


def _calibrate_confidence(combined_score, scored_df, flow_text, news_score, flow_score):
    news_count = int(len(scored_df)) if scored_df is not None else 0
    data_bonus = min(12, int(news_count * 1.2))
    coverage_bonus = min(8, _recent_coverage_days(scored_df))
    consistency_bonus = _align_bonus(news_score, flow_score, bool(flow_text))
    base = 50 + (abs(float(combined_score)) * 30) + data_bonus + coverage_bonus + consistency_bonus
    return int(max(45, min(95, round(base))))


def _lookback_news(df, days=30):
    if df is None or df.empty or "일시" not in df.columns:
        return df
    dated = df.dropna(subset=["일시"]).copy()
    if dated.empty:
        return df.copy()
    latest_ts = dated["일시"].max()
    cutoff = latest_ts - pd.Timedelta(days=max(1, int(days)))
    sliced = df[(df["일시"].isna()) | (df["일시"] >= cutoff)].copy()
    return sliced if not sliced.empty else df.copy()


def _normalize_keyword_token(token):
    return re.sub(r"[^0-9A-Za-z가-힣&]+", "", str(token or "")).lower()


def _stock_keywords(stock_name):
    norm = str(stock_name or "").strip()
    if not norm:
        return []

    base = [norm, re.sub(r"\s+", "", norm)]
    split_tokens = [t.strip() for t in re.split(r"[^0-9A-Za-z가-힣&]+", norm) if str(t).strip()]

    normalized_tokens = []
    for token in split_tokens:
        low = _normalize_keyword_token(token)
        if not low or len(low) < 2:
            continue
        if low.isdigit() and len(low) <= 3:
            continue
        normalized_tokens.append((token, low))

    for token, low in normalized_tokens:
        if low in GENERIC_NAME_TOKENS or low in ETF_BRAND_TOKENS:
            continue
        base.append(token)
        if "&" in token:
            base.append(token.replace("&", ""))

    brand_token = ""
    core_token = ""
    for token, low in normalized_tokens:
        if not brand_token and low in ETF_BRAND_TOKENS:
            brand_token = token
            continue
        if not core_token and low not in ETF_BRAND_TOKENS and low not in GENERIC_NAME_TOKENS:
            core_token = token
    if brand_token and core_token:
        base.append(f"{brand_token} {core_token}")
        base.append(f"{brand_token}{core_token}")

    seen = set()
    out = []
    for token in base:
        token = str(token).strip()
        if not token:
            continue
        low = token.lower()
        if low not in seen:
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


def _sentiment_meta(sentiment_pct):
    if sentiment_pct >= 63:
        return "Bullish", "매수 우위", "긍정적인 신호"
    if sentiment_pct <= 37:
        return "Bearish", "매도 우위", "부정적인 신호"
    return "Neutral", "관망", "중립 신호"


def build_investment_report(stock_name, news_rows, flow_df, stock_code=""):
    empty_trend_df = pd.DataFrame(columns=["일자", "감성지수"])
    news_df = pd.DataFrame(news_rows)
    if news_df.empty:
        return {
            "summary": "분석 가능한 뉴스가 부족합니다. 잠시 후 다시 시도해 주세요.",
            "sentiment_pct": 50,
            "sentiment_label": "Neutral",
            "opinion": "관망",
            "confidence": 50,
            "trend_df": empty_trend_df,
            "positive_points": [],
            "negative_points": [],
            "news_count": 0,
        }

    if "제목" in news_df.columns:
        news_df["제목"] = news_df["제목"].astype(str).str.strip()
    else:
        news_df["제목"] = ""
    if "일시" in news_df.columns:
        news_df["일시"] = news_df["일시"].apply(_parse_news_datetime)
    else:
        news_df["일시"] = pd.NaT
    related_df = _select_related_news(news_df, stock_name, stock_code=stock_code)

    flow_text, flow_score = _flow_signal_text(flow_df)

    if related_df.empty:
        if flow_text:
            sentiment_pct = int(round(max(0.0, min(100.0, 50 + flow_score * 25))))
            sentiment_label, opinion, signal_text = _sentiment_meta(sentiment_pct)
            summary = (
                f"{stock_name} 관련 뉴스가 부족하여 수급 데이터 중심으로 판단했습니다. "
                f"최근 {flow_text} 흐름에서 {signal_text}가 관찰됩니다."
            )
            confidence = 45
        else:
            sentiment_pct = 50
            sentiment_label, opinion, _ = _sentiment_meta(sentiment_pct)
            summary = (
                f"{stock_name} 관련 뉴스가 부족해 현재는 유의미한 신호가 부족합니다. "
                "추가 뉴스 확인 전까지 관망이 적절합니다."
            )
            confidence = 40

        return {
            "summary": summary,
            "sentiment_pct": sentiment_pct,
            "sentiment_label": sentiment_label,
            "opinion": opinion,
            "confidence": confidence,
            "trend_df": empty_trend_df,
            "positive_points": [],
            "negative_points": [],
            "news_count": 0,
        }

    dedupe_cols = [c for c in ["기사링크", "제목", "일시"] if c in related_df.columns]
    if dedupe_cols:
        related_df = related_df.drop_duplicates(subset=dedupe_cols, keep="first")
    related_df = _lookback_news(related_df, days=30)
    scored_df = _prepare_weighted_sentiment_frame(related_df)
    if scored_df.empty:
        return {
            "summary": "분석 가능한 뉴스가 부족합니다. 잠시 후 다시 시도해 주세요.",
            "sentiment_pct": 50,
            "sentiment_label": "Neutral",
            "opinion": "관망",
            "confidence": 50,
            "trend_df": empty_trend_df,
            "positive_points": [],
            "negative_points": [],
            "news_count": 0,
        }

    news_score = _weighted_average(scored_df["sent_score"], scored_df["news_weight"])
    combined_score = (news_score * 0.75) + (flow_score * 0.25)
    sentiment_pct = int(round(max(0.0, min(100.0, 50 + combined_score * 50))))
    sentiment_label, opinion, signal_text = _sentiment_meta(sentiment_pct)
    confidence = _calibrate_confidence(combined_score, scored_df, flow_text, news_score, flow_score)

    if flow_text:
        first_sentence = f"최근 {flow_text}와 뉴스 흐름에서 {signal_text}가 포착되었습니다."
    else:
        first_sentence = f"최근 뉴스 흐름에서 {signal_text}가 포착되었습니다."

    risk_words = ["금리", "환율", "인플레이션", "침체", "불확실성", "리스크"]
    risk_exists = scored_df["제목"].str.contains("|".join(risk_words), case=False, regex=True).any()
    if risk_exists:
        second_sentence = "다만 거시 환경 리스크와 단기 변동성에는 유의가 필요합니다."
    else:
        second_sentence = "다만 단기 변동성 확대 가능성은 함께 점검해야 합니다."

    trend_df = _build_recent_sentiment_trend(scored_df)
    scored_df = scored_df.copy()
    scored_df["_impact"] = scored_df["sent_score"] * scored_df["news_weight"]

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
        scored_df[scored_df["_impact"] > 0]
        .sort_values(["_impact", "일시"], ascending=[False, False])
        .head(2)
    )
    neg_points = _to_point_records(
        scored_df[scored_df["_impact"] < 0]
        .sort_values(["_impact", "일시"], ascending=[True, False])
        .head(2)
    )

    if not pos_points and not scored_df.empty:
        pos_points = _to_point_records(scored_df.sort_values("일시", ascending=False).head(1))

    return {
        "summary": f"{first_sentence} {second_sentence}",
        "sentiment_pct": sentiment_pct,
        "sentiment_label": sentiment_label,
        "opinion": opinion,
        "confidence": confidence,
        "trend_df": trend_df,
        "positive_points": pos_points,
        "negative_points": neg_points,
        "news_count": int(len(scored_df)),
    }


@st.cache_data(ttl=3600)
def _fetch_stock_list_from_krx(market):
    market = str(market or "KRX").upper()
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
        return pd.DataFrame(columns=["Code", "Name", "Market"])

    df = df.rename(columns={"short_code": "Code", "codeName": "Name", "marketEngName": "Market"})
    df["Code"] = df["Code"].astype(str).str.strip().str.zfill(6)
    df["Name"] = df["Name"].astype(str).str.strip()
    if market != "KRX":
        df = df[df["Market"].astype(str).str.upper() == market]
    return (
        df[["Code", "Name", "Market"]]
        .dropna(subset=["Code", "Name"])
        .drop_duplicates("Code")
        .reset_index(drop=True)
    )


@st.cache_data(ttl=3600)
def _fetch_etf_list_from_krx():
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "http://data.krx.co.kr/"}
    r = requests.post(
        "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
        headers=headers,
        data={
            "bld": "dbms/comm/finder/finder_secuprodisu",
            "mktsel": "ETF",
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("block1", [])
    df = pd.DataFrame(data)
    if df.empty:
        return pd.DataFrame(columns=["Code", "Name", "Market"])

    df = df.rename(columns={"short_code": "Code", "codeName": "Name"})
    df["Code"] = df["Code"].astype(str).str.strip().str.zfill(6)
    df["Name"] = df["Name"].astype(str).str.strip()
    df["Market"] = "ETF"
    return (
        df[["Code", "Name", "Market"]]
        .dropna(subset=["Code", "Name"])
        .drop_duplicates("Code")
        .reset_index(drop=True)
    )


@st.cache_data(ttl=3600)
def get_stock_list(market):
    market = str(market).upper()
    try:
        if market == "ETF":
            return _fetch_etf_list_from_krx()
        if market in {"KOSPI", "KOSDAQ", "KONEX", "KRX"}:
            return _fetch_stock_list_from_krx(market)
    except Exception:
        pass
    return pd.DataFrame(columns=["Code", "Name", "Market"])


@st.cache_data(ttl=600, show_spinner=False)
def _parse_kis_rank_output(body, value_patterns):
    df = _to_dataframe(body.get("output"))
    if df.empty:
        return pd.DataFrame(columns=["Code", "Value", "Rank"])

    code_col = _find_first_matching_col(df.columns, ["mksc_shrn_iscd", "stck_shrn_iscd", "isu_cd", "code"])
    rank_col = _find_first_matching_col(df.columns, ["data_rank", "rank"])
    value_col = _find_first_matching_col(df.columns, value_patterns)
    if not code_col:
        return pd.DataFrame(columns=["Code", "Value", "Rank"])

    out = pd.DataFrame({"Code": df[code_col].astype(str).str.strip().str.zfill(6)})
    out["Value"] = _to_numeric_series(df[value_col]) if value_col else pd.NA
    out["Rank"] = _to_numeric_series(df[rank_col]) if rank_col else pd.NA
    return out.dropna(subset=["Code"]).drop_duplicates("Code").reset_index(drop=True)


@st.cache_data(ttl=600, show_spinner=False)
def get_kis_volume_rank(app_key, app_secret, kis_env):
    body = _kis_get(
        app_key,
        app_secret,
        kis_env,
        "/uapi/domestic-stock/v1/quotations/volume-rank",
        "FHPST01710000",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "99999999",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        },
    )
    return _parse_kis_rank_output(body, ["acml_vol", "vol"])


@st.cache_data(ttl=600, show_spinner=False)
def get_kis_trade_value_rank(app_key, app_secret, kis_env):
    body = _kis_get(
        app_key,
        app_secret,
        kis_env,
        "/uapi/domestic-stock/v1/quotations/volume-rank",
        "FHPST01710000",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "3",
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "99999999",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        },
    )
    return _parse_kis_rank_output(body, ["acml_tr_pbmn", "avrg_tr_pbmn", "tr_pbmn", "amount"])


@st.cache_data(ttl=600, show_spinner=False)
def get_kis_market_cap_rank(app_key, app_secret, kis_env):
    body = _kis_get(
        app_key,
        app_secret,
        kis_env,
        "/uapi/domestic-stock/v1/ranking/market-cap",
        "FHPST01740000",
        {
            "FID_INPUT_PRICE_2": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20174",
            "FID_DIV_CLS_CODE": "0",
            "FID_INPUT_ISCD": "0000",
            "FID_TRGT_CLS_CODE": "0",
            "FID_TRGT_EXLS_CLS_CODE": "0",
            "FID_INPUT_PRICE_1": "",
            "FID_VOL_CNT": "",
        },
    )
    return _parse_kis_rank_output(body, ["stck_avls", "market_cap", "mktcap"])


def _sort_stock_list_by_rank(df_list, rank_df):
    if df_list is None or df_list.empty:
        return pd.DataFrame(columns=["Code", "Name", "Market"])

    base = df_list.copy()
    if rank_df is None or rank_df.empty:
        return base.sort_values("Name").reset_index(drop=True)

    merged = base.merge(rank_df, on="Code", how="left")
    merged["__rank"] = _to_numeric_series(merged["Rank"])
    merged["__value"] = _to_numeric_series(merged["Value"])
    merged["__rank_order"] = merged["__rank"].fillna(10**9)
    merged["__value_order"] = merged["__value"].fillna(-1)
    merged = merged.sort_values(
        ["__rank_order", "__value_order", "Name"],
        ascending=[True, False, True],
        kind="mergesort",
    )
    return merged[["Code", "Name", "Market"]].reset_index(drop=True)


def _sort_stock_list_by_name(df_list):
    if df_list is None or df_list.empty:
        return pd.DataFrame(columns=["Code", "Name", "Market"])
    return df_list.sort_values("Name").reset_index(drop=True)


def _get_rank_dataframe(sort_mode, app_key, app_secret, kis_env):
    fetchers = {
        "시가총액": get_kis_market_cap_rank,
        "거래대금": get_kis_trade_value_rank,
        "거래량": get_kis_volume_rank,
    }
    fetcher = fetchers.get(str(sort_mode))
    if not fetcher:
        return pd.DataFrame(columns=["Code", "Value", "Rank"])
    return fetcher(app_key, app_secret, kis_env)


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


def _extract_latest_numeric_ratio(ratio_df, value_col, period_col):
    if ratio_df is None or ratio_df.empty or not value_col:
        return None, ""

    ordered = ratio_df.copy()
    if period_col:
        ordered["__period"] = _to_numeric_series(ordered[period_col])
        ordered = ordered.sort_values("__period", ascending=False, na_position="last")

    for _, row in ordered.iterrows():
        value = _to_numeric_value(row.get(value_col))
        if value is not None:
            period = str(row.get(period_col, "")).strip() if period_col else ""
            return value, period

    if period_col and not ordered.empty:
        return None, str(ordered.iloc[0].get(period_col, "")).strip()
    return None, ""


@st.cache_data(ttl=120, show_spinner=False)
def get_kis_valuation_metrics(stock_code, app_key, app_secret, kis_env):
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
    output = body.get("output") if isinstance(body.get("output"), dict) else {}

    per = _to_numeric_value(output.get("per"))
    pbr = _to_numeric_value(output.get("pbr"))
    eps = _to_numeric_value(output.get("eps"))
    bps = _to_numeric_value(output.get("bps"))

    date_raw = str(output.get("stck_bsop_date", "")).strip()
    asof = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}" if len(date_raw) == 8 and date_raw.isdigit() else ""

    ev_ebitda = None
    ebitda = None
    ratio_period = ""
    ratio_df = pd.DataFrame()

    for div_cls_code in ("0", "1"):
        for _ in range(2):
            try:
                ratio_body = _kis_get(
                    app_key,
                    app_secret,
                    kis_env,
                    "/uapi/domestic-stock/v1/finance/other-major-ratios",
                    "FHKST66430500",
                    {
                        "FID_INPUT_ISCD": stock_code,
                        "FID_DIV_CLS_CODE": div_cls_code,
                        "FID_COND_MRKT_DIV_CODE": "J",
                    },
                )
                ratio_df = _to_dataframe(ratio_body.get("output"))
                if not ratio_df.empty:
                    break
            except Exception:
                time.sleep(0.2)
        if not ratio_df.empty:
            break

    if not ratio_df.empty:
        period_col = _find_first_matching_col(ratio_df.columns, ["stac_yymm", "stac_ym", "yymm", "year"])
        ev_col = _find_first_matching_col(ratio_df.columns, ["ev_ebitda", "ev/ebitda"])
        ebitda_col = _find_first_matching_col(ratio_df.columns, ["ebitda"])

        ev_ebitda, ev_period = _extract_latest_numeric_ratio(ratio_df, ev_col, period_col)
        ebitda, ebitda_period = _extract_latest_numeric_ratio(ratio_df, ebitda_col, period_col)
        ratio_period = ev_period or ebitda_period or ""

    return {
        "per": per,
        "pbr": pbr,
        "ev_ebitda": ev_ebitda,
        "eps": eps,
        "bps": bps,
        "ebitda": ebitda,
        "asof": asof,
        "ratio_period": ratio_period,
    }


@st.cache_data(ttl=120, show_spinner=False)
def get_kis_etf_components(stock_code, app_key, app_secret, kis_env):
    body = _kis_get(
        app_key,
        app_secret,
        kis_env,
        "/uapi/etfetn/v1/quotations/inquire-component-stock-price",
        "FHKST121600C0",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_COND_SCR_DIV_CODE": "11216",
        },
    )

    df = _to_dataframe(body.get("output2"))
    if df.empty:
        return pd.DataFrame(columns=["종목명", "현재가", "등락폭", "단위증권수", "구성시가총액", "비중(%)", "평가금액"])

    rename_map = {
        "hts_kor_isnm": "종목명",
        "stck_prpr": "현재가",
        "prdy_vrss": "등락폭",
        "etf_cu_unit_scrt_cnt": "단위증권수",
        "etf_cnfg_issu_avls": "구성시가총액",
        "etf_cnfg_issu_rlim": "비중(%)",
        "etf_vltn_amt": "평가금액",
    }
    available_map = {k: v for k, v in rename_map.items() if k in df.columns}
    df = df.rename(columns=available_map)

    required_cols = ["종목명", "현재가", "등락폭", "단위증권수", "구성시가총액", "비중(%)", "평가금액"]
    for col in required_cols:
        if col not in df.columns:
            df[col] = pd.NA

    numeric_cols = ["현재가", "등락폭", "단위증권수", "구성시가총액", "비중(%)", "평가금액"]
    for col in numeric_cols:
        df[col] = _to_numeric_series(df[col].fillna("").astype(str))

    df = df[required_cols].copy()
    df["종목명"] = df["종목명"].fillna("").astype(str).str.strip()
    df = df[df["종목명"] != ""].reset_index(drop=True)
    df = df.sort_values("비중(%)", ascending=False, na_position="last")
    return df.reset_index(drop=True)


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
    st.set_page_config(page_title="금융 프로젝트")

    stock_name = ""
    stock_code = ""
    stock_industry = ""
    stock_market = ""
    with st.sidebar:
        st.header("KOSPI")
        opendart_api = os.getenv("OPENDART_API_KEY", "").strip()
        openai_api = os.getenv("OPENAI_API_KEY", "").strip()
        kis_app_key = _get_env_first("KIS_APP_KEY", "KIS_APPKEY")
        kis_app_secret = _get_env_first("KIS_APP_SECRET", "KIS_APPSECRET")
        kis_env = _normalize_kis_env(_get_env_first("KIS_ENV"))

        product_type = st.selectbox("📂 종목유형", ("주식", "ETF"), index=0)
        market = "KOSPI" if product_type == "주식" else "ETF"
        sort_mode = st.selectbox("정렬", ("시가총액", "거래대금", "거래량", "이름"), index=0)
        df_list = get_stock_list(market)

        if not df_list.empty:
            if sort_mode == "이름":
                df_list = _sort_stock_list_by_name(df_list)
            elif kis_app_key and kis_app_secret:
                try:
                    rank_df = _get_rank_dataframe(sort_mode, kis_app_key, kis_app_secret, kis_env)

                    if rank_df is None or rank_df.empty:
                        df_list = _sort_stock_list_by_name(df_list)
                        st.caption(f"KIS {sort_mode} 랭킹 데이터가 없어 이름순 정렬")
                    else:
                        df_list = _sort_stock_list_by_rank(df_list, rank_df)
                        st.caption(f"KIS {sort_mode} 랭킹 기준 상위 종목 우선 정렬")
                except Exception:
                    df_list = _sort_stock_list_by_name(df_list)
                    st.caption(f"{sort_mode} 정렬 실패로 이름순 정렬")
            else:
                df_list = _sort_stock_list_by_name(df_list)
                st.caption("KIS 키가 없어 이름순 정렬")

        selected = None
        if not df_list.empty:
            normalized = df_list[["Code", "Name", "Market"]].copy()
            normalized["Code"] = normalized["Code"].astype(str).str.strip().str.zfill(6)
            normalized["Name"] = normalized["Name"].astype(str).str.strip()
            normalized["Market"] = normalized["Market"].astype(str).str.strip()

            code_to_meta = {
                row.Code: (row.Name, row.Market)
                for row in normalized.itertuples(index=False)
            }
            stock_codes = list(code_to_meta.keys())

            selected_code_key = f"selected_stock_code_{market}"
            current_code = st.session_state.get(selected_code_key)
            if current_code not in stock_codes:
                st.session_state[selected_code_key] = stock_codes[0]

            selected_code = st.selectbox(
                "📌 종목 선정",
                stock_codes,
                key=selected_code_key,
                format_func=lambda code: f"{code_to_meta[code][0]}({code})",
            )
            selected_name, selected_market = code_to_meta[selected_code]
            selected = (selected_code, selected_name, selected_market)
        else:
            st.warning("선택 가능한 종목이 없습니다.")

        st.subheader("API Key (.env)")
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

        if selected:
            stock_code, stock_name, stock_market = selected
            if kis_app_key and kis_app_secret:
                try:
                    stock_industry = get_kis_stock_industry(stock_code, kis_app_key, kis_app_secret, kis_env)
                except Exception:
                    stock_industry = ""

    if not stock_name:
        st.info("좌측에서 종목을 선택하세요.")
        return

    st.title(f"📌 {stock_name} ({stock_code})")
    st.caption(f" {stock_industry or '정보 없음'}")

    is_etf = str(stock_market or "").upper() == "ETF"
    include_valuation_tab = not is_etf
    tab_labels = ["📈 주가 차트", "💹 외국인/기관 수급"]
    if include_valuation_tab:
        tab_labels.append("📊 밸류 지표")
    if is_etf:
        tab_labels.append("🧩 구성종목")
    else:
        tab_labels.extend(["🧾 투자 분석 리포트", "🗂️ 전자공시"])
    tab_labels.append("📰 뉴스")
    tabs = dict(zip(tab_labels, st.tabs(tab_labels)))

    with tabs["📈 주가 차트"]:
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

    if include_valuation_tab:
        with tabs["📊 밸류 지표"]:
            if not kis_app_key or not kis_app_secret:
                st.warning("밸류 지표 조회를 위해 .env에 KIS_APP_KEY, KIS_APP_SECRET를 설정하세요.")
            else:
                render_valuation_tab(
                    stock_name=stock_name,
                    stock_code=stock_code,
                    kis_app_key=kis_app_key,
                    kis_app_secret=kis_app_secret,
                    kis_env=kis_env,
                    get_kis_valuation_metrics=get_kis_valuation_metrics,
                )

    with tabs["💹 외국인/기관 수급"]:
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

    if is_etf:
        with tabs["🧩 구성종목"]:
            if not kis_app_key or not kis_app_secret:
                st.warning("ETF 구성종목 조회를 위해 .env에 KIS_APP_KEY, KIS_APP_SECRET를 설정하세요.")
            else:
                render_etf_components_tab(
                    stock_name=stock_name,
                    stock_code=stock_code,
                    kis_app_key=kis_app_key,
                    kis_app_secret=kis_app_secret,
                    kis_env=kis_env,
                    get_kis_etf_components=get_kis_etf_components,
                )
    else:
        with tabs["🧾 투자 분석 리포트"]:
            render_report_tab(
                stock_name=stock_name,
                stock_code=stock_code,
                kis_app_key=kis_app_key,
                kis_app_secret=kis_app_secret,
                kis_env=kis_env,
                collect_stocknews_by_code=collect_stocknews_by_code,
                collect_mainnews_latest=collect_mainnews_latest,
                get_kis_investor_flow=get_kis_investor_flow,
                build_investment_report=build_investment_report,
            )

        with tabs["🗂️ 전자공시"]:
            if not opendart_api:
                st.warning("전자공시 조회를 위해 .env에 OPENDART_API_KEY를 설정하세요.")
            else:
                render_disclosure_tab(
                    stock_name=stock_name,
                    stock_code=stock_code,
                    stock_market=stock_market,
                    opendart_api=opendart_api,
                    openai_api_key=openai_api,
                    summarize_major_disclosures=summarize_major_disclosures,
                    is_openai_quota_error=_is_openai_quota_error,
                )

    with tabs["📰 뉴스"]:
        render_news_tab(
            stock_name=stock_name,
            stock_code=stock_code,
            openai_api_key=openai_api,
            collect_stocknews_by_code=collect_stocknews_by_code,
            collect_mainnews_latest=collect_mainnews_latest,
            summarize_latest_news=summarize_latest_news,
            is_openai_quota_error=_is_openai_quota_error,
        )


app()

