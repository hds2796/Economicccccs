import streamlit as st
import json
import sqlite3
import re
import threading
import requests
import pandas as pd
import urllib.request
import urllib.parse
import os
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google import genai

MODEL_NAME = "gemini-3.5-flash"
LITE_MODEL_NAME = "gemini-3.1-flash-lite"

st.set_page_config(page_title="Project2_Stock", page_icon="📊", layout="wide")

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
# 데이터베이스 초기화 및 DART 고유번호 캐싱
# =======================================================
@st.cache_resource
def init_db():
    try:
        connection = sqlite3.connect('market_analysis.db', check_same_thread=False)
        cursor = connection.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS scrapbook (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, link TEXT, summary TEXT, analysis TEXT, scrap_date TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS portfolio (id INTEGER PRIMARY KEY AUTOINCREMENT, stock_name TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS sentiment_history (id INTEGER PRIMARY KEY AUTOINCREMENT, calc_date TEXT, score REAL)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS dart_corp_codes (corp_code TEXT, corp_name TEXT, stock_code TEXT PRIMARY KEY)''')
        connection.commit()

        columns_to_add = [
            ("portfolio", "is_owned", "INTEGER DEFAULT 0"), ("portfolio", "avg_price", "REAL DEFAULT 0.0"),
            ("portfolio", "quantity", "INTEGER DEFAULT 0"), ("portfolio", "report_text", "TEXT"),
            ("portfolio", "tp_s", "REAL DEFAULT 0.0"), ("portfolio", "tp_m", "REAL DEFAULT 0.0"),
            ("portfolio", "tp_l", "REAL DEFAULT 0.0"), ("portfolio", "bp", "REAL DEFAULT 0.0"),
            ("scrapbook", "stock_name", "TEXT"), ("scrapbook", "ticker", "TEXT"),
            ("scrapbook", "saved_price", "REAL DEFAULT 0.0"), ("scrapbook", "target_price", "REAL DEFAULT 0.0"),
            ("scrapbook", "target_price_mid", "REAL DEFAULT 0.0"), ("scrapbook", "target_price_long", "REAL DEFAULT 0.0"),
            ("scrapbook", "buy_recommend_price", "REAL DEFAULT 0.0"), ("portfolio", "model_used", "TEXT"),
            ("portfolio", "report_time", "TEXT"), ("portfolio", "ticker", "TEXT"), ("scrapbook", "model_used", "TEXT"),
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
        print(f"[DB Init Error] {e}")
        return None

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
    except Exception as e:
        print(f"[DART Init Error] {e}")

initialize_dart_codes()

# =======================================================
# 드라이브 백업 및 복구 로직
# =======================================================
def get_drive_service_for_file():
    info = json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive'])
    return build('drive', 'v3', credentials=creds)

def backup_db_to_drive():
    try:
        conn.commit()
        drive_service = get_drive_service_for_file()
        folder_id = st.secrets.get("GOOGLE_BACKUP_FOLDER_ID", "").strip()
        file_name = "market_analysis.db"
        if not folder_id: return False
        query = f"'{folder_id}' in parents and name = '{file_name}' and trashed = false"
        results = drive_service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])
        if files:
            media = MediaFileUpload(file_name, mimetype='application/octet-stream', resumable=True)
            file_id = files[0]['id']
            drive_service.files().update(fileId=file_id, media_body=media).execute()
            return True
        else:
            st.error("구글 드라이브 폴더에서 'market_analysis.db' 파일을 찾지 못했습니다.")
            return False
    except Exception as e:
        st.error(f"백업 실패: {e}")
        return False

def restore_db_from_drive():
    try:
        drive_service = get_drive_service_for_file()
        folder_id = st.secrets.get("GOOGLE_BACKUP_FOLDER_ID", "").strip()
        file_name = "market_analysis.db"
        query = f"'{folder_id}' in parents and name = '{file_name}' and trashed = false"
        results = drive_service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])
        if not files: return False
        file_id = files[0]['id']
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: status, done = downloader.next_chunk()
        conn.close() 
        with open(file_name, 'wb') as f:
            f.write(fh.getvalue())
        return True
    except Exception as e: 
        print(f"[Restore DB Error] {e}")
        return False

with st.sidebar:
    st.markdown(f"**👤 접속 계정:** `{current_user}`")
    st.divider()
    st.subheader("⚙️ 데이터베이스 관리")
    if st.button("☁️ 구글 드라이브 백업", use_container_width=True):
        with st.spinner("클라우드 백업 중..."):
            if backup_db_to_drive(): st.success("✅ 백업 완료")
    if st.button("🔄 드라이브에서 복구", use_container_width=True):
        with st.spinner("데이터 복구 중..."):
            if restore_db_from_drive():
                st.success("✅ 복구 완료! 새로고침 진행합니다.")
                st.rerun()

# =======================================================
# 투 트랙 API 호출 함수
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
                    return client.models.generate_content(model="gemini-3-preview", contents=prompt).text
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
        except Exception as e1:
            try:
                fallback_response = client.models.generate_content_stream(model="gemini-3-preview", contents=prompt)
                yield f"\n[안내] 3.5-flash 서버 응답 지연으로 인해 3-preview 모델로 우회하여 분석을 진행합니다.\n\n"
                for chunk in fallback_response:
                    if chunk.text: yield chunk.text
            except Exception as e2:
                yield f"\n최종 호출 실패 (Flash 및 Preview 모두 에러): {e2}"
    finally:
        _gemini_semaphore.release()

# =======================================================
# 크롤링 및 데이터 가공 유틸
# =======================================================
@st.cache_data(ttl=600)
def get_dart_filings(stock_code):
    if not DART_API_KEY: return "DART API 키 없음"
    try:
        c.execute("SELECT corp_code FROM dart_corp_codes WHERE stock_code = ?", (stock_code,))
        row = c.fetchone()
        if not row: return "DART 매핑 데이터 없음"
        corp_code = row[0]
        
        bgn_de = (datetime.now() - pd.Timedelta(days=90)).strftime("%Y%m%d")
        url = f"https://opendart.fss.or.kr/api/list.json?crtfc_key={DART_API_KEY}&corp_code={corp_code}&bgn_de={bgn_de}&page_count=5"
        res = requests.get(url, timeout=5).json()
        if res.get("status") == "000":
            filings = [f"- [{item['rcept_dt']}] {item['report_nm']}" for item in res.get("list", [])]
            return "\n".join(filings)
        return "최근 3개월 주요 공시 없음"
    except Exception as e: 
        print(f"[DART Fetch Error - {stock_code}] {e}")
        return "DART 조회 실패"

@st.cache_data(ttl=600)
def get_advanced_fundamental_data(code):
    data = {"per": "-", "pbr": "-", "eps": 0, "bps": 0, "industry_per": "-", "quarter_trend": "정보 없음", "supply_demand": "정보 없음", "eps_history": [], "roe_history": []}
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        
        per_elem = soup.find(id="_per")
        if per_elem: data["per"] = per_elem.get_text()
        pbr_elem = soup.find(id="_pbr")
        if pbr_elem: data["pbr"] = pbr_elem.get_text()
        
        th_elements = soup.find_all("th")
        for th in th_elements:
            if "동일업종 PER" in th.get_text():
                td = th.find_next("td")
                if td: data["industry_per"] = td.get_text().strip().replace('배', '')
        
        cop_table = soup.find("div", class_="cop_details")
        if cop_table:
            data["quarter_trend"] = "최근 실적 및 동적 연산 변수 수집 완료"
            try:
                th_list = cop_table.find_all("th")
                for th_item in th_list:
                    text = th_item.get_text().strip()
                    if "EPS(원)" in text:
                        tds = th_item.find_next_siblings("td")
                        valid_eps = []
                        for td in tds:
                            val = td.get_text().strip().replace(',', '')
                            if val and val.replace('.', '', 1).replace('-', '', 1).isdigit():
                                valid_eps.append(float(val))
                        if valid_eps:
                            data["eps"] = valid_eps[-1]
                            data["eps_history"] = valid_eps[-3:]
                    if "BPS(원)" in text:
                        tds = th_item.find_next_siblings("td")
                        valid_bps = []
                        for td in tds:
                            val = td.get_text().strip().replace(',', '')
                            if val and val.replace('.', '', 1).replace('-', '', 1).isdigit():
                                valid_bps.append(float(val))
                        if valid_bps: 
                            data["bps"] = valid_bps[-1]
                    if "ROE" in text:
                        tds = th_item.find_next_siblings("td")
                        valid_roe = []
                        for td in tds:
                            val = td.get_text().strip().replace(',', '')
                            if val and val.replace('.', '', 1).replace('-', '', 1).isdigit():
                                valid_roe.append(float(val))
                        if valid_roe: 
                            data["roe_history"] = valid_roe[-3:]
            except Exception as inner_e: 
                print(f"[Fundamental Parsing Error - {code}] {inner_e}")
            
        url_frgn = f"https://finance.naver.com/item/frgn.naver?code={code}"
        res_frgn = requests.get(url_frgn, headers=headers, timeout=5)
        soup_frgn = BeautifulSoup(res_frgn.text, "html.parser")
        
        data_rows = soup_frgn.find_all("tr", {"onmouseover": "mouseOver(this)"})
        inst_sum, fore_sum = 0, 0
        count = 0
        for tr in data_rows:
            if count >= 5: break
            tds = tr.find_all("td")
            if len(tds) >= 7:
                try:
                    inst_text = tds[5].get_text().strip().replace(',', '')
                    fore_text = tds[6].get_text().strip().replace(',', '')
                    inst = int(inst_text) if inst_text else 0
                    fore = int(fore_text) if fore_text else 0
                    inst_sum += inst
                    fore_sum += fore
                    count += 1
                except Exception:
                    pass
        if count > 0:
            data["supply_demand"] = f"최근 {count}일 누적 -> 기관: {inst_sum:+,}주 / 외국인: {fore_sum:+,}주"
        else:
            data["supply_demand"] = "수급 데이터 수집 불가"
            
    except Exception as e: 
        print(f"[Fundamental Data Error - {code}] {e}")
    return data

@st.cache_data(ttl=600)
def get_technical_data(code):
    try:
        url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=250&requestType=0"
        res = requests.get(url, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        items = soup.find_all('item')
        if not items: return None
        df_data = [float(item['data'].split('|')[4]) for item in items]
        if len(df_data) < 60: return None
        
        df = pd.Series(df_data)
        macd = df.ewm(span=12, adjust=False).mean() - df.ewm(span=26, adjust=False).mean()
        signal = macd.ewm(span=9, adjust=False).mean()
        return {
            "current": df_data[-1], "high_52": max(df_data), "low_52": min(df_data),
            "ma20": sum(df_data[-20:]) / 20, "ma60": sum(df_data[-60:]) / 60, "macd": macd.iloc[-1], "signal": signal.iloc[-1]
        }
    except Exception as e: 
        print(f"[Technical Data Error - {code}] {e}")
        return None

@st.cache_data(ttl=600)
def fetch_stock_news(query, display=5):
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET: return []
    try:
        url = f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={urllib.parse.quote(query)}&display={display}&sort=date&format=json"
        req = urllib.request.Request(url, headers={"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET})
        with urllib.request.urlopen(req, timeout=3) as response:
            res = json.loads(response.read().decode('utf-8'))
            return [{"title": BeautifulSoup(i['title'], "html.parser").get_text(), "link": i['link']} for i in res.get("items", [])]
    except Exception as e: 
        print(f"[Stock News Fetch Error - {query}] {e}")
        return []

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
    except Exception as e: 
        print(f"[Stock Search Error - {name}] {e}")
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
                    found = find_datas(v)
                    if found is not None: return found
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
    except Exception as e: 
        print(f"[Current Prices Fetch Error] {e}")
        return {}

def dedupe_news(news_list):
    seen = set(); out = []
    for n in news_list or []:
        key = n.get("title", "").strip()
        if not key or key in seen: continue
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
        files = results.get('files', [])
        if not files: return None
        request = drive_service.files().get_media(fileId=files[0]['id'])
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: status, done = downloader.next_chunk()
        fh.seek(0)
        return json.loads(fh.read().decode('utf-8'))
    except Exception as e: 
        print(f"[Cached Data Fetch Error] {e}")
        return None

def fetch_realtime_data_direct():
    if not API_GATEWAY_REALTIME_URL: return None
    try:
        payload = {"seen_links": []} 
        res = requests.post(API_GATEWAY_REALTIME_URL, json=payload, timeout=30)
        res.raise_for_status()
        return res.json()
    except Exception as e: 
        print(f"[Realtime API Error] {e}")
        return None

# =======================================================
# 상태 변수 선언 및 데이터 누적 병합
# =======================================================
cached_data = fetch_cached_global_data() or {}

if "realtime_cache" not in st.session_state: 
    st.session_state.realtime_cache = {
        "market_status": cached_data.get("market_status", {}),
        "realtime_news": cached_data.get("realtime_news", []),
        "sectors": cached_data.get("sectors") or cached_data.get("sector_news", {}),
        "updated_at": cached_data.get("updated_at", "대기 중")
    }

def merge_realtime_data(new_data):
    if not new_data: return
    old_data = st.session_state.realtime_cache
    
    old_market = old_data.get("market_status", {})
    old_market.update(new_data.get("market_status", {}))
    
    merged_news = dedupe_news(new_data.get("realtime_news", []) + old_data.get("realtime_news", []))
    
    old_sector = old_data.get("sectors") or old_data.get("sector_news", {})
    new_sector = new_data.get("sectors") or new_data.get("sector_news", {})
    
    merged_sector = {}
    all_keys = set(old_sector.keys()).union(new_sector.keys())
    for sec in all_keys:
        merged_sector[sec] = dedupe_news(new_sector.get(sec, []) + old_sector.get(sec, []))
        
    st.session_state.realtime_cache = {
        "market_status": old_market,
        "realtime_news": merged_news,
        "sectors": merged_sector,
        "updated_at": new_data.get("updated_at", old_data.get("updated_at", "알 수 없음"))
    }

if not st.session_state.realtime_cache.get("realtime_news"):
    with st.spinner("데이터 로딩 중..."):
        new_data = fetch_realtime_data_direct()
        if new_data:
            merge_realtime_data(new_data)

g_data = st.session_state.realtime_cache

col_title, col_refresh = st.columns([5, 1.2])
with col_refresh:
    if st.button("실시간 갱신", use_container_width=True):
        with st.spinner("갱신 중..."):
            new_data = fetch_realtime_data_direct()
            if new_data:
                merge_realtime_data(new_data)
                st.rerun()

with col_title:
    st.caption(f"실시간(누적): {g_data.get('updated_at', '알 수 없음')} | 캐시: {cached_data.get('updated_at', '알 수 없음')}")

market_data = g_data.get("market_status", {})
target_indices = ["코스피", "코스닥", "S&P 500", "원/달러 환율"]
cols = st.columns(4)
for i, key in enumerate(target_indices):
    with cols[i]:
        if key in market_data:
            data = market_data[key]
            val, diff, diff_pct = data.get("current", 0.0), data.get("diff", 0.0), data.get("diff_pct", 0.0)
            if val == 0.0: st.metric(label=key, value="점검중")
            else: st.metric(label=key, value=f"{val:,.2f}", delta=f"{diff:+.2f} ({diff_pct:+.2f}%)")

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
            for idx, n in enumerate(news_pool[:10]): 
                st.markdown(f"{idx+1}. [{n['title']}]({n['link']})")
    else:
        st.info("현재 수집된 실시간 뉴스가 없습니다.")

    if st.button("브리핑 생성", key="btn_briefing"):
        if not news_pool:
            st.error("분석할 뉴스가 없습니다.")
        else:
            news_str = "\n".join([f"- {n['title']}: {n.get('description', '')}" for n in news_pool[:50]])
            
            with st.spinner("Lite 모델이 시황 뉴스를 상세히 압축 중..."):
                summary_prompt = f"다음 실시간 속보 뉴스를 읽고, 누락 없이 중요한 시장 호악재 요소를 상세히 통합 요약하라. 분량 제한 없이 정보 가치를 보존하라:\n\n{news_str}"
                lite_summary = call_gemini_lite_summary(summary_prompt)
                
            with st.spinner("Flash 3.5 모델이 시장 변화 및 의의를 분석 중..."):
                analysis_prompt = (f"지표 데이터:\n{json.dumps(market_data)}\n\n[Lite 전처리 요약본]\n{lite_summary}\n\n"
                                   f"위 요약 자료와 지표를 근거로, 현재 주식시장의 흐름이 가지는 '구체적 의미'와 '향후 증시에 가져올 변화'를 철저히 객관적인 관점에서 심층 분석하여 서술하라.")
                st.write_stream(call_gemini_stream_with_fallback(analysis_prompt))

# =======================================================
# 탭 2: 핵심 경제
# =======================================================
with tab2:
    st.subheader("핵심 경제 종합 브리핑 및 심리 지수")
    
    c.execute("SELECT calc_date, score FROM sentiment_history ORDER BY calc_date ASC")
    sentiment_rows = c.fetchall()
    
    if sentiment_rows:
        df_sent = pd.DataFrame(sentiment_rows, columns=['date', 'score'])
        df_sent['date'] = pd.to_datetime(df_sent['date']).dt.strftime('%Y-%m-%d')
        df_avg = df_sent.groupby('date')['score'].mean().reset_index()
        df_avg.set_index('date', inplace=True)
        
        st.markdown("#### 📈 주간/월간 시장 심리 지수 추이 (일별 평균)")
        st.line_chart(df_avg['score'])
        latest_score = df_avg.iloc[-1]['score']
        st.metric("가장 최근의 시장 심리 지수", f"{latest_score:.1f} / 100", help="0=극단적 공포, 50=중립, 100=극단적 탐욕")
        st.divider()

    eco_news = cached_data.get("eco_news", [])
    
    if st.button("거시경제 종합 브리핑 생성 (Top 50 뉴스 기반)", use_container_width=True):
        if not eco_news:
            st.error("분석할 경제 뉴스가 없습니다.")
        else:
            with st.spinner("Lite 모델이 핵심 경제 뉴스 50건을 심층 요약 중..."):
                eco_str = "\n".join([f"- {n['title']}: {n.get('summary', '')}" for n in eco_news[:50]])
                lite_prompt = f"다음은 인공지능이 중요도 순으로 선별한 핵심 경제 뉴스 50건이다. 누락 없이 오늘의 시황, 주요 호재와 악재를 상세히 통합 요약하라:\n\n{eco_str}"
                lite_summary = call_gemini_lite_summary(lite_prompt)
            
            with st.spinner("Flash 3.5 모델이 종합 브리핑 및 시장 심리 지수를 산출 중..."):
                flash_prompt = (
                    f"지표 데이터:\n{json.dumps(market_data)}\n\n[경제 뉴스 전처리 요약본]\n{lite_summary}\n\n"
                    f"당신은 거시경제 수석 애널리스트입니다. 위 데이터를 바탕으로 다음 항목이 포함된 종합 브리핑을 작성하십시오.\n"
                    f"1. 오늘의 시황 요약\n"
                    f"2. 주요 호재와 악재\n"
                    f"3. 주식시장에 미치는 영향\n\n"
                    f"그리고 이 모든 상황을 종합하여 '오늘의 시장 심리 지수(Sentiment Index)'를 0부터 100 사이의 숫자로 산출하십시오. "
                    f"(0=극단적 공포/폭락장, 50=중립, 100=극단적 탐욕/폭등장)\n\n"
                    f"※ 리포트 작성을 마친 후, 반드시 맨 마지막 줄에 아래 파싱 형식으로 심리 지수 점수만 출력하십시오.\n"
                    f"[SENTIMENT_SCORE]: 점수숫자"
                )
                
                full_report = "".join(call_gemini_stream_with_fallback(flash_prompt))
                
                score_match = re.search(r'\[SENTIMENT_SCORE\]:\s*(\d+)', full_report)
                if score_match:
                    score_val = float(score_match.group(1))
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    c.execute("INSERT INTO sentiment_history (calc_date, score) VALUES (?, ?)", (today_str, score_val))
                    conn.commit()
                
                display_text = re.sub(r'\[SENTIMENT_SCORE\]:.*', '', full_report).strip()
                st.session_state.eco_briefing = display_text
                st.rerun()

    if st.session_state.get('eco_briefing'):
        with st.expander("📝 오늘의 거시경제 종합 브리핑"):
            st.write(st.session_state.eco_briefing)

    st.subheader("핵심 경제 뉴스 목록")
    if eco_news:
        for idx, n in enumerate(eco_news[:10]):
            st.markdown(f"**[{idx+1}] {n['title']}**")
            if st.button("개별 심층 분석", key=f"eco_an_{idx}"):
                with st.spinner("Lite 전처리 및 Flash 3.5 의미론적 분석 진행 중..."):
                    l_sum = call_gemini_lite_summary(f"본 뉴스의 핵심적 사실을 왜곡 없이 상세히 요약하라:\n{n['title']}")
                    flash_p = f"[뉴스 요약]\n{l_sum}\n\n이 사실이 거시 경제 및 관련 주식 섹터에 파급 효과와 거시적 변화 의의를 분석하라."
                    st.write(call_gemini_with_fallback(flash_p))
    else: 
        st.info("조회된 핵심 경제 뉴스가 없습니다.")

# =======================================================
# 탭 3: 섹터 뉴스
# =======================================================
with tab3:
    st.subheader("섹터별 모멘텀 분석")
    sec_news = g_data.get("sectors") or cached_data.get("sectors") or g_data.get("sector_news", {})
    
    if sec_news:
        has_items = False
        for sec, items in sec_news.items():
            if not items: continue
            has_items = True
            with st.expander(f"📁 {sec} 섹터 뉴스 ({len(items)}건)"):
                for i in items:
                    st.markdown(f"- [{i['title']}]({i.get('link', '#')})")
                
                if st.button(f"{sec} 종합 전망 분석", key=f"sec_btn_{sec}"):
                    titles = "\n".join([f"- {i['title']}" for i in items])
                    with st.spinner("Lite 섹터 통합 전처리 중..."):
                        lite_s = call_gemini_lite_summary(f"다음 {sec} 섹터 뉴스들의 핵심 트렌드와 호악재 요인을 상세히 기술하라:\n{titles}")
                    with st.spinner("Flash 3.5 주도주 변화 예측 중..."):
                        flash_p = f"[{sec} 섹터 이슈 요약]\n{lite_s}\n\n이 트렌드가 향후 {sec} 섹터 내 주도주 흐름에 가져올 변화와 주식시장에 미칠 파장을 분석하라."
                        st.write(call_gemini_with_fallback(flash_p))
        if not has_items:
            st.info("현재 매칭된 섹터별 뉴스가 없습니다.")
    else:
        st.info("현재 매칭된 섹터별 뉴스가 없습니다.")

# =======================================================
# 탭 4: 종목 발굴 (💡 동적 클램프 해제 및 적자 방어 로직)
# =======================================================
with tab4:
    st.subheader("종목 발굴 (심층 분석)")
    investment_horizon = st.radio("투자기간", ["단기 (1~3개월)", "중기 (3~6개월)", "장기 (1년 이상)"], horizontal=True)

    if st.button("추천 종목 발굴", use_container_width=True, key="btn_recommend"):
        rec_news = dedupe_news((g_data.get("realtime_news", []) if g_data else []) + (cached_data.get("eco_news", []) if cached_data else []))
        
        if not rec_news: st.error("분석 대상 뉴스 풀이 비어있습니다.")
        else:
            with st.spinner("[1단계] 1차 후보군 10개 추출 중..."):
                articles_str = "\n".join([f"- {n['title']}" for n in rec_news[:50]])
                step1_prompt = (f"경제 뉴스를 바탕으로 투자기간 [{investment_horizon}] 동안 상승 모멘텀이 뛰어난 "
                                f"한국 주식 종목 10개를 골라 종목코드 6자리만 JSON 배열로 출력하라.\n오직 JSON만 출력할 것.\n\n{articles_str}")
                step1_res = call_gemini_with_fallback(step1_prompt, model=LITE_MODEL_NAME)
                match = re.search(r'\[.*\]', step1_res, re.DOTALL)
                selected_tickers = []
                if match:
                    try: selected_tickers = json.loads(match.group(0))[:10]
                    except: pass
                if not selected_tickers: st.stop()
            
            with st.spinner("[2단계] 후보군 심층 데이터 크롤링 및 정직한 퀀트 수식 계산 중..."):
                tech_data_str = ""
                for ticker in selected_tickers:
                    ticker = re.sub(r'[^\d]', '', ticker)
                    if len(ticker) != 6: continue
                    try:
                        res = requests.get(f"https://m.stock.naver.com/api/stock/{ticker}/basic", timeout=3).json()
                        name = res.get("stockName", ticker)
                    except: name = ticker
                    
                    tech = get_technical_data(ticker)
                    fund = get_advanced_fundamental_data(ticker)
                    dart_info = get_dart_filings(ticker)
                    news_raw = fetch_stock_news(name, display=4)
                    news_str = "\n".join([n['title'] for n in news_raw])
                    
                    summary_prompt = f"다음은 {name} 종목의 공시와 관련 뉴스 리스트다. 사실 위주로 통합 요약하라:\n[공시]\n{dart_info}\n[뉴스]\n{news_str}"
                    lite_summary = call_gemini_lite_summary(summary_prompt)
                    
                    try: float_per = float(fund['per'].replace(',', '')) if fund['per'] != '-' else 0.0
                    except: float_per = 0.0
                    
                    try: float_ind_per = float(fund['industry_per'].replace(',', '')) if fund['industry_per'] != '-' else 0.0
                    except: float_ind_per = 0.0
                    
                    current_price = tech['current'] if tech else 0.0
                    eps_val = fund.get('eps', 0.0)
                    bps_val = fund.get('bps', 0.0)
                    eps_history = fund.get('eps_history', [])
                    roe_history = fund.get('roe_history', [])
                    
                    calc_result_log = ""
                    # 💡 퀀트 로직 개선: 적자 시 가짜 정밀도를 버리고 플랜 B(자산/매출) 평가 유도
                    if investment_horizon == "단기 (1~3개월)":
                        if eps_val <= 0:
                            calc_result_log = f"▶ 파이썬 연산 팩트 [단기 PEG]: 산출 불가 (적자 지속). AI는 차트 수급과 뉴스 모멘텀만으로 기술적 목표가를 산출할 것."
                        else:
                            if len(eps_history) >= 2 and eps_history[-2] > 0:
                                eps_growth_rate = ((eps_history[-1] - eps_history[-2]) / eps_history[-2]) * 100
                            else:
                                eps_growth_rate = 10.0 # 최소한의 정상 기업 성장률 가정
                                
                            if eps_growth_rate > 0 and float_per > 0:
                                peg_val = float_per / eps_growth_rate
                                calc_result_log = f"▶ 파이썬 연산 팩트 [단기 PEG]: {peg_val:.2f} (계산식: PER {float_per} / 실적 데이터 기반 동적 이익성장률 {eps_growth_rate:.1f}%)"
                            else:
                                calc_result_log = f"▶ 파이썬 연산 팩트 [단기 PEG]: 산출 불가 (역성장 우려). AI는 모멘텀 위주로 분석할 것."
                        
                    elif investment_horizon == "중기 (3~6개월)":
                        if eps_val <= 0:
                            calc_result_log = f"▶ 파이썬 연산 팩트 [중기 상대가치]: 이익(PER) 기준 산출 불가. 자산(BPS {bps_val:,}원) 기준 하한선만 고려하여 AI가 모멘텀 기반으로 목표가 도출 요망."
                        else:
                            target_valuation_price = eps_val * float_ind_per if float_ind_per > 0 else eps_val * 10 # 업종 PER 없으면 보수적 10배
                            calc_result_log = f"▶ 파이썬 연산 팩트 [중기 상대가치 적정가]: {target_valuation_price:,.0f}원 (계산식: EPS {eps_val:,}원 × 업종평균 PER {float_ind_per}배)"
                        
                    else: # 장기
                        if len(roe_history) == 3:
                            expected_roe = ((roe_history[2] * 3) + (roe_history[1] * 2) + (roe_history[0] * 1)) / 6 / 100
                        elif len(roe_history) > 0:
                            expected_roe = roe_history[-1] / 100
                        else:
                            expected_roe = 0.05
                            
                        # 시총 구분이 어려우므로 기본 요구수익률 8% 유지하되, 마이너스 ROE 그대로 수용 (가짜 클램프 철거)
                        required_return = 0.08
                        
                        if bps_val > 0:
                            residual_income = bps_val * (expected_roe - required_return)
                            rim_intrinsic_value = bps_val + (residual_income / required_return)
                            if rim_intrinsic_value < 0: 
                                rim_intrinsic_value = 0 # 기업 가치 완전 훼손
                            calc_result_log = f"▶ 파이썬 연산 팩트 [장기 RIM 내재가치]: {rim_intrinsic_value:,.0f}원 (계산식: 주당자산 BPS {bps_val:,}원, 3개년 가중평균 ROE {expected_roe*100:.1f}%, 요구수익률 8% 대입)"
                        else:
                            calc_result_log = f"▶ 파이썬 연산 팩트 [장기 RIM 내재가치]: 자본 잠식으로 산출 불가. 펀더멘털 투자 부적합 판정 요망."

                    tech_data_str += f"[{name} ({ticker})]\n"
                    if tech:
                        tech_data_str += f"- 차트 지표: 현재가 {tech['current']:,.0f} | 20일선 {tech['ma20']:,.0f} | 60일선 {tech['ma60']:,.0f} | MACD {tech['macd']:,.2f} | Signal {tech['signal']:,.2f}\n"
                    if fund:
                        tech_data_str += f"- 펀더멘털: 종목PER {fund['per']} (업종PER {fund['industry_per']}) | PBR {fund['pbr']} | EPS {eps_val:,}원 | BPS {bps_val:,}원\n"
                        tech_data_str += f"- 수급동향: {fund['supply_demand']}\n"
                    tech_data_str += f"{calc_result_log}\n"
                    tech_data_str += f"- 뉴스 및 공시 요약본:\n{lite_summary}\n\n"
            
            with st.spinner("[3단계] Flash 3.5 기반 퀀트/모멘텀 유동적 최종 분석 중..."):
                step3_prompt = (
                    f"당신은 감정을 철저히 배제하는 퀀트 애널리스트이자 분석 전문가입니다. 데이터 정제 엔진(Python)이 "
                    f"연산하여 본문에 주입해 준 '파이썬 연산 팩트' 지표들을 바탕으로 리포트를 작성하십시오.\n\n"
                    f"[선택된 투자 기간]: {investment_horizon}\n"
                    f"[10개 후보군 팩트 데이터]\n{tech_data_str}\n\n"
                    f"=== ⚠️ AI 목표가 산출 핵심 지침 (중요) ===\n"
                    f"1. 파이썬 연산 팩트에 정상적인 가격이 있다면 이를 '기본 하/상한선'으로 참고하여 차트/뉴스 가중치를 부여해 목표가를 산출하십시오.\n"
                    f"2. 만약 파이썬 연산 팩트가 '산출 불가(적자 지속)' 등으로 나왔다면 펀더멘털 가치 평가는 포기하십시오. 대신 차트의 저항선 매물대와 뉴스의 모멘텀 파급력에 100% 가중치를 두고 '기술적/모멘텀 목표가'를 철저히 객관적으로 산출하십시오.\n\n"
                    f"=== 리포트 작성 지시 ===\n"
                    f"1. 10개 후보군 중 가장 확실한 매수 매력도(또는 강한 모멘텀)를 가진 **최상위 3개(Top 3) 종목만 엄선**하여 리포트를 작성하십시오.\n"
                    f"2. 각 종목 분석은 반드시 <ANALYSIS_티커숫자> 와 </ANALYSIS_티커숫자> 태그로 감싸십시오.\n"
                    f"<ANALYSIS_티커숫자>\n"
                    f"### [종목명] (티커)\n"
                    f"- 투자 의견 및 정량적 매수 근거\n"
                    f"- 가치/모멘텀 평가 해설: (퀀트가 가능하면 퀀트를 인용하고, 산출 불가 시 기술적/모멘텀 평가임을 명시)\n"
                    f"- 차트 및 기술적 지표 분석\n"
                    f"- 뉴스 및 공시 모멘텀의 시장 파급 효과\n"
                    f"- 목표가 및 진입 타점\n"
                    f"</ANALYSIS_티커숫자>\n\n"
                    f"※ 반드시 마지막 줄은 아래 파싱 형식으로 오직 3개의 종목 데이터만 출력하십시오. (목표가는 AI가 최종 조정한 수치로 입력)\n"
                    f"[TRACKING_DATA]\n"
                    f"종목명|티커|단기목표가|중기목표가|장기목표가|진입타점"
                )
                st.session_state.today_recommendation = "".join(call_gemini_stream_with_fallback(step3_prompt))

    if st.session_state.get('today_recommendation'):
        raw = st.session_state.today_recommendation
        with st.expander("추천 리포트"):
            display_text = raw.split("[TRACKING_DATA]")[0].strip()
            display_text = re.sub(r'</?ANALYSIS_[^>]+>', '', display_text)
            st.write(display_text)
            st.caption(f"🧠 전처리: {LITE_MODEL_NAME} | 최종 분석: {MODEL_NAME}")
            
            if "[TRACKING_DATA]" in raw:
                block = raw.split("[TRACKING_DATA]")[1].strip().replace("```", "")
                parsed_rows = []
                for line in block.split('\n'):
                    if not line.strip(): continue
                    data = line.split('|')
                    if len(data) >= 6: 
                        parsed_rows.append((data[0].strip(), data[1].strip(), parse_won(data[2]), parse_won(data[3]), parse_won(data[4]), parse_won(data[5])))
                price_map = fetch_current_prices([r[1] for r in parsed_rows])
                cols_rec = st.columns(3)
                
                for idx, (name, tick, tp_s, tp_m, tp_l, bp) in enumerate(parsed_rows):
                    code = re.sub(r'[^\d]', '', tick)
                    price_info = price_map.get(code, {})
                    current, diff, diff_pct = price_info.get("current", 0.0), price_info.get("diff", 0.0), price_info.get("diff_pct", 0.0)
                    with cols_rec[idx % 3]:
                        with st.container(border=True):
                            st.markdown(f"**{name}** `{tick}`")
                            if current > 0: st.metric("현재가", f"{current:,.0f}", delta=f"{diff:+,.0f} ({diff_pct:+.2f}%)")
                            c_tp, c_bp = st.columns(2)
                            c_tp.markdown(f"**목표가 밴드**<br>단: {tp_s:,.0f}<br>중: {tp_m:,.0f}<br>장: {tp_l:,.0f}", unsafe_allow_html=True)
                            c_bp.metric("진입 타점", f"{bp:,.0f}")
                            
                            if st.button("스크랩", key=f"rec_s_{tick}", use_container_width=True):
                                match = re.search(f"<ANALYSIS_{code}>(.*?)</ANALYSIS_{code}>", raw, re.DOTALL)
                                specific_analysis = match.group(1).strip() if match else display_text
                                
                                c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, scrap_date, model_used, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                          (f"{name} 퀀트 심층분석", specific_analysis, name, tick, current, tp_s, tp_m, tp_l, bp, datetime.now().strftime("%Y-%m-%d %H:%M"), MODEL_NAME, current_user))
                                conn.commit()
                                st.success(f"✅ '{name}' 종목의 리포트가 내 스크랩북에 저장되었습니다!")

# =======================================================
# 탭 5: 관심종목 진단 (💡 퀀트 예외 분기 및 AI 가중치 시프트 완벽 반영)
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
            
            c.execute("INSERT INTO portfolio (stock_name, ticker, is_owned, avg_price, quantity, user_id) VALUES (?,?,?,?,?,?)", 
                      (new_s.strip(), code or '', is_owned_flag, final_avg_p, qty if is_owned_flag else 0, current_user))
            conn.commit()
            
            st.session_state["input_stock_name"] = ""
            st.session_state["input_avg_price"] = "0"
            st.session_state["input_quantity"] = 0
            
            st.success(f"'{new_s.strip()}' 종목이 관심종목에 등록되었습니다.")
            st.rerun()

    c.execute("SELECT id, stock_name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp, model_used, report_time, ticker FROM portfolio WHERE user_id = ?", (current_user,))
    portfolios = c.fetchall()
    
    if portfolios:
        st.divider()
        col_bulk, _ = st.columns([2, 8])
        with col_bulk:
            if st.button("🗑️ 선택 항목 삭제", key="bulk_del_t5", use_container_width=True):
                to_del = [p[0] for p in portfolios if st.session_state.get(f"chk_t5_{p[0]}", False)]
                if to_del:
                    c.execute(f"DELETE FROM portfolio WHERE id IN ({','.join(['?']*len(to_del))})", to_del)
                    conn.commit()
                    st.rerun()

        price_map_watch = fetch_current_prices([p[12] for p in portfolios if p[12]])

        for p in portfolios:
            p_id, name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp, model_used, report_time, ticker = p
            code = re.sub(r'[^\d]', '', ticker or "")
            price_info = price_map_watch.get(code, {})
            current, diff, diff_pct = price_info.get("current", 0.0), price_info.get("diff", 0.0), price_info.get("diff_pct", 0.0)

            st.markdown(f"### {name} `{code}`")
            col_sel, col_info, col_price, col_btn, col_del = st.columns([0.5, 3.5, 3, 1.5, 1.5])
            
            with col_sel:
                st.checkbox("선택", key=f"chk_t5_{p_id}", label_visibility="collapsed")
            with col_info:
                if is_owned:
                    st.caption(f"보유 | 평단: {avg_price:,.0f} | 수량: {quantity}")
                    if current > 0 and avg_price > 0: st.caption(f"수익률: {((current - avg_price) / avg_price * 100):+.1f}%")
            with col_price:
                if current > 0: st.metric("현재가", f"{current:,.0f}", delta=f"{diff:+,.0f} ({diff_pct:+.2f}%)")
            with col_btn:
                if st.button("진단 실행", key=f"run_{p_id}", use_container_width=True):
                    with st.spinner("파이썬 정직한 퀀트 수식 계산 및 AI 종합 진단 중..."):
                        tech = get_technical_data(code)
                        fund = get_advanced_fundamental_data(code)
                        news_raw = fetch_stock_news(name, display=5)
                        dart_raw = get_dart_filings(code)
                        
                        news_str = "\n".join([n['title'] for n in news_raw])
                        summary_prompt = f"다음 {name}의 뉴스와 공시 데이터를 누락 없이 상세하게 사실 위주로 통합 요약하라:\n[뉴스]\n{news_str}\n[공시]\n{dart_raw}"
                        lite_summary = call_gemini_lite_summary(summary_prompt)

                        try: float_per = float(fund['per'].replace(',', '')) if fund['per'] != '-' else 0.0
                        except: float_per = 0.0
                        
                        try: float_ind_per = float(fund['industry_per'].replace(',', '')) if fund['industry_per'] != '-' else 0.0
                        except: float_ind_per = 0.0
                        
                        current_price = tech['current'] if tech else current
                        eps_val = fund.get('eps', 0.0)
                        bps_val = fund.get('bps', 0.0)
                        eps_history = fund.get('eps_history', [])
                        roe_history = fund.get('roe_history', [])
                        
                        calc_result_log = ""
                        
                        # [1] 단기 PEG
                        if eps_val <= 0:
                            calc_result_log += f"▶ 파이썬 연산 팩트 [단기 PEG]: 산출 불가 (적자 지속).\n"
                        else:
                            if len(eps_history) >= 2 and eps_history[-2] > 0:
                                eps_growth_rate = ((eps_history[-1] - eps_history[-2]) / eps_history[-2]) * 100
                            else:
                                eps_growth_rate = 10.0
                            if eps_growth_rate > 0 and float_per > 0:
                                peg_val = float_per / eps_growth_rate
                                calc_result_log += f"▶ 파이썬 연산 팩트 [단기 PEG]: {peg_val:.2f} (계산식: PER {float_per} / 이익성장률 {eps_growth_rate:.1f}%)\n"
                            else:
                                calc_result_log += f"▶ 파이썬 연산 팩트 [단기 PEG]: 산출 불가 (역성장 우려).\n"
                        
                        # [2] 중기 상대가치
                        if eps_val <= 0:
                            calc_result_log += f"▶ 파이썬 연산 팩트 [중기 상대가치]: 이익 기준 산출 불가. 자산(BPS {bps_val:,}원) 하한선 참고 바람.\n"
                        else:
                            target_valuation_price = eps_val * float_ind_per if float_ind_per > 0 else eps_val * 10
                            calc_result_log += f"▶ 파이썬 연산 팩트 [중기 상대가치 적정가]: {target_valuation_price:,.0f}원 (계산식: EPS {eps_val:,}원 × 업종평균 PER {float_ind_per}배)\n"
                            
                        # [3] 장기 RIM
                        if len(roe_history) == 3:
                            expected_roe = ((roe_history[2] * 3) + (roe_history[1] * 2) + (roe_history[0] * 1)) / 6 / 100
                        elif len(roe_history) > 0:
                            expected_roe = roe_history[-1] / 100
                        else:
                            expected_roe = 0.05
                            
                        required_return = 0.08
                        if bps_val > 0:
                            residual_income = bps_val * (expected_roe - required_return)
                            rim_intrinsic_value = bps_val + (residual_income / required_return)
                            if rim_intrinsic_value < 0: rim_intrinsic_value = 0
                            calc_result_log += f"▶ 파이썬 연산 팩트 [장기 RIM 내재가치]: {rim_intrinsic_value:,.0f}원 (계산식: BPS {bps_val:,}원, 3개년 평균 ROE {expected_roe*100:.1f}%, 요구수익률 8% 대입)\n"
                        else:
                            calc_result_log += f"▶ 파이썬 연산 팩트 [장기 RIM 내재가치]: 자본 잠식으로 산출 불가.\n"

                        data_str = f"현재가: {current_price:,.0f}\n"
                        
                        if is_owned and avg_price > 0:
                            current_yield = ((current_price - avg_price) / avg_price) * 100
                            data_str += f"[내 계좌 보유 정보] 평단가: {avg_price:,.0f}원 | 수량: {quantity}주 | 현재 수익률: {current_yield:+.1f}%\n"

                        if tech: data_str += f"[차트] 52주 고/저: {tech['high_52']}/{tech['low_52']} | MACD: {tech['macd']:,.2f}\n"
                        if fund: data_str += f"[펀더멘털] PER: {fund['per']} (업종 {fund['industry_per']}) | PBR: {fund['pbr']} | EPS: {eps_val:,}원 | BPS: {bps_val:,}원 | 수급: {fund['supply_demand']}\n"
                        data_str += f"{calc_result_log}\n"
                        data_str += f"[정제된 상세 이슈 요약]\n{lite_summary}"
                        
                        prompt = (f"[{name} 진단]\n[파이썬 연산 완료 데이터]\n{data_str}\n\n"
                                  f"당신은 퀀트 애널리스트입니다. 파이썬이 연산한 기간별 팩트 지표를 바탕으로 최종 목표가를 산출하십시오.\n"
                                  f"=== ⚠️ AI 목표가 산출 핵심 지침 ===\n"
                                  f"1. 파이썬 팩트 지표에 정상적인 가격이 있다면 이를 '기본 가치 밴드'로 참고하여 차트/뉴스 가중치를 부여해 목표가를 도출하십시오.\n"
                                  f"2. 만약 특정 기간의 파이썬 연산 팩트가 '산출 불가(적자)'로 나왔다면, 해당 종목의 펀더멘털 가치는 무시하십시오. 이 경우 철저하게 차트 기술적 흐름(저항선)과 뉴스의 모멘텀 파급력에 100% 가중치를 두고 '기술적 목표가'를 제시하십시오.\n\n"
                                  f"=== 진단 리포트 작성 항목 ===\n"
                                  f"- 매수의견 (Buy/Hold/Sell 명시)\n"
                                  f"  * 데이터에 [내 계좌 보유 정보]가 있다면, 현재 수익률과 기술적/기본적 가치를 종합하여 '추가매수(Buy)', '보유 유지(Hold)', '익절/손절(Sell)' 중 하나를 객관적으로 제시할 것.\n"
                                  f"- 단기/중기/장기 목표가 산출 논리: (퀀트가 가능하면 퀀트 인용, 불가 시 기술적/모멘텀 평가임을 명시하여 상세히 서술)\n"
                                  f"- 차트 기술적 분석 및 수급 동향 해석\n"
                                  f"- 시장 파급 효과 및 주가 변화 의의 분석\n\n"
                                  f"※ 마지막 줄은 아래 파싱 형식으로 작성하십시오.\n"
                                  f"TARGET_PRICE: 단기목표가|중기목표가|장기목표가|매수추천가")
                        report = call_gemini_with_fallback(prompt)
                    
                    tp_match = re.search(r'TARGET_PRICE:\s*([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|(.*)', report)
                    n_tp_s = parse_won(tp_match.group(1)) if tp_match else 0.0
                    n_tp_m = parse_won(tp_match.group(2)) if tp_match else 0.0
                    n_tp_l = parse_won(tp_match.group(3)) if tp_match else 0.0
                    n_bp = parse_won(tp_match.group(4)) if tp_match else 0.0
                    c.execute("UPDATE portfolio SET report_text=?, tp_s=?, tp_m=?, tp_l=?, bp=?, model_used=?, report_time=? WHERE id=?", (report, n_tp_s, n_tp_m, n_tp_l, n_bp, MODEL_NAME, datetime.now().strftime("%Y-%m-%d %H:%M"), p_id))
                    conn.commit(); st.rerun()
            with col_del:
                if st.button("개별 삭제", key=f"del_t5_{p_id}", use_container_width=True):
                    c.execute("DELETE FROM portfolio WHERE id=?", (p_id,))
                    conn.commit()
                    st.rerun()

            if report_text:
                with st.expander("진단 리포트"):
                    display_report = re.sub(r'TARGET_PRICE:.*', '', report_text).strip()
                    st.write(display_report)
                    st.caption(f"🧠 전처리 요약: {LITE_MODEL_NAME} | 심층 의의 종합 분석: {MODEL_NAME}")
                    
                    if st.button("스크랩북에 저장하여 가격 추적하기", key=f"scrap_t5_{p_id}", use_container_width=True):
                        c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, scrap_date, model_used, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (f"{name} 관심종목 진단", report_text, name, ticker, current, tp_s, tp_m, tp_l, bp, datetime.now().strftime("%Y-%m-%d %H:%M"), model_used, current_user))
                        conn.commit()
                        st.success(f"'{name}' 종목의 진단 리포트가 스크랩북에 저장되었습니다.")
            st.divider()

# =======================================================
# 탭 6: 스크랩북
# =======================================================
with tab6:
    st.subheader("저장된 분석 리포트")
    
    c.execute("""
        SELECT id, title, stock_name, ticker, scrap_date, analysis, model_used, 
               saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price 
        FROM scrapbook 
        WHERE user_id = ? 
        ORDER BY id DESC
    """, (current_user,))
    scraps = c.fetchall()
    
    if scraps:
        col_bulk_scrap, _ = st.columns([2, 8])
        with col_bulk_scrap:
            if st.button("🗑️ 선택 항목 삭제", key="bulk_del_t6", use_container_width=True):
                to_del = [s[0] for s in scraps if st.session_state.get(f"chk_t6_{s[0]}", False)]
                if to_del:
                    c.execute(f"DELETE FROM scrapbook WHERE id IN ({','.join(['?']*len(to_del))})", to_del)
                    conn.commit()
                    st.rerun()
                    
        tickers = [row[3] for row in scraps if row[3]]
        price_map_scrap = fetch_current_prices(tickers)
        
        for row in scraps:
            s_id, title, s_name, ticker, s_date, analysis, m_used, saved_p, tp_s, tp_m, tp_l, bp = row
            code = re.sub(r'[^\d]', '', ticker or "")
            
            price_info = price_map_scrap.get(code, {})
            current_p = price_info.get("current", 0.0)
            
            col_sel_s, col_exp_s = st.columns([0.5, 9.5])
            with col_sel_s:
                st.checkbox("선택", key=f"chk_t6_{s_id}", label_visibility="collapsed")
            with col_exp_s:
                with st.expander(f"📌 {title} ({s_name} | {ticker}) - {s_date}"):
                    st.markdown("#### 💰 가격 지표 비교")
                    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
                    
                    m_col1.metric("매수 추천가", f"{bp:,.0f}원" if bp else "정보 없음")
                    m_col2.metric("저장 당시 주가", f"{saved_p:,.0f}원" if saved_p else "정보 없음")
                    
                    if current_p > 0:
                        diff = current_p - saved_p if saved_p else 0.0
                        diff_pct = (diff / saved_p * 100) if saved_p else 0.0
                        m_col3.metric("실시간 현재가", f"{current_p:,.0f}원", delta=f"{diff:+,.0f}원 ({diff_pct:+.2f}%)")
                    else:
                        m_col3.metric("실시간 현재가", "조회 실패")
                        
                    m_col4.markdown(f"**목표가 밴드**<br>단기: {tp_s:,.0f}원<br>중기: {tp_m:,.0f}원<br>장기: {tp_l:,.0f}원", unsafe_allow_html=True)
                    
                    if current_p > 0 and tp_s > 0:
                        st.markdown("---")
                        st.markdown("#### 🎯 목표가 대비 현재가 현황")
                        
                        pct_s = (current_p / tp_s) * 100
                        pct_m = (current_p / tp_m) * 100 if tp_m else 0.0
                        pct_l = (current_p / tp_l) * 100 if tp_l else 0.0
                        
                        p_col1, p_col2, p_col3 = st.columns(3)
                        p_col1.progress(min(int(pct_s), 100), text=f"단기 목표가 대비: **{pct_s:.1f}%**")
                        if tp_m: p_col2.progress(min(int(pct_m), 100), text=f"중기 목표가 대비: **{pct_m:.1f}%**")
                        if tp_l: p_col3.progress(min(int(pct_l), 100), text=f"장기 목표가 대비: **{pct_l:.1f}%**")
                    
                    st.markdown("---")
                    st.markdown("#### 📝 상세 분석 리포트")
                    
                    clean_analysis = analysis.split("[TRACKING_DATA]")[0].strip()
                    clean_analysis = re.sub(r'TARGET_PRICE:.*', '', clean_analysis).strip()
                    
                    st.write(clean_analysis)
                    st.caption(f"🧠 생산 모델: {m_used}")
                    
                    if st.button("개별 삭제", key=f"del_t6_{s_id}", use_container_width=True):
                        c.execute("DELETE FROM scrapbook WHERE id=?", (s_id,))
                        conn.commit()
                        st.rerun()
    else:
        st.info("저장된 분석 리포트가 없습니다.")
