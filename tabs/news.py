import pandas as pd
import streamlit as st

DEFAULT_NEWS_SUMMARY_COUNT = 5


def render_news_tab(
    stock_name,
    stock_code,
    openai_api_key,
    collect_stocknews_by_code,
    collect_mainnews_latest,
    summarize_latest_news,
    is_openai_quota_error,
    max_items=60,
):
    st.header(f"📰 {stock_name} 최신 주요뉴스")
    used_mainnews_fallback = False
    try:
        rows = collect_stocknews_by_code(stock_code, max_pages=5, max_items=max_items)
        if not rows:
            used_mainnews_fallback = True
            rows = collect_mainnews_latest(max_pages=5, max_items=max_items)
    except Exception as e:
        st.error(f"뉴스 조회 중 오류가 발생했습니다: {type(e).__name__} - {e}")
        return

    if not rows:
        st.info("표시할 주요뉴스가 없습니다.")
        return

    if used_mainnews_fallback:
        st.caption("종목 전용 뉴스가 부족해 네이버 금융 주요뉴스를 대체 표시합니다.")

    news_df = pd.DataFrame(rows)
    display_df = news_df.copy()
    news_column_config = {}
    if {"제목", "기사링크"}.issubset(display_df.columns):
        title_text = (
            display_df["제목"]
            .fillna("")
            .astype(str)
            .str.replace("\r", " ", regex=False)
            .str.replace("\n", " ", regex=False)
            .str.strip()
        )
        invalid_title_mask = title_text.eq("") | title_text.str.match(r"^(https?://|www\.)", case=False, na=False)
        title_text = title_text.mask(invalid_title_mask, "기사보기")
        display_df["제목"] = display_df["기사링크"].astype(str) + "#" + title_text
        news_column_config["제목"] = st.column_config.LinkColumn(
            "제목",
            display_text=r".*#(.*)$",
        )
    display_cols = [c for c in ["일시", "언론사", "제목"] if c in display_df.columns]
    display_df = display_df[display_cols]
    display_limit = min(20, len(display_df))

    st.caption(f"최신순 상위 {display_limit}건 (요약 후보 {len(news_df)}건)")
    st.dataframe(
        display_df.head(display_limit),
        hide_index=True,
        use_container_width=True,
        column_config=news_column_config,
    )

    st.markdown("#### 중요도 반영 뉴스 요약")
    n = min(DEFAULT_NEWS_SUMMARY_COUNT, len(news_df))
    st.caption(f"최근 뉴스 {len(news_df)}건 중 중요도가 높은 {n}건을 요약합니다.")
    news_state_key = f"news_summary_text_{stock_code}"

    if st.button("중요도 반영 뉴스 요약 생성", key=f"news_btn_{stock_code}"):
        with st.spinner("중요도 반영 뉴스 요약 중입니다..."):
            try:
                text, err = summarize_latest_news(news_df, stock_name, openai_api_key, n)
                if err:
                    st.warning(err)
                else:
                    st.success(f"중요도 반영 뉴스 {n}건 요약 완료")
                    st.session_state[news_state_key] = text
            except Exception as e:
                if is_openai_quota_error(e):
                    st.error("OpenAI API 사용 한도를 초과했습니다(429 insufficient_quota).")
                else:
                    st.error(f"요약 중 오류가 발생했습니다: {type(e).__name__} - {e}")

    if news_state_key in st.session_state:
        st.markdown(st.session_state[news_state_key])
