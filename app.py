import streamlit as st
import json
import sqlite3
import re
import io
import threading
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google import genai

# =======================================================
# 1. 페이지 설정 및 비밀번호 로그인
# =======================================================
st.set_page_config(page_title="Project2_Stock", page_icon="📊", layout="wide")

def check_password():
    if "pwd" in st.query_params:
        if st.query_params["pwd"] == st.secrets["APP_PASSWORD"]:
            st.session_state["password_correct"] = True

    if st.session_state.get("password_correct", False):
        return True

    st.title("🔒 Project2_Stock 로그인")
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

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

# =======================================================
# 2. 로컬 데이터베이스 초기화 및 스키마 업데이트
# =======================================================
conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS scrapbook 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, link TEXT, summary TEXT, analysis TEXT, scrap_date TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS portfolio 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, stock_name TEXT)''')
conn.commit()

columns_to_add = [
    ("portfolio", "is_owned", "INTEGER DEFAULT 0"),
    ("portfolio", "avg_price", "REAL DEFAULT 0.0"),
    ("portfolio", "quantity", "INTEGER DEFAULT 0"),
    ("portfolio", "report_text", "TEXT"),
    ("portfolio", "tp_s", "REAL DEFAULT 0.0"),
    ("portfolio", "tp_m", "REAL DEFAULT 0.0"),
    ("portfolio", "tp_l", "REAL DEFAULT 0.0"),
    ("portfolio", "bp", "REAL DEFAULT 0.0"),
    ("scrapbook", "stock_name", "TEXT"),
    ("scrapbook", "ticker", "TEXT"),
    ("scrapbook", "saved_price", "REAL DEFAULT 0.0"),
    ("scrapbook", "target_price", "REAL DEFAULT 0.0"),
    ("scrapbook", "target_price_mid", "REAL DEFAULT 0.0"),
    ("scrapbook", "target_price_long", "REAL DEFAULT 0.0"),
    ("scrapbook", "buy_recommend_price", "REAL DEFAULT 0.0")
]

for table, col, dtype in columns_to_add:
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
        conn.commit()
    except sqlite3.OperationalError:
        pass

# =======================================================
# 3. 구글 드라이브 데이터 로더 (서비스 계정 - 읽기 전용)
#    ※ OAuth 수동 로그인/백업 기능은 제거되었습니다.
#      데이터는 별도 백엔드(람다)가 서비스 계정으로 이미 적재해두고,
#      이 앱은 그 결과를 읽기만 합니다. 사용자 로그인이 필요 없습니다.
# =======================================================
@st.cache_data(ttl=60)
def fetch_global_data():
    try:
        info = json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
        creds = Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive.readonly'])
        drive_service = build('drive', 'v3', credentials=creds)
        folder_id = st.secrets.get("GOOGLE_REALTIME_FOLDER_ID", "")

        results = drive_service.files().list(
            q=f"'{folder_id}' in parents and name = 'market_data_latest.json' and trashed = false",
            fields="files(id)"
        ).execute()
        files = results.get('files', [])

        if not files:
            return None

        request = drive_service.files().get_media(fileId=files[0]['id'])
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        fh.seek(0)
        return json.loads(fh.read().decode('utf-8'))
    except Exception as e:
        st.error(f"❌ 데이터 로드 에러: {e}")
        return None

# =======================================================
# 4. Gemini AI 처리 모델 설정
# =======================================================
GEMINI_CONCURRENCY_LIMIT = 3
_gemini_semaphore = threading.Semaphore(GEMINI_CONCURRENCY_LIMIT)

def call_gemini_with_fallback(prompt):
    acquired = _gemini_semaphore.acquire(timeout=25)
    if not acquired:
        return "API 호출 대기 시간 초과"
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        return client.models.generate_content(model='gemini-3.5-flash', contents=prompt).text
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
        for chunk in client.models.generate_content_stream(model='gemini-3.5-flash', contents=prompt):
            if chunk.text:
                yield chunk.text
    finally:
        _gemini_semaphore.release()

def build_prompt_deep_dive(stock_name, market_str):
    return (f"[{stock_name} 진단]\n[시장 지표]\n{market_str}\n\n위 데이터를 바탕으로 객관적인 진단 리포트를 작성하십시오.\n"
            f"1. 🏢 재무 및 펀더멘털 분석\n2. 🌐 뉴스/수급 분석\n"
            f"※ 반드시 마지막 줄에 파싱을 위해 아래 형식으로만 적으세요.\n"
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
# 5. 메인 대시보드 렌더링
# =======================================================
st.title("📊 Project2_Stock")

g_data = fetch_global_data()
if not g_data:
    st.warning("🔄 데이터를 불러오고 있습니다. 잠시만 대기해 주세요.")
    st.stop()

st.caption(f"☁️ 최종 동기화 시각: {g_data.get('updated_at', '알 수 없음')}")

market_data = g_data.get("market_status", {})
market_data_str = ", ".join([f"{k}: {v['current']}({v['diff_pct']}%)" for k, v in market_data.items() if v.get('current', 0) > 0])

# 메인 지표 카드 영역
target_indices = ["코스피", "코스닥", "S&P 500", "원/달러 환율"]
cols = st.columns(4)
for i, key in enumerate(target_indices):
    with cols[i]:
        if key in market_data:
            data = market_data[key]
            val = data.get("current", 0.0)
            diff = data.get("diff", 0.0)
            diff_pct = data.get("diff_pct", 0.0)

            if val == 0.0:
                st.metric(label=key, value="수집 오류", delta="데이터 점검중", delta_color="off")
            else:
                if key == "원/달러 환율":
                    st.metric(label=key, value=f"{val:,.2f}원", delta=f"{diff:+.2f}원 ({diff_pct:+.2f}%)")
                else:
                    st.metric(label=key, value=f"{val:,.2f}", delta=f"{diff:+.2f} ({diff_pct:+.2f}%)")
        else:
            st.metric(label=key, value="대기중", delta="-")

st.divider()

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["📰 실시간 브리핑", "🔥 핵심 경제", "📑 섹터 뉴스", "🎯 종목 발굴", "⭐️ 관심종목", "📁 스크랩북"])

with tab1:
    st.subheader("📰 실시간 경제·시사 뉴스 분석")
    news_list = g_data.get("realtime_news", [])

    if st.button("🤖 실시간 뉴스 기반 종합 분석", type="primary", use_container_width=True, key="btn_realtime"):
        prompt = f"최신 뉴스 브리핑:\n[지표]: {market_data_str}\n" + "\n".join([n['title'] for n in news_list])
        with st.spinner("AI가 뉴스를 분석하고 있습니다..."):
            st.session_state.realtime_analysis = "".join(call_gemini_stream_with_fallback(prompt))

    if st.session_state.get("realtime_analysis"):
        with st.expander("🤖 AI 분석 결과", expanded=True):
            st.write(st.session_state.realtime_analysis)

    for news in news_list:
        with st.expander(f"🕒 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']})\n\n{news['summary']}")

with tab2:
    st.subheader("今日 핵심 경제 뉴스")
    for news in g_data.get("eco_news", []):
        with st.expander(f"📰 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']})\n\n{news['summary']}")

with tab3:
    st.subheader("📑 섹터 뉴스")
    sectors_data = g_data.get("sectors", {})
    if sectors_data:
        selected_sector = st.selectbox("관심 섹터 선택", list(sectors_data.keys()))
        for news in sectors_data.get(selected_sector, []):
            with st.expander(f"🏭 {news['title']}"):
                st.write(news['summary'])

with tab4:
    st.subheader("🎯 AI 추천종목 발굴")
    investment_horizon = st.radio("⏳ 투자 기간 설정", ["단기 (1~3개월)", "중기 (3~6개월)", "장기 (1년 이상)"], horizontal=True)

    if st.button("🚀 추천 종목 발굴", type="primary", use_container_width=True, key="btn_recommend"):
        rec_news = g_data.get("realtime_news", []) + g_data.get("eco_news", [])
        prompt = build_prompt_recommend_step3(rec_news, market_data_str, investment_horizon)
        with st.spinner("AI가 종목을 발굴하고 있습니다..."):
            st.session_state.today_recommendation = "".join(call_gemini_stream_with_fallback(prompt))

    if st.session_state.get('today_recommendation'):
        raw = st.session_state.today_recommendation
        with st.expander("🤖 AI 추천 리포트", expanded=True):
            st.write(raw.split("[TRACKING_DATA]")[0].strip())

            if "[TRACKING_DATA]" in raw:
                cols_rec = st.columns(3)
                block = raw.split("[TRACKING_DATA]")[1].strip().replace("```", "")
                for idx, line in enumerate(block.split('\n')):
                    if not line.strip():
                        continue
                    data = line.split('|')
                    if len(data) >= 4:
                        name, tick = data[0].strip(), data[1].strip()
                        def extr(ix):
                            val_str = re.sub(r'[^\d.]', '', data[ix])
                            return float(val_str) if val_str else 0.0
                        tp, bp = extr(2), extr(3)

                        with cols_rec[idx % 3]:
                            st.info(f"**{name}** ({tick})")
                            st.metric("🎯 최종 목표가", f"{tp:,.0f}원")
                            st.metric("💰 매수 추천가", f"{bp:,.0f}원")
                            if st.button(f"💾 찜하기", key=f"rec_s_{tick}"):
                                c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, buy_recommend_price, scrap_date) VALUES (?,?,?,?,?,?,?,?)",
                                          (f"🎯 추천: {name}", raw, name, tick, 0.0, tp, bp, datetime.now().strftime("%Y-%m-%d %H:%M")))
                                conn.commit()
                                st.success("스크랩 완료!")

with tab5:
    st.subheader("⭐️ 관심종목 진단")
    with st.form("add_stock"):
        new_s = st.text_input("종목명 입력 (예: 삼성전자, 카카오)")
        c1, c2 = st.columns(2)
        avg_p = c1.text_input("평단가", value="0")
        qty = c2.number_input("수량", min_value=0, value=0)
        if st.form_submit_button("➕ 종목 등록") and new_s:
            try:
                final_avg_p = float(avg_p.replace(',', ''))
            except:
                final_avg_p = 0.0
            c.execute("INSERT INTO portfolio (stock_name, is_owned, avg_price, quantity) VALUES (?,?,?,?)", (new_s.strip(), 1 if final_avg_p > 0 else 0, final_avg_p, qty))
            conn.commit()
            st.rerun()

    c.execute("SELECT id, stock_name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp FROM portfolio")
    portfolios = c.fetchall()

    for p in portfolios:
        p_id, name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp = p
        st.markdown(f"### 📌 [{name}]")
        col_info, col_btn = st.columns([3, 1])
        with col_info:
            if is_owned:
                st.caption(f"💼 **보유** | 평단:{avg_price:,.0f} | 수량:{quantity}")
            else:
                st.caption(f"👀 **관심**")
        with col_btn:
            if st.button("🔄 새로운 보고서 내기", key=f"run_{p_id}", type="primary"):
                with st.spinner("AI가 진단하고 있습니다..."):
                    report = call_gemini_with_fallback(build_prompt_deep_dive(name, market_data_str))
                tp_match = re.search(r'TARGET_PRICE:\s*([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|(.*)', report)
                def extr(s):
                    val_str = re.sub(r'[^\d.]', '', s) if s else ""
                    return float(val_str) if val_str else 0.0
                n_tp_s = extr(tp_match.group(1)) if tp_match else 0.0
                n_tp_m = extr(tp_match.group(2)) if tp_match else 0.0
                n_tp_l = extr(tp_match.group(3)) if tp_match else 0.0
                n_bp = extr(tp_match.group(4)) if tp_match else 0.0

                c.execute("UPDATE portfolio SET report_text=?, tp_s=?, tp_m=?, tp_l=?, bp=? WHERE id=?", (report, n_tp_s, n_tp_m, n_tp_l, n_bp, p_id))
                conn.commit()
                st.rerun()

        if report_text:
            with st.expander("📝 AI 진단 리포트", expanded=True):
                st.info(f"**단기:** {tp_s:,.0f}원  |  **중기:** {tp_m:,.0f}원  |  **장기:** {tp_l:,.0f}원  |  **💰매수추천:** {bp:,.0f}원")
                st.write(re.sub(r'TARGET_PRICE:.*', '', report_text).strip())
                if st.button("💾 스크랩북에 저장", key=f"save_{p_id}"):
                    c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date, stock_name, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (f"[{name}] 리포트", "진단", report_text, datetime.now().strftime("%Y-%m-%d %H:%M"), name, 0.0, tp_s, tp_m, tp_l, bp))
                    conn.commit()
                    st.success("저장 완료")
        st.divider()

    if portfolios:
        st.subheader("🗑️ 관심종목 삭제")
        to_delete = st.multiselect("삭제할 종목을 선택하세요", [name for _, name, _, _, _, _, _, _, _, _ in portfolios])
        if st.button("선택 종목 삭제", type="primary"):
            for d_name in to_delete:
                c.execute("DELETE FROM portfolio WHERE stock_name=?", (d_name,))
            conn.commit()
            st.rerun()

with tab6:
    st.subheader("📁 내 스크랩북")
    c.execute("SELECT id, title, analysis, scrap_date, stock_name, saved_price, target_price, buy_recommend_price, target_price_mid, target_price_long FROM scrapbook ORDER BY id DESC")
    scraps = c.fetchall()

    col_ctrl1, _ = st.columns([1, 4])
    with col_ctrl1:
        if st.button("🗑️ 선택 삭제", type="primary", use_container_width=True):
            to_delete_ids = [sid.split("_")[1] for sid, checked in st.session_state.items() if sid.startswith("chk_") and checked]
            if to_delete_ids:
                c.executemany("DELETE FROM scrapbook WHERE id=?", [(int(i),) for i in to_delete_ids])
                conn.commit()
                for sid in list(st.session_state.keys()):
                    if sid.startswith("chk_"):
                        st.session_state.pop(sid)
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
                cols_sc[1].markdown(f"**🎯 밴드**<br>단기: {tp_s:,.0f}<br>중기: {tp_m:,.0f}<br>장기: {tp_l:,.0f}", unsafe_allow_html=True)
                cols_sc[2].metric("💰 매수 추천", f"{b_rec:,.0f}원" if b_rec > 0 else "기록 없음")
                st.write(analysis)
