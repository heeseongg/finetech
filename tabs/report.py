import altair as alt
import html
import pandas as pd
import streamlit as st


def _render_news_line_ticker(label, points, tone, key_suffix):
    style_map = {
        "positive": {"icon": "😊", "color": "#d92d20", "bg": "#fff5f5"},
        "negative": {"icon": "😭", "color": "#1d4ed8", "bg": "#f5f8ff"},
    }
    tone_style = style_map.get(tone, {"icon": "•", "color": "#374151", "bg": "#f9fafb"})

    clean_points = []
    for p in points or []:
        if isinstance(p, dict):
            text = str(p.get("text", "")).strip()
            link = str(p.get("link", "")).strip()
        else:
            text = str(p).strip()
            link = ""
        if text:
            clean_points.append(
                {
                    "text": html.escape(text),
                    "link": html.escape(link, quote=True)
                    if link.startswith("http://") or link.startswith("https://")
                    else "",
                }
            )

    def _item_html(item):
        text = item.get("text", "")
        link = item.get("link", "")
        if link:
            return (
                f"<a href=\"{link}\" target=\"_blank\" rel=\"noopener noreferrer\" "
                "style=\"color:#111827;text-decoration:none;\">"
                f"{text}</a>"
            )
        return f"<span style=\"color:#111827;\">{text}</span>"

    line_height = 1.7
    row_id = f"news_ticker_{tone}_{key_suffix}"

    if not clean_points:
        empty_html = (
            f"<div style=\"padding:0.4rem 0.6rem;border-radius:8px;background:{tone_style['bg']};\">"
            f"<span style=\"font-weight:600;color:{tone_style['color']};\">{tone_style['icon']} {label}</span>"
            "<span style=\"color:#6b7280;margin-left:0.5rem;\">관련 뉴스가 부족합니다.</span>"
            "</div>"
        )
        st.markdown(
            empty_html,
            unsafe_allow_html=True,
        )
        return

    if len(clean_points) == 1:
        single_item_html = _item_html(clean_points[0])
        single_html = (
            f"<div style=\"padding:0.4rem 0.6rem;border-radius:8px;background:{tone_style['bg']};display:flex;align-items:center;gap:0.55rem;\">"
            f"<span style=\"font-weight:600;color:{tone_style['color']};min-width:42px;\">{tone_style['icon']} {label}</span>"
            f"<span style=\"white-space:nowrap;overflow:hidden;text-overflow:ellipsis;\">{single_item_html}</span>"
            "</div>"
        )
        st.markdown(
            single_html,
            unsafe_allow_html=True,
        )
        return

    loop_points = clean_points + [clean_points[0]]
    steps = len(loop_points) - 1
    hold_seconds = 3.0
    move_seconds = 1.5
    segment_seconds = hold_seconds + move_seconds
    duration = steps * segment_seconds
    items_html = "".join([f"<div class='{row_id}_item'>{_item_html(item)}</div>" for item in loop_points])

    keyframe_rows = []
    for i in range(steps):
        start_pct = (i / steps) * 100
        hold_end_pct = ((i * segment_seconds + hold_seconds) / duration) * 100
        end_pct = (((i + 1) * segment_seconds) / duration) * 100
        cur_pos = -(i * line_height)
        next_pos = -((i + 1) * line_height)
        keyframe_rows.append(f"{start_pct:.4f}% {{ transform: translateY({cur_pos}rem); }}")
        keyframe_rows.append(f"{hold_end_pct:.4f}% {{ transform: translateY({cur_pos}rem); }}")
        keyframe_rows.append(f"{end_pct:.4f}% {{ transform: translateY({next_pos}rem); }}")
    keyframes_css = "\n".join(keyframe_rows)

    ticker_html = "\n".join(
        [
            "<style>",
            f".{row_id}_box {{ padding: 0.4rem 0.6rem; border-radius: 8px; background: {tone_style['bg']}; display: flex; align-items: center; gap: 0.55rem; }}",
            f".{row_id}_label {{ min-width: 42px; font-weight: 600; color: {tone_style['color']}; }}",
            f".{row_id}_window {{ height: {line_height}rem; overflow: hidden; flex: 1; }}",
            f".{row_id}_track {{ display: flex; flex-direction: column; animation: {row_id}_up {duration}s linear infinite; }}",
            f".{row_id}_item {{ height: {line_height}rem; line-height: {line_height}rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #111827; }}",
            f".{row_id}_item a:hover {{ text-decoration: underline; }}",
            f"@keyframes {row_id}_up {{",
            keyframes_css,
            "}",
            "</style>",
            f"<div class=\"{row_id}_box\">",
            f"<span class=\"{row_id}_label\">{tone_style['icon']} {label}</span>",
            f"<div class=\"{row_id}_window\"><div class=\"{row_id}_track\">{items_html}</div></div>",
            "</div>",
        ]
    )
    st.markdown(ticker_html, unsafe_allow_html=True)


def render_report_tab(
    stock_name,
    stock_code,
    kis_app_key,
    kis_app_secret,
    kis_env,
    collect_mainnews_latest,
    get_kis_investor_flow,
    build_investment_report,
):
    st.header(f"🧾 {stock_name} 투자 분석 리포트")
    try:
        news_rows = collect_mainnews_latest(max_pages=5, max_items=80)
    except Exception as e:
        st.error(f"리포트용 뉴스 수집 중 오류가 발생했습니다: {type(e).__name__} - {e}")
        return

    flow_df = pd.DataFrame()
    if kis_app_key and kis_app_secret:
        try:
            flow_df = get_kis_investor_flow(stock_code, kis_app_key, kis_app_secret, kis_env)
        except Exception:
            flow_df = pd.DataFrame()

    report = build_investment_report(stock_name, news_rows, flow_df)

    with st.container(border=True):
        st.write(report["summary"])

    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.markdown("#### 시장 심리")
            st.markdown(f"## {report['sentiment_pct']}%")
            st.caption(report["sentiment_label"])
            st.caption(
                "뉴스 감성 점수(긍정/부정 키워드) 75% + "
                "외국인/기관 순매수 점수 25%를 합산해 0~100으로 환산"
            )
    with col2:
        with st.container(border=True):
            st.markdown("#### AI 의견")
            st.markdown(f"## {report['opinion']}")
            st.caption(f"신뢰도 {report['confidence']}%")
            st.caption(
                "시장 심리 구간값으로 라벨링  \n"
                "(매도 우위 ≤ 37% < 관망 < 63% ≤ 매수 우위)"
            )

    with st.container(border=True):
        st.markdown("#### 최근 7일간 감성 지수 추이")
        trend_df = report["trend_df"]
        if trend_df.empty:
            st.info("감성 추이를 계산할 데이터가 부족합니다.")
        else:
            chart_df = trend_df.copy()
            chart_df["일자라벨"] = chart_df["일자"].dt.strftime("%m.%d")
            chart_df["감성지수"] = pd.to_numeric(chart_df["감성지수"], errors="coerce")
            chart_df = chart_df.dropna(subset=["감성지수"])
            if chart_df.empty:
                st.info("감성 추이를 계산할 데이터가 부족합니다.")
            else:
                y_min = float(chart_df["감성지수"].min())
                y_max = float(chart_df["감성지수"].max())
                span = y_max - y_min
                target_span = max(30.0, span + 10.0)
                center = (y_min + y_max) / 2.0

                axis_base = max(0.0, center - (target_span / 2.0))
                axis_top = min(100.0, center + (target_span / 2.0))
                if (axis_top - axis_base) < target_span:
                    if axis_base <= 0.0:
                        axis_top = min(100.0, target_span)
                    elif axis_top >= 100.0:
                        axis_base = max(0.0, 100.0 - target_span)
                if axis_top <= axis_base:
                    axis_base, axis_top = 0.0, 100.0

                chart_df["기준선"] = axis_base

                color_min = float(chart_df["감성지수"].min())
                color_max = float(chart_df["감성지수"].max())

                base_chart = alt.Chart(chart_df).encode(
                    x=alt.X(
                        "일자라벨:N",
                        sort=chart_df["일자라벨"].tolist(),
                        title="일자",
                        axis=alt.Axis(labelAngle=0),
                    ),
                    y=alt.Y(
                        "감성지수:Q",
                        title="감성 지수",
                        scale=alt.Scale(domain=[axis_base, axis_top]),
                    ),
                    y2=alt.Y2("기준선:Q"),
                    tooltip=[
                        alt.Tooltip("일자라벨:N", title="일자"),
                        alt.Tooltip("감성지수:Q", title="지수", format=".1f"),
                    ],
                )

                if color_min == color_max:
                    trend_chart = (
                        base_chart.mark_bar(
                            cornerRadiusTopLeft=6,
                            cornerRadiusTopRight=6,
                            color="#5d7eea",
                        ).properties(height=280)
                    )
                else:
                    trend_chart = (
                        base_chart.mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6)
                        .encode(
                            color=alt.Color(
                                "감성지수:Q",
                                title=None,
                                legend=None,
                                scale=alt.Scale(
                                    domain=[color_min, color_max],
                                    range=["#dbe7ff", "#1f5bd8"],
                                ),
                            )
                        )
                        .properties(height=280)
                    )
                st.altair_chart(trend_chart, use_container_width=True)
                st.caption(
                    "뉴스 제목 일자별 평균 감성 점수(긍정/부정 키워드)를 "
                    "0~100 지수로 변환해 최근 7일 표시"
                )

    with st.container(border=True):
        st.markdown("#### 핵심 뉴스 요약")
        _render_news_line_ticker("긍정", report["positive_points"], "positive", stock_code)
        st.markdown("<div style='height:0.35rem;'></div>", unsafe_allow_html=True)
        _render_news_line_ticker("부정", report["negative_points"], "negative", stock_code)

        st.caption(f"분석에 사용된 뉴스 건수: {report['news_count']}건")
