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
import zipfile
import xml.etree.ElementTree as ET
import concurrent.futures
from datetime import datetime
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google import genai
import streamlit.components.v1 as components

# =======================================================
# 설정 및 모델
# =======================================================
MODEL_NAME = "gemini-3.5-flash"
LITE_MODEL_NAME = "gemini-3.1-flash-lite"
FALLBACK_MODEL_NAME = "gemini-3-flash-preview"

db_backup_lock = threading.Lock()
db_schema_lock = threading.Lock() 
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
    with db_schema_lock: 
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
            ("portfolio", "user_id", "TEXT DEFAULT 'dongsu'"), ("scrapbook", "user_id", "TEXT DEFAULT 'dongsu'"),
            ("portfolio", "risk_flags", "INTEGER DEFAULT -1"), ("scrapbook", "risk_flags", "INTEGER DEFAULT -1")
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
    res = [row[0] for row in local_c.fetchall()]
    local_conn.close()
    return res

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
# 신뢰도 자가 튜닝 인프라 (스크랩북 실측 데이터 평가)
# =======================================================
@st.cache_data(ttl=3600)
def get_calibrated_confidence():
    try:
        local_conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
        local_c = local_conn.cursor()
        local_c.execute("SELECT risk_flags, saved_price, target_price, sl_s, scrap_date, ticker FROM scrapbook WHERE risk_flags >= 0")
        rows = local_c.fetchall()
        local_conn.close()
    except:
        return {}
    
    bucket = {}
    for rf, saved_p, tp_s, sl_s, s_date, ticker in rows:
        code = re.sub(r'[^\d]', '', ticker or "")
        max_high, min_low = get_historical_high_low(code, s_date)
        bucket.setdefault(rf, {"total": 0, "hit": 0})
        bucket[rf]["total"] += 1
        if tp_s > 0 and max_high >= tp_s: 
            bucket[rf]["hit"] += 1
    
    result = {}
    for rf, b in bucket.items():
        if b["total"] >= 5:   
            result[rf] = {
                "rate": round(b["hit"] / b["total"] * 100),
                "count": b["total"]
            }
    return result

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
    data = {
        "per": "-", "pbr": "-", "eps": None, "bps": None, "industry_per": "-", 
        "quarter_trend": "정보 없음", "supply_demand": "수급 정보 없음", 
        "sales_history": [], "op_history": [], "eps_history": [], "roe_history": [],
        "forward_eps_e": None, "forward_roe_e": None, "consensus_source": "Past Actual (A)"
    }
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
            data["quarter_trend"] = "최근 실적 및 컨센서스 테이블 파싱 완료"
            try:
                thead_tr = cop_table.find("thead").find("tr")
                headers_text = [th.get_text().strip() for th in thead_tr.find_all("th")] if thead_tr else []
                
                forward_idx = -1
                for idx, h_txt in enumerate(headers_text):
                    if "(E)" in h_txt:
                        forward_idx = idx
                
                tbody = cop_table.find("tbody")
                if tbody:
                    for tr in tbody.find_all("tr"):
                        th_title = tr.find("th").get_text().strip() if tr.find("th") else ""
                        tds = tr.find_all("td")
                        td_values = [td.get_text().strip().replace(',', '') for td in tds]
                        
                        valid_nums = [float(v) for v in td_values if v and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        
                        if "매출액" in th_title:
                            if valid_nums: data["sales_history"] = valid_nums[-3:]
                        elif "영업이익" in th_title:
                            if valid_nums: data["op_history"] = valid_nums[-3:]
                        elif "EPS" in th_title:
                            if valid_nums:
                                if data["eps"] is None: data["eps"] = valid_nums[-1]
                                data["eps_history"] = valid_nums[-3:]
                            if forward_idx != -1 and len(td_values) >= forward_idx:
                                target_v = td_values[forward_idx-1]
                                if target_v and target_v.replace('.', '', 1).replace('-', '', 1).isdigit():
                                    data["forward_eps_e"] = float(target_v)
                                    data["consensus_source"] = f"Forward Estimate (E) Column Match"
                        elif "BPS" in th_title:
                            if valid_nums:
                                data["bps"] = valid_nums[-1]
                        elif "ROE" in th_title:
                            if valid_nums: data["roe_history"] = valid_nums[-3:]
                            if forward_idx != -1 and len(td_values) >= forward_idx:
                                target_v = td_values[forward_idx-1]
                                if target_v and target_v.replace('.', '', 1).replace('-', '', 1).isdigit():
                                    data["forward_roe_e"] = float(target_v)
            except Exception as e:
                pass
            
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
    lite_summary = call_gemini_lite_summary(f"[{name}] 관련 기업 공시 및 뉴스 정보 요약:\n{dart_info}\n{news_text}")
    
    current_price = tech['current'] if tech else 0.0
    daily_vol = tech['daily_volatility'] if tech else 0.0
    beta = tech['beta'] if tech and 'beta' in tech else 1.0
    
    eps_val = fund.get('forward_eps_e') if fund.get('forward_eps_e') is not None else fund.get('eps')
    bps_val = fund.get('bps')
    
    if fund.get('forward_roe_e') is not None:
        expected_roe = fund.get('forward_roe_e') / 100
    else:
        roe_history = fund.get('roe_history', [])
        expected_roe = (roe_history[-1] / 100) if roe_history else 0.05
        
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
        else:
            fund_type = "기본 상대 가치 (업종 평균 수렴)"
            tp_m = eps_val * adjusted_ind_per
            
        required_return = rf + (beta * 0.06) 
        tp_l = bps_val + (bps_val * (expected_roe - required_return) / required_return)
        fund_type += f" | 장기 RIM(Rf {rf*100:.1f}%, Beta {beta:.2f})"
        conservative_bps = bps_val
        data_incomplete = False

    # =======================================================
    # [3x3 결합 트렌드 매트릭스 & Tier 필터 - 동적 실적 계산 연동]
    # =======================================================
    tp_trend = "유지/신규"
    
    if analyst_data:
        matched_report = next((rep for rep in analyst_data.values() if rep.get("ticker") == ticker), None)
        if matched_report:
            tp_trend = matched_report.get("tp_trend", "유지/신규")

    eps_e_trend = "유지"
    if len(eps_history) >= 2:
        if eps_history[-1] > eps_history[-2] * 1.05:
            eps_e_trend = "상향"
        elif eps_history[-1] < eps_history[-2] * 0.95:
            eps_e_trend = "하향"

    if "상향" in tp_trend:
        if "상향" in eps_e_trend: cross_signal, tier, penalty = "True Bull (목표가 상향 + 실적 상향 ➡️ 펀더멘털과 가격이 동행하는 건전한 성장)", "Tier 1 (Pass)", 0
        elif "하향" in eps_e_trend: cross_signal, tier, penalty = "Critical Value Trap (목표가 상향 + 실적 하향 ➡️ 실적은 꺾이는데 목표가만 높은 위험한 거품)", "Tier 3 (Fatal)", -20
        else: cross_signal, tier, penalty = "Momentum Driven (목표가 상향 + 실적 유지 ➡️ 실적 상향 없이 가격만 오르는 수급 주도)", "Tier 1 (Pass)", -5
    elif "하향" in tp_trend:
        if "상향" in eps_e_trend: cross_signal, tier, penalty = "Hidden Turnaround (목표가 하향 + 실적 상향 ➡️ 주가는 빠지지만 실적 체력은 오르는 소외된 우량주)", "Tier 1 (Pass)", 10
        elif "하향" in eps_e_trend: cross_signal, tier, penalty = "Genuine Bear (목표가 하향 + 실적 하향 ➡️ 이익과 가격이 모두 부러진 구조적 침체)", "Tier 3 (Fatal)", 0
        else: cross_signal, tier, penalty = "Sentiment Driven Drop (목표가 하향 + 실적 유지 ➡️ 실적 하향은 멈췄으나 심리적 과매도로 하락)", "Tier 2 (Warning)", 0
    else:
        if "상향" in eps_e_trend: cross_signal, tier, penalty = "Quiet Accumulation (목표가 유지 + 실적 상향 ➡️ 시장은 조용하나 실적 추정치가 개선되는 선취매 구간)", "Tier 1 (Pass)", 5
        elif "하향" in eps_e_trend: cross_signal, tier, penalty = "Hidden Value Trap (목표가 유지 + 실적 하향 ➡️ 목표가는 방어 중이나 이익은 몰래 하향 중인 눈치보기 장세)", "Tier 2 (Warning)", -10
        else: cross_signal, tier, penalty = "Neutral (목표가 유지 + 실적 유지 ➡️ 뚜렷한 방향성 없음)", "Tier 1 (Pass)", 0

    if is_discovery_mode and tier == "Tier 3 (Fatal)":
        return {"ticker": ticker, "name": name, "status": "KILLED", "reason": f"{cross_signal} ({tier} 치명적 리스크 제어 작동)"}

    if is_discovery_mode:
        if data_incomplete: pass 
        elif is_holding:
            if current_price > 0 and (tp_m <= current_price or tp_l <= current_price): return None
        elif eps_val <= 0:
            if current_price > 0 and conservative_bps < current_price: return None
        else:
            if current_price > 0 and (tp_m <= current_price or tp_l <= current_price): return None 

    # =======================================================
    # [시스템 결정론적 신뢰도 점수 및 역전 플래그 산출]
    # =======================================================
    is_flag_m_inv = (tp_s > tp_m and tp_m > 0)
    is_flag_l_inv = (tp_m > tp_l and tp_l > 0)
    
    flag_m = f"⚠️역전됨 (단기 모멘텀 {tp_s:,.0f}원 대비 중기 가치가 낮음)" if is_flag_m_inv else "정상"
    flag_l = f"⚠️역전됨 (중기 가치 대비 장기 RIM 가치({tp_l:,.0f}원)가 낮음)" if is_flag_l_inv else "정상"

    # --------------------------------=======================----------------
    # [신규 추가] 파이썬 연산 단에서 구체적인 감점 사유 추적 텍스트 생성
    # --------------------------------=======================----------------
    confidence_reasons = []
    if tier == "Tier 3 (Fatal)": confidence_reasons.append("3x3 매트릭스 치명적 위험 침체 국면(Tier 3) 진입")
    elif tier == "Tier 2 (Warning)": confidence_reasons.append("3x3 매트릭스 밸류 트랩 및 심리 과매도 경고 국면(Tier 2) 진입")
    
    if is_flag_l_inv: confidence_reasons.append("장기 가치 선행 역전 현상 감지 (중기 가치 > 장기 RIM 내재가치)")
    if is_flag_m_inv: confidence_reasons.append("중기 모멘텀 역전 현상 감지 (단기 밴드 상단 > 중기 적정가)")
    if bool(structural_warning): confidence_reasons.append(f"구조적 데이터 밸류에이션 변형 제약 작동 ({structural_warning.replace('⚠️', '').strip()})")
    if data_incomplete: confidence_reasons.append("기업의 핵심 재무 팩트 데이터(EPS/BPS) 누락 발생")

    risk_flags = len(confidence_reasons)
    
    calibrated_map = get_calibrated_confidence()
    if risk_flags in calibrated_map:
        system_confidence = calibrated_map[risk_flags]["rate"]
        conf_str = f"{system_confidence}% (스크랩 {calibrated_map[risk_flags]['count']}건 기반 자동 갱신)"
    else:
        system_confidence = max(30, 90 - risk_flags * 15)
        conf_str = f"{system_confidence}% (예비 추정치)"

    reasons_str = " | ".join(confidence_reasons) if confidence_reasons else "없음 (최상위 안정 규격 충족)"

    struct_warn_line = f"   - 구조적 분석: {structural_warning}\n" if structural_warning else ""
    bps_disp_val = f"{bps_val:,.0f}원" if bps_val is not None else "데이터 누락"

    consensus_log = ""
    if analyst_data and matched_report:
        a_target = float(matched_report.get("target_price", 0))
        if a_target > 0 and tp_m > 0:
            divergence = (tp_m - a_target) / a_target
            consensus_log = f"   - ⚖️ 컨센서스 교차검증: 증권사 목표가 {a_target:,.0f}원({tp_trend}) vs 퀀트 중기 적정가 {tp_m:,.0f}원 (괴리율: {divergence*100:+.1f}%) ➡️ 판정 (페널티 계수: {penalty}%)\n"

    calc_result_log = (
        f"▶ 리스크 팩트 (k={user_k:.1f}): 단기손절 {sl_s:,.0f}원 | 중기손절 {sl_m:,.0f}원 | 장기손절 {sl_l:,.0f}원\n"
        f"▶ [최종 채택 목표가]\n"
        f"   - 단기 목표가: {tp_s:,.0f}원\n"
        f"   - 중기 목표가: {tp_m:,.0f}원\n"
        f"   - 장기 목표가: {tp_l:,.0f}원\n"
        f"▶ [퀀트 엔진 내부 검증 로그 (리스크 플래그)]:\n"
        f"   - 밸류에이션 모델 타입: {fund_type}\n"
        f"   - 데이터 소스 엔진: {fund.get('consensus_source')}\n"
        f"{struct_warn_line}"
        f"   - 중기 시그널 상태: {flag_m}\n"
        f"   - 장기 시그널 상태: {flag_l}\n"
        f"   - 🧭 3x3 트렌드 매트릭스 판정: {tier} | 시그널: {cross_signal}\n"
        f"   - 🧮 시스템 신뢰도 점수(파이썬 산출, 감점요인 {risk_flags}개): {conf_str}\n"
        f"   - 🧩 시스템 신뢰도 감점 내역: {reasons_str}\n"
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
        "tech_data_str": tech_data_str,
        "is_flag_m_inv": is_flag_m_inv,
        "is_flag_l_inv": is_flag_l_inv,
        "risk_flags": risk_flags,
        "status": "PASSED"
    }

# =======================================================
# 상태 변수 선언 및 상단 레이아웃 제어
# =======================================================
cached_data = fetch_cached_global_data() or {}

if "realtime_cache" not in st.session_state: 
    st.session_state.realtime_cache = {
        "market_status": cached_data.get("market_status") or {
            "코스피": {"current": 0.0, "diff": 0.0, "diff_pct": 0.0},
            "코스닥": {"current": 0.0, "diff": 0.0, "diff_pct": 0.0},
            "S&P 500": {"current": 0.0, "diff": 0.0, "diff_pct": 0.0},
            "원/달러 환율": {"current": 0.0, "diff": 0.0, "diff_pct": 0.0}
        }, 
        "realtime_news": cached_data.get("realtime_news", []),
        "sectors": cached_data.get("sectors") or cached_data.get("sector_news", {}), 
        "updated_at": cached_data.get("updated_at", "대기 중")
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

if not st.session_state.realtime_cache.get("realtime_news") or st.session_state.realtime_cache["market_status"]["코스피"]["current"] == 0.0:
    with st.spinner("데이터 인프라 초기화 중..."):
        if new_data := fetch_realtime_data_direct(): merge_realtime_data(new_data)

g_data = st.session_state.realtime_cache

# =======================================================
# 상단 레이아웃: 트레이딩뷰 실시간 웹소켓 티커 및 파이썬 요약 지표
# =======================================================
market_data = g_data.get("market_status", {})
metrics_html = ""

for key in ["코스피", "코스닥", "S&P 500", "원/달러 환율"]:
    if key in market_data:
        val = market_data[key].get("current", 0.0)
        diff = market_data[key].get("diff", 0.0)
        diff_pct = market_data[key].get("diff_pct", 0.0)
        if val:
            color = "red" if diff > 0 else "blue" if diff < 0 else "gray"
            sign = "+" if diff > 0 else ""
            metrics_html += f"<span style='font-size:0.55em; margin-left:15px; font-weight:normal; color:#444;'><b>{key}</b> {val:,.2f} <span style='color:{color};'>({sign}{diff_pct:.2f}%)</span></span>"

st.markdown(f"### 📊 글로벌 마켓 실시간 전광판 {metrics_html}", unsafe_allow_html=True)

tv_config = {
    "symbols": [
        { "proName": "OANDA:SPX500USD", "title": "S&P 500" },
        { "proName": "OANDA:NAS100USD", "title": "나스닥 100" },
        { "proName": "FX_IDC:USDKRW", "title": "원/달러 환율" },
        { "proName": "BINANCE:BTCUSDT", "title": "비트코인" },
        { "proName": "OANDA:XAUUSD", "title": "금(Gold)" },
        { "proName": "OANDA:WTICOUSD", "title": "WTI 원유" }
    ],
    "showSymbolLogo": True,
    "isTransparent": False,
    "displayMode": "adaptive",
    "colorTheme": "light",
    "locale": "kr"
}

tv_url = f"https://s.tradingview.com/embed-widget/ticker-tape/?locale=kr#{urllib.parse.quote(json.dumps(tv_config))}"
components.iframe(tv_url, height=80)

col_title, col_refresh = st.columns([5, 1.2])
with col_refresh:
    if st.button("뉴스/리포트 갱신", use_container_width=True):
        with st.spinner("AI 서버와 동기화 중..."):
            if new_data := fetch_realtime_data_direct(): merge_realtime_data(new_data)
            st.rerun()
with col_title: 
    st.caption(f"🧠 AI 백엔드 동기화 시점: {g_data.get('updated_at', '대기 중')} (뉴스는 수동 갱신, 지수는 자동 실시간)")

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
            with st.spinner("Flash 모델 분석 중..."): st.write_stream(call_gemini_stream_with_fallback(f"지표:\n{json.dumps(g_data.get('market_status', {}))}\n\n요약:\n{lite_summary}\n\n시장 흐름 심층 분석 서술."))

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
    st.caption("단순 뉴스 나열을 넘어, 파이썬 퀀트 엔진의 목표가(tp_m)와 애널리스트 컨센서스를 정면으로 비교/교차검증하여 시장을 관통하는 핫픽 2종목을 도출합니다.")
    
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
                valid_results = [f.result() for f in concurrent.futures.as_completed(futures) if f.result() and isinstance(f.result(), dict) and f.result().get("status") == "PASSED"]

            tech_data_str_all = "".join([r['tech_data_str'] for r in valid_results])

        with st.spinner("3단계: 최종 모닝 보고서 컴파일 중..."):
            prompt = (
                f"당신은 엄격한 숫자의 지배를 받는 대형 자산운용사 리서치 센터장입니다. '미표기'나 데이터 누락은 절대 용납하지 않습니다.\n"
                f"[거시 경제 및 미국 시황 요약 팩트]:\n{lite_macro_summary}\n\n"
                f"[파이썬 퀀트 엔진 연산 및 교차검증 로그]:\n{tech_data_str_all}\n\n"
                f"=== ⚠️ 작성 지침 및 데이터 스펙 ===\n"
                f"1. 제공된 [매크로 요약 팩트]를 바탕으로 오늘 한국 증시의 자산 배분 방안과 주도 섹터 흐름을 매우 상세하고 풍부하게 서술하십시오.\n"
                f"2. 제공된 [퀀트 엔진 로그]를 분석하여 가장 매력적인 핫픽 2종목을 엄선하십시오.\n"
                f"3. 핫픽 종목 작성 시 로그에 찍힌 **[현재가], [우리의 시스템 퀀트 중기 목표가(tp_m)]**를 반드시 모두 숫자로 명시하십시오.\n"
                f"4. 강세/약세 논리 서술 시 차트 데이터(이평선)와 밸류에이션(EPS, PER) 숫자를 철저히 인용하십시오.\n"
                f"5. 브리핑 결과를 바탕으로 오늘의 종합 시장 심리 수치(0~100)를 산출하여 반드시 마지막에 [SENTIMENT_SCORE]: 50 형식으로 출력할 것.\n\n"
                f"=== 리포트 출력 포맷 ===\n"
                f"## 📰 1. 글로벌 매크로 국면 및 주도 섹터 심층 브리핑\n"
                f"(실시간 지표와 매크로 뉴스를 연결하여 오늘 장의 성격 규정, 리스크 요인, 기회 요인을 구체적이고 길게 서술)\n\n"
                f"## 🎯 2. 당일 기관 퀀트-모멘텀 핫픽 (Top 2)\n"
                f"<ANALYSIS_종목명>\n"
                f"### 📌 [종목명] ([티커])\n"
                f"- **주가 현황:** 현재가 [숫자]원\n"
                f"- **퀀트 시스템 적정가:** **[숫자]원**\n"
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
                            st.write(call_gemini_with_fallback(f"[{sec} 요약]\n{l_sum}\n\n위 요약을 바탕으로 해당 섹터의 주도주 흐름 및 향후 모멘텀을 심층 분석하라."))

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
            with st.spinner("[1단계] 주도 테마 및 모멘텀 문맥 추출 중..."):
                news_str = "\n".join([f"- {n.get('title', '')}: {n.get('summary', '')}" for n in rec_news])
                momentum_context = call_gemini_lite_summary(
                    f"다음 핵심 뉴스 목록을 요약하여 지배적 테마 3~5개를 선별하십시오:\n\n{news_str}"
                )

            with st.spinner("[2단계] 후보군 종목 코드 매핑 중..."):
                prompt = f"투자 [{investment_horizon}] 모멘텀 수혜 예상 종목 15개의 '종목명'만 JSON 배열로 출력하라.\n\n{momentum_context}\n\n※ [\"삼성전자\", \"현대차\"] 형태만 반환하시오."
                res = call_gemini_with_fallback(prompt, model=LITE_MODEL_NAME)

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
                st.error("⚠️ 후보 종목 매핑 오류. 잠시 후 시도해주세요.")
                st.stop()

            passed_results = []
            killed_logs = []

            with st.spinner("[3단계] 후보군 병렬 크롤링 및 3x3 Tier 필터링 가동 중..."):
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(process_single_ticker, t, investment_horizon, k_factor, True, analyst_universe) for t in selected_tickers]
                    for f in concurrent.futures.as_completed(futures):
                        f_res = f.result()
                        if f_res:
                            if f_res.get("status") == "KILLED":
                                killed_logs.append(f_res)
                            elif f_res.get("status") == "PASSED":
                                passed_results.append(f_res)

            tried_tickers = set(selected_tickers)
            max_retry = 2
            retry_count = 0

            while len(passed_results) < 10 and retry_count < max_retry:
                deficit = 10 - len(passed_results)
                with st.spinner(f"[보충 단계] 부족분 {deficit}개 보충 및 필터링 중 (시도 {retry_count+1}/{max_retry})..."):
                    extra_prompt = f"다음 코드를 제외하고 수혜 예상 종목 {deficit}개의 '종목명'만 JSON 배열로 출력하라.\n(제외: {', '.join(tried_tickers)})\n\n※ 다른 설명 생략."
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

                    if not extra_tickers: 
                        break
                        
                    tried_tickers.update(extra_tickers)

                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                        futures = [executor.submit(process_single_ticker, t, investment_horizon, k_factor, True, analyst_universe) for t in extra_tickers]
                        for f in concurrent.futures.as_completed(futures):
                            f_res = f.result()
                            if f_res:
                                if f_res.get("status") == "KILLED":
                                    killed_logs.append(f_res)
                                elif f_res.get("status") == "PASSED":
                                    passed_results.append(f_res)
                    retry_count += 1

            st.session_state.valid_results_cache = passed_results
            st.session_state.killed_results_cache = killed_logs

            if len(passed_results) == 0:
                st.warning("⚠️ 퀀트 가치평가 및 Tier 3 하드 필터를 통과한 종목이 없습니다.")
                st.session_state.today_recommendation = ""
            else:
                tech_data_str_all = "".join([r['tech_data_str'] for r in passed_results])

                with st.spinner(f"[4단계] 최종 선별된 {len(passed_results)}개 중 시니어 애널리스트 방식 Top 3 보고서 작성 중..."):
                    if "단기" in investment_horizon:
                        persona = "모멘텀 스윙 트레이더"
                        strategy_guide = "▶ [단기 전략]: 파이썬 로그의 장기 지표(BPS, RIM 등)는 완전히 무시하고, 20일 변동성, 수급동향, MACD 골든크로스 및 뉴스테마 모멘텀에만 집중하십시오."
                    elif "중기" in investment_horizon:
                        persona = "실적 가치투자 애널리스트"
                        strategy_guide = "▶ [중기 전략]: 최근 분기 영업이익률 트렌드와 PER 상대매력, 3x3 크로스 시그널의 정합성 상태에 집중하십시오."
                    else:
                        persona = "장기 구조적 성장 전략가"
                        strategy_guide = "▶ [장기 전략]: 선행 ROE(E)와 잔여이익모델(RIM)의 장기 내재가치 팩트에 전적으로 집중하십시오. 단기 차트 노이즈는 기사 요약과 함께 배제하십시오."

                    step3_prompt = (
                        f"당신은 월스트리트 헤지펀드의 {persona}입니다. 투자 타임라인은 {investment_horizon}입니다.\n\n"
                        f"[시장 모멘텀]\n{momentum_context}\n\n"
                        f"[팩트 데이터 로그]\n{tech_data_str_all}\n\n"
                        f"=== ⚠️ 시니어 애널리스트 분석 지침 ===\n"
                        f"1. 전달받은 전체 후보군 데이터를 비교 평가하여 최상위 Top 3 종목만 엄선해 서술하십시오.\n"
                        f"2. {strategy_guide}\n"
                        f"3. 결론을 맨 앞에 배치하는 구조(BLUF)를 준수하십시오. 투자의견, 핵심 이유를 먼저 출력한 뒤 스토리를 풀어내십시오.\n"
                        f"4. 팩트 데이터 로그에 적힌 3x3 '교차검증 시그널'(예: True Bull 등) 인용 시, 괄호 안에 있는 친절한 해설을 포함하여 사용자가 쉽게 이해하도록 작성하십시오.\n"
                        f"5. [자기 검증 - 치명적 리스크] 스스로 도출한 결론이 틀릴 가능성 3가지를 서술하되, 로그에 Tier 2 경고가 있다면 이를 1순위로 강력 경고하십시오.\n\n"
                        f"=== 리포트 작성 포맷 ===\n"
                        f"### 🏆 1차 후보군 스크리닝 요약 (전체 평가)\n\n"
                        f"<ANALYSIS_티커숫자>\n"
                        f"### 📌 [종목명] (티커)\n"
                        f"**🎯 투자의견: [매수 / 관망 / 비중축소]** (신뢰도: 팩트 데이터 로그의 '시스템 신뢰도 점수'를 그대로 인용)\n"
                        f"- **현재가:** 000원\n"
                        f"- **목표가:** [해당 기간 목표가 00원]\n"
                        f"- **손절가:** [해당 기간 손절선 00원]\n"
                        f"- **💡 3x3 매트릭스 진단:** [Tier 등급 기입] (파이썬 로그를 참고하여 이 등급이 부여된 이유를 '목표가 XX + 실적 XX' 형태로 1줄 설명)\n"
                        f"- **📋 시스템 신뢰도 차감 사유:** (파이썬 로그의 '시스템 신뢰도 감점 내역' 문자열을 토씨 하나 틀리지 말고 그대로 받아쓰기 하여 출력할 것. 자의적 수정 및 지어내기 절대 금지)\n"
                        f"- **한 줄 요약:** (가장 핵심이 되는 투자 논리 1줄)\n\n"
                        f"---\n"
                        f"**📖 핵심 투자 스토리 (Investment Story)**\n\n"
                        f"**📊 집중 전략 팩트 분석 (3x3 교차 라벨 해설 포함)**\n\n"
                        f"**🛑 AI 자기검증: 이 분석이 틀릴 가능성 3가지 (Bear Case)**\n"
                        f"</ANALYSIS_티커숫자>\n\n"
                        f"[SELECTED_TICKERS]: 000000, 111111, 222222"
                    )
                    st.session_state.today_recommendation = "".join(call_gemini_stream_with_fallback(step3_prompt))
                    st.rerun()

    if st.session_state.get('today_recommendation'):
        raw = st.session_state.today_recommendation
        cached_results = st.session_state.get('valid_results_cache', [])
        cached_killed = st.session_state.get('killed_results_cache', [])

        if cached_killed:
            with st.expander("🛑 [시스템 스크리닝] 3x3 매트릭스 Hard Kill 탈락 내역", expanded=False):
                for k_item in cached_killed:
                    st.error(f"**[KILLED]** 종목: `{k_item['name']}` ({k_item['ticker']}) ➡️ 사유: `{k_item['reason']}`")

        with st.expander("추천 리포트", expanded=False):
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

                                if tick_data.get('is_flag_l_inv'):
                                    st.warning(f"⚠️ **장기 시그널 역전**\n중기 목표가({tp_m:,.0f}원)가 장기 가치({tp_l:,.0f}원)를 초과. 구조적 리스크 추적 요망.")
                                if tick_data.get('is_flag_m_inv'):
                                    st.warning(f"⚠️ **중기 시그널 역전**\n단기 목표가({tp_s:,.0f}원)가 중기 가치({tp_m:,.0f}원)를 초과.")

                                if st.button("스크랩", key=f"rec_s_{tick}", use_container_width=True):
                                    analysis_match = re.search(f"<ANALYSIS_{tick}>(.*?)</ANALYSIS_{tick}>", raw, re.DOTALL)
                                    specific_analysis = analysis_match.group(1).strip() if analysis_match else display_text

                                    c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, sl_s, sl_m, sl_l, scrap_date, model_used, user_id, risk_flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                              (f"{name} 퀀트 심층분석", specific_analysis, name, tick, current, tp_s, tp_m, tp_l, current, sl_s, sl_m, sl_l, datetime.now().strftime("%Y-%m-%d %H:%M"), MODEL_NAME, current_user, tick_data.get('risk_flags', -1)))
                                    conn.commit()
                                    st.success(f"✅ 리포트 스크랩 완료!")

with tab5:
    st.subheader("관심종목 진단 (보유종목 정밀 평가)")
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

    c.execute("SELECT id, stock_name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l, model_used, report_time, ticker, risk_flags FROM portfolio WHERE user_id = ?", (current_user,))
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
        my_stock_news_pool = cached_data.get("my_stock_news", {}) 

        for p in portfolios:
            p_id, name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l, model_used, report_time, ticker, p_risk_flags = p
            code = re.sub(r'[^\d]', '', ticker or "")
            price_info = price_map_watch.get(code, {})
            current, diff, diff_pct = price_info.get("current", 0.0), price_info.get("diff", 0.0), price_info.get("diff_pct", 0.0)

            st.markdown(f"### {name} `{code}`")
            col_sel, col_info, col_price, col_btn, col_del = st.columns([0.5, 3.5, 3, 1.5, 1.5])
            
            with col_sel: st.checkbox("선택", key=f"chk_t5_{p_id}", label_visibility="collapsed")
            with col_info:
                if is_owned:
                    st.markdown(f"보유 \| 평단: {avg_price:,.0f} \| 수량: {quantity}")
                    if current > 0 and avg_price > 0: st.markdown(f"수익률: {((current - avg_price) / avg_price * 100):+.1f}%")
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

                        target_news = my_stock_news_pool.get(name, [])
                        if target_news:
                            extra_ctx += f"\n[{name} 최근 핵심 뉴스 요약 및 모멘텀 소스]\n"
                            extra_ctx += "\n".join([f"- 제목: {n['title']}\n  내용: {n.get('summary', '')}" for n in target_news[:5]])
                            extra_ctx += "\n"

                        if "단기" in eval_horizon:
                            persona_t5 = "단기 스윙 트레이더"
                            t5_strategy = "▶ [단기 전략]: 파이썬 로그에 장기 지표(BPS, PER 등)가 있더라도 철저히 무시하십시오. 오직 최근 5일 수급, 차트 흐름, 뉴스 모멘텀만으로 매수/손절을 판단하십시오."
                        elif "중기" in eval_horizon:
                            persona_t5 = "가치/성장 투자 애널리스트"
                            t5_strategy = "▶ [중기 전략]: 분기별 실적 증감 추세와 선행 이익 가이드라인, 3x3 크로스 매트릭스 라벨 상태에 집중하십시오."
                        else:
                            persona_t5 = "장기 구조적 매크로 전략가"
                            t5_strategy = "▶ [장기 전략]: 단기 수급이나 차트 노이즈는 배제하고, 선행 ROE(E)에 근거한 본질적 잔여이익 펀더멘털만 평가하십시오."

                        prompt = (
                            f"[{name} 진단]\n[팩트 데이터 로그]\n{data_dict['tech_data_str']}\n{extra_ctx}\n\n"
                            f"당신은 리스크와 기회를 종합적으로 분석하는 {persona_t5}입니다. 투자 타임라인은 {eval_horizon}입니다.\n\n"
                            f"=== ⚠️ AI 분석 지침 ===\n"
                            f"1. 결론을 맨 앞에 배치하는 구조(BLUF)를 준수하십시오. 투자의견, 신뢰도, 현재가, 목표가, 손절가, 한 줄 요약을 가장 먼저 출력하십시오.\n"
                            f"2. {t5_strategy}\n"
                            f"3. 계좌 수익률과 주가의 위치를 참고하여 추가매수/유지/비중축소/손절 여부를 권고하십시오.\n"
                            f"4. 파이썬 로그에 찍힌 3x3 크로스 매트릭스 시그널(예: Hidden Value Trap 등) 인용 시, 괄호 안에 있는 뜻풀이를 덧붙여 직관적으로 이해할 수 있게 하십시오.\n"
                            f"5. [자기 검증 - 치명적 리스크 강제 출력] 스스로 도출한 결론이 틀릴 가능성 3가지를 서술하되, 만약 파이썬 로그에 Tier 2 경고가 적혀 있다면 무조건 Bear Case 1순위로 경고하십시오.\n\n"
                            f"=== 리포트 작성 포맷 ===\n"
                            f"**🎯 투자의견: [매수 / 유지 / 비중축소 / 손절]** (신뢰도: 팩트 데이터 로그의 '시스템 신뢰도 점수'를 그대로 인용)\n"
                            f"- **현재가:** 000원\n"
                            f"- **목표가:** [{eval_horizon} 목표가 00원]\n"
                            f"- **시스템 손절가:** [{eval_horizon} 손절가 00원]\n"
                            f"- **💡 3x3 매트릭스 진단:** [Tier 등급 기입] (파이썬 로그를 참고하여 이 등급이 부여된 이유를 '목표가 XX + 실적 XX' 형태로 1줄 설명)\n"
                            f"- **📋 시스템 신뢰도 차감 사유:** (파이썬 로그의 '시스템 신뢰도 감점 내역' 문자열을 토씨 하나 틀리지 말고 그대로 받아쓰기 하여 출력할 것. 자의적 수정 및 지어내기 절대 금지)\n"
                            f"- **한 줄 요약:** (가장 핵심이 되는 판단 근거 1줄)\n\n"
                            f"---\n"
                            f"**📖 핵심 투자 스토리 (Investment Story)**\n\n"
                            f"**📊 {eval_horizon} 맞춤형 팩트 분석 (3x3 크로스 시그널 해설 포함)**\n\n"
                            f"**🛑 AI 자기검증: 이 분석이 틀릴 가능성 3가지 (Bear Case)**\n"
                        )
                        
                        report = call_gemini_with_fallback(prompt)
                        
                        n_tp_s, n_tp_m, n_tp_l = data_dict['tp_s'], data_dict['tp_m'], data_dict['tp_l']
                        n_sl_s, n_sl_m, n_sl_l = data_dict['sl_s'], data_dict['sl_m'], data_dict['sl_l']

                        c.execute("UPDATE portfolio SET report_text=?, tp_s=?, tp_m=?, tp_l=?, bp=?, sl_s=?, sl_m=?, sl_l=?, model_used=?, report_time=?, risk_flags=? WHERE id=?", 
                                  (report, n_tp_s, n_tp_m, n_tp_l, current, n_sl_s, n_sl_m, n_sl_l, MODEL_NAME, datetime.now().strftime("%Y-%m-%d %H:%M"), data_dict.get('risk_flags', -1), p_id))
                        conn.commit(); st.rerun()
            
            with col_del:
                if st.button("개별 삭제", key=f"del_t5_{p_id}", use_container_width=True):
                    c.execute("DELETE FROM portfolio WHERE id=?", (p_id,))
                    conn.commit(); st.rerun()

            target_news = my_stock_news_pool.get(name, [])
            if target_news:
                with st.expander(f"📰 {name} 관련 수집 뉴스 ({len(target_news)}건)", expanded=False):
                    limit_key = f"news_limit_t5_{p_id}"
                    if limit_key not in st.session_state:
                        st.session_state[limit_key] = 5
                        
                    current_limit = st.session_state[limit_key]
                    
                    for idx, n in enumerate(target_news[:current_limit]):
                        st.markdown(f"**{idx+1}. [{n['title']}]({n['link']})**")
                        if n.get('summary'):
                            st.caption(f"{n['summary']}")
                            
                    if len(target_news) > current_limit:
                        if st.button("🔽 뉴스 더보기", key=f"btn_more_news_{p_id}", use_container_width=True):
                            st.session_state[limit_key] += 5
                            st.rerun()
            else:
                st.info(f"ℹ️ {name} 관련 수집된 뉴스가 없습니다. (드라이브 백업 후 람다 스케줄러 실행 대기 필요)")

            if report_text:
                with st.expander("진단 리포트", expanded=False):
                    if tp_m > tp_l and tp_l > 0:
                        st.warning(f"⚠️ 시스템 경고: 중기 적정가({tp_m:,.0f}원)가 장기 RIM 내재가치({tp_l:,.0f}원)를 초과했습니다 (장기 가치 선행 역전 현상). AI 해설과 독립된 엔진 자체적 리스크 신호이므로 필히 확인하십시오.")
                    if tp_s > tp_m and tp_m > 0:
                        st.warning(f"⚠️ 시스템 경고: 단기 밴드 상단({tp_s:,.0f}원)이 중기 적정가({tp_m:,.0f}원)를 초과했습니다 (중기 모멘텀 역전 현상).")
                        
                    st.write(report_text)
                    st.divider()
                    col_tgt, col_sl = st.columns(2)
                    with col_tgt:
                        st.markdown(f"**🎯 AI 최종 채택 목표가 밴드**\n* **단기 목표가:** {tp_s:,.0f}원\n* **중기 목표가:** {tp_m:,.0f}원\n* **장기 목표가:** {tp_l:,.0f}원")
                    with col_sl:
                        st.markdown(f"**🔴 파이썬 연산 리스크 규격 (k={k_factor:.1f})**\n* **단기 손절선:** {sl_s:,.0f}원\n* **중기 손절선:** {sl_m:,.0f}원\n* **장기 손절선:** {sl_l:,.0f}원")
                    
                    if st.button("스크랩북에 저장하여 가격 추적하기", key=f"scrap_t5_{p_id}", use_container_width=True):
                        c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, sl_s, sl_m, sl_l, scrap_date, model_used, user_id, risk_flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (f"{name} 관심종목 진단", report_text, name, ticker, current, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l, datetime.now().strftime("%Y-%m-%d %H:%M"), model_used, current_user, p_risk_flags))
                        conn.commit(); st.success("스크랩북 저장 완료")
            st.divider()

with tab6:
    st.subheader("저장된 분석 리포트 및 모델 검증")
    c.execute("""
        SELECT id, title, stock_name, ticker, scrap_date, analysis, model_used, 
               saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, 
               sl_s, sl_m, sl_l, risk_flags
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
        
        bucket_stats = {}

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
                
            rf = row[15]
            if rf is not None and rf >= 0:
                if rf not in bucket_stats:
                    bucket_stats[rf] = {"total": 0, "hit": 0, "stop": 0}
                bucket_stats[rf]["total"] += 1
                if s_tp_s > 0 and max_high >= s_tp_s: bucket_stats[rf]["hit"] += 1
                if s_sl_s > 0 and min_low <= s_sl_s: bucket_stats[rf]["stop"] += 1

        avg_current_yield = avg_current_yield / total_evals if total_evals > 0 else 0.0
        
        with st.container(border=True):
            st.markdown(f"### 🎯 K={k_factor:.1f} 기반 시스템 트레이딩 성과 (기간 내 터치 기준)")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("총 스크랩 리포트", f"{total_evals}개")
            m2.metric("단기 목표 도달 / 손절", f"{(hit_count_s/total_evals)*100:.1f}% / {(stop_out_count_s/total_evals)*100:.1f}%")
            m3.metric("중기 목표 도달 / 손절", f"{(hit_count_m/total_evals)*100:.1f}% / {(stop_out_count_m/total_evals)*100:.1f}%")
            m4.metric("스크랩 포트폴리오 수익률", f"{avg_current_yield:+.2f}%")
            
        st.divider()

        st.markdown("### 📊 위험 신호 개수별 실측 적중률 (신뢰도 자가 튜닝 인프라)")
        if not bucket_stats:
            st.info("실측 데이터를 집계할 유효 스크랩 팩트 로그(risk_flags 컬럼 포함)가 부족합니다. 스크랩이 지속되면 확률이 조율됩니다.")
        else:
            cols_rf = st.columns(max(len(bucket_stats), 4))
            for idx, rf in enumerate(sorted(bucket_stats.keys())):
                b = bucket_stats[rf]
                if idx < len(cols_rf):
                    with cols_rf[idx]:
                        if b["total"] < 5:
                            st.caption(f"**위험 신호 {rf}개**<br>샘플 {b['total']}건 누적<br>*(통계 신뢰도 구축 중, 최소 5건 필요)*", unsafe_allow_html=True)
                        else:
                            hit_rate = b["hit"] / b["total"] * 100
                            stop_rate = b["stop"] / b["total"] * 100
                            st.metric(f"위험 신호 {rf}개 (n={b['total']})", f"목표도달 {hit_rate:.0f}%", f"손절이탈 {stop_rate:.0f}%", delta_color="off")
        
        st.divider()

        col_bulk_scrap, _ = st.columns([2, 8])
        with col_bulk_scrap:
            if st.button("🗑️ 선택 항목 삭제", key="bulk_del_t6", use_container_width=True):
                if to_del := [s[0] for s in scraps if st.session_state.get(f"chk_t6_{s[0]}", False)]:
                    c.execute(f"DELETE FROM scrapbook WHERE id IN ({','.join(['?']*len(to_del))})", to_del)
                    conn.commit(); st.rerun()
                    
        for row in scraps:
            s_id, title, s_name, ticker, s_date, analysis, m_used, saved_p, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l, risk_flags = row
            code = re.sub(r'[^\d]', '', ticker or "")
            price_info = price_map_scrap.get(code, {})
            current_p = price_info.get("current", 0.0)
            max_high, min_low = get_historical_high_low(code, s_date)
            
            col_sel_s, col_exp_s = st.columns([0.5, 9.5])
            with col_sel_s: st.checkbox("선택", key=f"chk_t6_{s_id}", label_visibility="collapsed")
            with col_exp_s:
                with st.expander(f"📌 {title} ({s_name} | {ticker}) - {s_date}"):
                    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
                    
                    if saved_p and current_p > 0:
                        scrap_diff = current_p - saved_p
                        scrap_diff_pct = (scrap_diff / saved_p) * 100
                        m_col1.metric("저장 당시 주가", f"{saved_p:,.0f}원", delta=f"스크랩 누적 {scrap_diff:+,.0f}원 ({scrap_diff_pct:+.2f}%)")
                    else: 
                        m_col1.metric("저장 당시 주가", f"{saved_p:,.0f}원" if saved_p else "정보 없음")
                    
                    if current_p > 0:
                        daily_diff = price_info.get("diff", 0.0)
                        daily_diff_pct = price_info.get("diff_pct", 0.0)
                        m_col2.metric("실시간 현재가", f"{current_p:,.0f}원", delta=f"전일 대비 {daily_diff:+,.0f}원 ({daily_diff_pct:+.2f}%)")
                    else: 
                        m_col2.metric("실시간 현재가", "조회 실패")
                        
                    m_col3.markdown(f"**손절가 라인**<br>단기: <span style='color:red;'>{sl_s:,.0f}</span><br>중기: <span style='color:red;'>{sl_m:,.0f}</span>", unsafe_allow_html=True)
                    m_col4.markdown(f"**목표가 밴드**<br>단기: {tp_s:,.0f}<br>중기: {tp_m:,.0f}", unsafe_allow_html=True)

                    if tp_m > tp_l and tp_l > 0:
                        st.warning(f"⚠️ 시스템 로그 (저장 시점 기준): 해당 분석 스크랩 당시 '중기 가치 > 장기 RIM 가치' 역전 플래그 상태였습니다.")
                    if tp_s > tp_m and tp_m > 0:
                        st.warning(f"⚠️ 시스템 로그 (저장 시점 기준): 해당 분석 스크랩 당시 '단기 가치 > 중기 가치' 역전 플래그 상태였습니다.")
                    
                    if current_p > 0 and tp_s > 0:
                        pct_s = (current_p / tp_s) * 100
                        st.progress(min(int(pct_s), 100), text=f"단기 목표가 대비 진행률: **{pct_s:.1f}%**")
                        if min_low > 0 and min_low <= sl_s and sl_s > 0:
                            st.error(f"⚠️ **과거 단기 손절선({sl_s:,.0f}원) 이탈 이력 발생!** 현재 주가 흐름과 별개로 리스크 위반 이력이 존재합니다.")
                        elif current_p <= sl_s and sl_s > 0:
                            st.error(f"⚠️ **단기 손절선({sl_s:,.0f}원) 이탈 진행 중!** 기계적 규칙에 의거해 청산을 고려하십시오.")
                    
                    st.markdown("---")
                    st.write(analysis)
                    
                    if st.button("개별 삭제", key=f"del_t6_{s_id}", use_container_width=True):
                        c.execute("DELETE FROM scrapbook WHERE id=?", (s_id,))
                        conn.commit(); st.rerun()
    else:
        st.info("저장된 분석 리포트가 없습니다.")
