"""
200-Day Moving Average Screener - Streamlit 웹앱
"""

import streamlit as st
import pandas as pd
import altair as alt

from ma_screener import get_universe, fetch_price_matrix, run_ma_backtest, MA_WINDOW

st.set_page_config(page_title="200일 이평선 스크리너", layout="wide")

st.markdown(
    "<h5 style='margin-bottom:0;'>📈 200일 이동평균선 스크리너</h5>",
    unsafe_allow_html=True,
)
st.caption("시가총액 상위 N개 종목에 200일선 매수/매도 규칙(종가가 이평선 위=매수, 아래=현금)을 각각 백테스트하고, "
           "현재 이평선 위(매수 상태)인 종목 중 백테스트 CAGR이 높은 순으로 상위 K개를 선별합니다.")

if "result" not in st.session_state:
    st.session_state.result = None

col1, col2, col3 = st.columns(3)
with col1:
    country = st.selectbox("국가", options=["KR", "US"], format_func=lambda x: "한국" if x == "KR" else "미국")
with col2:
    n = st.number_input("후보군 크기 (시가총액 상위 N개)", min_value=5, max_value=300, value=100, step=5)
with col3:
    k = st.number_input("최종 선별 종목 수 (K)", min_value=1, max_value=50, value=10, step=1)

col4, col5 = st.columns(2)
with col4:
    lookback_options = {"3년 (기본)": 1095, "5년": 1825, "10년": 3650}
    lookback_label = st.selectbox("가격 데이터 조회 기간", options=list(lookback_options.keys()),
                                   help="200일선 계산 + 백테스트에 쓰일 과거 데이터 기간입니다.")
    lookback_days = lookback_options[lookback_label]
with col5:
    only_above_ma = st.checkbox("현재 이평선 위(매수 상태) 종목만 표시", value=True,
                                 help="끄면 이평선 아래(현금 상태) 종목도 함께 표시하되, 여전히 CAGR 순으로 정렬됩니다.")

run = st.button("스크리닝 실행", type="primary")

if run:
    try:
        with st.spinner(f"{country} 시가총액 상위 {n}개 유니버스 구성 중..."):
            universe = get_universe(country, int(n))

        with st.spinner(f"가격 데이터 다운로드 중... ({lookback_label} 기준)"):
            price_matrix = fetch_price_matrix(universe["ticker"].tolist(), lookback_days=lookback_days)

        with st.spinner(f"{price_matrix.shape[1]}개 종목 200일선 백테스트 계산 중..."):
            results = []
            equity_curves = {}
            for ticker in price_matrix.columns:
                series = price_matrix[ticker].dropna()
                if len(series) < MA_WINDOW + 30:
                    continue
                try:
                    bt = run_ma_backtest(series)
                except Exception:
                    continue
                row = {"ticker": ticker}
                row.update(bt["metrics"])
                row.update(bt["current_status"])
                results.append(row)
                equity_curves[ticker] = bt["equity_curve"]

        if not results:
            raise ValueError("백테스트 가능한 종목이 없습니다.")

        df = pd.DataFrame(results)
        df = df.merge(universe[["ticker", "name", "market_cap"]], on="ticker", how="left")

        st.session_state.result = {
            "universe": universe, "price_matrix": price_matrix,
            "full": df, "equity_curves": equity_curves,
            "n_universe": len(universe),
        }

    except Exception as e:
        st.session_state.result = None
        st.error(f"오류 발생: {e}")
        st.info("네트워크 상태나 종목 수(N)를 줄여서 다시 시도해보세요.")

result = st.session_state.result

if result is None:
    st.info("옵션을 설정하고 '스크리닝 실행' 버튼을 눌러주세요.")
else:
    full = result["full"].copy()
    equity_curves = result["equity_curves"]

    st.success(f"유니버스 구성 완료: {result['n_universe']}개 종목 중 {len(full)}개 백테스트 완료")

    display_df = full.copy()
    if only_above_ma:
        display_df = display_df[display_df["above_ma"] == True]
    display_df = display_df.sort_values("cagr", ascending=False).reset_index(drop=True)
    top_k_df = display_df.head(int(k))

    st.subheader(f"🏆 200일선 스크리닝 상위 {len(top_k_df)}개")
    show = top_k_df[["ticker", "name", "above_ma", "deviation", "cagr", "mdd", "total_return", "n_trades", "market_cap"]].copy()
    show["above_ma"] = show["above_ma"].map({True: "매수(위)", False: "현금(아래)"})
    show["deviation"] = (show["deviation"] * 100).round(2)
    show["cagr"] = (show["cagr"] * 100).round(2)
    show["mdd"] = (show["mdd"] * 100).round(2)
    show["total_return"] = (show["total_return"] * 100).round(2)
    show.columns = ["티커", "종목명", "현재상태", "이격도(%)", "CAGR(%)", "MDD(%)", "총수익률(%)", "매매횟수", "시가총액"]
    st.dataframe(show, use_container_width=True, hide_index=True)

    st.subheader("📋 전체 후보군 결과")
    st.caption(f"백테스트 완료 {len(full)}개 (200거래일 미만 데이터 종목 자동 제외)")
    show_full = full.copy()
    show_full["above_ma"] = show_full["above_ma"].map({True: "매수(위)", False: "현금(아래)"})
    show_full["deviation"] = (show_full["deviation"] * 100).round(2)
    show_full["cagr"] = (show_full["cagr"] * 100).round(2)
    show_full["mdd"] = (show_full["mdd"] * 100).round(2)
    show_full["total_return"] = (show_full["total_return"] * 100).round(2)
    show_full = show_full.sort_values("cagr", ascending=False)
    show_full = show_full[["ticker", "name", "above_ma", "deviation", "cagr", "mdd", "total_return", "n_trades", "market_cap"]]
    show_full.columns = ["티커", "종목명", "현재상태", "이격도(%)", "CAGR(%)", "MDD(%)", "총수익률(%)", "매매횟수", "시가총액"]
    st.dataframe(show_full, use_container_width=True, hide_index=True)

    csv = full.to_csv(index=False).encode("utf-8-sig")
    st.download_button("결과 CSV 다운로드", csv, "ma_screener_result.csv", "text/csv")

    # --- 자산곡선 차트 (상위 K개) ---
    st.subheader("📈 상위 종목 자산곡선 (200일선 전략 백테스트)")
    name_map = dict(zip(full["ticker"], full["name"]))
    chart_df = pd.DataFrame({name_map.get(t, t): equity_curves[t] for t in top_k_df["ticker"] if t in equity_curves})

    log_scale = st.checkbox("Y축 로그 스케일", value=True)
    chart_long = chart_df.reset_index().melt(id_vars=chart_df.index.name or "index", var_name="종목", value_name="평가금액")
    chart_long = chart_long.rename(columns={chart_df.index.name or "index": "날짜"})

    y_scale = alt.Scale(type="log") if log_scale else alt.Scale(type="linear")
    legend_selection = alt.selection_point(fields=["종목"], bind="legend")
    line_chart = (
        alt.Chart(chart_long)
        .mark_line()
        .encode(
            x=alt.X("날짜:T", title="날짜"),
            y=alt.Y("평가금액:Q", scale=y_scale, title="평가금액"),
            color=alt.Color("종목:N", title="종목"),
            opacity=alt.condition(legend_selection, alt.value(1), alt.value(0.08)),
            tooltip=["날짜:T", "종목:N", alt.Tooltip("평가금액:Q", format=",.0f")],
        )
        .add_params(legend_selection)
        .interactive()
    )
    st.caption("💡 범례를 클릭하면 해당 선만 강조됩니다 (Shift+클릭으로 다중 선택)")
    st.altair_chart(line_chart, use_container_width=True)
