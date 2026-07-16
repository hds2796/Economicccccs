import streamlit as st
import json
import sqlite3
import urllib.parse
import re
import io
import threading
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google import genai
from google.genai import types as genai_types

# =======================================================
# 1. 페이지 설정 (가장 먼저 실행되어야 함)
# =======================================================
st.set_page_config(page_title="Project2_Stock", page_icon="📊", layout="wide")

# =======================================================
# 2. 보안: 비밀번호 로그인 시스템
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
# 3. API 키 및 폴더 ID 정의
# =======================================================
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

# =======================================================
# 4. 데이터베이스 초기화 및 스키마 업데이트
# =======================================================
conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS scrapbook 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, link TEXT, summary TEXT, analysis TEXT, scrap_date TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS portfolio 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, stock_name TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS market_score_history 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, check_date TEXT, score INTEGER)''')
conn.commit()

# 신규 컬럼 동적 추가 (기존 사용 데이터 호환 목적)
for table, col, dtype in [
    ("portfolio", "search_query", "TEXT"), ("portfolio", "ticker", "TEXT"),
    ("portfolio", "is_owned", "INTEGER DEFAULT 0"), ("portfolio", "avg_price", "REAL DEFAULT 0.0"),
    ("portfolio", "quantity", "INTEGER DEFAULT 0"), ("scrapbook", "stock_name", "TEXT"),
    ("scrapbook", "ticker", "TEXT"), ("scrapbook", "saved_price", "REAL DEFAULT 0.0"),
    ("scrapbook", "target_price", "REAL DEFAULT 0.0"),
    ("scrapbook", "target_price_mid", "REAL DEFAULT 0.0"),
    ("scrapbook", "target_price_long", "REAL DEFAULT 0.0"),
    ("scrapbook", "buy_recommend_price", "REAL DEFAULT 0.0")
]:
    try: c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
    except: pass
conn.commit()

# =======================================================
# 5. 구글 드라이브 통합 데이터 다운로더 (에러 출력 수정본)
# =======================================================
@st.cache_data(ttl=60)
def fetch_global_data():
    try:
        info = json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
        creds = Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive'])
        drive_service = build('drive', 'v3', credentials=creds)
        
        folder_id = st.secrets.get("GOOGLE_REALTIME_FOLDER_ID", st.secrets.get("GOOGLE_FOLDER_ID", ""))
        if not folder_id:
            st.error("❌ 구글 드라이브 폴더 ID 설정이 누락되었습니다.")
            return None
            
        results = drive_service.files().list(
            q=f"'{folder_id}' in parents and name = 'market_data_latest.json' and trashed = false",
            fields="files(id)"
        ).execute()
        files = results.get('files', [])
        
        if not files: 
            st.error("❌ 지정된 폴더 내에서 'market_data_latest.json' 파일을 찾을 수 없습니다.")
            return None
            
        file_id = files[0]['id']
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False: 
            status, done = downloader.next_chunk()
            
        fh.seek(0)
        return json.loads(fh.read().decode('utf-8'))
    except Exception as e:
        st.error(f"❌ 구글 드라이브 로드 실패 상세 에러: {e}")
        return None

# =======================================================
# 6. Gemini AI 핵심 엔진 및 비동기 처리
# =======================================================
GEMINI_CONCURRENCY_LIMIT = 3
_gemini_semaphore = threading.Semaphore(GEMINI_CONCURRENCY_LIMIT)

def call_gemini_with_fallback(prompt, is_json=False):
    acquired = _gemini_semaphore.acquire(timeout=25)
    if not acquired: 
        return "{}" if is_json else "API 호출 대기 시간 초과"
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        model = 'gemini-3.5-flash' if not is_json else 'gemini-3.1-flash-lite'
        res = client.models.generate_content(model=model, contents=prompt).text
        return res if is_json else f"*(🤖 **AI 엔진:** `[{model}]`)*\n\n" + res
    except Exception as e:
        return "{}" if is_json else f"호출 실패: {e}"
    finally:
        _gemini_semaphore.release()

def call_gemini_stream_with_fallback(prompt):
    acquired = _gemini_semaphore.acquire(timeout=25)
    if not acquired:
        yield "호출 실패"
        return
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content_stream(model='gemini-3.5-flash', contents=prompt)
        yield "*(🤖 **AI 엔진:** `[gemini-3.5-flash]`)*\n\n"
        for chunk in response:
            if chunk.text: 
                yield chunk.text
    finally:
        _gemini_semaphore.release()

# =======================================================
# 7. AI 프롬프트 빌더
# =======================================================
def build_prompt_realtime(news_list, market_str):
    combined = "\n".join([f"- {n['title']} : {n['summary']}" for n in news_list])
    return f"최신 실시간 뉴스 {len(news_list)}건 종합 브리핑:\n[지표]: {market_str}\n{combined}\n\n1. 🔔 핵심 이슈 요약\n2. 📉 경제/증시 파급력\n3. 🎯 리스크 및 섹터"

def build_prompt_deep_dive(stock_name, cur_price, market_str):
    return (f"[{stock_name} 진단]\n"
            f"[시장 지표]\n{market_str}\n"
            f"[현재가]\n{cur_price:,.0f}원\n\n"
            f"위 데이터를 바탕으로 객관적인 진단 리포트를 작성하십시오.\n"
            f"1. 🏢 재무 및 펀더멘털 분석\n"
            f"2. 🌐 뉴스/수급 분석\n"
            f"3. 🎯 기간별 최종 적정 목표가 산출 논리\n"
            f"   - 🎯 단기 목표가 (1~3개월): [최종 가격]원 (1차 퀀트 연산 내역 -> 2차 정성 수정 반영)\n"
            f"   - 🎯 중기 목표가 (3~6개월): [최종 가격]원 (1차 퀀트 연산 내역 -> 2차 정성 수정 반영)\n"
            f"   - 🎯 장기 목표가 (1년 이상): [최종 가격]원 (1차 퀀트 연산 내역 -> 2차 정성 수정 반영)\n"
            f"4. 💰 매수 추천 타점: [진입가]원 (안전마진 및 지지선 기반)\n\n"
            f"※ 마지막 줄에 파싱을 위해 반드시 아래 포맷으로만 기재하십시오.\n"
            f"TARGET_PRICE: 단기숫자만|중기숫자만|장기숫자만|매수추천가숫자만")

def build_prompt_recommend_step3(news_list, market_str, horizon):
    combined = "\n".join([f"- {n['title']}" for n in news_list[:15]])
    return (f"당신은 엄격한 애널리스트입니다.\n"
            f"[시장 거시 상황]: {market_str}\n"
            f"[선택된 투자 기간]: {horizon}\n"
            f"[수급 및 이슈]:\n{combined}\n\n"
            f"가장 적합한 3개 종목을 엄선하여 보고서를 작성하십시오.\n"
            f"### 🏆 [최종 추천 종목 3개]\n"
            f"1. 🥇 추천종목: [종목명] (티커)\n"
            f"- 💡 추천 사유: (핵심 모멘텀 서술)\n"
            f"- 🎯 {horizon} 최종 목표가: [최종 가격]원\n"
            f"  └ 🧮 1차 퀀트 연산: [산출 가격]원 (공식 명시)\n"
            f"  └ 🧠 2차 정성 수정: (가감 논리 명시)\n"
            f"- 💰 진입 타점: [진입가]원\n\n"
            f"※ 반드시 마지막 줄에 파싱을 위해 아래 형식으로만 적으세요.\n"
            f"[TRACKING_DATA]\n"
            f"종목명1|티커1|최종목표가숫자만|진입타점숫자만\n"
            f"종목명2|티커2|최종목표가숫자만|진입타점숫자만\n"
            f"종목명3|티커3|최종목표가숫자만|진입타점숫자만")

# =======================================================
# 8. 대시보드 메인 UI 렌더링
# =======================================================
st.title("📊 Project2_Stock")

# 구글 드라이브로부터 통합 JSON 파일 단건 로드
g_data = fetch_global_data()
if not g_data:
    st.warning("🔄 백그라운드 봇이 아직 데이터를 수집 중이거나 구글 드라이브 세팅을 진행 중입니다. 잠시 후 강제 새로고침 해주세요.")
    st.stop()

st.caption(f"☁️ 구글 드라이브 최종 동기화 시각: {g_data.get('updated_at', '알 수 없음')}")

market_data_str = ", ".join([f"{k}: {v['current']:,.2f}({v['diff_pct']:+.2f}%)" for k, v in g_data.get("market_status", {}).items() if v.get('current', 0) > 0])

cols = st.columns(len(g_data.get("market_status", {})))
for i, (name, data) in enumerate(g_data.get("market_status", {}).items()):
    with cols[i]: 
        st.metric(label=name, value=f"{data['current']:,.2f}", delta=f"{data['diff']:,.2f} ({data['diff_pct']:.2f}%)")
st.divider()

# 탭 메뉴 정의
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["📰 실시간 브리핑", "🔥 핵심 경제", "📑 섹터 뉴스", "🎯 종목 발굴", "⭐️ 관심종목", "📁 스크랩북"])

# --- [탭 1: 실시간 브리핑] ---
with tab1:
    st.subheader("📰 실시간 경제·시사 뉴스 분석")
    news_list = g_data.get("realtime_news", [])
    if st.button("🤖 실시간 뉴스 기반 종합 분석", type="primary", use_container_width=True):
        st.session_state.realtime_analysis = st.write_stream(call_gemini_stream_with_fallback(build_prompt_realtime(news_list, market_data_str)))
    
    if st.session_state.get("realtime_analysis"):
        st.info(st.session_state.realtime_analysis)
        
    for news in news_list:
        with st.expander(f"🕒 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']}) | {news['published']}\n\n{news['summary']}")

# --- [탭 2: 핵심 경제 뉴스] ---
with tab2:
    st.subheader("今日 핵심 경제 뉴스")
    news_list = g_data.get("eco_news", [])
    
    if st.button("🤖 AI 종합 마켓 브리핑 생성", type="primary", use_container_width=True):
        res = call_gemini_with_fallback(f"주요 경제 뉴스 브리핑:\n[지표]: {market_data_str}\n" + "\n".join([n['title'] for n in news_list]))
        st.session_state.overall_analysis = res
        
    if st.session_state.get("overall_analysis"):
        st.info(st.session_state.overall_analysis)
        
    for news in news_list:
        with st.expander(f"📰 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']}) | {news['published']}\n\n{news['summary']}")

# --- [탭 3: 섹터 뉴스] ---
with tab3:
    st.subheader("📑 섹터별 핵심 비즈니스 뉴스")
    sectors_data = g_data.get("sectors", {})
    selected_sector = st.selectbox("관심 섹터 선택", list(sectors_data.keys()))
    
    for news in sectors_data.get(selected_sector, []):
        with st.expander(f"🏭 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']}) | {news['published']}\n\n{news['summary']}")

# --- [탭 4: 추천종목 발굴] ---
with tab4:
    st.subheader("🎯 AI 추천종목 발굴")
    investment_horizon = st.radio("⏳ 투자 기간 설정", ["단기 (1~3개월)", "중기 (3~6개월)", "장기 (1년 이상)"], horizontal=True)
    
    if st.button("🚀 추천 종목 발굴", type="primary", use_container_width=True):
        rec_news = g_data.get("realtime_news", []) + g_data.get("eco_news", [])
        prompt = build_prompt_recommend_step3(rec_news, market_data_str, investment_horizon)
        st.session_state.today_recommendation = st.write_stream(call_gemini_stream_with_fallback(prompt))

    if st.session_state.get('today_recommendation'):
        raw = st.session_state.today_recommendation
        st.write(raw.split("[TRACKING_DATA]")[0].strip())
        
        if "[TRACKING_DATA]" in raw:
            cols = st.columns(3)
            block = raw.split("[TRACKING_DATA]")[1].strip().replace("```", "")
            for idx, line in enumerate(block.split('\n')):
                data = line.split('|')
                if len(data) >= 4:
                    name, tick = data[0].strip(), data[1].strip()
                    
                    def extr(ix): 
                        return float(re.sub(r'[^\d.]', '', data[ix])) if len(data) > ix and re.sub(r'[^\d.]', '', data[ix]) else 0.0
                    tp, bp = extr(2), extr(3)

                    with cols[idx % 3]:
                        st.info(f"**{name}** ({tick})")
                        st.metric("🎯 최종 목표가", f"{tp:,.0f}원")
                        st.metric("💰 매수 추천가", f"{bp:,.0f}원")
                        if st.button(f"💾 찜하기", key=f"rec_s_{tick}"):
                            c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, buy_recommend_price, scrap_date) VALUES (?,?,?,?,?,?,?,?)", 
                                      (f"🎯 추천: {name}", raw, name, tick, 0.0, tp, bp, datetime.now().strftime("%Y-%m-%d %H:%M")))
                            conn.commit()
                            st.success("스크랩 완료!")

# --- [탭 5: 관심종목] ---
with tab5:
    st.subheader("⭐️ 관심종목 진단")
    with st.form("add_stock"):
        new_s = st.text_input("종목명 입력 (예: 카카오, 삼성전자)")
        c1, c2 = st.columns(2)
        avg_p = c1.text_input("평단가", value="0")
        qty = c2.number_input("수량", min_value=0, value=0)
        
        if st.form_submit_button("➕ 종목 등록") and new_s:
            try:
                final_avg_p = float(avg_p.replace(',', ''))
            except: 
                final_avg_p = 0.0
            c.execute("INSERT INTO portfolio (stock_name, is_owned, avg_price, quantity) VALUES (?,?,?,?)", 
                      (new_s.strip(), 1 if final_avg_p > 0 else 0, final_avg_p, qty))
            conn.commit()
            st.rerun()

    c.execute("SELECT id, stock_name, is_owned, avg_price, quantity FROM portfolio")
    for p in c.fetchall():
        p_id, name, is_owned, avg_price, quantity = p
        st.markdown(f"### 📌 [{name}]")
        
        col_info, col_btn = st.columns([3, 1])
        with col_info:
            if is_owned: 
                st.caption(f"💼 **보유** | 평단:{avg_price:,.0f} | 수량:{quantity}")
            else: 
                st.caption(f"👀 **관심**")
        
        with col_btn:
            cache_key = f"deep_{p_id}"
            if cache_key in st.session_state:
                if st.button("📊 진단 보기", key=f"view_{p_id}"): 
                    st.session_state[f"show_{p_id}"] = True
            else:
                if st.button("🚀 AI 진단", key=f"run_{p_id}", type="primary"):
                    report = call_gemini_with_fallback(build_prompt_deep_dive(name, 0.0, market_data_str))
                    
                    tp_match = re.search(r'TARGET_PRICE:\s*([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|(.*)', report)
                    def extr(s): 
                        return float(re.sub(r'[^\d.]', '', s)) if s and re.sub(r'[^\d.]', '', s) else 0.0
                    tp_s = extr(tp_match.group(1)) if tp_match else 0.0
                    tp_m = extr(tp_match.group(2)) if tp_match else 0.0
                    tp_l = extr(tp_match.group(3)) if tp_match else 0.0
                    bp_val = extr(tp_match.group(4)) if tp_match else 0.0

                    st.session_state[cache_key] = {"text": report, "tp_s": tp_s, "tp_m": tp_m, "tp_l": tp_l, "bp": bp_val}
                    st.session_state[f"show_{p_id}"] = True
                    st.rerun()

        if st.session_state.get(f"show_{p_id}") and cache_key in st.session_state:
            with st.expander("📝 AI 진단 리포트", expanded=True):
                rep = st.session_state[cache_key]['text']
                tp_s = st.session_state[cache_key]['tp_s']
                tp_m = st.session_state[cache_key]['tp_m']
                tp_l = st.session_state[cache_key]['tp_l']
                bp_val = st.session_state[cache_key]['bp']

                st.info(f"**단기:** {tp_s:,.0f}원  |  **중기:** {tp_m:,.0f}원  |  **장기:** {tp_l:,.0f}원  |  **💰매수추천:** {bp_val:,.0f}원")
                st.write(re.sub(r'TARGET_PRICE:.*', '', rep).strip())
                
                c1, c2 = st.columns(2)
                if c1.button("💾 저장", key=f"save_{p_id}"):
                    c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date, stock_name, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)", 
                              (f"[{name}] 리포트", "진단", rep, datetime.now().strftime("%Y-%m-%d %H:%M"), name, 0.0, tp_s, tp_m, tp_l, bp_val))
                    conn.commit()
                    st.success("저장 완료")
                if c2.button("🗑️ 삭제", key=f"del_{p_id}"):
                    c.execute("DELETE FROM portfolio WHERE id=?", (p_id,))
                    conn.commit()
                    st.rerun()
        st.divider()

# --- [탭 6: 스크랩북] ---
with tab6:
    st.subheader("📁 내 스크랩북")
    c.execute("SELECT id, title, analysis, scrap_date, stock_name, saved_price, target_price, buy_recommend_price, target_price_mid, target_price_long FROM scrapbook ORDER BY id DESC")
    scraps = c.fetchall()
    
    col_ctrl1, col_ctrl2 = st.columns([1, 4])
    with col_ctrl1:
        if st.button("🗑️ 선택 삭제", type="primary", use_container_width=True):
            to_delete = [sid for sid, checked in st.session_state.items() if sid.startswith("chk_") and checked]
            if to_delete:
                ids = [int(sid.split("_")[1]) for sid in to_delete]
                c.executemany("DELETE FROM scrapbook WHERE id=?", [(i,) for i in ids])
                conn.commit()
                for sid in to_delete: 
                    st.session_state.pop(sid, None)
                st.rerun()
                
    for s in scraps:
        scrap_id, title, analysis, scrap_date, stock_name, saved_price = s[0], s[1], s[2], s[3], s[4], float(s[5] or 0)
        tp_s, b_rec, tp_m, tp_l = float(s[6] or 0), float(s[7] or 0), float(s[8] or 0), float(s[9] or 0)
        
        col_chk, col_exp = st.columns([0.05, 0.95])
        with col_chk:
            st.markdown("<br>", unsafe_allow_html=True)
            st.checkbox("", key=f"chk_{scrap_id}", label_visibility="collapsed")
        with col_exp:
            with st.expander(f"[{scrap_date}] {title}"):
                cols_sc = st.columns(4)
                cols_sc[0].metric("저장가", f"{saved_price:,.0f}원")
                if tp_m > 0 or tp_l > 0:
                    cols_sc[1].markdown(f"**🎯 밴드**<br>단기: {tp_s:,.0f}<br>중기: {tp_m:,.0f}<br>장기: {tp_l:,.0f}", unsafe_allow_html=True)
                else:
                    cols_sc[1].metric("🎯 목표가", f"{tp_s:,.0f}원")
                cols_sc[2].metric("💰 매수 추천", f"{b_rec:,.0f}원" if b_rec > 0 else "기록 없음")
                st.divider()
                st.write(analysis)
