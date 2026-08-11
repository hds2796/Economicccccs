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
    
    # 1. 인덱스가 종목코드 성격일 경우에만 컬럼으로 추출 (무조건 reset_index 시 'index' 중복 생성 방지)
    if df_krx.index.name in ['Code', 'Symbol', '종목코드']:
        df_krx = df_krx.reset_index()
    if df_desc.index.name in ['Code', 'Symbol', '종목코드']:
        df_desc = df_desc.reset_index()
        
    # 2. 중복 이름 생성을 방지하는 안전한 컬럼명 표준화 함수
    def standardize_df(df, target_mappings):
        for target_col, alt_names in target_mappings.items():
            if target_col not in df.columns:
                for alt in alt_names:
                    if alt in df.columns:
                        df = df.rename(columns={alt: target_col})
                        break # 첫 번째 일치 항목만 변경 후 종료하여 중복 방지
        return df
        
    df_krx = standardize_df(df_krx, {
        'Code': ['종목코드', 'Symbol', 'code'],
        'Name': ['종목명', 'name'],
        'Marcap': ['시가총액', 'marcap']
    })
    
    df_desc = standardize_df(df_desc, {
        'Code': ['종목코드', 'Symbol', 'code'],
        'Sector': ['업종', '업종명', 'Industry', 'sector']
    })
    
    # 3. 만약의 경우를 대비한 중복 컬럼 물리적 제거
    df_krx = df_krx.loc[:, ~df_krx.columns.duplicated()]
    df_desc = df_desc.loc[:, ~df_desc.columns.duplicated()]
    
    # 4. 필수 컬럼 존재 여부 최종 검증
    if 'Code' not in df_krx.columns or 'Code' not in df_desc.columns or 'Sector' not in df_desc.columns:
        st.error("오류: KRX 서버의 데이터 제공 구조가 완전히 파괴되었습니다. FinanceDataReader 라이브러리 업데이트가 필요합니다.")
        st.stop()
        
    # 5. 타입 강제 통일 후 병합
    df_krx['Code'] = df_krx['Code'].astype(str)
    df_desc['Code'] = df_desc['Code'].astype(str)
    
    df = pd.merge(df_krx[['Code', 'Name', 'Marcap']], df_desc[['Code', 'Sector']], on='Code', how='inner')
    df = df.dropna(subset=['Sector'])
    df = df[df['Sector'] != '']
    
    # 6. 정렬 및 반환
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
                
            eq_cond = report['account_nm'].str.contains('자본총계|지배.*자본|지배.*지분', regex=True)
            ni_cond = report['account_nm'].str.contains('당기순이익|지배.*순이익', regex=True)
            
            equity_row = report.loc[eq_cond & (report['fs_div'] == 'CFS')]
            if equity_row.empty: equity_row = report.loc[eq_cond & (report['fs_div'] == 'OFS')]
                
            net_income_row = report.loc[ni_cond & (report['fs_div'] == 'CFS')]
            if net_income_row.empty: net_income_row = report.loc[ni_cond & (report['fs_div'] == 'OFS')]

            if not equity_row.empty and not net_income_row.empty:
                equity = float(equity_row.iloc[0]['thstrm_amount'].replace(',', ''))
                net_income = float(net_income_row.iloc[0]['thstrm_amount'].replace(',', ''))
                
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
    
    start_date = f"{start_year}-01-01"
    end_date = f"{end_year+1}-12-31"
    try:
        df_price = fdr.DataReader(ticker, start_date, end_date)
    except Exception:
        return None
        
    if df_price.empty: return None

    try:
        krx_list = fdr.StockListing('KRX')
        shares = krx_list.loc[krx_list['Code'] == ticker, 'Stocks'].values[0]
    except Exception:
        shares = 100000000

    df_price['MarketCap'] = df_price['Close'] * shares
    df_market = df_price[['Close', 'MarketCap']].copy()
    df_market.index = pd.to_datetime(df_market.index).tz_localize(None)
    
    df = df_market.join(df_fs, how='outer')
    df['equity'] = df['equity'].ffill()
    df['net_income'] = df['net_income'].ffill()
    df = df.dropna(subset=['Close', 'equity'])
    
    if df.empty: return None
    
    df['PBR'] = df['MarketCap'] / df['equity']
    df['PER'] = df['MarketCap'] / df['net_income']
    df['PER'] = np.where(df['PER'] < 0, np.nan, df['PER']) 
    df['Daily_Return'] = df['Close'].pct_change().shift(-1)
    
    return df

# UI 구성 및 실행
st.sidebar.header("⚙️ 백테스트 조건 설정")

df_sectors, sector_list = get_dynamic_sectors()
selected_sector = st.sidebar.selectbox("분석 대상 섹터 (한국표준산업분류)", sector_list)

sampling_method = st.sidebar.radio("종목 추출 방식", ["대형주 집중 (시총 상위순)", "분산 추출 (시총 상/중/하위 균등)"], help="섹터 전반의 왜곡 없는 파라미터를 얻으려면 '분산 추출'을 권장합니다.")
top_n = st.sidebar.slider("분석할 종목 수", 3, 50, 15, help="종목 수가 많을수록 결과가 정확해지지만 DART 통신으로 인해 분석 시간이 증가합니다.")

sector_stocks_all = df_sectors[df_sectors['Sector'] == selected_sector]

if sampling_method == "대형주 집중 (시총 상위순)":
    sector_stocks = sector_stocks_all.head(top_n)
else:
    total_in_sector = len(sector_stocks_all)
    if total_in_sector <= top_n:
        sector_stocks = sector_stocks_all
    else:
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
    pbr_range = np.arange(0.4, 1.6, 0.1) 
    
    for idx, s_info in enumerate(target_stocks):
        ticker, name = s_info["ticker"], s_info["name"]
        st.write(f"🔄 ({idx+1}/{len(target_stocks)}) [{name}] 데이터 로드 및 연산 중...")
        
        df_stock = process_single_stock_data(ticker, start_year, end_year)
        if df_stock is None or df_stock.empty: 
            st.warning(f"⚠️ {name} 데이터를 구성할 수 없어 건너뜁니다.")
            continue
            
        df_stock['Cumulative_Market'] = (1 + df_stock['Daily_Return']).cumprod()
        market_return = (df_stock['Cumulative_Market'].iloc[-2] - 1) * 100
        
        for target_pbr in pbr_range:
            target_pbr = round(target_pbr, 2)
            df_test = df_stock.copy()
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
    
    sector_summary = df_res.groupby("target_pbr").agg(
        평균_전략수익률=("strategy_return", "mean"), 
        평균_시장수익률=("market_return", "mean")
    ).reset_index()
    
    sector_summary = sector_summary.sort_values(by="평균_전략수익률", ascending=False).reset_index(drop=True)
    best_row = sector_summary.iloc[0]
    best_pbr = float(best_row["target_pbr"])
    best_return = float(best_row["평균_전략수익률"])
    
    conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS quant_parameters (sector_name TEXT PRIMARY KEY, best_pbr REAL, best_return REAL, updated_at TEXT)''')
    c.execute('''INSERT OR REPLACE INTO quant_parameters (sector_name, best_pbr, best_return, updated_at) VALUES (?, ?, ?, datetime('now', 'localtime'))''', (selected_sector, best_pbr, best_return))
    conn.commit()
    conn.close()
    
    col1, col2 = st.columns(2)
    col1.metric("🔥 산출된 섹터 최적 PBR 진입점", f"PBR {best_pbr:.1f} 이하")
    col2.metric("섹터 평균 전략 수익률", f"{best_return:+.2f}%")
    
    st.success(f"✅ 분석 완료! 최적값(PBR {best_pbr:.1f})이 메인 DB에 성공적으로 저장되었습니다.")
    
    st.markdown("#### 📊 PBR 조건별 수익률 분포")
    st.dataframe(
        sector_summary.style.format({"target_pbr": "{:.1f}", "평균_전략수익률": "{:+.2f}%", "평균_시장수익률": "{:+.2f}%"}),
        use_container_width=True
    )
