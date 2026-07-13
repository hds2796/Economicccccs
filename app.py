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

# =======================================================
# 1. 보안: 로그인 시스템 (비밀번호 확인 및 URL 파스)
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
# 2. 구글 드라이브 OAuth 인증 콜백 처리
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
    if not service:
        raise Exception("먼저 구글 드라이브로 로그인해야 합니다.")
        
    file_name = f"market_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    file_metadata = {
        'name': file_name,
        'parents': [st.secrets["GOOGLE_FOLDER_ID"]]
    }
    
    json_bytes = json_string.encode('utf-8')
    media = MediaIoBaseUpload(io.BytesIO(json_bytes), mimetype='application/json', resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')

def download_latest_from_google_drive():
    service = init_drive_service()
    if not service:
        raise Exception("먼저 구글 드라이브로 로그인해야 합니다.")
        
    folder_id = st.secrets["GOOGLE_FOLDER_ID"]
    query = f"'{folder_id}' in parents and mimeType = 'application/json' and trashed = false"
    results = service.files().list(
        q=query,
        orderBy="modifiedTime desc",
        pageSize=1,
        fields="files(id, name)"
    ).execute()
    
    files = results.get('files', [])
    if not files:
        raise Exception("구글 드라이브 폴더에 백업된 JSON 파일이 없습니다.")
        
    latest_file = files[0]
    file_id = latest_file['id']
    file_name = latest_file['name']
    
    content = service.files().get_media(fileId=file_id).execute()
    return content, file_name


# --- [세션 상태 관리] ---
if 'analysis_results' not in st.session_state: st.session_state.analysis_results = {}
if 'overall_analysis' not in st.session_state: st.session_state.overall_analysis = None

# --- [시장 지표 수집 함수] ---
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
            diff_pct = float(data['fluctuationsRatio'])
            if diff_pct < 0: diff = -diff
            return {"current": current, "diff": diff, "diff_pct": diff_pct}
        except: return {"current": 0, "diff": 0, "diff_pct": 0.0}

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

# --- [네이버 뉴스 API 수집 함수] ---
def clean_html(raw_html):
    if not raw_html: return ""
    return BeautifulSoup(raw_html, "html.parser").get_text()

@st.cache_data(ttl=1800)
def get_naver_news(query, display=10):
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET: return []
    url = "https://naverapihub.apigw.ntruss.com/search/v1/news"
    headers = {"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET}
    params = {"query": query, "display": display, "sort": "sim", "format": "json"}
    
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        return [{"title": clean_html(i['title']), "link": i['link'], "summary": clean_html(i['description']), "published": i['pubDate']} for i in response.json().get("items", [])]
    return []

# --- [제미나이 AI 분석 함수] ---
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
    prompt = f"다음 뉴스 10개를 종합하여 현재 증시 방향성을 브리핑하십시오.\n{combined_news}\n\n[양식]\n1. 🌐 거시 환경 종합 요약\n2. ⚖️ 증시 호악재 분석\n3. 💡 주목할 섹터\n\n반드시 마지막 줄에 'SCORE: 숫자' 형태로 시장 심리 지수를 0~100 사이로 기재하십시오."
    try:
        text = client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text
        match = re.search(r'SCORE:\s*(\d+)', text)
        score = int(match.group(1)) if match else 50
        return re.sub(r'SCORE:\s*\d+', '', text).strip(), score
    except Exception as e: return f"분석 오류: {e}", 50


# --- [상단 대시보드 출력] ---
st.title("📊 AI 종합 증시 분석 플랫폼")
market_data = get_market_data()
cols = st.columns(len(market_data))
for i, (name, data) in enumerate(market_data.items()):
    with cols[i]:
        if data.get('current', 0) > 0:
            st.metric(label=name, value=f"{data['current']:,.2f}", delta=f"{data['diff']:,.2f} ({data['diff_pct']:.2f}%)")
        else: st.metric(label=name, value="데이터 오류")
st.divider()

# --- [탭 구성] ---
tab1, tab2, tab3, tab4, tab5 = st.tabs(["🔥 거시 뉴스 & 시장 심리", "📑 섹터별 분석", "⭐️ 내 관심종목", "📁 스크랩북", "⚙️ 데이터 백업/복구"])

# [탭 1: 거시 뉴스]
with tab1:
    st.subheader("오늘의 핵심 거시 뉴스 (Top 10)")
    macro_query = "(정치 OR 사회 OR 국제 OR 정책) AND (증시 OR 주식 OR 주가 OR 경제 OR 환율)"
    top_news = get_naver_news(macro_query, display=10)
    
    if top_news:
        if st.button("전체 기사 기반 시장 브리핑 생성", type="primary"):
            with st.spinner("분석 중..."):
                analysis_text, score = analyze_overall_market(top_news)
                st.session_state.overall_analysis = {"text": analysis_text, "score": score}
                
        if st.session_state.overall_analysis:
            score = st.session_state.overall_analysis['score']
            st.markdown(f"**현재 AI 시장 심리 지수: {score} / 100**")
            st.progress(score / 100.0)
            st.markdown(st.session_state.overall_analysis['text'])
        
        st.markdown("---")
        for i, news in enumerate(top_news):
            st.markdown(f"**{i+1}. [{news['title']}]({news['link']})**")
            st.caption(f"{news['published']} | {news['summary']}")
            if st.button("이 기사 심층 분석", key=f"t1_btn_{i}"):
                st.session_state.analysis_results[news['link']] = analyze_single_news(news['title'], news['summary'])
            if news['link'] in st.session_state.analysis_results:
                st.info(st.session_state.analysis_results[news['link']])
                if st.button("💾 이 리포트 스크랩하기", key=f"t1_scrap_{i}"):
                    c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)",
                              (news['title'], news['link'], news['summary'], st.session_state.analysis_results[news['link']], datetime.now().strftime("%Y-%m-%d %H:%M")))
                    conn.commit()
                    st.success("스크랩북 저장 완료")
            st.divider()

# [탭 2: 섹터별 분석]
with tab2:
    sectors = {"반도체": "반도체 (삼성전자 OR SK하이닉스 OR 엔비디아)", "2차전지": "2차전지 OR 전기차 OR 배터리", "바이오": "바이오 OR 제약 OR 신약", "금융/밸류업": "은행 OR 금융지주 OR 밸류업", "IT/플랫폼": "네이버 OR 카카오 OR 인공지능", "방산/조선": "조선 OR 방산 OR K방산"}
    selected_sector = st.selectbox("관심 섹터 선택", list(sectors.keys()))
    sector_news = get_naver_news(sectors[selected_sector], display=5)
    
    for i, news in enumerate(sector_news):
        with st.expander(f"📰 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']})\n\n{news['summary']}")
            if st.button("AI 분석 실행", key=f"t2_btn_{selected_sector}_{i}"):
                st.session_state.analysis_results[news['link']] = analyze_single_news(news['title'], news['summary'])
            if news['link'] in st.session_state.analysis_results:
                st.info(st.session_state.analysis_results[news['link']])
                if st.button("💾 스크랩", key=f"t2_scrap_{selected_sector}_{i}"):
                    c.execute("INSERT INTO scrapbook (title, link, summary, analysis, scrap_date) VALUES (?, ?, ?, ?, ?)",
                              (news['title'], news['link'], news['summary'], st.session_state.analysis_results[news['link']], datetime.now().strftime("%Y-%m-%d %H:%M")))
                    conn.commit()
                    st.success("저장 완료")

# [탭 3: 관심종목]
with tab3:
    st.subheader("⭐️ 내 관심종목 맞춤 뉴스")
    new_stock = st.text_input("종목명 입력 (예: 카카오, 삼성전자)")
    if st.button("➕ 등록") and new_stock.strip():
        c.execute("INSERT INTO portfolio (stock_name) VALUES (?)", (new_stock.strip(),))
        conn.commit(); st.rerun()
        
    c.execute("SELECT id, stock_name FROM portfolio")
    portfolio = c.fetchall()
    if portfolio:
        for p_id, p_name in portfolio:
            if st.button(f"{p_name} ✖", key=f"del_port_{p_id}"):
                c.execute("DELETE FROM portfolio WHERE id=?", (p_id,)); conn.commit(); st.rerun()
        st.divider()
        port_news = get_naver_news(" OR ".join([p[1] for p in portfolio]), display=10)
        for i, news in enumerate(port_news):
            with st.expander(f"📰 {news['title']}"):
                if st.button("분석하기", key=f"t3_btn_{i}"):
                    st.session_state.analysis_results[news['link']] = analyze_single_news(news['title'], news['summary'])
                if news['link'] in st.session_state.analysis_results:
                    st.info(st.session_state.analysis_results[news['link']])
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

# [탭 5: 데이터 백업/복구 (구글 드라이브 양방향 연동)]
with tab5:
    st.subheader("⚙️ 데이터 백업 및 복구 관리")
    st.write("클라우드 서버 재부팅 시 수집된 관심종목 데이터와 스크랩북 내역이 초기화될 수 있으므로, 구글 드라이브 보관소에 연동하여 영구 보관하십시오.")
    
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
                
        # 백업용 데이터 추출 (JSON 구조화)
        c.execute("SELECT title, link, summary, analysis, scrap_date FROM scrapbook")
        scrap_rows = c.fetchall()
        scrap_list = [{"title": r[0], "link": r[1], "summary": r[2], "analysis": r[3], "scrap_date": r[4]} for r in scrap_rows]
        
        c.execute("SELECT stock_name FROM portfolio")
        port_rows = c.fetchall()
        port_list = [r[0] for r in port_rows]
        
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
                    for name in restore_data.get("portfolio", []):
                        c.execute("INSERT INTO portfolio (stock_name) VALUES (?)", (name,))
                    conn.commit()
                    st.success(f"성공! 최신 백업 파일 [{file_name}] 데이터를 정상적으로 복구했습니다.")
                    st.rerun()
                except Exception as e: st.error(f"불러오기 실패: {e}")