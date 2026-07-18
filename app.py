import streamlit as st
import json
import sqlite3
import re
import threading
import requests
import pandas as pd
import numpy as np
import urllib.request
import urllib.parse
import os
import io
import zipfile
import xml.etree.ElementTree as ET
import concurrent.futures
from datetime import datetime
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google import genai

# =======================================================
# 설정 및 모델
# =======================================================
MODEL_NAME = "gemini-3.5-flash"
LITE_MODEL_NAME = "gemini-3.1-flash-lite"
FALLBACK_MODEL_NAME = "gemini-3-flash-preview"

db_backup_lock = threading.Lock()
xml_parse_lock = threading.Lock() 
thread_local = threading.local()  

def get_session():
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session()
    return thread_local.session

st.set_page_config(page_title="Project2_Stock", page_icon="📊", layout="wide", initial_sidebar_state="expanded")

# =======================================================
# 보안 및 로그인
# =======================================================
def check_password():
    passwords_dict = st.secrets.get("USER_PASSWORDS", {})
    for key, val in st.query_params.items():
        if key in passwords_dict:
            st.session_state["password_correct"] = True
            st.session_state["user_id"] = passwords_dict[key]
            break
        if val in passwords_dict:
            st.session_state["password_correct"] = True
            st.session_state["user_id"] = passwords_dict[val]
            break

    if "password_correct" in st.session_state and st.session_state["password_correct"]:
        return True

    st.title("Project2_Stock 로그인")
    password = st.text_input("비밀번호를 입력하세요", type="password")
    if st.button("접속하기"):
        if password in passwords_dict:
            st.session_state["password_correct"] = True
            st.session_state["user_id"] = passwords_dict[password]
            st.rerun()
        else:
            st.error("비밀번호가 일치하지 않습니다.")
    return False

if not check_password(): 
    st.stop()

current_user = st.session_state["user_id"]

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
API_GATEWAY_REALTIME_URL = st.secrets.get("API_GATEWAY_REALTIME_URL", "")
NAVER_CLIENT_ID = st.secrets.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = st.secrets.get("NAVER_CLIENT_SECRET", "")
DART_API_KEY = st.secrets.get("DART_API_KEY", "")

# =======================================================
# 데이터베이스 초기화
# =======================================================
@st.cache_resource
def init_db():
    connection = sqlite3.connect('market_analysis.db', check_same_thread=False, timeout=30)
    cursor = connection.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS scrapbook (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, link TEXT, summary TEXT, analysis TEXT, scrap_date TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS portfolio (id INTEGER PRIMARY KEY AUTOINCREMENT, stock_name TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS sentiment_history (id INTEGER PRIMARY KEY AUTOINCREMENT, calc_date TEXT, score REAL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS dart_corp_codes (corp_code TEXT, corp_name TEXT, stock_code TEXT PRIMARY KEY)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS user_settings (user_id TEXT PRIMARY KEY, k_factor REAL)''')
    connection.commit()

    columns_to_add = [
        ("portfolio", "is_owned", "INTEGER DEFAULT 0"), ("portfolio", "avg_price", "REAL DEFAULT 0.0"),
        ("portfolio", "quantity", "INTEGER DEFAULT 0"), ("portfolio", "report_text", "TEXT"),
        ("portfolio", "tp_s", "REAL DEFAULT 0.0"), ("portfolio", "tp_m", "REAL DEFAULT 0.0"), ("portfolio", "tp_l", "REAL DEFAULT 0.0"), 
        ("portfolio", "bp", "REAL DEFAULT 0.0"),
        ("portfolio", "sl_s", "REAL DEFAULT 0.0"), ("portfolio", "sl_m", "REAL DEFAULT 0.0"), ("portfolio", "sl_l", "REAL DEFAULT 0.0"), 
        ("scrapbook", "stock_name", "TEXT"), ("scrapbook", "ticker", "TEXT"),
        ("scrapbook", "saved_price", "REAL DEFAULT 0.0"), 
        ("scrapbook", "target_price", "REAL DEFAULT 0.0"), ("scrapbook", "target_price_mid", "REAL DEFAULT 0.0"), ("scrapbook", "target_price_long", "REAL DEFAULT 0.0"),
        ("scrapbook", "buy_recommend_price", "REAL DEFAULT 0.0"), 
        ("scrapbook", "sl_s", "REAL DEFAULT 0.0"), ("scrapbook", "sl_m", "REAL DEFAULT 0.0"), ("scrapbook", "sl_l", "REAL DEFAULT 0.0"), 
        ("portfolio", "model_used", "TEXT"), ("portfolio", "report_time", "TEXT"), 
        ("portfolio", "ticker", "TEXT"), ("scrapbook", "model_used", "TEXT"),
        ("portfolio", "user_id", "TEXT DEFAULT 'dongsu'"), ("scrapbook", "user_id", "TEXT DEFAULT 'dongsu'")
    ]
    for table, col, dtype in columns_to_add:
        try: 
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
            connection.commit()
        except Exception:
            pass
    return connection

conn = init_db()
c = conn.cursor()

@st.cache_resource
def initialize_dart_codes():
    if not DART_API_KEY: return
    try:
        c.execute("SELECT count(*) FROM dart_corp_codes")
        if c.fetchone()[0] == 0:
            url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={DART_API_KEY}"
            res = requests.get(url, timeout=15)
            with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                with z.open('CORPCODE.xml') as f:
                    tree = ET.parse(f)
                    root = tree.getroot()
                    data = [(lst.findtext('corp_code'), lst.findtext('corp_name'), lst.findtext('stock_code').strip()) 
                            for lst in root.findall('list') if lst.findtext('stock_code') and lst.findtext('stock_code').strip()]
                    c.executemany("INSERT OR IGNORE INTO dart_corp_codes (corp_code, corp_name, stock_code) VALUES (?, ?, ?)", data)
                    conn.commit()
    except: pass

initialize_dart_codes()

# =======================================================
# 드라이브 백업 및 복구
# =======================================================
def get_drive_service_for_file():
    info = json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive'])
    return build('drive', 'v3', credentials=creds)

def backup_db_to_drive():
    with db_backup_lock:
        try:
            conn.commit()
            drive_service = get_drive_service_for_file()
            folder_id = st.secrets.get("GOOGLE_BACKUP_FOLDER_ID", "").strip()
            if not folder_id: return False
            query = f"'{folder_id}' in parents and name = 'market_analysis.db' and trashed = false"
            results = drive_service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            if files:
                media = MediaFileUpload('market_analysis.db', mimetype='application/octet-stream', resumable=True)
                drive_service.files().update(fileId=files[0]['id'], media_body=media).execute()
                return True
            return False
        except: return False

def restore_db_from_drive():
    with db_backup_lock:
        try:
            drive_service = get_drive_service_for_file()
            folder_id = st.secrets.get("GOOGLE_BACKUP_FOLDER_ID", "").strip()
            query = f"'{folder_id}' in parents and name = 'market_analysis.db' and trashed = false"
            results = drive_service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            if not files: return False
            request = drive_service.files().get_media(fileId=files[0]['id'])
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: status, done = downloader.next_chunk()
            conn.close() 
            with open('market_analysis.db', 'wb') as f: f.write(fh.getvalue())
            return True
        except: return False

# =======================================================
# 사이드바 제어
# =======================================================
with st.sidebar:
    st.markdown(f"**👤 접속 계정:** `{current_user}`")
    st.divider()
    
    c.execute("SELECT k_factor FROM user_settings WHERE user_id = ?", (current_user,))
    row = c.fetchone()
    saved_k = row[0] if row else 2.0
    
    st.subheader("⚙️ 시스템 설정")
    k_factor = st.slider("리스크 관리 계수 (k)", min_value=1.0, max_value=3.5, value=saved_k, step=0.1)
    
    if k_factor != saved_k:
        c.execute("INSERT OR REPLACE INTO user_settings (user_id, k_factor) VALUES (?, ?)", (current_user, k_factor))
        conn.commit()

    st.divider()
    st.subheader("💾 데이터베이스 관리")
    if st.button("☁️ 구글 드라이브 백업", use_container_width=True):
        with st.spinner("클라우드 백업 중..."):
            if backup_db_to_drive(): st.success("✅ DB 백업 완료")
            
    if st.button("🔄 드라이브에서 복구", use_container_width=True):
        with st.spinner("데이터 복구 중..."):
            if restore_db_from_drive():
                init_db.clear()
                st.success("✅ 복구 완료! 새로고침 진행합니다.")
                st.rerun()

# =======================================================
# AI 통신 및 파싱 (오류 캡처 및 무조건 우회 Fallback)
# =======================================================
GEMINI_CONCURRENCY_LIMIT = 3
_gemini_semaphore = threading.Semaphore(GEMINI_CONCURRENCY_LIMIT)

def call_gemini_lite_summary(prompt):
    acquired = _gemini_semaphore.acquire(timeout=25)
    if not acquired: return "API 대기 시간 초과(Lite)"
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        return client.models.generate_content(model=LITE_MODEL_NAME, contents=prompt).text
    except Exception as e: return f"요약 실패: {e}"
    finally: _gemini_semaphore.release()

def call_gemini_with_fallback(prompt, model=MODEL_NAME):
    acquired = _gemini_semaphore.acquire(timeout=25)
    if not acquired: return "API 호출 대기 시간 초과"
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        return client.models.generate_content(model=model, contents=prompt).text
    except Exception as e1:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            fallback_res = client.models.generate_content(model=FALLBACK_MODEL_NAME, contents=prompt).text
            return f"[⚠️ 우회 안내] {model} 에러(원인: {str(e1)})로 인해 {FALLBACK_MODEL_NAME} 모델로 대체함:\n\n{fallback_res}"
        except Exception as e2:
            return f"최종 실패. 1차에러: {e1} | 2차에러: {e2}"
    finally:
        _gemini_semaphore.release()

def call_gemini_stream_with_fallback(prompt):
    acquired = _gemini_semaphore.acquire(timeout=25)
    if not acquired: 
        yield "API 호출 대기 시간 초과"
        return
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        try:
            response = client.models.generate_content_stream(model=MODEL_NAME, contents=prompt)
            for chunk in response:
                if chunk.text: yield chunk.text
        except Exception as e1:
            try:
                yield f"\n\n---\n⚠️ **[안내] 서버 장애로 인해 보조 모델로 우회하여 분석을 재개합니다.**\n*(오류 원인: {str(e1)}*)\n---\n\n"
                fallback_response = client.models.generate_content_stream(model=FALLBACK_MODEL_NAME, contents=prompt)
                for chunk in fallback_response:
                    if chunk.text: yield chunk.text
            except Exception as e2: 
                yield f"\n\n❌ [최종 호출 실패] 원인: {str(e2)}"
    finally: _gemini_semaphore.release()

# =======================================================
# 데이터 가공 및 팩트 추출 유틸
# =======================================================
@st.cache_data(ttl=600)
def get_dart_filings(stock_code):
    if not DART_API_KEY: return "DART API 키 없음"
    try:
        c.execute("SELECT corp_code FROM dart_corp_codes WHERE stock_code = ?", (stock_code,))
        row = c.fetchone()
        if not row: return "DART 매핑 데이터 없음"
        
        bgn_de = (datetime.now() - pd.Timedelta(days=90)).strftime("%Y%m%d")
        url = f"https://opendart.fss.or.kr/api/list.json?crtfc_key={DART_API_KEY}&corp_code={row[0]}&bgn_de={bgn_de}&page_count=5"
        session = get_session()
        res = session.get(url, timeout=5).json()
        if res.get("status") == "000": return "\n".join([f"- [{i['rcept_dt']}] {i['report_nm']}" for i in res.get("list", [])])
        return "최근 3개월 주요 공시 없음"
    except: return "DART 조회 실패"

@st.cache_data(ttl=600)
def get_advanced_fundamental_data(code):
    data = {"per": "-", "pbr": "-", "eps": 0, "bps": 0, "industry_per": "-", "quarter_trend": "정보 없음", "supply_demand": "정보 없음", "eps_history": [], "roe_history": []}
    headers = {'User-Agent': 'Mozilla/5.0'}
    session = get_session()
    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        res = session.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        
        per_elem = soup.find(id="_per")
        if per_elem: data["per"] = per_elem.get_text().strip()
            
        eps_elem = soup.find(id="_eps")
        if eps_elem:
            try:
                val = eps_elem.get_text().strip().replace(',', '')
                if val and val.replace('.', '', 1).replace('-', '', 1).isdigit():
                    data["eps"] = float(val)
            except: pass

        bps_elem = soup.find(id="_bps")
        if bps_elem:
            try:
                val = bps_elem.get_text().strip().replace(',', '')
                if val and val.replace('.', '', 1).replace('-', '', 1).isdigit():
                    data["bps"] = float(val)
            except: pass
        
        for th in soup.find_all("th"):
            if "동일업종 PER" in th.get_text():
                td = th.find_next("td")
                if td: data["industry_per"] = td.get_text().strip().replace('배', '')
        
        cop_table = soup.find("div", class_="cop_details")
        if cop_table:
            data["quarter_trend"] = "최근 실적 수집 완료"
            try:
                for th_item in cop_table.find_all("th"):
                    text = th_item.get_text().strip()
                    if "EPS(원)" in text:
                        valid_eps = [float(v) for td in th_item.find_next_siblings("td") if (v := td.get_text().strip().replace(',', '')) and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        if valid_eps:
                            if data["eps"] == 0: data["eps"] = valid_eps[-1]
                            data["eps_history"] = valid_eps[-3:]
                    if "BPS(원)" in text:
                        valid_bps = [float(v) for td in th_item.find_next_siblings("td") if (v := td.get_text().strip().replace(',', '')) and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        if valid_bps and data["bps"] == 0: data["bps"] = valid_bps[-1]
                    if "ROE" in text:
                        valid_roe = [float(v) for td in th_item.find_next_siblings("td") if (v := td.get_text().strip().replace(',', '')) and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        if valid_roe: data["roe_history"] = valid_roe[-3:]
            except: pass
            
        url_frgn = f"https://finance.naver.com/item/frgn.naver?code={code}"
        res_frgn = session.get(url_frgn, headers=headers, timeout=5)
        soup_frgn = BeautifulSoup(res_frgn.text, "html.parser")
        
        inst_sum, fore_sum, count = 0, 0, 0
        for tr in soup_frgn.find_all("tr", {"onmouseover": "mouseOver(this)"}):
            if count >= 5: break
            tds = tr.find_all("td")
            if len(tds) >= 7:
                try:
                    inst_sum += int(tds[5].get_text().strip().replace(',', '') or 0)
                    fore_sum += int(tds[6].get_text().strip().replace(',', '') or 0)
                    count += 1
                except: pass
        data["supply_demand"] = f"최근 {count}일 누적 -> 기관: {inst_sum:+,}주 / 외국인: {fore_sum:+,}주" if count > 0 else "수급 데이터 수집 불가"
    except: pass
    return data

@st.cache_data(ttl=600)
def get_technical_data(code):
    try:
        url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=250&requestType=0"
        session = get_session()
        res = session.get(url, timeout=5)
        with xml_parse_lock:
            root = ET.fromstring(res.text)
            items = root.findall('.//item')
        if not items: return None
        df_data = [float(item.attrib['data'].split('|')[4]) for item in items]
        if len(df_data) < 60: return None
        df = pd.Series(df_data)
        macd = df.ewm(span=12, adjust=False).mean() - df.ewm(span=26, adjust=False).mean()
        signal = macd.ewm(span=9, adjust=False).mean()
        returns = df.pct_change().dropna()
        daily_volatility = returns.iloc[-20:].std() if len(returns) >= 20 else 0.0

        return {"current": df_data[-1], "high_52": max(df_data), "low_52": min(df_data), "ma20": sum(df_data[-20:])/20, "ma60": sum(df_data[-60:])/60, "macd": macd.iloc[-1], "signal": signal.iloc[-1], "daily_volatility": daily_volatility}
    except: return None

# =======================================================
# 탭 4, 5 공통: 종목 분석 퀀트 코어 로직
# =======================================================
def process_single_ticker(ticker, investment_horizon, user_k, is_discovery_mode=False):
    ticker = re.sub(r'[^\d]', '', ticker)
    if len(ticker) != 6: return ""
    
    session = get_session()
    try:
        res = session.get(f"https://m.stock.naver.com/api/stock/{ticker}/basic", timeout=3).json()
        name = res.get("stockName", ticker)
    except: name = ticker
    
    tech = get_technical_data(ticker)
    fund = get_advanced_fundamental_data(ticker)
    dart_info = get_dart_filings(ticker)
    news_raw = fetch_stock_news(name, display=4)
    lite_summary = call_gemini_lite_summary(f"뉴스/공시 요약:\n{dart_info}\n{chr(10).join([n['title'] for n in news_raw])}")
    
    current_price = tech['current'] if tech else 0.0
    daily_vol = tech['daily_volatility'] if tech else 0.0
    eps_val = fund.get('eps', 0.0)
    bps_val = fund.get('bps', 0.0)
    roe_history = fund.get('roe_history', [])
    eps_history = fund.get('eps_history', [])
    
    try: float_ind_per = float(fund['industry_per'].replace(',', '')) if fund['industry_per'] != '-' else 0.0
    except: float_ind_per = 0.0

    if bps_val > 0 and current_price > 0: fund['pbr'] = f"{current_price / bps_val:.2f}"
    else: fund['pbr'] = "-"

    eps_growth = 0.0
    if len(eps_history) >= 2 and eps_history[0] != 0:
        eps_growth = (eps_history[-1] - eps_history[0]) / abs(eps_history[0])
        eps_growth = min(max(eps_growth, -0.5), 1.0)

    bps_discount = 1.0
    if len(eps_history) >= 2 and eps_history[-1] < 0 and eps_history[0] < 0 and eps_history[-1] < eps_history[0]:
        bps_discount = 0.8
        
    conservative_bps = (bps_val * bps_discount) if bps_val > 0 else current_price * 0.8

    # 손절가 캡핑 적용
    sl_s = current_price * (1 - min(user_k * daily_vol * np.sqrt(20), 0.15)) if daily_vol > 0 else current_price * 0.95
    sl_m = current_price * (1 - min(user_k * daily_vol * np.sqrt(60), 0.30)) if daily_vol > 0 else current_price * 0.90
    sl_l = current_price * (1 - min(user_k * daily_vol * np.sqrt(250), 0.50)) if daily_vol > 0 else current_price * 0.80

    # 목표가 산출 및 모멘텀 상한선(+25%) 제한
    tp_s = current_price * min(1 + user_k * daily_vol * np.sqrt(20), 1.25) if daily_vol > 0 else current_price * 1.05

    if eps_val <= 0:
        tp_m = current_price * min(1 + user_k * daily_vol * np.sqrt(60), 1.40) if daily_vol > 0 else current_price * 1.10
        tp_l = current_price * min(1 + user_k * daily_vol * np.sqrt(250), 1.60) if daily_vol > 0 else current_price * 1.15
        fund_type = "적자 대용치 (기술적 밴드)"
    else:
        adjusted_ind_per = float_ind_per * (1 + eps_growth) if float_ind_per > 0 else 0.0
        tp_m = eps_val * adjusted_ind_per if adjusted_ind_per > 0 else eps_val * 10
        required_return = 0.08
        expected_roe = (roe_history[-1] / 100) if roe_history else 0.05
        tp_l = bps_val + (bps_val * (expected_roe - required_return) / required_return) if bps_val > 0 else current_price * 1.1
        fund_type = "기본 펀더멘털"

    if is_discovery_mode:
        if eps_val <= 0:
            if current_price > 0 and conservative_bps < current_price: return ""
        else:
            if current_price > 0 and (tp_m < current_price or tp_l < current_price): return "" 

    flag_m = "정상"
    flag_l = "정상"
    if tp_s > tp_m: flag_m = f"⚠️역전됨 (단기 모멘텀 {tp_s:,.0f}원 대비 중기 가치가 낮음)"
    if tp_m > tp_l: flag_l = f"⚠️역전됨 (중기 가치 대비 장기 RIM 가치({tp_l:,.0f}원)가 낮음)"

    calc_result_log = (
        f"▶ 리스크 팩트 (k={user_k:.1f}): 단기손절 {sl_s:,.0f}원 | 중기손절 {sl_m:,.0f}원 | 장기손절 {sl_l:,.0f}원\n"
        f"▶ [최종 채택 목표가] (출력 화면 1:1 매칭용 - 역전 시 역전된 그대로 인용할 것):\n"
        f"   - 단기 목표가: {tp_s:,.0f}원\n"
        f"   - 중기 목표가: {tp_m:,.0f}원\n"
        f"   - 장기 목표가: {tp_l:,.0f}원\n"
        f"▶ [퀀트 엔진 내부 검증 로그 (리스크 플래그)]:\n"
        f"   - 밸류에이션 모델 타입: {fund_type}\n"
        f"   - 중기 시그널 상태: {flag_m}\n"
        f"   - 장기 시그널 상태: {flag_l}\n"
        f"   - 참고 BPS 청산가치: {conservative_bps:,.0f}원\n"
    )

    tech_data_str = f"[{name} ({ticker})]\n"
    if tech: tech_data_str += f"- 차트/리스크: 현재가 {tech['current']:,.0f} | 20일선 {tech['ma20']:,.0f} | 60일선 {tech['ma60']:,.0f} | MACD {tech['macd']:,.2f} | 20일 변동성(일간) {daily_vol*100:.2f}%\n"
    if fund: tech_data_str += f"- 재무 비율: PER {fund['per']} (업종PER {fund['industry_per']}) | PBR {fund['pbr']} | EPS {eps_val:,}원 | BPS {bps_val:,}원\n"
    tech_data_str += f"{calc_result_log}\n- 뉴스/공시 요약본:\n{lite_summary}\n\n"
    
    return tech_data_str

# =======================================================
# 각 탭별 레이아웃 및 렌더링
# =======================================================
# 탭 4: 종목 발굴
with tab4:
    st.subheader("종목 발굴 (병렬 고속 분석)")
    investment_horizon = st.radio("투자기간", ["단기 (1~3개월)", "중기 (3~6개월)", "장기 (1년 이상)"], horizontal=True)

    if st.button("추천 종목 발굴", use_container_width=True, key="btn_recommend"):
        rec_news = dedupe_news((g_data.get("realtime_news", []) if g_data else []) + (cached_data.get("eco_news", []) if cached_data else []))
        
        if not rec_news: 
            st.error("분석 대상 뉴스 풀이 비어있습니다.")
        else:
            with st.spinner("[1단계] 1차 후보군 10개 추출 중..."):
                selected_tickers = fetch_candidate_tickers(rec_news, investment_horizon, set(), 10)
            
            if not selected_tickers: 
                st.error("⚠️ AI가 조건에 맞는 종목을 추출하지 못했거나 API 응답이 지연되었습니다. 잠시 후 다시 시도해주세요.")
                st.stop()
            
            with st.spinner("[2단계] 후보군 동시 병렬 크롤링 및 리스크/목표가 밴드 산출 중..."):
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(process_single_ticker, t, investment_horizon, k_factor, True) for t in selected_tickers]
                    results = [future.result() for future in concurrent.futures.as_completed(futures)]
                    valid_results = [r for r in results if r and r.strip()]

            tried_tickers = set(selected_tickers)
            max_retry = 2
            retry_count = 0
            
            while len(valid_results) < 10 and retry_count < max_retry:
                with st.spinner(f"[보충 단계] 현재 {len(valid_results)}개 확보. 부족분 보충 중 (시도 {retry_count+1}/{max_retry})..."):
                    deficit = 10 - len(valid_results)
                    extra_tickers = fetch_candidate_tickers(rec_news, investment_horizon, tried_tickers, deficit)
                    extra_tickers = [t for t in extra_tickers if t not in tried_tickers]
                    if not extra_tickers: break
                    tried_tickers.update(extra_tickers)
                    
                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                        futures = [executor.submit(process_single_ticker, t, investment_horizon, k_factor, True) for t in extra_tickers]
                        extra_results = [f.result() for f in concurrent.futures.as_completed(futures)]
                        valid_results += [r for r in extra_results if r and r.strip()]
                    retry_count += 1

            tech_data_str = "".join(valid_results)

            if len(valid_results) == 0:
                st.warning("⚠️ 2회 재시도 보충을 진행했으나, 후보군 전부 밸류에이션상 상승여력이 없어 추천에서 제외되었습니다.")
                st.session_state.today_recommendation = ""
            else:
                with st.spinner(f"[3단계] 최종 선별된 {len(valid_results)}개 중 Flash 기반 Top 3 보고서 작성 중..."):
                    step3_prompt = (
                        f"당신은 리스크와 기회를 종합적으로 분석하는 전문 퀀트 애널리스트입니다.\n"
                        f"[후보군 팩트 데이터(뉴스, 공시, 재무, 차트 포함)]\n{tech_data_str}\n\n"
                        f"=== ⚠️ AI 분석 지침 ===\n"
                        f"1. 가장 매력적이며 종합매력도 점수가 가장 높은 **Top 3 종목만 엄선**하십시오.\n"
                        f"2. **[종합 분석 및 스코어링]** 제공된 모든 데이터(뉴스 모멘텀, 수급, 펀더멘털)를 당신의 역량으로 종합하여 **'종합 매력도 점수(0~100점)'**를 1순위로 산정하고 최상단에 명시하십시오.\n"
                        f"3. 절대 주가나 수식을 임의 계산하지 마시고 파이썬이 연산한 [최종 채택 목표가]와 손절가를 그대로 인용하십시오.\n"
                        f"4. **[논리적 근거 강제]** 강세/약세 논리를 서술할 때 반드시 뉴스/공시 이슈를 서술하고, 목표가의 타당성을 논증할 때는 파이썬이 제공한 팩트 데이터의 숫자를 직접 인용하여 증명하십시오.\n"
                        f"5. **[매우 중요]** 목표가가 기간별로 '역전됨' 플래그가 발견된 종목은 단기 오버슈팅 상태입니다. 반드시 <BEAR_CASE>에 '장기 가치 수렴 한계 리스크'를 구체적으로 경고하십시오.\n\n"
                        f"=== 리포트 작성 항목 ===\n"
                        f"<ANALYSIS_티커숫자>\n"
                        f"### [종목명] (티커)\n"
                        f"**🎯 종합 매력도 점수: [00]/100점**\n"
                        f"**🎯 핵심 투자 아이디어 및 모멘텀 (Why Buy?)**\n"
                        f"- (뉴스/공시와 데이터를 결합한 종합적 의견)\n"
                        f"**🟢 강세 논리 (Bull Case)**\n"
                        f"**🔴 약세/위험 논리 (Bear Case - 역전 보정 경고 포함)**\n"
                        f"**⚖️ 최종 판단 및 리스크 평가**\n"
                        f"- 단기 목표가 논증: (기술적/수급 수치 인용)\n"
                        f"- 중/장기 목표가 논증: (펀더멘털 수치 인용)\n"
                        f"</ANALYSIS_티커숫자>\n\n"
                        f"※ 마지막 줄은 아래 파싱 형식으로 출력\n"
                        f"[TRACKING_DATA]\n"
                        f"종목명|티커|단기목표가|중기목표가|장기목표가|진입타점|단기손절가|중기손절가|장기손절가"
                    )
                    st.session_state.today_recommendation = "".join(call_gemini_stream_with_fallback(step3_prompt))

    if st.session_state.get('today_recommendation'):
        raw = st.session_state.today_recommendation
        with st.expander("추천 리포트"):
            display_text = re.sub(r'</?ANALYSIS_[^>]+>', '', raw.split("[TRACKING_DATA]")[0].strip())
            st.write(display_text)
            
            if "[TRACKING_DATA]" in raw:
                block = raw.split("[TRACKING_DATA]")[1].strip().replace("```", "")
                parsed_rows = []
                for line in block.split('\n'):
                    if not line.strip(): continue
                    data = line.split('|')
                    if len(data) >= 9: 
                        parsed_rows.append((data[0].strip(), data[1].strip(), parse_won(data[2]), parse_won(data[3]), parse_won(data[4]), parse_won(data[5]), parse_won(data[6]), parse_won(data[7]), parse_won(data[8])))
                
                price_map = fetch_current_prices([r[1] for r in parsed_rows])
                cols_rec = st.columns(3)
                
                for idx, (name, tick, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l) in enumerate(parsed_rows):
                    code = re.sub(r'[^\d]', '', tick)
                    price_info = price_map.get(code, {})
                    current, diff, diff_pct = price_info.get("current", 0.0), price_info.get("diff", 0.0), price_info.get("diff_pct", 0.0)
                    
                    with cols_rec[idx % 3]:
                        with st.container(border=True):
                            st.markdown(f"**{name}** `{tick}`")
                            if current > 0: st.metric("현재가", f"{current:,.0f}", delta=f"{diff:+,.0f} ({diff_pct:+.2f}%)")
                            c_tp, c_bp = st.columns(2)
                            c_tp.markdown(f"**목표가 밴드**<br>단: {tp_s:,.0f}<br>중: {tp_m:,.0f}<br>장: {tp_l:,.0f}", unsafe_allow_html=True)
                            c_bp.markdown(f"**손절가 라인**<br>단: <span style='color:red;'>{sl_s:,.0f}</span><br>중: <span style='color:red;'>{sl_m:,.0f}</span>", unsafe_allow_html=True)
                            
                            if st.button("스크랩", key=f"rec_s_{tick}", use_container_width=True):
                                match = re.search(f"<ANALYSIS_{code}>(.*?)</ANALYSIS_{code}>", raw, re.DOTALL)
                                specific_analysis = match.group(1).strip() if match else display_text
                                c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, sl_s, sl_m, sl_l, scrap_date, model_used, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                          (f"{name} 퀀트 심층분석", specific_analysis, name, tick, current, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l, datetime.now().strftime("%Y-%m-%d %H:%M"), MODEL_NAME, current_user))
                                conn.commit()
                                st.success(f"✅ 리포트 스크랩 완료!")

# 탭 5: 관심종목 진단
with tab5:
    st.subheader("관심종목 진단")
    own_status = st.radio("상태", ["미보유", "보유"], horizontal=True, key="add_own_status")
    
    if "input_stock_name" not in st.session_state: st.session_state["input_stock_name"] = ""
    if "input_avg_price" not in st.session_state: st.session_state["input_avg_price"] = "0"
    if "input_quantity" not in st.session_state: st.session_state["input_quantity"] = 0

    with st.form("add_stock", clear_on_submit=True):
        new_s = st.text_input("종목명 (예: 삼성전자)", key="widget_stock_name")
        c2, c3 = st.columns(2)
        avg_p = c2.text_input("평단가", value="0", disabled=(own_status == "미보유"), key="widget_avg_price")
        qty = c3.number_input("수량", min_value=0, value=0, disabled=(own_status == "미보유"), key="widget_quantity")
        
        if st.form_submit_button("추가") and new_s:
            code, matched_name = search_stock_code(new_s.strip())
            is_owned_flag = 1 if own_status == "보유" else 0
            final_avg_p = float(str(avg_p).replace(',', '')) if is_owned_flag else 0.0
            c.execute("INSERT INTO portfolio (stock_name, ticker, is_owned, avg_price, quantity, user_id) VALUES (?,?,?,?,?,?)", (new_s.strip(), code or '', is_owned_flag, final_avg_p, qty if is_owned_flag else 0, current_user))
            conn.commit()
            st.session_state["input_stock_name"], st.session_state["input_avg_price"], st.session_state["input_quantity"] = "", "0", 0
            st.rerun()

    c.execute("SELECT id, stock_name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l, model_used, report_time, ticker FROM portfolio WHERE user_id = ?", (current_user,))
    portfolios = c.fetchall()
    
    if portfolios:
        st.divider()
        col_bulk, _ = st.columns([2, 8])
        with col_bulk:
            if st.button("🗑️ 선택 항목 삭제", key="bulk_del_t5", use_container_width=True):
                if to_del := [p[0] for p in portfolios if st.session_state.get(f"chk_t5_{p[0]}", False)]:
                    c.execute(f"DELETE FROM portfolio WHERE id IN ({','.join(['?']*len(to_del))})", to_del)
                    conn.commit(); st.rerun()

        price_map_watch = fetch_current_prices([p[15] for p in portfolios if p[15]])

        for p in portfolios:
            p_id, name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l, model_used, report_time, ticker = p
            code = re.sub(r'[^\d]', '', ticker or "")
            price_info = price_map_watch.get(code, {})
            current, diff, diff_pct = price_info.get("current", 0.0), price_info.get("diff", 0.0), price_info.get("diff_pct", 0.0)

            st.markdown(f"### {name} `{code}`")
            col_sel, col_info, col_price, col_btn, col_del = st.columns([0.5, 3.5, 3, 1.5, 1.5])
            
            with col_sel: st.checkbox("선택", key=f"chk_t5_{p_id}", label_visibility="collapsed")
            with col_info:
                if is_owned:
                    st.caption(f"보유 | 평단: {avg_price:,.0f} | 수량: {quantity}")
                    if current > 0 and avg_price > 0: st.caption(f"수익률: {((current - avg_price) / avg_price * 100):+.1f}%")
            with col_price:
                if current > 0: st.metric("현재가", f"{current:,.0f}", delta=f"{diff:+,.0f} ({diff_pct:+.2f}%)")
            with col_btn:
                if st.button("진단 실행", key=f"run_{p_id}", use_container_width=True):
                    with st.spinner("파이썬 연산 및 수치 방어 논리 작성 중..."):
                        data_str_base = process_single_ticker(ticker, "단기/중기/장기 종합", k_factor, is_discovery_mode=False)
                        extra_ctx = f"\n현재가: {current:,.0f}\n"
                        if is_owned and avg_price > 0: extra_ctx += f"[내 계좌 정보] 평단가: {avg_price:,.0f} | 현재 수익률: {((current - avg_price) / avg_price * 100):+.1f}%\n"
                        
                        prompt = (f"[{name} 진단]\n[팩트 데이터]\n{data_str_base}\n{extra_ctx}\n\n"
                                  f"당신은 리스크와 기회를 종합적으로 분석하는 전문 퀀트 애널리스트입니다.\n"
                                  f"1. 파이썬이 선행 연산하여 제공한 [최종 채택 목표가] 가격을 그대로 채택하여 진단하십시오.\n"
                                  f"2. **[종합 매력도 점수]** 해당 종목의 뉴스/이슈와 퀀트 수치 및 차트 수치를 스스로 가중치 부여하여 **'종합 매력도 점수(0~100점)'**를 산정하십시오.\n"
                                  f"3. **[논리적 근거 강제]** 현황 및 촉매제를 설명할 때는 반드시 제공된 '뉴스/공시 요약본'의 구체적 이슈를 인용하고, '목표가의 타당성'을 논증할 때는 제공된 팩트 데이터(EPS, BPS, 변동성 등)의 '숫자'를 직접 인용하여 방어하십시오.\n"
                                  f"4. **[위험 요소]** '역전됨' 플래그가 발견된 종목은 반드시 <BEAR_CASE>에 구체적인 오버슈팅 리스크를 서술하십시오.\n"
                                  f"5. 계좌 수익률을 참고하여 '추가매수/유지/손절' 여부를 제시하십시오.\n\n"
                                  f"=== 작성 항목 ===\n"
                                  f"**🎯 종합 매력도 점수: [00]/100점**\n"
                                  f"**🎯 핵심 투자 아이디어 및 모멘텀 (Why Buy/Hold/Sell?)**\n"
                                  f"**🟢 강세 논리 (Bull Case)**\n"
                                  f"**🔴 약세/위험 논리 (Bear Case)**\n"
                                  f"**⚖️ 최종 판단 및 리스크 평가**\n"
                                  f"- 단기 목표가 논증: (기술적 수치를 인용하여 작성)\n"
                                  f"- 중/장기 목표가 논증: (펀더멘털 수치를 인용하여 작성)\n"
                                  f"※ 마지막 줄은 아래 파싱 형식으로 출력\n"
                                  f"TARGET_PRICE: 단기목표가|중기목표가|장기목표가|매수추천가|단기손절가|중기손절가|장기손절가")
                    
                    report = call_gemini_with_fallback(prompt)
                    tp_match = re.search(r'TARGET_PRICE:\s*([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|(.*)', report)
                    
                    n_tp_s = parse_won(tp_match.group(1)) if tp_match else 0.0
                    n_tp_m = parse_won(tp_match.group(2)) if tp_match else 0.0
                    n_tp_l = parse_won(tp_match.group(3)) if tp_match else 0.0
                    n_bp = parse_won(tp_match.group(4)) if tp_match else 0.0
                    n_sl_s = parse_won(tp_match.group(5)) if tp_match else 0.0
                    n_sl_m = parse_won(tp_match.group(6)) if tp_match else 0.0
                    n_sl_l = parse_won(tp_match.group(7)) if tp_match else 0.0

                    c.execute("UPDATE portfolio SET report_text=?, tp_s=?, tp_m=?, tp_l=?, bp=?, sl_s=?, sl_m=?, sl_l=?, model_used=?, report_time=? WHERE id=?", 
                              (report, n_tp_s, n_tp_m, n_tp_l, n_bp, n_sl_s, n_sl_m, n_sl_l, MODEL_NAME, datetime.now().strftime("%Y-%m-%d %H:%M"), p_id))
                    conn.commit(); st.rerun()
            with col_del:
                if st.button("개별 삭제", key=f"del_t5_{p_id}", use_container_width=True):
                    c.execute("DELETE FROM portfolio WHERE id=?", (p_id,))
                    conn.commit(); st.rerun()

            if report_text:
                with st.expander("진단 리포트"):
                    st.write(re.sub(r'TARGET_PRICE:.*', '', report_text).strip())
                    st.divider()
                    col_tgt, col_sl = st.columns(2)
                    with col_tgt:
                        st.markdown(f"**🎯 AI 최종 채택 목표가 밴드**\n* **단기 목표가:** {tp_s:,.0f}원\n* **중기 목표가:** {tp_m:,.0f}원\n* **장기 목표가:** {tp_l:,.0f}원")
                    with col_sl:
                        st.markdown(f"**🔴 파이썬 연산 리스크 규격 (k={k_factor:.1f})**\n* **단기 손절선:** {sl_s:,.0f}원\n* **중기 손절선:** {sl_m:,.0f}원\n* **장기 손절선:** {sl_l:,.0f}원")
                    
                    if st.button("스크랩북에 저장하여 가격 추적하기", key=f"scrap_t5_{p_id}", use_container_width=True):
                        c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, sl_s, sl_m, sl_l, scrap_date, model_used, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (f"{name} 관심종목 진단", report_text, name, ticker, current, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l, datetime.now().strftime("%Y-%m-%d %H:%M"), model_used, current_user))
                        conn.commit(); st.success("스크랩북 저장 완료")
            st.divider()

# 탭 6: 스크랩북
with tab6:
    st.subheader("저장된 분석 리포트 및 모델 검증")
    c.execute("""
        SELECT id, title, stock_name, ticker, scrap_date, analysis, model_used, 
               saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, 
               sl_s, sl_m, sl_l 
        FROM scrapbook 
        WHERE user_id = ? 
        ORDER BY id DESC
    """, (current_user,))
    scraps = c.fetchall()
    
    if scraps:
        tickers = [row[3] for row in scraps if row[3]]
        price_map_scrap = fetch_current_prices(tickers)
        total_evals = len(scraps)
        hit_count_s, hit_count_m = 0, 0
        stop_out_count_s, stop_out_count_m = 0, 0
        avg_current_yield = 0.0
        
        for row in scraps:
            s_saved_p, s_tp_s, s_tp_m, s_sl_s, s_sl_m, s_date = row[7], row[8], row[9], row[12], row[13], row[4]
            c_code = re.sub(r'[^\d]', '', row[3] or "")
            c_price = price_map_scrap.get(c_code, {}).get("current", 0.0)
            max_high, min_low = get_historical_high_low(c_code, s_date)
            
            if s_saved_p > 0 and c_price > 0:
                avg_current_yield += ((c_price - s_saved_p) / s_saved_p) * 100
                if s_tp_s > 0 and max_high >= s_tp_s: hit_count_s += 1
                if s_tp_m > 0 and max_high >= s_tp_m: hit_count_m += 1
                if s_sl_s > 0 and min_low <= s_sl_s and min_low > 0: stop_out_count_s += 1
                if s_sl_m > 0 and min_low <= s_sl_m and min_low > 0: stop_out_count_m += 1
                
        avg_current_yield = avg_current_yield / total_evals if total_evals > 0 else 0.0
        
        with st.container(border=True):
            st.markdown(f"### 🎯 K={k_factor:.1f} 기반 시스템 트레이딩 성과 (기간 내 터치 기준)")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("총 스크랩 리포트", f"{total_evals}개")
            m2.metric("단기 목표 도달 / 손절", f"{(hit_count_s/total_evals)*100:.1f}% / {(stop_out_count_s/total_evals)*100:.1f}%")
            m3.metric("중기 목표 도달 / 손절", f"{(hit_count_m/total_evals)*100:.1f}% / {(stop_out_count_m/total_evals)*100:.1f}%")
            m4.metric("스크랩 포트폴리오 수익률", f"{avg_current_yield:+.2f}%")

            if total_evals >= 5:
                stop_rate_s = stop_out_count_s / total_evals
                hit_rate_s = hit_count_s / total_evals
                if stop_rate_s >= 0.35:
                    st.warning(f"⚠️ 단기 손절 이탈률이 {stop_rate_s*100:.0f}%로 높은 편입니다. 현재 k={k_factor:.1f} 값이 다소 타이트할 수 있으니, 사이드바에서 k값을 조금 높여보는 것을 고려해보세요.")
                elif hit_rate_s <= 0.2 and stop_rate_s <= 0.1:
                    st.info(f"💡 단기 목표 도달률이 {hit_rate_s*100:.0f}%로 낮은 반면 손절 이탈은 적습니다. 목표가 밴드가 다소 보수적으로 산출되고 있을 수 있습니다.")
            
        st.divider()

        col_bulk_scrap, _ = st.columns([2, 8])
        with col_bulk_scrap:
            if st.button("🗑️ 선택 항목 삭제", key="bulk_del_t6", use_container_width=True):
                if to_del := [s[0] for s in scraps if st.session_state.get(f"chk_t6_{s[0]}", False)]:
                    c.execute(f"DELETE FROM scrapbook WHERE id IN ({','.join(['?']*len(to_del))})", to_del)
                    conn.commit(); st.rerun()
                    
        for row in scraps:
            s_id, title, s_name, ticker, s_date, analysis, m_used, saved_p, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l = row
            code = re.sub(r'[^\d]', '', ticker or "")
            price_info = price_map_scrap.get(code, {})
            current_p = price_info.get("current", 0.0)
            max_high, min_low = get_historical_high_low(code, s_date)
            
            col_sel_s, col_exp_s = st.columns([0.5, 9.5])
            with col_sel_s: st.checkbox("선택", key=f"chk_t6_{s_id}", label_visibility="collapsed")
            with col_exp_s:
                with st.expander(f"📌 {title} ({s_name} | {ticker}) - {s_date}"):
                    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
                    m_col1.metric("저장 당시 주가", f"{saved_p:,.0f}원" if saved_p else "정보 없음")
                    if current_p > 0:
                        diff = current_p - saved_p if saved_p else 0.0
                        diff_pct = (diff / saved_p * 100) if saved_p else 0.0
                        m_col2.metric("실시간 현재가", f"{current_p:,.0f}원", delta=f"{diff:+,.0f}원 ({diff_pct:+.2f}%)")
                    else: m_col2.metric("실시간 현재가", "조회 실패")
                    m_col3.markdown(f"**손절가 라인**<br>단기: <span style='color:red;'>{sl_s:,.0f}</span><br>중기: <span style='color:red;'>{sl_m:,.0f}</span>", unsafe_allow_html=True)
                    m_col4.markdown(f"**목표가 밴드**<br>단기: {tp_s:,.0f}<br>중기: {tp_m:,.0f}", unsafe_allow_html=True)
                    
                    if current_p > 0 and tp_s > 0:
                        pct_s = (current_p / tp_s) * 100
                        st.progress(min(int(pct_s), 100), text=f"단기 목표가 대비 진행률: **{pct_s:.1f}%**")
                        if min_low > 0 and min_low <= sl_s and sl_s > 0:
                            st.error(f"⚠️ **과거 단기 손절선({sl_s:,.0f}원) 이탈 이력 발생!** 현재 반등했더라도 시스템 룰에 따른 리뷰가 필요합니다.")
                        elif current_p <= sl_s and sl_s > 0:
                            st.error(f"⚠️ **단기 손절선({sl_s:,.0f}원) 이탈 진행 중!** 기계적 손절을 고려하십시오.")
                    
                    clean_analysis = re.sub(r'TARGET_PRICE:.*', '', analysis.split("[TRACKING_DATA]")[0].strip()).strip()
                    st.markdown("---")
                    st.write(clean_analysis)
                    
                    if st.button("개별 삭제", key=f"del_t6_{s_id}", use_container_width=True):
                        c.execute("DELETE FROM scrapbook WHERE id=?", (s_id,))
                        conn.commit(); st.rerun()
    else:
        st.info("저장된 분석 리포트가 없습니다.")
