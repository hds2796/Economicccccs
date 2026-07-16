except: return "재무 정보 데이터 누락"

# =======================================================
# 💡 AI 프롬프트 빌더 (통합 목표가 산출 로직)
# 💡 AI 프롬프트 빌더
# =======================================================
def build_prompt_single_news(title, summary, market_data_str):
return f"아래 뉴스가 증시에 미칠 영향을 분석하세요.\n[지표]: {market_data_str}\n[제목]: {title}\n[요약]: {summary}\n1. 💡 핵심 요약\n2. 📈 시장 파급력\n3. 🎯 연관 섹터"
@@ -570,8 +570,8 @@ def build_prompt_recommend_step3(candidate_context, news_list, market_data_str,
f"⚠️ [목표가 산출 및 작성 규칙]\n"
f"1. 목표가는 기간별로 여러 개를 제시하지 말고, 오직 선택된 '{horizon}'에 맞는 '단 하나의 최종 목표가'만 도출하십시오.\n"
f"2. 추천 사유에는 이 종목이 왜 해당 투자 기간에 적합한지 (뉴스 팩트, 수급, 차트 상황) 구체적으로 서술하십시오.\n"
            f"3. 목표가 산출 논리는 반드시 다음 두 단계를 거쳐 깔끔한 리스트 형태로 작성하십시오:\n"
            f"   - [1차 퀀트 연산]: 어떤 수학적/통계적 공식(예: EPS * PER, 볼린저 밴드 상단, PBR 배수 등)을 사용했고 대입된 수치는 무엇인지 기재.\n"
            f"3. 목표가 산출 논리는 반드시 다음 두 단계를 거쳐 작성하십시오:\n"
            f"   - [1차 퀀트 연산]: 어떤 수학적/통계적 공식을 사용했고 대입된 수치는 무엇인지 기재.\n"
f"   - [2차 정성적 수정]: 1차 퀀트 수치에서 최신 뉴스, 수급, 차트 모멘텀을 반영하여 최종적으로 목표가를 어떻게 가감(수정)했는지 기재.\n\n"
f"[보고서 필수 양식]\n"
f"### 🏆 [최종 추천 종목 3개]\n"
@@ -580,13 +580,13 @@ def build_prompt_recommend_step3(candidate_context, news_list, market_data_str,
f"- 🎯 {horizon} 최종 목표가: [최종 가격]원\n"
f"  └ 🧮 1차 퀀트 연산: [산출 가격]원 (사용된 공식 및 수치 명시)\n"
f"  └ 🧠 2차 정성 수정: (차트/뉴스/수급을 기반으로 1차 가격에서 가감한 논리 명시)\n"
            f"- 💰 진입 타점: [진입가]원 (지지선 및 안전마진 근거)\n\n"
            f"- 💰 진입 타점: [진입가]원 (매수 추천을 위한 지지선 및 안전마진 근거)\n\n"
f"(2번, 3번 종목 동일하게 작성)\n\n"
f"※ 반드시 마지막 줄에 파싱을 위해 아래 형식으로만 적으세요. 다른 글자 추가 절대 금지.\n"
f"[TRACKING_DATA]\n"
            f"종목명1|티커1|단일최종목표가숫자만|진입타점숫자만\n"
            f"종목명2|티커2|단일최종목표가숫자만|진입타점숫자만\n"
            f"종목명3|티커3|단일최종목표가숫자만|진입타점숫자만")
            f"종목명1|티커1|최종목표가숫자만|진입타점숫자만\n"
            f"종목명2|티커2|최종목표가숫자만|진입타점숫자만\n"
            f"종목명3|티커3|최종목표가숫자만|진입타점숫자만")

def build_prompt_deep_dive(stock_name, ticker, news_list, is_owned, avg_price, quantity, current_price, market_data_str, tech_str, supply_str):
fin_data = get_financial_data(ticker)
@@ -605,12 +605,12 @@ def build_prompt_deep_dive(stock_name, ticker, news_list, is_owned, avg_price, q
f"3. 📊 투자의견\n"
f"4. 🎯 기간별 최종 적정 목표가 산출 논리\n"
f"   (※ 반드시 '1차 퀀트(구체적 계산 공식 명시) -> 2차 정성적 분석(차트, 모멘텀, 뉴스를 통한 가감 수정)' 프로세스를 거쳐 서술할 것)\n"
            f"   - 🎯 단기 목표가 (1~3개월): [가격]원 (1차 퀀트 연산 -> 2차 정성 수정)\n"
            f"   - 🎯 중기 목표가 (3~6개월): [가격]원 (1차 퀀트 연산 -> 2차 정성 수정)\n"
            f"   - 🎯 장기 목표가 (1년 이상): [가격]원 (1차 퀀트 연산 -> 2차 정성 수정)\n"
            f"5. 💰 매수/손절 진입 타점: [진입가]원 (지지선 및 안전마진 근거)\n\n"
            f"   - 🎯 단기 목표가 (1~3개월): [최종 가격]원 (1차 퀀트 연산 내역 -> 2차 정성 수정 반영)\n"
            f"   - 🎯 중기 목표가 (3~6개월): [최종 가격]원 (1차 퀀트 연산 내역 -> 2차 정성 수정 반영)\n"
            f"   - 🎯 장기 목표가 (1년 이상): [최종 가격]원 (1차 퀀트 연산 내역 -> 2차 정성 수정 반영)\n"
            f"5. 💰 매수 추천 타점: [진입가]원 (안전마진 및 지지선 기반)\n\n"
f"※ 마지막 줄에 시스템 파싱을 위해 반드시 아래 포맷으로만 기재하십시오. (다른 글자 추가 금지)\n"
            f"TARGET_PRICE: 단기숫자만|중기숫자만|장기숫자만")
            f"TARGET_PRICE: 단기숫자만|중기숫자만|장기숫자만|매수추천가숫자만")

# =======================================================
# 4. 메인 대시보드 UI
@@ -758,9 +758,11 @@ def fetch_candidate_data(c_info):
st.write(display_report)

if "[TRACKING_DATA]" in raw:
            st.markdown("### 📌 AI 분석 추천 매수 밴드 대시보드")
            st.markdown("### 📌 추천 매수 밴드 대시보드")
cols = st.columns(3)
            for idx, line in enumerate(raw.split("[TRACKING_DATA]")[1].strip().split('\n')):
            
            tracking_block = raw.split("[TRACKING_DATA]")[1].strip().replace("```", "")
            for idx, line in enumerate(tracking_block.split('\n')):
data = line.split('|')
if len(data) >= 4:
name, tick = data[0].strip(), data[1].strip()
@@ -774,9 +776,9 @@ def extract(ix):
p_info = get_stock_current_price(tick)
cp, dpct = p_info["current"], p_info["diff_pct"]
st.info(f"**{name}** ({tick})")
                        st.metric("실시간 현재가", f"{cp:,.0f}원", f"전일대비 {dpct:+.2f}%")
                        st.metric("🎯 최종 목표가", f"{tp:,.0f}원", f"현재가 대비 {((tp - cp)/cp)*100:+.1f}%" if cp > 0 and tp > 0 else "")
                        st.metric("💰 매수 추천가", f"{bp:,.0f}원", f"현재가 대비 {((bp - cp)/cp)*100:+.1f}%" if cp > 0 and bp > 0 else "데이터 없음")
                        st.metric("실시간 현재가", f"{cp:,.0f}원", f"{dpct:+.2f}% (전일대비)")
                        st.metric("🎯 최종 목표가", f"{tp:,.0f}원", f"{((tp - cp)/cp)*100:+.1f}% (현재가 대비)" if cp > 0 and tp > 0 else "")
                        st.metric("💰 매수 추천가", f"{bp:,.0f}원", f"{((bp - cp)/cp)*100:+.1f}% (현재가 대비)" if cp > 0 and bp > 0 else "데이터 없음")

if st.button(f"💾 {name} 찜하기", key=f"rec_s_{tick}"):
c.execute("INSERT INTO scrapbook (title, analysis, stock_name, ticker, saved_price, target_price, buy_recommend_price, scrap_date) VALUES (?,?,?,?,?,?,?,?)", 
@@ -786,8 +788,6 @@ def extract(ix):
c.execute("INSERT INTO portfolio (stock_name, ticker, search_query) VALUES (?,?,?)", (name, tick, name))
conn.commit()
st.success(f"'{name}' 스크랩 완료!")
                            time.sleep(0.5)
                            st.rerun()

# =======================================================
# 💡 [탭 5: 관심종목]
@@ -859,6 +859,7 @@ def fetch_stock_raw_worker(p_tuple):
port_cache[p_id] = result
st.session_state.port_data_cache[p_id] = {'data': result, 'time': now_ts}

        @st.fragment
def render_stock_box(p, p_data):
p_id, name, query, ticker, is_owned, avg_price, quantity = p
p_info, fact_news, raw_news, tech_str, supply_str, dart_str = p_data[1], p_data[2], p_data[3], p_data[4], p_data[5], p_data[6]
@@ -890,13 +891,15 @@ def render_stock_box(p, p_data):
combined = {n['link']: n for n in (fact_news + st.session_state.get(f"ai_news_{p_id}", []))}.values()
report = call_gemini_with_fallback(build_prompt_deep_dive(name, ticker, list(combined), is_owned, avg_price, quantity, cur_price, market_data_str, tech_str, supply_str))

                        tp_match = re.search(r'TARGET_PRICE:\s*([^|]+)\|([^|]+)\|(.*)', report)
                        tp_match = re.search(r'TARGET_PRICE:\s*([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|(.*)', report)
def extr(s): return float(re.sub(r'[^\d.]', '', s)) if s and re.sub(r'[^\d.]', '', s) else 0.0
                        
tp_s = extr(tp_match.group(1)) if tp_match else 0.0
tp_m = extr(tp_match.group(2)) if tp_match else 0.0
tp_l = extr(tp_match.group(3)) if tp_match else 0.0
                        bp_val = extr(tp_match.group(4)) if tp_match else 0.0

                        st.session_state.analysis_results[cache_key] = {"text": report, "tp_s": tp_s, "tp_m": tp_m, "tp_l": tp_l, "time": time.time()}
                        st.session_state.analysis_results[cache_key] = {"text": report, "tp_s": tp_s, "tp_m": tp_m, "tp_l": tp_l, "bp": bp_val, "time": time.time()}
st.session_state[f"show_{p_id}"] = True; st.rerun()

if st.session_state.get(f"show_{p_id}") and cache_key in st.session_state.analysis_results:
@@ -906,19 +909,18 @@ def extr(s): return float(re.sub(r'[^\d.]', '', s)) if s and re.sub(r'[^\d.]', '
tp_s = st.session_state.analysis_results[cache_key].get('tp_s', 0.0)
tp_m = st.session_state.analysis_results[cache_key].get('tp_m', 0.0)
tp_l = st.session_state.analysis_results[cache_key].get('tp_l', 0.0)
                    bp_val = st.session_state.analysis_results[cache_key].get('bp', 0.0)

st.markdown(f"#### 🎯 최종 목표가 밴드")
                    st.info(f"**단기 (1~3개월):** {tp_s:,.0f}원  |  **중기 (3~6개월):** {tp_m:,.0f}원  |  **장기 (1년 이상):** {tp_l:,.0f}원")
                    st.info(f"**단기 (1~3개월):** {tp_s:,.0f}원  |  **중기 (3~6개월):** {tp_m:,.0f}원  |  **장기 (1년 이상):** {tp_l:,.0f}원  |  **💰매수추천:** {bp_val:,.0f}원")
st.write(re.sub(r'TARGET_PRICE:.*', '', rep).strip())

c1, c2 = st.columns(2)
if c1.button("💾 저장", key=f"save_{p_id}"):
                        c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long) VALUES (?,?,?,?,?,?,?,?,?,?)", 
                                  (f"[{name}] 리포트", "진단", rep, datetime.now().strftime("%Y-%m-%d %H:%M"), name, ticker, cur_price, tp_s, tp_m, tp_l))
                        c.execute("INSERT INTO scrapbook (title, summary, analysis, scrap_date, stock_name, ticker, saved_price, target_price, target_price_mid, target_price_long, buy_recommend_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)", 
                                  (f"[{name}] 리포트", "진단", rep, datetime.now().strftime("%Y-%m-%d %H:%M"), name, ticker, cur_price, tp_s, tp_m, tp_l, bp_val))
conn.commit()
st.success("저장 완료")
                        time.sleep(0.5)
                        st.rerun()
if c2.button("🔄 재분석", key=f"force_{p_id}"): 
del st.session_state.analysis_results[cache_key]
st.rerun()
@@ -979,15 +981,15 @@ def extr(s): return float(re.sub(r'[^\d.]', '', s)) if s and re.sub(r'[^\d.]', '
cols_sc = st.columns(4)
cols_sc[0].metric("저장가(당시주가)", f"{saved_price:,.0f}원")
return_pct = ((cur - saved_price) / saved_price) * 100 if saved_price > 0 else 0.0
                    cols_sc[1].metric("실시간 주가", f"{cur:,.0f}원", f"일일 {cur_diff_pct:+.2f}% / 누적 {return_pct:+.2f}%")
                    cols_sc[1].metric("실시간 주가", f"{cur:,.0f}원", f"{cur_diff_pct:+.2f}% (일일) / {return_pct:+.2f}% (누적)")

if tp_m > 0 or tp_l > 0:
cols_sc[2].markdown(f"**🎯 목표가 밴드**<br>단기: {tp_s:,.0f}원<br>중기: {tp_m:,.0f}원<br>장기: {tp_l:,.0f}원", unsafe_allow_html=True)
else:
                        cols_sc[2].metric("🎯 최종 목표가", f"{tp_s:,.0f}원", f"저장가 대비 {((tp_s - saved_price)/saved_price)*100:+.1f}%" if saved_price > 0 else "")
                        cols_sc[2].metric("🎯 최종 목표가", f"{tp_s:,.0f}원", f"{((tp_s - saved_price)/saved_price)*100:+.1f}% (저장가 대비)" if saved_price > 0 else "")

if b_rec > 0:
                        cols_sc[3].metric("💰 매수 추천가", f"{b_rec:,.0f}원", f"진입대비 {((cur - b_rec)/b_rec)*100:+.1f}%" if cur > 0 else "")
                        cols_sc[3].metric("💰 매수 추천가", f"{b_rec:,.0f}원", f"{((cur - b_rec)/b_rec)*100:+.1f}% (추천가 대비)" if cur > 0 else "")
else:
cols_sc[3].metric("💰 매수 추천가", "기록 없음")
st.divider()
