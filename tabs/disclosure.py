import datetime
import re

import OpenDartReader
import pandas as pd
import streamlit as st


def _normalize_text(value):
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", str(value or "")).lower()


@st.cache_data(ttl=900, show_spinner=False)
def _fetch_fund_disclosures(opendart_api, start):
    dart = OpenDartReader(opendart_api)
    df = dart.list(start=start, kind="G", final=False)
    if isinstance(df, pd.DataFrame):
        return df
    return pd.DataFrame()


def _filter_fund_disclosures_by_etf_name(df, stock_name):
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    target = _normalize_text(stock_name)
    if not target:
        return pd.DataFrame()

    report_series = df["report_nm"].fillna("").astype(str) if "report_nm" in df.columns else pd.Series("", index=df.index)
    corp_series = df["corp_name"].fillna("").astype(str) if "corp_name" in df.columns else pd.Series("", index=df.index)
    normalized = (report_series + " " + corp_series).map(_normalize_text)
    mask = normalized.str.contains(target, regex=False, na=False)

    if not mask.any():
        tokens = [_normalize_text(t) for t in str(stock_name).split() if _normalize_text(t)]
        if len(tokens) >= 2:
            mask = pd.Series(True, index=df.index)
            for token in tokens:
                mask = mask & normalized.str.contains(token, regex=False, na=False)

    return df[mask].copy()


def _contains_any_keyword(text, keywords):
    value = str(text or "")
    return any(keyword in value for keyword in keywords)


def _build_summary_targets(df, lookback_days=90, max_recent_events=6):
    meta = {
        "has_regular": False,
        "has_audit_or_review": False,
        "recent_event_count": 0,
        "used_fallback": False,
    }
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame(), meta

    report_series = df["report_nm"].fillna("").astype(str) if "report_nm" in df.columns else pd.Series("", index=df.index)

    regular_keywords = ["사업보고서", "반기보고서", "분기보고서"]
    audit_keywords = ["감사보고서", "검토보고서"]
    event_keywords = ["주요사항보고서", "정정"]

    regular_df = df[report_series.apply(lambda x: _contains_any_keyword(x, regular_keywords))].head(1).copy()
    if not regular_df.empty:
        regular_df["_summary_bucket"] = "기본: 최신 정기보고서"
        meta["has_regular"] = True

    audit_df = df[report_series.apply(lambda x: _contains_any_keyword(x, audit_keywords))].head(1).copy()
    if not audit_df.empty:
        audit_df["_summary_bucket"] = "보강: 최신 감사/검토"
        meta["has_audit_or_review"] = True

    if "rcept_dt" in df.columns:
        cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=lookback_days)
        recent_mask = df["rcept_dt"].notna() & (df["rcept_dt"] >= cutoff)
    else:
        recent_mask = pd.Series(True, index=df.index)

    event_mask = report_series.apply(lambda x: _contains_any_keyword(x, event_keywords))
    event_df = df[event_mask & recent_mask].head(max_recent_events).copy()
    if not event_df.empty:
        event_df["_summary_bucket"] = f"보강: 최근 {lookback_days}일 주요사항/정정"
        meta["recent_event_count"] = len(event_df)

    pieces = [frame for frame in (regular_df, audit_df, event_df) if not frame.empty]
    if not pieces:
        return pd.DataFrame(), meta

    selected = pd.concat(pieces).copy()
    if "rcept_no" in selected.columns:
        selected = selected.drop_duplicates(subset=["rcept_no"], keep="first")
    else:
        selected = selected.drop_duplicates(subset=["report_nm", "rcept_dt"], keep="first")
    if "rcept_dt" in selected.columns:
        selected = selected.sort_values("rcept_dt", ascending=False)

    return selected, meta


def render_disclosure_tab(
    stock_name,
    stock_code,
    stock_market,
    opendart_api,
    openai_api_key,
    summarize_major_disclosures,
    is_openai_quota_error,
):
    st.header(f"🗂️ {stock_name} 최근 전자공시")
    try:
        dart = OpenDartReader(opendart_api)
        market = str(stock_market or "").upper()
        if market == "ETF":
            # OpenDART fund disclosures without corp_code can only be queried up to 3 months.
            disclosures = pd.DataFrame()
            for lookback_days in (30, 90):
                start = (datetime.date.today() - datetime.timedelta(days=lookback_days)).strftime("%Y-%m-%d")
                fund_df = _fetch_fund_disclosures(opendart_api, start)
                disclosures = _filter_fund_disclosures_by_etf_name(fund_df, stock_name)
                if disclosures is not None and not disclosures.empty:
                    break
        else:
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
    if {"rcept_no", "report_nm"}.issubset(df.columns):
        report_title = (
            df["report_nm"]
            .fillna("보고서")
            .astype(str)
            .str.replace("\r", " ", regex=False)
            .str.replace("\n", " ", regex=False)
        )
        df["report_nm_link"] = (
            "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + df["rcept_no"].astype(str) + "#" + report_title
        )

    lookback_days = 90
    summary_df, summary_meta = _build_summary_targets(df, lookback_days=lookback_days)
    if summary_df.empty:
        summary_df = df.head(1).copy()
        summary_df["_summary_bucket"] = "대체: 최신 공시 1건"
        summary_meta["used_fallback"] = True

    if "rcept_no" in df.columns and "rcept_no" in summary_df.columns:
        selected_ids = set(summary_df["rcept_no"].astype(str))
        other_df = df[~df["rcept_no"].astype(str).isin(selected_ids)].copy()
    else:
        other_df = df.drop(index=summary_df.index, errors="ignore").copy()

    report_col = "report_nm_link" if "report_nm_link" in df.columns else "report_nm"
    display_cols = [c for c in ["rcept_dt", report_col, "flr_nm", "corp_name"] if c in df.columns]
    rename_map = {
        "rcept_dt": "접수일자",
        "report_nm": "보고서명",
        "report_nm_link": "보고서명",
        "flr_nm": "제출인",
        "corp_name": "회사명",
    }
    column_config = {}
    if report_col == "report_nm_link":
        column_config["보고서명"] = st.column_config.LinkColumn(
            "보고서명",
            display_text=r".*#(.*)$",
        )

    tab_major, tab_other = st.tabs(["요약 대상 공시", "기타 공시"])

    with tab_major:
        st.caption(
            f"기본: 최신 정기보고서 1건 | 보강: 최근 {lookback_days}일 주요사항/정정 + 최신 감사/검토 1건"
        )
        if not summary_meta["has_regular"]:
            st.warning("최신 정기보고서를 찾지 못해 대체 공시가 포함될 수 있습니다.")
        if not summary_meta["has_audit_or_review"]:
            st.info("최신 감사/검토 보고서를 찾지 못해 해당 항목은 생략됩니다.")
        if summary_meta["recent_event_count"] == 0:
            st.info(f"최근 {lookback_days}일 주요사항/정정 공시가 없어 해당 항목은 생략됩니다.")
        if summary_meta["used_fallback"]:
            st.warning("규칙에 맞는 공시를 찾지 못해 최신 공시 1건을 대체 요약 대상으로 사용합니다.")

        summary_display_cols = [c for c in ["_summary_bucket", "rcept_dt", report_col, "flr_nm", "corp_name"] if c in summary_df.columns]
        summary_rename_map = {"_summary_bucket": "선정기준", **rename_map}
        summary_view = summary_df[summary_display_cols].rename(columns=summary_rename_map).head(30)

        st.dataframe(
            summary_view,
            hide_index=True,
            use_container_width=True,
            column_config=column_config,
        )

        st.markdown("#### 공시 요약")
        summary_text_key = f"major_summary_text_{stock_code}"
        summary_metrics_key = f"major_summary_metrics_{stock_code}"

        if st.button("요약 생성", key=f"major_btn_{stock_code}"):
            with st.spinner("요약 생성 중입니다..."):
                try:
                    text, metrics_rows, err = summarize_major_disclosures(
                        dart,
                        summary_df,
                        stock_name,
                        openai_api_key,
                    )
                    if err:
                        st.warning(err)
                    else:
                        st.success(f"요약 대상 공시 {len(summary_df)}건 요약 완료")
                        st.session_state[summary_text_key] = text
                        st.session_state[summary_metrics_key] = metrics_rows
                except Exception as e:
                    if is_openai_quota_error(e):
                        st.error("OpenAI API 사용 한도를 초과했습니다(429 insufficient_quota).")
                    else:
                        st.error(f"요약 중 오류가 발생했습니다: {type(e).__name__} - {e}")

        if summary_text_key in st.session_state:
            st.markdown(st.session_state[summary_text_key])
            st.markdown("#### 핵심 수치 테이블")
            metrics_rows = st.session_state.get(summary_metrics_key, [])
            if metrics_rows:
                metrics_df = pd.DataFrame(metrics_rows)
                metric_cols = ["항목", "값", "기준기간", "전기/전년 대비", "출처 공시"]
                for col in metric_cols:
                    if col not in metrics_df.columns:
                        metrics_df[col] = "-"
                st.dataframe(
                    metrics_df[metric_cols],
                    hide_index=True,
                    use_container_width=True,
                )
            else:
                st.info("핵심 수치로 추출된 값이 없어 테이블을 표시하지 않습니다.")

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
                column_config=column_config,
            )
