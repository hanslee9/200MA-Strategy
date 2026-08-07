"""
200-Day Moving Average Screener
==================================

로직
----
1. 국가(한국/미국) + 후보군 크기 N을 입력받아 시가총액 상위 N개 종목 유니버스를 구성한다.
2. 각 종목의 가격 데이터를 다운로드하고 200일 이동평균선을 계산한다.
3. 종목별로 "종가 < 200일선 → 매도(현금), 종가 > 200일선 → 매수(보유)" 상태전이 백테스트를 돌린다.
   (이전 Trailing Stop-Loss 프로젝트와 동일한 상태전이 구조, 기준선만 200일 이동평균선으로 교체)
4. 백테스트 결과(CAGR, MDD, Total Return)와 현재 상태(매수/현금, 이격도)를 종목별로 계산한다.
5. "현재 매수 상태(종가 > 200일선)"인 종목만 남기고, 백테스트 CAGR 높은 순으로 정렬해 상위 K개를 출력한다.

주의: 상장 200거래일 미만이거나 가격 데이터가 결측인 종목은 자동 제외된다.
"""

import argparse
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

MA_WINDOW = 200  # 이동평균 기간(거래일)


# ------------------------------------------------------------------
# 1. 유니버스 구성 (국가별 시가총액 상위 N개) - momentum_screener와 동일 로직
# ------------------------------------------------------------------
def get_universe(country: str, n: int) -> pd.DataFrame:
    """
    country: 'KR' 또는 'US'
    반환: columns=['ticker', 'name', 'market_cap'] (시가총액 내림차순, 상위 n개)
    """
    country = country.upper()

    if country == "KR":
        # KRX(data.krx.co.kr) 직접 호출은 클라우드 IP가 차단되는 사례가 많아
        # 상대적으로 안정적인 네이버 금융 페이지를 파싱한다.
        import requests
        from bs4 import BeautifulSoup

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        rows = []
        page = 1
        max_pages = 20
        marcap_col_idx = None

        while len(rows) < n and page <= max_pages:
            url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page={page}"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")

            table = soup.select_one("table.type_2")
            if table is None:
                break

            if marcap_col_idx is None:
                header_ths = table.select("thead th") or table.select("tr th")
                for idx, th in enumerate(header_ths):
                    if "시가총액" in th.text:
                        marcap_col_idx = idx
                        break
                if marcap_col_idx is None:
                    raise ValueError("네이버 금융 페이지 구조가 변경된 것으로 보입니다 ('시가총액' 헤더를 찾지 못함).")

            trs = table.select("tr")
            page_rows_found = 0
            for tr in trs:
                link = tr.select_one("a.tltle")
                if link is None:
                    continue
                href = link.get("href", "")
                code_match = href.split("code=")
                if len(code_match) < 2:
                    continue
                code = code_match[1][:6]
                name = link.text.strip()

                tds = tr.select("td")
                try:
                    marcap = float(tds[marcap_col_idx].text.strip().replace(",", ""))
                except (ValueError, IndexError):
                    continue

                rows.append({"code": code, "name": name, "market_cap": marcap})
                page_rows_found += 1

            if page_rows_found == 0:
                break
            page += 1

        if not rows:
            raise ValueError("네이버 금융에서 시가총액 데이터를 가져오지 못했습니다.")

        df = pd.DataFrame(rows).drop_duplicates(subset=["code"])
        df = df.sort_values("market_cap", ascending=False).head(n).reset_index(drop=True)
        df["ticker"] = df["code"] + ".KS"
        return df[["ticker", "name", "market_cap"]]

    elif country == "US":
        # 네이버 해외증시 API를 직접 호출 (marketValue 기준 정렬되어 반환됨)
        import requests

        headers = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        exchanges = ["NASDAQ", "NYSE", "AMEX"]
        rows = []

        for exch in exchanges:
            page = 1
            fetched = 0
            while fetched < n and page <= 20:
                url = f"http://api.stock.naver.com/stock/exchange/{exch}/marketValue?page={page}&pageSize=60"
                try:
                    resp = requests.get(url, headers=headers, timeout=10)
                    jo = resp.json()
                except Exception:
                    break

                stocks = jo.get("stocks", [])
                if not stocks:
                    break

                for s in stocks:
                    symbol_raw = s.get("symbolCode", "")
                    symbol = symbol_raw.split(".")[0]
                    name = s.get("stockNameEng") or s.get("stockName") or symbol

                    marcap = None
                    for key in s.keys():
                        kl = key.lower()
                        if "marketvalue" in kl or ("market" in kl and "cap" in kl):
                            try:
                                marcap = float(s[key])
                            except (TypeError, ValueError):
                                pass
                            break

                    rows.append({"ticker": symbol, "name": name, "market_cap": marcap})
                    fetched += 1

                page += 1

        if not rows:
            raise ValueError("미국 종목 리스트를 가져오지 못했습니다.")

        df = pd.DataFrame(rows).drop_duplicates(subset=["ticker"])
        if df["market_cap"].notna().any():
            df = df.sort_values("market_cap", ascending=False)
        return df.head(n).reset_index(drop=True)

    else:
        raise ValueError("country는 'KR' 또는 'US'만 지원합니다.")


# ------------------------------------------------------------------
# 2. 가격 데이터 다운로드 (배치)
# ------------------------------------------------------------------
def fetch_price_matrix(tickers: list, lookback_days: int = 1095) -> pd.DataFrame:
    """
    yfinance로 여러 종목의 종가를 한 번에 배치 다운로드한다.
    200일선 계산 + 백테스트를 위해 기본 조회기간을 넉넉히(3년) 잡는다.
    """
    import yfinance as yf

    end = pd.Timestamp.today()
    start = end - pd.Timedelta(days=lookback_days)

    raw = yf.download(
        tickers, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
        progress=False, auto_adjust=True, group_by="ticker", threads=True,
    )

    if raw.empty:
        raise ValueError("가격 데이터를 가져오지 못했습니다.")

    if isinstance(raw.columns, pd.MultiIndex):
        close = pd.DataFrame({t: raw[t]["Close"] for t in tickers if t in raw.columns.get_level_values(0)})
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})

    return close


# ------------------------------------------------------------------
# 3. 종목별 200일 이평선 상태전이 백테스트
#    (이전 Trailing Stop-Loss 프로젝트와 동일한 상태전이 구조)
# ------------------------------------------------------------------
def run_ma_backtest(prices: pd.Series, ma_window: int = MA_WINDOW, initial_capital: float = 10_000_000) -> dict:
    """
    prices: 일별 종가 시계열 (충분히 긴 기간 - 최소 ma_window 이상)
    로직:
      - 이동평균선을 계산 후, 첫 유효 이평선 시점부터 시뮬레이션 시작
      - 시작 시점 종가가 이평선 위면 보유로 시작, 아래면 현금으로 시작
      - 보유 중 종가가 이평선 아래로 이탈 -> 매도(현금 전환)
      - 현금 중 종가가 이평선 위로 회복 -> 매수(보유 전환)
    반환: {'equity_curve', 'trades', 'metrics', 'current_status'}
    """
    ma = prices.rolling(ma_window).mean()
    valid = ma.dropna()
    if len(valid) < 30:
        raise ValueError("이동평균 계산에 필요한 데이터가 부족합니다.")

    start_idx = prices.index.get_loc(valid.index[0])
    sim_prices = prices.iloc[start_idx:]
    sim_ma = ma.iloc[start_idx:]

    dates = sim_prices.index
    position = 1 if sim_prices.iloc[0] > sim_ma.iloc[0] else 0
    shares = initial_capital / sim_prices.iloc[0] if position == 1 else 0.0
    cash = 0.0 if position == 1 else initial_capital

    trades = []
    equity = []

    for i in range(len(sim_prices)):
        p = sim_prices.iloc[i]
        m = sim_ma.iloc[i]
        d = dates[i]

        if position == 1 and p < m:
            cash = shares * p
            shares = 0.0
            position = 0
            trades.append({"date": d, "action": "SELL", "price": p})
        elif position == 0 and p > m:
            shares = cash / p
            cash = 0.0
            position = 1
            trades.append({"date": d, "action": "BUY", "price": p})

        equity.append(shares * p + cash)

    equity_curve = pd.Series(equity, index=dates, name="Equity")

    n_years = (dates[-1] - dates[0]).days / 365.25
    end_value = equity_curve.iloc[-1]
    total_return = end_value / initial_capital - 1
    cagr = (end_value / initial_capital) ** (1 / n_years) - 1 if n_years >= 30 / 365.25 else float("nan")
    cum_max = equity_curve.cummax()
    mdd = (equity_curve / cum_max - 1).min()

    current_price = prices.iloc[-1]
    current_ma = ma.iloc[-1]
    above_ma = bool(current_price > current_ma)
    deviation = (current_price / current_ma - 1) if current_ma > 0 else float("nan")

    return {
        "equity_curve": equity_curve,
        "trades": trades,
        "metrics": {
            "start_date": dates[0], "end_date": dates[-1],
            "end_value": end_value, "total_return": total_return,
            "cagr": cagr, "mdd": mdd, "n_trades": len(trades),
        },
        "current_status": {
            "above_ma": above_ma, "current_price": current_price,
            "current_ma": current_ma, "deviation": deviation,
        },
    }


# ------------------------------------------------------------------
# 4. 전체 파이프라인: 유니버스 전종목에 MA 백테스트 적용 후 스크리닝
# ------------------------------------------------------------------
def run_screener(country: str, n: int, k: int, lookback_days: int = 1095, min_deviation: float = -0.05):
    """
    min_deviation: 이격도(현재가/이평선 - 1) 하한선. 예: 0.0 -> 이평선 위만, -0.05 -> 이평선 대비 -5%까지 포함(돌파 임박 후보 포함)
    """
    universe = get_universe(country, n)
    print(f"[1/3] 유니버스 구성 완료: {country} 시가총액 상위 {len(universe)}개")

    price_matrix = fetch_price_matrix(universe["ticker"].tolist(), lookback_days=lookback_days)
    print(f"[2/3] 가격 데이터 다운로드 완료: {price_matrix.shape[1]}개 종목, {price_matrix.shape[0]}거래일")

    results = []
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
        row["_equity_curve"] = bt["equity_curve"]
        results.append(row)

    if not results:
        raise ValueError("백테스트 가능한 종목이 없습니다.")

    df = pd.DataFrame(results)
    df = df.merge(universe[["ticker", "name", "market_cap"]], on="ticker", how="left")

    df = df[df["deviation"] >= min_deviation]

    df = df.sort_values("cagr", ascending=False).reset_index(drop=True)
    print(f"[3/3] 스크리닝 완료: 조건 충족 {len(df)}개 종목")

    top_k = df.head(k).copy()
    return top_k, df, price_matrix


# ------------------------------------------------------------------
# 5. CLI
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="200-Day Moving Average Screener")
    parser.add_argument("--country", required=True, choices=["KR", "US"])
    parser.add_argument("--n", type=int, default=100, help="시가총액 상위 몇 개를 후보군으로 볼지 (기본 100)")
    parser.add_argument("--k", type=int, default=10, help="최종 몇 개 종목을 선별할지 (기본 10)")
    parser.add_argument("--lookback-days", type=int, default=1095, help="가격 데이터 조회 기간(일, 기본 3년)")
    parser.add_argument("--min-deviation", type=float, default=-0.05,
                         help="이격도 하한선(소수, 예: -0.05 = -5%%). 기본 -0.05(돌파 임박 종목까지 기본 포함). "
                              "이평선 위만 보려면 0.0으로 설정")
    parser.add_argument("--out", default="ma_screener_result.csv")
    args = parser.parse_args()

    top_k, full, _ = run_screener(args.country, args.n, args.k,
                                   lookback_days=args.lookback_days,
                                   min_deviation=args.min_deviation)

    print("\n" + "=" * 80)
    print(f"200일선 스크리너 결과 상위 {len(top_k)}개 ({args.country}, 후보군 {args.n}개 중)")
    print("=" * 80)
    for _, row in top_k.iterrows():
        status = "매수(이평선 위)" if row["above_ma"] else "현금(이평선 아래)"
        print(f"{row['ticker']:<10} {row['name']:<20} {status}  이격도 {row['deviation']:.1%}  "
              f"CAGR {row['cagr']:.2%}  MDD {row['mdd']:.2%}  거래횟수 {row['n_trades']}")

    full.drop(columns=["_equity_curve"]).to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"\n[저장 완료] 전체 결과 -> {args.out}")


if __name__ == "__main__":
    main()
