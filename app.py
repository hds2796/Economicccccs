import streamlit as st
import json
import sqlite3
import re
import threading
import requests
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google import genai

MODEL_NAME = "gemini-3.5-flash"

st.set_page_config(page_title="Project2_Stock", page_icon="📊", layout="wide")

def check_password():
    if "pwd" in st.query_params:
        if st.query_params["pwd"] == st.secrets["APP_PASSWORD"]: st.session_state["password_correct"] = True
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

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
API_GATEWAY_REALTIME_URL = st.secrets.get("API_GATEWAY_REALTIME_URL", "")

conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS scrapbook (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, link TEXT, summary TEXT, analysis TEXT, scrap_date TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS portfolio (id INTEGER PRIMARY KEY AUTOINCREMENT, stock_name TEXT)''')
conn.commit()

columns_to_add = [
    ("portfolio", "is_owned", "INTEGER DEFAULT 0"), ("portfolio", "avg_price", "REAL DEFAULT 0.0"),
    ("portfolio", "quantity", "INTEGER DEFAULT 0"), ("portfolio", "report_text", "TEXT"),
    ("portfolio", "tp_s", "REAL DEFAULT 0.0"), ("portfolio", "tp_m", "REAL DEFAULT 0.0"),
    ("portfolio", "tp_l", "REAL DEFAULT 0.0"), ("portfolio", "bp", "REAL DEFAULT 0.0"),
    ("scrapbook", "stock_name", "TEXT"), ("scrapbook", "ticker", "TEXT"),
    ("scrapbook", "saved_price", "REAL DEFAULT 0.0"), ("scrapbook", "target_price", "REAL DEFAULT 0.0"),
    ("scrapbook", "target_price_mid", "REAL DEFAULT 0.0"), ("scrapbook", "target_price_long", "REAL DEFAULT 0.0"),
    ("scrapbook", "buy_recommend_price", "REAL DEFAULT 0.0"), ("portfolio", "model_used", "TEXT"),
    ("portfolio", "report_time", "TEXT"), ("portfolio", "ticker", "TEXT"), ("scrapbook", "model_used", "TEXT")
]
for table, col, dtype in columns_to_add:
    try: c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}"); conn.commit()
    except: pass

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
        import io
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: status, done = downloader.next_chunk()
        fh.seek(0)
        return json.loads(fh.read().decode('utf-8'))
    except Exception as e:
        st.error(f"❌ 캐시 데이터 로드 에러: {e}")
        return None

# POST 방식으로 변경. 이미 본 링크 리스트를 서버로 보냅니다.
def fetch_realtime_data_direct(seen_links):
    if not API_GATEWAY_REALTIME_URL:
        st.error("❌ secrets.toml에 API_GATEWAY_REALTIME_URL이 설정되지 않았습니다.")
        return None
    try:
        payload = {"seen_links": list(seen_links)}
        res = requests.post(API_GATEWAY_REALTIME_URL, json=payload, timeout=20)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        st.error(f"❌ 실시간 데이터 갱신 실패: {e}")
        return None

GEMINI_CONCURRENCY_LIMIT = 3
_gemini_semaphore = threading.Semaphore(GEMINI_CONCURRENCY_LIMIT)

def call_gemini_with_fallback(prompt, model=MODEL_NAME):
    acquired = _gemini_semaphore.acquire(timeout=25)
    if not acquired: return "API 호출 대기 시간 초과"
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        return client.models.generate_content(model=model, contents=prompt).text
    except Exception as e: return f"호출 실패: {e}"
    finally: _gemini_semaphore.release()

def call_gemini_stream_with_fallback(prompt):
    acquired = _gemini_semaphore.acquire(timeout=25)
    if not acquired: yield "API 호출 대기 시간 초과"; return
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        for chunk in client.models.generate_content_stream(model=MODEL_NAME, contents=prompt):
            if chunk.text: yield chunk.text
    finally: _gemini_semaphore.release()

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
    if not name: return None, None
    try:
        url = f"https://m.stock.naver.com/front-api/search/autoComplete?query={requests.utils.quote(name)}&target=stock,index,marketindicator,coin,ipo"
        res = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"}).json()
        items = (res.get("result") or {}).get("items", [])
        stock_items = [i for i in items if i.get("typeName") in ("코스피", "코스닥")] or items
        if not stock_items: return None, None
        return stock_items[0].get("code"), stock_items[0].get("name")
    except: return None, None

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
    except: return {}

def dedupe_news(news_list):
    seen = set(); out = []
    for n in news_list or []:
        key = n.get("link") or n.get("title")
        if not key or key in seen: continue
        seen.add(key); out.append(n)
    return out

def build_prompt_deep_dive(stock_name, market_str):
    return (f"[{stock_name} 진단]\n[시장 지표]\n{market_str}\n\n위 데이터를 바탕으로 객관적인 진단 리포트를 작성하십시오.\n"
            f"1. 🏢 재무 및 펀더멘털 분석\n2. 🌐 뉴스/수급 분석\n"
            f"※ 반드시 마지막 줄에 파싱을 위해 아래 형식으로만 적으세요. 가격은 단위 없이 순수 숫자만 적으세요.\n"
            f"TARGET_PRICE: 단기숫자만|중기숫자만|장기숫자만|매수추천가숫자만")

def build_prompt_recommend_step3(news_list, market_str, horizon):
    combined = "\n".join([f"- [발행일: {n.get('published', '알수없음')}] {n['title']}\n  요약: {n.get('summary', '(요약 없음)')}" for n in news_list[:100]])
    return (f"당신은 엄격한 애널리스트입니다.\n[시장 거시 상황]: {market_str}\n[선택된 투자 기간]: {horizon}\n[전달된 뉴스 목록]:\n{combined}\n\n"
            f"※ 중요 지시사항:\n1. 50개를 엄선하여 분석.\n2. {horizon} 관점 추천 종목 3개 작성.\n\n"
            f"### 🏆 [최종 추천 종목 3개]\n1. 🥇 추천종목: [종목명] (티커)\n- 💡 추천 사유: (핵심 모멘텀 서술)\n"
            f"- 🎯 {horizon} 최종 목표가: [최종 가격]원\n  └ 🧮 1차 퀀트 연산: [산출 가격]원\n  └ 🧠 2차 정성 수정: (가감 논리 명시)\n- 💰 진입 타점: [진입가]원\n\n"
            f"※ 마지막 줄은 반드시 아래 형식으로만.\n[TRACKING_DATA]\n종목명1|티커1|최종목표가숫자만|진입타점숫자만\n종목명2|티커2|최종목표가숫자만|진입타점숫자만\n종목명3|티커3|최종목표가숫자만|진입타점숫자만")

st.title("📊 Project2_Stock")

# 세션 상태 초기화
if "seen_realtime_links" not in st.session_state:
    st.session_state.seen_realtime_links = set()
if "realtime_cache" not in st.session_state:
    st.session_state.realtime_cache = None
if "eco_display_limit" not in st.session_state:
    st.session_state.eco_display_limit = 10

cached_data = fetch_cached_global_data()

# ⭐️ 처음 들어갈 때 자동으로 실시간 데이터 즉시 로딩
if st.session_state.realtime_cache is None:
    with st.spinner("AI가 가십성 뉴스를 걸러내고 시장 핵심 뉴스를 로딩 중입니다..."):
        new_data = fetch_realtime_data_direct(st.session_state.seen_realtime_links)
        if new_data:
            st.session_state.realtime_cache = new_data
            for n in new_data.get("realtime_news", []):
                st.session_state.seen_realtime_links.add(n['link'])

g_data = st.session_state.realtime_cache

col_title, col_refresh = st.columns([5, 1.2])
with col_refresh:
    # ⭐️ 새로고침 버튼 (이미 본 기사는 제외하고 새로운 것만 요청)
    if st.button("🔄 실시간 뉴스 새로고침", use_container_width=True):
        with st.spinner("새로운 뉴스를 탐색 중입니다..."):
            new_data = fetch_realtime_data_direct(st.session_state.seen_realtime_links)
            if new_data:
                new_news = new_data.get("realtime_news", [])
                
                # 새로 뜬 뉴스가 없는 경우 알림
                if not new_news:
                    st.info("💡 새로운 뉴스가 없습니다. 잠시 후 다시 시도해주세요.")
                else:
                    # 새로운 뉴스로 교체 및 본 링크 목록 업데이트
                    st.session_state.realtime_cache = new_data
                    for n in new_news:
                        st.session_state.seen_realtime_links.add(n['link'])
                    st.success(f"{len(new_news)}개의 새로운 뉴스가 업데이트 되었습니다!")
                st.rerun()

if not g_data or not cached_data:
    st.stop()

with col_title:
    st.caption(f"⚡ 실시간 갱신: {g_data.get('updated_at', '알 수 없음')} | ☁️ 종합 30분 캐시: {cached_data.get('updated_at', '알 수 없음')}")

market_data = g_data.get("market_status", {})
market_data_str = ", ".join([f"{k}: {v['current']}({v['diff_pct']}%)" for k, v in market_data.items() if v.get('current', 0) > 0])

target_indices = ["코스피", "코스닥", "S&P 500", "원/달러 환율"]
cols = st.columns(4)
for i, key in enumerate(target_indices):
    with cols[i]:
        if key in market_data:
            data = market_data[key]
            val, diff, diff_pct = data.get("current", 0.0), data.get("diff", 0.0), data.get("diff_pct", 0.0)
            if val == 0.0: st.metric(label=key, value="수집 오류", delta="점검중", delta_color="off")
            elif key == "원/달러 환율": st.metric(label=key, value=f"{val:,.2f}원", delta=f"{diff:+.2f}원 ({diff_pct:+.2f}%)")
            else: st.metric(label=key, value=f"{val:,.2f}", delta=f"{diff:+.2f} ({diff_pct:+.2f}%)")
        else:
            st.metric(label=key, value="대기중", delta="-")

st.divider()

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["📰 실시간 브리핑", "🔥 핵심 경제", "📑 섹터 뉴스", "🎯 종목 발굴", "⭐️ 관심종목", "📁 스크랩북"])

with tab1:
    st.subheader("📰 실시간 경제·시사 뉴스 분석 (AI 가십 노이즈 필터링 적용)")
    news_list = dedupe_news(g_data.get("realtime_news", []))

    if not news_list:
         st.write("새로 표시할 최신 실시간 뉴스가 없습니다.")

    if st.button("🤖 실시간 뉴스 기반 종합 분석", type="primary", use_container_width=True, key="btn_realtime"):
        articles_str = "\n".join([f"- [발행일: {n.get('published', '알수없음')}] {n['title']}\n  요약: {n.get('summary', '(요약 없음)')}" for n in news_list[:100]])
        prompt = (f"최신 뉴스 브리핑:\n[지표]: {market_data_str}\n\n[기사 목록 ({len(news_list[:100])}건)]\n{articles_str}\n\n"
                  f"※ 중요 지시사항:\n가장 핵심적인 20개의 기사를 선별하여 종합 실시간 경제 분석 보고서를 작성하십시오.")
        with st.spinner("AI가 뉴스를 분석하고 있습니다..."):
            st.session_state.realtime_analysis = "".join(call_gemini_stream_with_fallback(prompt))
            st.session_state.realtime_analysis_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    if st.session_state.get("realtime_analysis"):
        with st.expander("🤖 AI 분석 결과", expanded=True):
            st.write(st.session_state.realtime_analysis)
            st.caption(f"🧠 생성 모델: {MODEL_NAME} · {st.session_state.get('realtime_analysis_time', '')}")

    for news in news_list:
        with st.expander(f"🕒 {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']})\n\n**발행일**: {news.get('published', '알수없음')}\n\n{news['summary']}")

with tab2:
    st.subheader("今日 핵심 경제 뉴스 (30분 주기 AI 채점순 정렬)")
    eco_news_list = dedupe_news(cached_data.get("eco_news", []))
    
    current_limit = st.session_state.eco_display_limit

    if st.button("🤖 핵심 경제 뉴스 종합 분석 (상위 50개 대상)", type="primary", use_container_width=True, key="btn_eco"):
        articles_str = "\n".join([f"- [발행일: {n.get('published', '알수없음')}] {n['title']}\n  요약: {n.get('summary', '(요약 없음)')}" for n in eco_news_list[:100]])
        prompt = (f"오늘의 핵심 경제 뉴스 브리핑:\n[지표]: {market_data_str}\n\n[기사 목록 ({len(eco_news_list[:100])}건)]\n{articles_str}\n\n"
                  f"※ 중요 지시사항:\n가장 핵심적인 50개의 기사를 선별하여 깊이 있는 종합 거시경제 분석을 수행하십시오.")
        with st.spinner("AI가 뉴스를 분석하고 있습니다..."):
            st.session_state.eco_analysis = "".join(call_gemini_stream_with_fallback(prompt))
            st.session_state.eco_analysis_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    if st.session_state.get("eco_analysis"):
        with st.expander("🤖 AI 분석 결과", expanded=True):
            st.write(st.session_state.eco_analysis)
            st.caption(f"🧠 생성 모델: {MODEL_NAME} · {st.session_state.get('eco_analysis_time', '')}")

    for news in eco_news_list[:current_limit]:
        with st.expander(f"🔥 [투자 가치 상위] {news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']})\n\n**발행일**: {news.get('published', '알수없음')}\n\n{news['summary']}")
            
    if current_limit < len(eco_news_list):
        if st.button("➕ 뉴스 더보기", use_container_width=True):
            st.session_state.eco_display_limit += 10
            st.rerun()

with tab3:
    st.subheader("📑 섹터 뉴스 (실시간 갱신)")
    sectors_data = g_data.get("sectors", {})
    if sectors_data:
        selected_sector = st.selectbox("관심 섹터 선택", list(sectors_data.keys()))
        sector_news = dedupe_news(sectors_data.get(selected_sector, []))

        if st.button(f"🤖 {selected_sector} 섹터 분석", type="primary", use_container_width=True, key=f"btn_sector_{selected_sector}"):
            articles_str = "\n".join([f"- {n['title']}\n  요약: {n.get('summary', '(요약 없음)')}" for n in sector_news])
            prompt = f"[{selected_sector}] 섹터 뉴스 분석:\n[지표]: {market_data_str}\n\n[기사 목록 ({len(sector_news)}건)]\n{articles_str}"
            with st.spinner(f"AI가 {selected_sector} 섹터를 분석하고 있습니다..."):
                st.session_state[f"sector_analysis_{selected_sector}"] = "".join(call_gemini_stream_with_fallback(prompt))
                st.session_state[f"sector_analysis_time_{selected_sector}"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        if st.session_state.get(f"sector_analysis_{selected_sector}"):
            with st.expander(f"🤖 {selected_sector} AI 분석 결과", expanded=True):
                st.write(st.session_state[f"sector_analysis_{selected_sector}"])
                st.caption(f"🧠 생성 모델: {MODEL_NAME} · {st.session_state.get(f'sector_analysis_time_{selected_sector}', '')}")

        for news in sector_news[:10]:
            with st.expander(f"🏭 {news['title']}"):
                st.write(news['summary'])

with tab4:
    st.subheader("🎯 AI 추천종목 발굴")
    investment_horizon = st.radio("⏳ 투자 기간 설정", ["단기 (1~3개월)", "중기 (3~6개월)", "장기 (1년 이상)"], horizontal=True)

    if st.button("🚀 추천 종목 발굴", type="primary", use_container_width=True, key="btn_recommend"):
        rec_news = dedupe_news(g_data.get("realtime_news", []) + cached_data.get("eco_news", []))
        prompt = build_prompt_recommend_step3(rec_news, market_data_str, investment_horizon)
        with st.spinner("AI가 종목을 발굴하고 있습니다..."):
            st.session_state.today_recommendation = "".join(call_gemini_stream_with_fallback(prompt))
            st.session_state.today_recommendation_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    if st.session_state.get('today_recommendation'):
        raw = st.session_state.today_recommendation
        with st.expander("🤖 AI 추천 리포트", expanded=True):
            st.write(raw.split("[TRACKING_DATA]")[0].strip())
            st.caption(f"🧠 생성 모델: {MODEL_NAME} · {st.session_state.get('today_recommendation_time', '')}")

            if "[TRACKING_DATA]" in raw:
                block = raw.split("[TRACKING_DATA]")[1].strip().replace("```", "")
                parsed_rows = []
                for line in block.split('\n'):
                    if not line.strip(): continue
                    data = line.split('|')
                    if len(data) >= 4: parsed_rows.append((data[0].strip(), data[1].strip(), parse_won(data[2]), parse_won(data[3])))

                price_map = fetch_current_prices([r[1] for r in parsed_rows])
                cols_rec = st.columns(3)
                for idx, (name, tick, tp, bp) in enumerate(parsed_rows):
                    code = re.sub(r'[^\d]', '', tick)
                    price_info = price_map.get(code, {})
                    current, diff, diff_pct = price_info.get("current", 0.0), price_info.get("diff", 0.0), price_info.get("diff_pct", 0.0)

                    with cols_rec[idx % 3]:
                        with st.container(border=True):
                            st.markdown(f"**{name}** `{tick}`")
                            if current > 0: st.metric("현재가", f"{current:,.0f}원", delta=f"{diff:+,.0f}원 ({diff_pct:+.2f}%)")
                            else: st.metric("현재가", "조회 실패", delta="코드 확인 필요", delta_color="off")
                            
                            c_tp, c_bp = st.columns(2)
                            c_tp.metric("🎯 목표가", f"{tp:,.0f}원")
                            c_bp.metric("💰 매수 추천가", f"{bp:,.0f}원")

                            if current > 0 and tp > 0:
                                gap_pct = (tp - current) / current * 100
                                if gap_pct >= 0: st.caption(f"🎯 목표가까지 **+{gap_pct:.1f}%** 남음")
                                else: st.caption(f"🎯 목표가 대비 **{gap_pct:.1f}%** 초과 달성")

                            if st.button("💾 찜하기", key=f"rec_s_{tick}", use_container_width=True):
                                c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, buy_recommend_price, scrap_date, model_used) VALUES (?,?,?,?,?,?,?,?,?)",
                                          (f"🎯 추천: {name}", raw, name, tick, current, tp, bp, datetime.now().strftime("%Y-%m-%d %H:%M"), MODEL_NAME))
                                conn.commit(); st.success("스크랩 완료!")

with tab5:
    st.subheader("⭐️ 관심종목 진단")
    own_status = st.radio("보유 상태", ["👀 미보유 (관심만)", "💼 보유"], horizontal=True, key="add_own_status")
    with st.form("add_stock"):
        new_s = st.text_input("종목명 입력 (예: 삼성전자, 카카오)")
        c2, c3 = st.columns(2)
        avg_p = c2.text_input("평단가", value="0", disabled=(own_status.startswith("👀")))
        qty = c3.number_input("수량", min_value=0, value=0, disabled=(own_status.startswith("👀")))

        if st.form_submit_button("➕ 종목 등록") and new_s:
            code, matched_name = search_stock_code(new_s.strip())
            is_owned_flag = 1 if own_status.startswith("💼") else 0
            if is_owned_flag:
                try: final_avg_p = float(str(avg_p).replace(',', ''))
                except: final_avg_p = 0.0
                final_qty = qty
            else: final_avg_p, final_qty = 0.0, 0

            c.execute("INSERT INTO portfolio (stock_name, ticker, is_owned, avg_price, quantity) VALUES (?,?,?,?,?)", (new_s.strip(), code or '', is_owned_flag, final_avg_p, final_qty))
            conn.commit()
            if code: st.session_state.watch_add_msg = ("success", f"✅ '{matched_name}'({code}) 등록 완료!")
            else: st.session_state.watch_add_msg = ("warning", f"⚠️ 종목코드를 찾지 못했어요.")
            st.rerun()

    if st.session_state.get("watch_add_msg"):
        level, msg = st.session_state.pop("watch_add_msg"); getattr(st, level)(msg)

    c.execute("SELECT id, stock_name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp, model_used, report_time, ticker FROM portfolio")
    portfolios = c.fetchall()
    price_map_watch = fetch_current_prices([p[12] for p in portfolios if p[12]])

    for p in portfolios:
        p_id, name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp, model_used, report_time, ticker = p
        code = re.sub(r'[^\d]', '', ticker or "")
        price_info = price_map_watch.get(code, {})
        current, diff, diff_pct = price_info.get("current", 0.0), price_info.get("diff", 0.0), price_info.get("diff_pct", 0.0)

        st.markdown(f"### 📌 [{name}]" + (f" `{code}`" if code else ""))
        col_info, col_price, col_btn = st.columns([2, 2, 1])
        with col_info:
            if is_owned:
                st.caption(f"💼 **보유** | 평단:{avg_price:,.0f}원 | 수량:{quantity}")
                if current > 0 and avg_price > 0: st.caption(f"📈 평단 대비 수익률: **{((current - avg_price) / avg_price * 100):+.1f}%**")
            else: st.caption("👀 **관심**")
        with col_price:
            if current > 0: st.metric("현재가", f"{current:,.0f}원", delta=f"{diff:+,.0f}원 ({diff_pct:+.2f}%)")
            elif code: st.caption("조회 실패")
            else: st.caption("종목코드를 등록하세요")
        with col_btn:
            if st.button("🔄 AI 진단", key=f"run_{p_id}", type="primary"):
                with st.spinner("AI가 진단하고 있습니다..."):
                    report = call_gemini_with_fallback(build_prompt_deep_dive(name, market_data_str))
                tp_match = re.search(r'TARGET_PRICE:\s*([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|(.*)', report)
                n_tp_s = parse_won(tp_match.group(1)) if tp_match else 0.0
                n_tp_m = parse_won(tp_match.group(2)) if tp_match else 0.0
                n_tp_l = parse_won(tp_match.group(3)) if tp_match else 0.0
                n_bp = parse_won(tp_match.group(4)) if tp_match else 0.0
                c.execute("UPDATE portfolio SET report_text=?, tp_s=?, tp_m=?, tp_l=?, bp=?, model_used=?, report_time=? WHERE id=?", (report, n_tp_s, n_tp_m, n_tp_l, n_bp, MODEL_NAME, datetime.now().strftime("%Y-%m-%d %H:%M"), p_id))
                conn.commit(); st.rerun()

        if report_text:
            with st.expander("📝 AI 진단 리포트", expanded=True):
                st.info(f"**단기:** {tp_s:,.0f}원  |  **중기:** {tp_m:,.0f}원  |  **장기:** {tp_l:,.0f}원  |  **💰매수:** {bp:,.0f}원")
                st.write(re.sub(r'TARGET_PRICE:.*', '', report_text).strip())
                st.caption(f"🧠 생성 모델: {model_used or MODEL_NAME} · {report_time or ''}")
                if st.button("💾 스크랩", key=f"save_{p_id}"):
                    c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date, stock_name, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, model_used) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (f"[{name}] 리포트", "진단", report_text, datetime.now().strftime("%Y-%m-%d %H:%M"), name, current, tp_s, tp_m, tp_l, bp, model_used or MODEL_NAME))
                    conn.commit(); st.success("저장 완료")
        st.divider()

    if portfolios:
        to_delete = st.multiselect("삭제할 종목 선택", [p[1] for p in portfolios])
        if st.button("🗑️ 종목 삭제", type="primary") and to_delete:
            for d_name in to_delete: c.execute("DELETE FROM portfolio WHERE stock_name=?", (d_name,))
            conn.commit(); st.rerun()

with tab6:
    st.subheader("📁 내 스크랩북")
    c.execute("SELECT id, title, analysis, scrap_date, stock_name, saved_price, target_price, buy_recommend_price, target_price_mid, target_price_long, model_used FROM scrapbook ORDER BY id DESC")
    scraps = c.fetchall()

    col_ctrl1, _ = st.columns([1, 4])
    with col_ctrl1:
        if st.button("🗑️ 선택 삭제", type="primary", use_container_width=True):
            to_delete_ids = [sid.split("_")[1] for sid, checked in st.session_state.items() if sid.startswith("chk_") and checked]
            if to_delete_ids:
                c.executemany("DELETE FROM scrapbook WHERE id=?", [(int(i),) for i in to_delete_ids])
                conn.commit()
                for sid in list(st.session_state.keys()):
                    if sid.startswith("chk_"): st.session_state.pop(sid)
                st.rerun()

    for s in scraps:
        scrap_id, title, analysis, scrap_date = s[0], s[1], s[2], s[3]
        saved_price, tp_s, b_rec, tp_m, tp_l = float(s[5] or 0), float(s[6] or 0), float(s[7] or 0), float(s[8] or 0), float(s[9] or 0)
        
        col_chk, col_exp = st.columns([0.05, 0.95])
        with col_chk:
            st.markdown("<br>", unsafe_allow_html=True); st.checkbox("", key=f"chk_{scrap_id}", label_visibility="collapsed")
        with col_exp:
            with st.expander(f"[{scrap_date}] {title}"):
                cols_sc = st.columns(4)
                cols_sc[0].metric("저장가", f"{saved_price:,.0f}원")
                cols_sc[1].markdown(f"**🎯 밴드**<br>단기: {tp_s:,.0f}<br>중기: {tp_m:,.0f}<br>장기: {tp_l:,.0f}", unsafe_allow_html=True)
                cols_sc[2].metric("💰 매수 추천", f"{b_rec:,.0f}원" if b_rec > 0 else "기록 없음")
                st.write(analysis)
                st.caption(f"🧠 {s[10] if len(s)>10 else '이전 저장분'}")
