import pandas as pd
import streamlit as st


def _fmt_int(value):
    if value is None or pd.isna(value):
        return "-"
    try:
        return f"{float(value):,.0f}"
    except Exception:
        return "-"


def _fmt_change(value):
    if value is None or pd.isna(value):
        return "-"
    try:
        num = float(value)
    except Exception:
        return "-"
    arrow = "▲" if num > 0 else ("▼" if num < 0 else "-")
    return f"{arrow} {abs(num):,.0f}" if arrow != "-" else "- 0"


def _fmt_weight(value):
    if value is None or pd.isna(value):
        return "-"
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return "-"


def _change_color(val):
    text = str(val or "").strip()
    if text.startswith("▲"):
        return "color: #d92d20;"
    if text.startswith("▼"):
        return "color: #1d4ed8;"
    return "color: #374151;"


def render_etf_components_tab(
    stock_name,
    stock_code,
    kis_app_key,
    kis_app_secret,
    kis_env,
    get_kis_etf_components,
):
    st.subheader(f"🧩 {stock_name} 구성종목")
    try:
        comp_df = get_kis_etf_components(stock_code, kis_app_key, kis_app_secret, kis_env)
    except Exception as e:
        st.error(f"ETF 구성종목 조회 중 오류가 발생했습니다: {type(e).__name__} - {e}")
        return

    if comp_df.empty:
        st.info("표시할 ETF 구성종목 데이터가 없습니다.")
        return

    view_df = comp_df.copy()
    view_df["현재가"] = view_df["현재가"].map(_fmt_int)
    view_df["등락폭"] = view_df["등락폭"].map(_fmt_change)
    view_df["단위증권수"] = view_df["단위증권수"].map(_fmt_int)
    view_df["구성시가총액"] = view_df["구성시가총액"].map(_fmt_int)
    view_df["비중(%)"] = view_df["비중(%)"].map(_fmt_weight)
    view_df["평가금액"] = view_df["평가금액"].map(_fmt_int)

    st.caption(f"구성종목 {len(view_df)}건")
    styled = view_df.style.map(_change_color, subset=["등락폭"])
    st.dataframe(styled, hide_index=True, use_container_width=True)
