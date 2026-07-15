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
from bs4 import BeautifulSoup
from google import genai

# 로컬 및 클라우드 환경 테스트 시 HTTPS 오류 우회
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ['https://www.googleapis.com/auth/drive']

# --- [페이지 설정] ---
st.set_page_config(page_title="Project2_Stock", page_icon="📊", layout="wide")

# --- [API 키 설정] ---
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
NAVER_CLIENT_ID = st.secrets.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = st.secrets.get("NAVER_CLIENT_SECRET", "")
DART_API_KEY = st.secrets.get("DART_API_KEY", "")

# --- [데이터베이스 설정 및 스키마 업데이트] ---
conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS scrapbook 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, link TEXT, summary TEXT, analysis TEXT, scrap_date TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS portfolio 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, stock_name TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_store (state TEXT, verifier TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_creds (creds TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS market_score_history 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, check_date TEXT, score INTEGER)''')
conn.commit()

for table, col, dtype in [
    ("portfolio", "search_query", "TEXT"), ("portfolio", "ticker", "TEXT"),
    ("portfolio", "is_owned", "INTEGER DEFAULT 0"), ("portfolio", "avg_price", "REAL DEFAULT 0.0"),
    ("portfolio", "quantity", "INTEGER DEFAULT 0"), ("scrapbook", "stock_name", "TEXT"),
    ("scrapbook", "ticker", "TEXT"), ("scrapbook", "saved_price", "REAL DEFAULT 0.0"),
    ("scrapbook", "target_price", "REAL DEFAULT 0.0"), ("scrapbook", "buy_recommend_price", "REAL DEFAULT 0.0")
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
    if st.session_state.get("password_correct", False): return True
    st.title("🔒 Project2_Stock 로그인")
    password = st.text_input("비밀번호를 입력하세요", type="password")
    if st.button("접속하기"):
        if password == st.secrets["APP_PASSWORD"]:
            st.session_state["password_correct"] = True
            st.rerun()
        else: st.error("❌ 비밀번호가 일치하지 않습니다.")
    return False

if not check_password(): st.stop()

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
            st.query_params.clear(); st.warning("로그인 세션 만료. 다시 시도해 주세요.")
            return
        verifier = row[0]
        try:
            flow = Flow.from_client_config(json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"]), scopes=SCOPES, redirect_uri=st.secrets["REDIRECT_URI"])
            flow.code_verifier = verifier
            flow.fetch_token(code=code)
            cred_dict = {'token': flow.credentials.token, 'refresh_token': flow.credentials.refresh_token, 'token_uri': flow.credentials.token_uri, 'client_id': flow.credentials.client_id, 'client_secret': flow.credentials.client_secret, 'scopes': flow.credentials.scopes}
            c.execute("DELETE FROM oauth_creds"); c.execute("INSERT INTO oauth_creds VALUES (?)", (json.dumps(cred_dict),)); c.execute("DELETE FROM oauth_store"); conn.commit()
            st.query_params.clear(); st.rerun()
        except Exception as e: st.error(f"구글 인증 오류: {e}")

handle_oauth_callback()

def init_drive_service():
    c.execute("SELECT creds FROM oauth_creds")
    row = c.fetchone()
    if row:
        try: return build('drive', 'v3', credentials=Credentials.from_authorized_user_info(json.loads(row[0]), SCOPES))
        except: pass
    return None

def upload_to_google_drive(json_string):
    service = init_drive_service()
    if not service: raise Exception("구글 로그인 필요")
    media = MediaIoBaseUpload(io.BytesIO(json_string.encode('utf-8')), mimetype='application/json', resumable=True)
    return service.files().create(body={'name': f"market_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", 'parents': [st.secrets["GOOGLE_FOLDER_ID"]]}, media_body=media, fields='id').execute().get('id')

def download_latest_from_google_drive():
    service = init_drive_service()
    if not service: raise Exception("구글 로그인 필요")
    results = service.files().list(q=f"'{st.secrets['GOOGLE_FOLDER_ID']}' in parents and mimeType = 'application/json' and trashed = false", orderBy="modifiedTime desc", pageSize=1, fields="files(id, name)").execute()
    files = results.get('files', [])
    if not files: raise Exception("백업 파일 없음")
    return service.files().get_media(fileId=files[0]['id']).execute(), files[0]['name']

# =======================================================
# 3. 데이터 상태 관리 및 캐시된 메인 로직
# =======================================================
for key in ['analysis_results', 'overall_analysis', 'realtime_analysis', 'today_recommendation', 'current_realtime_news', 'current_eco_news', 'current_sector_news', 'sector_starts', 'seen_sectors', 'port_starts']:
    if key not in st.session_state: st.session_state[key] = {} if 'news' in key or 'starts' in key or 'sectors' in key or 'results' in key else (None if 'analysis' in key or 'recommendation' in key else [])

if 'realtime_start' not in st.session_state: st.session_state.realtime_start = 1
if 'seen_realtime' not in st.session_state: st.session_state.seen_realtime = set()
if 'eco_start' not in st.session_state: st.session_state.eco_start = 1
if 'seen_eco' not in st.session_state: st.session_state.seen_eco = set()
if 'port_data_cache' not in st.session_state: st.session_state.port_data_cache = {}

@st.cache_data(ttl=60)
def get_market_data():
    results = {}
    def fetch_naver_realtime(code):
        try:
            data = requests.get(f"https://polling.finance.naver.com/api/realtime/domestic/index/{code}", headers={'User-Agent': 'Mozilla/5.0'}, timeout=2).json()['datas'][0]
            current = float(data['closePrice'].replace(',', ''))
            diff = float(data['compareToPreviousClosePrice'].replace(',', ''))
            diff_pct = float(data['fluctuationsRatio'].replace(',', ''))
            if str(data.get('compareToPreviousPrice', {}).get('code', '3')) in ['4', '5']: diff, diff_pct = -abs(diff), -abs(diff_pct)
            return {"current": current, "diff": diff, "diff_pct": diff_pct}
        except: return {"current": 0, "diff": 0, "diff_pct": 0.0}

    results["코스피 (실시간)"] = fetch_naver_realtime("KOSPI")
    results["코스닥 (실시간)"] = fetch_naver_realtime("KOSDAQ")

    def fetch_yahoo_direct(ticker):
        try:
            res = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range=5d&interval=1d", headers={'User-Agent': 'Mozilla/5.0'}, timeout=2).json()
            closes = [c for c in res['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
            if len(closes) >= 2:
                diff = closes[-1] - closes[-2]
                return {"current": closes[-1], "diff": diff, "diff_pct": (diff / closes[-2]) * 100 if closes[-2] > 0 else 0.0}
        except: pass
        return {"current": 0, "diff": 0, "diff_pct": 0.0}

    results["S&P 500 (실시간)"] = fetch_yahoo_direct("^GSPC")
    results["원/달러 환율"] = fetch_yahoo_direct("KRW=X")
    return results

def clean_html(raw_html):
    return BeautifulSoup(raw_html, "html.parser").get_text() if raw_html else ""

# =======================================================
# 💡 순수 파이썬 코어 로직 (볼린저 밴드, 등락률 탑재)
# =======================================================
def raw_get_stock_current_price(ticker):
    res_data = {"current": 0.0, "diff": 0.0, "diff_pct": 0.0}
    if not ticker: return res_data
    try:
        code_match = re.search(r'\d{6}', ticker)
        if code_match:
            res = requests.get(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code_match.group()}", headers={'User-Agent': 'Mozilla/5.0'}, timeout=2)
            if res.status_code == 200 and res.json().get('datas'):
                data = res.json()['datas'][0]
                current = float(data['closePrice'].replace(',', ''))
                diff = float(data['compareToPreviousClosePrice'].replace(',', ''))
                diff_pct = float(data['fluctuationsRatio'].replace(',', ''))
                if str(data.get('compareToPreviousPrice', {}).get('code', '3')) in ['4', '5']: 
                    diff = -abs(diff)
                    diff_pct = -abs(diff_pct)
                res_data.update({"current": current, "diff": diff, "diff_pct": diff_pct})
                return res_data
                
        res = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range=2d&interval=1d", headers={'User-Agent': 'Mozilla/5.0'}, timeout=2).json()
        closes = [c for c in res['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) >= 2:
            current = float(closes[-1])
            diff = current - float(closes[-2])
            diff_pct = (diff / float(closes[-2])) * 100
            res_data.update({"current": current, "diff": diff, "diff_pct": diff_pct})
            return res_data
        elif closes:
            res_data["current"] = float(closes[-1])
    except: pass
    return res_data

def raw_fetch_naver_news(query, display=100, start=1, sort_type="date", cid="", secret=""):
    if not cid or not secret: return []
    queries = [q.strip() for q in query.split('|') if q.strip()]
    all_items = []
    per_query = max(10, display // len(queries)) if queries else display
    for q in queries:
        try:
            res = requests.get("https://naverapihub.apigw.ntruss.com/search/v1/news", headers={"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret}, params={"query": q, "display": per_query, "start": start, "sort": sort_type, "format": "json"}, timeout=3).json()
            for i in res.get("items", []):
                try: dt = parsedate_to_datetime(i['pubDate'])
                except: dt = datetime.now(timezone.utc)
                all_items.append({"title": clean_html(i['title']), "link": i['link'], "summary": clean_html(i['description']), "published": dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"), "raw_date": dt})
        except: pass
    unique = []
    seen = set()
    for item in sorted(all_items, key=lambda x: x['raw_date'], reverse=True):
        if item['link'] not in seen: seen.add(item['link']); unique.append(item)
    return unique[:display]

def raw_calculate_technical_indicators(ticker):
    try:
        df = None
        code_match = re.search(r'\d{6}', ticker)
        if code_match:
            code = code_match.group()
            df = yf.Ticker(f"{code}.KS").history(period="1y")
            if df.empty: df = yf.Ticker(f"{code}.KQ").history(period="1y")
        else:
            df = yf.Ticker(ticker).history(period="1y")
            
        if df is not None and len(df) >= 60:
            cur = float(df['Close'].iloc[-1])
            ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
            ma60 = float(df['Close'].rolling(60).mean().iloc[-1])
            
            high52 = float(df['High'].rolling(252, min_periods=100).max().iloc[-1])
            low52 = float(df['Low'].rolling(252, min_periods=100).min().iloc[-1])
            
            std20 = float(df['Close'].rolling(20).std().iloc[-1])
            bb_upper = ma20 + (std20 * 2)
            bb_lower = ma20 - (std20 * 2)
            
            exp12 = df['Close'].ewm(span=12, adjust=False).mean()
            exp26 = df['Close'].ewm(span=26, adjust=False).mean()
            macd = exp12 - exp26
            signal = macd.ewm(span=9, adjust=False).mean()
            macd_osc = float((macd - signal).iloc[-1])
            
            delta = df['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean().iloc[-1]
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean().iloc[-1]
            rs = gain / loss if loss > 0 else 0
            rsi = 100 - (100 / (1 + rs)) if loss > 0 else 100
            
            return (f"- 20일선/60일선: {ma20:,.0f}원 / {ma60:,.0f}원\n"
                    f"- 52주 최고/최저가: {high52:,.0f}원 / {low52:,.0f}원\n"
                    f"- 볼린저밴드 상단/하단: {bb_upper:,.0f}원 / {bb_lower:,.0f}원\n"
                    f"- MACD 오실레이터: {macd_osc:+.2f} ({'상승🔴' if macd_osc>0 else '하락🔵'})\n"
                    f"- RSI(14): {rsi:.1f} ({'과열🔴' if rsi>=70 else '침체🔵' if rsi<=30 else '중립⚖️'})")
    except: pass
    return "- 기술적 지표 누락"

def raw_fetch_naver_disclosures(ticker):
    try:
        code_match = re.search(r'\d{6}', ticker)
        if not code_match: return "- 국내 종목 아님"
        code = code_match.group()
        res = requests.get(f"https://finance.naver.com/item/news_notice.naver?code={code}&page=1", headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            rows = soup.find_all('tr')
            lines = []
            for tr in rows:
                title_td = tr.find('td', class_='title')
                date_td = tr.find('td', class_='date')
                info_td = tr.find('td', class_='info')
                if title_td and date_td:
                    a_tag = title_td.find('a')
                    href = a_tag.get('href', '').lower() if a_tag else ""
                    if 'notice_read' in href or 'dart' in href:
                        title = a_tag.text.strip()
                        date_str = date_td.text.strip()
                        info_str = info_td.text.strip() if info_td else "공시"
                        lines.append(f"[{date_str}] [{info_str}] {title}")
                        if len(lines) >= 5: break
            if lines: return "\n".join(lines)
            return "최근 주요 공시 없음"
    except: pass
    return "공시 조회 불가"

def raw_fetch_supply_demand_trend(ticker):
    try:
        code_match = re.search(r'\d{6}', ticker)
        if code_match:
            res = requests.get(f"https://finance.naver.com/item/frgn.naver?code={code_match.group()}", headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                rows = soup.select("table.type2 tr[onmouseover]")
                if rows:
                    lines = []
                    for row in rows[:5]:
                        cols = row.find_all('td')
                        if len(cols) >= 7:
                            d_str = cols[0].text.strip()
                            inst_txt = cols[5].text.strip().replace(',', '')
                            frgn_txt = cols[6].text.strip().replace(',', '')
                            inst = int(inst_txt) if inst_txt.lstrip('+-').isdigit() else 0
                            frgn = int(frgn_txt) if frgn_txt.lstrip('+-').isdigit() else 0
                            lines.append(f"[{d_str}] 기관: {inst:+,}주 / 외인: {frgn:+,}주")
                    if lines: return "\n".join(lines)
    except: pass
    return "수급 동향 조회 불가"

# =======================================================
# 💡 [핵심] 4단계 폭포수 우회 및 에러 추적 (3.5 -> 3.0 -> 2.5 -> 3.1 Lite)
# =======================================================
def get_fallback_models(use_lite):
    if use_lite:
        return [('gemini-3.1-flash-lite', 'Gemini 3.1 Flash Lite')]
    return [
        ('gemini-3.5-flash', 'Gemini 3.5 Flash'),
        ('gemini-3.0-flash', 'Gemini 3.0 Flash (Fallback)'),
        ('gemini-2.5-flash', 'Gemini 2.5 Flash (Fallback)'),
        ('gemini-3.1-flash-lite', 'Gemini 3.1 Flash Lite (Fallback)')
    ]

def get_clean_error(e):
    error_str = str(e)
    if "429" in error_str or "quota" in error_str.lower(): return "일일 호출 한도 초과 (429 Quota)"
    if "503" in error_str: return "구글 서버 과부하 (503 Service Unavailable)"
    return (error_str[:50] + '...') if len(error_str) > 50 else error_str

def call_gemini_with_fallback(prompt, is_json=False, use_lite=False):
    client = genai.Client(api_key=GEMINI_API_KEY)
    models = get_fallback_models(use_lite)
    last_error = ""
    
    for idx, (m, base_badge) in enumerate(models):
        try:
            res = client.models.generate_content(model=m, contents=prompt).text
            if not is_json:
                badge_name = base_badge
                if idx > 0: badge_name += f" - ⚠️ 우회 사유: {last_error}"
                res = f"*(🤖 **엔진 식별 프로토콜:** `[💡 {badge_name}]`)*\n\n" + res
            return res
        except Exception as e:
            last_error = get_clean_error(e)
            continue
            
    raise Exception(f"모든 AI 모델 호출 실패. 마지막 오류: {last_error}")

def call_gemini_stream_with_fallback(prompt):
    client = genai.Client(api_key=GEMINI_API_KEY)
    models = get_fallback_models(False)
    last_error = ""
    
    for idx, (m, base_badge) in enumerate(models):
        try:
            response = client.models.generate_content_stream(model=m, contents=prompt)
            badge_name = base_badge
            if idx > 0: badge_name += f" - ⚠️ 우회 사유: {last_error}"
            yield f"*(🤖 **엔진 식별 프로토콜:** `[💡 {badge_name}]`)*\n\n"
            
            for chunk in response:
                if chunk.text: yield chunk.text
            return
        except Exception as e:
            last_error = get_clean_error(e)
            continue
            
    yield f"\n\n🚨 **서버 과부하:** 모든 AI 모델 호출 실패. 마지막 오류: {last_error}"

# =======================================================
# 기존 캐시 및 데이터 연산 유닛
# =======================================================
@st.cache_data(ttl=60)
def get_stock_current_price(ticker): return raw_get_stock_current_price(ticker)

@st.cache_data(ttl=300)
def get_naver_news(query, display=100, start=1, sort_type="date"): 
    return raw_fetch_naver_news(query, display, start, sort_type, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET)

def filter_news_with_gemini_lite(raw_news_list):
    if not raw_news_list: return []
    context_block = "\n".join([f"[{idx}] {n['title']}" for idx, n in enumerate(raw_news_list)])
    prompt = (f"너는 베테랑 헤지펀드 매니저야. 아래 최신 뉴스 제목 50개 목록을 읽고, "
              f"단순 시황 요약이나 자극성 찌라시는 탈락시키고 실적/수주 등 주가에 지대한 영향을 줄 진짜 '알짜 기사'의 인덱스 번호만 파이썬 배열로 출력해라. 예: [0, 3, 15]\n\n{context_block}")
    try:
        res = call_gemini_with_fallback(prompt, is_json=True, use_lite=True)
        matched_indices = json.loads(re.search(r'\[.*\]', res).group())
        filtered_result = [raw_news_list[i] for i in matched_indices if i < len(raw_news_list)]
        if filtered_result: return filtered_result
    except: pass
    return raw_news_list[:12]

def fetch_unique_realtime_news(query):
    unique_news = []
    attempts = 0
    while len(unique_news) < 20 and st.session_state.realtime_start <= 900 and attempts < 4:
        batch = get_naver_news(query, display=10, start=st.session_state.realtime_start, sort_type="date")
        st.session_state.realtime_start += 10; attempts += 1
        if not batch: break
        for n in batch:
            if n['link'] not in st.session_state.seen_realtime: unique_news.append(n); st.session_state.seen_realtime.add(n['link'])
            if len(unique_news) == 20: break
    st.session_state.current_realtime_news = unique_news

def fetch_unique_eco_news(query):
    unique_news = []
    attempts = 0
    while len(unique_news) < 15 and st.session_state.eco_start <= 900 and attempts < 4:
        batch = get_naver_news(query, display=50, start=st.session_state.eco_start, sort_type="date")
        st.session_state.eco_start += 50; attempts += 1
        if not batch: break
        core_batch = filter_news_with_gemini_lite(batch)
        for n in core_batch:
            if n['link'] not in st.session_state.seen_eco: unique_news.append(n); st.session_state.seen_eco.add(n['link'])
            if len(unique_news) == 15: break
    st.session_state.current_eco_news = unique_news

def fetch_unique_sector_news(sector_name, query):
    if sector_name not in st.session_state.sector_starts: st.session_state.sector_starts[sector_name] = 1; st.session_state.seen_sectors[sector_name] = set()
    unique_news = []
    attempts = 0
    while len(unique_news) < 15 and st.session_state.sector_starts[sector_name] <= 900 and attempts < 4:
        batch = get_naver_news(query, display=50, start=st.session_state.sector_starts[sector_name], sort_type="date")
        st.session_state.sector_starts[sector_name] += 50; attempts += 1
        if not batch: break
        core_batch = filter_news_with_gemini_lite(batch)
        for n in core_batch:
            if n['link'] not in st.session_state.seen_sectors[sector_name]: unique_news.append(n); st.session_state.seen_sectors[sector_name].add(n['link'])
            if len(unique_news) == 15: break
    st.session_state.current_sector_news[sector_name] = unique_news

def get_financial_data(ticker):
    try:
        code = re.search(r'\d{6}', ticker)
        if code:
            res = requests.get(f"https://finance.daum.net/api/quotes/A{code.group()}?summary=false", headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.daum.net/'}, timeout=2).json()
            return f"- 시총: {res.get('marketCap', 0)/1e8:,.0f}억 원\n- PER: {res.get('per','N/A')}배\n- PBR: {res.get('pbr','N/A')}배"
        info = yf.Ticker(f"{ticker}.KS" if ".K" not in ticker else ticker).info
        return f"- 시총: {info.get('marketCap',0)/1e12:.2f}조 원\n- PER: {info.get('trailingPE','N/A')}배"
    except: return "재무 정보 데이터 누락"

# =======================================================
# AI 프롬프트 빌더 
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
    combined = "\n".join([f"- {n['title']}" for n in news_list[:20]])
    return (f"당신은 엄격한 헤지펀드 수석 퀀트 애널리스트입니다. 아래 데이터를 바탕으로 정밀 밸류에이션을 집행하십시오.\n\n"
            f"[시장 거시 상황]: {market_data_str}\n"
            f"[예비 후보 5종목 팩트체크 데이터 (현재가 및 등락률 포함)]:\n{candidate_context}\n"
            f"[최신 관련 뉴스 팩트]:\n{combined}\n\n"
            f"위 데이터를 분석하여, '{investment_horizon}' 투자에 부적합한 종목 2개를 먼저 제외하고, 최종 3개만 엄선하여 보고서를 작성하십시오.\n\n"
            f"⚠️ 절대 주의사항 (Chain-of-Thought 수학적 논리 전개) ⚠️\n"
            f"1. 목표가 산출 시, 반드시 본문에 `[현재가 × (동종업계 적정 PER 추정치 ÷ 현재 PER)]` 수식을 텍스트로 적고 직접 계산하여 목표가를 산출하십시오.\n"
            f"2. 매수추천가 산출 시, 반드시 제공된 보조지표 중 `볼린저 밴드 하단` 또는 `장기 이평선`, `52주 최저가` 중 하나를 언급하며 방어적인 진입가를 수식처럼 작성하십시오.\n"
            f"3. 탈락시킨 2개 종목은 아래 '최종 추천 종목' 목록에 절대 중복되면 안 됩니다.\n\n"
            f"[보고서 필수 양식]\n"
            f"### 🗑️ [탈락 종목 2개]\n"
            f"- [탈락 종목명 1, 2]: (고평가, 악재 등 제외한 구체적 이유)\n\n"
            f"### 🏆 [최종 추천 종목 3개]\n"
            f"1. 🥇 추천종목: [종목명] (티커)\n"
            f"- 선정 근거: (뉴스 모멘텀 및 수급 서술)\n"
            f"- 🧮 적정 목표가 산출식: (수식 및 계산 과정 명시)\n"
            f"- 🎯 퀀트 목표가: [수식으로 도출된 가격]\n"
            f"- 🛡️ 진입 타점 연산: (볼린저 밴드 하단 등 수치를 대입하여 설명)\n"
            f"- 💰 정밀 매수 추천가: [연산된 현실적 진입가]\n\n"
            f"(2번, 3번 종목 동일하게 작성)\n\n"
            f"※ 가장 중요: 보고서 맨 마지막 줄에 시스템 추적용 3개의 데이터를 기재하십시오.\n"
            f"[TRACKING_DATA]\n"
            f"종목명1|티커1|목표가숫자만|매수추천가숫자만\n"
            f"종목명2|티커2|목표가숫자만|매수추천가숫자만\n"
            f"종목명3|티커3|목표가숫자만|매수추천가숫자만")

def build_prompt_deep_dive(stock_name, ticker, news_list, is_owned, avg_price, quantity, current_price, market_data_str, tech_str, supply_str):
    fin_data = get_financial_data(ticker)
    status = "미보유 관심종목"
    if is_owned == 1:
        roi = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0
        status = f"보유 중 (평단: {avg_price:,.0f}원, 수량: {quantity}주, 현재가: {current_price:,.0f}원, 수익률: {roi:.2f}%)"
    combined = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list[:30]])
    return (f"[{stock_name} 심층 진단]\n"
            f"[시장 지표]\n{market_data_str}\n"
            f"[내 상태]\n{status}\n"
            f"[최근 5일 수급 동향]\n{supply_str}\n"
            f"[보조지표/기술적 수치]\n{tech_str}\n"
            f"[최신 뉴스]\n{combined}\n"
            f"[재무]\n{fin_data}\n\n"
            f"위 데이터를 바탕으로 아래 항목을 반드시 포함하여 리포트를 작성하십시오.\n"
            f"1. 🏢 재무 및 기업 펀더멘털 분석\n"
            f"2. 🌐 뉴스 및 수급 파급력 종합 분석 (MACD, RSI 과열구간 언급 필수)\n"
            f"3. 📊 포트폴리오 맞춤 진단 및 투자의견\n"
            f"4. 🧮 적정 목표가 산출식: (현재가 * (업종 평균 추정 PER / 현재 PER) 등의 수식 기재)\n"
            f"5. 💰 적정 목표가 및 손절가 (※ 반드시 산출식을 거친 구체적 수치, 손절가는 볼린저 밴드 하단 이탈 가격 명시)\n\n"
            f"마지막줄에 파싱을 위해 'TARGET_PRICE: 목표가숫자만' 을 필수로 적어주세요.")

# =======================================================
# 4. 메인 대시보드 UI
# =======================================================
st.title("📊 Project2_Stock")
market_data = get_market_data()
market_data_str = ", ".join([f"{k}: {v['current']:,.2f}({v['diff_pct']:+.2f}%)" for k, v in market_data.items() if v.get('current', 0) > 0])

cols = st.columns(len(market_data))
for i, (name, data) in enumerate(market_data.items()):
    with cols[i]:
        if data.get('current', 0) > 0: st.metric(label=name, value=f"{data['current']:,.2f}", delta=f"{data['diff']:,.2f} ({data['diff_pct']:.2f}%)")
        else: st.metric(label=name, value="데이터 오류")
st.divider()

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(["📰 실시간 경제·시사", "🔥 핵심 경제 뉴스", "📑 섹터별 분석", "🎯 오늘의 추천종목", "⭐️ 내 관심종목", "📁 스크랩북", "⚙️ 데이터 관리"])

with tab1:
    st.subheader("📰 실시간 경제·시사 뉴스 분석")
    realtime_query = "증시|금융|환율|물가|부동산|정책"
    if not st.session_state.current_realtime_news: fetch_unique_realtime_news(realtime_query)
    if st.button("🤖 실시간 뉴스 TOP 20 기반 종합 분석", type="primary", use_container_width=True):
        st.session_state.realtime_analysis = st.write_stream(call_gemini_stream_with_fallback(build_prompt_realtime(st.session_state.current_realtime_news[:20], market_data_str)))
        st.rerun()
    if st.session_state.realtime_analysis:
        with st.expander("📊 AI 실시간 시황 종합 브리핑", expanded=True):
            st.write(st.session_state.realtime_analysis)
            if st.button("💾 이 리포트 스크랩", key="sc_rt_all"):
                c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date) VALUES (?, ?, ?, ?)", ("📰 실시간 시황 종합 브리핑", "실시간 수집 기반 요약", st.session_state.realtime_analysis, datetime.now().strftime("%Y-%m-%d %H:%M"))); conn.commit(); st.success("저장 완료")
    st.markdown("---")
    for news in st.session_state.current_realtime_news:
        with st.expander(f"🕒 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']}) | {news['published']}\n\n{news['summary']}")
            if st.button("이 기사 심층 분석", key=f"tr_btn_{news['link']}"):
                st.session_state.analysis_results[f"news_{news['link']}"] = {"text": call_gemini_with_fallback(build_prompt_single_news(news['title'], news['summary'], market_data_str)), "time": time.time()}
            if f"news_{news['link']}" in st.session_state.analysis_results:
                st.info(st.session_state.analysis_results[f"news_{news['link']}"]['text'])

with tab2:
    st.subheader("今日 오늘의 핵심 경제 뉴스")
    c.execute("""
        SELECT substr(check_date, 1, 10) as date_day, ROUND(AVG(score), 1) 
        FROM market_score_history 
        GROUP BY date_day 
        ORDER BY date_day DESC LIMIT 15
    """)
    if hist := c.fetchall():
        with st.expander("📈 AI 시장 심리 지수 추이 그래프 (일별 평균)", expanded=False): 
            dates = [r[0][5:] for r in reversed(hist)]
            scores = [r[1] for r in reversed(hist)]
            st.line_chart(dict(zip(dates, scores)))
    
    eco_query = "경제|증시|주식|금리|실적"
    if not st.session_state.current_eco_news: fetch_unique_eco_news(eco_query)
    
    col_e1, col_e2 = st.columns([4, 1])
    with col_e1:
        if st.button("🤖 AI 종합 마켓 브리핑 리포트 생성", type="primary", use_container_width=True):
            res = call_gemini_with_fallback(build_prompt_overall(get_naver_news(eco_query, display=50, sort_type="date"), market_data_str))
            score = int(m.group(1)) if (m := re.search(r'SCORE:\s*(\d+)', res)) else 50
            c.execute("INSERT INTO market_score_history (check_date, score) VALUES (?, ?)", (datetime.now().strftime("%Y-%m-%d %H:%M"), score)); conn.commit()
            st.session_state.overall_analysis = {"text": re.sub(r'SCORE:\s*\d+', '', res).strip(), "score": score}; st.rerun()
    with col_e2:
        if st.button("🔄 다음 기사 보기", key="next_eco_btn", use_container_width=True):
            fetch_unique_eco_news(eco_query); st.rerun()

    if st.session_state.overall_analysis:
        st.markdown(f"**실시간 AI 시장 심리 지수: {st.session_state.overall_analysis['score']}/100**")
        with st.expander("📝 거시 브리핑 리포트", expanded=True): st.write(st.session_state.overall_analysis['text'])
    
    for i, news in enumerate(st.session_state.current_eco_news):
        with st.expander(f"📰 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']}) | {news['published']}")
            st.caption(news['summary'])
            if st.button("이 기사 심층 분석", key=f"t1_btn_{news['link']}"):
                with st.spinner("분석 중..."):
                    st.session_state.analysis_results[f"eco_{news['link']}"] = {"text": call_gemini_with_fallback(build_prompt_single_news(news['title'], news['summary'], market_data_str)), "time": time.time()}
            if f"eco_{news['link']}" in st.session_state.analysis_results:
                st.write(st.session_state.analysis_results[f"eco_{news['link']}"]['text'])

with tab3:
    st.subheader("📑 섹터별 핵심 비즈니스 뉴스")
    sectors = {"반도체": "반도체|삼성전자|SK하이닉스", "2차전지": "2차전지|배터리|양극재", "바이오": "바이오|제약|신약|FDA", "금융/밸류업": "금융|은행|밸류업", "IT/플랫폼": "IT|네이버|카카오"}
    
    col_s1, col_s2, col_s3 = st.columns([2, 1, 1])
    with col_s1: selected_sector = st.selectbox("관심 섹터 선택", list(sectors.keys()))
    if selected_sector not in st.session_state.current_sector_news: fetch_unique_sector_news(selected_sector, sectors[selected_sector])
        
    with col_s2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🤖 이 섹터 종합 리포트", type="primary", use_container_width=True):
            st.session_state[f'sec_sum_{selected_sector}'] = call_gemini_with_fallback(build_prompt_sector(selected_sector, get_naver_news(sectors[selected_sector], display=20, sort_type="date"), market_data_str))
    with col_s3:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🔄 다음 섹터 뉴스 보기", key="next_sec_btn", use_container_width=True):
            fetch_unique_sector_news(selected_sector, sectors[selected_sector]); st.rerun()

    if f'sec_sum_{selected_sector}' in st.session_state:
        with st.info(st.session_state[f'sec_sum_{selected_sector}']): pass

    for i, news in enumerate(st.session_state.current_sector_news.get(selected_sector, [])):
        with st.expander(f"🏭 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']}) | {news['published']}\n\n{news['summary']}")

with tab4:
    st.subheader("🎯 AI 맞춤 추천종목 발굴 (병렬 고속 엔진 + 스트리밍)")
    investment_horizon = st.radio("투자 기간 설정", ["단기 (1~3개월)", "중기 (3~6개월)", "장기 (1년 이상)"], horizontal=True)
    if st.button("🚀 유망 종목 정밀 발굴 가동", type="primary", use_container_width=True):
        raw_rec = get_naver_news("특징주|수주|실적|목표가", display=100, sort_type="date")
        rec_news = filter_news_with_gemini_lite(raw_rec)
        
        res1 = call_gemini_with_fallback(f"다음 뉴스에서 유망 종목 5개를 골라 JSON 배열로 출력하세요. [{{\"name\":\"종목명\",\"ticker\":\"6자리코드\"}}]\n" + "\n".join([n['title'] for n in rec_news]), is_json=True)
        success_rec = False
        try:
            candidates = json.loads(re.search(r'\[.*\]', res1, re.S).group())[:5]
            
            # 💡 [핵심 패치] 5차선 고속도로 병렬 처리로 1분 대기시간 싹쓸이!
            def fetch_candidate_data(c_info):
                t = c_info.get('ticker', '')
                name = c_info.get('name', '')
                p_info = raw_get_stock_current_price(t)
                cp, dpct = p_info["current"], p_info["diff_pct"]
                tech = raw_calculate_technical_indicators(t)
                fin = get_financial_data(t)
                return f"- 종목: {name}({t})\n  현재가: {cp:,.0f}원 (전일대비 {dpct:+.2f}%)\n  보조지표: \n{tech}\n  재무: \n{fin}\n"
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(fetch_candidate_data, candidates))
            
            ctx_str = "".join(results)
            
            # 💡 [핵심 패치] 답답하게 기다릴 필요 없이 실시간 타자(스트리밍) 효과로 전면 교체
            prompt_step3 = build_prompt_recommend_step3(ctx_str, rec_news, market_data_str, investment_horizon)
            st.session_state.today_recommendation = st.write_stream(call_gemini_stream_with_fallback(prompt_step3))
            success_rec = True
            
        except Exception as e: st.error(f"추천 오류 발생: {e}")
        if success_rec: st.rerun()

    if st.session_state.get('today_recommendation'):
        raw = st.session_state.today_recommendation
        display_report = raw.split("[TRACKING_DATA]")[0].strip()
        st.write(display_report)
        
        if "[TRACKING_DATA]" in raw:
            st.markdown("### 📌 AI 분석 추천 매수 밴드 대시보드")
            cols = st.columns(3)
            for idx, line in enumerate(raw.split("[TRACKING_DATA]")[1].strip().split('\n')):
                data = line.split('|')
                if len(data) >= 3:
                    name, tick = data[0].strip(), data[1].strip()
                    try: tp = float(re.sub(r'[^\d.]', '', data[2])) if re.sub(r'[^\d.]', '', data[2]) else 0.0
                    except: tp = 0.0
                    bp = 0.0
                    if len(data) >= 4:
                        try: bp = float(re.sub(r'[^\d.]', '', data[3])) if re.sub(r'[^\d.]', '', data[3]) else 0.0
                        except: pass

                    with cols[idx % 3]:
                        p_info = get_stock_current_price(tick)
                        cp, dpct = p_info["current"], p_info["diff_pct"]
                        st.info(f"**{name}** ({tick})")
                        st.metric("실시간 현재가", f"{cp:,.0f}원", f"전일대비 {dpct:+.2f}%")
                        st.metric("🎯 퀀트 목표가", f"{tp:,.0f}원", f"{((tp - cp)/cp)*100:+.1f}% 여력" if cp > 0 else "")
                        st.metric("💰 정밀 매수 추천가", f"{bp:,.0f}원", f"현재가 대비 {((bp - cp)/cp)*100:+.1f}%" if cp > 0 and bp > 0 else "데이터 없음")
                        
                        if st.button(f"💾 {name} 찜하기", key=f"rec_s_{tick}"):
                            c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, buy_recommend_price, scrap_date) VALUES (?,?,?,?,?,?,?,?)", 
                                      (f"🎯 추천: {name}", display_report, name, tick, cp, tp, bp, datetime.now().strftime("%Y-%m-%d %H:%M")))
                            c.execute("SELECT id FROM portfolio WHERE ticker=?", (tick,))
                            if not c.fetchone():
                                c.execute("INSERT INTO portfolio (stock_name, ticker, search_query) VALUES (?,?,?)", (name, tick, name))
                            conn.commit(); st.success(f"'{name}' 관심종목 연동 및 스크랩 완료!")

# =======================================================
# 💡 [탭 5: 관심종목]
# =======================================================
with tab5:
    st.subheader("⭐️ 내 관심종목 & AI 앙상블 진단")
    with st.form("add_stock"):
        new_s = st.text_input("종목명 입력 (예: 카카오, 삼성전자)")
        st_owned = st.radio("보유상태", ["미보유", "보유중"], horizontal=True)
        c1, c2 = st.columns(2)
        avg_p = c1.text_input("평단가", value="0")
        qty = c2.number_input("수량", min_value=0, value=0)
        
        if st.form_submit_button("➕ 종목 등록") and new_s:
            with st.spinner("정보 분석 중..."):
                res = call_gemini_with_fallback(f"한국주식 '{new_s}'의 야후티커와 검색어 JSON으로 줘. {{'ticker':'', 'query':''}}", is_json=True, use_lite=True)
                success = False
                try:
                    data = json.loads(re.search(r'\{.*\}', res, re.S).group())
                    try: final_avg_p = float(avg_p.replace(',', ''))
                    except: final_avg_p = 0.0
                    c.execute("INSERT INTO portfolio (stock_name, search_query, ticker, is_owned, avg_price, quantity) VALUES (?,?,?,?,?,?)", 
                              (new_s.strip(), data.get('query', new_s), data.get('ticker', ''), 1 if st_owned=="보유중" else 0, final_avg_p, qty))
                    conn.commit()
                    success = True
                except Exception as e: st.error(f"등록 실패: {e}")
                if success: st.rerun() 

    c.execute("SELECT id, stock_name, search_query, ticker, is_owned, avg_price, quantity FROM portfolio")
    portfolio = c.fetchall()
    
    if portfolio:
        port_cache = {}
        tasks_to_run = []
        now_ts = time.time()
        
        for p in portfolio:
            p_id = p[0]
            if p_id in st.session_state.port_data_cache and (now_ts - st.session_state.port_data_cache[p_id]['time'] < 60):
                port_cache[p_id] = st.session_state.port_data_cache[p_id]['data']
            else:
                tasks_to_run.append((p, st.session_state.port_starts.get(p_id, 1), NAVER_CLIENT_ID, NAVER_CLIENT_SECRET))

        if tasks_to_run:
            with st.spinner("⚡ 퀀트 레이더 가동: 실시간 주가 및 뉴스 스크래핑 중..."):
                def fetch_stock_raw_worker(p_tuple):
                    p, start_idx, cid, sec = p_tuple
                    p_id, name, query, ticker, owned, avg, qnt = p
                    
                    p_info = raw_get_stock_current_price(ticker or name)
                    tech = raw_calculate_technical_indicators(ticker or name)
                    supply = raw_fetch_supply_demand_trend(ticker or name)
                    dart = raw_fetch_naver_disclosures(ticker or name) 
                    
                    broad = "|".join([k.strip() for k in (query or name).split(" OR ")])
                    raw_news = raw_fetch_naver_news(broad, display=50, start=start_idx, sort_type="date", cid=cid, secret=sec)
                    fact_news = filter_news_with_gemini_lite(raw_news)
                    return p_id, p_info, fact_news[:10], raw_news, tech, supply, dart

                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    for r in executor.map(fetch_stock_raw_worker, tasks_to_run): 
                        port_cache[r[0]] = r
                        st.session_state.port_data_cache[r[0]] = {'data': r, 'time': now_ts}

        @st.fragment
        def render_stock_box(p, p_data):
            p_id, name, query, ticker, is_owned, avg_price, quantity = p
            p_info, fact_news, raw_news, tech_str, supply_str, dart_str = p_data[1], p_data[2], p_data[3], p_data[4], p_data[5], p_data[6]
            cur_price = p_info["current"]
            cur_diff_pct = p_info["diff_pct"]

            st.markdown(f"### 📌 [{name}]")
            c_m1, c_m2 = st.columns(2)
            with c_m1:
                st.caption("📈 **기술적 수치 지표 (RSI/MACD/이평선/볼린저)**")
                st.code(tech_str, language="text")
            with c_m2:
                st.caption("👥 **최근 5일 외국인/기관 매매 동향**")
                st.code(supply_str, language="text")
                
            col_info, col_btn = st.columns([3, 1])
            with col_info:
                if is_owned:
                    roi = ((cur_price - avg_price)/avg_price)*100 if avg_price > 0 else 0.0
                    st.caption(f"💼 **보유** | 평단:{avg_price:,.0f} | 수량:{quantity} | 현재:{cur_price:,.0f}원 ({cur_diff_pct:+.2f}%) | 수익률: {'🔴' if roi>0 else '🔵'} {roi:.2f}%")
                else: st.caption(f"👀 **관심** | 현재가: {cur_price:,.0f}원 ({cur_diff_pct:+.2f}%)")
            
            with col_btn:
                cache_key = f"deep_{p_id}"
                if cache_key in st.session_state.analysis_results:
                    if st.button("📊 저장된 진단 보기", key=f"view_{p_id}", type="primary"): st.session_state[f"show_{p_id}"] = True
                else:
                    if st.button("🚀 AI 앙상블 진단", key=f"run_{p_id}", type="primary"):
                        combined = {n['link']: n for n in (fact_news + st.session_state.get(f"ai_news_{p_id}", []))}.values()
                        report = call_gemini_with_fallback(build_prompt_deep_dive(name, ticker, list(combined), is_owned, avg_price, quantity, cur_price, market_data_str, tech_str, supply_str))
                        st.session_state.analysis_results[cache_key] = {"text": report, "time": time.time()}; st.session_state[f"show_{p_id}"] = True; st.rerun()

            if st.session_state.get(f"show_{p_id}"):
                with st.expander("📝 AI 종합 진단 리포트", expanded=True):
                    rep = st.session_state.analysis_results[cache_key]['text']
                    st.write(re.sub(r'TARGET_PRICE:\s*[\d,]+', '', rep).strip())
                    tp = float(m.group(1).replace(',','')) if (m := re.search(r'TARGET_PRICE:\s*([\d,]+)', rep)) else 0.0
                    c1, c2 = st.columns(2)
                    if c1.button("💾 스크랩 저장", key=f"save_{p_id}"):
                        c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?,?,?,?,?,?,?,?)", (f"[{name}] 리포트", "심층 퀀트 진단", rep, datetime.now().strftime("%Y-%m-%d %H:%M"), name, ticker, cur_price, tp)); conn.commit(); st.success("저장 완료")
                    if c2.button("🔄 재분석", key=f"force_{p_id}"): del st.session_state.analysis_results[cache_key]; st.rerun()

            with st.expander("🏢 네이버 전자공시 최근 5회 현황", expanded=False):
                st.text_area(label="최신 전자공시 스트리밍", value=dart_str, height=140, disabled=True, label_visibility="collapsed", key=f"dart_ta_{p_id}")
                if "•" in dart_str or "[" in dart_str:
                    if st.button("🤖 공시 AI 원포인트 요약", key=f"dart_ai_{p_id}"):
                        st.session_state.analysis_results[f"dart_res_{p_id}"] = call_gemini_with_fallback(f"[{name}] 공시 요약 요청:\n{dart_str}")
                if f"dart_res_{p_id}" in st.session_state.analysis_results: 
                    st.info(st.session_state.analysis_results[f"dart_res_{p_id}"])

            with st.expander(f"📰 관련 최신 뉴스 ({len(fact_news)}건)", expanded=False):
                for n in fact_news: st.markdown(f"**[{n['title']}]({n['link']})** ({n['published']})")
            
            col_edit1, col_edit2 = st.columns([1, 1])
            with col_edit1:
                with st.expander("⚙️ 투자 상태 변경"):
                    with st.form(key=f"edit_{p_id}"):
                        new_own = st.radio("보유", ["미보유", "보유중"], index=1 if is_owned else 0)
                        na_p = st.text_input("평단", value=f"{int(avg_price)}")
                        nq = st.number_input("수량", min_value=0, value=int(quantity))
                        if st.form_submit_button("적용"):
                            fp = float(na_p.replace(',', '')) if new_own=="보유중" else 0.0
                            c.execute("UPDATE portfolio SET is_owned=?, avg_price=?, quantity=? WHERE id=?", (1 if new_own=="보유중" else 0, fp, int(nq) if new_own=="보유중" else 0, p_id)); conn.commit(); st.rerun()
            with col_edit2:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("🗑️ 관심종목 삭제", key=f"del_{p_id}", use_container_width=True): 
                    c.execute("DELETE FROM portfolio WHERE id=?", (p_id,))
                    conn.commit()
                    if p_id in st.session_state.port_data_cache:
                        del st.session_state.port_data_cache[p_id]
                    st.rerun()
            st.divider()

        for p in portfolio:
            if p[0] in port_cache: render_stock_box(p, port_cache[p[0]])

# ----------------- [탭 6: 스크랩북 및 탭 7: 백업] -----------------
with tab6:
    st.subheader("📁 내 스크랩북 & AI 예측 트래킹")
    c.execute("SELECT id, title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price, buy_recommend_price FROM scrapbook ORDER BY id DESC")
    scraps = c.fetchall()
    if scraps:
        for s in scraps:
            with st.expander(f"[{s[5]}] {s[1]}"):
                if s[6] and s[9] > 0:
                    p_info = get_stock_current_price(s[7] or s[6])
                    cur = p_info["current"]
                    cur_diff_pct = p_info["diff_pct"]
                    
                    cols_sc = st.columns(4)
                    cols_sc[0].metric("저장가(당시주가)", f"{s[8]:,.0f}원")
                    return_pct = ((cur - s[8]) / s[8]) * 100 if s[8] > 0 else 0.0
                    cols_sc[1].metric("실시간 주가", f"{cur:,.0f}원", f"일일 {cur_diff_pct:+.2f}% / 누적 {return_pct:+.2f}%")
                    cols_sc[2].metric("🎯 퀀트 목표가", f"{s[9]:,.0f}원", f"{(cur / s[9]) * 100 if s[9] > 0 else 0.0:.1f}% 달성")
                    
                    b_rec = s[10] if len(s) > 10 and s[10] else 0.0
                    if b_rec > 0:
                        cols_sc[3].metric("💰 정밀 매수 추천가", f"{b_rec:,.0f}원", f"진입대비 {((cur - b_rec)/b_rec)*100:+.1f}%" if cur > 0 else "")
                    else:
                        cols_sc[3].metric("💰 정밀 매수 추천가", "기록 없음")
                    st.divider()
                st.write(s[4])
                if st.button("🗑️ 삭제", key=f"sd_{s[0]}"): c.execute("DELETE FROM scrapbook WHERE id=?", (s[0],)); conn.commit(); st.rerun()

with tab7:
    st.subheader("⚙️ 데이터 관리")
    c.execute("SELECT COUNT(*) FROM oauth_creds")
    if not c.fetchone()[0] > 0:
        flow = Flow.from_client_config(json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"]), scopes=SCOPES, redirect_uri=st.secrets["REDIRECT_URI"])
        url, state = flow.authorization_url(prompt='consent')
        c.execute("DELETE FROM oauth_store"); c.execute("INSERT INTO oauth_store VALUES (?,?)", (state, flow.code_verifier)); conn.commit()
        st.link_button("👉 구글 드라이브 연동 로그인", url)
    else:
        st.success("✅ 클라우드 연결 완료")
        c.execute("SELECT * FROM portfolio"); p_all = c.fetchall()
        c.execute("SELECT * FROM scrapbook"); s_all = c.fetchall()
        json_data = json.dumps({"portfolio": p_all, "scrapbook": s_all}, ensure_ascii=False)
        if st.button("🚀 지금 구글 드라이브 백업"):
            try: upload_to_google_drive(json_data); st.success("백업 성공")
            except Exception as e: st.error(f"실패: {e}")
        if st.button("🔄 최신 백업 복구"):
            try:
                b, name = download_latest_from_google_drive(); db = json.loads(b.decode('utf-8'))
                c.execute("DELETE FROM portfolio"); c.execute("DELETE FROM scrapbook")
                for p in db['portfolio']: c.execute("INSERT INTO portfolio VALUES (" + ",".join(["?"]*len(p)) + ")", p)
                for s in db['scrapbook']: c.execute("INSERT INTO scrapbook VALUES (" + ",".join(["?"]*len(s)) + ")", s)
                conn.commit(); st.success(f"복구 완료: {name}"); st.rerun()
            except Exception as e: st.error(f"실패: {e}")
