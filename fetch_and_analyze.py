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
    "S&P 500": "^GSPC",
    "NASDAQ": "^IXIC",
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

            volume = int(hist["Volume"].iloc[-1]) if "Volume" in hist else None

            return {
                "price": round(float(today_close), 2),
                "prev_close": round(float(prev_close), 2),
                "change": round(float(change), 2),
                "pct_change": round(float(pct_change), 2),
                "volume": volume,
                "momentum_20d": round(float(momentum), 2) if momentum is not None else None,
            }
        except Exception as e:
            print(f"  [오류] {ticker} 시도 {attempt}/{retries} 가격 조회 실패: {e}")
            time.sleep(2)

    print(f"  [실패] {ticker}: {retries}번 재시도했지만 데이터를 가져오지 못했습니다.")
    return None


def determine_cycle(momentum, flow: str) -> str:
    """
    차기싸이클 판정 (규칙 기반, 100% 결정적 계산 — AI가 아님).
    - 현재 싸이클: 모멘텀 +5% 이상 이고 자금흐름이 '자금 유입'
    - 차기 싸이클 가능성: 자금흐름은 '자금 유입'인데 모멘텀은 아직 +2% 미만
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

def fetch_investor_flow(krx_ticker: str):
    """
    최근 거래일의 개인/기관/외국인 순매수 금액, 최근 5거래일 추세를 가져오고,
    노이즈에 강하도록 두 가지를 추가로 계산함:
    - foreign_streak_days: 외국인이 며칠 연속 같은 방향(순매수/순매도)인지
    - foreign_zscore: 최근 5일 평균이 최근 30거래일 평균 대비 통계적으로 얼마나 이례적인지
      (0에 가까우면 평소와 비슷, 클수록 평소보다 훨씬 강한 매수/매도세)
    krx_ticker: "KOSPI" 같은 지수명 또는 "091160" 같은 6자리 종목코드
    금액 단위: 원 (양수=순매수, 음수=순매도)
    """
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=50)).strftime("%Y%m%d")  # 30영업일 확보 위해 넉넉히

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

        return {
            "date": str(df.index[-1].date()),
            "individual": int(latest.get("개인", 0)),
            "institution": int(latest.get("기관합계", 0)),
            "foreign": int(latest.get("외국인합계", 0)),
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
    # 미국 종목이면 KRX 데이터가 없으므로 가격 모멘텀(후행지표)으로 대체
    investor_flow = None
    if ticker.endswith(".KS") or ticker == "KRX:KOSPI":
        krx_code = ticker.replace(".KS", "")
        investor_flow = fetch_investor_flow(krx_code)

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
        "cycle": determine_cycle(price_info["momentum_20d"], flow),
        "confidence": ai_result.get("confidence", 3),
        "news_summary": ai_result.get("news_summary", ai_result.get("__error", "")),
        "headlines": headlines,
    }


# =========================================================
# 6. 캘린더 이벤트 영향 분석
# =========================================================

def analyze_calendar_event(client: Anthropic, date: str, event: str) -> dict:
    print(f"  캘린더 분석 중: {date} - {event}")

    system_prompt = (
        "당신은 거시경제 이벤트가 주식 섹터에 미치는 영향을 분석하는 애널리스트입니다. "
        "반드시 아래 JSON 형식으로만 답변하세요.\n"
        '{"affected_sectors": "영향받는 섹터 1~3개를 쉼표로 (다음 목록 중에서만 선택: '
        + SECTOR_NAME_LIST + ')", '
        '"impact": "호재" 또는 "악재" 또는 "중립", '
        '"comment": "30자 이내 한국어 코멘트"}'
    )
    user_prompt = f"날짜: {date}\n이벤트: {event}"

    ai_result = call_claude_json(client, system_prompt, user_prompt)
    time.sleep(0.3)

    return {
        "date": date,
        "event": event,
        "affected_sectors": ai_result.get("affected_sectors", ""),
        "impact": ai_result.get("impact", "중립"),
        "comment": ai_result.get("comment", ai_result.get("__error", "")),
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

    print("\n[KOSPI 수급(개인/기관/외국인)]")
    kospi_flow = fetch_investor_flow("KOSPI")

    print("\n[주요지수]")
    for name, ticker in INDICES.items():
        item = analyze_item(client, "주요지수", name, ticker)
        if name == "KOSPI" and kospi_flow:
            item["investor_flow"] = kospi_flow
            item["flow"] = determine_flow(item.get("momentum_20d"), kospi_flow)
            item["flow_basis"] = "외국인 수급(선행지표)"
        result["indices"].append(item)

    print("\n[한국 섹터]")
    for name, ticker in KR_SECTORS.items():
        result["kr_sectors"].append(analyze_item(client, "한국 섹터", name, ticker))

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
