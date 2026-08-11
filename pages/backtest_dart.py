import streamlit as st
import OpenDartReader
import FinanceDataReader as fdr
import pandas as pd
import numpy as np
import time
import sqlite3

st.set_page_config(page_title="퀀트 백테스트 연구소", page_icon="🧪", layout="wide")
st.title("🧪 섹터별 퀀트 파라미터 자동 최적화")
st.caption("안정적인 FinanceDataReader와 DART 데이터를 결합하여 최적의 PBR 타점을 찾고 DB에 연동합니다.")

# DART API KEY 설정
try:
    DART_API_KEY = st.secrets["DART_API_KEY"]
except KeyError:
    st.error("⚠️ secrets 설정에 DART_API_KEY가 없습니다.")
    st.stop()

dart = OpenDartReader(DART_API_KEY)

@st.cache_data(ttl=86400)
def get_dynamic_sectors():
    """KRX 전체 종목의 섹터 정보와 시가총액을 동적으로 불러옵니다."""
    df_krx = fdr.StockListing('KRX')
    df_desc = fdr.StockListing('KRX-DESC')
    
    # 외부 데이터 소스(KRX)의 컬럼명 변경 대응 (한글화 또는 명칭 변경 시 영문으로 정규화)
    df_krx = df_krx.rename(columns={'종목코드': 'Code', '종목명': 'Name', '시가총액': 'Marcap'})
    df_desc = df_desc.rename(columns={'종목코드': 'Symbol', '업종': 'Sector', '업종명': 'Sector', 'Industry': 'Sector'})
    
    # 필수 컬럼 방어 로직
    if 'Sector' not in df_desc.columns:
        st.error("오류: KRX 데이터 구조가 변경되어 섹터 정보를 식별할 수 없습니다.")
        st.stop()
        
    # 종목코드 기준으로 시가총액과 섹터 데이터 병합
    df = pd.merge(df_krx[['Code', 'Name', 'Marcap']], df_desc[['Symbol', 'Sector']], left_on='Code', right_on='Symbol', how='inner')
    df = df.dropna(subset=['Sector'])
    df = df[df['Sector'] != '']
    
    # 섹터별로 시가총액 내림차순 정렬
    df = df.sort_values(by=['Sector', 'Marcap'], ascending=[True, False])
    return df, sorted(df['Sector'].unique().tolist())

@st.cache_data(ttl=3600)
def get_historical_financials(corp_code, start_year, end_year):
    """DART에서 과거 자본총계 및 당기순이익 추출 (계정과목 정규식 및 미래참조 차단 적용)"""
    fs_data = []
    for year in range(start_year, end_year + 1):
        try:
            report = dart.finstate(corp_code, year, reprt_code='11011')
            if report is None or report.empty: continue
                
            # 계정과목 파편화 해결: 금융주 및 지주사의 다양한 명칭을 정규식으로 포괄 검색
            eq_cond = report['account_nm'].str.contains('자본총계|지배.*자본|지배.*지분', regex=True)
            ni_cond = report['account_nm'].str.contains('당기순이익|지배.*순이익', regex=True)
            
            equity_row = report.loc[eq_cond & (report['fs_div'] == 'CFS')]
            if equity_row.empty: equity_row = report.loc[eq_cond & (report['fs_div'] == 'OFS')]
                
            net_income_row = report.loc[ni_cond & (report['fs_div'] == 'CFS')]
            if net_income_row.empty: net_income_row = report.loc[ni_cond & (report['fs_div'] == 'OFS')]

            if not equity_row.empty and not net_income_row.empty:
                equity = float(equity_row.iloc[0]['thstrm_amount'].replace(',', ''))
                net_income = float(net_income_row.iloc[0]['thstrm_amount'].replace(',', ''))
                
                # 미래참조 편향 제거: 사업보고서 제출 지연까지 고려하여 익년 4월 30일로 적용
                fs_data.append({'report_date': f"{year+1}-04-30", 'equity': equity, 'net_income': net_income})
            time.sleep(0.4) 
        except Exception:
            pass
            
    df_fs = pd.DataFrame(fs_data)
    if not df_fs.empty:
        df_fs['report_date'] = pd.to_datetime(df_fs['report_date'])
        df_fs.set_index('report_date', inplace=True)
    return df_fs

def process_single_stock_data(ticker, start_year, end_year):
    """FinanceDataReader와 DART 데이터를 병합하여 백테스트용 시계열 생성"""
    df_fs = get_historical_financials(ticker, start_year, end_year)
    if df_fs.empty: return None
    
    # FDR 주가 데이터 호출 (수정주가 자동 반영됨)
    start_date = f"{start_year}-01-01"
    end_date = f"{end_year+1}-12-31"
    try:
        df_price = fdr.DataReader(ticker, start_date, end_date)
    except Exception:
        return None
        
    if df_price.empty: return None

    # KRX 상장주식수 조회하여 시가총액 계산
    try:
        krx_list = fdr.StockListing('KRX')
        shares = krx_list.loc[krx_list['Code'] == ticker, 'Stocks'].values[0]
    except Exception:
        shares = 100000000

    df_price['MarketCap'] = df_price['Close'] * shares
    df_market = df_price[['Close', 'MarketCap']].copy()
    df_market.index = pd.to_datetime(df_market.index).tz_localize(None)
    
    # 가격 데이터와 재무 데이터 병합
    df = df_market.join(df_fs, how='outer')
    df['equity'] = df['equity'].ffill()
    df['net_income'] = df['net_income'].ffill()
    df = df.dropna(subset=['Close', 'equity'])
    
    if df.empty: return None
    
    # PBR, PER 및 일일 수익률 계산
    df['PBR'] = df['MarketCap'] / df['equity']
    df['PER'] = df['MarketCap'] / df['net_income']
    df['PER'] = np.where(df['PER'] < 0, np.nan, df['PER']) # 적자는 결측처리
    df['Daily_Return'] = df['Close'].pct_change().shift(-1)
    
    return df

# UI 구성 및 실행
st.sidebar.header("⚙️ 백테스트 조건 설정")

# 동적 섹터 데이터 로드
df_sectors, sector_list = get_dynamic_sectors()
selected_sector = st.sidebar.selectbox("분석 대상 섹터 (한국표준산업분류)", sector_list)

# 샘플링 방식 및 종목 수 설정
sampling_method = st.sidebar.radio("종목 추출 방식", ["대형주 집중 (시총 상위순)", "분산 추출 (시총 상/중/하위 균등)"], help="섹터 전반의 왜곡 없는 파라미터를 얻으려면 '분산 추출'을 권장합니다.")
top_n = st.sidebar.slider("분석할 종목 수", 3, 50, 15, help="종목 수가 많을수록 결과가 정확해지지만 DART 통신으로 인해 분석 시간이 증가합니다.")

sector_stocks_all = df_sectors[df_sectors['Sector'] == selected_sector]

# 추출 로직
if sampling_method == "대형주 집중 (시총 상위순)":
    sector_stocks = sector_stocks_all.head(top_n)
else:
    # 균등 추출 (Stratified Sampling)
    total_in_sector = len(sector_stocks_all)
    if total_in_sector <= top_n:
        sector_stocks = sector_stocks_all
    else:
        # np.linspace를 사용하여 시가총액 분포에서 고르게 인덱스 추출
        indices = np.linspace(0, total_in_sector - 1, top_n, dtype=int)
        sector_stocks = sector_stocks_all.iloc[indices]

target_stocks = [{"ticker": row['Code'], "name": row['Name']} for _, row in sector_stocks.iterrows()]

with st.sidebar.expander(f"추출된 분석 대상 종목 ({len(target_stocks)}개)"):
    st.markdown(", ".join([s['name'] for s in target_stocks]))

start_year = st.sidebar.slider("백테스트 시작 연도", 2018, 2023, 2020)
end_year = 2023

if st.sidebar.button("🚀 백테스트 실행 및 DB 업데이트", type="primary", use_container_width=True):
    st.subheader(f"📊 [{selected_sector}] 최적화 분석 진행 중 (총 {len(target_stocks)}종목)")
    progress_bar = st.progress(0)
    
    sector_results = []
    pbr_range = np.arange(0.4, 1.6, 0.1) # 탐색할 PBR 범위
    
    for idx, s_info in enumerate(target_stocks):
        ticker, name = s_info["ticker"], s_info["name"]
        st.write(f"🔄 ({idx+1}/{len(target_stocks)}) [{name}] 데이터 로드 및 연산 중...")
        
        df_stock = process_single_stock_data(ticker, start_year, end_year)
        if df_stock is None or df_stock.empty: 
            st.warning(f"⚠️ {name} 데이터를 구성할 수 없어 건너뜁니다.")
            continue
            
        # 단순 보유 수익률 (Benchmark)
        df_stock['Cumulative_Market'] = (1 + df_stock['Daily_Return']).cumprod()
        market_return = (df_stock['Cumulative_Market'].iloc[-2] - 1) * 100
        
        # PBR 임계값별 수익률 전수 조사
        for target_pbr in pbr_range:
            target_pbr = round(target_pbr, 2)
            df_test = df_stock.copy()
            # 조건식: PBR이 타겟 이하이고, 흑자(PER>0)일 때 매수 유지
            df_test['Signal'] = np.where((df_test['PBR'] < target_pbr) & (df_test['PER'] > 0), 1, 0)
            df_test['Strategy_Return'] = df_test['Signal'] * df_test['Daily_Return']
            df_test['Cumulative_Strategy'] = (1 + df_test['Strategy_Return']).cumprod()
            
            strategy_return = (df_test['Cumulative_Strategy'].iloc[-2] - 1) * 100
            sector_results.append({
                "name": name, 
                "target_pbr": target_pbr, 
                "market_return": market_return, 
                "strategy_return": strategy_return
            })
            
        progress_bar.progress((idx + 1) / len(target_stocks))

    if not sector_results:
        st.error("유효한 백테스트 결과를 도출하지 못했습니다.")
        st.stop()
        
    df_res = pd.DataFrame(sector_results)
    
    # 섹터별 PBR 타겟 평균 성과 집계
    sector_summary = df_res.groupby("target_pbr").agg(
        평균_전략수익률=("strategy_return", "mean"), 
        평균_시장수익률=("market_return", "mean")
    ).reset_index()
    
    # 수익률이 가장 높은 PBR 도출
    sector_summary = sector_summary.sort_values(by="평균_전략수익률", ascending=False).reset_index(drop=True)
    best_row = sector_summary.iloc[0]
    best_pbr = float(best_row["target_pbr"])
    best_return = float(best_row["평균_전략수익률"])
    
    # SQLite DB에 최적 파라미터 자동 저장
    conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS quant_parameters (sector_name TEXT PRIMARY KEY, best_pbr REAL, best_return REAL, updated_at TEXT)''')
    c.execute('''INSERT OR REPLACE INTO quant_parameters (sector_name, best_pbr, best_return, updated_at) VALUES (?, ?, ?, datetime('now', 'localtime'))''', (selected_sector, best_pbr, best_return))
    conn.commit()
    conn.close()
    
    # 결과 출력
    col1, col2 = st.columns(2)
    col1.metric("🔥 산출된 섹터 최적 PBR 진입점", f"PBR {best_pbr:.1f} 이하")
    col2.metric("섹터 평균 전략 수익률", f"{best_return:+.2f}%")
    
    st.success(f"✅ 분석 완료! 최적값(PBR {best_pbr:.1f})이 메인 DB에 성공적으로 저장되었습니다.")
    
    st.markdown("#### 📊 PBR 조건별 수익률 분포")
    st.dataframe(
        sector_summary.style.format({"target_pbr": "{:.1f}", "평균_전략수익률": "{:+.2f}%", "평균_시장수익률": "{:+.2f}%"}),
        use_container_width=True
    )
