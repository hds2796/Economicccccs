import streamlit as st
import json
import sqlite3
import re
import threading
import requests
import time
import pandas as pd
import numpy as np
import urllib.request
import urllib.parse
import os
import io
import streamlit.components.v1 as components
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
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS holding_companies (stock_code TEXT PRIMARY KEY, corp_name TEXT)''')
    cursor.execute("SELECT count(*) FROM holding_companies")
    if cursor.fetchone()[0] == 0:
        default_holdings = [
            ('078930', 'GS'), ('000880', '한화'), ('001040', 'CJ'), ('006260', 'LS'), 
            ('034730', 'SK'), ('000150', '두산'), ('004800', '효성'), ('028260', '삼성물산'), 
            ('267250', 'HD현대'), ('004990', '롯데지주'), ('002020', '코오롱'), ('000240', '한국앤컴퍼니'), 
            ('002790', '아모레G'), ('000210', 'DL'), ('058650', '세아홀딩스'), ('000140', '하이트진로홀딩스'), 
            ('005720', '넥센'), ('003550', 'LG')
        ]
        cursor.executemany("INSERT OR IGNORE INTO holding_companies (stock_code, corp_name) VALUES (?, ?)", default_holdings)
    
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

@st.cache_data(ttl=86400)
def fetch_holding_ticker_list_from_db():
    local_conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
    local_c = local_conn.cursor()
    local_c.execute("SELECT stock_code FROM holding_companies")
    return [row[0] for row in local_c.fetchall()]

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
    except Exception: pass

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
# AI 통신 및 파싱
# =======================================================
GEMINI_CONCURRENCY_LIMIT = 3
_gemini_semaphore = threading.Semaphore(GEMINI_CONCURRENCY_LIMIT)

def call_gemini_lite_summary(prompt):
    acquired = _gemini_semaphore.acquire(timeout=40)
    if not acquired: return "API 대기 시간 초과(Lite)"
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        return client.models.generate_content(model=LITE_MODEL_NAME, contents=prompt).text
    except Exception as e: 
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            return client.models.generate_content(model=FALLBACK_MODEL_NAME, contents=prompt).text
        except: return f"요약 실패: {e}"
    finally: _gemini_semaphore.release()

def call_gemini_with_fallback(prompt, model=MODEL_NAME):
    acquired = _gemini_semaphore.acquire(timeout=40)
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
    acquired = _gemini_semaphore.acquire(timeout=40)
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
# CAPM 전용 매크로 지표 추출
# =======================================================
@st.cache_data(ttl=3600)
def get_kospi_returns():
    try:
        url = "https://fchart.stock.naver.com/sise.nhn?symbol=KOSPI&timeframe=day&count=250&requestType=0"
        res = get_session().get(url, timeout=5)
        with xml_parse_lock:
            root = ET.fromstring(res.text)
            items = root.findall('.//item')
        dates, prices = [], []
        for item in items:
            data = item.attrib['data'].split('|')
            dates.append(datetime.strptime(data[0], "%Y%m%d"))
            prices.append(float(data[4]))
        df = pd.DataFrame({"kospi_close": prices}, index=dates)
        df["kospi_return"] = df["kospi_close"].pct_change()
        return df["kospi_return"].dropna()
    except: return pd.Series(dtype=float)

@st.cache_data(ttl=3600)
def get_risk_free_rate():
    try:
        url = "https://finance.naver.com/marketindex/interestDailyQuote.naver?marketindexCd=IRR_GOVT10Y"
        res = get_session().get(url, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        val = soup.select_one("div.head_info > span.value").text.strip()
        rf_rate = float(val) / 100
        if rf_rate > 0.10 or rf_rate <= 0: 
            return 0.032
        return rf_rate
    except: 
        return 0.032 

# =======================================================
# 데이터 가공 및 팩트 추출 유틸
# =======================================================
@st.cache_data(ttl=600)
def get_dart_filings(stock_code):
    if not DART_API_KEY: return "DART API 키 없음"
    try:
        local_conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
        local_c = local_conn.cursor()
        local_c.execute("SELECT corp_code FROM dart_corp_codes WHERE stock_code = ?", (stock_code,))
        row = local_c.fetchone()
        local_conn.close()
        
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
    data = {"per": "-", "pbr": "-", "eps": None, "bps": None, "industry_per": "-", "quarter_trend": "정보 없음", "supply_demand": "수급 정보 없음", "sales_history": [], "op_history": [], "eps_history": [], "roe_history": []}
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

        cop_table = soup.find("div", class_="cop_analysis")
        if cop_table:
            data["quarter_trend"] = "최근 실적 수집 완료"
            try:
                for th in cop_table.find_all("th"):
                    text = th.get_text().strip()
                    if "매출액" in text:
                        valid_sales = [float(v) for td in th.find_parent("tr").find_all("td") if (v := td.get_text().strip().replace(',', '')) and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        if valid_sales: data["sales_history"] = valid_sales[-3:]
                    elif "영업이익" in text:
                        valid_op = [float(v) for td in th.find_parent("tr").find_all("td") if (v := td.get_text().strip().replace(',', '')) and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        if valid_op: data["op_history"] = valid_op[-3:]
                    elif "EPS" in text:
                        valid_eps = [float(v) for td in th.find_parent("tr").find_all("td") if (v := td.get_text().strip().replace(',', '')) and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        if valid_eps:
                            if data["eps"] is None: data["eps"] = valid_eps[-1] 
                            data["eps_history"] = valid_eps[-3:]
                    elif "BPS" in text:
                        valid_bps = [float(v) for td in th.find_parent("tr").find_all("td") if (v := td.get_text().strip().replace(',', '')) and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        if valid_bps:
                            if data["bps"] is None: data["bps"] = valid_bps[-1] 
                    elif "ROE" in text:
                        valid_roe = [float(v) for td in th.find_parent("tr").find_all("td") if (v := td.get_text().strip().replace(',', '')) and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        if valid_roe: data["roe_history"] = valid_roe[-3:]
            except: pass
            
        for th in soup.find_all("th"):
            if "동일업종 PER" in th.get_text():
                td = th.find_next("td")
                if td: data["industry_per"] = td.get_text().strip().replace('배', '')
        
        url_frgn = f"https://finance.naver.com/item/frgn.naver?code={code}"
        res_frgn = session.get(url_frgn, headers=headers, timeout=5)
        soup_frgn = BeautifulSoup(res_frgn.text, "html.parser")
        
        inst_sum_5, fore_sum_5 = 0, 0
        inst_sum_20, fore_sum_20 = 0, 0
        count = 0
        for tr in soup_frgn.find_all("tr", {"onmouseover": "mouseOver(this)"}):
            if count >= 20: break
            tds = tr.find_all("td")
            if len(tds) >= 7:
                try:
                    i_vol = int(tds[5].get_text().strip().replace(',', '') or 0)
                    f_vol = int(tds[6].get_text().strip().replace(',', '') or 0)
                    if count < 5:
                        inst_sum_5 += i_vol
                        fore_sum_5 += f_vol
                    inst_sum_20 += i_vol
                    fore_sum_20 += f_vol
                    count += 1
                except: pass
        if count > 0:
            data["supply_demand"] = f"[수급 동향] 최근 5일(기관 {inst_sum_5:+,}주 / 외인 {fore_sum_5:+,}주) | 최근 20일(기관 {inst_sum_20:+,}주 / 외인 {fore_sum_20:+,}주)"
    except: pass
    return data

@st.cache_data(ttl=600)
def get_technical_data(code):
    try:
        kospi_returns = get_kospi_returns() 
        url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=250&requestType=0"
        session = get_session()
        res = session.get(url, timeout=5)
        with xml_parse_lock:
            root = ET.fromstring(res.text)
            items = root.findall('.//item')
        if not items: return None
        
        dates, prices = [], []
        for item in items:
            data = item.attrib['data'].split('|')
            dates.append(datetime.strptime(data[0], "%Y%m%d"))
            prices.append(float(data[4]))
            
        df_stock = pd.DataFrame({"stock_close": prices}, index=dates)
        df_stock["stock_return"] = df_stock["stock_close"].pct_change()
        returns = df_stock["stock_return"].dropna()
        
        current_price = prices[-1]
        daily_volatility = returns.iloc[-20:].std() if len(returns) >= 20 else 0.0
        
        df_series = pd.Series(prices)
        macd = df_series.ewm(span=12, adjust=False).mean() - df_series.ewm(span=26, adjust=False).mean()
        signal = macd.ewm(span=9, adjust=False).mean()

        beta = 1.0
        if not kospi_returns.empty and not returns.empty:
            combined_df = pd.concat([returns, kospi_returns], axis=1, join="inner").dropna()
            if len(combined_df) > 30:
                cov_matrix = np.cov(combined_df.iloc[:, 0], combined_df.iloc[:, 1])
                if cov_matrix[1, 1] != 0:
                    beta = cov_matrix[0, 1] / cov_matrix[1, 1]
                    beta = max(0.5, min(beta, 2.5)) 

        return {"current": current_price, "high_52": max(prices), "low_52": min(prices), "ma20": sum(prices[-20:])/20, "ma60": sum(prices[-60:])/60, "macd": macd.iloc[-1], "signal": signal.iloc[-1], "daily_volatility": daily_volatility, "beta": beta}
    except: return None

@st.cache_data(ttl=600)
def fetch_stock_news(query, display=5):
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET: return []
    try:
        url = f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={urllib.parse.quote(query)}&display={display}&sort=date&format=json"
        req = urllib.request.Request(url, headers={"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET})
        with urllib.request.urlopen(req, timeout=3) as response:
            return [{
                "title": BeautifulSoup(i['title'], "html.parser").get_text(), 
                "link": i['link'],
                "summary": BeautifulSoup(i.get('description', ''), "html.parser").get_text()
            } for i in json.loads(response.read().decode('utf-8')).get("items", [])]
    except: return []

@st.cache_data(ttl=1800)
def get_historical_high_low(code, start_date_str):
    try:
        url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=250&requestType=0"
        res = requests.get(url, timeout=5)
        with xml_parse_lock:
            root = ET.fromstring(res.text)
            items = root.findall('.//item')
        start_date = datetime.strptime(start_date_str.split()[0], "%Y-%m-%d")
        max_h, min_l = 0.0, float('inf')
        for item in items:
            data = item.attrib['data'].split('|')
            item_date = datetime.strptime(data[0], "%Y%m%d")
            if item_date >= start_date:
                high, low = float(data[2]), float(data[3])
                if high > max_h: max_h = high
                if low < min_l: min_l = low
        return max_h, (min_l if min_l != float('inf') else 0.0)
    except: return 0.0, 0.0

def search_stock_code(name):
    try:
        url = f"https://m.stock.naver.com/front-api/search/autoComplete?query={requests.utils.quote(name)}&target=stock,index,marketindicator,coin,ipo"
        res = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"}).json()
        items = (res.get("result") or {}).get("items", [])
        stock_items = [i for i in items if i.get("typeName") in ("코스피", "코스닥")] or items
        if stock_items: return stock_items[0].get("code"), stock_items[0].get("name")
    except: pass
    return None, None

def fetch_current_prices(codes):
    valid_codes = [re.sub(r'[^\d]', '', str(c)) for c in codes if len(re.sub(r'[^\d]', '', str(c))) == 6]
    if not valid_codes: return {}
    try:
        url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{','.join(valid_codes)}"
        payload = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"}).json()
        def find_datas(obj):
            if isinstance(obj, dict):
                if "datas" in obj and isinstance(obj["datas"], list): return obj["datas"]
                for v in obj.values():
                    if (found := find_datas(v)) is not None: return found
            return None
        datas = find_datas(payload) or []
        out = {}
        for item in datas:
            code = str(item.get("itemCode") or item.get("code") or item.get("cd") or "")
            def to_f(*keys):
                for k in keys:
                    if k in item and item[k] not in (None, ""):
                        try: return float(str(item[k]).replace(",", ""))
                        except: continue
                return 0.0
            out[code] = {"current": to_f("closePrice", "nv"), "diff": to_f("compareToPreviousClosePrice", "cv"), "diff_pct": to_f("fluctuationsRatio", "cr")}
        return out
    except: return {}

def dedupe_news(news_list):
    seen, out = set(), []
    for n in news_list or []:
        if (key := n.get("title", "").strip()) and key not in seen:
            seen.add(key); out.append(n)
    return out

@st.cache_data(ttl=1800)
def fetch_cached_global_data():
    try:
        info = json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
        creds = Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive.readonly'])
        drive_service = build('drive', 'v3', credentials=creds)
        folder_id = st.secrets.get("GOOGLE_REALTIME_FOLDER_ID", "")
        results = drive_service.files().list(q=f"'{folder_id}' in parents and name = 'market_data_latest.json' and trashed = false", fields="files(id)").execute()
        if not (files := results.get('files', [])): return None
        request = drive_service.files().get_media(fileId=files[0]['id'])
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: status, done = downloader.next_chunk()
        fh.seek(0)
        return json.loads(fh.read().decode('utf-8'))
    except: return None

def fetch_realtime_data_direct():
    if not API_GATEWAY_REALTIME_URL: return None
    try:
        res = requests.post(API_GATEWAY_REALTIME_URL, json={"seen_links": []}, timeout=30)
        res.raise_for_status()
        return res.json()
    except: return None

# =======================================================
# 핵심 퀀트 엔진: 퀀트/차트/뉴스 및 컨센서스 트렌드 조인
# =======================================================
def process_single_ticker(ticker, investment_horizon, user_k, is_discovery_mode=False, analyst_data=None):
    ticker = re.sub(r'[^\d]', '', str(ticker))
    if len(ticker) != 6: return None
    
    session = get_session()
    try:
        res = session.get(f"https://m.stock.naver.com/api/stock/{ticker}/basic", timeout=3).json()
        name = res.get("stockName", ticker)
    except: name = ticker
    
    tech = get_technical_data(ticker)
    fund = get_advanced_fundamental_data(ticker)
    dart_info = get_dart_filings(ticker)
    news_raw = fetch_stock_news(name, display=4)
    
    news_text = "\n".join([f"- 제목: {n['title']}\n  내용: {n.get('summary', '요약 없음')}" for n in news_raw])
    lite_summary = call_gemini_lite_summary(f"뉴스/공시 요약:\n{dart_info}\n{news_text}")
    
    current_price = tech['current'] if tech else 0.0
    daily_vol = tech['daily_volatility'] if tech else 0.0
    beta = tech['beta'] if tech and 'beta' in tech else 1.0
    
    eps_val = fund.get('eps')
    bps_val = fund.get('bps')
    roe_history = fund.get('roe_history', [])
    eps_history = fund.get('eps_history', [])
    
    try: float_ind_per = float(fund['industry_per'].replace(',', '')) if fund['industry_per'] != '-' else 0.0
    except: float_ind_per = 0.0

    if bps_val is not None and bps_val > 0 and current_price > 0: 
        fund['pbr'] = f"{current_price / bps_val:.2f}"
    else: 
        fund['pbr'] = "-"

    eps_growth = 0.0
    if len(eps_history) >= 2 and eps_history[0] != 0:
        eps_growth = (eps_history[-1] - eps_history[0]) / abs(eps_history[0])
        if eps_history[0] < 0 and eps_history[-1] > 0:
            eps_growth = min(eps_growth, 0.2) 
        else:
            eps_growth = min(max(eps_growth, -0.5), 1.0)

    sl_s = current_price * (1 - min(user_k * daily_vol * np.sqrt(20), 0.15)) if daily_vol > 0 else current_price * 0.95
    sl_m = current_price * (1 - min(user_k * daily_vol * np.sqrt(60), 0.30)) if daily_vol > 0 else current_price * 0.90
    sl_l = current_price * (1 - min(user_k * daily_vol * np.sqrt(250), 0.50)) if daily_vol > 0 else current_price * 0.80
    tp_s = current_price * min(1 + user_k * daily_vol * np.sqrt(20), 1.25) if daily_vol > 0 else current_price * 1.05
    rf = get_risk_free_rate()

    holding_ticker_list = fetch_holding_ticker_list_from_db()
    is_holding = any(kw in name for kw in ['지주', '홀딩스']) or (ticker in holding_ticker_list)
    
    structural_warning = ""

    if bps_val is None:
        tp_m = current_price * min(1 + user_k * daily_vol * np.sqrt(60), 1.40) if daily_vol > 0 else current_price * 1.10
        tp_l = current_price * min(1 + user_k * daily_vol * np.sqrt(250), 1.60) if daily_vol > 0 else current_price * 1.15
        fund_type = "BPS 데이터 누락 (기술적 밴드 대체)"
        structural_warning = "⚠️ [핵심 데이터 누락] BPS 누락으로 기술적 밴드 연산."
        conservative_bps = 0.0
        data_incomplete = True
        
    elif is_holding:
        holding_discount = 0.5  
        effective_bps = bps_val * holding_discount
        tp_m = effective_bps
        fund_type = "지주사 특수 모델 (NAV 50% 할인 앵커링)"
        if eps_val is None:
            structural_warning = "⚠️ [지주사 디스카운트] 지주사 할인 0.5 적용. EPS 누락."
        else:
            structural_warning = f"⚠️ [지주사 디스카운트] 타겟 PBR 0.5 수준({tp_m:,.0f}원) 앵커링."
            
        required_return = rf + (beta * 0.06) 
        expected_roe = (roe_history[-1] / 100) if (roe_history and roe_history[-1] > 0) else 0.05
        tp_l = effective_bps + (effective_bps * (expected_roe - required_return) / required_return)
        fund_type += f" | 장기 RIM(Rf {rf*100:.1f}%, Beta {beta:.2f})"
        conservative_bps = effective_bps
        data_incomplete = False
        
    elif eps_val is None:
        tp_m = current_price * min(1 + user_k * daily_vol * np.sqrt(60), 1.40) if daily_vol > 0 else current_price * 1.10
        tp_l = current_price * min(1 + user_k * daily_vol * np.sqrt(250), 1.60) if daily_vol > 0 else current_price * 1.15
        fund_type = "EPS 데이터 누락 (기술적 밴드 대체)"
        structural_warning = "⚠️ [실적 데이터 누락] 일반 사업회사 EPS 미수집."
        conservative_bps = bps_val
        data_incomplete = True
        
    elif eps_val <= 0:
        tp_m = current_price * min(1 + user_k * daily_vol * np.sqrt(60), 1.40) if daily_vol > 0 else current_price * 1.10
        tp_l = current_price * min(1 + user_k * daily_vol * np.sqrt(250), 1.60) if daily_vol > 0 else current_price * 1.15
        fund_type = "적자 운영 기업 (기술적 밴드 대용)"
        bps_discount = 0.8 if len(eps_history) >= 2 and eps_history[-1] < 0 and eps_history[0] < 0 and eps_history[-1] < eps_history[0] else 1.0
        conservative_bps = bps_val * bps_discount
        data_incomplete = False
        
    else:
        growth_pct = max(eps_growth * 100, 5.0) 
        dynamic_per_cap = max(8.0, min(growth_pct * 1.2, 40.0)) 

        adjusted_ind_per = float_ind_per * (1 + eps_growth) if float_ind_per > 0 else dynamic_per_cap
        current_per = (current_price / eps_val)
        
        if float_ind_per > (dynamic_per_cap * 1.5):
            fund_type = "상대 가치 (PEG 동적 캡 적용)"
            structural_warning = f"⚠️ [Value Trap 방어] 업종 PER({float_ind_per:.1f}배)이 기업의 이익성장력(PEG 한계치 {dynamic_per_cap:.1f}배) 대비 비정상적으로 높아 동적 상한선을 강제 적용했습니다."
            
            safe_per_cap = min(current_per * 1.5, dynamic_per_cap)
            tp_m = eps_val * safe_per_cap
            tp_m = max(tp_m, current_price * 1.05) 
        else:
            fund_type = "기본 상대 가치 (업종 평균 수렴)"
            tp_m = eps_val * adjusted_ind_per
            
        required_return = rf + (beta * 0.06) 
        expected_roe = (roe_history[-1] / 100) if roe_history else 0.05
        tp_l = bps_val + (bps_val * (expected_roe - required_return) / required_return)
        fund_type += f" | 장기 RIM(Rf {rf*100:.1f}%, Beta {beta:.2f})"
        conservative_bps = bps_val
        data_incomplete = False

    if is_discovery_mode:
        if data_incomplete:
            pass 
        elif is_holding:
            if current_price > 0 and (tp_m <= current_price or tp_l <= current_price): return None
        elif eps_val <= 0:
            if current_price > 0 and conservative_bps < current_price: return None
        else:
            if current_price > 0 and (tp_m <= current_price or tp_l <= current_price): return None 

    flag_m = "정상" if tp_s <= tp_m else f"⚠️역전됨 (단기 모멘텀 {tp_s:,.0f}원 대비 중기 가치가 낮음)"
    flag_l = "정상" if tp_m <= tp_l else f"⚠️역전됨 (중기 가치 대비 장기 RIM 가치({tp_l:,.0f}원)가 낮음)"
    
    struct_warn_line = f"   - 구조적 분석: {structural_warning}\n" if structural_warning else ""
    bps_disp_val = f"{bps_val:,.0f}원" if bps_val is not None else "데이터 누락"

    consensus_log = ""
    if analyst_data:
        matched_report = next((rep for rep in analyst_data.values() if rep.get("ticker") == ticker), None)
        if matched_report:
            a_target = float(matched_report.get("target_price", 0))
            a_opinion = matched_report.get("opinion", "N/A")
            a_broker = matched_report.get("broker", "알 수 없음")
            a_trend = matched_report.get("tp_trend", "유지/신규") 
            
            if a_target > 0 and tp_m > 0:
                divergence = (tp_m - a_target) / a_target
                trend_text = f"시장 모멘텀: {a_trend}"
                if abs(divergence) >= 0.15:
                    if divergence > 0:
                        consensus_log = f"   - ⚖️ 컨센서스 교차검증: ⚠️[퀀트 고평가] 증권사 목표가({a_target:,.0f}원 | {trend_text}) 대비 퀀트 엔진 중기 목표가({tp_m:,.0f}원)가 {abs(divergence)*100:.1f}% 과도하게 높음. (Bear Case에 반영할 것)\n"
                    else:
                        consensus_log = f"   - ⚖️ 컨센서스 교차검증: ⚠️[퀀트 보수적] 증권사 목표가({a_target:,.0f}원 | {trend_text}) 대비 퀀트 엔진이 {abs(divergence)*100:.1f}% 더 보수적으로 하향 조정함. (시장 기대치가 비이성적일 수 있음)\n"
                else:
                    consensus_log = f"   - ⚖️ 컨센서스 교차검증: ✅[수렴] 퀀트 모델({tp_m:,.0f}원)과 {a_broker} 컨센서스({a_target:,.0f}원 | {trend_text})가 오차범위 내 일치함.\n"
            else:
                consensus_log = f"   - ⚖️ 컨센서스 교차검증: 시장 목표가 데이터 없음 ({a_opinion})\n"

    calc_result_log = (
        f"▶ 리스크 팩트 (k={user_k:.1f}): 단기손절 {sl_s:,.0f}원 | 중기손절 {sl_m:,.0f}원 | 장기손절 {sl_l:,.0f}원\n"
        f"▶ [최종 채택 목표가]\n"
        f"   - 단기 목표가: {tp_s:,.0f}원\n"
        f"   - 중기 목표가: {tp_m:,.0f}원\n"
        f"   - 장기 목표가: {tp_l:,.0f}원\n"
        f"▶ [퀀트 엔진 내부 검증 로그 (리스크 플래그)]:\n"
        f"   - 밸류에이션 모델 타입: {fund_type}\n"
        f"{struct_warn_line}"
        f"   - 중기 시그널 상태: {flag_m}\n"
        f"   - 장기 시그널 상태: {flag_l}\n"
        f"{consensus_log}"
        f"   - 참고 원본 BPS: {bps_disp_val}\n"
    )

    eps_str = f"{eps_val:,}원" if eps_val is not None else "데이터 누락"
    bps_str = f"{bps_val:,}원" if bps_val is not None else "데이터 누락"

    tech_data_str = f"[{name} ({ticker})]\n"
    if tech: tech_data_str += f"- 차트/리스크: 현재가 {tech['current']:,.0f} | Beta {beta:.2f} | 20일선 {tech['ma20']:,.0f} | 60일선 {tech['ma60']:,.0f} | MACD {tech['macd']:,.2f} | 20일 변동성(일간) {daily_vol*100:.2f}%\n"
    
    fund_str = f"- 재무 비율: PER {fund.get('per', '-')} (업종PER {fund.get('industry_per', '-')}) | PBR {fund.get('pbr', '-')} | EPS {eps_str} | BPS {bps_str}\n"
    fund_str += f"- 분기 실적 추세(단위: 억원, %): 매출액 {fund.get('sales_history', [])} | 영업이익 {fund.get('op_history', [])} | EPS {fund.get('eps_history', [])} | ROE {fund.get('roe_history', [])}\n"
    fund_str += f"- {fund.get('supply_demand', '수급 정보 없음')}\n"
    
    tech_data_str += fund_str
    tech_data_str += f"{calc_result_log}\n- 뉴스/공시 요약본:\n{lite_summary}\n\n"
    
    return {
        "ticker": ticker,
        "name": name,
        "tp_s": tp_s,
        "tp_m": tp_m,
        "tp_l": tp_l,
        "sl_s": sl_s,
        "sl_m": sl_m,
        "sl_l": sl_l,
        "current_price": current_price,
        "conservative_bps": conservative_bps,
        "tech_data_str": tech_data_str
    }

# =======================================================
# 상태 변수 선언 및 상단 레이아웃 제어
# =======================================================
cached_data = fetch_cached_global_data() or {}
if "realtime_cache" not in st.session_state: 
    st.session_state.realtime_cache = {
        "market_status": cached_data.get("market_status", {}), "realtime_news": cached_data.get("realtime_news", []),
        "sectors": cached_data.get("sectors") or cached_data.get("sector_news", {}), "updated_at": cached_data.get("updated_at", "대기 중")
    }

def merge_realtime_data(new_data):
    if not new_data: return
    old = st.session_state.realtime_cache
    old_market = old.get("market_status", {})
    old_market.update(new_data.get("market_status", {}))
    merged_news = dedupe_news(new_data.get("realtime_news", []) + old.get("realtime_news", []))
    old_sec, new_sec = old.get("sectors") or old.get("sector_news", {}), new_data.get("sectors") or new_data.get("sector_news", {})
    merged_sec = {sec: dedupe_news(new_sec.get(sec, []) + old_sec.get(sec, [])) for sec in set(old_sec.keys()).union(new_sec.keys())}
    st.session_state.realtime_cache = {"market_status": old_market, "realtime_news": merged_news, "sectors": merged_sec, "updated_at": new_data.get("updated_at", old.get("updated_at", "알 수 없음"))}

if not st.session_state.realtime_cache.get("realtime_news"):
    with st.spinner("데이터 로딩 중..."):
        if new_data := fetch_realtime_data_direct(): merge_realtime_data(new_data)

g_data = st.session_state.realtime_cache

st.markdown("### 📊 글로벌 마켓 실시간 전광판")

components.html(
    """
    <div class="tradingview-widget-container">
      <div class="tradingview-widget-container__widget"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/tv-widget-ticker-tape.js" async>
      {
      "symbols": [
        { "proName": "KRX:KOSPI", "title": "코스피" },
        { "proName": "KRX:KOSDAQ", "title": "코스닥" },
        { "proName": "FX_IDC:USDKRW", "title": "원/달러 환율" },
        { "proName": "OANDA:SPX500USD", "title": "S&P 500" },
        { "proName": "OANDA:NAS100USD", "title": "나스닥 100" },
        { "proName": "ECONOMICS:USINTR", "title": "미국 기준금리" }
      ],
      "showSymbolLogo": true,
      "isTransparent": false,
      "displayMode": "adaptive",
      "colorTheme": "light",
      "locale": "kr"
      }
      </script>
    </div>
    """,
    height=100
)

col_title, col_refresh = st.columns([5, 1.2])
with col_refresh:
    if st.button("뉴스/리포트 갱신", use_container_width=True):
        with st.spinner("AI 서버와 동기화 중..."):
            if new_data := fetch_realtime_data_direct(): merge_realtime_data(new_data)
            st.rerun()
with col_title: 
    st.caption(f"🧠 AI 백엔드 동기화 시점: {g_data.get('updated_at', '대기 중')} (뉴스는 수동 갱신, 지수는 자동 실시간)")

st.divider()

col_title, col_refresh = st.columns([5, 1.2])
with col_refresh:
    if st.button("뉴스/리포트 갱신", use_container_width=True):
        with st.spinner("AI 서버와 동기화 중..."):
            if new_data := fetch_realtime_data_direct(): merge_realtime_data(new_data)
            st.rerun()
with col_title: 
    st.caption(f"🧠 AI 백엔드 동기화 시점: {g_data.get('updated_at', '대기 중')} (뉴스는 수동 갱신, 지수는 자동 실시간)")

# 기존 파이썬 람다 동기화 컨트롤러

col_title, col_refresh = st.columns([5, 1.2])
with col_refresh:
    if st.button("뉴스/리포트 갱신", use_container_width=True):
        with st.spinner("AI 서버와 동기화 중..."):
            if new_data := fetch_realtime_data_direct(): merge_realtime_data(new_data)
            st.rerun()
with col_title: 
    st.caption(f"🧠 AI 백엔드 동기화 시점: {g_data.get('updated_at', '대기 중')} (뉴스와 리포트 데이터만 수동 동기화되며 지수는 실시간입니다.)")

st.divider()
 )

# 기존 파이썬 람다 동기화 컨트롤러 (뉴스와 리포트를 위해 유지하되 UI는 간소화)
col_title, col_refresh = st.columns([5, 1.2])
with col_refresh:
    if st.button("뉴스/리포트 갱신", use_container_width=True):
        with st.spinner("AI 서버와 동기화 중..."):
            if new_data := fetch_realtime_data_direct(): merge_realtime_data(new_data)
            st.rerun()
with col_title: 
    st.caption(f"🧠 AI 백엔드 동기화 시점: {g_data.get('updated_at', '대기 중')} (뉴스와 리포트 데이터만 수동 동기화되며 지수는 실시간입니다.)")

st.divider()

# =======================================================
# 각 탭별 기능
# =======================================================
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["실시간 브리핑", "핵심 경제", "섹터 뉴스", "종목 발굴", "관심종목 진단", "스크랩북"])

with tab1:
    st.subheader("실시간 시황 브리핑")
    news_pool = dedupe_news(g_data.get("realtime_news", []))
    if news_pool:
        with st.expander(f"📰 수집된 실시간 뉴스 (최신 10건 표시 / 총 {len(news_pool)}건 누적)"):
            for idx, n in enumerate(news_pool[:10]): st.markdown(f"{idx+1}. [{n['title']}]({n['link']})")
    if st.button("브리핑 생성", key="btn_briefing"):
        if not news_pool: st.error("분석할 뉴스가 없습니다.")
        else:
            news_str = "\n".join([f"- {n['title']}: {n.get('summary', '')}" for n in news_pool[:50]])
            with st.spinner("Lite 모델 압축 중..."): lite_summary = call_gemini_lite_summary(f"다음 뉴스를 요약하라:\n\n{news_str}")
            with st.spinner("Flash 모델 분석 중..."): st.write_stream(call_gemini_stream_with_fallback(f"지표:\n{json.dumps(market_data)}\n\n요약:\n{lite_summary}\n\n시장 흐름 심층 분석 서술."))

with tab2:
    st.subheader("핵심 경제 종합 브리핑 및 시장 심리")
    
    c.execute("SELECT calc_date, score FROM sentiment_history ORDER BY calc_date ASC")
    if sentiment_rows := c.fetchall():
        df_sent = pd.DataFrame(sentiment_rows, columns=['date', 'score'])
        df_sent['date'] = pd.to_datetime(df_sent['date']).dt.strftime('%Y-%m-%d')
        df_avg = df_sent.groupby('date')['score'].mean().reset_index().set_index('date')
        df_avg_7d = df_avg.tail(7)
        today_str = datetime.now().strftime('%Y-%m-%d')
        if not df_avg.empty:
            today_score = df_avg.loc[today_str, 'score'] if today_str in df_avg.index else df_avg.iloc[-1]['score']
            prev_score = df_avg.iloc[-2]['score'] if len(df_avg) > 1 else today_score
            col_s1, col_s2 = st.columns([2, 8])
            with col_s1:
                diff = today_score - prev_score
                st.metric("오늘의 평균 시장 심리", f"{today_score:.1f}점", f"{diff:+.1f}p" if len(df_avg) > 1 else "첫 측정")
                st.caption("0(공포) ◀ 50(중립) ▶ 100(탐욕)\n*최근 7일 트렌드")
            with col_s2:
                st.line_chart(df_avg_7d['score'], height=150)
                
    st.divider()

    st.markdown("### 🌐 실시간 마켓 데이터 및 리포트 유니버스 상황")
    
    analyst_universe = cached_data.get("analyst_universe", {})
    today_active_reps = cached_data.get("today_active_reports", [])
    us_news_pool = cached_data.get("us_market_news", [])
    eco_news_pool = cached_data.get("eco_news", [])
    realtime_news_pool = g_data.get("realtime_news", [])

    if "us_news_limit" not in st.session_state: st.session_state.us_news_limit = 6
    if "kr_news_limit" not in st.session_state: st.session_state.kr_news_limit = 6

    c_us_news, c_kr_news, c_rep_news = st.columns(3)
    
    with c_us_news:
        with st.container(border=True):
            st.markdown("🇺🇸 **미국 증시 및 빅테크 (24h)**")
            if us_news_pool:
                for idx, n in enumerate(us_news_pool[:st.session_state.us_news_limit]):
                    st.markdown(f"{idx+1}. [{n['title']}]({n['link']})")
                if len(us_news_pool) > st.session_state.us_news_limit:
                    if st.button("🔽 6개 더보기", key="more_us", use_container_width=True):
                        st.session_state.us_news_limit += 6
                        st.rerun()
            else:
                st.info("수집된 미국 증시 뉴스가 없습니다.")

    with c_kr_news:
        with st.container(border=True):
            st.markdown("🇰🇷 **국내 거시 및 증시 (24h)**")
            if eco_news_pool:
                for idx, n in enumerate(eco_news_pool[:st.session_state.kr_news_limit]):
                    st.markdown(f"{idx+1}. [{n['title']}]({n['link']})")
                if len(eco_news_pool) > st.session_state.kr_news_limit:
                    if st.button("🔽 6개 더보기", key="more_kr", use_container_width=True):
                        st.session_state.kr_news_limit += 6
                        st.rerun()
            else:
                st.info("수집된 국내 거시 뉴스가 없습니다.")

    with c_rep_news:
        with st.container(border=True):
            st.markdown("🔥 **당일 신규 애널리스트 리포트**")
            if today_active_reps:
                st.success(f"오늘자 신규/수정 리포트 **{len(today_active_reps)}건**")
                for idx, r in enumerate(today_active_reps[:6]):
                    st.markdown(f"- **{r['stock_name']}** ({r['broker']})<br>&nbsp;&nbsp;<span style='font-size:0.9em; color:gray;'>{r['title']}</span>", unsafe_allow_html=True)
            else:
                st.info("오늘 자 신규 리포트 없음 (최근 100% 리포트 유니버스 자동 대기 중)")

    st.divider()

    st.markdown("### 🎯 초강력 국면 융합형 모닝 핫브리핑")
    st.caption("단순 뉴스 나열을 넘어, 파이썬 퀀트 엔진의 목표가(tp_m)와 애널리스트 컨센서를 정면으로 비교/교차검증하여 시장을 관통하는 핫픽 2종목을 도출합니다.")
    
    btn_cols = st.columns([1, 4, 1])
    with btn_cols[1]:
        run_morning = st.button("🚀 당일 퀀트-모멘텀 통합 모닝 핫브리핑 가동", use_container_width=True, type="primary")

    if run_morning:
        if not analyst_universe:
            st.error("람다 파일 시스템에서 리포트 유니버스 DB를 로드하지 못했습니다.")
            st.stop()
            
        with st.spinner("1단계: 시황 뉴스 심층 분석 및 매크로 국면 압축 중..."):
            all_news_text = "== 미국 증시/빅테크 ==\n" + "\n".join([f"- 제목: {n['title']}\n  내용: {n.get('summary', '')}" for n in us_news_pool[:15]])
            all_news_text += "\n== 국내 매크로/증시 ==\n" + "\n".join([f"- 제목: {n['title']}\n  내용: {n.get('summary', '')}" for n in eco_news_pool[:15]])
            all_news_text += "\n== 실시간 긴급 속보 ==\n" + "\n".join([f"- 제목: {n['title']}\n  내용: {n.get('summary', '')}" for n in realtime_news_pool[:15]])
            
            macro_prompt = (f"아래 뉴스 스트림과 실시간 지표를 종합하여 오늘 장을 지배할 '3가지 핵심 매크로 이슈'와 "
                            f"'주도 섹터에 미칠 파급 효과'를 매우 구체적이고 전문적인 리서치 수준으로 풍부하게 작성하라:\n{all_news_text}")
            lite_macro_summary = call_gemini_lite_summary(macro_prompt)

        with st.spinner("2단계: 퀀트 엔진 가동 및 컨센서스 목표가 교차 검증 중... (약 10~15초 소요)"):
            target_pool = today_active_reps if today_active_reps else list(analyst_universe.values())[:30]
            tickers_to_process = []
            for r in target_pool:
                t = r.get("ticker")
                if not t: t, _ = search_stock_code(r["stock_name"])
                if t and t not in tickers_to_process: tickers_to_process.append(t)
                if len(tickers_to_process) >= 10: break

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(process_single_ticker, t, "중기 (3~6개월)", k_factor, False, analyst_universe) for t in tickers_to_process]
                valid_results = [f.result() for f in concurrent.futures.as_completed(futures) if f.result()]

            tech_data_str_all = "".join([r['tech_data_str'] for r in valid_results])

        with st.spinner("3단계: 최종 모닝 보고서 컴파일 중..."):
            prompt = (
                f"당신은 엄격한 숫자의 지배를 받는 대형 자산운용사 리서치 센터장입니다. '미표기'나 데이터 누락은 절대 용납하지 않습니다.\n"
                f"[거시 경제 및 미국 시황 요약 팩트]:\n{lite_macro_summary}\n\n"
                f"[파이썬 퀀트 엔진 연산 및 교차검증 로그]:\n{tech_data_str_all}\n\n"
                f"=== ⚠️ 작성 지침 및 데이터 스펙 ===\n"
                f"1. 제공된 [매크로 요약 팩트]를 바탕으로 오늘 한국 증시의 자산 배분 방안과 주도 섹터 흐름을 매우 상세하고 풍부하게 서술하십시오.\n"
                f"2. 제공된 [퀀트 엔진 로그]를 분석하여 가장 매력적인 핫픽 2종목을 엄선하십시오.\n"
                f"3. 핫픽 종목 작성 시 로그에 찍힌 **[현재가], [증권사 목표가 및 트렌드], [우리의 시스템 퀀트 중기 목표가(tp_m)]**를 반드시 모두 숫자로 명시하십시오.\n"
                f"4. 두 목표가 간의 괴리율(+-5% 기준)을 바탕으로 수렴/과대평가 여부를 객관적으로 평가하십시오.\n"
                f"5. 강세/약세 논리 서술 시 차트 데이터(이평선)와 밸류에이션(EPS, PER) 숫자를 철저히 인용하십시오.\n"
                f"6. 브리핑 결과를 바탕으로 오늘의 종합 시장 심리 수치(0~100)를 산출하여 반드시 마지막에 [SENTIMENT_SCORE]: 50 형식으로 출력할 것.\n\n"
                f"=== 리포트 출력 포맷 ===\n"
                f"## 📰 1. 글로벌 매크로 국면 및 주도 섹터 심층 브리핑\n"
                f"(실시간 지표와 매크로 뉴스를 연결하여 오늘 장의 성격 규정, 리스크 요인, 기회 요인을 구체적이고 길게 서술)\n\n"
                f"## 🎯 2. 당일 기관 퀀트-모멘텀 핫픽 (Top 2)\n"
                f"<ANALYSIS_종목명>\n"
                f"### 📌 [종목명] ([티커])\n"
                f"- **주가 현황:** 현재가 [숫자]원\n"
                f"- **목표가 교차검증:** 애널리스트 목표가 **[숫자]원 (모멘텀: [트렌드])** vs 퀀트 시스템 적정가 **[숫자]원**\n"
                f"- **거시 국면 연결고리:** (이 종목이 왜 지금 매크로 상태에서 촉매를 받는지 서술)\n"
                f"- **펀더멘털 및 퀀트 강세 논리:** (파이썬 로그의 PER, BPS, 기술적 이격도 수치 인용)\n"
                f"</ANALYSIS_종목명>\n\n"
                f"※ 주의: 마지막 줄은 반드시 선정된 2개 종목의 '6자리 숫자 티커만' 콤마로 구분하여 아래 형식으로 출력하십시오. 종목명이나 괄호는 절대 쓰지 마십시오.\n"
                f"[SELECTED_TICKERS]: 000000, 111111\n"
                f"[SENTIMENT_SCORE]: (점수)"
            )
            full_report = "".join(call_gemini_stream_with_fallback(prompt))
            clean_report_for_regex = full_report.replace('*', '').replace('#', '')
            if score_match := re.search(r'\[SENTIMENT_SCORE\]\s*:\s*(\d+)', clean_report_for_regex):
                c.execute("INSERT INTO sentiment_history (calc_date, score) VALUES (?, ?)", (datetime.now().strftime("%Y-%m-%d"), float(score_match.group(1))))
                conn.commit()
            st.session_state.morning_report = re.sub(r'\[SENTIMENT_SCORE\].*', '', full_report, flags=re.DOTALL).strip()
            st.rerun()

    if st.session_state.get('morning_report'):
        raw_report = st.session_state.morning_report
        display_text = re.sub(r'</?ANALYSIS_[^>]+>', '', raw_report.split("[SELECTED_TICKERS]")[0].strip())
        with st.container(border=True):
            st.markdown(display_text)

with tab3:
    st.subheader("섹터별 모멘텀 분석")
    sec_news = g_data.get("sectors") or cached_data.get("sectors") or {}
    
    if sec_news:
        for sec, items in sec_news.items():
            if not items: continue
            
            page_key = f"page_{sec}"
            if page_key not in st.session_state:
                st.session_state[page_key] = 10
                
            with st.expander(f"📁 {sec} ({len(items)}건)"):
                current_limit = st.session_state[page_key]
                for i in items[:current_limit]:
                    score_badge = f"⭐{i.get('score', 0)}점" if 'score' in i else ""
                    st.markdown(f"- {score_badge} [{i['title']}]({i.get('link', '#')})")
                
                col_m1, col_m2 = st.columns(2)
                with col_m1:
                    if len(items) > current_limit:
                        if st.button("🔽 다음 10개 더보기", key=f"more_{sec}"):
                            st.session_state[page_key] += 10
                            st.rerun()
                with col_m2:
                    if st.button(f"🧠 상위 30개 핵심 분석", key=f"sec_{sec}"):
                        with st.spinner("상위 30개 핵심 뉴스 요약 중..."):
                            top_30_titles = "\n".join([i['title'] for i in items[:30]])
                            l_sum = call_gemini_lite_summary(f"아래 상위 30개 뉴스를 바탕으로 {sec} 섹터의 핵심 모멘텀을 요약하라:\n{top_30_titles}")
                            st.write(call_gemini_with_fallback(f"[{sec} 요약]\n{l_sum}\n\n위 요약을 바탕으로 해당 섹터의 주도주 흐름과 향후 모멘텀을 심층 분석하라."))

with tab4:
    st.subheader("종목 발굴 (시니어 애널리스트 퀀트 분석)")
    investment_horizon = st.radio("투자기간", ["단기 (1~3개월)", "중기 (3~6개월)", "장기 (1년 이상)"], horizontal=True)

    if st.button("추천 종목 발굴", use_container_width=True, key="btn_recommend"):
        t_start = time.time()

        all_raw_news = (g_data.get("realtime_news", []) if g_data else []) + (cached_data.get("eco_news", []) if cached_data else [])
        sec_data = g_data.get("sectors") or cached_data.get("sectors") or {}
        for sec, items in sec_data.items():
            all_raw_news.extend(items[:30])

        rec_news = dedupe_news(all_raw_news)

        if not rec_news:
            st.error("분석 대상 뉴스 풀이 비어있습니다.")
        else:
            with st.spinner("[1단계] Lite 모델이 수집된 핵심 뉴스를 바탕으로 시장 전체 모멘텀을 추출 중..."):
                t1 = time.time()
                news_str = "\n".join([f"- {n.get('title', '')}: {n.get('summary', '')}" for n in rec_news])
                momentum_context = call_gemini_lite_summary(
                    f"다음은 현재 시장의 실시간, 거시경제, 섹터별 핵심 뉴스 목록이다. "
                    f"이 중 시장을 지배하는 핵심 테마 3~5개만 골라, 각 테마당 2~3문장으로 "
                    f"핵심 근거와 관련 종목/섹터를 요약하라. 전체 800자를 넘기지 마라:\n\n{news_str}"
                )
                print(f"[TIMING] 뉴스풀 {len(rec_news)}건 / Lite 모멘텀 추출: {time.time()-t1:.1f}초")

            with st.spinner("[2단계] 추출된 모멘텀 기반으로 1차 후보군 발굴 중..."):
                t2 = time.time()
                prompt = f"투자 [{investment_horizon}] 모멘텀 수혜가 예상되는 종목 15개의 '종목명'만 JSON 배열로 출력하라.\n\n[시장 모멘텀 분석]\n{momentum_context}\n\n※ 다른 설명 없이 [\"삼성전자\", \"현대차\"] 형태의 배열만 출력하시오."
                res = call_gemini_with_fallback(prompt, model=LITE_MODEL_NAME)
                print(f"[TIMING] 1차 종목 추출: {time.time()-t2:.1f}초")

                selected_tickers = []
                try:
                    match = re.search(r'\[(.*?)\]', res, re.DOTALL)
                    if match:
                        names = json.loads(f"[{match.group(1)}]")
                        for name in names:
                            code, _ = search_stock_code(name.strip())
                            if code and code not in selected_tickers:
                                selected_tickers.append(code)
                            if len(selected_tickers) >= 10: break
                except: pass

            if not selected_tickers:
                st.error("⚠️ AI가 조건에 맞는 종목을 추출하지 못했거나 API 응답이 지연되었습니다. 잠시 후 다시 시도해주세요.")
                st.stop()

            with st.spinner("[3단계] 후보군 동시 병렬 크롤링 및 리스크/목표가 밴드 산출 중..."):
                t3 = time.time()
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(process_single_ticker, t, investment_horizon, k_factor, True, analyst_universe) for t in selected_tickers]
                    results = [f.result() for f in concurrent.futures.as_completed(futures) if f.result()]
                    valid_results = results
                print(f"[TIMING] 3단계 병렬 크롤링({len(selected_tickers)}종목): {time.time()-t3:.1f}초")

            tried_tickers = set(selected_tickers)
            max_retry = 2
            retry_count = 0

            while len(valid_results) < 10 and retry_count < max_retry:
                with st.spinner(f"[보충 단계] 부족분 보충 중 (시도 {retry_count+1}/{max_retry})..."):
                    t_retry = time.time()
                    deficit = 10 - len(valid_results)
                    extra_prompt = f"다음 코드를 제외하고, 투자 [{investment_horizon}] 모멘텀 수혜 예상 종목 {deficit}개의 '종목명'만 JSON 배열로 출력하라.\n(제외: {', '.join(tried_tickers)})\n\n[시장 모멘텀 분석]\n{momentum_context}\n\n※ 다른 설명 없이 [\"SK하이닉스\", \"LG에너지솔루션\"] 형태의 배열만 출력하시오."
                    extra_res = call_gemini_with_fallback(extra_prompt, model=LITE_MODEL_NAME)

                    extra_tickers = []
                    try:
                        ex_match = re.search(r'\[(.*?)\]', extra_res, re.DOTALL)
                        if ex_match:
                            names = json.loads(f"[{ex_match.group(1)}]")
                            for name in names:
                                code, _ = search_stock_code(name.strip())
                                if code and code not in tried_tickers and code not in extra_tickers:
                                    extra_tickers.append(code)
                                if len(extra_tickers) >= deficit: break
                    except: pass

                    if not extra_tickers: break
                    tried_tickers.update(extra_tickers)

                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                        futures = [executor.submit(process_single_ticker, t, investment_horizon, k_factor, True, analyst_universe) for t in extra_tickers]
                        extra_results = [f.result() for f in concurrent.futures.as_completed(futures) if f.result()]
                        valid_results += extra_results
                    retry_count += 1
                    print(f"[TIMING] 보충 {retry_count}회차: {time.time()-t_retry:.1f}초")

            if len(valid_results) == 0:
                st.warning("⚠️ 2회 재시도 보충을 진행했으나, 후보군 전부 밸류에이션상 상승여력이 없어 추천에서 제외되었습니다.")
                st.session_state.today_recommendation = ""
                st.session_state.valid_results_cache = []
            else:
                tech_data_str_all = "".join([r['tech_data_str'] for r in valid_results])
                st.session_state.valid_results_cache = valid_results

                with st.spinner(f"[4단계] 최종 선별된 {len(valid_results)}개 중 시니어 애널리스트 방식 Top 3 보고서 작성 중..."):
                    t4 = time.time()
                    
                    # [핵심 패치] 투자 기간에 따른 평가 지침을 동적으로 변경하여 AI의 채점 기준을 통제
                    if "단기" in investment_horizon:
                        strategy_guide = "▶ [단기(1~3개월) 집중 전략]: 장기 펀더멘털보다 **실시간 뉴스 테마 모멘텀, 최근 5일 수급(외인/기관), 차트 기술적 지표(20일선, MACD, 변동성)**를 최우선 가중치로 평가하십시오. 장기 지표는 배제하고 단기 목표가 달성 가능성에 집중하십시오."
                    elif "중기" in investment_horizon:
                        strategy_guide = "▶ [중기(3~6개월) 집중 전략]: **분기별 실적 턴어라운드(매출/영업이익 추세)와 밸류에이션(PER 괴리율)**을 최우선 가중치로 평가하십시오. 단기 노이즈를 걸러내고 60일선 추세와 20일 누적 수급을 핵심 근거로 삼으십시오."
                    else:
                        strategy_guide = "▶ [장기(1년 이상) 집중 전략]: 단기적 수급이나 차트 기술적 노이즈는 완전히 무시하십시오. 오직 **본질적 가치(BPS), 자본효율성(ROE), 구조적 산업 성장성, 그리고 장기 RIM(잔여이익모델) 적정가**만을 바탕으로 기업의 펀더멘털을 평가하십시오."

                    step3_prompt = (
                        f"당신은 월스트리트 헤지펀드의 시니어 퀀트 애널리스트입니다.\n"
                        f"고객의 투자 기간은 **{investment_horizon}**입니다.\n\n"
                        f"[시장 전체 핵심 모멘텀 분석]\n{momentum_context}\n\n"
                        f"[후보군 팩트 데이터(실적 추세, 최근 20일 수급 동향, 밸류에이션, 차트)]\n{tech_data_str_all}\n\n"
                        f"=== ⚠️ 시니어 애널리스트 분석 지침 ===\n"
                        f"1. [투자기간 맞춤형 평가 - 핵심] {strategy_guide}\n"
                        f"2. [전수 평가 및 선별] 전달받은 전체 후보군 데이터를 모두 비교 평가하여 각각의 '종합 매력도 점수'를 매기십시오. 그 후 가장 점수가 높은 **최상위 Top 3 종목만 엄선**하여 심층 리포트를 작성하십시오.\n"
                        f"3. [집중 전략 준수] 사용자가 설정한 기간에 따른 집중 전략을 준수하여 리포트를 작성하십시오.
                        f"4. [숫자 일치 강제 - 매우 중요] 리포트에 기재하는 모든 가격(현재가, 목표가, 손절가 등)과 실적 수치는 **'파이썬 퀀트 엔진 연산 로그'에 찍힌 숫자를 절대 반올림하거나 가공하지 말고 1원 단위까지 똑같이 복사해서 기재**하십시오.\n"
                        f"5. [스토리텔링] 파편화된 데이터(뉴스, 수급, 밸류에이션, 실적 추이)를 단순 나열하지 말고, **하나의 투자 논리(Investment Story)**로 연결하여 서술하십시오.\n"
                        f"6. [추세 변화] 실적(매출/영업이익)의 분기별 증감 추세와 외국인/기관의 '최근 5일 vs 20일 수급 변화'를 바탕으로 모멘텀을 분석하십시오.\n"
                        f"7. [자기 검증] 스스로 도출한 결론이 틀릴 가능성(반대 논리 및 Value Trap 리스크) 3가지를 반드시 서술하십시오.\n"
                        f"8. [액션 플랜] 마지막에 투자 의견, 매수가/목표가/손절가, 향후 확인해야 할 이벤트를 반드시 요약하십시오.\n\n"
                        f"=== 리포트 작성 포맷 ===\n"
                        f"### 🏆 1차 후보군 스크리닝 요약 (전체 평가)\n"
                        f"(검토한 전체 후보군의 종목명, 매력도 점수, 핵심 이유 1줄을 간단한 표나 리스트로 먼저 제시할 것)\n\n"
                        f"---\n\n"
                        f"<ANALYSIS_티커숫자>\n"
                        f"### [종목명] (티커)\n"
                        f"**📖 핵심 투자 스토리 (Investment Story)**\n"
                        f"(수급, 실적 추세, 뉴스, 밸류에이션을 하나로 엮은 심층 분석)\n\n"
                        f"**📊 실적 및 수급 추세 분석**\n"
                        f"(분기별 성장성 및 기관/외인 수급 변화 분석)\n\n"
                        f"**🛑 AI 자기검증: 이 분석이 틀릴 가능성 3가지 (Bear Case)**\n"
                        f"1. \n2. \n3. \n\n"
                        f"---\n"
                        f"**🏁 최종 투자 의사결정 (Action Plan)**\n"
                        f"- **현재 투자의견:** [매수 / 관망 / 비중축소] (신뢰도: 00%)\n"
                        f"- **적정 매수가:** [000원 이하]\n"
                        f"- **목표가:** [단기 00원 / 중기 00원]\n"
                        f"- **손절가:** [00원]\n"
                        f"- **의사결정 핵심 근거 Top 3:** (1, 2, 3)\n"
                        f"- **향후 반드시 확인해야 할 트리거 이벤트:** (예: 다음 분기 영업이익률 회복 여부 등)\n"
                        f"</ANALYSIS_티커숫자>\n\n"
                        f"※ 주의: 마지막 줄은 반드시 아래 형식으로 선정된 3개 종목의 '6자리 숫자 티커만' 콤마로 구분하여 출력하십시오. 종목명이나 괄호는 절대 쓰지 마십시오.\n"
                        f"[SELECTED_TICKERS]: 000000, 111111, 222222"
                    )
                    st.session_state.today_recommendation = "".join(call_gemini_stream_with_fallback(step3_prompt))
                    print(f"[TIMING] 4단계 최종 리포트: {time.time()-t4:.1f}초")

            print(f"[TIMING] 종목 발굴 전체 누적: {time.time()-t_start:.1f}초")

    if st.session_state.get('today_recommendation'):
        raw = st.session_state.today_recommendation
        cached_results = st.session_state.get('valid_results_cache', [])

        with st.expander("추천 리포트"):
            display_text = re.sub(r'</?ANALYSIS_[^>]+>', '', raw.split("[SELECTED_TICKERS]")[0].strip())
            st.write(display_text)

            if "[SELECTED_TICKERS]" in raw:
                match = re.search(r'\[SELECTED_TICKERS\]\s*:\s*(.*)', raw)
                if match:
                    selected_ticks = re.findall(r'\b\d{6}\b', match.group(1))
                    selected_ticks = list(dict.fromkeys(selected_ticks))[:3]
                    
                    price_map = fetch_current_prices(selected_ticks)
                    cols_rec = st.columns(3)

                    for idx, tick in enumerate(selected_ticks):
                        tick_data = next((r for r in cached_results if r['ticker'] == tick), None)
                        if not tick_data: continue

                        name = tick_data['name']
                        tp_s, tp_m, tp_l = tick_data['tp_s'], tick_data['tp_m'], tick_data['tp_l']
                        sl_s, sl_m, sl_l = tick_data['sl_s'], tick_data['sl_m'], tick_data['sl_l']

                        price_info = price_map.get(tick, {})
                        current, diff, diff_pct = price_info.get("current", 0.0), price_info.get("diff", 0.0), price_info.get("diff_pct", 0.0)

                        with cols_rec[idx % 3]:
                            with st.container(border=True):
                                st.markdown(f"**{name}** `{tick}`")
                                if current > 0: st.metric("현재가", f"{current:,.0f}", delta=f"{diff:+,.0f} ({diff_pct:+.2f}%)")
                                c_tp, c_bp = st.columns(2)
                                c_tp.markdown(f"**목표가 밴드**<br>단: {tp_s:,.0f}<br>중: {tp_m:,.0f}<br>장: {tp_l:,.0f}", unsafe_allow_html=True)
                                c_bp.markdown(f"**손절가 라인**<br>단: <span style='color:red;'>{sl_s:,.0f}</span><br>중: <span style='color:red;'>{sl_m:,.0f}</span>", unsafe_allow_html=True)

                                if st.button("스크랩", key=f"rec_s_{tick}", use_container_width=True):
                                    analysis_match = re.search(f"<ANALYSIS_{tick}>(.*?)</ANALYSIS_{tick}>", raw, re.DOTALL)
                                    specific_analysis = analysis_match.group(1).strip() if analysis_match else display_text

                                    c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, sl_s, sl_m, sl_l, scrap_date, model_used, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                              (f"{name} 퀀트 심층분석", specific_analysis, name, tick, current, tp_s, tp_m, tp_l, current, sl_s, sl_m, sl_l, datetime.now().strftime("%Y-%m-%d %H:%M"), MODEL_NAME, current_user))
                                    conn.commit()
                                    st.success(f"✅ 리포트 스크랩 완료!")

with tab5:
    st.subheader("관심종목 진단 (보유종목 정밀 평가)")
    
    # [핵심 패치] 내 관심종목도 단/중/장기 어떤 관점으로 평가받을지 선택할 수 있도록 추가
    eval_horizon = st.radio("진단 관점 (투자기간)", ["단기 (1~3개월)", "중기 (3~6개월)", "장기 (1년 이상)"], horizontal=True)
    st.divider()

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
        analyst_universe = cached_data.get("analyst_universe", {})

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
                        data_dict = process_single_ticker(ticker, eval_horizon, k_factor, False, analyst_universe)
                        if not data_dict:
                            st.error("데이터 수집 실패")
                            continue
                            
                        extra_ctx = f"\n현재가: {current:,.0f}\n"
                        if is_owned and avg_price > 0: extra_ctx += f"[내 계좌 정보] 평단가: {avg_price:,.0f} | 현재 수익률: {((current - avg_price) / avg_price * 100):+.1f}%\n"

                        if "단기" in eval_horizon:
                            t5_strategy = "▶ [단기(1~3개월) 집중 전략]: 펀더멘털보다 **수급(최근 5일), 차트 기술적 지표(20일선, MACD), 단기 뉴스 모멘텀**을 최우선으로 평가하십시오. 현재 가격이 단기 손절선에 근접했는지 철저히 경고하십시오."
                        elif "중기" in eval_horizon:
                            t5_strategy = "▶ [중기(3~6개월) 집중 전략]: **분기 실적 추세(영업이익/EPS)와 밸류에이션(PER)**의 조화를 중시하고, 20일 누적 기관/외인 수급 이탈 여부를 확인하십시오."
                        else:
                            t5_strategy = "▶ [장기(1년 이상) 집중 전략]: 단기 수급/차트는 무시하고 오직 **장기 RIM 적정가, BPS, ROE 추세, 구조적 산업 성장성** 등 펀더멘털만 집중 평가하십시오."
                        
                        prompt = (f"[{name} 진단]\n[팩트 데이터]\n{data_dict['tech_data_str']}\n{extra_ctx}\n\n"
                                  f"당신은 리스크와 기회를 종합적으로 분석하는 전문 퀀트 애널리스트입니다.\n"
                                  f"고객의 투자 타임라인은 **{eval_horizon}**입니다.\n\n"
                                  f"=== ⚠️ AI 분석 지침 ===\n"
                                  f"1. [투자기간 맞춤 진단] {t5_strategy}\n"
                                  f"2. [숫자 일치 강제] 리포트에 기재하는 모든 수치는 **'팩트 데이터'에 찍힌 숫자를 절대 반올림하지 말고 1원 단위까지 똑같이 복사해서 기재**하십시오.\n"
                                  f"3. [스토리텔링] 파편화된 데이터를 나열하지 말고, **하나의 투자 논리(Investment Story)**로 연결하여 서술하십시오.\n"
                                  f"4. [계좌 진단] 계좌 수익률을 참고하여 '추가매수/유지/손절' 여부를 객관적으로 판단하십시오.\n"
                                  f"5. [자기 검증] 스스로 도출한 결론이 틀릴 가능성(반대 논리 및 Value Trap 리스크) 3가지를 반드시 서술하십시오.\n"
                                  f"6. [액션 플랜] 마지막에 투자 의견, 매수가/목표가/손절가, 향후 확인해야 할 이벤트를 요약하십시오.\n\n"
                                  f"=== 리포트 작성 포맷 ===\n"
                                  f"**📖 핵심 투자 스토리 (Investment Story)**\n"
                                  f"(수급, 실적 추세, 뉴스, 밸류에이션을 하나로 엮은 심층 분석)\n\n"
                                  f"**📊 실적 및 수급 추세 분석**\n"
                                  f"(분기별 성장성 및 기관/외인 수급 변화 분석)\n\n"
                                  f"**🛑 AI 자기검증: 이 분석이 틀릴 가능성 3가지 (Bear Case)**\n"
                                  f"1. \n2. \n3. \n\n"
                                  f"---\n"
                                  f"**🏁 최종 투자 의사결정 (Action Plan)**\n"
                                  f"- **현재 투자의견:** [매수 / 유지 / 비중축소 / 손절] (신뢰도: 00%)\n"
                                  f"- **적정 매수가:** [000원 이하]\n"
                                  f"- **목표가:** [단기 00원 / 중기 00원 / 장기 00원]\n"
                                  f"- **시스템 손절가:** [00원]\n"
                                  f"- **의사결정 핵심 근거 Top 3:** (1, 2, 3)\n"
                                  f"- **향후 반드시 확인해야 할 트리거 이벤트:** (예: 다음 분기 영업이익률 회복 여부 등)\n")
                        
                        report = call_gemini_with_fallback(prompt)
                        
                        n_tp_s, n_tp_m, n_tp_l = data_dict['tp_s'], data_dict['tp_m'], data_dict['tp_l']
                        n_sl_s, n_sl_m, n_sl_l = data_dict['sl_s'], data_dict['sl_m'], data_dict['sl_l']

                        c.execute("UPDATE portfolio SET report_text=?, tp_s=?, tp_m=?, tp_l=?, bp=?, sl_s=?, sl_m=?, sl_l=?, model_used=?, report_time=? WHERE id=?", 
                                  (report, n_tp_s, n_tp_m, n_tp_l, current, n_sl_s, n_sl_m, n_sl_l, MODEL_NAME, datetime.now().strftime("%Y-%m-%d %H:%M"), p_id))
                        conn.commit(); st.rerun()
            with col_del:
                if st.button("개별 삭제", key=f"del_t5_{p_id}", use_container_width=True):
                    c.execute("DELETE FROM portfolio WHERE id=?", (p_id,))
                    conn.commit(); st.rerun()

            if report_text:
                with st.expander("진단 리포트"):
                    st.write(report_text)
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
                    
                    st.markdown("---")
                    st.write(analysis)
                    
                    if st.button("개별 삭제", key=f"del_t6_{s_id}", use_container_width=True):
                        c.execute("DELETE FROM scrapbook WHERE id=?", (s_id,))
                        conn.commit(); st.rerun()
    else:
        st.info("저장된 분석 리포트가 없습니다.")
