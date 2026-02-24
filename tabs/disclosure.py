import datetime

import OpenDartReader
import pandas as pd
import streamlit as st


def render_disclosure_tab(
    stock_name,
    stock_code,
    opendart_api,
    openai_api_key,
    summarize_major_disclosures,
    is_openai_quota_error,
):
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

    major_keywords = ["사업보고서", "반기보고서", "분기보고서", "감사보고서"]
    if "report_nm" in df.columns:
        major_mask = df["report_nm"].astype(str).apply(lambda x: any(k in x for k in major_keywords))
    else:
        major_mask = pd.Series([True] * len(df), index=df.index)

    major_df = df[major_mask]
    other_df = df[~major_mask]

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
                column_config=column_config,
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
                        if is_openai_quota_error(e):
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
                column_config=column_config,
            )
