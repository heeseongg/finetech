import pandas as pd
import streamlit as st


def _price_row_color(sign_value):
    if pd.isna(sign_value):
        return ""
    if sign_value > 0:
        return "color: #d92d20;"
    if sign_value < 0:
        return "color: #1d4ed8;"
    return "color: #374151;"


def render_price_chart_tab(
    stock_name,
    stock_code,
    stock_industry,
    kis_app_key,
    kis_app_secret,
    kis_env,
    get_kis_daily_ohlcv,
    get_kis_realtime_price,
):
    st.subheader(f"📈 {stock_name} 주가 차트 (KIS)")
    st.caption(f"산업군: {stock_industry or '정보 없음'}")
    try:
        rt = get_kis_realtime_price(stock_code, kis_app_key, kis_app_secret, kis_env)
    except Exception:
        rt = {}

    price = rt.get("price")
    change = rt.get("change")
    rate = rt.get("change_rate")
    asof = rt.get("asof", "")

    if price is None:
        st.caption("실시간 현재가를 불러오지 못했습니다.")
    else:
        if change is None:
            delta_text = None
        else:
            arrow = "▲" if change > 0 else ("▼" if change < 0 else "-")
            if rate is None:
                delta_text = f"{arrow} {abs(change):,.0f}" if arrow != "-" else "- 0"
            else:
                delta_text = (
                    f"{arrow} {abs(change):,.0f} ({abs(rate):.2f}%)"
                    if arrow != "-"
                    else f"- 0 ({abs(rate):.2f}%)"
                )
        st.metric("실시간 현재가", f"{price:,.0f}원", delta=delta_text)
        if asof:
            st.caption(f"기준시각: {asof}")

    days = st.slider("주가 조회 기간(거래일)", 20, 120, 60, key=f"kis_chart_days_{stock_code}")

    st.markdown("#### 주가 시계열")
    try:
        price_df = get_kis_daily_ohlcv(stock_code, kis_app_key, kis_app_secret, kis_env, days)
    except Exception as e:
        st.error(f"KIS 일봉 조회 중 오류가 발생했습니다: {type(e).__name__} - {e}")
        price_df = pd.DataFrame()

    if price_df.empty:
        st.info("표시할 일봉 데이터가 없습니다.")
    else:
        if {"일자", "종가"}.issubset(price_df.columns):
            chart_df = price_df[["일자", "종가"]].dropna().tail(days).set_index("일자")
            st.line_chart(chart_df, use_container_width=True)

        st.markdown("#### 최근 일봉 데이터")
        view_df = price_df.copy()
        if "전일대비" not in view_df.columns and "종가" in view_df.columns:
            view_df["전일대비"] = view_df["종가"].diff()

        if {"종가", "전일대비"}.issubset(view_df.columns):
            prev_close = view_df["종가"] - view_df["전일대비"]
            prev_close = prev_close.replace(0, pd.NA)
            view_df["전일대비율"] = (view_df["전일대비"] / prev_close) * 100
            sign = view_df["전일대비"].fillna(0)
        else:
            view_df["전일대비율"] = pd.NA
            sign = pd.Series(0, index=view_df.index)

        if "일자" in view_df.columns:
            view_df["일자"] = pd.to_datetime(view_df["일자"], errors="coerce").dt.strftime("%y/%m/%d")

        def _fmt_int(val):
            if pd.isna(val):
                return "-"
            return f"{float(val):,.0f}"

        def _fmt_change(val):
            if pd.isna(val):
                return "-"
            num = float(val)
            arrow = "▲" if num > 0 else ("▼" if num < 0 else "-")
            return f"{arrow} {abs(num):,.0f}" if arrow != "-" else "- 0"

        def _fmt_rate(val):
            if pd.isna(val):
                return "-"
            return f"{abs(float(val)):.2f}%"

        price_view = pd.DataFrame(
            {
                "일자": view_df["일자"] if "일자" in view_df.columns else "-",
                "종가": view_df["종가"].map(_fmt_int) if "종가" in view_df.columns else "-",
                "등락폭": view_df["전일대비"].map(_fmt_change) if "전일대비" in view_df.columns else "-",
                "전일대비": view_df["전일대비율"].map(_fmt_rate),
                "거래량": view_df["거래량"].map(_fmt_int) if "거래량" in view_df.columns else "-",
                "_sign": sign,
            }
        ).tail(30).iloc[::-1]

        sign_map = price_view["_sign"].copy()
        price_view = price_view.drop(columns=["_sign"])

        def _style_price_row(row):
            row_sign = sign_map.loc[row.name]
            color_css = _price_row_color(row_sign)
            return [
                color_css if col in {"종가", "등락폭", "전일대비"} else ""
                for col in price_view.columns
            ]

        styled_price = price_view.style.apply(_style_price_row, axis=1)
        st.dataframe(styled_price, hide_index=True, use_container_width=True)
