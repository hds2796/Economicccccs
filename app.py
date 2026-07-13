import streamlit as st
import requests
import re
import sqlite3
import json
import os
import io
import yfinance as yf
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

# --- [데이터베이스 설정] ---
conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS scrapbook 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, link TEXT, summary TEXT, analysis TEXT, scrap_date TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS portfolio 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, stock_name TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_store (state TEXT, verifier TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS oauth_creds (creds TEXT)''')
conn.commit()

# 기존 포트폴리오 테이블에 신규 컬럼 강제 추가 (스키마 업데이트)
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
if 'today_recommendation' not in st.session_state: st.session_state.today_recommendation = None

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
            data = yf.Ticker(ticker).history(period="2d")
            if len(data) >= 2:
                prev_close = data['Close'].iloc[0]
                current = data['Close'].iloc[1]
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
            # HTML 스크래핑 대신 모바일 전용 JSON API 호출 (UI 변경에 강건함)
            code = code_match.group()
            url = f"https://m.stock.naver.com/api/stock/{code}/basic"
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(url, headers=headers, timeout=3)
            if res.status_code == 200:
                data = res.json()
                return float(data.get('closePrice', '0').replace(',', ''))
                
        data = yf.Ticker(ticker).history(period="1d")
        if not data.empty:
            return float(data['Close'].iloc[-1])
    except: pass
    return 0.0

def clean_html(raw_html):
    if not raw_html: return ""
    return BeautifulSoup(raw_html, "html.parser").get_text()

@st.cache_data(ttl=300)
def get_naver_news(query, display=10, start=1):
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET: return []
    url = "https://naverapihub.apigw.ntruss.com/search/v1/news"
    headers = {"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET}
    params = {"query": query, "display": display, "start": start, "sort": "sim", "format": "json"}
    
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        return [{"title": clean_html(i['title']), "link": i['link'], "summary": clean_html(i['description']), "published": i['pubDate']} for i in response.json().get("items", [])]
    return []

def is_within_7_days(pub_date_str):
    try:
        dt = parsedate_to_datetime(pub_date_str)
        now = datetime.now(timezone.utc)
        return (now - dt) <= timedelta(days=7)
    except Exception:
        return True

def fetch_unique_eco_news(query):
    unique_news = []
    attempts = 0
    while len(unique_news) < 10 and st.session_state.eco_start <= 900 and attempts < 3:
        batch = get_naver_news(query, display=10, start=st.session_state.eco_start)
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
        batch = get_naver_news(query, display=10, start=1)
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
        batch = get_naver_news(query, display=30, start=st.session_state.sector_starts[sector_name])
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
        batch = get_naver_news(query, display=30, start=1)
        st.session_state.sector_starts[sector_name] = 31
        for n in (batch or []):
            if any(b_kw in n['title'] or b_kw in n['summary'] for b_kw in business_kws):
                unique_news.append(n)
                st.session_state.seen_sectors[sector_name].add(n['link'])
            if len(unique_news) == 10: break
            
    st.session_state.current_sector_news[sector_name] = unique_news

# --- [제미나이 AI 분석 함수 및 예외 처리(Fallback) 로직] ---
def call_gemini_with_fallback(prompt, is_json=False):
    if not GEMINI_API_KEY: raise Exception("Gemini API 키 오류")
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    try:
        return client.models.generate_content(model='gemini-3.5-flash', contents=prompt).text
    except Exception as e1:
        if "429" in str(e1) or "RESOURCE_EXHAUSTED" in str(e1) or "quota" in str(e1).lower() or "not found" in str(e1).lower() or "404" in str(e1):
            try:
                res = client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text
                if not is_json: res += "\n\n*(💡 3.5 모델 오류/한도로 인해 2.5-flash가 우회 적용되었습니다.)*"
                return res
            except Exception as e2:
                if "429" in str(e2) or "RESOURCE_EXHAUSTED" in str(e2) or "quota" in str(e2).lower() or "not found" in str(e2).lower() or "404" in str(e2):
                    try:
                        res = client.models.generate_content(model='gemini-1.5-flash', contents=prompt).text
                        if not is_json: res += "\n\n*(💡 3.5 및 2.5 모델 한도 초과로 1.5-flash가 최종 우회 적용되었습니다.)*"
                        return res
                    except Exception as e3:
                        raise Exception(f"최종 우회 모델(1.5) 호출 실패: {e3}")
                raise e2
        raise e1

def get_financial_data(ticker):
    fin_data = "재무 데이터 조회 불가 (통신 오류 또는 티커 누락)"
    if not ticker: return fin_data
    
    try:
        code_match = re.search(r'\d{6}', ticker)
        if code_match:
            # HTML 스크래핑(BeautifulSoup) 제거 및 JSON API 직접 통신 적용
            code = code_match.group()
            url = f"https://m.stock.naver.com/api/stock/{code}/basic"
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(url, headers=headers, timeout=3)
            
            if res.status_code == 200:
                data = res.json()
                market_sum = data.get('marketValue', 'N/A')
                per = data.get('per', 'N/A')
                pbr = data.get('pbr', 'N/A')
                
                fin_data = (f"- 시가총액: {market_sum}억 원\n"
                            f"- PER (주가수익비율): {per}배\n"
                            f"- PBR (주가순자산비율): {pbr}배")
        else:
            info = yf.Ticker(ticker).info
            market_cap = info.get('marketCap', 0)
            market_cap_str = f"{market_cap / 1_000_000_000_000:.1f}조" if market_cap else "N/A"
            fin_data = (f"- 시가총액: {market_cap_str}\n"
                        f"- PER: {info.get('trailingPE', 'N/A')}\n"
                        f"- PBR: {info.get('priceToBook', 'N/A')}")
    except Exception:
        pass
    return fin_data

def analyze_single_news(title, summary, market_data_str):
    prompt = (f"아래 뉴스가 주식 시장에 미칠 영향을 분석하십시오.\n"
              f"[현재 실시간 시장 지표]: {market_data_str}\n"
              f"[제목]: {title}\n[요약]: {summary}\n"
              f"위 실시간 시장 지표(지수, 환율 등)의 흐름과 뉴스를 연관 지어 다음을 객관적으로 작성하십시오.\n"
              f"1. 💡 사건 핵심 요약\n2. 📈 시장 파급력 및 현재 지표와의 연관성\n3. 🎯 연관 섹터")
    try: return call_gemini_with_fallback(prompt)
    except Exception as e: return f"분석 오류: {e}"

def analyze_overall_market(news_list, market_data_str):
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    prompt = (f"다음 수집된 {len(news_list)}개의 주요 뉴스와 현재 시장 지표를 종합하여 증시 방향성을 객관적으로 브리핑하십시오.\n"
              f"[현재 실시간 시장 지표]: {market_data_str}\n\n"
              f"{combined_news}\n\n[양식]\n"
              f"1. 🌐 거시 환경 종합 요약 (현재 지수 및 환율 흐름 반영)\n"
              f"2. ⚖️ 증시 호악재 분석\n"
              f"3. 💡 주목할 섹터\n\n"
              f"반드시 마지막 줄에 'SCORE: 숫자' 형태로 시장 심리 지수를 0~100 사이로 기재하십시오.")
    try:
        text = call_gemini_with_fallback(prompt)
        match = re.search(r'SCORE:\s*(\d+)', text)
        score = int(match.group(1)) if match else 50
        return re.sub(r'SCORE:\s*\d+', '', text).strip(), score
    except Exception as e: return f"분석 오류: {e}", 50

def analyze_sector_news(sector_name, news_list, market_data_str):
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    prompt = (f"다음 수집된 '{sector_name}' 섹터 관련 최신 주요 뉴스와 실시간 시장 지표를 종합하여 분석하십시오.\n"
              f"[현재 실시간 시장 지표]: {market_data_str}\n\n"
              f"{combined_news}\n\n[양식]\n"
              f"1. 🏭 섹터 전반적 흐름 요약 (시장 지수와 연계)\n"
              f"2. 📈 주요 호재 및 악재 요인\n"
              f"3. 🎯 투자 심리 및 단기 전망")
    try: return call_gemini_with_fallback(prompt)
    except Exception as e: return f"분석 오류: {e}"

def analyze_recommended_stocks(news_list, market_data_str):
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list[:30]])
    prompt = (f"다음은 최근 실적 개선, 목표가 상향, 대규모 수주 등과 관련된 시장 핵심 뉴스 30건과 실시간 지표입니다.\n"
              f"[현재 실시간 시장 지표]: {market_data_str}\n\n"
              f"{combined_news}\n\n"
              f"위 뉴스와 실시간 지표를 바탕으로 단기적으로 가장 유망해 보이는 '추천종목 3개'를 선정하십시오.\n\n"
              f"[양식]\n"
              f"1. 🥇 추천종목 1: [종목명]\n"
              f"- 선정 근거: (뉴스와 현재 지수 흐름을 바탕으로 객관적 작성)\n"
              f"- 투자 전략: (진입 시점 및 단기 목표가 등)\n\n"
              f"2. 🥈 추천종목 2: [종목명]\n"
              f"- 선정 근거: ...\n"
              f"- 투자 전략: ...\n\n"
              f"3. 🥉 추천종목 3: [종목명]\n"
              f"- 선정 근거: ...\n"
              f"- 투자 전략: ...")
    try: return call_gemini_with_fallback(prompt)
    except Exception as e: return f"분석 오류: {e}"

def analyze_deep_dive(stock_name, ticker, news_list, is_owned, avg_price, quantity, current_price, market_data_str):
    fin_data = get_financial_data(ticker)
        
    user_portfolio_status = "미보유 관심종목 (관망 중)"
    if is_owned == 1:
        roi = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0
        user_portfolio_status = (f"실제 보유 중 (매수단가: {avg_price:,.0f}원, "
                                 f"수량: {quantity}주, 실시간 현재가: {current_price:,.0f}원, "
                                 f"현재 수익률: {roi:.2f}%)")
        
    top_30_news = news_list[:30]
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in top_30_news])
        
    prompt = (f"[{stock_name} 심층 분석 리포트]\n\n"
              f"[현재 실시간 시장 지표]\n{market_data_str}\n\n"
              f"[사용자 포트폴리오 상태]\n- {user_portfolio_status}\n\n"
              f"[최신 핵심 뉴스 TOP {len(top_30_news)}]\n{combined_news}\n\n"
              f"[현재 재무 상태]\n{fin_data}\n\n"
              f"위 데이터를 모두 종합하여 다음 양식으로 브리핑을 작성하십시오. 실시간 거시 지표와 개별 종목의 현재가를 반드시 연계하여 해석하십시오.\n"
              f"1. 🏢 기업 펀더멘털 및 재무 요약\n"
              f"2. 🌐 최신 뉴스 및 거시 지표(환율/지수 등) 파급력 종합 분석\n"
              f"3. 📊 사용자 맞춤형 포트폴리오 진단 (사용자의 매수 단가, 수량, 현재 수익률을 구체적으로 언급하며 진단)\n"
              f"4. 🎯 최종 투자의견 (매수/보유/매도 중 택 1) 및 객관적 근거 제시\n"
              f"5. 💰 적정 목표가 및 손절가 (현재가 대비 객관적 산출 근거를 포함하여 구체적인 가격 제시)")
    try: return call_gemini_with_fallback(prompt)
    except Exception as e: return f"분석 오류: {e}"

# =======================================================
# 4. 상단 대시보드 및 UI 구성
# =======================================================
st.title("📊 Project2_Stock")
market_data = get_market_data()

# AI 프롬프트 주입용 실시간 시장 지표 문자열 생성
market_data_str = ", ".join([f"{k}: {v['current']:,.2f}({v['diff_pct']:+.2f}%)" for k, v in market_data.items() if v.get('current', 0) > 0])

cols = st.columns(len(market_data))
for i, (name, data) in enumerate(market_data.items()):
    with cols[i]:
        if data.get('current', 0) > 0:
            st.metric(label=name, value=f"{data['current']:,.2f}", delta=f"{data['diff']:,.2f} ({data['diff_pct']:.2f}%)")
        else: st.metric(label=name, value="데이터 오류")
st.divider()

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["🔥 경제 뉴스 & 시장 심리", "📑 섹터별 분석", "🎯 오늘의 추천종목", "⭐️ 내 관심종목", "📁 스크랩북", "⚙️ 데이터 백업/복구"])

# [탭 1: 경제 뉴스]
with tab1:
    st.subheader("오늘의 핵심 경제 뉴스")
    eco_query = "경제|증시|주식|금융|코스피|코스닥"
    
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
            with st.spinner("최근 50개의 핵심 뉴스를 백그라운드에서 수집 및 정밀 분석 중입니다..."):
                top_50_news = get_naver_news(eco_query, display=50, start=1)
                analysis_text, score = analyze_overall_market(top_50_news, market_data_str)
                st.session_state.overall_analysis = {"text": analysis_text, "score": score}
                
        if st.session_state.overall_analysis:
            score = st.session_state.overall_analysis['score']
            
            if score >= 80: sentiment_label = "매우 강세 🔥"
            elif score >= 60: sentiment_label = "강세 📈"
            elif score >= 40: sentiment_label = "중립 ⚖️"
            elif score >= 20: sentiment_label = "약세 📉"
            else: sentiment_label = "매우 약세 ❄️"
            
            st.markdown(f"**현재 AI 시장 심리 지수: {score} / 100 ({sentiment_label})**")
            st.progress(score / 100.0)
            
            # 아코디언 UI 적용
            with st.expander("📝 AI 거시 환경 브리핑 전체 보기", expanded=True):
                st.markdown(st.session_state.overall_analysis['text'])
        
        st.markdown("---")
        
        recent_eco_news = [n for n in st.session_state.current_eco_news if is_within_7_days(n['published'])]
        if recent_eco_news:
            for i, news in enumerate(recent_eco_news):
                st.markdown(f"**{i+1}. [{news['title']}]({news['link']})**")
                st.caption(f"{news['published']} | {news['summary']}")
                if st.button("이 기사 심층 분석", key=f"t1_btn_{news['link']}"):
                    st.session_state.analysis_results[news['link']] = analyze_single_news(news['title'], news['summary'], market_data_str)
                
                if news['link'] in st.session_state.analysis_results:
                    with st.expander("🤖 AI 뉴스 분석 결과", expanded=True):
                        st.write(st.session_state.analysis_results[news['link']])
                        if st.button("💾 이 리포트 스크랩하기", key=f"t1_scrap_{news['link']}"):
                            c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)",
                                      (news['title'], news['link'], news['summary'], st.session_state.analysis_results[news['link']], datetime.now().strftime("%Y-%m-%d %H:%M")))
                            conn.commit()
                            st.success("스크랩북 저장 완료")
                st.divider()
        else:
            st.info("최근 7일 이내에 보도된 뉴스가 없습니다.")

# [탭 2: 섹터별 분석]
with tab2:
    sectors = {
        "반도체": "반도체|삼성전자|SK하이닉스", 
        "2차전지": "2차전지|전기차|배터리", 
        "바이오": "바이오|제약|신약", 
        "금융/밸류업": "금융|은행|밸류업", 
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
            with st.spinner(f"{selected_sector} 섹터 동향을 분석 중입니다..."):
                top_20_news = get_naver_news(sectors[selected_sector], display=20, start=1)
                st.session_state[f'sector_summary_{selected_sector}'] = analyze_sector_news(selected_sector, top_20_news, market_data_str)
                
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
                        st.session_state.analysis_results[news['link']] = analyze_single_news(news['title'], news['summary'], market_data_str)
                    
                    if news['link'] in st.session_state.analysis_results:
                        with st.expander("🤖 AI 뉴스 분석 결과", expanded=True):
                            st.write(st.session_state.analysis_results[news['link']])
                            if st.button("💾 스크랩", key=f"t2_scrap_{news['link']}"):
                                c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)",
                                          (news['title'], news['link'], news['summary'], st.session_state.analysis_results[news['link']], datetime.now().strftime("%Y-%m-%d %H:%M")))
                                conn.commit()
                                st.success("저장 완료")
        else:
            st.info("최근 7일 이내에 보도된 관련 섹터 뉴스가 없습니다.")

# [탭 3: 오늘의 추천종목]
with tab3:
    st.subheader("🎯 AI 오늘의 추천종목 발굴")
    st.write("실적 개선, 목표가 상향, 대규모 수주 등의 핵심 키워드를 바탕으로 시장 최신 뉴스를 분석하여 가장 유망한 종목 3가지를 추천합니다.")
    
    if st.button("🚀 오늘의 추천종목 발굴 실행", type="primary", use_container_width=True):
        with st.spinner("유망 종목 관련 최신 뉴스를 수집 및 분석 중입니다..."):
            # 쿼리 범위를 넓혀 빈 결과 방지
            rec_query = "특징주|목표가|수주|흑자|실적"
            rec_news = get_naver_news(rec_query, display=50, start=1)
            recent_rec_news = [n for n in rec_news if is_within_7_days(n['published'])]
            
            # 폴백(Fallback): 그래도 없으면 매우 넓은 범위로 재검색
            if not recent_rec_news:
                fallback_rec_news = get_naver_news("주식 추천|특징주", display=50, start=1)
                recent_rec_news = [n for n in fallback_rec_news if is_within_7_days(n['published'])]
            
            if recent_rec_news:
                st.session_state.today_recommendation = analyze_recommended_stocks(recent_rec_news, market_data_str)
            else:
                st.warning("분석할 만한 최신 유망 뉴스가 부족합니다.")
    
    if st.session_state.get('today_recommendation'):
        with st.expander("🎯 AI 추천종목 리포트 보기", expanded=True):
            st.write(st.session_state.today_recommendation)
            if st.button("💾 추천종목 리포트 스크랩", key="scrap_rec"):
                c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)",
                          ("🎯 오늘의 AI 추천종목", "", "실적/수주/목표가 상향 기반 추천", st.session_state.today_recommendation, datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit()
                st.success("저장 완료")

# [탭 4: 관심종목 및 포트폴리오 관리]
with tab4:
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
            
        submitted = st.form_submit_button("➕ 종목 등록")
        
        if submitted and new_stock.strip():
            with st.spinner(f"AI가 '{new_stock.strip()}'의 종목 코드와 연관 검색어를 분석 중입니다..."):
                prompt = f"""사용자가 한국 주식 '{new_stock.strip()}'을 관심종목에 추가했습니다.
                1. 야후 파이낸스 티커: 코스피는 '6자리숫자.KS', 코스닥은 '6자리숫자.KQ'. (모르면 빈 문자열 "")
                2. 검색어: 뉴스 검색 시 유용한 핵심 계열사, 지주사, 자회사, 대표 브랜드, 영문명 등 종목과 관련된 폭넓은 유의어 포함 (예: {new_stock.strip()} OR 영문명 OR 지주사명 OR 주요자회사)
                반드시 아래 JSON 형식으로만 답변하세요.
                {{"ticker": "005930.KS", "search_query": "{new_stock.strip()} OR 유의어"}}"""
                
                ticker = ""
                search_query = new_stock.strip()
                try:
                    res = call_gemini_with_fallback(prompt, is_json=True)
                    match = re.search(r'\{.*\}', res, re.DOTALL)
                    if match:
                        data = json.loads(match.group(0))
                        ticker = data.get("ticker", "")
                        search_query = data.get("search_query", new_stock.strip())
                except Exception as e:
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
        
        for p_id, p_name, p_query, p_ticker, p_is_owned, p_avg_price, p_quantity in portfolio:
            st.markdown("---")
            
            col_title, col_refresh, col_deep = st.columns([3, 1, 2])
            with col_title:
                st.markdown(f"#### 📌 [{p_name}]")
                
            start_idx = st.session_state.port_starts.get(p_id, 1)
            with col_refresh:
                if st.button("🔄 새 뉴스 보기", key=f"ref_port_{p_id}", use_container_width=True):
                    st.session_state.port_starts[p_id] = start_idx + 50
                    st.rerun()
            
            current_price = get_stock_current_price(p_ticker)
            
            search_keywords = [k.strip() for k in (p_query or p_name).split(" OR ")]
            broad_query = "|".join(search_keywords)
            raw_news = get_naver_news(broad_query, display=50, start=start_idx) 
            
            if not raw_news:
                st.session_state.port_starts[p_id] = 1
                raw_news = get_naver_news(broad_query, display=50, start=1)
            
            raw_news = [n for n in raw_news if is_within_7_days(n['published'])]
            
            business_kws = ["주가", "실적", "목표가", "수주", "배당", "합병", "투자", "인수", "매출", "영업이익", "전망", "동향", "계약", "신제품", "개발", "수출", "공급", "M&A", "규제"]
            port_news_all = [n for n in raw_news if any(b_kw in n['title'] or b_kw in n['summary'] for b_kw in business_kws)]
            
            is_ai_picked = False
            
            if not port_news_all and raw_news:
                try:
                    prompt = f"다음 뉴스 목록에서 주식 투자자 관점으로 가장 의미 있는 뉴스 최대 10개의 인덱스를 JSON 배열(예: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]) 형태로만 출력하십시오.\n"
                    for idx, n in enumerate(raw_news[:50]): 
                        prompt += f"[{idx}] {n['title']} : {n['summary']}\n"
                    
                    res = call_gemini_with_fallback(prompt, is_json=True)
                    match = re.search(r'\[.*?\]', res, re.DOTALL)
                    if match:
                        indices = json.loads(match.group(0))
                        port_news_all = [raw_news[i] for i in indices if i < len(raw_news)]
                        is_ai_picked = True
                except Exception:
                    port_news_all = raw_news[:10]
                    
            if not port_news_all:
                raw_news_fallback = get_naver_news(p_name, display=50, start=start_idx)
                port_news_all = [n for n in raw_news_fallback if is_within_7_days(n['published'])][:10]
            
            with col_deep:
                if st.button("📊 포트폴리오 심층 진단 (TOP 30)", type="primary", key=f"t3_deep_{p_id}"):
                    with st.spinner("실시간 재무 데이터 스크래핑 및 투자 의견 생성 중..."):
                        st.session_state.analysis_results[f"deep_{p_id}"] = analyze_deep_dive(
                            p_name, p_ticker, port_news_all, p_is_owned, p_avg_price, p_quantity, current_price, market_data_str
                        )

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
                        
            if f"deep_{p_id}" in st.session_state.analysis_results:
                with st.expander("📊 AI 포트폴리오 심층 진단 결과", expanded=True):
                    st.write(st.session_state.analysis_results[f"deep_{p_id}"])
                    if st.button("💾 이 리포트 스크랩", key=f"t3_scrap_deep_{p_id}"):
                        c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)",
                                  (f"[{p_name}] 포트폴리오 심층 진단", "", "TOP 30 뉴스 및 실시간 재무 분석 기반", st.session_state.analysis_results[f"deep_{p_id}"], datetime.now().strftime("%Y-%m-%d %H:%M")))
                        conn.commit()
                        st.success("저장 완료")
            
            if is_ai_picked:
                st.caption("✨ 직접적인 비즈니스 키워드가 포함된 뉴스가 없어 AI가 선별한 최근 7일 내 주요 뉴스입니다.")
                
            if port_news_all:
                for i, news in enumerate(port_news_all[:10]):
                    with st.expander(f"📰 {news['title']}"):
                        st.caption(news['published'])
                        st.write(news['summary'])
                        if st.button("이 개별 뉴스 분석", key=f"t3_btn_{p_id}_{i}"):
                            st.session_state.analysis_results[news['link']] = analyze_single_news(news['title'], news['summary'], market_data_str)
                        
                        if news['link'] in st.session_state.analysis_results:
                            with st.expander("🤖 AI 뉴스 분석 결과", expanded=True):
                                st.write(st.session_state.analysis_results[news['link']])
            else:
                st.info(f"'{p_name}' 관련 최근 7일 이내 뉴스가 없습니다. (새 뉴스 보기 버튼을 눌러보십시오.)")
    else: st.info("등록된 관심종목이 없습니다.")

# [탭 5: 스크랩북]
with tab5:
    st.subheader("📁 내 스크랩북 (저장된 리포트)")
    c.execute("SELECT id, title, link, summary, analysis, scrap_date FROM scrapbook ORDER BY id DESC")
    scraps = c.fetchall()
    for s_id, s_title, s_link, s_summary, s_analysis, s_date in scraps:
        with st.expander(f"[{s_date}] {s_title}"):
            if s_link: st.markdown(f"[기사 링크]({s_link})\n\n**요약:** {s_summary}\n\n**AI 분석:**\n{s_analysis}")
            else: st.markdown(f"**AI 분석:**\n{s_analysis}")
            if st.button("🗑️ 삭제", key=f"del_scrap_{s_id}"):
                c.execute("DELETE FROM scrapbook WHERE id=?", (s_id,)); conn.commit(); st.rerun()

# [탭 6: 데이터 백업/복구]
with tab6:
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
                
        c.execute("SELECT title, link, summary, analysis, scrap_date FROM scrapbook")
        scrap_list = [{"title": r[0], "link": r[1], "summary": r[2], "analysis": r[3], "scrap_date": r[4]} for r in c.fetchall()]
        
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
                        c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)",
                                  (item['title'], item['link'], item['summary'], item['analysis'], item['scrap_date']))
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
