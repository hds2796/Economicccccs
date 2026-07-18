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

MODEL_NAME = "gemini-3.5-flash"
LITE_MODEL_NAME = "gemini-3.1-flash-lite"

# DB 백업/복구(커넥션 close/reopen) 시 다른 세션의 접근을 막기 위한 락
db_backup_lock = threading.Lock()

st.set_page_config(page_title="Project2_Stock", page_icon="📊", layout="wide", initial_sidebar_state="expanded")

# =======================================================
# 비밀번호 기반 로그인 및 유저 식별
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
    try:
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
    except Exception as e:
        return None

conn = init_db()
try:
    c = conn.cursor()
except sqlite3.ProgrammingError:
    init_db.clear()
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
                    data = []
                    for lst in root.findall('list'):
                        corp_code = lst.findtext('corp_code')
                        corp_name = lst.findtext('corp_name')
                        stock_code = lst.findtext('stock_code')
                        if stock_code and stock_code.strip():
                            data.append((corp_code, corp_name, stock_code.strip()))
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
# 사이드바 제어 (K값 저장 및 백업/복구 통합)
# =======================================================
with st.sidebar:
    st.markdown(f"**👤 접속 계정:** `{current_user}`")
    st.divider()
    
    c.execute("SELECT k_factor FROM user_settings WHERE user_id = ?", (current_user,))
    row = c.fetchone()
    saved_k = row[0] if row else 2.0
    
    st.subheader("⚙️ 시스템 설정")
    k_factor = st.slider("리스크 관리 계수 (k)", min_value=1.0, max_value=3.5, value=saved_k, step=0.1, help="값이 변경되면 즉시 DB에 자동 저장되어 백업 시 함께 보관됩니다.")
    
    if k_factor != saved_k:
        c.execute("INSERT OR REPLACE INTO user_settings (user_id, k_factor) VALUES (?, ?)", (current_user, k_factor))
        conn.commit()

    st.divider()
    st.subheader("💾 데이터베이스 관리")
    if st.button("☁️ 구글 드라이브 백업", use_container_width=True):
        with st.spinner("클라우드 백업 중..."):
            if backup_db_to_drive(): st.success("✅ DB 및 K값 백업 완료")
            
    if st.button("🔄 드라이브에서 복구", use_container_width=True):
        with st.spinner("데이터 복구 중..."):
            if restore_db_from_drive():
                init_db.clear()
                st.success("✅ 복구 완료! 새로고침 진행합니다.")
                st.rerun()

# =======================================================
# 투 트랙 API 호출 (동시성 제어)
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
        try:
            return client.models.generate_content(model=model, contents=prompt).text
        except Exception as e1:
            if model == MODEL_NAME:
                try:
                    return client.models.generate_content(model="gemini-3-flash-preview", contents=prompt).text
                except Exception as e2:
                    return f"최종 호출 실패 (Flash 및 Preview 모두 에러): {e2}"
            else:
                return f"호출 실패: {e1}"
    except Exception as e:
        return f"호출 실패: {e}"
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
        except Exception:
            try:
                fallback_response = client.models.generate_content_stream(model="gemini-3-flash-preview", contents=prompt)
                yield f"\n[안내] 3.5-flash 서버 응답 지연으로 인해 3-preview 모델로 우회하여 분석을 진행합니다.\n\n"
                for chunk in fallback_response:
                    if chunk.text: yield chunk.text
            except Exception as e2: yield f"\n최종 호출 실패: {e2}"
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
        res = requests.get(url, timeout=5).json()
        if res.get("status") == "000": return "\n".join([f"- [{i['rcept_dt']}] {i['report_nm']}" for i in res.get("list", [])])
        return "최근 3개월 주요 공시 없음"
    except: return "DART 조회 실패"

@st.cache_data(ttl=600)
def get_advanced_fundamental_data(code):
    data = {"per": "-", "pbr": "-", "eps": 0, "bps": 0, "industry_per": "-", "quarter_trend": "정보 없음", "supply_demand": "정보 없음", "eps_history": [], "roe_history": []}
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        
        per_elem = soup.find(id="_per")
        if per_elem: data["per"] = per_elem.get_text().strip()
            
        pbr_elem = soup.find(id="_pbr")
        if pbr_elem: data["pbr"] = pbr_elem.get_text().strip()
            
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
                            if data["eps"] == 0:
                                data["eps"] = valid_eps[-1]
                            data["eps_history"] = valid_eps[-3:]
                    if "BPS(원)" in text:
                        valid_bps = [float(v) for td in th_item.find_next_siblings("td") if (v := td.get_text().strip().replace(',', '')) and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        if valid_bps and data["bps"] == 0: 
                            data["bps"] = valid_bps[-1]
                    if "ROE" in text:
                        valid_roe = [float(v) for td in th_item.find_next_siblings("td") if (v := td.get_text().strip().replace(',', '')) and v.replace('.', '', 1).replace('-', '', 1).isdigit()]
                        if valid_roe: data["roe_history"] = valid_roe[-3:]
            except: pass
            
        url_frgn = f"https://finance.naver.com/item/frgn.naver?code={code}"
        res_frgn = requests.get(url_frgn, headers=headers, timeout=5)
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
        res = requests.get(url, timeout=5)
        items = BeautifulSoup(res.text, "html.parser").find_all('item')
        if not items: return None
        df_data = [float(item['data'].split('|')[4]) for item in items]
        if len(df_data) < 60: return None
        
        df = pd.Series(df_data)
        macd = df.ewm(span=12, adjust=False).mean() - df.ewm(span=26, adjust=False).mean()
        signal = macd.ewm(span=9, adjust=False).mean()
        
        returns = df.pct_change().dropna()
        daily_volatility = returns.iloc[-20:].std() if len(returns) >= 20 else 0.0

        return {
            "current": df_data[-1], "high_52": max(df_data), "low_52": min(df_data),
            "ma20": sum(df_data[-20:]) / 20, "ma60": sum(df_data[-60:]) / 60, "macd": macd.iloc[-1], "signal": signal.iloc[-1],
            "daily_volatility": daily_volatility
        }
    except: return None

@st.cache_data(ttl=600)
def fetch_stock_news(query, display=5):
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET: return []
    try:
        url = f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={urllib.parse.quote(query)}&display={display}&sort=date&format=json"
        req = urllib.request.Request(url, headers={"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET})
        with urllib.request.urlopen(req, timeout=3) as response:
            return [{"title": BeautifulSoup(i['title'], "html.parser").get_text(), "link": i['link']} for i in json.loads(response.read().decode('utf-8')).get("items", [])]
    except: return []

@st.cache_data(ttl=1800)
def get_historical_high_low(code, start_date_str):
    try:
        url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=250&requestType=0"
        res = requests.get(url, timeout=5)
        items = BeautifulSoup(res.text, "html.parser").find_all('item')
        
        start_date = datetime.strptime(start_date_str.split()[0], "%Y-%m-%d")
        max_h, min_l = 0.0, float('inf')
        
        for item in items:
            data = item['data'].split('|')
            item_date = datetime.strptime(data[0], "%Y%m%d")
            if item_date >= start_date:
                high, low = float(data[2]), float(data[3])
                if high > max_h: max_h = high
                if low < min_l: min_l = low
                
        return max_h, (min_l if min_l != float('inf') else 0.0)
    except: return 0.0, 0.0

def parse_won(s):
    if not s: return 0.0
    s = str(s).strip()
    multiplier = 1
    if '조' in s: multiplier = 1_000_000_000_000; s = s.split('조')[0]
    elif '억' in s: multiplier = 100_000_000; s = s.split('억')[0]
    elif '만' in s: multiplier = 10_000; s = s.split('만')[0]
    num_str = re.sub(r'[^\d.]', '', s)
    return float(num_str) * multiplier if num_str else 0.0

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

st.markdown("### 📊 실시간 시장 상태")
col_title, col_refresh = st.columns([5, 1.2])
with col_refresh:
    if st.button("실시간 갱신", use_container_width=True):
        with st.spinner("갱신 중..."):
            if new_data := fetch_realtime_data_direct(): merge_realtime_data(new_data)
            st.rerun()

with col_title: 
    st.caption(f"동기화 시점: {g_data.get('updated_at', '알 수 없음')}")

market_data = g_data.get("market_status", {})
cols = st.columns(4)
for i, key in enumerate(["코스피", "코스닥", "S&P 500", "원/달러 환율"]):
    with cols[i]:
        if key in market_data:
            data = market_data[key]
            val, diff, diff_pct = data.get("current", 0.0), data.get("diff", 0.0), data.get("diff_pct", 0.0)
            st.metric(label=key, value=f"{val:,.2f}" if val else "점검중", delta=f"{diff:+.2f} ({diff_pct:+.2f}%)" if val else None)

st.divider()
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["실시간 브리핑", "핵심 경제", "섹터 뉴스", "종목 발굴", "관심종목 진단", "스크랩북"])

# =======================================================
# 탭 1: 실시간 브리핑
# =======================================================
with tab1:
    st.subheader("실시간 시황 브리핑")
    news_pool = g_data.get("realtime_news", [])
    if news_pool:
        with st.expander(f"📰 수집된 실시간 뉴스 (최신 10건 표시 / 총 {len(news_pool)}건 누적)"):
            for idx, n in enumerate(news_pool[:10]): st.markdown(f"{idx+1}. [{n['title']}]({n['link']})")
    if st.button("브리핑 생성", key="btn_briefing"):
        if not news_pool: st.error("분석할 뉴스가 없습니다.")
        else:
            news_str = "\n".join([f"- {n['title']}: {n.get('description', '')}" for n in news_pool[:50]])
            with st.spinner("Lite 모델 압축 중..."): lite_summary = call_gemini_lite_summary(f"다음 뉴스를 요약하라:\n\n{news_str}")
            with st.spinner("Flash 모델 분석 중..."): st.write_stream(call_gemini_stream_with_fallback(f"지표:\n{json.dumps(market_data)}\n\n요약:\n{lite_summary}\n\n시장 흐름 심층 분석 서술."))

# =======================================================
# 탭 2: 핵심 경제 (💡 프롬프트 구체화 및 마크다운 정규식 방어)
# =======================================================
with tab2:
    st.subheader("핵심 경제 종합 브리핑 및 시장 심리")
    
    # 심리 지수 일간 평균 추출 및 최근 7일(일주일) 시각화 적용
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

    eco_news = cached_data.get("eco_news", [])
    if st.button("거시경제 종합 분석 및 전망 생성", use_container_width=True):
        if not eco_news: st.error("분석할 뉴스가 없습니다.")
        else:
            with st.spinner("Lite 요약 중..."): 
                lite_summary = call_gemini_lite_summary("요약하라:\n" + "\n".join([f"- {n['title']}" for n in eco_news[:50]]))
            with st.spinner("Flash 심층 분석 중..."):
                prompt = (
                    f"당신은 리스크 관리에 철저한 매크로 퀀트 전략가입니다.\n"
                    f"[현재 시장 지표]\n{json.dumps(market_data)}\n"
                    f"[거시 경제 요약]\n{lite_summary}\n\n"
                    f"=== 리포트 작성 항목 ===\n"
                    f"**📰 핵심 경제 종합 브리핑**\n"
                    f"- 제공된 [거시 경제 요약]을 바탕으로 현재 시장에 큰 영향을 미치는 주요 경제/증시 뉴스 3~5가지를 구체적 사실과 함께 상세히 브리핑하십시오.\n"
                    f"**🔮 앞으로 주식시장은?**\n"
                    f"- (향후 시장 전망 및 최대 하방 리스크 점검)\n"
                    f"**🛡️ 대응 전략**\n"
                    f"- (현재 거시 지표에 기반한 포트폴리오 관리 전략)\n\n"
                    f"※ 필수 지침: 리포트 맨 마지막 줄에는 반드시 현재 시장 심리를 0에서 100 사이의 숫자로 평가하여 아래와 같은 정확한 포맷으로 출력하십시오. 볼드체나 다른 수식어를 절대 붙이지 마십시오.\n"
                    f"[SENTIMENT_SCORE]: 50"
                )
                full_report = "".join(call_gemini_stream_with_fallback(prompt))
                
                # 정규식 강화: 특수문자 제거 및 띄어쓰기 유연성 확보
                clean_report_for_regex = full_report.replace('*', '').replace('#', '')
                if score_match := re.search(r'\[SENTIMENT_SCORE\]\s*:\s*(\d+)', clean_report_for_regex):
                    c.execute("INSERT INTO sentiment_history (calc_date, score) VALUES (?, ?)", (datetime.now().strftime("%Y-%m-%d"), float(score_match.group(1))))
                    conn.commit()
                
                # 출력 시 점수 부분만 깔끔하게 제거
                st.session_state.eco_briefing = re.sub(r'\[SENTIMENT_SCORE\].*', '', full_report, flags=re.DOTALL).strip()
                st.rerun()

    if st.session_state.get('eco_briefing'):
        with st.expander("📝 거시경제 분석 및 전망 리포트", expanded=True): 
            st.write(st.session_state.eco_briefing)
        
    st.subheader("핵심 경제 뉴스 목록")
    if eco_news:
        for idx, n in enumerate(eco_news[:10]):
            st.markdown(f"**[{idx+1}] {n['title']}**")
            if st.button("개별 심층 분석", key=f"eco_an_{idx}"):
                with st.spinner("Lite 전처리 및 Flash 의미론적 분석 진행 중..."):
                    l_sum = call_gemini_lite_summary(f"본 뉴스의 핵심적 사실을 왜곡 없이 상세히 요약하라:\n{n['title']}")
                    st.write(call_gemini_with_fallback(f"[뉴스 요약]\n{l_sum}\n\n이 사실이 거시 경제 및 관련 주식 섹터에 파급 효과와 거시적 변화 의의를 분석하라."))
    else: st.info("조회된 핵심 경제 뉴스가 없습니다.")

# =======================================================
# 탭 3: 섹터 뉴스
# =======================================================
with tab3:
    st.subheader("섹터별 모멘텀 분석")
    sec_news = g_data.get("sectors") or cached_data.get("sectors") or g_data.get("sector_news", {})
    if sec_news:
        for sec, items in sec_news.items():
            if not items: continue
            with st.expander(f"📁 {sec} ({len(items)}건)"):
                for i in items: st.markdown(f"- [{i['title']}]({i.get('link', '#')})")
                if st.button(f"분석", key=f"sec_{sec}"): st.write(call_gemini_with_fallback(f"[{sec} 요약]\n" + call_gemini_lite_summary("\n".join([i['title'] for i in items])) + "\n\n주도주 흐름 분석."))

# =======================================================
# 탭 4: 종목 발굴
# =======================================================
def process_single_ticker_for_tab4(ticker, investment_horizon, user_k):
    ticker = re.sub(r'[^\d]', '', ticker)
    if len(ticker) != 6: return ""
    try:
        res = requests.get(f"https://m.stock.naver.com/api/stock/{ticker}/basic", timeout=3).json()
        name = res.get("stockName", ticker)
    except: name = ticker
    
    tech = get_technical_data(ticker)
    fund = get_advanced_fundamental_data(ticker)
    dart_info = get_dart_filings(ticker)
    news_raw = fetch_stock_news(name, display=4)
    lite_summary = call_gemini_lite_summary(f"뉴스 및 공시 요약:\n{dart_info}\n{chr(10).join([n['title'] for n in news_raw])}")
    
    current_price = tech['current'] if tech else 0.0
    daily_vol = tech['daily_volatility'] if tech else 0.0
    eps_val = fund.get('eps', 0.0)
    bps_val = fund.get('bps', 0.0)
    roe_history = fund.get('roe_history', [])
    eps_history = fund.get('eps_history', [])
    
    try: float_per = float(fund['per'].replace(',', '')) if fund['per'] != '-' else 0.0
    except: float_per = 0.0
    try: float_ind_per = float(fund['industry_per'].replace(',', '')) if fund['industry_per'] != '-' else 0.0
    except: float_ind_per = 0.0

    # [PEG] 최근 EPS 추세 기반 성장률 (-50%~+100% 범위로 클램핑하여 극단값 방어)
    eps_growth = 0.0
    if len(eps_history) >= 2 and eps_history[0] != 0:
        eps_growth = (eps_history[-1] - eps_history[0]) / abs(eps_history[0])
        eps_growth = min(max(eps_growth, -0.5), 1.0)

    # [PBR 하한선 방어] 적자가 지속되며 확대되는 추세면 BPS 청산가치에 할인 적용
    bps_discount = 1.0
    if len(eps_history) >= 2 and eps_history[-1] < 0 and eps_history[0] < 0 and eps_history[-1] < eps_history[0]:
        bps_discount = 0.8
    
    sl_s = current_price * (1 - user_k * daily_vol * np.sqrt(20)) if daily_vol > 0 else 0.0
    sl_m = current_price * (1 - user_k * daily_vol * np.sqrt(60)) if daily_vol > 0 else 0.0
    sl_l = current_price * (1 - user_k * daily_vol * np.sqrt(250)) if daily_vol > 0 else 0.0

    if eps_val <= 0:
        conservative_tp = (bps_val * bps_discount) if bps_val > 0 else current_price * 0.8
        fund_target_log = (
            f"   - [보수적 시나리오] BPS 자산가치 기준(할인율 {int((1-bps_discount)*100)}%): {conservative_tp:,.0f}원\n"
            "   - [중립적 시나리오] 적자 기업으로 이익 기반 퀀트 밴드 산출 불가 (차트/모멘텀 기준 기술적 밴드로 대체 요망)\n"
            "   - [공격적 시나리오] 적자 기업으로 이익 기반 퀀트 밴드 산출 불가 (차트/모멘텀 기준 기술적 밴드로 대체 요망)\n"
        )
    else:
        conservative_tp = (bps_val * bps_discount) if bps_val > 0 else current_price * 0.9
        adjusted_ind_per = float_ind_per * (1 + eps_growth) if float_ind_per > 0 else 0.0
        base_tp = eps_val * adjusted_ind_per if adjusted_ind_per > 0 else eps_val * 10
        required_return = 0.08
        expected_roe = (roe_history[-1] / 100) if roe_history else 0.05
        rim_tp = bps_val + (bps_val * (expected_roe - required_return) / required_return) if bps_val > 0 else current_price * 1.1
        aggressive_tp = max(base_tp, rim_tp)
        
        fund_target_log = (
            f"   - [보수적 시나리오] BPS 자산가치 기준(할인율 {int((1-bps_discount)*100)}%): {conservative_tp:,.0f}원\n"
            f"   - [중립적 시나리오] 성장률 반영 업종PER 기준(EPS성장률 {eps_growth*100:+.1f}%): {base_tp:,.0f}원\n"
            f"   - [공격적 시나리오] RIM 초과이익 기준: {aggressive_tp:,.0f}원\n"
        )

    calc_result_log = (
        f"▶ 리스크 팩트 (k={user_k:.1f}): 단기손절 {sl_s:,.0f}원 | 중기손절 {sl_m:,.0f}원 | 장기손절 {sl_l:,.0f}원\n"
        f"▶ 파이썬 선행연산 목표가 밴드:\n{fund_target_log}"
    )

    tech_data_str = f"[{name} ({ticker})]\n"
    if tech: tech_data_str += f"- 차트/리스크: 현재가 {tech['current']:,.0f} | 20일선 {tech['ma20']:,.0f} | 60일선 {tech['ma60']:,.0f} | MACD {tech['macd']:,.2f} | 20일 변동성(일간) {daily_vol*100:.2f}%\n"
    if fund: tech_data_str += f"- 재무 비율: PER {fund['per']} (업종PER {fund['industry_per']}) | PBR {fund['pbr']} | EPS {eps_val:,}원 | BPS {bps_val:,}원\n"
    tech_data_str += f"{calc_result_log}\n- 요약본:\n{lite_summary}\n\n"
    return tech_data_str

with tab4:
    st.subheader("종목 발굴 (병렬 고속 분석)")
    investment_horizon = st.radio("투자기간", ["단기 (1~3개월)", "중기 (3~6개월)", "장기 (1년 이상)"], horizontal=True)

    if st.button("추천 종목 발굴", use_container_width=True, key="btn_recommend"):
        rec_news = dedupe_news((g_data.get("realtime_news", []) if g_data else []) + (cached_data.get("eco_news", []) if cached_data else []))
        if not rec_news: st.error("분석 대상 뉴스 풀이 비어있습니다.")
        else:
            with st.spinner("[1단계] 1차 후보군 10개 추출 중..."):
                articles_str = "\n".join([f"- {n['title']}" for n in rec_news[:50]])
                step1_prompt = (f"투자 [{investment_horizon}] 모멘텀 종목 10개 6자리 JSON 배열 출력.\n\n{articles_str}")
                step1_res = call_gemini_with_fallback(step1_prompt, model=LITE_MODEL_NAME)
                selected_tickers = []
                if match := re.search(r'\[.*\]', step1_res, re.DOTALL):
                    try: selected_tickers = json.loads(match.group(0))[:10]
                    except: pass
                if not selected_tickers: st.stop()
            
            with st.spinner("[2단계] 10개 후보군 동시 병렬 크롤링 및 리스크/목표가 밴드 산출 중..."):
                tech_data_str = ""
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                    futures = [executor.submit(process_single_ticker_for_tab4, t, investment_horizon, k_factor) for t in selected_tickers]
                    for future in concurrent.futures.as_completed(futures):
                        tech_data_str += future.result()
            
            with st.spinner("[3단계] Flash 기반 목표가 밴드 매칭 및 수치 방어 논리 작성 중..."):
                step3_prompt = (
                    f"당신은 리스크 관리를 최우선으로 하는 퀀트 애널리스트입니다.\n"
                    f"[10개 후보군 팩트 데이터]\n{tech_data_str}\n\n"
                    f"=== ⚠️ AI 분석 및 목표가 선택 지침 (환각 금지 및 수치 인용 필수) ===\n"
                    f"1. 가장 매력적인 **Top 3 종목만 엄선**하십시오.\n"
                    f"2. 기계적인 장단점 나열에 앞서, **'왜 수많은 주식 중 굳이 이 종목을 지금 사야 하는가?'**에 대한 핵심 투자 아이디어(Why Buy?)를 최상단에 선언하십시오.\n"
                    f"3. **절대 주가나 목표가를 직접 사칙연산하여 임의의 값을 창조하지 마십시오.** 파이썬이 제공한 [보수적/중립적/공격적] 목표가 밴드 가격 중 현재 상황에 가장 맞는 시나리오를 매칭/선택하십시오.\n"
                    f"4. 편향을 제거하기 위해 반드시 <BULL_CASE>와 <BEAR_CASE>를 분리 작성하여 자가 검열하십시오.\n"
                    f"5. 파이썬이 연산한 '단기/중기/장기 손절가' 데이터를 그대로 신뢰하여 대응 전략을 제시하십시오.\n\n"
                    f"=== 리포트 작성 항목 ===\n"
                    f"<ANALYSIS_티커숫자>\n"
                    f"### [종목명] (티커)\n"
                    f"**🎯 핵심 투자 아이디어 (Why Buy?)**\n"
                    f"- (가장 강력하고 결정적인 이유 1~2줄 명시)\n"
                    f"**🟢 강세 논리 (Bull Case)**\n"
                    f"**🔴 약세/위험 논리 (Bear Case)**\n"
                    f"**⚖️ 최종 판단 및 리스크 평가**\n"
                    f"- 목표가 도달 논증 (구체적 수치 인용 필수): 파이썬이 제공한 3대 목표가 시나리오 중 최종 선택한 단기/중기/장기 가격을 명시하십시오. 그리고 **반드시 본문에 제공된 팩트 수치(EPS, BPS, PER, 20일선, MACD 등)를 직접 인용하여** 왜 이 목표가가 타당한지 정량적/기술적으로 증명하십시오.\n"
                    f"</ANALYSIS_티커숫자>\n\n"
                    f"※ 마지막 줄은 아래 파싱 형식으로 출력 (손절가 필수)\n"
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
                    if len(data := line.split('|')) >= 9: 
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
                                specific_analysis = match.group(1).strip() if (match := re.search(f"<ANALYSIS_{code}>(.*?)</ANALYSIS_{code}>", raw, re.DOTALL)) else display_text
                                c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, sl_s, sl_m, sl_l, scrap_date, model_used, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                          (f"{name} 퀀트 심층분석", specific_analysis, name, tick, current, tp_s, tp_m, tp_l, bp, sl_s, sl_m, sl_l, datetime.now().strftime("%Y-%m-%d %H:%M"), MODEL_NAME, current_user))
                                conn.commit(); st.success(f"✅ 리포트 스크랩 완료!")

# =======================================================
# 탭 5: 관심종목 진단
# =======================================================
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
                    with st.spinner("파이썬 타임프레임 연산 및 수치 방어 논리 작성 중..."):
                        tech = get_technical_data(code)
                        fund = get_advanced_fundamental_data(code)
                        news_raw = fetch_stock_news(name, display=5)
                        dart_raw = get_dart_filings(code)
                        
                        lite_summary = call_gemini_lite_summary(f"뉴스 및 공시:\n{chr(10).join([n['title'] for n in news_raw])}\n{dart_raw}")
                        
                        current_price = tech['current'] if tech else current
                        daily_vol = tech['daily_volatility'] if tech else 0.0
                        eps_val = fund.get('eps', 0.0)
                        bps_val = fund.get('bps', 0.0)
                        roe_history = fund.get('roe_history', [])
                        eps_history = fund.get('eps_history', [])
                        
                        try: float_per = float(fund['per'].replace(',', '')) if fund['per'] != '-' else 0.0
                        except: float_per = 0.0
                        try: float_ind_per = float(fund['industry_per'].replace(',', '')) if fund['industry_per'] != '-' else 0.0
                        except: float_ind_per = 0.0

                        # [PEG] 최근 EPS 추세 기반 성장률 (-50%~+100% 범위로 클램핑하여 극단값 방어)
                        eps_growth = 0.0
                        if len(eps_history) >= 2 and eps_history[0] != 0:
                            eps_growth = (eps_history[-1] - eps_history[0]) / abs(eps_history[0])
                            eps_growth = min(max(eps_growth, -0.5), 1.0)

                        # [PBR 하한선 방어] 적자가 지속되며 확대되는 추세면 BPS 청산가치에 할인 적용
                        bps_discount = 1.0
                        if len(eps_history) >= 2 and eps_history[-1] < 0 and eps_history[0] < 0 and eps_history[-1] < eps_history[0]:
                            bps_discount = 0.8
                        
                        calc_sl_s = current_price * (1 - k_factor * daily_vol * np.sqrt(20)) if daily_vol > 0 else 0.0
                        calc_sl_m = current_price * (1 - k_factor * daily_vol * np.sqrt(60)) if daily_vol > 0 else 0.0
                        calc_sl_l = current_price * (1 - k_factor * daily_vol * np.sqrt(250)) if daily_vol > 0 else 0.0

                        if eps_val <= 0:
                            conservative_tp = (bps_val * bps_discount) if bps_val > 0 else current_price * 0.9
                            neutral_tp = current_price * (1 + k_factor * daily_vol * np.sqrt(60)) if daily_vol > 0 else current_price * 1.2
                            aggressive_tp = current_price * (1 + k_factor * daily_vol * np.sqrt(250)) if daily_vol > 0 else current_price * 1.5

                            fund_target_log = (
                                f"   - [보수적 시나리오] BPS 청산가치 방어선(할인율 {int((1-bps_discount)*100)}%): {conservative_tp:,.0f}원\n"
                                f"   - [중립적 시나리오] 60일 기술적 상방 변동성 밴드: {neutral_tp:,.0f}원\n"
                                f"   - [공격적 시나리오] 250일 장기 추세 돌파 밴드: {aggressive_tp:,.0f}원\n"
                            )
                        else:
                            conservative_tp = (bps_val * bps_discount) if bps_val > 0 else current_price * 0.9
                            adjusted_ind_per = float_ind_per * (1 + eps_growth) if float_ind_per > 0 else 0.0
                            base_tp = eps_val * adjusted_ind_per if adjusted_ind_per > 0 else eps_val * 10
                            required_return = 0.08
                            expected_roe = (roe_history[-1] / 100) if roe_history else 0.05
                            rim_tp = bps_val + (bps_val * (expected_roe - required_return) / required_return) if bps_val > 0 else current_price * 1.1
                            aggressive_tp = max(base_tp, rim_tp)
                            
                            fund_target_log = (
                                f"   - [보수적 시나리오] BPS 자산가치 기준(할인율 {int((1-bps_discount)*100)}%): {conservative_tp:,.0f}원\n"
                                f"   - [중립적 시나리오] 성장률 반영 업종PER 기준(EPS성장률 {eps_growth*100:+.1f}%): {base_tp:,.0f}원\n"
                                f"   - [공격적 시나리오] RIM 초과이익 기준: {aggressive_tp:,.0f}원\n"
                            )

                        calc_result_log = (
                            f"▶ 리스크 분석 팩트: 일간 변동성 {daily_vol*100:.2f}% 기준 (k={k_factor:.1f})\n"
                            f"   - 단기 손절가: {calc_sl_s:,.0f}원 | 중기 손절가: {calc_sl_m:,.0f}원 | 장기 손절가: {calc_sl_l:,.0f}원\n"
                            f"▶ 파이썬 선행연산 목표가 밴드:\n{fund_target_log}"
                        )

                        data_str = f"현재가: {current_price:,.0f}\n"
                        if is_owned and avg_price > 0: data_str += f"[내 계좌 정보] 평단가: {avg_price:,.0f} | 현재 수익률: {((current_price - avg_price) / avg_price * 100):+.1f}%\n"
                        if tech: data_str += f"[차트/리스크] 20일선 {tech['ma20']:,.0f} | 60일선 {tech['ma60']:,.0f} | MACD {tech['macd']:,.2f} | 20일 변동성(일간) {daily_vol*100:.2f}%\n"
                        if fund: data_str += f"[재무 비율] PER {fund['per']} (업종PER {fund['industry_per']}) | PBR {fund['pbr']} | EPS {eps_val:,}원 | BPS {bps_val:,}원\n"
                        data_str += f"{calc_result_log}\n[요약]\n{lite_summary}"
                        
                        prompt = (f"[{name} 진단]\n[팩트 데이터]\n{data_str}\n\n"
                                  f"당신은 리스크 관리에 철저한 애널리스트입니다.\n"
                                  f"1. 기계적인 장단점 나열에 앞서, **'왜 수많은 주식 중 이 종목을 지금 매수/보유/매도해야 하는가?'**에 대한 핵심 논리를 최상단에 선언하십시오.\n"
                                  f"2. **절대 가격이나 수식을 직접 계산하여 사칙연산 오류를 내지 마십시오.** 파이썬이 선행 연산하여 제공한 3가지 목표가 시나리오 밴드 가격 중 가장 실현 타당한 가치 가격을 '선택'하십시오.\n"
                                  f"3. 낙관적 편향 제거를 위해 반드시 <BULL_CASE>와 <BEAR_CASE>를 분리하여 자가 검열하십시오.\n"
                                  f"4. 내 계좌 정보가 있다면 수익률을 참고하여 '추가매수/유지/손절' 여부를 객관적으로 제시하십시오.\n\n"
                                  f"=== 작성 항목 ===\n"
                                  f"**🎯 핵심 아이디어 (Why Buy/Hold/Sell?)**\n"
                                  f"- (현재 시점에서 이 종목을 매수/보유/매도해야 하는 가장 강력하고 결정적인 한 가지 이유)\n"
                                  f"**🟢 강세 논리 (Bull Case)**\n"
                                  f"**🔴 약세/위험 논리 (Bear Case)**\n"
                                  f"**⚖️ 최종 판단 및 리스크 평가**\n"
                                  f"- 목표가 도달 논증 (구체적 수치 인용 필수): 파이썬이 제공한 3대 목표가 시나리오 중 최종 선택한 단기/중기/장기 가격을 명시하십시오. 그리고 **반드시 본문에 제공된 팩트 수치(EPS, BPS, 평단가, 수익률, 20일 변동성, 이평선 등)를 직접 인용하여** 왜 이 목표가가 타당한지 정량적/기술적으로 증명하십시오. 두루뭉술한 표현을 배제하고 철저히 숫자로 방어하십시오.\n"
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

# =======================================================
# 탭 6: 스크랩북
# =======================================================
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

            # [k값 튜닝 힌트] 스크랩북 성과 데이터 기반 참고용 제안 (자동 반영 아님)
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
