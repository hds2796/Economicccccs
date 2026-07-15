import streamlit as st
import requests
import re
import sqlite3
import json
import os
import io
import time
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

# --- [데이터베이스 설정 및 스키마 업데이트] ---
conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS scrapbook 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, link TEXT, summary TEXT, analysis TEXT, scrap_date TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS portfolio 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, stock_name TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_store (state TEXT, verifier TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_creds (creds TEXT)''')
conn.commit()

try: c.execute("ALTER TABLE portfolio ADD COLUMN search_query TEXT")
except sqlite3.OperationalError: pass
try: c.execute("ALTER TABLE portfolio ADD COLUMN ticker TEXT")
except sqlite3.OperationalError: pass
try: c.execute("ALTER TABLE portfolio ADD COLUMN is_owned INTEGER DEFAULT 0")
except sqlite3.OperationalError: pass
try: c.execute("ALTER TABLE portfolio ADD COLUMN avg_price REAL DEFAULT 0.0")
except sqlite3.OperationalError: pass
try: c.execute("ALTER TABLE portfolio ADD COLUMN quantity INTEGER DEFAULT 0")
except sqlite3.OperationalError: pass

try: c.execute("ALTER TABLE scrapbook ADD COLUMN stock_name TEXT")
except sqlite3.OperationalError: pass
try: c.execute("ALTER TABLE scrapbook ADD COLUMN ticker TEXT")
except sqlite3.OperationalError: pass
try: c.execute("ALTER TABLE scrapbook ADD COLUMN saved_price REAL DEFAULT 0.0")
except sqlite3.OperationalError: pass
try: c.execute("ALTER TABLE scrapbook ADD COLUMN target_price REAL DEFAULT 0.0")
except sqlite3.OperationalError: pass
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
            flow = Flow.from_client_config(
                client_config,
                scopes=SCOPES,
                redirect_uri=st.secrets["REDIRECT_URI"]
            )
            
            flow.code_verifier = verifier
            flow.fetch_token(code=code)
            creds = flow.credentials
            
            cred_dict = {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': creds.scopes
            }
            
            c.execute("DELETE FROM oauth_creds")
            c.execute("INSERT INTO oauth_creds VALUES (?)", (json.dumps(cred_dict),))
            c.execute("DELETE FROM oauth_store") 
            conn.commit()
            
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"구글 로그인 인증 오류가 발생했습니다: {e}")

handle_oauth_callback()

def init_drive_service():
    c.execute("SELECT creds FROM oauth_creds")
    row = c.fetchone()
    if row:
        try:
            cred_dict = json.loads(row[0])
            creds = Credentials.from_authorized_user_info(cred_dict, SCOPES)
            return build('drive', 'v3', credentials=creds)
        except:
            pass
    return None

def upload_to_google_drive(json_string):
    service = init_drive_service()
    if not service: raise Exception("먼저 구글 드라이브로 로그인해야 합니다.")
    file_name = f"market_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    file_metadata = {'name': file_name, 'parents': [st.secrets["GOOGLE_FOLDER_ID"]]}
    json_bytes = json_string.encode('utf-8')
    media = MediaIoBaseUpload(io.BytesIO(json_bytes), mimetype='application/json', resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')

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
            headers = {'User-Agent': 'Mozilla/5.0'}
            data = requests.get(url, headers=headers, timeout=3).json()['datas'][0]
            current = float(data['closePrice'].replace(',', ''))
            diff = float(data['compareToPreviousClosePrice'].replace(',', ''))
            diff_pct = float(data['fluctuationsRatio'].replace(',', ''))
            
            f_code = str(data.get('compareToPreviousPrice', {}).get('code', '3'))
            if f_code in ['4', '5']:
                diff = -abs(diff)
                diff_pct = -abs(diff_pct)
            else:
                diff = abs(diff)
                diff_pct = abs(diff_pct)
            return {"current": current, "diff": diff, "diff_pct": diff_pct}
        except Exception: return {"current": 0, "diff": 0, "diff_pct": 0.0}

    results["코스피 (실시간)"] = fetch_naver_realtime("KOSPI")
    results["코스닥 (실시간)"] = fetch_naver_realtime("KOSDAQ")

    for name, ticker in {"S&P 500 (실시간)": "^GSPC", "원/달러 환율": "KRW=X"}.items():
        try:
            # 주말/휴장일 대응을 위해 넉넉하게 5일 치를 불러옴
            data = yf.Ticker(ticker).history(period="5d")
            if len(data) >= 2:
                prev_close = float(data['Close'].iloc[-2])
                current = float(data['Close'].iloc[-1])
                diff = current - prev_close
                results[name] = {"current": current, "diff": diff, "diff_pct": (diff / prev_close) * 100}
        except: results[name] = {"current": 0, "diff": 0, "diff_pct": 0.0}
    return results

@st.cache_data(ttl=300)
def get_stock_current_price(ticker):
    if not ticker: return 0.0
    try:
        code_match = re.search(r'\d{6}', ticker)
        if code_match:
            code = code_match.group()
            url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('datas'):
                    return float(data['datas'][0]['closePrice'].replace(',', ''))
                    
        data = yf.Ticker(ticker).history(period="1d")
        if not data.empty:
            return float(data['Close'].iloc[-1])
    except Exception:
        pass
    return 0.0

def clean_html(raw_html):
    if not raw_html: return ""
    return BeautifulSoup(raw_html, "html.parser").get_text()

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
                    except Exception:
                        pub_date_formatted = pub_date_str
                        raw_date = now
                        
                    all_items.append({
                        "title": clean_html(i['title']), 
                        "link": i['link'], 
                        "summary": clean_html(i['description']), 
                        "published": pub_date_formatted,
                        "raw_date": raw_date
                    })
        except Exception:
            pass
            
    unique_items = []
    seen = set()
    for item in all_items:
        if item['link'] not in seen:
            seen.add(item['link'])
            unique_items.append(item)
            
    unique_items.sort(key=lambda x: x['raw_date'], reverse=True)
    return unique_items[:display]

def is_within_7_days(pub_date_str):
    try:
        dt = parsedate_to_datetime(pub_date_str)
        now = datetime.now(timezone.utc)
        return (now - dt) <= timedelta(days=7)
    except Exception:
        return True

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
                unique_news.append(n)
                st.session_state.seen_realtime.add(n['link'])
            if len(unique_news) == 20: break
            
    if len(unique_news) <= 3:
        st.session_state.realtime_start = 1
        st.session_state.seen_realtime = set()
        try:
            prompt = f"'{query}' 검색어로 최신 뉴스가 3개 이하로 부족합니다. '경제'나 '시사' 같은 카테고리 명칭 대신, 현재 뉴스에 자주 등장하는 구체적인 '경제 관련 핵심 용어'와 '시사 관련 핵심 용어' 5개를 '|' 기호로 연결하여 출력하십시오. (예: 금리|환율|물가|부동산|선거)"
            expanded_query_raw = call_gemini_with_fallback(prompt, is_json=False, use_lite=True)
            expanded_query = re.sub(r'[^가-힣a-zA-Z0-9|]', '', expanded_query_raw).strip()
            if not expanded_query or len(expanded_query) < 2:
                expanded_query = "금리|환율|물가|수출|부동산"
            
            batch = get_naver_news(expanded_query, display=20, start=1, sort_type="date")
            st.session_state.realtime_start = 21
            for n in (batch or []):
                if n['link'] not in st.session_state.seen_realtime:
                    unique_news.append(n)
                    st.session_state.seen_realtime.add(n['link'])
                if len(unique_news) >= 10: break
        except Exception:
            fallback_query = "금리|환율|물가|수출|부동산"
            batch = get_naver_news(fallback_query, display=20, start=1, sort_type="date")
            st.session_state.realtime_start = 21
            for n in (batch or []):
                if n['link'] not in st.session_state.seen_realtime:
                    unique_news.append(n)
                    st.session_state.seen_realtime.add(n['link'])
                if len(unique_news) >= 10: break
            
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
                unique_news.append(n)
                st.session_state.seen_eco.add(n['link'])
            if len(unique_news) == 10: break
            
    if not unique_news:
        st.session_state.eco_start = 1
        st.session_state.seen_eco = set()
        batch = get_naver_news(query, display=10, start=1, sort_type="sim")
        st.session_state.eco_start = 11
        for n in (batch or []):
            unique_news.append(n)
            st.session_state.seen_eco.add(n['link'])
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
                    unique_news.append(n)
                    st.session_state.seen_sectors[sector_name].add(n['link'])
            if len(unique_news) == 10: break
            
    if not unique_news:
        st.session_state.sector_starts[sector_name] = 1
        st.session_state.seen_sectors[sector_name] = set()
        batch = get_naver_news(query, display=30, start=1, sort_type="sim")
        st.session_state.sector_starts[sector_name] = 31
        for n in (batch or []):
            if any(b_kw in n['title'] or b_kw in n['summary'] for b_kw in business_kws):
                unique_news.append(n)
                st.session_state.seen_sectors[sector_name].add(n['link'])
            if len(unique_news) == 10: break
            
    st.session_state.current_sector_news[sector_name] = unique_news

# =======================================================
# 💡 AI 호출 로직
# =======================================================
def call_gemini_with_fallback(prompt, is_json=False, use_lite=False):
    if not GEMINI_API_KEY: raise Exception("Gemini API 키 오류")
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    if use_lite:
        models_to_try = [('gemini-3.1-flash-lite', '')]
    else:
        models_to_try = [
            ('gemini-3.5-flash', '\n\n*(💡 3.5 모델이 적용되었습니다.)*'),
            ('gemini-2.5-flash', '\n\n*(💡 3.5 모델 과부하로 2.5-flash가 우회 적용되었습니다.)*'),
            ('gemini-1.5-flash', '\n\n*(💡 2.5 모델 과부하로 1.5-flash가 우회 적용되었습니다.)*'),
            ('gemini-3.1-flash-lite', '\n\n*(💡 1.5 모델 과부하로 3.1 Flash Lite가 우회 적용되었습니다.)*')
        ]
    
    quota_keywords = ["quota exceeded", "quota", "billing"]
    fallback_keywords = ["429", "resource_exhausted", "not found", "404", "503", "high demand", "overloaded", "unavailable"]
    last_exception = None
    
    for model_name, fallback_msg in models_to_try:
        for attempt in range(2): 
            try:
                res = client.models.generate_content(model=model_name, contents=prompt).text
                if not is_json and fallback_msg:
                    res += fallback_msg
                return res
            except Exception as e:
                error_str = str(e).lower()
                last_exception = e
                
                if any(q in error_str for q in quota_keywords):
                    raise Exception(f"일일 API 사용 한도가 초과되었습니다. 유료 결제 계정 상태를 확인하세요. (에러: {e})")
                    
                if "not found" in error_str or "404" in error_str: 
                    break 
                    
                if any(k in error_str for k in fallback_keywords):
                    time.sleep(1.0)
                    continue
                break 
                
    raise Exception(f"API 호출 실패 (서버 오류 지속). (에러: {last_exception})")

def call_gemini_stream_with_fallback(prompt):
    if not GEMINI_API_KEY:
        yield "Gemini API 키 오류"
        return
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    models_to_try = [
        ('gemini-3.5-flash', '\n\n*(💡 3.5 모델이 적용되었습니다.)*'),
        ('gemini-2.5-flash', '\n\n*(💡 3.5 모델 과부하로 2.5-flash가 우회 적용되었습니다.)*'),
        ('gemini-1.5-flash', '\n\n*(💡 2.5 모델 과부하로 1.5-flash가 우회 적용되었습니다.)*'),
        ('gemini-3.1-flash-lite', '\n\n*(💡 1.5 모델 과부하로 3.1 Flash Lite가 우회 적용되었습니다.)*')
    ]
    quota_keywords = ["quota exceeded", "quota", "billing"]
    
    for model_name, fallback_msg in models_to_try:
        try:
            response = client.models.generate_content_stream(model=model_name, contents=prompt)
            for chunk in response:
                if chunk.text:
                    yield chunk.text
            yield fallback_msg
            return
        except Exception as e:
            error_str = str(e).lower()
            if any(q in error_str for q in quota_keywords):
                yield f"\n\n🚨 일일 API 사용 한도가 초과되었습니다. 대시보드를 사용할 수 없습니다."
                return
            continue
            
    yield "\n\n서버 과부하로 분석을 완료할 수 없습니다. 잠시 후 다시 시도해주세요."

@st.cache_data(ttl=86400)
def get_dynamic_business_keywords():
    try:
        prompt = "현재 한국 주식 시장에서 특급 호재나 악재를 나타내는 가장 트렌디하고 핵심적인 비즈니스 키워드 15개를 '|' 기호로 연결하여 출력하십시오. (예: HBM|전고체|밸류업|FDA승인|독점공급|M&A|어닝서프라이즈|경영권분쟁). 부가 설명 없이 키워드만 출력하세요."
        res = call_gemini_with_fallback(prompt, is_json=True, use_lite=True)
        clean_res = re.sub(r'[^가-힣a-zA-Z0-9|]', '', res).strip()
        if len(clean_res.split('|')) > 3:
            return clean_res.split('|')
    except:
        pass
    return ["HBM", "AI", "밸류업", "전고체", "비만치료제", "자율주행", "초전도체", "경영권분쟁", "독점공급", "FDA"]

# =======================================================
# 재무 데이터 및 AI 프롬프트 생성 함수들
# =======================================================
def get_financial_data(ticker):
    fin_data = "재무 데이터 조회 불가 (통신 오류 또는 티커 누락)"
    if not ticker: return fin_data
    try:
        code_match = re.search(r'\d{6}', ticker)
        if code_match:
            code = code_match.group()
            try:
                daum_url = f"https://finance.daum.net/api/quotes/A{code}?summary=false"
                daum_headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.daum.net/'}
                res = requests.get(daum_url, headers=daum_headers, timeout=3)
                if res.status_code == 200:
                    data = res.json()
                    market_cap = data.get('marketCap', 0)
                    per = data.get('per', 'N/A')
                    pbr = data.get('pbr', 'N/A')
                    m_str = f"{market_cap / 100000000:,.0f}억 원" if market_cap else "N/A"
                    per_str = f"{per}배" if per is not None and per != 'N/A' else "N/A"
                    pbr_str = f"{pbr}배" if pbr is not None and pbr != 'N/A' else "N/A"
                    return f"- 시가총액: {m_str}\n- PER: {per_str}\n- PBR: {pbr_str}"
            except: pass
            
            try:
                naver_url = f"https://m.stock.naver.com/api/stock/{code}/basic"
                naver_headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://m.stock.naver.com/'}
                res = requests.get(naver_url, headers=naver_headers, timeout=3)
                if res.status_code == 200:
                    data = res.json()
                    return f"- 시가총액: {data.get('marketValue', 'N/A')}억 원\n- PER: {data.get('per', 'N/A')}배\n- PBR: {data.get('pbr', 'N/A')}배"
            except: pass

        info = {}
        if code_match:
            code = code_match.group()
            yf_ticker = yf.Ticker(f"{code}.KS")
            info = yf_ticker.info
            if not info or info.get('marketCap') is None:
                yf_ticker = yf.Ticker(f"{code}.KQ")
                info = yf_ticker.info
        else:
            yf_ticker = yf.Ticker(ticker)
            info = yf_ticker.info
            
        market_cap = info.get('marketCap')
        per = info.get('trailingPE', 'N/A')
        pbr = info.get('priceToBook', 'N/A')
        
        market_cap_str = f"{market_cap / 1_000_000_000_000:.2f}조 원" if market_cap else "N/A"
        per_str = f"{per:.2f}배" if isinstance(per, (int, float)) else "N/A"
        pbr_str = f"{pbr:.2f}배" if isinstance(pbr, (int, float)) else "N/A"
        
        if market_cap_str != "N/A" or per_str != "N/A" or pbr_str != "N/A":
            fin_data = f"- 시가총액: {market_cap_str}\n- PER: {per_str}\n- PBR: {pbr_str}"
    except Exception: pass
    return fin_data

def build_prompt_single_news(title, summary, market_data_str):
    return (f"아래 뉴스가 주식 시장에 미칠 영향을 분석하십시오.\n"
            f"[현재 실시간 시장 지표]: {market_data_str}\n"
            f"[제목]: {title}\n[요약]: {summary}\n"
            f"위 실시간 시장 지표(지수, 환율 등)의 흐름과 뉴스를 연관 지어 다음을 객관적으로 작성하십시오.\n"
            f"1. 💡 사건 핵심 요약\n2. 📈 시장 파급력 및 현재 지표와의 연관성\n3. 🎯 연관 섹터")

def build_prompt_realtime(news_list, market_data_str):
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    return (f"다음은 방금 네이버에 송고된 최신 실시간 경제/시사 뉴스 {len(news_list)}건과 현재 시장 지표입니다.\n"
            f"[현재 실시간 시장 지표]: {market_data_str}\n\n"
            f"{combined_news}\n\n[양식]\n"
            f"1. 🔔 실시간 핵심 이슈 요약 (가장 주목받고 있는 핫이슈 정리)\n"
            f"2. 📉 경제 및 증시 파급력 (단기적 관점의 영향도)\n"
            f"3. 🎯 주목해야 할 섹터 및 리스크 요인\n\n"
            f"※ 뉴스들의 흐름을 관통하는 최신 트렌드를 객관적으로 분석하십시오.")

def build_prompt_overall(news_list, market_data_str):
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    return (f"다음 수집된 {len(news_list)}개의 주요 뉴스와 현재 시장 지표를 종합하여 증시 방향성을 객관적으로 브리핑하십시오.\n"
            f"[현재 실시간 시장 지표]: {market_data_str}\n\n"
            f"{combined_news}\n\n[양식]\n"
            f"1. 🌐 거시 환경 종합 요약 (현재 지수 및 환율 흐름 반영)\n"
            f"2. ⚖️ 증시 호악재 분석\n"
            f"3. 💡 주목할 섹터\n"
            f"4. 🔮 앞으로 주식시장은? (향후 전망 및 요약 정리)\n\n"
            f"반드시 마지막 줄에 'SCORE: 숫자' 형태로 시장 심리 지수를 0~100 사이로 기재하십시오.")

def build_prompt_sector(sector_name, news_list, market_data_str):
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    return (f"다음 수집된 '{sector_name}' 섹터 관련 최신 주요 뉴스와 실시간 시장 지표를 종합하여 분석하십시오.\n"
            f"[현재 실시간 시장 지표]: {market_data_str}\n\n"
            f"{combined_news}\n\n[양식]\n"
            f"1. 🏭 섹터 전반적 흐름 요약 (시장 지수와 연계)\n"
            f"2. 📈 주요 호재 및 악재 요인\n"
            f"3. 🎯 투자 심리 및 단기 전망")

def build_prompt_recommend(news_list, market_data_str, investment_horizon):
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list[:30]])
    return (f"다음은 최근 시장 핵심 뉴스 30건과 실시간 지표입니다.\n"
            f"[현재 실시간 시장 지표]: {market_data_str}\n\n"
            f"{combined_news}\n\n"
            f"위 뉴스와 실시간 지표를 바탕으로, 사용자가 설정한 투자 기간인 '{investment_horizon}'에 최적화된 기준을 엄격히 적용하여 가장 유망한 '추천종목 3개'를 선정하십시오.\n\n"
            f"[양식]\n"
            f"1. 🥇 추천종목 1: [종목명]\n"
            f"- 선정 근거: (뉴스와 현재 지수 흐름을 바탕으로 {investment_horizon} 관점에서 객관적 작성)\n"
            f"- 투자 전략: (진입 시점 및 비중 등)\n"
            f"- 💰 목표가: [구체적 가격] / 손절가: [구체적 가격]\n\n"
            f"2. 🥈 추천종목 2: [종목명]\n"
            f"- 선정 근거: ...\n"
            f"- 투자 전략: ...\n"
            f"- 💰 목표가: ... / 손절가: ...\n\n"
            f"3. 🥉 추천종목 3: [종목명]\n"
            f"- 선정 근거: ...\n"
            f"- 투자 전략: ...\n"
            f"- 💰 목표가: ... / 손절가: ...\n\n"
            f"※ 중요: 리포트 맨 마지막 줄에 시스템 추적을 위해 추천종목 3개의 데이터를 아래와 같이 기재하십시오. (다른 설명 없이 형식만 유지할 것)\n"
            f"[TRACKING_DATA]\n"
            f"종목명1|티커1|현재가1(숫자만)|목표가1(숫자만)\n"
            f"종목명2|티커2|현재가2(숫자만)|목표가2(숫자만)\n"
            f"종목명3|티커3|현재가3(숫자만)|목표가3(숫자만)")

def build_prompt_deep_dive(stock_name, ticker, news_list, is_owned, avg_price, quantity, current_price, market_data_str):
    fin_data = get_financial_data(ticker)
    user_portfolio_status = "미보유 관심종목 (관망 중)"
    if is_owned == 1:
        roi = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0
        user_portfolio_status = (f"실제 보유 중 (매수단가: {avg_price:,.0f}원, "
                                 f"수량: {quantity}주, 실시간 현재가: {current_price:,.0f}원, "
                                 f"현재 수익률: {roi:.2f}%)")
        
    top_30_news = news_list[:30]
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in top_30_news])
        
    return (f"[{stock_name} 심층 분석 리포트]\n\n"
            f"[현재 실시간 시장 지표]\n{market_data_str}\n\n"
            f"[사용자 포트폴리오 상태]\n- {user_portfolio_status}\n\n"
            f"[최신 핵심 뉴스 TOP {len(top_30_news)}]\n{combined_news}\n\n"
            f"[현재 재무 상태]\n{fin_data}\n\n"
            f"위 데이터를 모두 종합하여 다음 양식으로 브리핑을 작성하십시오. 실시간 거시 지표와 개별 종목의 현재가를 반드시 연계하여 해석하십시오.\n"
            f"1. 🏢 기업 펀더멘털 및 재무 요약\n"
            f"2. 🌐 최신 뉴스 및 거시 지표 파급력 종합 분석\n"
            f"3. 📊 사용자 맞춤형 포트폴리오 진단 (사용자의 매수 단가, 수량, 현재 수익률 언급)\n"
            f"4. 🎯 최종 투자의견 (매수/보유/매도 중 택 1) 및 객관적 근거 제시\n"
            f"5. 💰 적정 목표가 및 손절가 (현재가 대비 객관적 산출 근거를 포함하여 구체적인 가격 제시)\n"
            f"6. 👥 동종 업계(Peer Group) 비교\n\n"
            f"※ 중요: 반드시 리포트 맨 마지막 줄에 'TARGET_PRICE: 숫자' 형태로 단기 목표가격을 숫자로만 기재하십시오. (예: TARGET_PRICE: 85000)")

# =======================================================
# 4. 상단 대시보드 및 UI 구성
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

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(["📰 실시간 경제·시사", "🔥 핵심 경제 뉴스", "📑 섹터별 분석", "🎯 오늘의 추천종목", "⭐️ 내 관심종목", "📁 스크랩북", "⚙️ 데이터 백업/복구"])

# [탭 1: 실시간 경제·시사 뉴스]
with tab1:
    st.subheader("📰 실시간 경제·시사 뉴스 분석")
    st.write("네이버 뉴스에 방금 송고된 최신 경제, 시사, 정치 기사를 실시간(최신순)으로 수집하고 트렌드를 분석합니다.")
    
    realtime_query = "증시|금융|환율|물가|부동산|정책|수출"
    
    if not st.session_state.current_realtime_news:
        fetch_unique_realtime_news(realtime_query)
        
    col_r1, col_r2 = st.columns([4, 1])
    with col_r2:
        if st.button("🔄 실시간 뉴스 갱신", key="refresh_realtime", use_container_width=True):
            st.session_state.realtime_start = 1
            st.session_state.seen_realtime = set()
            get_naver_news.clear()
            fetch_unique_realtime_news(realtime_query)
            st.session_state.realtime_analysis = None
            st.rerun()

    if st.session_state.current_realtime_news:
        if st.button("🤖 실시간 뉴스 TOP 20 기반 종합 분석", type="primary", use_container_width=True):
            my_bar = st.progress(0, text="진행률: 0% (대기 중...)")
            my_bar.progress(30, text="진행률: 30% (실시간 최신 뉴스 20건 수집 중...)")
            
            top_20_realtime = st.session_state.current_realtime_news[:20]
            prompt = build_prompt_realtime(top_20_realtime, market_data_str)
            
            my_bar.progress(80, text="진행률: 80% (AI 실시간 분석 및 리포트 작성 중...)")
            st.markdown("### 🤖 실시간 AI 브리핑 작성 중...")
            
            full_response = st.write_stream(call_gemini_stream_with_fallback(prompt))
            
            my_bar.progress(100, text="진행률: 100% (분석 완료!)")
            time.sleep(1)
            my_bar.empty()
            
            st.session_state.realtime_analysis = full_response
            st.rerun()
                
        if st.session_state.realtime_analysis:
            with st.expander("📊 AI 실시간 시황 종합 브리핑", expanded=True):
                st.markdown(st.session_state.realtime_analysis)
                if st.button("💾 이 리포트 스크랩", key="scrap_realtime_overall"):
                    c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              ("📰 실시간 시황 종합 브리핑", "", "최신 경제/시사 송고 기사 기반", st.session_state.realtime_analysis, datetime.now().strftime("%Y-%m-%d %H:%M"), "", "", 0.0, 0.0))
                    conn.commit()
                    st.success("스크랩북 저장 완료")
        
        st.markdown("---")
        
        recent_realtime = [n for n in st.session_state.current_realtime_news if is_within_7_days(n['published'])]
        if recent_realtime:
            for i, news in enumerate(recent_realtime):
                with st.expander(f"🕒 {news['title']}"):
                    st.markdown(f"[원문 읽기]({news['link']})")
                    st.caption(f"{news['published']}")
                    st.write(news['summary'])
                    if st.button("이 기사 심층 분석", key=f"tr_btn_{news['link']}"):
                        with st.spinner("기사 내용 분석 중..."):
                            prompt = build_prompt_single_news(news['title'], news['summary'], market_data_str)
                            
                            cache_key = f"news_{news['link']}"
                            st.session_state.analysis_results[cache_key] = {
                                "text": call_gemini_with_fallback(prompt),
                                "time": time.time()
                            }
                    
                    cache_key = f"news_{news['link']}"
                    cached_data = st.session_state.analysis_results.get(cache_key)
                    if cached_data and isinstance(cached_data, dict):
                        with st.expander("🤖 AI 뉴스 분석 결과", expanded=True):
                            st.write(cached_data['text'])
                            if st.button("💾 이 리포트 스크랩하기", key=f"tr_scrap_{news['link']}"):
                                c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                          (news['title'], news['link'], news['summary'], cached_data['text'], datetime.now().strftime("%Y-%m-%d %H:%M"), "", "", 0.0, 0.0))
                                conn.commit()
                                st.success("스크랩북 저장 완료")
        else:
            st.info("최근 보도된 뉴스가 없습니다.")

# [탭 2: 핵심 경제 뉴스]
with tab2:
    st.subheader("오늘의 핵심 경제 뉴스")
    st.write("주식 시장과 연관성이 높은 핵심 경제 기사를 정확도순으로 수집합니다.")
    
    eco_query = "경제|증시|주식|코스피|코스닥|금리|실적"
    
    if not st.session_state.current_eco_news:
        fetch_unique_eco_news(eco_query)
        
    col_m1, col_m2 = st.columns([4, 1])
    with col_m2:
        if st.button("🔄 완전히 새로운 뉴스 보기", key="refresh_eco", use_container_width=True):
            fetch_unique_eco_news(eco_query)
            st.session_state.overall_analysis = None
            st.rerun()

    if st.session_state.current_eco_news:
        if st.button("🤖 TOP 50 뉴스 기반 시장 브리핑 생성", type="primary"):
            my_bar = st.progress(0, text="진행률: 0% (대기 중...)")
            
            my_bar.progress(30, text="진행률: 30% (핵심 뉴스 50건 스크래핑 중...)")
            top_50_news = get_naver_news(eco_query, display=50, start=1, sort_type="sim")
            
            my_bar.progress(70, text="진행률: 70% (데이터 정제 및 프롬프트 준비 중...)")
            prompt = build_prompt_overall(top_50_news, market_data_str)
            
            my_bar.progress(90, text="진행률: 90% (AI 실시간 리포트 작성 중...)")
            st.markdown("### 🤖 실시간 AI 브리핑 작성 중...")
            
            full_response = st.write_stream(call_gemini_stream_with_fallback(prompt))
            
            my_bar.progress(100, text="진행률: 100% (분석 완료!)")
            time.sleep(1)
            my_bar.empty()
            
            match = re.search(r'SCORE:\s*(\d+)', full_response)
            score = int(match.group(1)) if match else 50
            clean_text = re.sub(r'SCORE:\s*\d+', '', full_response).strip()
            
            st.session_state.overall_analysis = {"text": clean_text, "score": score}
            st.rerun()
                 
        if st.session_state.overall_analysis:
            score = st.session_state.overall_analysis['score']
            
            if score >= 80: sentiment_label = "매우 강세 🔥"
            elif score >= 60: sentiment_label = "강세 📈"
            elif score >= 40: sentiment_label = "중립 ⚖️"
            elif score >= 20: sentiment_label = "약세 📉"
            else: sentiment_label = "매우 약세 ❄️"
            
            st.markdown(f"**현재 AI 시장 심리 지수: {score} / 100 ({sentiment_label})**")
            st.progress(score / 100.0)
            
            with st.expander("📝 AI 거시 환경 브리핑 전체 보기", expanded=True):
                st.markdown(st.session_state.overall_analysis['text'])
        
        st.markdown("---")
        
        recent_eco_news = [n for n in st.session_state.current_eco_news if is_within_7_days(n['published'])]
        if recent_eco_news:
            for i, news in enumerate(recent_eco_news):
                with st.expander(f"📰 {news['title']}"):
                    st.markdown(f"[원문 읽기]({news['link']})")
                    st.caption(f"{news['published']}")
                    st.write(news['summary'])
                    
                    if st.button("이 기사 심층 분석", key=f"t1_btn_{news['link']}"):
                        with st.spinner("기사 내용 분석 중..."):
                            prompt = build_prompt_single_news(news['title'], news['summary'], market_data_str)
                            cache_key = f"eco_{news['link']}"
                            st.session_state.analysis_results[cache_key] = {"text": call_gemini_with_fallback(prompt), "time": time.time()}
                    
                    cache_key = f"eco_{news['link']}"
                    cached_data = st.session_state.analysis_results.get(cache_key)
                    if cached_data and isinstance(cached_data, dict):
                        with st.expander("🤖 AI 뉴스 분석 결과", expanded=True):
                            st.write(cached_data['text'])
                            if st.button("💾 이 리포트 스크랩하기", key=f"t1_scrap_{news['link']}"):
                                c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                          (news['title'], news['link'], news['summary'], cached_data['text'], datetime.now().strftime("%Y-%m-%d %H:%M"), "", "", 0.0, 0.0))
                                conn.commit()
                                st.success("스크랩북 저장 완료")
        else:
            st.info("최근 7일 이내에 보도된 뉴스가 없습니다.")

# [탭 3: 섹터별 분석]
with tab3:
    sectors = {
        "반도체": "반도체|삼성전자|SK하이닉스", 
        "2차전지": "2차전지|전기차|배터리", 
        "바이오": "바이오|제약|신약", 
        "금융/밸류업": "금융|은행|밸류업|증권", 
        "IT/플랫폼": "IT|플랫폼|네이버|카카오|인공지능", 
        "방산/조선": "방산|조선|K방산"
    }
    
    col_s1, col_s2 = st.columns([4, 1])
    with col_s1:
        selected_sector = st.selectbox("관심 섹터 선택", list(sectors.keys()))
        
    if selected_sector not in st.session_state.current_sector_news:
        fetch_unique_sector_news(selected_sector, sectors[selected_sector])
        
    with col_s2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🔄 다른 기사 보기", key="refresh_sector", use_container_width=True):
            fetch_unique_sector_news(selected_sector, sectors[selected_sector])
            if f'sector_summary_{selected_sector}' in st.session_state:
                del st.session_state[f'sector_summary_{selected_sector}']
            st.rerun()
            
    sector_news = st.session_state.current_sector_news.get(selected_sector, [])
    
    if sector_news:
        if st.button(f"🤖 '{selected_sector}' 섹터 종합 분석 (TOP 20 뉴스 기반)", type="primary"):
            my_bar = st.progress(0, text="진행률: 0% (대기 중...)")
            
            my_bar.progress(30, text=f"진행률: 30% ({selected_sector} 뉴스 스크래핑 중...)")
            top_20_news = get_naver_news(sectors[selected_sector], display=20, start=1, sort_type="sim")
            
            prompt = build_prompt_sector(selected_sector, top_20_news, market_data_str)
            
            my_bar.progress(80, text="진행률: 80% (AI 실시간 분석 및 리포트 작성 중...)")
            st.markdown(f"### 🤖 [{selected_sector}] 실시간 AI 브리핑 작성 중...")
            
            full_response = st.write_stream(call_gemini_stream_with_fallback(prompt))
            
            my_bar.progress(100, text="진행률: 100% (분석 완료!)")
            time.sleep(1)
            my_bar.empty()
            
            st.session_state[f'sector_summary_{selected_sector}'] = full_response
            st.rerun()
            
        if f'sector_summary_{selected_sector}' in st.session_state:
            with st.expander("📊 AI 섹터 종합 브리핑", expanded=True):
                st.write(st.session_state[f'sector_summary_{selected_sector}'])
            st.markdown("---")
            
        recent_sector_news = [n for n in sector_news if is_within_7_days(n['published'])]
        if recent_sector_news:
            for i, news in enumerate(recent_sector_news):
                with st.expander(f"📰 {news['title']}"):
                    st.markdown(f"[원문 읽기]({news['link']})\n\n{news['summary']}")
                    if st.button("AI 분석 실행", key=f"t2_btn_{news['link']}"):
                        with st.spinner("분석 중..."):
                            prompt = build_prompt_single_news(news['title'], news['summary'], market_data_str)
                            cache_key = f"sec_{news['link']}"
                            st.session_state.analysis_results[cache_key] = {"text": call_gemini_with_fallback(prompt), "time": time.time()}
                    
                    cache_key = f"sec_{news['link']}"
                    cached_data = st.session_state.analysis_results.get(cache_key)
                    if cached_data and isinstance(cached_data, dict):
                        with st.expander("🤖 AI 뉴스 분석 결과", expanded=True):
                            st.write(cached_data['text'])
                            if st.button("💾 스크랩", key=f"t2_scrap_{news['link']}"):
                                c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                          (news['title'], news['link'], news['summary'], cached_data['text'], datetime.now().strftime("%Y-%m-%d %H:%M"), "", "", 0.0, 0.0))
                                conn.commit()
                                st.success("저장 완료")
        else:
            st.info("최근 7일 이내에 보도된 관련 섹터 뉴스가 없습니다.")

# [탭 4: 오늘의 추천종목]
with tab4:
    st.subheader("🎯 AI 오늘의 맞춤 추천종목 발굴")
    st.write("시장 최신 뉴스를 분석하여 설정한 투자 기간의 특성에 부합하는 가장 유망한 종목 3가지를 추천합니다.")
    
    investment_horizon = st.radio(
        "희망 투자 기간 설정", 
        ["단기 (1~3개월 - 테마/모멘텀/수주)", "중기 (3~6개월 - 실적/사이클/정책)", "중장기 (6개월~1년 - 구조적 성장/시장 지배력)", "장기 (1년 이상 - 배당/안정성/메가트렌드)"],
        horizontal=True
    )
    
    if st.button(f"🚀 {investment_horizon.split(' ')[0]} 맞춤 추천종목 발굴 실행", type="primary", use_container_width=True):
        my_bar = st.progress(0, text="진행률: 0% (대기 중...)")
        
        my_bar.progress(30, text="진행률: 30% (시장의 최신 핵심 뉴스 스크래핑 중...)")
        rec_query = "특징주|목표가|수주|흑자|실적"
        rec_news = get_naver_news(rec_query, display=50, start=1, sort_type="sim")
        recent_rec_news = [n for n in rec_news if is_within_7_days(n['published'])]
        
        if not recent_rec_news:
            fallback_rec_news = get_naver_news("주식 추천|특징주", display=50, start=1, sort_type="sim")
            recent_rec_news = [n for n in fallback_rec_news if is_within_7_days(n['published'])]
        
        if recent_rec_news:
            my_bar.progress(70, text=f"진행률: 70% ({investment_horizon.split(' ')[0]} 관점 데이터 필터링 및 프롬프트 준비 중...)")
            prompt = build_prompt_recommend(recent_rec_news, market_data_str, investment_horizon)
            
            my_bar.progress(90, text="진행률: 90% (AI 추천 알고리즘 가동 및 종목 발굴 중...)")
            st.markdown(f"### 🤖 {investment_horizon.split(' ')[0]} AI 맞춤 추천종목 발굴 중...")
            
            full_response = st.write_stream(call_gemini_stream_with_fallback(prompt))
            
            my_bar.progress(100, text="진행률: 100% (발굴 완료!)")
            time.sleep(1)
            my_bar.empty()
            
            st.session_state.today_recommendation = full_response
            st.rerun()
        else:
            my_bar.empty()
            st.warning("분석할 만한 최신 유망 뉴스가 부족합니다.")
    
    if st.session_state.get('today_recommendation'):
        raw_report = st.session_state.today_recommendation
        display_report = raw_report.split("[TRACKING_DATA]")[0].strip() if "[TRACKING_DATA]" in raw_report else raw_report
        
        if "[TRACKING_DATA]" in raw_report:
            parts = raw_report.split("[TRACKING_DATA]")
            tracking_lines = parts[1].strip().split('\n')
            
            with st.expander("🎯 AI 맞춤 추천종목 리포트 보기", expanded=True):
                st.write(display_report)
                
            st.markdown("### 📌 찜하기 (스크랩 및 관심종목 자동 등록)")
            st.caption("버튼을 누르면 스크랩북에 개별 저장되고, 내 관심종목(탭 5)에 미보유 상태로 추가됩니다.")
            
            cols = st.columns(3)
            idx = 0
            for line in tracking_lines:
                data = line.split('|')
                if len(data) >= 4:
                    s_name = data[0].strip()
                    s_ticker = data[1].strip()
                    try:
                        t_price = float(re.sub(r'[^\d.]', '', data[3]))
                    except:
                        t_price = 0.0
                    
                    if s_name and t_price > 0:
                        with cols[idx % 3]:
                            if st.button(f"💾 [{s_name}] 찜하기", key=f"scrap_rec_{s_name}_{idx}"):
                                s_price = get_stock_current_price(s_ticker if s_ticker else s_name)
                                c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                          (f"🎯 AI 추천종목: {s_name} ({investment_horizon.split(' ')[0]})", "", "AI 맞춤 추천종목 발굴 리포트", display_report, datetime.now().strftime("%Y-%m-%d %H:%M"), s_name, s_ticker, s_price, t_price))
                                
                                c.execute("SELECT id FROM portfolio WHERE stock_name=?", (s_name,))
                                if not c.fetchone():
                                    c.execute("INSERT INTO portfolio (stock_name, search_query, ticker, is_owned, avg_price, quantity) VALUES (?, ?, ?, ?, ?, ?)", 
                                              (s_name, s_name, s_ticker, 0, 0.0, 0))
                                conn.commit()
                                st.success(f"'{s_name}' 찜하기 완료! (탭 5, 탭 6 확인)")
                        idx += 1
        else:
            with st.expander("🎯 AI 맞춤 추천종목 리포트 보기", expanded=True):
                st.write(raw_report)
                if st.button("💾 추천종목 리포트 통째로 스크랩", key="scrap_rec_fallback"):
                    c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (f"🎯 AI 맞춤 추천종목 ({investment_horizon.split(' ')[0]})", "", "설정 기간에 맞춘 전략적 추천", raw_report, datetime.now().strftime("%Y-%m-%d %H:%M"), "", "", 0.0, 0.0))
                    conn.commit()
                    st.success("스크랩북 저장 완료")

# [탭 5: 관심종목 및 포트폴리오 관리]
with tab5:
    st.subheader("⭐️ 내 관심종목 및 포트폴리오 맞춤 뉴스")
    
    with st.form("add_stock_form"):
        new_stock = st.text_input("종목명 입력 (예: 카카오, 삼성전자, 에코프로)")
        is_owned_ui = st.radio("보유 상태", ["관심종목 (미보유)", "실제 보유중"], horizontal=True)
        
        col_p1, col_p2 = st.columns(2)
        with col_p1:
            avg_price_str = st.text_input("매수 단가 (원, 쉼표 입력 가능)", value="0") if is_owned_ui == "실제 보유중" else "0"
            try:
                avg_price = float(avg_price_str.replace(',', ''))
            except ValueError:
                avg_price = 0.0
        with col_p2:
            quantity = st.number_input("보유 수량 (주)", min_value=0, value=0, step=1) if is_owned_ui == "실제 보유중" else 0
            
        submitted = st.form_submit_button("➕ 종목 수동 등록")
        
        if submitted and new_stock.strip():
            with st.spinner(f"AI가 '{new_stock.strip()}'의 종목 코드와 연관 검색어를 분석 중입니다..."):
                prompt = f"""사용자가 한국 주식 '{new_stock.strip()}'을 관심종목에 추가했습니다. 
                1. 야후 파이낸스 티커: 코스피는 '6자리숫자.KS', 코스닥은 '6자리숫자.KQ'. (모르면 빈 문자열 "")
                2. 검색어: 뉴스 검색 시 유용한 핵심 계열사, 지주사, 자회사, 대표 브랜드, 영문명 등 종목과 관련된 폭넓은 유의어 포함
                반드시 아래 JSON 형식으로만 답변하세요. {{"ticker": "005930.KS", "search_query": "{new_stock.strip()} OR 유의어"}}"""
                
                ticker = ""
                search_query = new_stock.strip()
                try:
                    res = call_gemini_with_fallback(prompt, is_json=True, use_lite=True)
                    match = re.search(r'\{.*\}', res, re.DOTALL)
                    if match:
                        data = json.loads(match.group(0))
                        ticker = data.get("ticker", "")
                        search_query = data.get("search_query", new_stock.strip())
                except Exception:
                    pass
                
                is_owned_int = 1 if is_owned_ui == "실제 보유중" else 0
                c.execute("INSERT INTO portfolio (stock_name, search_query, ticker, is_owned, avg_price, quantity) VALUES (?, ?, ?, ?, ?, ?)", 
                          (new_stock.strip(), search_query, ticker, is_owned_int, float(avg_price), int(quantity)))
                conn.commit()
                st.rerun()
            
    c.execute("SELECT id, stock_name, search_query, ticker, is_owned, avg_price, quantity FROM portfolio")
    portfolio = c.fetchall()
    
    if portfolio:
        st.divider()
        st.write(f"🔍 **등록된 종목 관련 핵심 비즈니스 뉴스 및 포트폴리오 진단**")
        
        dynamic_kws = get_dynamic_business_keywords()
        static_kws = ["주가", "실적", "목표가", "수주", "배당", "합병", "투자", "인수", "매출", "영업이익", "전망", "동향", "계약", "신제품", "개발", "수출", "공급", "M&A", "규제", "상장"]
        all_kws = list(set(static_kws + dynamic_kws))
        
        port_data_cache = {}
        with st.spinner("⚡ 1차 텍스트 망으로 전체 관심종목 뉴스를 초고속 수집 중입니다..."):
            
            def fetch_single_portfolio_data(task_data):
                p, start_idx = task_data
                p_id, p_name, p_query, p_ticker, p_is_owned, p_avg_price, p_quantity = p

                current_price = get_stock_current_price(p_ticker or p_name)
                search_keywords = [k.strip() for k in (p_query or p_name).split(" OR ")]
                broad_query = "|".join(search_keywords)

                raw_news = get_naver_news(broad_query, display=100, start=start_idx, sort_type="date")
                now_utc = datetime.now(timezone.utc)
                raw_news = [n for n in raw_news if (now_utc - n.get('raw_date', now_utc)) <= timedelta(hours=24)]

                if not raw_news:
                    raw_news_fallback = get_naver_news(broad_query, display=100, start=1, sort_type="date")
                    raw_news = [n for n in raw_news_fallback if (now_utc - n.get('raw_date', now_utc)) <= timedelta(hours=24)]

                port_news_all = [n for n in raw_news if any(b_kw in n['title'] or b_kw in n['summary'] for b_kw in all_kws)]

                if not port_news_all and raw_news:
                    port_news_all = raw_news[:10]

                if not port_news_all:
                    raw_news_fallback = get_naver_news(p_name, display=50, start=start_idx, sort_type="sim")
                    port_news_all = [n for n in raw_news_fallback if is_within_7_days(n['published'])][:10]

                return p_id, current_price, port_news_all, raw_news

            tasks = []
            for p in portfolio:
                p_id = p[0]
                tasks.append((p, st.session_state.port_starts.get(p_id, 1)))

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                results = executor.map(fetch_single_portfolio_data, tasks)
                for res in results:
                    p_id, c_price, p_news, r_news = res
                    port_data_cache[p_id] = {"price": c_price, "news": p_news, "raw_news": r_news}

        for p_id, p_name, p_query, p_ticker, p_is_owned, p_avg_price, p_quantity in portfolio:
            st.markdown("---")
            
            p_data = port_data_cache.get(p_id, {"price": 0.0, "news": [], "raw_news": []})
            current_price = p_data["price"]
            port_news_all = p_data["news"]
            raw_news = p_data["raw_news"]
            
            col_title, col_refresh, col_deep = st.columns([3, 1, 2])
            with col_title:
                st.markdown(f"#### 📌 [{p_name}]")
                
            with col_refresh:
                if st.button("🔄 새 뉴스", key=f"ref_port_{p_id}", use_container_width=True):
                    st.session_state.port_starts[p_id] = st.session_state.port_starts.get(p_id, 1) + 10
                    if f"ai_filtered_news_{p_id}" in st.session_state:
                        del st.session_state[f"ai_filtered_news_{p_id}"]
                    st.rerun()
            
            with col_deep:
                cache_key = f"deep_{p_id}"
                cached_report = st.session_state.analysis_results.get(cache_key)
                
                # 영구 캐싱: 이전에 생성된 리포트가 있으면 시간과 무관하게 무조건 불러옴
                has_valid_cache = cached_report and isinstance(cached_report, dict)
                
                if has_valid_cache:
                    if st.button("📊 저장된 진단 보기", type="primary", key=f"t3_deep_{p_id}"):
                        st.session_state[f"show_cache_{p_id}"] = True
                else:
                    if st.button("📊 포트폴리오 심층 진단 (TOP 30)", type="primary", key=f"t3_deep_{p_id}"):
                        my_bar = st.progress(0, text="진행률: 0% (대기 중...)")
                        my_bar.progress(30, text="진행률: 30% (실시간 재무 데이터 매핑 중...)")
                        
                        ai_news = st.session_state.get(f"ai_filtered_news_{p_id}", [])
                        if ai_news:
                            combined_news = port_news_all + ai_news
                            best_news = []
                            seen_links = set()
                            for n in combined_news:
                                if n['link'] not in seen_links:
                                    seen_links.add(n['link'])
                                    best_news.append(n)
                        else:
                            best_news = port_news_all
                            
                        prompt = build_prompt_deep_dive(p_name, p_ticker, best_news, p_is_owned, p_avg_price, p_quantity, current_price, market_data_str)
                        
                        my_bar.progress(80, text="진행률: 80% (AI 실시간 분석 및 리포트 작성 중...)")
                        st.markdown(f"### 🤖 [{p_name}] AI 심층 진단 작성 중...")
                        
                        full_response = st.write_stream(call_gemini_stream_with_fallback(prompt))
                        
                        my_bar.progress(100, text="진행률: 100% (진단 완료!)")
                        time.sleep(1)
                        my_bar.empty()
                        
                        st.session_state.analysis_results[cache_key] = {"text": full_response, "time": time.time()}
                        st.session_state[f"show_cache_{p_id}"] = True
                        st.rerun()

            col_info, col_del = st.columns([5, 1])
            with col_info:
                if p_is_owned == 1:
                    roi = ((current_price - p_avg_price) / p_avg_price) * 100 if p_avg_price > 0 else 0
                    roi_color = "🔴" if roi > 0 else "🔵" if roi < 0 else "⚫"
                    st.caption(f"💼 **보유중** | 매수단가: {p_avg_price:,.0f}원 | 수량: {p_quantity}주 | 현재가: {current_price:,.0f}원 | 수익률: {roi_color} {roi:.2f}%")
                else:
                    st.caption(f"👀 **관심종목 (미보유)** | 현재가: {current_price:,.0f}원")
            with col_del:
                if st.button("✖ 삭제", key=f"del_port_{p_id}"):
                    c.execute("DELETE FROM portfolio WHERE id=?", (p_id,)); conn.commit(); st.rerun()
            
            with st.expander("📝 보유 상태 변경"):
                with st.form(key=f"edit_form_{p_id}"):
                    new_is_owned_ui = st.radio("보유 상태", ["관심종목 (미보유)", "실제 보유중"], index=1 if p_is_owned == 1 else 0)
                    col_e1, col_e2 = st.columns(2)
                    with col_e1:
                        new_avg_price_str = st.text_input("매수 단가 (원, 쉼표 입력 가능)", value=f"{int(p_avg_price):,}")
                    with col_e2:
                        new_quantity = st.number_input("보유 수량 (주)", min_value=0, value=int(p_quantity), step=1)
                    
                    if st.form_submit_button("상태 업데이트"):
                        new_is_owned = 1 if new_is_owned_ui == "실제 보유중" else 0
                        try:
                            parsed_avg_price = float(new_avg_price_str.replace(',', ''))
                        except ValueError:
                            parsed_avg_price = 0.0
                            
                        if new_is_owned == 0:
                            parsed_avg_price = 0.0
                            new_quantity = 0
                        c.execute("UPDATE portfolio SET is_owned=?, avg_price=?, quantity=? WHERE id=?", 
                                  (new_is_owned, parsed_avg_price, int(new_quantity), p_id))
                        conn.commit()
                        st.rerun()
                        
            if st.session_state.get(f"show_cache_{p_id}") and cache_key in st.session_state.analysis_results:
                cached_report_data = st.session_state.analysis_results[cache_key]
                with st.expander("📊 AI 포트폴리오 심층 진단 결과", expanded=True):
                    
                    saved_time = datetime.fromtimestamp(cached_report_data.get('time', time.time())).strftime("%m-%d %H:%M")
                    st.success(f"⚡ 이전에 작성된 분석 리포트({saved_time})를 즉시 불러왔습니다. (토큰 절약)")
                    
                    raw_report = cached_report_data['text']
                    target_price = 0.0
                    match = re.search(r'TARGET_PRICE:\s*([\d,]+)', raw_report)
                    if match:
                        target_price = float(match.group(1).replace(',', ''))
                    clean_report = re.sub(r'TARGET_PRICE:\s*[\d,]+', '', raw_report).strip()
                    
                    st.write(clean_report)
                    
                    col_rp1, col_rp2 = st.columns(2)
                    with col_rp1:
                        if st.button("💾 이 리포트 스크랩 (목표가 추적 시작)", key=f"t3_scrap_deep_{p_id}", use_container_width=True):
                            c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                      (f"[{p_name}] 포트폴리오 심층 진단", "", "TOP 30 뉴스 및 실시간 재무 분석 기반", clean_report, datetime.now().strftime("%Y-%m-%d %H:%M"), p_name, p_ticker, current_price, target_price))
                            conn.commit()
                            st.success("저장 완료. '스크랩북' 탭에서 AI 목표가 적중률을 확인할 수 있습니다.")
                    with col_rp2:
                        if st.button("🔄 최신 데이터로 강제 재분석 (토큰 소모)", key=f"force_re_{p_id}", use_container_width=True):
                            del st.session_state.analysis_results[cache_key]
                            del st.session_state[f"show_cache_{p_id}"]
                            st.rerun()
            
            if port_news_all or raw_news:
                with st.expander(f"📰 '{p_name}' 관련 최신 뉴스 보기", expanded=False):
                    
                    news_to_display = st.session_state.get(f"ai_filtered_news_{p_id}", port_news_all[:10])
                    
                    if st.session_state.get(f"ai_filtered_news_{p_id}"):
                        st.success("✨ AI가 30개의 원본 기사를 읽고, 숨은 호재와 투자 맥락이 담긴 기사만 선별해냈습니다. (심층 진단 시 앙상블 분석 적용됨)")
                    else:
                        st.caption(f"⚡ 1차 필터(트렌드 키워드)로 스크리닝된 뉴스 {len(news_to_display)}건입니다.")
                        if st.button("✨ AI 문맥 정밀 필터링 가동 (숨은 호재 찾기)", key=f"ai_filter_btn_{p_id}"):
                            with st.spinner("AI가 기사 문맥을 정밀하게 읽고 옥석을 가려내는 중... (비용 절감을 위해 Lite 모델 전담 호출)"):
                                prompt = f"다음은 '{p_name}' 관련 최근 뉴스 {len(raw_news[:30])}건입니다.\n"
                                for idx, n in enumerate(raw_news[:30]):
                                    prompt += f"[{idx}] {n['title']} : {n['summary']}\n"
                                prompt += "\n위 기사들 중, 제목에 뻔한 단어가 없더라도 주식 투자자 관점에서 기업 가치에 큰 영향을 미칠 수 있는(우회적 호재/악재 등) 가장 중요한 기사의 인덱스를 JSON 배열(예: [0, 2, 5]) 형태로 최대 7개만 출력하십시오."
                                
                                try:
                                    res = call_gemini_with_fallback(prompt, is_json=True, use_lite=True)
                                    match = re.search(r'\[.*?\]', res, re.DOTALL)
                                    if match:
                                        indices = json.loads(match.group(0))
                                        st.session_state[f"ai_filtered_news_{p_id}"] = [raw_news[:30][i] for i in indices if i < len(raw_news[:30])]
                                        st.rerun()
                                except Exception as e:
                                    st.error("AI 필터링 중 오류가 발생했거나, 한도가 초과되었습니다.")
                    
                    st.markdown("---")
                    for i, news in enumerate(news_to_display):
                        st.markdown(f"**[{news['title']}]({news['link']})**")
                        st.caption(f"{news['published']} | {news['summary'][:150]}...")
                        
                        col_btn1, col_btn2 = st.columns([1, 4])
                        with col_btn1:
                            if st.button("🤖 개별 심층 분석", key=f"t3_btn_{p_id}_{i}"):
                                with st.spinner("분석 중..."):
                                    prompt = build_prompt_single_news(news['title'], news['summary'], market_data_str)
                                    n_cache_key = f"n_{news['link']}"
                                    st.session_state.analysis_results[n_cache_key] = {"text": call_gemini_with_fallback(prompt), "time": time.time()}
                        
                        n_cache_key = f"n_{news['link']}"
                        n_cached_data = st.session_state.analysis_results.get(n_cache_key)
                        if n_cached_data and isinstance(n_cached_data, dict):
                            st.info(n_cached_data['text'])
                        st.markdown("<br>", unsafe_allow_html=True)
            else:
                st.info(f"'{p_name}' 관련 최근 뉴스가 없습니다. (새 뉴스 보기 버튼을 눌러보십시오.)")
    else: st.info("등록된 관심종목이 없습니다.")

# [탭 6: 스크랩북 및 적중률 트래킹]
with tab6:
    st.subheader("📁 내 스크랩북 및 AI 예측 트래킹")
    c.execute("SELECT id, title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price FROM scrapbook ORDER BY id DESC")
    scraps = c.fetchall()
    
    if scraps:
        with st.expander("🗑️ 여러 스크랩 한 번에 선택 삭제하기", expanded=False):
            with st.form("bulk_delete_form"):
                st.write("삭제할 항목을 선택하고 아래 버튼을 누르세요.")
                delete_ids = []
                for s in scraps:
                    s_id, s_title, s_date = s[0], s[1], s[5]
                    if st.checkbox(f"[{s_date}] {s_title}", key=f"bulk_del_{s_id}"):
                        delete_ids.append(s_id)
                
                if st.form_submit_button("선택한 항목 일괄 삭제", type="primary"):
                    if delete_ids:
                        placeholders = ','.join(['?'] * len(delete_ids))
                        c.execute(f"DELETE FROM scrapbook WHERE id IN ({placeholders})", tuple(delete_ids))
                        conn.commit()
                        st.rerun()
                    else:
                        st.warning("선택된 항목이 없습니다.")
        st.divider()

        for s_id, s_title, s_link, s_summary, s_analysis, s_date, s_name, s_ticker, s_saved_price, s_target_price in scraps:
            with st.expander(f"[{s_date}] {s_title}"):
                
                if s_name and s_target_price > 0:
                    current_price = get_stock_current_price(s_ticker or s_name)
                    actual_roi = ((current_price - s_saved_price) / s_saved_price) * 100 if s_saved_price > 0 else 0
                    achievement_rate = (current_price / s_target_price) * 100 if s_target_price > 0 else 0
                    
                    st.markdown("### 🎯 AI 예측 트래커")
                    t_col1, t_col2, t_col3, t_col4 = st.columns(4)
                    t_col1.metric("저장 당시 주가", f"{s_saved_price:,.0f}원")
                    t_col2.metric("실시간 주가", f"{current_price:,.0f}원", f"{actual_roi:+.2f}%")
                    t_col3.metric("AI 목표가", f"{s_target_price:,.0f}원")
                    t_col4.metric("목표가 달성률", f"{achievement_rate:.1f}%")
                    st.divider()

                if s_link: st.markdown(f"[기사 링크]({s_link})\n\n**요약:** {s_summary}\n\n**AI 분석:**\n{s_analysis}")
                else: st.markdown(f"**AI 분석:**\n{s_analysis}")
                
                col_b1, col_b2 = st.columns([1, 1])
                with col_b1:
                    html_content = f"""
                    <html>
                    <head><meta charset="utf-8"><title>{s_title}</title></head>
                    <body style="font-family: sans-serif; line-height: 1.6; max-width: 800px; margin: 0 auto; padding: 20px;">
                    <h2>{s_title}</h2>
                    <p><strong>스크랩 날짜:</strong> {s_date}</p>
                    <hr>
                    <h3>요약</h3><p>{s_summary}</p>
                    <h3>AI 분석 리포트</h3><p>{s_analysis.replace(chr(10), '<br>')}</p>
                    </body>
                    </html>
                    """
                    st.download_button("📄 HTML 리포트로 저장", data=html_content, file_name=f"Report_{s_date[:10]}.html", mime="text/html", key=f"dl_{s_id}")
                with col_b2:
                    if st.button("🗑️ 리포트 개별 삭제", key=f"del_scrap_{s_id}"):
                        c.execute("DELETE FROM scrapbook WHERE id=?", (s_id,)); conn.commit(); st.rerun()
    else:
        st.info("저장된 스크랩 리포트가 없습니다. 관심 있는 종목과 뉴스를 분석해 스크랩해 보세요!")

# [탭 7: 데이터 백업/복구]
with tab7:
    st.subheader("⚙️ 데이터 백업 및 복구 관리")
    st.write("클라우드 서버 재부팅 시 수집된 데이터가 초기화될 수 있으므로, 구글 드라이브 보관소에 연동하여 영구 보관하십시오.")
    
    c.execute("SELECT COUNT(*) FROM oauth_creds")
    is_authenticated = c.fetchone()[0] > 0
    
    if not is_authenticated:
        st.warning("클라우드 자동 저장 및 복구 기능을 사용하려면 권한 인증이 필요합니다.")
        try:
            client_config = json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"])
            flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=st.secrets["REDIRECT_URI"])
            auth_url, state = flow.authorization_url(prompt='consent', access_type='offline')
            c.execute("DELETE FROM oauth_store")
            c.execute("INSERT INTO oauth_store (state, verifier) VALUES (?, ?)", (state, flow.code_verifier))
            conn.commit()
            st.markdown(f"### [👉 구글 계정으로 로그인하여 드라이브 연동하기]({auth_url})")
        except Exception as e: st.error(f"Secrets 설정 확인 요망: {e}")
    else:
        col_auth1, col_auth2 = st.columns([3, 1])
        with col_auth1: st.success("✅ 구글 드라이브 인증이 완료되었습니다.")
        with col_auth2:
            if st.button("🔌 연동 해제", use_container_width=True):
                c.execute("DELETE FROM oauth_creds"); conn.commit(); st.rerun()
                
        c.execute("SELECT title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price FROM scrapbook")
        scrap_list = [{"title": r[0], "link": r[1], "summary": r[2], "analysis": r[3], "scrap_date": r[4], "stock_name": r[5], "ticker": r[6], "saved_price": r[7], "target_price": r[8]} for r in c.fetchall()]
        
        c.execute("SELECT stock_name, search_query, ticker, is_owned, avg_price, quantity FROM portfolio")
        port_list = [{"stock_name": r[0], "search_query": r[1], "ticker": r[2], "is_owned": r[3], "avg_price": r[4], "quantity": r[5]} for r in c.fetchall()]
        
        backup_dict = {"scrapbook": scrap_list, "portfolio": port_list}
        json_data = json.dumps(backup_dict, ensure_ascii=False, indent=4)
        
        col_b1, col_b2 = st.columns(2)
        with col_b1:
            st.download_button(label="기기(폰/PC)에 JSON 다운로드", data=json_data.encode('utf-8'), file_name=f"market_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", mime="application/json", use_container_width=True)
        with col_b2:
            if st.button("🚀 구글 드라이브로 백업 파일 자동 전송", use_container_width=True):
                with st.spinner('구글 드라이브 업로드 중...'):
                    try:
                        upload_to_google_drive(json_data)
                        st.success("구글 드라이브 백업 완료!")
                    except Exception as e: st.error(f"업로드 실패: {e}")
            
        st.divider()
        st.markdown("### 📤 데이터 복구 (보관소 -> 서버)")
        if st.button("🔄 구글 드라이브에서 최신 백업 즉시 불러오기", use_container_width=True):
            with st.spinner('최신 백업 탐색 중...'):
                try:
                    content_bytes, file_name = download_latest_from_google_drive()
                    restore_data = json.loads(content_bytes.decode('utf-8'))
                    
                    c.execute("DELETE FROM scrapbook")
                    c.execute("DELETE FROM portfolio")
                    
                    for item in restore_data.get("scrapbook", []):
                        c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                  (item['title'], item['link'], item['summary'], item['analysis'], item['scrap_date'], item.get('stock_name', ''), item.get('ticker', ''), item.get('saved_price', 0.0), item.get('target_price', 0.0)))
                    for item in restore_data.get("portfolio", []):
                        if isinstance(item, str): 
                            c.execute("INSERT INTO portfolio (stock_name) VALUES (?)", (item,))
                        else:
                            c.execute("INSERT INTO portfolio (stock_name, search_query, ticker, is_owned, avg_price, quantity) VALUES (?, ?, ?, ?, ?, ?)", 
                                      (item.get("stock_name"), item.get("search_query"), item.get("ticker"), item.get("is_owned", 0), item.get("avg_price", 0.0), item.get("quantity", 0)))
                    conn.commit()
                    st.success(f"성공! 최신 백업 파일 [{file_name}] 데이터를 정상적으로 복구했습니다.")
                    st.rerun()
                except Exception as e: st.error(f"불러오기 실패: {e}")
