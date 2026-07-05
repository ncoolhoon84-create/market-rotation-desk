# -*- coding: utf-8 -*-
"""
마켓 로테이션 데스크 - GitHub Actions용 파이썬 버전
------------------------------------------------------
이 스크립트가 하는 일:
1. 지수/한국섹터/미국섹터 가격 데이터를 가져옴 (yfinance)
2. 20일 모멘텀으로 자금흐름을 규칙 기반 자동 판단
3. 구글 뉴스에서 관련 헤드라인을 가져옴 (무료, 키 불필요)
4. Claude API에게 싸이클/신뢰도/뉴스요약을 분석시킴
5. 이달 주요일정도 같은 방식으로 섹터별 영향을 분석
6. 전체 결과를 data.json 파일로 저장 (대시보드가 이 파일을 읽어서 화면에 표시)

로컬에서 테스트하려면:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY="여기에_API_키"
    python fetch_and_analyze.py

GitHub Actions에서는 저장소 Secrets에 등록된 키를 자동으로 읽습니다.

------------------------------------------------------
[2026-07-04 수정사항]
한국 섹터(반도체/2차전지/화장품 등)는 전부 KODEX/TIGER "ETF"인데,
기존에는 일반 주식용 함수(get_market_trading_value_by_date)로
수급을 조회하고 있었음. 이 함수는 ETF에는 원래 지원되지 않는
함수라서, 한국 섹터 14개 전부 수급 데이터가 항상 null로 나오는
버그가 있었음 (KOSPI 지수나 개별 주식은 정상 작동했었음).
-> ETF 전용 함수(get_etf_trading_volume_and_value)로 교체.
   컬럼명도 "기관"/"외국인" -> "기관합계"/"외국인합계"로 통일.
"""

import json
import os
import re
import statistics
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
import yfinance as yf
from anthropic import Anthropic
from pykrx import stock as krx

# =========================================================
# 1. 종목 목록 (구글 시트 버전과 동일한 구성)
# =========================================================

INDICES = {
    "KOSPI": "^KS11",
    "코스닥": "^KQ11",
    "나스닥": "^IXIC",
    "나스닥 선물": "NQ=F",
    "S&P 500": "^GSPC",
    "다우존스": "^DJI",
}

KR_SECTORS = {
    "반도체 (KODEX 반도체)": "091160.KS",
    "2차전지 (TIGER 2차전지테마)": "305540.KS",
    "바이오 (KODEX 헬스케어)": "266420.KS",
    "은행 (KODEX 은행)": "091170.KS",
    "조선 (TIGER 조선TOP10)": "494670.KS",
    "방산 (TIGER K방산&우주)": "463250.KS",
    "전력 (KODEX AI전력핵심설비)": "487240.KS",
    "자동차 (KODEX 자동차)": "091180.KS",
    "화학 (KODEX 에너지화학)": "117460.KS",
    "건설 (KODEX 건설)": "117700.KS",
    "철강 (KODEX 철강)": "117680.KS",
    "미디어·엔터 (TIGER 미디어컨텐츠)": "228810.KS",
    "화장품 (TIGER 화장품)": "228790.KS",
    "로봇 (KODEX 로봇액티브)": "445290.KS",
}

US_SECTORS = {
    "기술 (Technology, XLK)": "XLK",
    "금융 (Financials, XLF)": "XLF",
    "헬스케어 (Healthcare, XLV)": "XLV",
    "에너지 (Energy, XLE)": "XLE",
    "임의소비재 (Cons. Discretionary, XLY)": "XLY",
    "필수소비재 (Cons. Staples, XLP)": "XLP",
    "산업재 (Industrials, XLI)": "XLI",
    "소재 (Materials, XLB)": "XLB",
    "부동산 (Real Estate, XLRE)": "XLRE",
    "유틸리티 (Utilities, XLU)": "XLU",
    "커뮤니케이션 (Communication, XLC)": "XLC",
}

# =========================================================
# 1-1. 섹터별 대표종목 5개 (시가총액 상위 위주)
#      "매수세 랭킹"에서 ETF 하나만으로는 섹터 전체 수급을 대표하기 부족해서,
#      각 섹터의 대표종목 5개 실제 수급을 추가로 조회해 ETF 수급과 합산함.
#      (KRX에 "업종 전체" 수급을 한 번에 주는 함수가 없어서, 대표종목을
#       개별 조회해서 근사치를 만드는 방식 — 완전한 전체 합산은 아님)
# =========================================================
SECTOR_REPRESENTATIVE_STOCKS = {
    "반도체 (KODEX 반도체)": ["005930", "000660", "000990", "042700", "058470"],
    "2차전지 (TIGER 2차전지테마)": ["373220", "006400", "096770", "247540", "003670"],
    "바이오 (KODEX 헬스케어)": ["207940", "068270", "302440", "128940", "000100"],
    "은행 (KODEX 은행)": ["105560", "055550", "086790", "316140", "323410"],
    "조선 (TIGER 조선TOP10)": ["009540", "329180", "010140", "042660", "010620"],
    "방산 (TIGER K방산&우주)": ["012450", "079550", "064350", "047810", "103140"],
    "전력 (KODEX AI전력핵심설비)": ["010120", "298040", "103590", "267260", "034020"],
    "자동차 (KODEX 자동차)": ["005380", "000270", "012330", "018880", "204320"],
    "화학 (KODEX 에너지화학)": ["051910", "011170", "011780", "009830", "010950"],
    "건설 (KODEX 건설)": ["028260", "000720", "006360", "375500", "047040"],
    "철강 (KODEX 철강)": ["005490", "004020", "001230", "003030", "016380"],
    "미디어·엔터 (TIGER 미디어컨텐츠)": ["352820", "035900", "041510", "122870", "035760"],
    "화장품 (TIGER 화장품)": ["090430", "051900", "002790", "192820", "161890"],
    "로봇 (KODEX 로봇액티브)": ["454910", "277810", "090360", "388720", "117730"],
}

# 이달 주요일정 (직접 수정/추가 가능)
CALENDAR_EVENTS = [
    {"date": "2026-07-03", "event": "미국 6월 고용보고서(비농업고용지표) 발표"},
    {"date": "2026-07-14", "event": "미국 6월 CPI(소비자물가) 발표"},
    {"date": "2026-07-15", "event": "2분기 실적시즌 개막 (JP모건 등 주요 은행 실적 발표 시작)"},
    {"date": "2026-07-28", "event": "FOMC 회의 첫날 (금리결정은 익일)"},
    {"date": "2026-07-29", "event": "FOMC 금리결정 발표 및 의장 기자회견"},
]

SECTOR_NAME_LIST = ", ".join(
    list(KR_SECTORS.keys()) + list(US_SECTORS.keys()) + ["지수 전체"]
)

CLAUDE_MODEL = "claude-sonnet-5"


# =========================================================
# 2. 가격/모멘텀 데이터 가져오기
# =========================================================

def fetch_price_info(ticker: str, retries: int = 2) -> dict:
    """현재가, 전일대비, 등락률, 20일 모멘텀을 가져옴 (일시적 실패 시 재시도)"""
    for attempt in range(1, retries + 1):
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2mo")  # 20영업일 모멘텀 계산을 위해 넉넉히 2개월치

            if hist.empty or len(hist) < 2:
                print(f"  [경고] {ticker} 시도 {attempt}/{retries}: 데이터가 비어있음 (행 개수: {len(hist)})")
                time.sleep(2)
                continue

            today_close = hist["Close"].iloc[-1]
            prev_close = hist["Close"].iloc[-2]
            change = today_close - prev_close
            pct_change = (change / prev_close) * 100

            # 20영업일 전 종가 (모멘텀 계산용)
            momentum = None
            if len(hist) >= 21:
                close_20d_ago = hist["Close"].iloc[-21]
                momentum = (today_close - close_20d_ago) / close_20d_ago * 100

            # 5영업일 전 종가 (단기 모멘텀, US 섹터 OBV 판정용으로도 사용)
            momentum_5d = None
            if len(hist) >= 6:
                close_5d_ago = hist["Close"].iloc[-6]
                momentum_5d = (today_close - close_5d_ago) / close_5d_ago * 100

            # OBV(On-Balance Volume) 기반 매수/매도세 판정
            # - 상승일엔 그날 거래량을 더하고 하락일엔 뺀 누적값(OBV)의 최근 5일 변화를 계산
            # - 가격 방향(5일 모멘텀)과 OBV 방향이 같으면 "강"(거래량이 뒷받침),
            #   가격만 움직이고 거래량이 안 따라오면 "약"(힘 빠진 신호, 되돌림 가능성)
            # - KRX 실데이터처럼 완전히 독립적인 소스는 아니지만(같은 가격/거래량 데이터에서 파생),
            #   최소한 "거래량 확인"이라는 두 번째 관점을 더해주는 보조지표
            obv_signal = None
            if len(hist) >= 6:
                closes = hist["Close"].tolist()
                volumes = hist["Volume"].tolist()
                obv_series = [0]
                for i in range(1, len(closes)):
                    if closes[i] > closes[i - 1]:
                        obv_series.append(obv_series[-1] + volumes[i])
                    elif closes[i] < closes[i - 1]:
                        obv_series.append(obv_series[-1] - volumes[i])
                    else:
                        obv_series.append(obv_series[-1])
                obv_change_5d = obv_series[-1] - obv_series[-6]

                if momentum_5d is not None:
                    if momentum_5d > 0:
                        obv_signal = "매수세 강" if obv_change_5d > 0 else "매수세 약"
                    elif momentum_5d < 0:
                        obv_signal = "매도세 강" if obv_change_5d < 0 else "매도세 약"
                    else:
                        obv_signal = "중립"

            volume = int(hist["Volume"].iloc[-1]) if "Volume" in hist else None

            return {
                "price": round(float(today_close), 2),
                "prev_close": round(float(prev_close), 2),
                "change": round(float(change), 2),
                "pct_change": round(float(pct_change), 2),
                "volume": volume,
                "momentum_20d": round(float(momentum), 2) if momentum is not None else None,
                "momentum_5d": round(float(momentum_5d), 2) if momentum_5d is not None else None,
                "obv_signal": obv_signal,
            }
        except Exception as e:
            print(f"  [오류] {ticker} 시도 {attempt}/{retries} 가격 조회 실패: {e}")
            time.sleep(2)

    print(f"  [실패] {ticker}: {retries}번 재시도했지만 데이터를 가져오지 못했습니다.")
    return None


def determine_cycle(momentum, flow: str, foreign_trend: str = None) -> str:
    """
    차기싸이클 판정 (규칙 기반, 100% 결정적 계산 — AI가 아님).
    - 현재 싸이클: 모멘텀 +5% 이상 이고 자금흐름이 '자금 유입'
    - 차기 싸이클 가능성: 자금흐름은 '자금 유입'인데 모멘텀은 아직 +2% 미만
    - 관찰 대상: 정식 '자금 유입' 조건(2일 이상 연속 + 통계적 유의성)은 못 채웠지만,
      최근 5일 외국인 순매수 방향 자체는 매수 우위이고 가격도 오르고 있는 경우.
      (아직 확신하긴 이르지만 지켜볼 가치가 있는 초기 신호)
    - 그 외: "-"
    (이전에는 Claude에게 이 계산을 맡겼으나, LLM이 숫자 임계값을 가끔 정확히
     따르지 않는 문제가 있어 코드로 직접 계산하도록 변경함 — 100% 재현 가능)
    """
    if momentum is None:
        return "-"
    if momentum >= 5 and flow == "자금 유입":
        return "현재 싸이클"
    if flow == "자금 유입" and momentum < 2:
        return "차기 싸이클 가능성"
    if flow != "자금 유입" and foreign_trend == "순매수 우위" and momentum > 0:
        return "관찰 대상"
    return "-"


def determine_flow(momentum=None, flow_data=None) -> str:
    """
    자금흐름 판정.
    - flow_data(개인/기관/외국인 수급 정보 dict)가 있으면 외국인 데이터를 우선 사용 (선행지표).
      단순히 "최근 5일 합계가 양수/음수"만 보지 않고, 아래 조건을 함께 만족해야
      "자금 유입/유출"로 판정 (노이즈에 강하도록):
        1) 최소 2일 이상 연속 같은 방향 (streak)
        2) 최근 5일 평균이 30일 평균 대비 통계적으로 유의미하게 벗어남 (|z-score| > 0.2)
      두 조건 중 하나라도 약하면 "중립"으로 판정해서, 하루 반짝 신호에 흔들리지 않게 함
    - flow_data가 없으면 가격 모멘텀으로 대체 판단 (후행지표, 이미 오른 뒤에야 잡히는 신호)
    """
    if flow_data:
        sum5 = flow_data.get("foreign_recent5_sum", 0)
        streak = flow_data.get("foreign_streak_days", 0) or 0
        z = flow_data.get("foreign_zscore")
        significant = (z is None) or (abs(z) > 0.2)  # z를 못 구했으면 방향만으로 판단

        if sum5 > 0 and streak >= 2 and significant:
            return "자금 유입"
        elif sum5 < 0 and streak >= 2 and significant:
            return "자금 유출"
        return "중립"

    if momentum is None:
        return "중립"
    if momentum > 2:
        return "자금 유입"
    elif momentum < -2:
        return "자금 유출"
    return "중립"


# =========================================================
# 3-1. 개인/기관/외국인 수급 (한국거래소 실데이터, pykrx)
#      KOSPI 지수든, 개별 섹터 ETF든 동일하게 사용 가능
# =========================================================

def fetch_investor_flow(krx_ticker: str, is_etf: bool = False):
    """
    최근 거래일의 개인/기관/외국인 순매수 금액, 최근 5거래일 추세를 가져오고,
    노이즈에 강하도록 두 가지를 추가로 계산함:
    - foreign_streak_days: 외국인이 며칠 연속 같은 방향(순매수/순매도)인지
    - foreign_zscore: 최근 5일 평균이 최근 30거래일 평균 대비 통계적으로 얼마나 이례적인지
      (0에 가까우면 평소와 비슷, 클수록 평소보다 훨씬 강한 매수/매도세)
    krx_ticker: "KOSPI" 같은 지수명 또는 "091160" 같은 6자리 종목코드
    is_etf: True면 ETF 전용 조회 함수를 사용함.
      (KODEX/TIGER 같은 한국 섹터 ETF는 일반 주식용 함수로는 조회가 안 되고,
       반드시 ETF 전용 함수를 써야 함 — 안 그러면 항상 실패함)
    금액 단위: 원 (양수=순매수, 음수=순매도)
    """
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=50)).strftime("%Y%m%d")  # 30영업일 확보 위해 넉넉히

        if is_etf:
            df = krx.get_etf_trading_volume_and_value(start, end, krx_ticker, "거래대금", "순매수")
            if df is not None and not df.empty:
                # ETF 전용 함수는 컬럼명이 "기관"/"외국인" (합계 안 붙음) 이라
                # 기존 로직이 기대하는 "기관합계"/"외국인합계"로 통일시켜줌
                df = df.rename(columns={"기관": "기관합계", "외국인": "외국인합계"})
        else:
            df = krx.get_market_trading_value_by_date(start, end, krx_ticker)

        if df is None or df.empty or len(df) < 5:
            print(f"  [경고] {krx_ticker} 수급 데이터가 비어있거나 부족합니다.")
            return None

        latest = df.iloc[-1]
        recent5 = df.tail(5)
        foreign_series = df["외국인합계"].tolist()
        baseline = foreign_series[-30:] if len(foreign_series) >= 30 else foreign_series

        # --- 강도(Z-score): 최근 5일 평균이 최근 30일 평균 대비 몇 표준편차만큼 벗어났는지 ---
        baseline_avg = statistics.mean(baseline)
        baseline_std = statistics.pstdev(baseline) if len(baseline) > 1 else 0
        recent5_avg = statistics.mean(recent5["외국인합계"].tolist())
        foreign_zscore = round((recent5_avg - baseline_avg) / baseline_std, 2) if baseline_std > 0 else None

        # --- 연속일수(streak): 최근일부터 거꾸로 같은 방향이 며칠 이어지는지 ---
        streak = 0
        sign = None
        for val in reversed(foreign_series):
            cur_sign = 1 if val > 0 else (-1 if val < 0 else 0)
            if sign is None:
                sign = cur_sign
                if sign == 0:
                    break
                streak = 1
            elif cur_sign == sign:
                streak += 1
            else:
                break

        def trend_label(col_name):
            total = recent5[col_name].sum()
            if total > 0:
                return "순매수 우위"
            elif total < 0:
                return "순매도 우위"
            return "중립"

        individual_trend = trend_label("개인")
        institution_trend = trend_label("기관합계")
        foreign_trend = trend_label("외국인합계")

        # 최근 5거래일(이번 주) vs 그 이전 5거래일(지난 주) 순매수 비교
        # -> "매수세 랭킹"에서 순매수액과 증가율을 함께 보여주기 위함
        def weekly_summary(col_name):
            recent5_sum = int(recent5[col_name].sum())
            prior5 = df[col_name].iloc[-10:-5] if len(df) >= 10 else None
            prior5_sum = int(prior5.sum()) if prior5 is not None and len(prior5) == 5 else None
            growth_pct = None
            if prior5_sum:  # 0이거나 None이면 증가율 계산 불가(분모 0 방지)
                growth_pct = round((recent5_sum - prior5_sum) / abs(prior5_sum) * 100, 1)
            return {"net_buy": recent5_sum, "growth_pct": growth_pct, "prior5_sum": prior5_sum}

        weekly = {
            "individual": weekly_summary("개인"),
            "institution": weekly_summary("기관합계"),
            "foreign": weekly_summary("외국인합계"),
        }

        # 최근 5거래일 일별 개인/기관/외국인 순매수 금액 (대시보드 마우스오버 미니차트용)
        # 날짜 오름차순(과거 -> 최근)으로 정렬해서 넘김
        recent5_days = []
        for idx, row in recent5.iterrows():
            day_date = idx.date() if hasattr(idx, "date") else idx
            recent5_days.append({
                "date": str(day_date),
                "individual": int(row.get("개인", 0)),
                "institution": int(row.get("기관합계", 0)),
                "foreign": int(row.get("외국인합계", 0)),
            })

        return {
            "date": str(df.index[-1].date()),
            "individual": int(latest.get("개인", 0)),
            "institution": int(latest.get("기관합계", 0)),
            "foreign": int(latest.get("외국인합계", 0)),
            "recent5_days": recent5_days,
            "weekly_summary": weekly,
            "foreign_recent5_sum": int(recent5["외국인합계"].sum()),
            "foreign_streak_days": streak,
            "foreign_zscore": foreign_zscore,
            "individual_trend": individual_trend,
            "institution_trend": institution_trend,
            "foreign_trend": foreign_trend,
            "trend_summary": (
                f"개인 최근 5거래일 {individual_trend} · "
                f"기관 최근 5거래일 {institution_trend} · "
                f"외국인 최근 5거래일 {foreign_trend} "
                f"({streak}일 연속" + (f", 평소 대비 강도 {foreign_zscore})" if foreign_zscore is not None else ")")
            ),
        }
    except Exception as e:
        print(f"  [경고] {krx_ticker} 수급 데이터 조회 실패: {e}")
        return None


# =========================================================
# 3. 구글 뉴스 RSS (무료, 키 불필요)
# =========================================================

def get_news_headlines(query: str, max_count: int = 3) -> list:
    """뉴스 헤드라인과 링크를 함께 가져옴. 반환값: [{"title": ..., "link": ...}, ...]"""
    try:
        url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
        resp = requests.get(url, timeout=10)
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")[:max_count]
        results = []
        for item in items:
            title_el = item.find("title")
            link_el = item.find("link")
            if title_el is not None:
                results.append({
                    "title": title_el.text,
                    "link": link_el.text if link_el is not None else None,
                })
        return results
    except Exception as e:
        print(f"  [경고] 뉴스 조회 실패 ({query}): {e}")
        return []


# =========================================================
# 4. Claude API 호출 (JSON 응답 요청)
# =========================================================

def call_claude_json(client: Anthropic, system_prompt: str, user_prompt: str, retries: int = 2) -> dict:
    """Claude에게 JSON 응답을 요청. 파싱 실패 시 '고쳐서 다시 보내라'고 재요청."""
    messages = [{"role": "user", "content": user_prompt}]

    for attempt in range(1, retries + 1):
        try:
            message = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=500,
                system=system_prompt,
                messages=messages,
            )
            raw_text = "".join(
                block.text for block in message.content if hasattr(block, "text")
            ).strip()
            cleaned = re.sub(r"```json|```", "", raw_text).strip()

            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as parse_err:
                print(f"  [경고] JSON 파싱 실패 (시도 {attempt}/{retries}): {parse_err}")
                if attempt < retries:
                    # 모델에게 이전 응답을 보여주고, 유효한 JSON으로 고쳐 달라고 재요청
                    messages.append({"role": "assistant", "content": raw_text})
                    messages.append({
                        "role": "user",
                        "content": (
                            "방금 응답은 유효한 JSON이 아니었습니다. 문자열 안의 따옴표(\")는 "
                            "반드시 \\\" 로 이스케이프하고, 오직 유효한 JSON만 다시 응답하세요. "
                            "다른 설명 텍스트는 절대 포함하지 마세요."
                        )
                    })
                    continue
                return {"__error": f"JSON 파싱 실패: {str(parse_err)}"}

        except Exception as e:
            print(f"  [오류] Claude API 호출 실패 (시도 {attempt}/{retries}): {e}")
            if attempt >= retries:
                return {"__error": str(e)}
            time.sleep(1)

    return {"__error": "알 수 없는 오류"}


# =========================================================
# 5. 섹터/지수 하나를 완전히 분석 (가격 + 흐름 + AI)
# =========================================================

def analyze_item(client: Anthropic, group: str, name: str, ticker: str) -> dict:
    print(f"  분석 중: {name} ({ticker})")

    price_info = fetch_price_info(ticker)
    if price_info is None:
        return {
            "group": group, "name": name, "ticker": ticker,
            "error": "가격 데이터를 가져오지 못했습니다.",
        }

    # 한국 종목(.KS로 끝남)이면 외국인 수급(선행지표)을 우선 사용,
    # 미국 종목이면 KRX 데이터가 없으므로 가격 모멘텀(후행지표)으로 대체.
    # 한국 섹터는 전부 KODEX/TIGER "ETF"이므로 반드시 ETF 전용 함수(is_etf=True)로 조회해야 함.
    investor_flow = None
    if ticker.endswith(".KS"):
        krx_code = ticker.replace(".KS", "")
        investor_flow = fetch_investor_flow(krx_code, is_etf=True)

    foreign_recent_sum = investor_flow["foreign_recent5_sum"] if investor_flow else None
    flow = determine_flow(price_info["momentum_20d"], investor_flow)

    search_name = re.sub(r"\(.*?\)", "", name).strip()
    headlines = get_news_headlines(search_name + " 주가", 3)

    system_prompt = (
        "당신은 주식 섹터 로테이션을 분석하는 애널리스트입니다. "
        "주어진 모멘텀, 자금흐름 판정, 최근 뉴스를 참고해서 "
        "신뢰도 점수와 한 줄 뉴스 요약만 작성하세요. "
        "반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요.\n"
        '{"confidence": 1~5 사이 정수 (모멘텀과 자금흐름 방향이 서로 일치할수록, '
        '뉴스 근거가 뚜렷할수록 높게), '
        '"news_summary": "20자 이내 한국어 한 줄 요약"}'
    )
    user_prompt = (
        f"종목: {name}\n"
        f"등락률: {price_info['pct_change']}%\n"
        f"20일 모멘텀(후행지표): {price_info['momentum_20d']}%\n"
        + (
            f"외국인 자금흐름 판정(선행지표): {flow} "
            f"(최근 5일 순매수 {foreign_recent_sum:,}원, {investor_flow['foreign_streak_days']}일 연속, "
            f"평소 대비 강도 {investor_flow['foreign_zscore']})\n"
            if investor_flow else ""
        )
        + f"최근 뉴스:\n" + ("\n".join(f"- {h['title']}" for h in headlines) if headlines else "(없음)")
    )

    ai_result = call_claude_json(client, system_prompt, user_prompt)
    time.sleep(0.3)  # API 호출 간격

    return {
        "group": group,
        "name": name,
        "ticker": ticker,
        **price_info,
        "flow": flow,
        "flow_basis": "외국인 수급(선행지표)" if investor_flow else "가격 모멘텀(후행지표)",
        "investor_flow": investor_flow,
        "cycle": determine_cycle(
            price_info["momentum_20d"],
            flow,
            investor_flow.get("foreign_trend") if investor_flow else None,
        ),
        "confidence": ai_result.get("confidence", 3),
        "news_summary": ai_result.get("news_summary", ai_result.get("__error", "")),
        "headlines": headlines,
    }


# =========================================================
# 6. 캘린더 이벤트 영향 분석
# =========================================================

def analyze_calendar_event(client: Anthropic, date: str, event: str) -> dict:
    print(f"  캘린더 분석 중: {date} - {event}")

    headlines = get_news_headlines(event, 3)

    system_prompt = (
        "당신은 거시경제 이벤트가 주식 섹터에 미치는 영향을 분석하는 애널리스트입니다. "
        "반드시 아래 JSON 형식으로만 답변하세요.\n"
        '{"affected_sectors": "영향받는 섹터 1~3개를 쉼표로 (다음 목록 중에서만 선택: '
        + SECTOR_NAME_LIST + ')", '
        '"impact": "호재" 또는 "악재" 또는 "중립", '
        '"comment": "이 이벤트로 예상되는 상황(원인) → 그로 인한 결과 → 어떤 섹터에 '
        '호재/악재로 작용할지까지 이어지는 한 문장. 60자 이내 한국어. '
        '예시 형식: \'고용지표가 예상보다 강하게 나오면 금리인하 기대가 후퇴해 반도체 섹터에 악재로 작용 예상\'"}'
    )
    user_prompt = (
        f"날짜: {date}\n이벤트: {event}\n"
        + "최근 관련 뉴스:\n"
        + ("\n".join(f"- {h['title']}" for h in headlines) if headlines else "(없음)")
    )

    ai_result = call_claude_json(client, system_prompt, user_prompt)
    time.sleep(0.3)

    return {
        "date": date,
        "event": event,
        "affected_sectors": ai_result.get("affected_sectors", ""),
        "impact": ai_result.get("impact", "중립"),
        "comment": ai_result.get("comment", ai_result.get("__error", "")),
        "headlines": headlines,
    }


# =========================================================
# 5-1. 메가트렌드 / 수급 뉴스 — 개별 종목이 아닌 "시장 전체" 관점 트렌드 추출
# =========================================================

MEGATREND_QUERIES = [
    "외국인 순매수 동향",
    "글로벌 자금 흐름 신흥산업",
    "차세대 유망 산업 테마",
    "수급 주도주 전망",
]

def analyze_megatrends(client: Anthropic) -> list:
    print("\n[메가트렌드 / 수급 뉴스] 분석 중...")

    all_headlines = []
    for q in MEGATREND_QUERIES:
        all_headlines += get_news_headlines(q, 4)
        time.sleep(0.2)

    if not all_headlines:
        return []

    headline_text = "\n".join(f"- {h['title']}" for h in all_headlines)

    system_prompt = (
        "당신은 시장 전체의 자금 흐름과 산업 트렌드를 조기에 포착하는 수석 애널리스트입니다. "
        "아래 뉴스 헤드라인들을 종합해서, 앞으로 주목할 만한 '메가트렌드'(특정 종목이 아닌 "
        "산업/테마/자금흐름 단위의 큰 흐름) 3~5개를 뽑아주세요. "
        "반드시 아래 JSON 배열 형식으로만 답변하세요. 다른 텍스트는 포함하지 마세요.\n"
        '[{"trend": "트렌드 제목 (15자 이내)", '
        '"description": "왜 주목해야 하는지 1문장 설명 (40자 이내)", '
        '"related_sectors": "관련 섹터 1~3개 쉼표 구분"}]'
    )
    user_prompt = "최근 수집된 뉴스 헤드라인:\n" + headline_text

    ai_result = call_claude_json(client, system_prompt, user_prompt)
    time.sleep(0.3)

    if isinstance(ai_result, list):
        # 참고용 근거 뉴스(제목+링크)를 같이 붙여서 반환
        for trend in ai_result:
            trend["source_headlines"] = all_headlines[:5]
        return ai_result

    print(f"  [경고] 메가트렌드 분석 실패: {ai_result}")
    return []


# =========================================================
# 6-1. NaN/Infinity 값을 JSON에 안전하게 쓸 수 있도록 정리
#      (파이썬은 NaN을 허용하지만, 브라우저는 이를 유효한 JSON으로
#      인식하지 못해 "Unexpected token" 에러를 일으킵니다)
# =========================================================
def clean_for_json(obj):
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):  # NaN 또는 무한대
            return None
        return obj
    if isinstance(obj, dict):
        return {k: clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_for_json(v) for v in obj]
    return obj


# =========================================================
# 6-2. 이번 주 매수세 랭킹 — 히스토리 기반 지난주 순위/신규진입/연속주차 계산
#      (data.json과 별도로 ranking_history.json 파일에 매일 스냅샷을 저장해서,
#       다음 실행 때 "지난주"와 비교할 수 있게 함. GitHub Actions가 이 파일도
#       같이 커밋해야 하므로 update.yml의 git add에도 추가해야 함)
# =========================================================
RANKING_HISTORY_FILE = "ranking_history.json"
RANKING_INVESTOR_KEYS = ["individual", "institution", "foreign"]


def load_ranking_history() -> list:
    if not os.path.exists(RANKING_HISTORY_FILE):
        return []
    try:
        with open(RANKING_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [경고] {RANKING_HISTORY_FILE} 읽기 실패: {e}")
        return []


def save_ranking_history(history: list):
    with open(RANKING_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(clean_for_json(history), f, ensure_ascii=False, indent=2, allow_nan=False)


def clean_sector_name(name: str) -> str:
    """랭킹 표시용: '반도체 (KODEX 반도체)' -> '반도체' 처럼 괄호 안 ETF/티커명 제거.
    (매수세 랭킹은 섹터 자체의 수급을 보여주는 것이지, 특정 ETF 상품을
     매수했다는 뜻이 아니므로 오해를 막기 위해 이름을 단순화함)"""
    return re.sub(r"\(.*?\)", "", name).strip()


def fetch_sector_representative_totals(tickers: list) -> dict:
    """섹터 대표종목 5개의 최근5일/직전5일 순매수 합계를 투자자 유형별로 합산"""
    totals = {k: {"net_buy": 0, "prior5_sum": 0} for k in RANKING_INVESTOR_KEYS}
    for ticker in tickers:
        flow = fetch_investor_flow(ticker, is_etf=False)
        time.sleep(0.3)
        if not flow or "weekly_summary" not in flow:
            continue
        for k in RANKING_INVESTOR_KEYS:
            ws = flow["weekly_summary"].get(k)
            if ws:
                totals[k]["net_buy"] += ws["net_buy"]
                totals[k]["prior5_sum"] += (ws.get("prior5_sum") or 0)
    return totals


def rep_stocks_only_weekly(rep_totals) -> dict:
    """대표종목 5개 합산 수급만으로 순매수/증가율 계산 (ETF 수급은 랭킹에서 제외)"""
    net_buy = rep_totals["net_buy"]
    prior5_sum = rep_totals["prior5_sum"]
    growth_pct = round((net_buy - prior5_sum) / abs(prior5_sum) * 100, 1) if prior5_sum else None
    return {"net_buy": net_buy, "growth_pct": growth_pct}


def compute_ranked_list(kr_sectors: list, investor_key: str, direction: str = "buy") -> list:
    """direction='buy'면 순매수 상위, 'sell'이면 순매도 상위(가장 많이 판 순)를 반환"""
    rows = []
    for item in kr_sectors:
        iv = item.get("investor_flow")
        if not iv:
            continue
        source = iv.get("sector_wide_weekly")
        if not source:
            continue
        ws = source.get(investor_key)
        if not ws:
            continue
        if direction == "buy" and ws["net_buy"] <= 0:
            continue
        if direction == "sell" and ws["net_buy"] >= 0:
            continue
        rows.append({"name": clean_sector_name(item["name"]), "net_buy": ws["net_buy"], "growth_pct": ws["growth_pct"]})
    rows.sort(key=lambda r: r["net_buy"], reverse=(direction == "buy"))
    return rows[:10]


def find_snapshot_near(history_by_date: dict, target_date, tolerance_days: int = 2):
    """target_date 기준 +-tolerance_days 이내에서 가장 가까운 스냅샷을 찾음 (주말/휴장 보정용)"""
    for offset in range(0, tolerance_days + 1):
        for d in (target_date - timedelta(days=offset), target_date + timedelta(days=offset)):
            snap = history_by_date.get(d.isoformat())
            if snap:
                return snap
    return None


def lookup_rank(snapshot, field: str, investor_key: str, name: str):
    if not snapshot:
        return None
    lst = snapshot.get(field, {}).get(investor_key, [])
    for i, row in enumerate(lst):
        if row["name"] == name:
            return i + 1
    return None


def compute_streak_weeks(history_by_date: dict, field: str, investor_key: str, name: str, today_date, max_weeks: int = 12) -> int:
    """오늘을 포함해서, 7일 간격으로 거슬러 올라가며 몇 주 연속 TOP5였는지 계산"""
    streak = 1  # 오늘 이미 TOP5에 있다는 전제 하에 호출됨
    check_date = today_date
    for _ in range(1, max_weeks):
        check_date = check_date - timedelta(days=7)
        snap = find_snapshot_near(history_by_date, check_date)
        if not snap:
            break
        rank = lookup_rank(snap, field, investor_key, name)
        if rank is not None and rank <= 5:
            streak += 1
        else:
            break
    return streak


def build_weekly_ranking(kr_sectors: list) -> dict:
    """개인/기관/외국인별 매수 TOP5 + 매도 TOP5, 그리고 각각의 지난주 순위/신규진입/연속주차를 계산하고
    오늘자 스냅샷(매수·매도 모두)을 히스토리 파일에 기록함"""
    today_date = datetime.now().date()
    history = load_ranking_history()
    history_by_date = {h["date"]: h for h in history}

    buy_top10 = {key: compute_ranked_list(kr_sectors, key, "buy") for key in RANKING_INVESTOR_KEYS}
    sell_top10 = {key: compute_ranked_list(kr_sectors, key, "sell") for key in RANKING_INVESTOR_KEYS}

    def enrich(top10_by_type: dict, field: str) -> dict:
        out = {}
        for key in RANKING_INVESTOR_KEYS:
            top5 = top10_by_type[key][:5]
            enriched = []
            for i, row in enumerate(top5):
                prior_snap = find_snapshot_near(history_by_date, today_date - timedelta(days=7))
                last_week_rank = lookup_rank(prior_snap, field, key, row["name"])
                is_new_entry = last_week_rank is None
                weeks_in_top5 = 1 if is_new_entry else compute_streak_weeks(history_by_date, field, key, row["name"], today_date)
                enriched.append({
                    **row,
                    "rank": i + 1,
                    "last_week_rank": last_week_rank,
                    "is_new_entry": is_new_entry,
                    "weeks_in_top5": weeks_in_top5,
                })
            out[key] = enriched
        return out

    result = {
        "buy": enrich(buy_top10, "rankings"),
        "sell": enrich(sell_top10, "sell_rankings"),
    }

    # 오늘자 스냅샷 기록 (매수/매도 둘 다, 같은 날 여러 번 실행돼도 마지막 실행 값으로 덮어씀)
    history_by_date[today_date.isoformat()] = {
        "date": today_date.isoformat(),
        "rankings": buy_top10,
        "sell_rankings": sell_top10,
    }
    cutoff = (today_date - timedelta(days=90)).isoformat()
    new_history = sorted(
        [h for h in history_by_date.values() if h["date"] >= cutoff],
        key=lambda h: h["date"],
    )
    save_ranking_history(new_history)

    return result


# =========================================================
# 7. 메인 실행
# =========================================================

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY 환경변수가 없습니다. "
            "로컬에서는 export ANTHROPIC_API_KEY=..., "
            "GitHub Actions에서는 Secrets에 등록하세요."
        )
    client = Anthropic(api_key=api_key)

    print("=" * 55)
    print(" 마켓 로테이션 데스크 - 데이터 수집 + AI 분석 시작")
    print(" 실행 시각:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 55)

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "indices": [],
        "kr_sectors": [],
        "us_sectors": [],
        "calendar": [],
        "megatrends": [],
    }

    print("\n[KOSPI/코스닥 수급(개인/기관/외국인)]")
    kospi_flow = fetch_investor_flow("KOSPI")   # KOSPI는 지수이지 ETF가 아니므로 is_etf=False(기본값)
    kosdaq_flow = fetch_investor_flow("KOSDAQ")  # 코스닥도 KRX가 시장 전체 수급을 동일 방식으로 제공함

    print("\n[주요지수]")
    for name, ticker in INDICES.items():
        item = analyze_item(client, "주요지수", name, ticker)
        if name == "KOSPI" and kospi_flow:
            item["investor_flow"] = kospi_flow
            item["flow"] = determine_flow(item.get("momentum_20d"), kospi_flow)
            item["flow_basis"] = "외국인 수급(선행지표)"
        elif name == "코스닥" and kosdaq_flow:
            item["investor_flow"] = kosdaq_flow
            item["flow"] = determine_flow(item.get("momentum_20d"), kosdaq_flow)
            item["flow_basis"] = "외국인 수급(선행지표)"
        result["indices"].append(item)

    print("\n[한국 섹터]")
    for name, ticker in KR_SECTORS.items():
        result["kr_sectors"].append(analyze_item(client, "한국 섹터", name, ticker))

    print("\n[섹터별 대표종목 5개 수급 조회 - 매수세 랭킹용 (ETF 제외, 대표종목 5개만 반영)]")
    for item in result["kr_sectors"]:
        reps = SECTOR_REPRESENTATIVE_STOCKS.get(item["name"])
        if not reps:
            continue
        print(f"  대표종목 조회 중: {item['name']} {reps}")
        rep_totals = fetch_sector_representative_totals(reps)
        sector_wide = {key: rep_stocks_only_weekly(rep_totals[key]) for key in RANKING_INVESTOR_KEYS}
        if item.get("investor_flow") is None:
            item["investor_flow"] = {}
        item["investor_flow"]["sector_wide_weekly"] = sector_wide

    print("\n[이번 주 매수세 랭킹 계산 + 히스토리 기록]")
    result["ranking"] = build_weekly_ranking(result["kr_sectors"])

    print("\n[미국 섹터]")
    for name, ticker in US_SECTORS.items():
        result["us_sectors"].append(analyze_item(client, "미국 섹터", name, ticker))

    print("\n[이달 주요일정]")
    for item in CALENDAR_EVENTS:
        result["calendar"].append(analyze_calendar_event(client, item["date"], item["event"]))

    result["megatrends"] = analyze_megatrends(client)

    # data.json 으로 저장 (대시보드가 이 파일을 읽어서 화면에 표시)
    # NaN/Infinity 값을 null로 정리한 뒤, allow_nan=False로 유효한 JSON만 생성되도록 함
    cleaned_result = clean_for_json(result)
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(cleaned_result, f, ensure_ascii=False, indent=2, allow_nan=False)

    print("\n저장 완료: data.json")


if __name__ == "__main__":
    main()
