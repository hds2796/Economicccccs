import streamlit as st
import requests
import re
import sqlite3
import json
import os
import io
import time
import urllib.parse
import yfinance as yf
import concurrent.futures
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# 로컬 및 클라우드 환경 테스트 시 HTTPS 오류 우회
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 구글 로그인(OAuth 2.0) 및 드라이브 라이브러리 ---
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from bs4 import BeautifulSoup
from google import genai

# 구글 드라이브 파일 접근 권한 범위
SCOPES = ['https://www.googleapis.com/auth/drive']

# --- [페이지 설정] ---
st.set_page_config(page_title="Project2_Stock", page_icon="📊", layout="wide")

# --- [API 키 설정] ---
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
NAVER_CLIENT_ID = st.secrets.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = st.secrets.get("NAVER_CLIENT_SECRET", "")
DART_API_KEY = st.secrets.get("DART_API_KEY", "")  # 💡 DART API 키 추가

# --- [데이터베이스 설정 및 스키마 업데이트] ---
conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS scrapbook 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, link TEXT, summary TEXT, analysis TEXT, scrap_date TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS portfolio 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, stock_name TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_store (state TEXT, verifier TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_creds (creds TEXT)''')
# 💡 2번 기능: 시장 심리 지수(SCORE) 히스토리 트래킹 테이블 생성
c.execute('''CREATE TABLE IF NOT EXISTS market_score_history 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, check_date TEXT, score INTEGER)''')
conn.commit()

for table, col, dtype in [
    ("portfolio", "search_query", "TEXT"), ("portfolio", "ticker", "TEXT"),
    ("portfolio", "is_owned", "INTEGER DEFAULT 0"), ("portfolio", "avg_price", "REAL DEFAULT 0.0"),
    ("portfolio", "quantity", "INTEGER DEFAULT 0"), ("scrapbook", "stock_name", "TEXT"),
    ("scrapbook", "ticker", "TEXT"), ("scrapbook", "saved_price", "REAL DEFAULT 0.0"),
    ("scrapbook", "target_price", "REAL DEFAULT 0.0")
]:
    try: c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
    except: pass
conn.commit()

# =======================================================
# 1. 보안: 로그인 시스템
# =======================================================
def check_password():
    if "pwd" in st.query_params:
        if st.query_params["pwd"] == st.secrets["APP_PASSWORD"]:
            st.session_state["password_correct"] = True

    if st.session_state.get("password_correct", False):
        return True

    st.title("🔒 Project2_Stock 로그인")
    st.warning("⚠️ **경고: 처음에 설정한 비밀번호를 잃어버리면 절대 찾을 수 없습니다.**")
    
    password = st.text_input("비밀번호를 입력하세요", type="password")
    
    if st.button("접속하기"):
        if password == st.secrets["APP_PASSWORD"]:
            st.session_state["password_correct"] = True
            st.rerun()
        else:
            st.error("❌ 비밀번호가 일치하지 않습니다.")
    return False

if not check_password():
    st.stop()

# =======================================================
# 2. 구글 드라이브 OAuth 인증
# =======================================================
def handle_oauth_callback():
    if 'code' in st.query_params and 'state' in st.query_params:
        state = st.query_params['state']
        code = st.query_params['code']
        
        c.execute("SELECT verifier FROM oauth_store WHERE state=?", (state,))
        row = c.fetchone()
        
        if not row:
            st.query_params.clear()
            st.warning("로그인 세션이 만료되었습니다. 데이터 백업 탭에서 버튼을 다시 클릭하여 주십시오.")
            return

        verifier = row[0]
        
        try:
            client_config = json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"])
            flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=st.secrets["REDIRECT_URI"])
            flow.code_verifier = verifier
            flow.fetch_token(code=code)
            creds = flow.credentials
            
            cred_dict = {'token': creds.token, 'refresh_token': creds.refresh_token, 'token_uri': creds.token_uri, 'client_id': creds.client_id, 'client_secret': creds.client_secret, 'scopes': creds.scopes}
            c.execute("DELETE FROM oauth_creds")
            c.execute("INSERT INTO oauth_creds VALUES (?)", (json.dumps(cred_dict),))
            c.execute("DELETE FROM oauth_store") 
            conn.commit()
            st.query_params.clear()
            st.rerun()
        except Exception as e: st.error(f"구글 로그인 인증 오류가 발생했습니다: {e}")

handle_oauth_callback()

def init_drive_service():
    c.execute("SELECT creds FROM oauth_creds")
    row = c.fetchone()
    if row:
        try:
            cred_dict = json.loads(row[0])
            creds = Credentials.from_authorized_user_info(cred_dict, SCOPES)
            return build('drive', 'v3', credentials=creds)
        except: pass
    return None

def upload_to_google_drive(json_string):
    service = init_drive_service()
    if not service: raise Exception("먼저 구글 드라이브로 로그인해야 합니다.")
    file_name = f"market_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    file_metadata = {'name': file_name, 'parents': [st.secrets["GOOGLE_FOLDER_ID"]]}
    json_bytes = json_string.encode('utf-8')
    media = MediaIoBaseUpload(io.BytesIO(json_bytes), mimetype='application/json', resumable=True)
    return service.files().create(body=file_metadata, media_body=media, fields='id').execute().get('id')

def download_latest_from_google_drive():
    service = init_drive_service()
    if not service: raise Exception("먼저 구글 드라이브로 로그인해야 합니다.")
    folder_id = st.secrets["GOOGLE_FOLDER_ID"]
    query = f"'{folder_id}' in parents and mimeType = 'application/json' and trashed = false"
    results = service.files().list(q=query, orderBy="modifiedTime desc", pageSize=1, fields="files(id, name)").execute()
    files = results.get('files', [])
    if not files: raise Exception("구글 드라이브 폴더에 백업된 JSON 파일이 없습니다.")
    file_id = files[0]['id']
    content = service.files().get_media(fileId=file_id).execute()
    return content, files[0]['name']

# =======================================================
# 3. 데이터 상태 관리 및 핵심 로직
# =======================================================
if 'analysis_results' not in st.session_state: st.session_state.analysis_results = {}
if 'overall_analysis' not in st.session_state: st.session_state.overall_analysis = None
if 'realtime_analysis' not in st.session_state: st.session_state.realtime_analysis = None
if 'today_recommendation' not in st.session_state: st.session_state.today_recommendation = None

if 'realtime_start' not in st.session_state: st.session_state.realtime_start = 1
if 'seen_realtime' not in st.session_state: st.session_state.seen_realtime = set()
if 'current_realtime_news' not in st.session_state: st.session_state.current_realtime_news = []

if 'eco_start' not in st.session_state: st.session_state.eco_start = 1
if 'seen_eco' not in st.session_state: st.session_state.seen_eco = set()
if 'current_eco_news' not in st.session_state: st.session_state.current_eco_news = []

if 'sector_starts' not in st.session_state: st.session_state.sector_starts = {}
if 'seen_sectors' not in st.session_state: st.session_state.seen_sectors = {}
if 'current_sector_news' not in st.session_state: st.session_state.current_sector_news = {}
if 'port_starts' not in st.session_state: st.session_state.port_starts = {}

@st.cache_data(ttl=60)
def get_market_data():
    results = {}
    def fetch_naver_realtime(code):
        try:
            url = f"https://polling.finance.naver.com/api/realtime/domestic/index/{code}"
            data = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3).json()['datas'][0]
            current = float(data['closePrice'].replace(',', ''))
            diff = float(data['compareToPreviousClosePrice'].replace(',', ''))
            diff_pct = float(data['fluctuationsRatio'].replace(',', ''))
            f_code = str(data.get('compareToPreviousPrice', {}).get('code', '3'))
            if f_code in ['4', '5']: diff, diff_pct = -abs(diff), -abs(diff_pct)
            else: diff, diff_pct = abs(diff), abs(diff_pct)
            return {"current": current, "diff": diff, "diff_pct": diff_pct}
        except Exception: return {"current": 0, "diff": 0, "diff_pct": 0.0}

    results["코스피 (실시간)"] = fetch_naver_realtime("KOSPI")
    results["코스닥 (실시간)"] = fetch_naver_realtime("KOSDAQ")

    def fetch_yahoo_direct(ticker):
        try:
            encoded_ticker = urllib.parse.quote(ticker)
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded_ticker}?range=5d&interval=1d"
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
            res = requests.get(url, headers=headers, timeout=5)
            data = res.json()
            closes = data['chart']['result'][0]['indicators']['quote'][0]['close']
            closes = [c for c in closes if c is not None]
            if len(closes) >= 2:
                prev_close = float(closes[-2])
                current = float(closes[-1])
                diff = current - prev_close
                diff_pct = (diff / prev_close) * 100 if prev_close > 0 else 0.0
                return {"current": current, "diff": diff, "diff_pct": diff_pct}
        except: pass
        return {"current": 0, "diff": 0, "diff_pct": 0.0}

    results["S&P 500 (실시간)"] = fetch_yahoo_direct("^GSPC")
    results["원/달러 환율"] = fetch_yahoo_direct("KRW=X")
    return results

@st.cache_data(ttl=60)
def get_stock_current_price(ticker):
    if not ticker: return 0.0
    try:
        code_match = re.search(r'\d{6}', ticker)
        if code_match:
            code = code_match.group()
            url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('datas'): return float(data['datas'][0]['closePrice'].replace(',', ''))
        encoded_ticker = urllib.parse.quote(ticker)
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded_ticker}?range=2d&interval=1d"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        data = res.json()
        closes = data['chart']['result'][0]['indicators']['quote'][0]['close']
        closes = [c for c in closes if c is not None]
        if closes: return float(closes[-1])
    except: pass
    return 0.0

# 💡 1번 기능: 기술적 지표 수치 계산기 (AI 프롬프트 주입용)
def calculate_technical_indicators(ticker):
    try:
        code_match = re.search(r'\d{6}', ticker)
        formatted_ticker = f"{code_match.group()}.KS" if code_match else ticker
        df = yf.Ticker(formatted_ticker).history(period="60d")
        if len(df) >= 20:
            current_close = float(df['Close'].iloc[-1])
            ma20 = float(df['Close'].rolling(window=20).mean().iloc[-1])
            ma60 = float(df['Close'].rolling(window=60).mean().iloc[-1])
            
            delta = df['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean().iloc[-1]
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean().iloc[-1]
            rs = gain / loss if loss > 0 else 0
            rsi = 100 - (100 / (1 + rs)) if loss > 0 else 100
            
            return f"- 20일 이동평균선: {ma20:,.0f}원 (현재가 대비 {((current_close-ma20)/ma20)*100:+.1f}%)\n- 60일 이동평균선: {ma60:,.0f}원\n- RSI (14일 과열지표): {rsi:.1f} ({'과열🔴' if rsi >= 70 else '침체🔵' if rsi <= 30 else '중립⚖️'})"
    except: pass
    return "- 기술적 지표 계산 불가 (데이터 누락)"

# 💡 3번 기능: Open DART API 실시간 공시 수집기 (무료 팩트체크용)
def fetch_dart_disclosures(ticker):
    if not DART_API_KEY: return "- [알림] Open DART API 키가 설정되지 않아 공시를 불러올 수 없습니다."
    try:
        code_match = re.search(r'\d{6}', ticker)
        if not code_match: return "- 국내 종목 코드가 아닙니다."
        code = code_match.group()
        
        # 최근 30일 이내의 주요 공시 탐색
        b_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        url = f"https://opendart.fss.or.kr/api/list.json?crtfc_key={DART_API_KEY}&corp_code={code}&bgn_de={b_date}&pblntf_ty=A&pblntf_ty=B&pblntf_ty=C"
        res = requests.get(url, timeout=5).json()
        
        if res.get('status') == '000' and res.get('list'):
            lines = []
            for item in res['list'][:5]: # 최신 5개만 노출
                lines.append(f"• [{item['rcept_no']}] ({item['pblntf_dt'][:4]}-{item['pblntf_dt'][4:6]}-{item['pblntf_dt'][6:]}) {item['report_nm']} [접수처: {item['flr_nm']}]")
            return "\n".join(lines)
        return "- 최근 30일 이내에 등록된 주요 자본/경영 공시가 없습니다."
    except: return "- DART 서버 통신 오류"

# 💡 4번 기능: 당일 기관/외국인 가상 수급동향 (네이버 실시간 수급 파싱 우회)
def fetch_supply_demand_trend(ticker):
    try:
        code_match = re.search(r'\d{6}', ticker)
        if code_match:
            code = code_match.group()
            url = f"https://finance.naver.com/item/frgn.naver?code={code}"
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
            soup = BeautifulSoup(res.text, 'html.parser')
            table = soup.find('table', summary='외국인 기관 순매매량에 관한 표이며 날짜, 종가, 전일비, 등락률, 거래량, 기관순매매량, 외국인순매매량, 보유주수, 보유율 정보를 제공합니다.')
            if table:
                rows = table.find_all('tr', onmouseover="mouseOver(this)")
                if rows:
                    cols = rows[0].find_all('td')
                    if len(cols) >= 7:
                        inst = cols[5].text.strip().replace(',', '')
                        frgn = cols[6].text.strip().replace(',', '')
                        def fmt(val):
                            v = int(val)
                            return f"{v:+,}주" if v != 0 else "0주"
                        return f"- 기관 당일 매매동향: {fmt(inst)}\n- 외국인 당일 매매동향: {fmt(frgn)}"
    except: pass
    return "- 수급 데이터 조회 불가"

@st.cache_data(ttl=300)
def get_naver_news(query, display=100, start=1, sort_type="date"):
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET: return []
    url = "https://naverapihub.apigw.ntruss.com/search/v1/news"
    headers = {"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET}
    queries = [q.strip() for q in query.split('|') if q.strip()]
    all_items = []
    now = datetime.now(timezone.utc)
    per_query_display = max(10, display // len(queries)) if queries else display
    
    for q in queries:
        params = {"query": q, "display": per_query_display, "start": start, "sort": sort_type, "format": "json"}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=3)
            if response.status_code == 200:
                for i in response.json().get("items", []):
                    pub_date_str = i['pubDate']
                    try:
                        dt = parsedate_to_datetime(pub_date_str)
                        kst = timezone(timedelta(hours=9))
                        pub_date_formatted = dt.astimezone(kst).strftime("%Y-%m-%d %H:%M")
                        raw_date = dt
                    except:
                        pub_date_formatted = pub_date_str
                        raw_date = now
                    all_items.append({"title": clean_html(i['title']), "link": i['link'], "summary": clean_html(i['description']), "published": pub_date_formatted, "raw_date": raw_date})
        except: pass
    unique_items = []
    seen = set()
    for item in sorted(all_items, key=lambda x: x['raw_date'], reverse=True):
        if item['link'] not in seen:
            seen.add(item['link'])
            unique_items.append(item)
    return unique_items[:display]

def is_within_7_days(pub_date_str):
    try:
        dt = parsedate_to_datetime(pub_date_str)
        return (datetime.now(timezone.utc) - dt) <= timedelta(days=7)
    except: return True

def fetch_unique_realtime_news(query):
    unique_news = []
    attempts = 0
    while len(unique_news) < 20 and st.session_state.realtime_start <= 900 and attempts < 4:
        batch = get_naver_news(query, display=10, start=st.session_state.realtime_start, sort_type="date")
        st.session_state.realtime_start += 10
        attempts += 1
        if not batch: break
        for n in batch:
            if n['link'] not in st.session_state.seen_realtime:
                unique_news.append(n); st.session_state.seen_realtime.add(n['link'])
            if len(unique_news) == 20: break
            
    if len(unique_news) <= 3:
        st.session_state.realtime_start = 1
        st.session_state.seen_realtime = set()
        try:
            prompt = f"'{query}' 검색어로 최신 뉴스가 부족합니다. 현재 뉴스에 자주 등장하는 경제/시사 관련 핫키워드 5개를 '|' 기호로 연결해 출력하세요. (예: 금리|환율|물가)"
            expanded_query_raw = call_gemini_with_fallback(prompt, use_lite=True)
            expanded_query = re.sub(r'[^가-힣a-zA-Z0-9|]', '', expanded_query_raw).strip()
            if not expanded_query or len(expanded_query) < 2: expanded_query = "금리|환율|물가|수출|부동산"
            
            batch = get_naver_news(expanded_query, display=20, start=1, sort_type="date")
            st.session_state.realtime_start = 21
            for n in (batch or []):
                if n['link'] not in st.session_state.seen_realtime:
                    unique_news.append(n); st.session_state.seen_realtime.add(n['link'])
                if len(unique_news) >= 10: break
        except: pass
    st.session_state.current_realtime_news = unique_news

def fetch_unique_eco_news(query):
    unique_news = []
    attempts = 0
    while len(unique_news) < 10 and st.session_state.eco_start <= 900 and attempts < 3:
        batch = get_naver_news(query, display=10, start=st.session_state.eco_start, sort_type="sim")
        st.session_state.eco_start += 10
        attempts += 1
        if not batch: break
        for n in batch:
            if n['link'] not in st.session_state.seen_eco:
                unique_news.append(n); st.session_state.seen_eco.add(n['link'])
            if len(unique_news) == 10: break
            
    if not unique_news:
        st.session_state.eco_start = 1
        st.session_state.seen_eco = set()
        batch = get_naver_news(query, display=10, start=1, sort_type="sim")
        st.session_state.eco_start = 11
        for n in (batch or []):
            unique_news.append(n); st.session_state.seen_eco.add(n['link'])
            if len(unique_news) == 10: break
    st.session_state.current_eco_news = unique_news

def fetch_unique_sector_news(sector_name, query):
    if sector_name not in st.session_state.sector_starts:
        st.session_state.sector_starts[sector_name] = 1
        st.session_state.seen_sectors[sector_name] = set()
    business_kws = ["주가", "실적", "목표가", "수주", "배당", "합병", "투자", "인수", "매출", "영업이익", "전망", "동향", "계약", "신제품", "개발", "수출", "공급", "M&A", "규제"]
    unique_news = []
    attempts = 0
    while len(unique_news) < 10 and st.session_state.sector_starts[sector_name] <= 900 and attempts < 3:
        batch = get_naver_news(query, display=30, start=st.session_state.sector_starts[sector_name], sort_type="sim")
        st.session_state.sector_starts[sector_name] += 30
        attempts += 1
        if not batch: break
        for n in batch:
            if n['link'] not in st.session_state.seen_sectors[sector_name]:
                if any(b_kw in n['title'] or b_kw in n['summary'] for b_kw in business_kws):
                    unique_news.append(n); st.session_state.seen_sectors[sector_name].add(n['link'])
            if len(unique_news) == 10: break
            
    if not unique_news:
        st.session_state.sector_starts[sector_name] = 1
        st.session_state.seen_sectors[sector_name] = set()
        batch = get_naver_news(query, display=30, start=1, sort_type="sim")
        st.session_state.sector_starts[sector_name] = 31
        for n in (batch or []):
            if any(b_kw in n['title'] or b_kw in n['summary'] for b_kw in business_kws):
                unique_news.append(n); st.session_state.seen_sectors[sector_name].add(n['link'])
            if len(unique_news) == 10: break
    st.session_state.current_sector_news[sector_name] = unique_news

# =======================================================
# AI 호출 로직
# =======================================================
def call_gemini_with_fallback(prompt, is_json=False, use_lite=False):
    if not GEMINI_API_KEY: raise Exception("Gemini API 키 오류")
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    if use_lite: models_to_try = [('gemini-3.1-flash-lite', '')]
    else: models_to_try = [('gemini-3.5-flash', '\n\n*(💡 3.5 모델 적용)*'), ('gemini-2.5-flash', '\n\n*(💡 2.5 우회 적용)*'), ('gemini-1.5-flash', '\n\n*(💡 1.5 우회 적용)*'), ('gemini-3.1-flash-lite', '\n\n*(💡 Lite 우회 적용)*')]
    
    quota_keywords = ["quota exceeded", "quota", "billing"]
    fallback_keywords = ["429", "resource_exhausted", "not found", "404", "503", "high demand", "overloaded", "unavailable"]
    last_exception = None
    
    for model_name, fallback_msg in models_to_try:
        for attempt in range(2): 
            try:
                res = client.models.generate_content(model=model_name, contents=prompt).text
                if not is_json and fallback_msg: res += fallback_msg
                return res
            except Exception as e:
                error_str = str(e).lower()
                last_exception = e
                if any(q in error_str for q in quota_keywords): raise Exception(f"일일 API 한도 초과: {e}")
                if "not found" in error_str or "404" in error_str: break 
                if any(k in error_str for k in fallback_keywords): time.sleep(1.0); continue
                break 
    raise Exception(f"API 호출 실패 지속: {last_exception}")

def call_gemini_stream_with_fallback(prompt):
    if not GEMINI_API_KEY: yield "Gemini API 키 오류"; return
    client = genai.Client(api_key=GEMINI_API_KEY)
    models_to_try = [('gemini-3.5-flash', '\n\n*(💡 3.5 모델 적용)*'), ('gemini-2.5-flash', '\n\n*(💡 2.5 우회 적용)*'), ('gemini-1.5-flash', '\n\n*(💡 1.5 우회 적용)*'), ('gemini-3.1-flash-lite', '\n\n*(💡 Lite 우회 적용)*')]
    
    for model_name, fallback_msg in models_to_try:
        try:
            response = client.models.generate_content_stream(model=model_name, contents=prompt)
            for chunk in response:
                if chunk.text: yield chunk.text
            yield fallback_msg
            return
        except Exception as e:
            if "quota" in str(e).lower(): yield f"\n\n🚨 API 한도 초과입니다."; return
            continue
    yield "\n\n서버 과부하로 분석을 완료할 수 없습니다."

@st.cache_data(ttl=86400)
def get_dynamic_business_keywords():
    try:
        prompt = "현재 한국 주식 시장에서 특급 호재/악재를 나타내는 핵심 비즈니스 키워드 15개를 '|'로 연결해 출력하세요. (예: HBM|전고체|밸류업)"
        res = call_gemini_with_fallback(prompt, is_json=True, use_lite=True)
        clean_res = re.sub(r'[^가-힣a-zA-Z0-9|]', '', res).strip()
        if len(clean_res.split('|')) > 3: return clean_res.split('|')
    except: pass
    return ["HBM", "AI", "밸류업", "전고체", "비만치료제", "자율주행", "초전도체", "경영권분쟁", "독점공급", "FDA"]

# =======================================================
# 재무 데이터 및 프롬프트 빌더
# =======================================================
def build_prompt_single_news(title, summary, market_data_str):
    return f"아래 뉴스가 증시에 미칠 영향을 분석하세요.\n[지표]: {market_data_str}\n[제목]: {title}\n[요약]: {summary}\n1. 💡 핵심 요약\n2. 📈 시장 파급력\n3. 🎯 연관 섹터"

def build_prompt_realtime(news_list, market_data_str):
    combined = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    return f"최신 실시간 뉴스 {len(news_list)}건 종합 브리핑:\n[지표]: {market_data_str}\n{combined}\n\n1. 🔔 핵심 이슈 요약\n2. 📉 경제/증시 파급력\n3. 🎯 리스크 및 섹터"

def build_prompt_overall(news_list, market_data_str):
    combined = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    return f"주요 경제 뉴스 {len(news_list)}건 시장 브리핑:\n[지표]: {market_data_str}\n{combined}\n\n1. 🌐 거시 환경 요약\n2. ⚖️ 호악재 분석\n3. 💡 주목 섹터\n4. 🔮 향후 전망\n\n마지막줄에 'SCORE: 숫자' (0~100) 기재."

def build_prompt_sector(sector_name, news_list, market_data_str):
    combined = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    return f"'{sector_name}' 섹터 분석:\n[지표]: {market_data_str}\n{combined}\n\n1. 🏭 섹터 흐름 요약\n2. 📈 주요 호/악재\n3. 🎯 투자 심리 전망"

def build_prompt_recommend_step3(candidate_context, news_list, market_data_str, investment_horizon):
    combined = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list[:30]])
    return (f"당신은 엄격한 퀀트 애널리스트입니다. 앞서 발굴된 예비 후보 5개의 '실시간 주가'와 '재무 데이터'를 확인했습니다.\n\n"
            f"[현재 실시간 시장 지표]: {market_data_str}\n\n"
            f"[예비 후보 5종목 팩트체크 데이터]\n{candidate_context}\n"
            f"[관련 뉴스]\n{combined}\n\n"
            f"위 데이터를 분석하여, 주가가 이미 너무 고평가되었거나(PER 과도 등) 상승 여력이 없는 2개를 쳐내고, "
            f"'{investment_horizon}' 투자에 가장 적합한 최종 3개만 엄선하여 리포트를 작성하십시오.\n\n"
            f"[양식]\n"
            f"1. 🥇 추천종목 1: [종목명]\n"
            f"- 선정 근거: (뉴스와 재무데이터 기반으로 고평가가 아님을 증명)\n"
            f"- 투자 전략: (진입 시점 및 비중 등)\n"
            f"- 💰 적정 목표가: [구체적 가격] / 손절가: [구체적 가격]\n\n"
            f"2. 🥈 추천종목 2: [종목명]\n"
            f"- 선정 근거: ...\n"
            f"- 투자 전략: ...\n"
            f"- 💰 적정 목표가: ... / 손절가: ...\n\n"
            f"3. 🥉 추천종목 3: [종목명]\n"
            f"- 선정 근거: ...\n"
            f"- 투자 전략: ...\n"
            f"- 💰 적정 목표가: ... / 손절가: ...\n\n"
            f"※ 중요1: 본문에 '실시간 현재가'는 절대 기재하지 마십시오. (시스템이 UI로 띄웁니다)\n"
            f"※ 중요2: 목표가는 반드시 제시된 '실시간 현재가'보다 현실적으로 높은 가격이어야 합니다.\n"
            f"※ 중요3: 리포트 맨 마지막 줄에 시스템 추적을 위해 추천종목 3개의 데이터를 아래와 같이 기재하십시오. (다른 설명 없이 형식만 유지)\n"
            f"[TRACKING_DATA]\n"
            f"종목명1|티커1|목표가1(숫자만)\n"
            f"종목명2|티커2|목표가2(숫자만)\n"
            f"종목명3|티커3|목표가3(숫자만)")

# 💡 1번(기술적 지표), 4번(수급 데이터) 수치 정보를 심층 분석 프롬프트에 자동 병합 주입
def build_prompt_deep_dive(stock_name, ticker, news_list, is_owned, avg_price, quantity, current_price, market_data_str, tech_str, supply_str):
    fin_data = get_financial_data(ticker)
    status = "미보유 관심종목"
    if is_owned == 1:
        roi = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0
        status = f"보유 중 (평단: {avg_price:,.0f}원, 수량: {quantity}주, 현재가: {current_price:,.0f}원, 수익률: {roi:.2f}%)"
    combined = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list[:30]])
    return f"[{stock_name} 심층 진단]\n[지표]\n{market_data_str}\n[내 상태]\n{status}\n[실시간 수급 동향]\n{supply_str}\n[보조지표/기술적 수치]\n{tech_str}\n[뉴스]\n{combined}\n[재무]\n{fin_data}\n\n1. 🏢 재무 및 기업 펀더멘털 분석\n2. 🌐 뉴스 및 수급 파급력 종합 분석 (외인/기관 동향 및 RSI 과열구간 언급 필수)\n3. 📊 포트폴리오 맞춤 진단\n4. 🎯 투자의견\n5. 💰 적정 목표가\n6. 👥 동종업계 비교\n\n마지막줄에 'TARGET_PRICE: 숫자' 필수."

# =======================================================
# 4. 메인 대시보드 UI
# =======================================================
st.title("📊 Project2_Stock")
market_data = get_market_data()
market_data_str = ", ".join([f"{k}: {v['current']:,.2f}({v['diff_pct']:+.2f}%)" for k, v in market_data.items() if v.get('current', 0) > 0])

cols = st.columns(len(market_data))
for i, (name, data) in enumerate(market_data.items()):
    with cols[i]:
        if data.get('current', 0) > 0:
            st.metric(label=name, value=f"{data['current']:,.2f}", delta=f"{data['diff']:,.2f} ({data['diff_pct']:.2f}%)")
        else: st.metric(label=name, value="데이터 오류")
st.divider()

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(["📰 실시간 경제·시사", "🔥 핵심 경제 뉴스", "📑 섹터별 분석", "🎯 오늘의 추천종목", "⭐️ 내 관심종목", "📁 스크랩북", "⚙️ 데이터 관리"])

# ----------------- [탭 1: 실시간 뉴스] -----------------
with tab1:
    st.subheader("📰 실시간 경제·시사 뉴스 분석")
    st.write("방금 송고된 최신 기사를 실시간(최신순)으로 수집하고 트렌드를 분석합니다.")
    
    realtime_query = "증시|금융|환율|물가|부동산|정책|수출"
    if not st.session_state.current_realtime_news: fetch_unique_realtime_news(realtime_query)
        
    col_r1, col_r2 = st.columns([4, 1])
    with col_r2:
        if st.button("🔄 실시간 뉴스 갱신", key="ref_rt", use_container_width=True):
            st.session_state.realtime_start = 1
            st.session_state.seen_realtime = set()
            get_naver_news.clear()
            fetch_unique_realtime_news(realtime_query)
            st.session_state.realtime_analysis = None
            st.rerun()

    if st.session_state.current_realtime_news:
        if st.button("🤖 실시간 뉴스 TOP 20 기반 종합 분석", type="primary", use_container_width=True):
            my_bar = st.progress(30, text="실시간 최신 뉴스 수집 중...")
            prompt = build_prompt_realtime(st.session_state.current_realtime_news[:20], market_data_str)
            my_bar.progress(80, text="AI 실시간 분석 및 리포트 작성 중...")
            st.markdown("### 🤖 실시간 AI 브리핑 작성 중...")
            full_response = st.write_stream(call_gemini_stream_with_fallback(prompt))
            my_bar.empty()
            st.session_state.realtime_analysis = full_response
            st.rerun()
                
        if st.session_state.realtime_analysis:
            with st.expander("📊 AI 실시간 시황 종합 브리핑", expanded=True):
                st.write(st.session_state.realtime_analysis)
                if st.button("💾 이 리포트 스크랩", key="scrap_rt_all"):
                    c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date) VALUES (?, ?, ?, ?)", ("📰 실시간 시황 종합 브리핑", "최신 경제/시사 송고 기사 기반", st.session_state.realtime_analysis, datetime.now().strftime("%Y-%m-%d %H:%M")))
                    conn.commit(); st.success("스크랩북 저장 완료")
        st.markdown("---")
        
        for i, news in enumerate([n for n in st.session_state.current_realtime_news if is_within_7_days(n['published'])]):
            with st.expander(f"🕒 {news['title']}"):
                st.markdown(f"[원문 읽기]({news['link']})")
                st.caption(f"{news['published']} | {news['summary']}")
                if st.button("이 기사 심층 분석", key=f"tr_btn_{news['link']}"):
                    with st.spinner("기사 내용 분석 중..."):
                        prompt = build_prompt_single_news(news['title'], news['summary'], market_data_str)
                        st.session_state.analysis_results[f"news_{news['link']}"] = {"text": call_gemini_with_fallback(prompt), "time": time.time()}
                
                cached_data = st.session_state.analysis_results.get(f"news_{news['link']}")
                if cached_data:
                    with st.expander("🤖 AI 뉴스 분석 결과", expanded=True):
                        st.write(cached_data['text'])
                        if st.button("💾 스크랩", key=f"tr_scrap_{news['link']}"):
                            c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)", (news['title'], news['link'], news['summary'], cached_data['text'], datetime.now().strftime("%Y-%m-%d %H:%M")))
                            conn.commit(); st.success("저장 완료")

# ----------------- [탭 2: 핵심 경제 뉴스] -----------------
with tab2:
    st.subheader("오늘의 핵심 경제 뉴스")
    st.write("주식 시장과 연관성이 높은 핵심 경제 기사를 정확도순으로 수집합니다.")
    
    # 💡 2번 기능: 시장 심리 히스토리 트래킹용 차트 상단 배치
    c.execute("SELECT check_date, score FROM market_score_history ORDER BY id DESC LIMIT 15")
    hist_data = c.fetchall()
    if hist_data:
        with st.expander("📈 AI 시장 심리 지수 추이 그래프 (최근 15회)", expanded=False):
            dates = [r[0][5:] for r in reversed(hist_data)]
            scores = [r[1] for r in reversed(hist_data)]
            st.line_chart(dict(zip(dates, scores)))
            
    eco_query = "경제|증시|주식|코스피|코스닥|금리|실적"
    if not st.session_state.current_eco_news: fetch_unique_eco_news(eco_query)
        
    col_m1, col_m2 = st.columns([4, 1])
    with col_m2:
        if st.button("🔄 새로운 뉴스 보기", key="ref_eco", use_container_width=True):
            fetch_unique_eco_news(eco_query)
            st.session_state.overall_analysis = None
            st.rerun()

    if st.session_state.current_eco_news:
        if st.button("🤖 TOP 50 뉴스 기반 시장 브리핑 생성", type="primary"):
            my_bar = st.progress(30, text="핵심 뉴스 50건 스크래핑 중...")
            top_50_news = get_naver_news(eco_query, display=50, sort_type="sim")
            prompt = build_prompt_overall(top_50_news, market_data_str)
            my_bar.progress(80, text="AI 실시간 리포트 작성 중...")
            full_response = st.write_stream(call_gemini_stream_with_fallback(prompt))
            my_bar.empty()
            
            match = re.search(r'SCORE:\s*(\d+)', full_response)
            score = int(match.group(1)) if match else 50
            
            # 💡 2번 기능: 점수가 생성되면 영구 보관용 DB에 자동 저장
            c.execute("INSERT INTO market_score_history (check_date, score) VALUES (?, ?)", (datetime.now().strftime("%Y-%m-%d %H:%M"), score))
            conn.commit()
            
            st.session_state.overall_analysis = {"text": re.sub(r'SCORE:\s*\d+', '', full_response).strip(), "score": score}
            st.rerun()
                 
        if st.session_state.overall_analysis:
            score = st.session_state.overall_analysis['score']
            sentiment_label = "매우 강세 🔥" if score >= 80 else "강세 📈" if score >= 60 else "중립 ⚖️" if score >= 40 else "약세 📉" if score >= 20 else "매우 약세 ❄️"
            st.markdown(f"**현재 AI 시장 심리 지수: {score} / 100 ({sentiment_label})**")
            st.progress(score / 100.0)
            
            with st.expander("📝 AI 거시 환경 브리핑 전체 보기", expanded=True):
                st.write(st.session_state.overall_analysis['text'])
        st.markdown("---")
        
        for i, news in enumerate([n for n in st.session_state.current_eco_news if is_within_7_days(n['published'])]):
            with st.expander(f"📰 {news['title']}"):
                st.markdown(f"[원문 읽기]({news['link']})")
                st.caption(f"{news['published']} | {news['summary']}")
                if st.button("이 기사 심층 분석", key=f"t1_btn_{news['link']}"):
                    with st.spinner("분석 중..."):
                        st.session_state.analysis_results[f"eco_{news['link']}"] = {"text": call_gemini_with_fallback(build_prompt_single_news(news['title'], news['summary'], market_data_str)), "time": time.time()}
                cached_data = st.session_state.analysis_results.get(f"eco_{news['link']}")
                if cached_data:
                    with st.expander("🤖 AI 분석 결과", expanded=True):
                        st.write(cached_data['text'])
                        if st.button("💾 스크랩", key=f"t1_sc_{news['link']}"):
                            c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)", (news['title'], news['link'], news['summary'], cached_data['text'], datetime.now().strftime("%Y-%m-%d %H:%M")))
                            conn.commit(); st.success("저장 완료")

# ----------------- [탭 3: 섹터별 분석] -----------------
with tab3:
    sectors = {"반도체": "반도체|삼성전자|SK하이닉스", "2차전지": "2차전지|전기차|배터리", "바이오": "바이오|제약|신약", "금융/밸류업": "금융|은행|밸류업|증권", "IT/플랫폼": "IT|플랫폼|네이버|카카오", "방산/조선": "방산|조선|K방산"}
    col_s1, col_s2 = st.columns([4, 1])
    with col_s1: selected_sector = st.selectbox("관심 섹터 선택", list(sectors.keys()))
    if selected_sector not in st.session_state.current_sector_news: fetch_unique_sector_news(selected_sector, sectors[selected_sector])
        
    with col_s2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🔄 다른 기사 보기", key="ref_sec", use_container_width=True):
            fetch_unique_sector_news(selected_sector, sectors[selected_sector])
            st.session_state.pop(f'sec_sum_{selected_sector}', None)
            st.rerun()
            
    if sector_news := st.session_state.current_sector_news.get(selected_sector, []):
        if st.button(f"🤖 '{selected_sector}' 섹터 종합 분석", type="primary"):
            my_bar = st.progress(30, text=f"'{selected_sector}' 스크래핑 중...")
            top_20_news = get_naver_news(sectors[selected_sector], display=20, sort_type="sim")
            prompt = build_prompt_sector(selected_sector, top_20_news, market_data_str)
            my_bar.progress(80, text="AI 실시간 분석 중...")
            full_response = st.write_stream(call_gemini_stream_with_fallback(prompt))
            my_bar.empty()
            st.session_state[f'sec_sum_{selected_sector}'] = full_response
            st.rerun()
            
        if f'sec_sum_{selected_sector}' in st.session_state:
            with st.expander("📊 AI 섹터 브리핑", expanded=True): st.write(st.session_state[f'sec_sum_{selected_sector}'])
            st.markdown("---")
            
        for i, news in enumerate([n for n in sector_news if is_within_7_days(n['published'])]):
            with st.expander(f"📰 {news['title']}"):
                st.markdown(f"[원문 읽기]({news['link']})\n\n{news['summary']}")
                if st.button("AI 분석 실행", key=f"t2_btn_{news['link']}"):
                    with st.spinner("분석 중..."):
                        st.session_state.analysis_results[f"sec_{news['link']}"] = {"text": call_gemini_with_fallback(build_prompt_single_news(news['title'], news['summary'], market_data_str)), "time": time.time()}
                cached_data = st.session_state.analysis_results.get(f"sec_{news['link']}")
                if cached_data:
                    with st.expander("🤖 결과", expanded=True):
                        st.write(cached_data['text'])
                        if st.button("💾 스크랩", key=f"t2_sc_{news['link']}"):
                            c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)", (news['title'], news['link'], news['summary'], cached_data['text'], datetime.now().strftime("%Y-%m-%d %H:%M")))
                            conn.commit(); st.success("저장 완료")

# ----------------- [탭 4: 추천 종목] -----------------
with tab4:
    st.subheader("🎯 AI 맞춤 추천종목 발굴 (2-Step 퀀트 필터링)")
    st.write("단순히 뉴스만 보지 않고, 시스템이 실시간 주가를 팩트체크하여 **과대평가된 종목을 스스로 걸러냅니다.**")
    investment_horizon = st.radio("희망 투자 기간 설정", ["단기 (1~3개월 - 테마/모멘텀/수주)", "중기 (3~6개월 - 실적/사이클/정책)", "중장기 (6개월~1년 - 구조적 성장/시장 지배력)", "장기 (1년 이상 - 배당/안정성/메가트렌드)"], horizontal=True)
    
    if st.button(f"🚀 {investment_horizon.split(' ')[0]} 맞춤 추천종목 발굴", type="primary", use_container_width=True):
        my_bar = st.progress(10, text="1단계: 최신 유망 뉴스 스크래핑 중...")
        rec_news = get_naver_news("특징주|목표가|수주|흑자|실적", display=50, sort_type="sim")
        recent_rec_news = [n for n in rec_news if is_within_7_days(n['published'])]
        if not recent_rec_news: recent_rec_news = [n for n in get_naver_news("주식 추천|특징주", display=50, sort_type="sim") if is_within_7_days(n['published'])]
        
        if recent_rec_news:
            my_bar.progress(30, text="1단계: AI가 기사를 읽고 예비 후보 5종목을 1차 발굴 중...")
            step1_prompt = f"다음 뉴스를 분석하여 '{investment_horizon}' 투자에 적합한 유망 종목 5개를 찾아 JSON 배열로만 출력하세요.\n형식: [{{\"name\":\"종목명\",\"ticker\":\"6자리종목코드\"}}]\n\n" + "\n".join([n['title'] for n in recent_rec_news[:30]])
            
            candidates = []
            try:
                res1 = call_gemini_with_fallback(step1_prompt, is_json=True)
                candidates = json.loads(re.search(r'\[.*\]', res1, re.S).group())
            except: pass
            
            if candidates:
                my_bar.progress(60, text="2단계: 시스템이 5개 후보의 '실시간 주가'와 '재무 상태'를 팩트체크 중...")
                candidate_context = ""
                for c_info in candidates[:5]:
                    t_code = c_info.get('ticker', '')
                    n_name = c_info.get('name', '')
                    cp = get_stock_current_price(t_code)
                    fin = get_financial_data(t_code)
                    candidate_context += f"- 종목명: {n_name} (코드: {t_code})\n  [실시간 현재가]: {cp:,.0f}원\n  [재무/밸류에이션]:\n  {fin}\n\n"
                
                my_bar.progress(80, text="3단계: AI가 고평가 종목을 쳐내고 최종 3개 리포트를 작성 중...")
                st.markdown(f"### 🤖 퀀트 모델 가동: {investment_horizon.split(' ')[0]} 최적화 발굴 중...")
                step3_prompt = build_prompt_recommend_step3(candidate_context, recent_rec_news, market_data_str, investment_horizon)
                full_response = st.write_stream(call_gemini_stream_with_fallback(step3_prompt))
                my_bar.empty()
                st.session_state.today_recommendation = full_response
                st.rerun()
            else:
                my_bar.empty()
                st.error("예비 후보 추출에 실패했습니다. 다시 시도해 주세요.")
        else: my_bar.empty(); st.warning("유망 뉴스가 부족합니다.")
    
    if st.session_state.get('today_recommendation'):
        raw_report = st.session_state.today_recommendation
        display_report = raw_report.split("[TRACKING_DATA]")[0].strip() if "[TRACKING_DATA]" in raw_report else raw_report
        
        with st.expander("🎯 AI 맞춤 추천 리포트 (현재가는 아래 UI 카드 참조)", expanded=True): 
            st.write(display_report)
        
        if "[TRACKING_DATA]" in raw_report:
            st.markdown("### 📌 AI 추천 종목 요약 (실시간 주가 자동 반영)")
            cols = st.columns(3)
            for idx, line in enumerate(raw_report.split("[TRACKING_DATA]")[1].strip().split('\n')):
                data = line.split('|')
                if len(data) >= 3:
                    s_name = data[0].strip()
                    s_ticker = data[1].strip()
                    try: t_price = float(re.sub(r'[^\d.]', '', data[-1]))
                    except: t_price = 0.0
                    
                    if s_name and t_price > 0:
                        with cols[idx % 3]:
                            s_price = get_stock_current_price(s_ticker or s_name)
                            st.info(f"**{s_name}** ({s_ticker})")
                            st.metric("실시간 현재가", f"{s_price:,.0f}원")
                            if s_price > 0:
                                st.metric("AI 적정 목표가", f"{t_price:,.0f}원", f"{((t_price - s_price)/s_price)*100:+.1f}%")
                            else:
                                st.metric("AI 적정 목표가", f"{t_price:,.0f}원")
                                
                            if st.button(f"💾 [{s_name}] 찜하기", key=f"sr_{s_name}_{idx}"):
                                c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?,?,?,?,?,?,?,?)", (f"🎯 AI 추천종목: {s_name}", "AI 2단계 정밀 추천 발굴", display_report, datetime.now().strftime("%Y-%m-%d %H:%M"), s_name, s_ticker, s_price, t_price))
                                c.execute("SELECT id FROM portfolio WHERE stock_name=?", (s_name,))
                                if not c.fetchone(): c.execute("INSERT INTO portfolio (stock_name, search_query, ticker, is_owned, avg_price, quantity) VALUES (?,?,?,?,?,?)", (s_name, s_name, s_ticker, 0, 0.0, 0))
                                conn.commit(); st.success(f"'{s_name}' 찜하기 완료!")

# ----------------- [탭 5: 관심종목 (초고속 병렬 퀀트 패치 완료)] -----------------
with tab5:
    st.subheader("⭐️ 내 관심종목 & AI 앙상블 진단")
    with st.form("add_stock"):
        new_s = st.text_input("종목명 입력 (예: 카카오, 삼성전자)")
        st_owned = st.radio("보유상태", ["미보유", "보유중"], horizontal=True)
        c1, c2 = st.columns(2)
        avg_p = c1.text_input("평단가", value="0")
        qty = c2.number_input("수량", min_value=0, value=0)
        if st.form_submit_button("➕ 종목 수동 등록") and new_s:
            with st.spinner("정보 분석 중..."):
                res = call_gemini_with_fallback(f"한국주식 '{new_s}'의 야후티커와 검색어 JSON으로 줘. {{'ticker':'', 'query':''}}", is_json=True, use_lite=True)
                try:
                    data = json.loads(re.search(r'\{.*\}', res, re.S).group())
                    c.execute("INSERT INTO portfolio (stock_name, search_query, ticker, is_owned, avg_price, quantity) VALUES (?,?,?,?,?,?)", (new_s.strip(), data.get('query', new_s), data.get('ticker', ''), 1 if st_owned=="보유중" else 0, float(avg_p.replace(',','')), qty))
                    conn.commit(); st.rerun()
                except: st.error("등록 실패")

    c.execute("SELECT id, stock_name, search_query, ticker, is_owned, avg_price, quantity FROM portfolio")
    portfolio = c.fetchall()
    
    if portfolio:
        all_kws = list(set(["주가","실적","목표가","수주","공급","M&A"] + get_dynamic_business_keywords()))
        port_cache = {}
        with st.spinner("⚡ 1/4/3번 동시 수집: 실시간 주가 + 수급 동향 + 기술적 보조지표 + DART 최신공시 초고속 병렬 처리 중..."):
            # 💡 속도 향상법: AI가 없는 순수 연산/크롤링 작업을 하나의 쓰레드 패키지로 묶어 초고속 동시 출발
            def fetch_p(p_data_tuple):
                p, start_idx = p_data_tuple
                p_id, name, query, ticker, owned, avg, qnt = p
                cur_p = get_stock_current_price(ticker or name)
                
                # 병렬 수집 1번 기능: 기술적 지표 자동 연산
                tech_indicators_str = calculate_technical_indicators(ticker or name)
                # 병렬 수집 4번 기능: 네이버 순매매 수급 동향 파싱
                supply_demand_str = fetch_supply_demand_trend(ticker or name)
                # 병렬 수집 3번 기능: Open DART 무료 공시 리스트업
                dart_disclosures_str = fetch_dart_disclosures(ticker or name)
                
                broad = "|".join([k.strip() for k in (query or name).split(" OR ")])
                raw = get_naver_news(broad, display=100, start=start_idx)
                now = datetime.now(timezone.utc)
                raw = [n for n in raw if (now - n.get('raw_date', now)) <= timedelta(hours=24)]
                if not raw: raw = [n for n in get_naver_news(broad, display=100, start=1) if (now - n.get('raw_date', now)) <= timedelta(hours=24)]
                
                fact_news = [n for n in raw if any(k in n['title'] or k in n['summary'] for k in all_kws)]
                if not fact_news and raw: fact_news = raw[:10]
                if not fact_news: fact_news = [n for n in get_naver_news(name, display=50, start=start_idx, sort_type="sim") if is_within_7_days(n['published'])][:10]
                
                return p_id, cur_p, fact_news, raw, tech_indicators_str, supply_demand_str, dart_disclosures_str

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                tasks = [(p, st.session_state.port_starts.get(p[0], 1)) for p in portfolio]
                for r in executor.map(fetch_p, tasks): port_cache[r[0]] = r

        @st.fragment
        def render_stock_box(p, p_data):
            p_id, name, query, ticker, is_owned, avg_price, quantity = p
            cur_price, fact_news, raw_news, tech_str, supply_str, dart_str = p_data[1], p_data[2], p_data[3], p_data[4], p_data[5], p_data[6]
            
            st.markdown(f"### 📌 [{name}]")
            
            # 상단 레이아웃에 기술적 수치 및 당일 매매수급 계측 데이터 노출
            c_m1, c_m2 = st.columns(2)
            with c_m1:
                st.caption("📈 **1번 기술적 수치 지표 (RSI/이평선)**")
                st.code(tech_str, language="text")
            with c_m2:
                st.caption("👥 **4번 외국인/기관 당일 매매 동향**")
                st.code(supply_str, language="text")
                
            col_info, col_btn = st.columns([3, 1])
            with col_info:
                if is_owned:
                    roi = ((cur_price - avg_price)/avg_price)*100 if avg_price > 0 else 0.0
                    st.caption(f"💼 **보유** | 평단:{avg_price:,.0f} | 수량:{quantity} | 현재:{cur_price:,.0f} | 수익률: {'🔴' if roi>0 else '🔵'} {roi:.2f}%")
                else: st.caption(f"👀 **관심 (미보유)** | 현재가: {cur_price:,.0f}원")
            
            with col_btn:
                cache_key = f"deep_{p_id}"
                has_cache = cache_key in st.session_state.analysis_results
                if has_cache:
                    if st.button("📊 저장된 진단 보기", key=f"view_{p_id}", type="primary"):
                        st.session_state[f"show_{p_id}"] = True
                else:
                    if st.button("🚀 AI 앙상블 심층 진단", key=f"run_{p_id}", type="primary"):
                        with st.spinner("🤖 퀀트 결합 진단서 작성 중... (탭 이동 가능)"):
                            ai_news = st.session_state.get(f"ai_news_{p_id}", [])
                            combined = {n['link']: n for n in (fact_news + ai_news)}.values()
                            
                            # 💡 1번, 4번 데이터를 포함해 입체적 애널리스트 프롬프트 발행
                            prompt = build_prompt_deep_dive(name, ticker, list(combined), 1 if is_owned else 0, avg_price, quantity, cur_price, market_data_str, tech_str, supply_str)
                            report = call_gemini_with_fallback(prompt)
                            st.session_state.analysis_results[cache_key] = {"text": report, "time": time.time()}
                            st.session_state[f"show_{p_id}"] = True
                            st.rerun()

            if st.session_state.get(f"show_{p_id}"):
                with st.expander("📝 AI 포트폴리오 진단 리포트", expanded=True):
                    rep_data = st.session_state.analysis_results[cache_key]
                    st.success(f"⚡ 저장된 분석 리포트 ({datetime.fromtimestamp(rep_data['time']).strftime('%m-%d %H:%M')})")
                    rep = rep_data['text']
                    st.write(re.sub(r'TARGET_PRICE:\s*[\d,]+', '', rep).strip())
                    tp = 0.0
                    match = re.search(r'TARGET_PRICE:\s*([\d,]+)', rep)
                    if match: tp = float(match.group(1).replace(',',''))
                    
                    c1, c2 = st.columns(2)
                    if c1.button("💾 이 리포트 스크랩 (적중률 추적)", key=f"save_{p_id}", use_container_width=True):
                        c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?,?,?,?,?,?,?,?)", (f"[{name}] 진단 리포트", "앙상블 심층 진단", re.sub(r'TARGET_PRICE:\s*[\d,]+', '', rep).strip(), datetime.now().strftime("%Y-%m-%d %H:%M"), name, ticker, cur_price, tp))
                        conn.commit(); st.success("저장 완료")
                    if c2.button("🔄 강제 재분석 (토큰 소모)", key=f"force_{p_id}", use_container_width=True):
                        del st.session_state.analysis_results[cache_key]; st.rerun()

            # 💡 3번 기능: DART 공시 무료 상시 노출 구역 (해석 버튼을 누를 때만 AI 작동하여 비용 세이브)
            with st.expander(f"🏢 3번 Open DART 최근 주요 공시 확인하기", expanded=False):
                st.markdown(dart_str)
                if "•" in dart_str: # 공시 정보가 존재하는 경우에만 해석 버튼 노출
                    if st.button("🤖 발견된 최신 공시들 AI 정밀 해석 요청 (토큰 소모)", key=f"dart_ai_{p_id}"):
                        with st.spinner("공시 전문 구조 분석 중..."):
                            prompt_dart = f"[{name}]의 최근 공시 목록입니다.\n{dart_str}\n\n이 중에서 자본 변동, 경영권, 대규모 계약 등 주가에 지대한 영향을 주는 핵심 공시가 있다면 투자자 관점에서 호재인지 악재인지 쉽고 날카롭게 원포인트 요약해 주십시오."
                            st.session_state.analysis_results[f"dart_res_{p_id}"] = call_gemini_with_fallback(prompt_dart)
                if f"dart_res_{p_id}" in st.session_state.analysis_results:
                    st.info(st.session_state.analysis_results[f"dart_res_{p_id}"])

            with st.expander(f"📰 '{name}' 최신 뉴스 ({len(fact_news)}건)", expanded=False):
                if st.button("✨ AI 문맥 정밀 필터 가동", key=f"ai_f_{p_id}"):
                    with st.spinner("Lite 모델이 옥석을 가려내는 중..."):
                        news_context = "\n".join([f"[{i}] {n['title']}" for i,n in enumerate(raw_news[:30])])
                        res = call_gemini_with_fallback(f"{news_context}\n\n위 뉴스 중 호재/악재 등 투자 맥락상 핵심 기사 인덱스만 JSON [0,1,5] 형태로 최대 7개 뽑아줘", use_lite=True)
                        try:
                            idx = json.loads(re.search(r'\[.*\]', res).group())
                            st.session_state[f"ai_news_{p_id}"] = [raw_news[:30][i] for i in idx if i < len(raw_news[:30])]
                            st.success("✅ 필터링 완료")
                        except: st.error("필터링 실패")
                
                display_news = st.session_state.get(f"ai_news_{p_id}", fact_news[:10])
                for i, n in enumerate(display_news):
                    st.markdown(f"**[{n['title']}]({n['link']})**")
                    st.caption(f"{n['published']} | {n['summary'][:100]}...")
                    if st.button("🤖 이 뉴스 심층 분석", key=f"ind_n_{p_id}_{i}"):
                        with st.spinner("분석 중..."):
                            st.session_state.analysis_results[f"n_{n['link']}"] = {"text": call_gemini_with_fallback(build_prompt_single_news(n['title'], n['summary'], market_data_str))}
                    if f"n_{n['link']}" in st.session_state.analysis_results:
                        st.info(st.session_state.analysis_results[f"n_{n['link']}"]['text'])
            
            # 하단 관리 버튼 배치
            col_edit1, col_edit2 = st.columns([1, 1])
            with col_edit1:
                with st.expander("⚙️ 상태 변경"):
                    with st.form(key=f"edit_{p_id}"):
                        new_own = st.radio("보유", ["미보유", "보유중"], index=1 if is_owned else 0)
                        na_p = st.text_input("평단", value=f"{int(avg_price)}")
                        nq = st.number_input("수량", min_value=0, value=int(quantity))
                        if st.form_submit_button("수정"):
                            try: final_p = float(na_p.replace(',', ''))
                            except: final_p = 0.0
                            if new_own == "미보유": final_p, nq = 0.0, 0
                            c.execute("UPDATE portfolio SET is_owned=?, avg_price=?, quantity=? WHERE id=?", (1 if new_own=="보유중" else 0, final_p, int(nq), p_id))
                            conn.commit(); st.rerun()
            with col_edit2:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("🗑️ 관심종목 삭제", key=f"del_{p_id}", use_container_width=True):
                    c.execute("DELETE FROM portfolio WHERE id=?", (p_id,)); conn.commit(); st.rerun()
            st.divider()

        for p in portfolio:
            if p[0] in port_cache: render_stock_box(p, port_cache[p[0]])

# ----------------- [탭 6: 스크랩북] -----------------
with tab6:
    st.subheader("📁 내 스크랩북 & AI 예측 트래킹")
    c.execute("SELECT id, title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price FROM scrapbook ORDER BY id DESC")
    scraps = c.fetchall()
    
    if scraps:
        with st.expander("🗑️ 여러 스크랩 한 번에 일괄 삭제"):
            with st.form("bulk_del_form"):
                del_ids = [s[0] for s in scraps if st.checkbox(f"[{s[5]}] {s[1]}", key=f"bd_{s[0]}")]
                if st.form_submit_button("선택 삭제", type="primary") and del_ids:
                    c.execute(f"DELETE FROM scrapbook WHERE id IN ({','.join(['?']*len(del_ids))})", tuple(del_ids))
                    conn.commit(); st.rerun()

        for s in scraps:
            with st.expander(f"[{s[5]}] {s[1]}"):
                if s[6] and s[9] > 0:
                    cur = get_stock_current_price(s[7] or s[6])
                    roi = ((cur - s[8]) / s[8]) * 100 if s[8] > 0 else 0.0
                    ach = (cur / s[9]) * 100 if s[9] > 0 else 0.0
                    
                    c1, c2, c3 = st.columns(3)
                    c1.metric("저장가", f"{s[8]:,.0f}")
                    c2.metric("실시간 주가", f"{cur:,.0f}", f"{roi:+.2f}%")
                    c3.metric("AI 목표가", f"{s[9]:,.0f}", f"{ach:.1f}% 달성")
                    st.divider()
                
                if s[2]: st.markdown(f"[기사 원문]({s[2]})")
                st.write(s[4])
                
                html = f"<html><head><meta charset='utf-8'></head><body><h2>{s[1]}</h2><p>{s[5]}</p><hr><p>{s[4].replace(chr(10), '<br>')}</p></body></html>"
                cl1, cl2 = st.columns(2)
                cl1.download_button("📄 HTML 저장", html, f"Report_{s[0]}.html", "text/html")
                if cl2.button("🗑️ 삭제", key=f"sd_{s[0]}"):
                    c.execute("DELETE FROM scrapbook WHERE id=?", (s[0],)); conn.commit(); st.rerun()
    else: st.info("저장된 스크랩 리포트가 없습니다.")

# ----------------- [탭 7: 설정 및 백업] -----------------
with tab7:
    st.subheader("⚙️ 데이터 관리")
    c.execute("SELECT COUNT(*) FROM oauth_creds")
    is_authenticated = c.fetchone()[0] > 0
    
    if not is_authenticated:
        try:
            flow = Flow.from_client_config(json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"]), scopes=SCOPES, redirect_uri=st.secrets["REDIRECT_URI"])
            url, state = flow.authorization_url(prompt='consent')
            c.execute("DELETE FROM oauth_store")
            c.execute("INSERT INTO oauth_store VALUES (?,?)", (state, flow.code_verifier))
            conn.commit()
            st.link_button("👉 구글 드라이브 연동 로그인", url)
        except Exception as e: st.error(f"인증 URL 생성 실패: {e}")
    else:
        st.success("✅ 구글 드라이브 인증이 완료되었습니다.")
        if st.button("🔌 연동 해제"):
            c.execute("DELETE FROM oauth_creds"); conn.commit(); st.rerun()
            
        c.execute("SELECT * FROM portfolio"); p_all = c.fetchall()
        c.execute("SELECT * FROM scrapbook"); s_all = c.fetchall()
        json_data = json.dumps({"portfolio": p_all, "scrapbook": s_all}, ensure_ascii=False)
        
        c1, c2 = st.columns(2)
        c1.download_button("로컬 기기에 다운로드", json_data, f"backup_{int(time.time())}.json", "application/json")
        if c2.button("🚀 구글 드라이브에 지금 덮어쓰기 백업"):
            try:
                upload_to_google_drive(json_data)
                st.success("구글 클라우드에 백업 성공!")
            except Exception as e: st.error(f"실패: {e}")
            
        st.divider()
        if st.button("🔄 최신 구글 백업 강제 불러오기 (현재 데이터 덮어씀)"):
            try:
                content_bytes, file_name = download_latest_from_google_drive()
                db = json.loads(content_bytes.decode('utf-8'))
                c.execute("DELETE FROM portfolio"); c.execute("DELETE FROM scrapbook")
                for p in db['portfolio']: c.execute("INSERT INTO portfolio VALUES (" + ",".join(["?"]*len(p)) + ")", p)
                for s in db['scrapbook']: c.execute("INSERT INTO scrapbook VALUES (" + ",".join(["?"]*len(s)) + ")", s)
                conn.commit(); st.success(f"복구 완료: {file_name}"); st.rerun()
            except Exception as e: st.error(f"실패: {e}")
