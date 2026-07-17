import streamlit as st
import json
import sqlite3
import re
import threading
import requests
import pandas as pd
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google import genai

MODEL_NAME = "gemini-3.5-flash"

st.set_page_config(page_title="Project2_Stock", page_icon="📊", layout="wide")

# =======================================================
# 비밀번호 기반 자동 유저 식별 로그인 (URL 파라미터 지원)
# =======================================================
def check_password():
    passwords_dict = st.secrets.get("USER_PASSWORDS", {})
    
    # 1. URL 파라미터 확인 (/?pwd=비밀번호)
    if "pwd" in st.query_params:
        url_pwd = st.query_params["pwd"]
        if url_pwd in passwords_dict:
            st.session_state["password_correct"] = True
            st.session_state["user_id"] = passwords_dict[url_pwd]
            
    # 2. 세션 상태 확인
    if "password_correct" in st.session_state and st.session_state["password_correct"]:
        return True

    # 3. 로그인 폼 렌더링
    st.title("Project2_Stock 로그인")
    password = st.text_input("비밀번호를 입력하세요", type="password")
    if st.button("접속하기"):
        if password in passwords_dict:
            st.session_state["password_correct"] = True
            st.session_state["user_id"] = passwords_dict[password]
            st.rerun()
        else:
            st.error("비밀번호가 일치하지 않습니다.")
    return False

if not check_password(): 
    st.stop()

current_user = st.session_state["user_id"]

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
API_GATEWAY_REALTIME_URL = st.secrets.get("API_GATEWAY_REALTIME_URL", "")
NAVER_CLIENT_ID = st.secrets.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = st.secrets.get("NAVER_CLIENT_SECRET", "")

conn = sqlite3.connect('market_analysis.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS scrapbook (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, link TEXT, summary TEXT, analysis TEXT, scrap_date TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS portfolio (id INTEGER PRIMARY KEY AUTOINCREMENT, stock_name TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS sentiment_history (id INTEGER PRIMARY KEY AUTOINCREMENT, calc_date TEXT, score REAL)''')
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
    ("portfolio", "report_time", "TEXT"), ("portfolio", "ticker", "TEXT"), ("scrapbook", "model_used", "TEXT"),
    ("portfolio", "user_id", "TEXT DEFAULT 'dongsu'"), ("scrapbook", "user_id", "TEXT DEFAULT 'dongsu'")
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
    except Exception:
        return None

def fetch_realtime_data_direct(seen_links):
    if not API_GATEWAY_REALTIME_URL:
        return None
    try:
        payload = {"seen_links": list(seen_links)}
        res = requests.post(API_GATEWAY_REALTIME_URL, json=payload, timeout=30)
        res.raise_for_status()
        return res.json()
    except Exception:
        return None

@st.cache_data(ttl=600)
def get_technical_data(code):
    try:
        url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=250&requestType=0"
        res = requests.get(url, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        items = soup.find_all('item')
        if not items: return None
        
        df_data = [float(item['data'].split('|')[4]) for item in items]
        if len(df_data) < 60: return None
        
        current = df_data[-1]
        high_52 = max(df_data)
        low_52 = min(df_data)
        ma20 = sum(df_data[-20:]) / 20
        ma60 = sum(df_data[-60:]) / 60
        
        df = pd.Series(df_data)
        ema12 = df.ewm(span=12, adjust=False).mean()
        ema26 = df.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        
        return {
            "current": current, "high_52": high_52, "low_52": low_52,
            "ma20": ma20, "ma60": ma60, "macd": macd.iloc[-1], "signal": signal.iloc[-1]
        }
    except Exception:
        return None

@st.cache_data(ttl=600)
def get_fundamental_data(code):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'}
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        
        fund_data = {"per": "-", "pbr": "-", "eps": 0, "bps": 0}
        
        per_elem = soup.find(id="_per")
        if per_elem:
            try: fund_data["per"] = float(re.search(r'[\d.]+', per_elem.get_text()).group())
            except: pass
            
        pbr_elem = soup.find(id="_pbr")
        if pbr_elem:
            try: fund_data["pbr"] = float(re.search(r'[\d.]+', pbr_elem.get_text()).group())
            except: pass
            
        eps_elem = soup.find(id="_eps")
        if eps_elem:
            try: fund_data["eps"] = int(re.sub(r'[^\d]', '', eps_elem.get_text()))
            except: pass

        th_elements = soup.find_all("th")
        for th in th_elements:
            if "BPS" in th.get_text():
                td = th.find_next("td")
                if td:
                    txt = re.sub(r'[^\d]', '', td.get_text())
                    if txt:
                        fund_data["bps"] = int(txt)
                        break

        return fund_data
    except Exception:
        return {"per": "-", "pbr": "-", "eps": 0, "bps": 0}

@st.cache_data(ttl=600)
def fetch_stock_news(query, display=5):
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return []
    try:
        url = f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={urllib.parse.quote(query)}&display={display}&sort=date&format=json"
        req = urllib.request.Request(url, headers={"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET})
        with urllib.request.urlopen(req, timeout=3) as response:
            res = json.loads(response.read().decode('utf-8'))
            items = []
            for i in res.get("items", []):
                title = BeautifulSoup(i['title'], "html.parser").get_text()
                pub_date = i.get('pubDate', '')
                if pub_date:
                    try:
                        dt = parsedate_to_datetime(pub_date)
                        pub_date = dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
                    except: pass
                items.append({"title": title, "link": i['link'], "published": pub_date})
            return items
    except:
        return []

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

if "seen_realtime_links" not in st.session_state:
    st.session_state.seen_realtime_links = set()
if "realtime_cache" not in st.session_state:
    st.session_state.realtime_cache = None
if "eco_display_limit" not in st.session_state:
    st.session_state.eco_display_limit = 10
if "last_saved_eco_time" not in st.session_state:
    st.session_state.last_saved_eco_time = ""

cached_data = fetch_cached_global_data() or {}

if st.session_state.realtime_cache is None:
    with st.spinner("데이터 로딩 중..."):
        new_data = fetch_realtime_data_direct(st.session_state.seen_realtime_links)
        if new_data:
            st.session_state.realtime_cache = new_data
            for n in new_data.get("realtime_news", []):
                st.session_state.seen_realtime_links.add(n['link'])

g_data = st.session_state.realtime_cache or {}

col_title, col_refresh = st.columns([5, 1.2])
with col_refresh:
    if st.button("실시간 갱신", use_container_width=True):
        with st.spinner("갱신 중..."):
            new_data = fetch_realtime_data_direct(st.session_state.seen_realtime_links)
            if new_data:
                st.session_state.realtime_cache = new_data
                new_news = new_data.get("realtime_news", [])
                if new_news:
                    for n in new_news:
                        st.session_state.seen_realtime_links.add(n['link'])
                st.rerun()
            else:
                st.error("데이터 갱신에 실패했습니다. API 연결 상태를 확인해주세요.")

if not g_data:
    st.warning("실시간 데이터를 호출하지 못했습니다. 상단의 '실시간 갱신' 버튼을 눌러주세요.")

with col_title:
    st.caption(f"실시간: {g_data.get('updated_at', '알 수 없음')} | 캐시: {cached_data.get('updated_at', '알 수 없음')} | 사용자: {current_user}")

market_data = g_data.get("market_status", {}) if g_data else {}
market_data_str = ", ".join([f"{k}: {v['current']}({v['diff_pct']}%)" for k, v in market_data.items() if v.get('current', 0) > 0])

target_indices = ["코스피", "코스닥", "S&P 500", "원/달러 환율"]
cols = st.columns(4)
for i, key in enumerate(target_indices):
    with cols[i]:
        if key in market_data:
            data = market_data[key]
            val, diff, diff_pct = data.get("current", 0.0), data.get("diff", 0.0), data.get("diff_pct", 0.0)
            if val == 0.0: st.metric(label=key, value="점검중")
            elif key == "원/달러 환율": st.metric(label=key, value=f"{val:,.2f}", delta=f"{diff:+.2f} ({diff_pct:+.2f}%)")
            else: st.metric(label=key, value=f"{val:,.2f}", delta=f"{diff:+.2f} ({diff_pct:+.2f}%)")
        else:
            st.metric(label=key, value="대기중")

st.divider()

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["실시간 브리핑", "핵심 경제", "섹터 뉴스", "종목 발굴", "관심종목 진단", "스크랩북"])

with tab1:
    st.subheader("실시간 시사 뉴스")
    news_list = dedupe_news(g_data.get("realtime_news", [])) if g_data else []

    if not news_list:
         st.write("새로 표시할 실시간 뉴스가 없습니다.")

    if st.button("실시간 종합 분석", use_container_width=True, key="btn_realtime"):
        if not news_list:
            st.error("분석할 뉴스 데이터가 존재하지 않습니다.")
        else:
            articles_str = "\n".join([f"- [발행일: {n.get('published', '알수없음')}] {n['title']}\n  요약: {n.get('summary', '')}" for n in news_list[:100]])
            prompt = (f"지표: {market_data_str}\n\n[기사 목록]\n{articles_str}\n\n"
                      f"위 데이터를 바탕으로 객관적이고 간결한 시장 분석 보고서를 작성하십시오. 감정적 수사나 이모지는 완전히 배제하십시오.")
            with st.spinner("분석 중..."):
                st.session_state.realtime_analysis = "".join(call_gemini_stream_with_fallback(prompt))

    if st.session_state.get("realtime_analysis"):
        with st.expander("분석 결과", expanded=True):
            st.write(st.session_state.realtime_analysis)
            st.caption(f"🧠 분석 모델: {MODEL_NAME}")

    for news in news_list[:10]:
        with st.expander(f"{news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']})\n\n발행일: {news.get('published', '')}\n\n{news['summary']}")

with tab2:
    st.subheader("핵심 경제 및 심리 지수")
    
    c.execute("SELECT calc_date, AVG(score) FROM sentiment_history GROUP BY calc_date ORDER BY calc_date ASC")
    sentiment_data = c.fetchall()
    
    if sentiment_data:
        df = pd.DataFrame(sentiment_data, columns=["날짜", "시장 심리 지수"]).set_index("날짜")
        st.line_chart(df, height=200)
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_score = next((row[1] for row in sentiment_data if row[0] == today_str), None)
        if today_score is not None:
            st.metric("오늘의 심리 지수 평균", f"{today_score:.1f}점")
        st.divider()

    eco_news_list = dedupe_news(cached_data.get("eco_news", [])) if cached_data else []
    current_limit = st.session_state.eco_display_limit

    if st.button("핵심 경제 분석 (상위 50개)", use_container_width=True, key="btn_eco"):
        if not eco_news_list:
            st.error("캐시된 경제 뉴스 데이터가 없습니다.")
        else:
            articles_str = "\n".join([f"- {n['title']}" for n in eco_news_list[:100]])
            prompt = (f"지표: {market_data_str}\n\n[기사 목록]\n{articles_str}\n\n"
                      f"1. 거시경제 분석을 객관적이고 명확하게 서술하십시오.\n"
                      f"2. 전체 시장의 지수 투심을 고려하여 '시장 심리 지수(0~100)'를 평가하십시오.\n"
                      f"3. 보고서 마지막에 반드시 아래 형식으로 기재하십시오.\n[SENTIMENT_SCORE: 점수]")
            
            with st.spinner("분석 중..."):
                st.session_state.eco_analysis = "".join(call_gemini_stream_with_fallback(prompt))
                st.session_state.eco_analysis_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    if st.session_state.get("eco_analysis"):
        raw_text = st.session_state.eco_analysis
        match = re.search(r'\[SENTIMENT_SCORE:\s*(\d+(?:\.\d+)?)\]', raw_text)
        if match and st.session_state.get("last_saved_eco_time") != st.session_state.eco_analysis_time:
            score = float(match.group(1))
            today_str = datetime.now().strftime("%Y-%m-%d")
            c.execute("INSERT INTO sentiment_history (calc_date, score) VALUES (?, ?)", (today_str, score))
            conn.commit()
            st.session_state.last_saved_eco_time = st.session_state.eco_analysis_time
            st.rerun()
            
        clean_text = re.sub(r'\[SENTIMENT_SCORE:.*?\]', '', raw_text).strip()
        with st.expander("분석 결과", expanded=True):
            st.write(clean_text)
            st.caption(f"🧠 분석 모델: {MODEL_NAME}")

    for news in eco_news_list[:current_limit]:
        with st.expander(f"{news['title']}"):
            st.markdown(f"[원문 읽기]({news['link']})\n\n{news['summary']}")
            
    if current_limit < len(eco_news_list):
        if st.button("목록 더보기", use_container_width=True):
            st.session_state.eco_display_limit += 10
            st.rerun()

with tab3:
    st.subheader("섹터 뉴스")
    sectors_data = cached_data.get("sectors", {}) if cached_data else {}
    
    if not sectors_data:
        st.info("섹터 데이터가 아직 수집되지 않았습니다.")
    else:
        selected_sector = st.selectbox("섹터 선택", list(sectors_data.keys()))
        sector_news = dedupe_news(sectors_data.get(selected_sector, []))

        if st.button(f"{selected_sector} 섹터 분석", use_container_width=True, key=f"btn_sector_{selected_sector}"):
            articles_str = "\n".join([f"- {n['title']}" for n in sector_news])
            prompt = f"[{selected_sector} 섹터]\n지표: {market_data_str}\n\n[기사 목록]\n{articles_str}\n\n섹터 동향을 객관적으로 서술하십시오. 수식어나 이모지는 배제하십시오."
            with st.spinner("분석 중..."):
                st.session_state[f"sector_analysis_{selected_sector}"] = "".join(call_gemini_stream_with_fallback(prompt))

        if st.session_state.get(f"sector_analysis_{selected_sector}"):
            with st.expander("분석 결과", expanded=True):
                st.write(st.session_state[f"sector_analysis_{selected_sector}"])
                st.caption(f"🧠 분석 모델: {MODEL_NAME}")

        for news in sector_news[:10]:
            with st.expander(f"{news['title']}"):
                st.write(news['summary'])

with tab4:
    st.subheader("종목 발굴")
    investment_horizon = st.radio("투자기간", ["단기 (1~3개월)", "중기 (3~6개월)", "장기 (1년 이상)"], horizontal=True)

    if st.button("추천 종목 발굴", use_container_width=True, key="btn_recommend"):
        rec_news = dedupe_news((g_data.get("realtime_news", []) if g_data else []) + (cached_data.get("eco_news", []) if cached_data else []))
        
        if not rec_news:
            st.error("분석 대상 뉴스 풀이 비어있습니다. 실시간 갱신을 먼저 진행해 주세요.")
        else:
            with st.spinner("1차 종목 선별 중..."):
                articles_str = "\n".join([f"- {n['title']}" for n in rec_news[:50]])
                step1_prompt = (f"다음 경제 뉴스를 바탕으로 {investment_horizon} 상승 모멘텀이 뛰어난 "
                                f"한국 주식 종목 3개를 골라 종목코드 6자리만 JSON 배열로 출력하라.\n"
                                f"예: [\"005930\", \"000660\", \"035420\"]\n오직 JSON만 출력할 것.\n\n{articles_str}")
                
                step1_res = call_gemini_with_fallback(step1_prompt)
                match = re.search(r'\[.*\]', step1_res, re.DOTALL)
                selected_tickers = []
                if match:
                    try: selected_tickers = json.loads(match.group(0))[:3]
                    except: pass
                if not selected_tickers:
                    st.error("종목 선별에 실패했습니다. 다시 시도해주세요.")
                    st.stop()
            
            with st.spinner("선별 종목의 기술적/퀀트 데이터 계산 중..."):
                tech_data_str = ""
                for ticker in selected_tickers:
                    ticker = re.sub(r'[^\d]', '', ticker)
                    if len(ticker) != 6: continue
                    try:
                        res = requests.get(f"https://m.stock.naver.com/api/stock/{ticker}/basic", timeout=3).json()
                        name = res.get("stockName", ticker)
                    except: name = ticker
                    
                    tech = get_technical_data(ticker)
                    fund = get_fundamental_data(ticker)
                    tech_data_str += f"[{name} ({ticker})]\n"
                    
                    if tech:
                        tech_data_str += f"- 현재가: {tech['current']:,.0f}\n"
                        tech_data_str += f"- 차트: 52주 최고 {tech['high_52']:,.0f} / 최저 {tech['low_52']:,.0f} | 20일선 {tech['ma20']:,.0f} | 60일선 {tech['ma60']:,.0f}\n"
                        tech_data_str += f"- MACD: {tech['macd']:,.2f} | 시그널: {tech['signal']:,.2f}\n"
                    if fund:
                        tech_data_str += f"- 펀더멘털: PER {fund['per']} | PBR {fund['pbr']} | EPS {fund['eps']:,.0f} | BPS {fund['bps']:,.0f}\n\n"
            
            with st.spinner("최종 심층 분석 중..."):
                step2_prompt = (
                    f"당신은 감정을 철저히 배제하고 숫자와 팩트로만 승부하는 퀀트 및 차트 애널리스트입니다.\n"
                    f"선택된 투자 기간: {investment_horizon}\n\n"
                    f"[추출된 실데이터]\n{tech_data_str}\n"
                    f"위 종목들의 실제 제공된 '현재가', '차트 지표', '펀더멘털 지표'를 절대적으로 신뢰하여 분석하십시오.\n\n"
                    f"아래 항목들을 누락 없이 심층적이고 풍부하게 서술하여 리포트를 작성하십시오.\n\n"
                    f"[종목명] (티커)\n"
                    f"- 매수의견: (현재가 대비 정량적 가치를 판단하여 Buy/Hold/Sell 중 명확한 투자의견 제시)\n"
                    f"- 모멘텀 분석: (관련 핵심 이슈 요약)\n"
                    f"- 기술적 분석: (이평선 위치, MACD 등 실제 숫자에 기반한 차트 추세 서술)\n"
                    f"- 퀀트 분석: (제공된 PER, EPS, PBR, BPS 수치를 활용하여 적용된 적정주가 산출 공식과 계산 과정을 구체적이고 투명하게 명시)\n"
                    f"- 목표가 산출 근거: (퀀트 밸류에이션 결과와 기술적 매물대/지지선을 어떻게 유기적으로 융합하여 단기·중기·장기 목표가를 도출했는지 각각 구체적인 수식과 숫자를 들어 상세히 서술)\n"
                    f"- 진입 타점: (기술적 지지선에 기반한 현실적인 진입 가격)\n\n"
                    f"※ 마지막 줄은 반드시 아래 파싱 형식으로만 출력. 가격은 단위 없이 숫자로만 기입합니다.\n"
                    f"[TRACKING_DATA]\n"
                    f"종목명1|티커1|단기목표가숫자만|중기목표가숫자만|장기목표가숫자만|진입타점숫자만\n"
                    f"종목명2|티커2|단기목표가숫자만|중기목표가숫자만|장기목표가숫자만|진입타점숫자만\n"
                    f"종목명3|티커3|단기목표가숫자만|중기목표가숫자만|장기목표가숫자만|진입타점숫자만"
                )
                st.session_state.today_recommendation = "".join(call_gemini_stream_with_fallback(step2_prompt))

    if st.session_state.get('today_recommendation'):
        raw = st.session_state.today_recommendation
        with st.expander("추천 리포트", expanded=True):
            st.write(raw.split("[TRACKING_DATA]")[0].strip())
            st.caption(f"🧠 분석 모델: {MODEL_NAME}")

            if "[TRACKING_DATA]" in raw:
                block = raw.split("[TRACKING_DATA]")[1].strip().replace("```", "")
                parsed_rows = []
                for line in block.split('\n'):
                    if not line.strip(): continue
                    data = line.split('|')
                    if len(data) >= 6: 
                        parsed_rows.append((data[0].strip(), data[1].strip(), parse_won(data[2]), parse_won(data[3]), parse_won(data[4]), parse_won(data[5])))

                price_map = fetch_current_prices([r[1] for r in parsed_rows])
                cols_rec = st.columns(3)
                for idx, (name, tick, tp_s, tp_m, tp_l, bp) in enumerate(parsed_rows):
                    code = re.sub(r'[^\d]', '', tick)
                    price_info = price_map.get(code, {})
                    current, diff, diff_pct = price_info.get("current", 0.0), price_info.get("diff", 0.0), price_info.get("diff_pct", 0.0)

                    with cols_rec[idx % 3]:
                        with st.container(border=True):
                            st.markdown(f"**{name}** `{tick}`")
                            if current > 0: st.metric("현재가", f"{current:,.0f}", delta=f"{diff:+,.0f} ({diff_pct:+.2f}%)")
                            else: st.metric("현재가", "조회 실패")
                            
                            c_tp, c_bp = st.columns(2)
                            c_tp.markdown(f"**목표가 밴드**<br>단: {tp_s:,.0f}<br>중: {tp_m:,.0f}<br>장: {tp_l:,.0f}", unsafe_allow_html=True)
                            c_bp.metric("진입 타점", f"{bp:,.0f}")

                            if st.button("스크랩", key=f"rec_s_{tick}", use_container_width=True):
                                c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, scrap_date, model_used, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                          (f"{name} 분석", raw, name, tick, current, tp_s, tp_m, tp_l, bp, datetime.now().strftime("%Y-%m-%d %H:%M"), MODEL_NAME, current_user))
                                conn.commit(); st.success("저장 완료")

with tab5:
    st.subheader("관심종목 진단")
    own_status = st.radio("상태", ["미보유", "보유"], horizontal=True, key="add_own_status")
    with st.form("add_stock"):
        new_s = st.text_input("종목명 (예: 삼성전자)")
        c2, c3 = st.columns(2)
        avg_p = c2.text_input("평단가", value="0", disabled=(own_status == "미보유"))
        qty = c3.number_input("수량", min_value=0, value=0, disabled=(own_status == "미보유"))

        if st.form_submit_button("추가") and new_s:
            code, matched_name = search_stock_code(new_s.strip())
            is_owned_flag = 1 if own_status == "보유" else 0
            if is_owned_flag:
                try: final_avg_p = float(str(avg_p).replace(',', ''))
                except: final_avg_p = 0.0
                final_qty = qty
            else: final_avg_p, final_qty = 0.0, 0

            c.execute("INSERT INTO portfolio (stock_name, ticker, is_owned, avg_price, quantity, user_id) VALUES (?,?,?,?,?,?)", (new_s.strip(), code or '', is_owned_flag, final_avg_p, final_qty, current_user))
            conn.commit()
            st.rerun()

    c.execute("SELECT id, stock_name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp, model_used, report_time, ticker FROM portfolio WHERE user_id = ?", (current_user,))
    portfolios = c.fetchall()
    price_map_watch = fetch_current_prices([p[12] for p in portfolios if p[12]])

    for p in portfolios:
        p_id, name, is_owned, avg_price, quantity, report_text, tp_s, tp_m, tp_l, bp, model_used, report_time, ticker = p
        code = re.sub(r'[^\d]', '', ticker or "")
        price_info = price_map_watch.get(code, {})
        current, diff, diff_pct = price_info.get("current", 0.0), price_info.get("diff", 0.0), price_info.get("diff_pct", 0.0)

        st.markdown(f"### {name} `{code}`")
        col_info, col_price, col_btn = st.columns([2, 2, 1])
        with col_info:
            if is_owned:
                st.caption(f"보유 | 평단: {avg_price:,.0f} | 수량: {quantity}")
                if current > 0 and avg_price > 0: st.caption(f"수익률: {((current - avg_price) / avg_price * 100):+.1f}%")
            else: st.caption("미보유")
        with col_price:
            if current > 0: st.metric("현재가", f"{current:,.0f}", delta=f"{diff:+,.0f} ({diff_pct:+.2f}%)")
            else: st.caption("조회 실패")
        with col_btn:
            if st.button("진단 실행", key=f"run_{p_id}", use_container_width=True):
                with st.spinner("진단 중..."):
                    tech = get_technical_data(code)
                    fund = get_fundamental_data(code)
                    data_str = f"현재가: {current:,.0f}\n"
                    if tech:
                        data_str += f"[차트] 52주 고/저: {tech['high_52']:,.0f}/{tech['low_52']:,.0f} | 20일선: {tech['ma20']:,.0f} | 60일선: {tech['ma60']:,.0f} | MACD: {tech['macd']:,.2f}\n"
                    if fund:
                        data_str += f"[퀀트] PER: {fund['per']} | PBR: {fund['pbr']} | EPS: {fund['eps']:,.0f} | BPS: {fund['bps']:,.0f}\n"
                    
                    prompt = (f"[{name} 진단]\n[실데이터]\n{data_str}\n\n"
                              f"당신은 객관적인 수치에 입각해 판단하는 애널리스트입니다. 감정적 표현과 이모지를 배제하십시오.\n\n"
                              f"아래 항목들을 분량을 축소하지 말고 상세히 서술하여 리포트를 작성하십시오.\n"
                              f"- 매수의견: (현재가 대비 정량적 밸류에이션을 근거로 Buy/Hold/Sell 명시)\n"
                              f"- 모멘텀 분석\n"
                              f"- 기술적 분석 (이평선, MACD 등 실제 값 사용)\n"
                              f"- 퀀트 분석 (제공된 지표를 적용한 구체적인 주가 계산 공식과 수치 증명 명시)\n"
                              f"- 목표가 산출 근거: (단기, 중기, 장기 가격을 도출해낸 구체적인 수식과 근거 서술)\n\n"
                              f"※ 반드시 마지막 줄에 아래 파싱 형식으로만 작성.\n"
                              f"TARGET_PRICE: 단기숫자만|중기숫자만|장기숫자만|매수추천가숫자만")
                    
                    report = call_gemini_with_fallback(prompt)
                tp_match = re.search(r'TARGET_PRICE:\s*([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|(.*)', report)
                n_tp_s = parse_won(tp_match.group(1)) if tp_match else 0.0
                n_tp_m = parse_won(tp_match.group(2)) if tp_match else 0.0
                n_tp_l = parse_won(tp_match.group(3)) if tp_match else 0.0
                n_bp = parse_won(tp_match.group(4)) if tp_match else 0.0
                c.execute("UPDATE portfolio SET report_text=?, tp_s=?, tp_m=?, tp_l=?, bp=?, model_used=?, report_time=? WHERE id=?", (report, n_tp_s, n_tp_m, n_tp_l, n_bp, MODEL_NAME, datetime.now().strftime("%Y-%m-%d %H:%M"), p_id))
                conn.commit(); st.rerun()

        with st.expander(f"{name} 데이터 및 뉴스", expanded=False):
            tech = get_technical_data(code)
            fund = get_fundamental_data(code)
            
            c1, c2, c3, c4 = st.columns(4)
            if tech:
                c1.metric("20일/60일 이평선", f"{tech['ma20']:,.0f} / {tech['ma60']:,.0f}")
                c2.metric("52주 최고/최저", f"{tech['high_52']:,.0f} / {tech['low_52']:,.0f}")
            if fund:
                c3.metric("PER / PBR", f"{fund['per']} / {fund['pbr']}")
                c4.metric("EPS / BPS", f"{fund['eps'] if isinstance(fund['eps'], str) else f'{fund['eps']:,.0f}'} / {fund['bps'] if isinstance(fund['bps'], str) else f'{fund['bps']:,.0f}'}")

            st.markdown("---")
            news_list = fetch_stock_news(name, display=5)
            if news_list:
                for n in news_list:
                    st.markdown(f"- [{n['title']}]({n['link']})")
            else:
                st.caption("뉴스가 없습니다.")

        if report_text:
            with st.expander("진단 리포트", expanded=True):
                st.info(f"단기 목표: {tp_s:,.0f} | 중기: {tp_m:,.0f} | 장기: {tp_l:,.0f} | 진입 타점: {bp:,.0f}")
                st.write(re.sub(r'TARGET_PRICE:.*', '', report_text).strip())
                st.caption(f"🧠 분석 모델: {model_used or MODEL_NAME}")
                
                if st.button("결과 저장", key=f"save_{p_id}"):
                    c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price, model_used, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (f"{name} 진단", "", report_text, datetime.now().strftime("%Y-%m-%d %H:%M"), name, code, current, tp_s, tp_m, tp_l, bp, MODEL_NAME, current_user))
                    conn.commit(); st.success("저장 완료")
        st.divider()

    if portfolios:
        to_delete = st.multiselect("삭제 항목 선택", [p[1] for p in portfolios])
        if st.button("선택 삭제"):
            for d_name in to_delete: c.execute("DELETE FROM portfolio WHERE stock_name=? AND user_id=?", (d_name, current_user))
            conn.commit(); st.rerun()

with tab6:
    st.subheader("스크랩북")
    c.execute("SELECT id, title, analysis, scrap_date, stock_name, saved_price, target_price, buy_recommend_price, target_price_mid, target_price_long, model_used, ticker FROM scrapbook WHERE user_id = ? ORDER BY id DESC", (current_user,))
    scraps = c.fetchall()

    col_ctrl1, _ = st.columns([1, 4])
    with col_ctrl1:
        if st.button("체크된 항목 삭제", use_container_width=True):
            to_delete_ids = [sid.split("_")[1] for sid, checked in st.session_state.items() if sid.startswith("chk_") and checked]
            if to_delete_ids:
                c.executemany("DELETE FROM scrapbook WHERE id=? AND user_id=?", [(int(i), current_user) for i in to_delete_ids])
                conn.commit()
                for sid in list(st.session_state.keys()):
                    if sid.startswith("chk_"): st.session_state.pop(sid)
                st.rerun()

    tickers_to_fetch = [s[11] for s in scraps if s[11]]
    price_map_scrap = fetch_current_prices(tickers_to_fetch)

    for s in scraps:
        scrap_id, title, analysis, scrap_date, stock_name = s[0], s[1], s[2], s[3], s[4]
        saved_price, tp_s, b_rec, tp_m, tp_l = float(s[5] or 0), float(s[6] or 0), float(s[7] or 0), float(s[8] or 0), float(s[9] or 0)
        model_used = s[10]
        ticker = s[11]

        current_price = 0.0
        diff, diff_pct = 0.0, 0.0
        if ticker:
            code = re.sub(r'[^\d]', '', ticker)
            p_info = price_map_scrap.get(code, {})
            current_price = p_info.get("current", 0.0)
            diff = p_info.get("diff", 0.0)
            diff_pct = p_info.get("diff_pct", 0.0)
        
        col_chk, col_exp = st.columns([0.05, 0.95])
        with col_chk:
            st.markdown("<br>", unsafe_allow_html=True); st.checkbox("", key=f"chk_{scrap_id}", label_visibility="collapsed")
        with col_exp:
            with st.expander(f"[{scrap_date}] {title}"):
                cols_sc = st.columns(5)
                cols_sc[0].metric("저장 당시가", f"{saved_price:,.0f}원")
                if current_price > 0:
                    cols_sc[1].metric("현재가", f"{current_price:,.0f}원", delta=f"{diff:+,.0f}원 ({diff_pct:+.2f}%)")
                else:
                    cols_sc[1].metric("현재가", "조회 불가")
                
                cols_sc[2].metric("단기 목표가", f"{tp_s:,.0f}원" if tp_s > 0 else "-")
                cols_sc[3].markdown(f"**중기/장기 목표가**<br>{tp_m:,.0f} / {tp_l:,.0f}", unsafe_allow_html=True)
                cols_sc[4].metric("매수 타점", f"{b_rec:,.0f}원" if b_rec > 0 else "-")

                if current_price > 0 and tp_s > 0:
                    gap_pct = (tp_s - current_price) / current_price * 100
                    if gap_pct >= 0:
                        st.caption(f"🎯 현재가 기준 단기 목표가까지 **+{gap_pct:.1f}%** 상승 공간 남아있음")
                    else:
                        st.caption(f"🎯 현재가 기준 단기 목표가 대비 **{abs(gap_pct):.1f}%** 초과 달성 중")

                st.write(analysis)
                st.caption(f"🧠 분석 모델: {model_used or '정보 없음'}")
