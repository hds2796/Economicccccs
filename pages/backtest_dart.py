import streamlit as st
import OpenDartReader
from pykrx import stock
import pandas as pd
import numpy as np
import time

st.title("🧪 퀀트 파라미터 자동 최적화 연구소 (Grid Search)")
st.caption("PBR 임계값을 0.5부터 1.5까지 자동으로 테스트하여 최적의 승률 구간을 발굴합니다.")

try:
    DART_API_KEY = st.secrets["DART_API_KEY"]
except KeyError:
    st.error("⚠️ secrets 설정에 DART_API_KEY가 없습니다.")
    st.stop()

dart = OpenDartReader(DART_API_KEY)

@st.cache_data(ttl=3600)
def get_historical_financials(corp_code, start_year, end_year):
    fs_data = []
    for year in range(start_year, end_year + 1):
        try:
            report = dart.finstate(corp_code, year, reprt_code='11011')
            if report is None or report.empty: continue
                
            equity_row = report.loc[(report['account_nm'] == '자본총계') & (report['fs_div'] == 'CFS')]
            if equity_row.empty: equity_row = report.loc[(report['account_nm'] == '자본총계') & (report['fs_div'] == 'OFS')]
                
            net_income_row = report.loc[(report['account_nm'] == '당기순이익') & (report['fs_div'] == 'CFS')]
            if net_income_row.empty: net_income_row = report.loc[(report['account_nm'] == '당기순이익') & (report['fs_div'] == 'OFS')]

            if not equity_row.empty and not net_income_row.empty:
                equity = float(equity_row.iloc[0]['thstrm_amount'].replace(',', ''))
                net_income = float(net_income_row.iloc[0]['thstrm_amount'].replace(',', ''))
                
                fs_data.append({
                    'report_date': f"{year+1}-03-31", 
                    'equity': equity,
                    'net_income': net_income
                })
            time.sleep(0.5) 
        except Exception:
            pass
            
    df_fs = pd.DataFrame(fs_data)
    if not df_fs.empty:
        df_fs['report_date'] = pd.to_datetime(df_fs['report_date'])
        df_fs.set_index('report_date', inplace=True)
    return df_fs

def run_parameter_optimization(ticker, corp_name, start_year, end_year):
    with st.spinner(f"[{corp_name}] 재무 및 시가총액 데이터 병합 중..."):
        df_fs = get_historical_financials(corp_name, start_year, end_year)
        if df_fs.empty:
            st.error("재무 데이터를 불러오지 못했습니다.")
            return
            
        start_date = f"{start_year}0101"
        end_date = f"{end_year+1}1231"
        
        df_price = stock.get_market_ohlcv(start_date, end_date, ticker)
        df_cap = stock.get_market_cap(start_date, end_date, ticker)
        
        df_market = pd.concat([df_price['종가'], df_cap['시가총액']], axis=1)
        df_market.columns = ['Close', 'MarketCap']
        
        df = df_market.join(df_fs, how='outer')
        df['equity'] = df['equity'].ffill()
        df['net_income'] = df['net_income'].ffill()
        df.dropna(subset=['Close', 'equity'], inplace=True)
        
        df['PBR'] = df['MarketCap'] / df['equity']
        df['PER'] = df['MarketCap'] / df['net_income']
        df['PER'] = np.where(df['PER'] < 0, np.nan, df['PER'])

        # 시장 단순 존버 수익률
        df['Daily_Return'] = df['Close'].pct_change().shift(-1)
        df['Cumulative_Market'] = (1 + df['Daily_Return']).cumprod()
        final_market = (df['Cumulative_Market'].iloc[-2] - 1) * 100

    with st.spinner("컴퓨터가 최적의 PBR 타겟 숫자를 찾는 중입니다..."):
        optimization_results = []
        
        # PBR 0.5부터 1.5까지 0.1 단위로 10번의 백테스트를 자동 반복
        for target_pbr in np.arange(0.5, 1.6, 0.1):
            df_test = df.copy()
            df_test['Signal'] = np.where((df_test['PBR'] < target_pbr) & (df_test['PER'] > 0), 1, 0)
            df_test['Strategy_Return'] = df_test['Signal'] * df_test['Daily_Return']
            df_test['Cumulative_Strategy'] = (1 + df_test['Strategy_Return']).cumprod()
            
            final_strategy = (df_test['Cumulative_Strategy'].iloc[-2] - 1) * 100
            market_beat = final_strategy - final_market
            
            optimization_results.append({
                "PBR 진입 조건": f"PBR {target_pbr:.1f} 이하",
                "누적 수익률": round(final_strategy, 2),
                "시장 초과 수익": round(market_beat, 2)
            })
            
        result_df = pd.DataFrame(optimization_results)
        result_df = result_df.sort_values(by="누적 수익률", ascending=False).reset_index(drop=True)
        
        st.subheader(f"📊 최적화 결과 리포트: {corp_name} ({ticker})")
        st.info(f"단순 보유(Buy & Hold) 시장 수익률: **{final_market:+.2f}%**")
        
        st.dataframe(
            result_df.style.format({"누적 수익률": "{:+.2f}%", "시장 초과 수익": "{:+.2f}%"})\
                           .background_gradient(subset=['누적 수익률'], cmap='RdYlGn'),
            use_container_width=True
        )
        
        best_pbr = result_df.iloc[0]['PBR 진입 조건']
        st.success(f"💡 **AI 퀀트의 결론:** 과거 {start_year}~{end_year}년 데이터 분석 결과, {corp_name}은(는) **'{best_pbr}'** 일 때 매수하는 것이 가장 압도적인 수익을 냈습니다.")

with st.form("backtest_form"):
    st.write("백테스트할 종목과 기간을 선택하세요.")
    c1, c2, c3 = st.columns(3)
    t_ticker = c1.text_input("종목코드 (예: 005380)", value="005380")
    t_name = c2.text_input("종목명 (예: 현대차)", value="현대차")
    t_year = c3.slider("시작 연도 (최근 5년 권장)", 2018, 2023, 2020)
    
    if st.form_submit_button("최적의 매수 타점 찾기 (Run)", type="primary"):
        run_parameter_optimization(t_ticker, t_name, t_year, 2023)
