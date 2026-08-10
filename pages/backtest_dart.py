import streamlit as st
import OpenDartReader
from pykrx import stock
import pandas as pd
import numpy as np
import time

st.title("🧪 DART + pykrx 퀀트 백테스트 연구소")

# 1. 스트림릿 시크릿에서 DART 키 불러오기
try:
    DART_API_KEY = st.secrets["DART_API_KEY"]
except KeyError:
    st.error("⚠️ secrets 설정에 DART_API_KEY가 없습니다.")
    st.stop()

dart = OpenDartReader(DART_API_KEY)

def get_historical_financials(corp_code, start_year, end_year):
    """DART에서 과거 자본총계 및 당기순이익 추출"""
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

def run_backtest_with_marketcap(ticker, corp_name, start_year, end_year):
    with st.spinner(f"[{corp_name}] DART 재무 데이터 및 KRX 시가총액 수집 중..."):
        df_fs = get_historical_financials(corp_name, start_year, end_year)
        
        if df_fs.empty:
            st.error("재무 데이터를 불러오지 못했습니다.")
            return
            
        start_date = f"{start_year}0101"
        end_date = f"{end_year+1}1231"
        
        # pykrx 데이터 수집
        df_price = stock.get_market_ohlcv(start_date, end_date, ticker)
        df_cap = stock.get_market_cap(start_date, end_date, ticker)
        
        # 주가와 시가총액 결합
        df_market = pd.concat([df_price['종가'], df_cap['시가총액']], axis=1)
        df_market.columns = ['Close', 'MarketCap']
        
        # 데이터 병합 (앞방향 채우기)
        df = df_market.join(df_fs, how='outer')
        df['equity'] = df['equity'].ffill()
        df['net_income'] = df['net_income'].ffill()
        df.dropna(subset=['Close', 'equity'], inplace=True)
        
        # PBR, PER 산출
        df['PBR'] = df['MarketCap'] / df['equity']
        df['PER'] = df['MarketCap'] / df['net_income']
        df['PER'] = np.where(df['PER'] < 0, np.nan, df['PER'])

        # 백테스트 룰 세팅 (PBR 0.8 이하 매수)
        df['Signal'] = np.where((df['PBR'] < 0.8) & (df['PER'] > 0), 1, 0)
        
        df['Daily_Return'] = df['Close'].pct_change().shift(-1)
        df['Strategy_Return'] = df['Signal'] * df['Daily_Return']
        
        df['Cumulative_Market'] = (1 + df['Daily_Return']).cumprod()
        df['Cumulative_Strategy'] = (1 + df['Strategy_Return']).cumprod()

        # 결과 연산
        final_market = (df['Cumulative_Market'].iloc[-2] - 1) * 100
        final_strategy = (df['Cumulative_Strategy'].iloc[-2] - 1) * 100
        
        # 웹 화면 출력
        st.subheader(f"📊 백테스트 결과: {corp_name} ({ticker})")
        st.caption(f"테스트 기간: {start_year}년 ~ {end_year}년")
        
        col1, col2 = st.columns(2)
        col1.metric("단순 보유(Buy & Hold) 수익률", f"{final_market:+.2f}%")
        col2.metric("퀀트 전략(PBR<0.8) 수익률", f"{final_strategy:+.2f}%")
        
        st.divider()
        st.markdown("**최근 10일 산출 데이터 샘플**")
        st.dataframe(df[['Close', 'MarketCap', 'equity', 'net_income', 'PBR', 'PER', 'Signal']].tail(10))

# 실행 버튼 (웹 UI 제어용)
if st.button("현대차(005380) 백테스트 실행", type="primary"):
    run_backtest_with_marketcap("005380", "현대차", 2020, 2023)
