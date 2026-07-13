import streamlit as st
import requests
import re
import sqlite3
import json
import os
import io
import yfinance as yf
from datetime import datetime

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
st.set_page_config(page_title="AI 증시 분석 플랫폼", page_icon="📊", layout="wide")

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
try:
    c.execute("ALTER TABLE portfolio ADD COLUMN search_query TEXT")
except sqlite3.OperationalError:
    pass

try:
    c.execute("ALTER TABLE portfolio ADD COLUMN ticker TEXT")
except sqlite3.OperationalError:
    pass
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

    st.title("🔒 AI 증시 분석 플랫폼 로그인")
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

# 중복 기사 필터링을 위한 메모리 저장소 초기화 (거시 -> 경제 변수명 변경)
if 'eco_start' not in st.session_state: st.session_state.eco_start = 1
if 'seen_eco' not in st.session_state: st.session_state.seen_eco = set()
if 'current_eco_news' not in st.session_state: st.session_state.current_eco_news = []

if 'sector_starts' not in st.session_state: st.session_state.sector_starts = {}
if 'seen_sectors' not in st.session_state: st.session_state.seen_sectors = {}
if 'current_sector_news' not in st.session_state: st.session_state.current_sector_news = {}

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
            
            # API 내부 상태 코드 명시적 판별 (4:하락, 5:하한)
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

def clean_html(raw_html):
    if not raw_html: return ""
    return BeautifulSoup(raw_html, "html.parser").get_text()

# API 캐시 유지 시간을 30분에서 5분으로 단축하여 새로고침 효율 최적화
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

# 중복을 피하며 무한 새로고침을 처리하는 커스텀 로직 (경제 뉴스용)
def fetch_unique_eco_news(query):
    unique_news = []
    attempts = 0
    # 최대 3회까지만 API를 호출하여 루프 방지
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
            
    # API 한계 도달 시 메모리 초기화 후 1페이지부터 재시작
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
        
    unique_news = []
    attempts = 0
    while len(unique_news) < 10 and st.session_state.sector_starts[sector_name] <= 900 and attempts < 3:
        batch = get_naver_news(query, display=10, start=st.session_state.sector_starts[sector_name])
        st.session_state.sector_starts[sector_name] += 10
        attempts += 1
        if not batch: break
        for n in batch:
            if n['link'] not in st.session_state.seen_sectors[sector_name]:
                unique_news.append(n)
                st.session_state.seen_sectors[sector_name].add(n['link'])
            if len(unique_news) == 10: break
            
    if not unique_news:
        st.session_state.sector_starts[sector_name] = 1
        st.session_state.seen_sectors[sector_name] = set()
        batch = get_naver_news(query, display=10, start=1)
        st.session_state.sector_starts[sector_name] = 11
        for n in (batch or []):
            unique_news.append(n)
            st.session_state.seen_sectors[sector_name].add(n['link'])
            if len(unique_news) == 10: break
            
    st.session_state.current_sector_news[sector_name] = unique_news

# --- [제미나이 AI 분석 함수 및 데이터] ---
# 사전 정의: 종목명 유의어 및 야후 파이낸스 티커 매핑 (UI 이전에 정의)
STOCK_INFO = {
    "삼성전자": {"alias": "삼성전자", "ticker": "005930.KS"},
    "SK하이닉스": {"alias": "SK하이닉스 OR 하이닉스", "ticker": "000660.KS"},
    "현대차": {"alias": "현대차 OR 현대자동차", "ticker": "005380.KS"},
    "기아": {"alias": "기아 OR 기아차 OR 기아자동차", "ticker": "000270.KS"},
    "카카오": {"alias": "카카오 OR 카카오톡 OR 카카오페이 OR 카카오뱅크", "ticker": "035720.KS"},
    "네이버": {"alias": "네이버 OR NAVER OR 라인", "ticker": "035420.KS"},
    "우리금융지주": {"alias": "우리금융지주 OR 우리은행 OR 우리투자증권 OR 우리카드 OR 우리종금", "ticker": "316140.KS"},
    "KB금융": {"alias": "KB금융 OR 국민은행 OR KB증권 OR KB국민카드", "ticker": "105560.KS"},
    "신한지주": {"alias": "신한지주 OR 신한은행 OR 신한투자증권 OR 신한카드", "ticker": "055550.KS"},
    "하나금융지주": {"alias": "하나금융지주 OR 하나은행 OR 하나증권 OR 하나카드", "ticker": "086790.KS"},
    "LG에너지솔루션": {"alias": "LG에너지솔루션 OR LG엔솔", "ticker": "373220.KS"},
    "셀트리온": {"alias": "셀트리온 OR 셀트리온제약", "ticker": "068270.KS"}
}

def analyze_single_news(title, summary):
    if not GEMINI_API_KEY: return "Gemini API 키 오류"
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"아래 뉴스가 주식 시장에 미칠 영향을 분석하십시오.\n[제목]: {title}\n[요약]: {summary}\n1. 💡 사건 핵심 요약\n2. 📈 시장 파급력\n3. 🎯 연관 섹터"
    try: return client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text
    except Exception as e: return f"분석 오류: {e}"

def analyze_overall_market(news_list):
    if not GEMINI_API_KEY: return "Gemini API 키 오류", 50
    client = genai.Client(api_key=GEMINI_API_KEY)
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    prompt = f"다음 수집된 {len(news_list)}개의 주요 뉴스를 모두 종합하여 현재 증시 방향성을 브리핑하십시오.\n{combined_news}\n\n[양식]\n1. 🌐 거시 환경 종합 요약\n2. ⚖️ 증시 호악재 분석\n3. 💡 주목할 섹터\n\n반드시 마지막 줄에 'SCORE: 숫자' 형태로 시장 심리 지수를 0~100 사이로 기재하십시오."
    try:
        text = client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text
        match = re.search(r'SCORE:\s*(\d+)', text)
        score = int(match.group(1)) if match else 50
        return re.sub(r'SCORE:\s*\d+', '', text).strip(), score
    except Exception as e: return f"분석 오류: {e}", 50

def analyze_sector_news(sector_name, news_list):
    if not GEMINI_API_KEY: return "Gemini API 키 오류"
    client = genai.Client(api_key=GEMINI_API_KEY)
    combined_news = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    prompt = f"다음 수집된 '{sector_name}' 섹터 관련 {len(news_list)}개의 최신 주요 뉴스를 모두 종합하여 분석하십시오.\n{combined_news}\n\n[양식]\n1. 🏭 섹터 전반적 흐름 요약\n2. 📈 주요 호재 및 악재 요인\n3. 🎯 투자 심리 및 단기 전망"
    try: return client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text
    except Exception as e: return f"분석 오류: {e}"

def analyze_deep_dive(stock_name, news_title, news_summary):
    if not GEMINI_API_KEY: return "Gemini API 키 오류"
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # 재무 데이터 스크래핑 (yfinance)
    fin_data = "재무 데이터 조회 불가 (미지원 또는 야후 파이낸스 통신 오류)"
    if stock_name in STOCK_INFO and STOCK_INFO[stock_name]["ticker"]:
        try:
            info = yf.Ticker(STOCK_INFO[stock_name]["ticker"]).info
            market_cap = info.get('marketCap', 0)
            market_cap_str = f"{market_cap / 1_000_000_000_000:.1f}조 원" if market_cap else "N/A"
            
            fin_data = (f"- 시가총액: {market_cap_str}\n"
                        f"- PER (주가수익비율): {info.get('trailingPE', 'N/A')}\n"
                        f"- PBR (주가순자산비율): {info.get('priceToBook', 'N/A')}\n"
                        f"- 52주 최고/최저: {info.get('fiftyTwoWeekHigh', 'N/A')} / {info.get('fiftyTwoWeekLow', 'N/A')}")
        except Exception: pass
        
    prompt = (f"[{stock_name} 심층 분석 리포트]\n\n"
              f"[최신 핵심 뉴스]\n- 제목: {news_title}\n- 요약: {news_summary}\n\n"
              f"[현재 재무 상태]\n{fin_data}\n\n"
              f"위의 뉴스 데이터와 객관적 재무 상태를 종합하여 다음 양식으로 브리핑을 작성하십시오.\n"
              f"1. 🏢 기업 펀더멘털 및 재무 요약\n"
              f"2. 📈 뉴스가 주가에 미치는 단기/중장기 파급력\n"
              f"3. 🎯 투자 관점 종합 의견 (매수/보유/매도 등 방향성 제시)")
    try: return client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text
    except Exception as e: return f"분석 오류: {e}"

# =======================================================
# 4. 상단 대시보드 및 UI 구성
# =======================================================
st.title("📊 AI 종합 증시 분석 플랫폼")
market_data = get_market_data()
cols = st.columns(len(market_data))
for i, (name, data) in enumerate(market_data.items()):
    with cols[i]:
        if data.get('current', 0) > 0:
            st.metric(label=name, value=f"{data['current']:,.2f}", delta=f"{data['diff']:,.2f} ({data['diff_pct']:.2f}%)")
        else: st.metric(label=name, value="데이터 오류")
st.divider()

tab1, tab2, tab3, tab4, tab5 = st.tabs(["🔥 경제 뉴스 & 시장 심리", "📑 섹터별 분석", "⭐️ 내 관심종목", "📁 스크랩북", "⚙️ 데이터 백업/복구"])

# [탭 1: 경제 뉴스]
with tab1:
    st.subheader("오늘의 핵심 경제 뉴스")
    eco_query = "경제 OR 증시 OR 주식 OR 금융"
    
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
                analysis_text, score = analyze_overall_market(top_50_news)
                st.session_state.overall_analysis = {"text": analysis_text, "score": score}
                
        if st.session_state.overall_analysis:
            score = st.session_state.overall_analysis['score']
            st.markdown(f"**현재 AI 시장 심리 지수: {score} / 100**")
            st.progress(score / 100.0)
            st.markdown(st.session_state.overall_analysis['text'])
        
        st.markdown("---")
        for i, news in enumerate(st.session_state.current_eco_news):
            st.markdown(f"**{i+1}. [{news['title']}]({news['link']})**")
            st.caption(f"{news['published']} | {news['summary']}")
            if st.button("이 기사 심층 분석", key=f"t1_btn_{news['link']}"):
                st.session_state.analysis_results[news['link']] = analyze_single_news(news['title'], news['summary'])
            if news['link'] in st.session_state.analysis_results:
                st.info(st.session_state.analysis_results[news['link']])
                if st.button("💾 이 리포트 스크랩하기", key=f"t1_scrap_{news['link']}"):
                    c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)",
                              (news['title'], news['link'], news['summary'], st.session_state.analysis_results[news['link']], datetime.now().strftime("%Y-%m-%d %H:%M")))
                    conn.commit()
                    st.success("스크랩북 저장 완료")
            st.divider()

# [탭 2: 섹터별 분석]
with tab2:
    # NCP API의 오류를 방지하기 위해 괄호()를 제거하고 직관적인 OR 연산으로 쿼리 최적화
    sectors = {
        "반도체": "반도체 주가 OR 삼성전자 실적 OR SK하이닉스 주가", 
        "2차전지": "2차전지 주가 OR 전기차 실적 OR 배터리 주가", 
        "바이오": "바이오 주가 OR 제약 실적 OR 신약", 
        "금융/밸류업": "금융지주 주가 OR 은행 실적 OR 밸류업", 
        "IT/플랫폼": "네이버 주가 OR 카카오 실적 OR 인공지능 주식", 
        "방산/조선": "조선주 실적 OR 방산주 주가 OR K방산"
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
                st.session_state[f'sector_summary_{selected_sector}'] = analyze_sector_news(selected_sector, top_20_news)
                
        if f'sector_summary_{selected_sector}' in st.session_state:
            st.markdown("### 📊 섹터 종합 브리핑")
            st.info(st.session_state[f'sector_summary_{selected_sector}'])
            st.markdown("---")
            
    for i, news in enumerate(sector_news):
        with st.expander(f"📰 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']})\n\n{news['summary']}")
            if st.button("AI 분석 실행", key=f"t2_btn_{news['link']}"):
                st.session_state.analysis_results[news['link']] = analyze_single_news(news['title'], news['summary'])
            if news['link'] in st.session_state.analysis_results:
                st.info(st.session_state.analysis_results[news['link']])
                if st.button("💾 스크랩", key=f"t2_scrap_{news['link']}"):
                    c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)",
                              (news['title'], news['link'], news['summary'], st.session_state.analysis_results[news['link']], datetime.now().strftime("%Y-%m-%d %H:%M")))
                    conn.commit()
                    st.success("저장 완료")

# [탭 3: 관심종목]
with tab3:
    st.subheader("⭐️ 내 관심종목 맞춤 뉴스")
    new_stock = st.text_input("종목명 입력 (예: 카카오, 삼성전자, 에코프로)")
    if st.button("➕ 등록") and new_stock.strip():
        with st.spinner(f"AI가 '{new_stock.strip()}'의 종목 코드와 연관 검색어를 분석 중입니다..."):
            client = genai.Client(api_key=GEMINI_API_KEY)
            prompt = f"""사용자가 한국 주식 '{new_stock.strip()}'을 관심종목에 추가했습니다.
            1. 야후 파이낸스 티커: 코스피는 '6자리숫자.KS', 코스닥은 '6자리숫자.KQ'. (모르면 빈 문자열 "")
            2. 검색어: 뉴스 검색 시 유용한 계열사/유의어 포함 (예: {new_stock.strip()} OR 자회사)
            반드시 아래 JSON 형식으로만 답변하세요.
            {{"ticker": "005930.KS", "search_query": "{new_stock.strip()} OR 유의어"}}"""
            
            ticker = ""
            search_query = new_stock.strip()
            try:
                res = client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text
                match = re.search(r'\{.*\}', res, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                    ticker = data.get("ticker", "")
                    search_query = data.get("search_query", new_stock.strip())
            except: pass
            
            c.execute("INSERT INTO portfolio (stock_name, search_query, ticker) VALUES (?, ?, ?)", (new_stock.strip(), search_query, ticker))
            conn.commit()
            st.rerun()
            
    c.execute("SELECT id, stock_name, search_query, ticker FROM portfolio")
    portfolio = c.fetchall()
    if portfolio:
        for p_id, p_name, p_query, p_ticker in portfolio:
            if st.button(f"{p_name} ✖", key=f"del_port_{p_id}"):
                c.execute("DELETE FROM portfolio WHERE id=?", (p_id,)); conn.commit(); st.rerun()
        st.divider()
        
        st.write(f"🔍 **등록된 종목 관련 핵심 비즈니스 뉴스** (가십성 기사 제외)")
        for p_id, p_name, p_query, p_ticker in portfolio:
            st.markdown(f"#### 📌 [{p_name}] 최신 동향")
            
            # DB에 저장된 AI 자동 생성 유의어를 사용하여 검색
            search_keywords = [k.strip() for k in (p_query or p_name).split(" OR ")]
            broad_query = " OR ".join(search_keywords)
            raw_news = get_naver_news(broad_query, display=30)
            
            # 파이썬 내부에서 비즈니스 키워드가 포함된 기사만 강력하게 필터링
            business_kws = ["주가", "실적", "목표가", "수주", "배당", "합병", "투자", "인수", "매출", "영업이익"]
            port_news = []
            
            for n in raw_news:
                if any(b_kw in n['title'] or b_kw in n['summary'] for b_kw in business_kws):
                    port_news.append(n)
                if len(port_news) == 3: # 3개만 노출
                    break
            
            if port_news:
                for i, news in enumerate(port_news):
                    with st.expander(f"📰 {news['title']}"):
                        st.caption(news['published'])
                        st.write(news['summary'])
                        
                        col_btn1, col_btn2 = st.columns(2)
                        with col_btn1:
                            if st.button("일반 뉴스 분석", key=f"t3_btn_{p_id}_{i}"):
                                st.session_state.analysis_results[news['link']] = analyze_single_news(news['title'], news['summary'])
                        with col_btn2:
                            if st.button("📊 심층 분석 리포트 (재무+뉴스)", type="primary", key=f"t3_deep_{p_id}_{i}"):
                                with st.spinner("실시간 재무 데이터 스크래핑 및 종합 분석 중..."):
                                    st.session_state.analysis_results[news['link']] = analyze_deep_dive(p_name, p_ticker, news['title'], news['summary'])
                                    
                        if news['link'] in st.session_state.analysis_results:
                            st.info(st.session_state.analysis_results[news['link']])
                            if st.button("💾 스크랩", key=f"t3_scrap_{p_id}_{i}"):
                                c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)",
                                          (news['title'], news['link'], news['summary'], st.session_state.analysis_results[news['link']], datetime.now().strftime("%Y-%m-%d %H:%M")))
                                conn.commit()
                                st.success("저장 완료")
            else:
                st.info(f"'{p_name}' 관련 비즈니스 뉴스가 없습니다.")
            st.markdown("---")
    else: st.info("등록된 관심종목이 없습니다.")

# [탭 4: 스크랩북]
with tab4:
    st.subheader("📁 내 스크랩북 (저장된 리포트)")
    c.execute("SELECT id, title, link, summary, analysis, scrap_date FROM scrapbook ORDER BY id DESC")
    scraps = c.fetchall()
    for s_id, s_title, s_link, s_summary, s_analysis, s_date in scraps:
        with st.expander(f"[{s_date}] {s_title}"):
            st.markdown(f"[기사 링크]({s_link})\n\n**요약:** {s_summary}\n\n**AI 분석:**\n{s_analysis}")
            if st.button("🗑️ 삭제", key=f"del_scrap_{s_id}"):
                c.execute("DELETE FROM scrapbook WHERE id=?", (s_id,)); conn.commit(); st.rerun()

# [탭 5: 데이터 백업/복구]
with tab5:
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
        
        c.execute("SELECT stock_name, search_query, ticker FROM portfolio")
        port_list = [{"stock_name": r[0], "search_query": r[1], "ticker": r[2]} for r in c.fetchall()]
        
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
                        # 구버전 백업 파일 호환성 유지
                        if isinstance(item, str): 
                            c.execute("INSERT INTO portfolio (stock_name) VALUES (?)", (item,))
                        else:
                            c.execute("INSERT INTO portfolio (stock_name, search_query, ticker) VALUES (?, ?, ?)", 
                                      (item.get("stock_name"), item.get("search_query"), item.get("ticker")))
                    conn.commit()
                    st.success(f"성공! 최신 백업 파일 [{file_name}] 데이터를 정상적으로 복구했습니다.")
                    st.rerun()
                except Exception as e: st.error(f"불러오기 실패: {e}")
