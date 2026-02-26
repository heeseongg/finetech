import pandas as pd
import streamlit as st


def _render_big_metric(label, value):
    st.markdown(
        f"""
        <div style=\"border:1px solid #e5e7eb;border-radius:12px;padding:0.85rem 0.95rem;background:#ffffff;\">
            <div style=\"font-size:1.05rem;color:#6b7280;font-weight:600;line-height:1.2;\">{label}</div>
            <div style=\"font-size:2.05rem;color:inherit;font-family:'Source Sans Pro',sans-serif;font-weight:400;line-height:1.25;margin-top:0.25rem;\">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _fmt_ratio(value, digits=2):
    if value is None or pd.isna(value):
        return "N/A"
    try:
        return f"{float(value):,.{digits}f}"
    except Exception:
        return "N/A"


def _fmt_number(value):
    if value is None or pd.isna(value):
        return "N/A"
    try:
        return f"{float(value):,.0f}"
    except Exception:
        return "N/A"


def render_valuation_tab(
    stock_name,
    stock_code,
    kis_app_key,
    kis_app_secret,
    kis_env,
    get_kis_valuation_metrics,
):
    st.subheader(f"📊 {stock_name} 밸류 지표 (KIS)")

    try:
        metrics = get_kis_valuation_metrics(stock_code, kis_app_key, kis_app_secret, kis_env)
    except Exception as e:
        st.error(f"KIS 밸류 지표 조회 중 오류가 발생했습니다: {type(e).__name__} - {e}")
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        _render_big_metric("PER", _fmt_ratio(metrics.get("per")))
    with col2:
        _render_big_metric("PBR", _fmt_ratio(metrics.get("pbr")))
    with col3:
        _render_big_metric("EV/EBITDA", _fmt_ratio(metrics.get("ev_ebitda")))

    asof = str(metrics.get("asof", "")).strip()
    ratio_period = str(metrics.get("ratio_period", "")).strip()
    if asof:
        st.caption(f"가격 지표 기준일: {asof}")
    if ratio_period:
        st.caption(f"기타 주요비율 기준연월: {ratio_period}")

    if metrics.get("ev_ebitda") is None:
        st.info("EV/EBITDA는 KIS 응답에서 미제공될 수 있습니다(ETF 포함 일부 종목).")

    st.caption("PER/PBR: KIS 현재가 조회, EV/EBITDA: KIS 기타 주요비율")

    st.markdown("#### 📏 지표 산출식")
    st.markdown(
        """
        - PER (주가 / 주당순이익) : 이익 대비 주가 수준
        - PBR (주가 / 주당순자산) : 순자산 대비 주가 수준
        - EV/EBITDA (기업가치 / EBITDA): 영업현금창출력 대비 기업가치 수준
        - EBITDA (영업이익 + 감가상각비 + 무형자산상각비) : 현금창출력에 가까운 이익지표
        """
    )
