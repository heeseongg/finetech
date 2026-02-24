import streamlit as st


def _flow_color(val):
    try:
        num = float(val)
    except Exception:
        return ""
    if num > 0:
        return "color: #d92d20;"
    if num < 0:
        return "color: #1d4ed8;"
    return "color: #374151;"


def render_flow_tab(
    stock_name,
    stock_code,
    kis_app_key,
    kis_app_secret,
    kis_env,
    get_kis_investor_flow,
):
    st.subheader(f"💹 {stock_name} 외국인/기관 수급 (KIS)")
    days = st.slider("수급 조회 기간(거래일)", 20, 120, 60, key=f"kis_flow_days_{stock_code}")

    try:
        flow_df = get_kis_investor_flow(stock_code, kis_app_key, kis_app_secret, kis_env)
    except Exception as e:
        st.error(f"KIS 수급 조회 중 오류가 발생했습니다: {type(e).__name__} - {e}")
        return

    if flow_df.empty:
        st.info("표시할 수급 데이터가 없습니다.")
        return

    metric_targets = [c for c in ["외국인순매수", "개인순매수", "기관순매수"] if c in flow_df.columns]
    if metric_targets:
        metric_cols = st.columns(len(metric_targets))
        for idx, col in enumerate(metric_targets):
            metric_cols[idx].metric(f"최근 {col.replace('순매수', '')} 순매수", f"{flow_df.iloc[-1][col]:,.0f}")

    chart_targets = [c for c in ["외국인순매수", "개인순매수", "기관순매수"] if c in flow_df.columns]
    if "일자" in flow_df.columns and chart_targets:
        chart_df = flow_df[["일자"] + chart_targets].dropna(subset=chart_targets, how="all").tail(days).set_index("일자")
        st.line_chart(chart_df, use_container_width=True)
        flow_view = flow_df[["일자"] + chart_targets].tail(30).copy()
    else:
        st.caption("수급 컬럼명이 환경에 따라 달라 원본 응답을 함께 표시합니다.")
        flow_view = flow_df.tail(30).copy()

    if "일자" in flow_view.columns:
        flow_view["일자"] = flow_view["일자"].dt.strftime("%Y-%m-%d")

    value_cols = [c for c in ["외국인순매수", "개인순매수", "기관순매수"] if c in flow_view.columns]
    display_flow = flow_view.iloc[::-1].copy()

    def _fmt_flow_num(v):
        if v is None:
            return "-"
        try:
            if str(v).strip().lower() in {"nan", "<na>"}:
                return "-"
            return f"{float(v):,.0f}"
        except Exception:
            return "-"

    for col in value_cols:
        display_flow[col] = display_flow[col].map(_fmt_flow_num)

    def _style_flow_col(val):
        try:
            num = float(str(val).replace(",", ""))
        except Exception:
            return ""
        return _flow_color(num)

    styled_flow = display_flow.style
    if value_cols:
        styled_flow = styled_flow.map(_style_flow_col, subset=value_cols)
    st.dataframe(styled_flow, hide_index=True, use_container_width=True)
