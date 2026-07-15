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

# 스키마 업데이트
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
# 1. 보안 & OAuth 인증
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

def handle_oauth_callback():
    if 'code' in st.query_params and 'state' in st.query_params:
        state, code = st.query_params['state'], st.query_params['code']
        c.execute("SELECT verifier FROM oauth_store WHERE state=?", (state,))
        row = c.fetchone()
        if row:
            try:
                flow = Flow.from_client_config(json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"]), scopes=SCOPES, redirect_uri=st.secrets["REDIRECT_URI"])
                flow.code_verifier = row[0]
                flow.fetch_token(code=code)
                cred_dict = {'token': flow.credentials.token, 'refresh_token': flow.credentials.refresh_token, 'token_uri': flow.credentials.token_uri, 'client_id': flow.credentials.client_id, 'client_secret': flow.credentials.client_secret, 'scopes': flow.credentials.scopes}
                c.execute("DELETE FROM oauth_creds"); c.execute("INSERT INTO oauth_creds VALUES (?)", (json.dumps(cred_dict),)); conn.commit()
                st.query_params.clear(); st.rerun()
            except Exception as e: st.error(f"인증 오류: {e}")

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
    return service.files().create(body={'name': f"market_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", 'parents': [st.secrets["GOOGLE_FOLDER_ID"]]}, media_body=media, fields='id').execute()

def download_latest_from_google_drive():
    service = init_drive_service()
    if not service: raise Exception("구글 로그인 필요")
    results = service.files().list(q=f"'{st.secrets['GOOGLE_FOLDER_ID']}' in parents and trashed = false", orderBy="modifiedTime desc", pageSize=1).execute()
    files = results.get('files', [])
    if not files: raise Exception("백업 파일 없음")
    return service.files().get_media(fileId=files[0]['id']).execute(), files[0]['name']

# =======================================================
# 2. 데이터 상태 관리 및 시장 데이터 (S&P 500 패치 완료)
# =======================================================
for key in ['analysis_results', 'seen_realtime', 'seen_eco', 'sector_starts', 'seen_sectors', 'port_starts', 'current_sector_news']:
    if key not in st.session_state: st.session_state[key] = {} if 'starts' in key or 'news' in key or 'sectors' in key or 'results' in key else set()

if 'realtime_start' not in st.session_state: st.session_state.realtime_start = 1
if 'eco_start' not in st.session_state: st.session_state.eco_start = 1

@st.cache_data(ttl=60)
def get_market_data():
    results = {}
    def fetch_naver(code):
        try:
            data = requests.get(f"https://polling.finance.naver.com/api/realtime/domestic/index/{code}", headers={'User-Agent': 'Mozilla/5.0'}, timeout=3).json()['datas'][0]
            current = float(data['closePrice'].replace(',', ''))
            diff = float(data['compareToPreviousClosePrice'].replace(',', ''))
            pct = float(data['fluctuationsRatio'].replace(',', ''))
            f = str(data.get('compareToPreviousPrice', {}).get('code', '3'))
            if f in ['4', '5']: diff, pct = -abs(diff), -abs(pct)
            return {"current": current, "diff": diff, "diff_pct": pct}
        except: return {"current": 0, "diff": 0, "diff_pct": 0.0}

    results["코스피 (실시간)"] = fetch_naver("KOSPI")
    results["코스닥 (실시간)"] = fetch_naver("KOSDAQ")

    def fetch_yahoo(ticker):
        try:
            encoded = urllib.parse.quote(ticker)
            res = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d", headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'}, timeout=5).json()
            closes = [c for c in res['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
            if len(closes) >= 2:
                diff = closes[-1] - closes[-2]
                return {"current": closes[-1], "diff": diff, "diff_pct": (diff / closes[-2]) * 100}
        except: pass
        return {"current": 0, "diff": 0, "diff_pct": 0.0}

    results["S&P 500 (실시간)"] = fetch_yahoo("^GSPC")
    results["원/달러 환율"] = fetch_yahoo("KRW=X")
    return results

@st.cache_data(ttl=300)
def get_stock_current_price(ticker):
    if not ticker: return 0.0
    try:
        if re.search(r'\d{6}', ticker):
            res = requests.get(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{re.search(r'\d{6}', ticker).group()}", headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
            if res.get('datas'): return float(res['datas'][0]['closePrice'].replace(',', ''))
        res = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range=2d&interval=1d", headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
        closes = [c for c in res['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if closes: return float(closes[-1])
    except: pass
    return 0.0

@st.cache_data(ttl=300)
def get_naver_news(query, display=100, start=1, sort_type="date"):
    if not NAVER_CLIENT_ID: return []
    all_items = []
    for q in [q.strip() for q in query.split('|') if q.strip()]:
        try:
            res = requests.get("https://naverapihub.apigw.ntruss.com/search/v1/news", headers={"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET}, params={"query": q, "display": display//2, "start": start, "sort": sort_type, "format": "json"}, timeout=3).json()
            for i in res.get("items", []):
                try: dt = parsedate_to_datetime(i['pubDate'])
                except: dt = datetime.now(timezone.utc)
                all_items.append({"title": BeautifulSoup(i['title'], "html.parser").get_text(), "link": i['link'], "summary": BeautifulSoup(i['description'], "html.parser").get_text(), "published": dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"), "raw_date": dt})
        except: pass
    unique = []
    seen = set()
    for item in sorted(all_items, key=lambda x: x['raw_date'], reverse=True):
        if item['link'] not in seen: seen.add(item['link']); unique.append(item)
    return unique[:display]

# =======================================================
# 3. AI 코어 엔진 (Lite 모델 분리 & 앙상블 로직)
# =======================================================
def call_gemini_with_fallback(prompt, is_json=False, use_lite=False):
    client = genai.Client(api_key=GEMINI_API_KEY)
    models = [('gemini-3.1-flash-lite', '')] if use_lite else [('gemini-3.5-flash', ''), ('gemini-2.5-flash', ''), ('gemini-1.5-flash', ''), ('gemini-3.1-flash-lite', '')]
    for m_name, _ in models:
        try: return client.models.generate_content(model=m_name, contents=prompt).text
        except: continue
    raise Exception("AI 호출 실패")

@st.cache_data(ttl=86400)
def get_dynamic_keywords():
    try:
        res = call_gemini_with_fallback("현재 한국 주식 시장 핫 키워드 15개를 '|'로 연결해 출력해줘(예: HBM|밸류업)", use_lite=True)
        return re.sub(r'[^가-힣a-zA-Z0-9|]', '', res).strip().split('|')
    except: return ["HBM", "AI", "밸류업", "전고체", "M&A", "실적"]

def build_deep_dive_prompt(name, ticker, news, owned_info, current_price, market_str):
    fin = "재무 정보 조회 불가"
    try:
        yf_info = yf.Ticker(ticker if ".K" in ticker else f"{ticker}.KS").info
        fin = f"- 시총: {yf_info.get('marketCap',0)/1e12:.1f}조\n- PER: {yf_info.get('trailingPE','N/A')}\n- PBR: {yf_info.get('priceToBook','N/A')}"
    except: pass
    news_str = "\n".join([f"- {n['title']}" for n in news[:15]])
    return f"[{name} 심층분석]\n시장:{market_str}\n보유:{owned_info}\n현재가:{current_price:,.0f}원\n뉴스:\n{news_str}\n재무:\n{fin}\n양식: 1.기업진단 2.거시파급력 3.포폴전략 4.투자의견 5.목표가(Peer비교포함)\n마지막줄에 'TARGET_PRICE: 숫자' 필수."

# =======================================================
# 4. 메인 대시보드 UI
# =======================================================
st.title("📊 Project2_Stock")
market = get_market_data()
market_str = ", ".join([f"{k}:{v['current']:,.0f}({v['diff_pct']:+.2f}%)" for k,v in market.items() if v['current']>0])

cols = st.columns(len(market))
for i, (name, data) in enumerate(market.items()):
    with cols[i]:
        if data['current'] > 0: st.metric(name, f"{data['current']:,.2f}", f"{data['diff']:,.2f}({data['diff_pct']:.2f}%)")
        else: st.metric(name, "오류")
st.divider()

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(["📰 실시간", "🔥 핵심경제", "📑 섹터", "🎯 추천", "⭐️ 관심종목", "📁 스크랩", "⚙️ 설정"])

# --- [탭 5: 관심종목] (Fragment 멀티태스킹 적용 핵심 구역) ---
with tab5:
    st.subheader("⭐️ 내 관심종목 & AI 앙상블 진단")
    
    with st.form("add_stock"):
        new_s = st.text_input("종목명 입력")
        st_owned = st.radio("보유상태", ["미보유", "보유중"], horizontal=True)
        c1, c2 = st.columns(2)
        avg_p = c1.text_input("평단가", value="0")
        qty = c2.number_input("수량", min_value=0, value=0)
        if st.form_submit_button("➕ 등록") and new_s:
            res = call_gemini_with_fallback(f"한국주식 '{new_s}'의 야후티커(.KS/.KQ)와 검색어 JSON으로 줘. {{'ticker':'', 'query':''}}", is_json=True, use_lite=True)
            try:
                data = json.loads(re.search(r'\{.*\}', res, re.S).group())
                c.execute("INSERT INTO portfolio (stock_name, search_query, ticker, is_owned, avg_price, quantity) VALUES (?,?,?,?,?,?)", (new_s, data['query'], data['ticker'], 1 if st_owned=="보유중" else 0, float(avg_p.replace(',','')), qty))
                conn.commit(); st.rerun()
            except: st.error("등록 실패")

    c.execute("SELECT id, stock_name, search_query, ticker, is_owned, avg_price, quantity FROM portfolio")
    portfolio = c.fetchall()
    
    if portfolio:
        all_kws = list(set(["주가","실적","수주","공급","M&A"] + get_dynamic_keywords()))
        port_cache = {}
        with st.spinner("⚡ 데이터 병렬 수집 중..."):
            def fetch_p(p):
                p_id, name, query, ticker, owned, avg, qnt = p
                cur_p = get_stock_current_price(ticker or name)
                raw = get_naver_news(query or name, display=50)
                fact_news = [n for n in raw if any(k in n['title'] or k in n['summary'] for k in all_kws)]
                return p_id, cur_p, fact_news[:10], raw[:30]
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as exe:
                for r in exe.map(fetch_p, portfolio): port_cache[r[0]] = r

        # --- ⭐️ 우회법 핵심: Fragment 함수 정의 ---
        @st.fragment
        def render_stock_box(p, p_data):
            p_id, name, query, ticker, is_owned, avg_price, quantity = p
            cur_price, fact_news, raw_news = p_data[1], p_data[2], p_data[3]
            
            st.markdown(f"### 📌 [{name}]")
            col_info, col_btn = st.columns([3, 1])
            
            with col_info:
                if is_owned:
                    roi = ((cur_price - avg_price)/avg_price)*100 if avg_price>0 else 0
                    st.caption(f"💼 **보유** | 평단:{avg_price:,.0f} | 수량:{quantity} | 현재:{cur_price:,.0f} | 수익률: {'🔴' if roi>0 else '🔵'} {roi:.2f}%")
                else: st.caption(f"👀 **관심** | 현재가: {cur_price:,.0f}원")
            
            with col_btn:
                # 영구 캐시 확인
                cache_key = f"deep_{p_id}"
                has_cache = cache_key in st.session_state.analysis_results
                
                if has_cache:
                    if st.button("📊 저장된 진단 보기", key=f"view_{p_id}"):
                        st.session_state[f"show_{p_id}"] = True
                else:
                    if st.button("🚀 AI 심층 진단", key=f"run_{p_id}", type="primary"):
                        with st.spinner("🤖 AI가 멀티태스킹 중... (탭 이동 가능)"):
                            # 앙상블 뉴스 결합
                            ai_news = st.session_state.get(f"ai_news_{p_id}", [])
                            combined = {n['link']: n for n in (fact_news + ai_news)}.values()
                            status = f"보유중(수익률{((cur_price-avg_price)/avg_price)*100:.1f}%)" if is_owned else "미보유"
                            prompt = build_deep_dive_prompt(name, ticker, list(combined), status, cur_price, market_str)
                            report = call_gemini_with_fallback(prompt)
                            st.session_state.analysis_results[cache_key] = {"text": report, "time": time.time()}
                            st.session_state[f"show_{p_id}"] = True
                            st.rerun()

            # 리포트 출력 구역
            if st.session_state.get(f"show_{p_id}"):
                with st.expander("📝 AI 포트폴리오 진단 리포트", expanded=True):
                    rep = st.session_state.analysis_results[cache_key]['text']
                    st.write(rep)
                    tp = 0.0
                    match = re.search(r'TARGET_PRICE:\s*([\d,]+)', rep)
                    if match: tp = float(match.group(1).replace(',',''))
                    
                    c1, c2 = st.columns(2)
                    if c1.button("💾 스크랩", key=f"save_{p_id}"):
                        c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price) VALUES (?,?,?,?,?,?,?,?)", (f"[{name}] 진단", "AI 심층 진단 리포트", rep, datetime.now().strftime("%Y-%m-%d %H:%M"), name, ticker, cur_price, tp))
                        conn.commit(); st.success("저장 완료")
                    if c2.button("🔄 강제 재분석", key=f"force_{p_id}"):
                        del st.session_state.analysis_results[cache_key]; st.rerun()

            # 뉴스 구역
            with st.expander(f"📰 '{name}' 관련 뉴스 보기", expanded=False):
                if st.button("✨ AI 문맥 정밀 필터 가동", key=f"ai_f_{p_id}"):
                    with st.spinner("Lite 모델이 옥석 가리는 중..."):
                        news_context = "\n".join([f"[{i}] {n['title']}" for i,n in enumerate(raw_news)])
                        res = call_gemini_with_fallback(f"{news_context}\n위 뉴스 중 호재/악재 기사 인덱스만 JSON [0,1]로 줘", use_lite=True)
                        try:
                            idx = json.loads(re.search(r'\[.*\]', res).group())
                            st.session_state[f"ai_news_{p_id}"] = [raw_news[i] for i in idx if i < len(raw_news)]
                            st.success("필터링 완료 (심층 진단 시 반영됨)")
                        except: pass
                
                display_news = st.session_state.get(f"ai_news_{p_id}", fact_news)
                for n in display_news:
                    st.markdown(f"**[{n['title']}]({n['link']})**")
                    st.caption(f"{n['published']} | {n['summary'][:100]}...")
            
            if st.button("✖ 삭제", key=f"del_{p_id}"):
                c.execute("DELETE FROM portfolio WHERE id=?", (p_id,)); conn.commit(); st.rerun()
            st.divider()

        # 개별 종목 박스 렌더링
        for p in portfolio:
            if p[0] in port_cache: render_stock_box(p, port_cache[p[0]])

# =======================================================
# 5. 기타 탭 (스크랩북, 백업 등 기존 로직 유지)
# =======================================================
with tab6:
    st.subheader("📁 내 스크랩북 & AI 예측 트래킹")
    c.execute("SELECT id, title, link, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price FROM scrapbook ORDER BY id DESC")
    scraps = c.fetchall()
    if scraps:
        for s in scraps:
            with st.expander(f"[{s[5]}] {s[1]}"):
                if s[6] and s[9] > 0:
                    cur = get_stock_current_price(s[7] or s[6])
                    c1, c2, c3 = st.columns(3)
                    c1.metric("저장가", f"{s[8]:,.0f}")
                    c2.metric("실시간", f"{cur:,.0f}", f"{((cur-s[8])/s[8])*100:+.2f}%")
                    c3.metric("AI 목표가", f"{s[9]:,.0f}", f"{(cur/s[9])*100:.1f}% 달성")
                st.write(s[4])
                if st.button("🗑️ 삭제", key=f"sd_{s[0]}"):
                    c.execute("DELETE FROM scrapbook WHERE id=?", (s[0],)); conn.commit(); st.rerun()

with tab7:
    st.subheader("⚙️ 데이터 관리")
    c.execute("SELECT COUNT(*) FROM oauth_creds")
    if not f_auth := c.fetchone()[0] > 0:
        url, state = Flow.from_client_config(json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"]), scopes=SCOPES, redirect_uri=st.secrets["REDIRECT_URI"]).authorization_url(prompt='consent')
        c.execute("DELETE FROM oauth_store"); c.execute("INSERT INTO oauth_store VALUES (?,?)", (state, Flow.from_client_config(json.loads(st.secrets["GOOGLE_CLIENT_CONFIG"]), scopes=SCOPES, redirect_uri=st.secrets["REDIRECT_URI"]).code_verifier)); conn.commit()
        st.link_button("👉 구글 드라이브 연동", url)
    else:
        if st.button("🚀 구글 드라이브에 지금 백업"):
            c.execute("SELECT * FROM portfolio"); p_all = c.fetchall()
            c.execute("SELECT * FROM scrapbook"); s_all = c.fetchall()
            try:
                upload_to_google_drive(json.dumps({"portfolio": p_all, "scrapbook": s_all}, ensure_ascii=False))
                st.success("백업 완료")
            except Exception as e: st.error(f"실패: {e}")
        if st.button("🔄 최신 백업 불러오기"):
            try:
                data, name = download_latest_from_google_drive()
                db = json.loads(data.decode('utf-8'))
                c.execute("DELETE FROM portfolio"); c.execute("DELETE FROM scrapbook")
                for p in db['portfolio']: c.execute("INSERT INTO portfolio VALUES (" + ",".join(["?"]*len(p)) + ")", p)
                for s in db['scrapbook']: c.execute("INSERT INTO scrapbook VALUES (" + ",".join(["?"]*len(s)) + ")", s)
                conn.commit(); st.success(f"복구 완료: {name}"); st.rerun()
            except Exception as e: st.error(f"실패: {e}")

# 탭 1~4는 기존의 효율적인 로직을 그대로 유지하되 3.5 모델 호출로 자동 배정됨
with tab1:
    if st.button("🕒 실시간 뉴스 브리핑 (최신 20건)"):
        news = get_naver_news("증시|금융|경제", display=20)
        st.write_stream(genai.Client(api_key=GEMINI_API_KEY).models.generate_content_stream(model='gemini-3.5-flash', contents=f"뉴스:\n{news}\n요약해줘"))
