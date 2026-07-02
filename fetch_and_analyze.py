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
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
import yfinance as yf
from anthropic import Anthropic

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

def fetch_price_info(ticker: str) -> dict:
    """현재가, 전일대비, 등락률, 20일 모멘텀을 가져옴"""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="2mo")  # 20영업일 모멘텀 계산을 위해 넉넉히 2개월치

        if hist.empty or len(hist) < 2:
            return None

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
        print(f"  [오류] {ticker} 가격 조회 실패: {e}")
        return None


def determine_flow(momentum) -> str:
    """모멘텀 수치를 기준으로 자금흐름 규칙 기반 판단"""
    if momentum is None:
        return "중립"
    if momentum > 2:
        return "자금 유입"
    elif momentum < -2:
        return "자금 유출"
    return "중립"


# =========================================================
# 3. 구글 뉴스 RSS (무료, 키 불필요)
# =========================================================

def get_news_headlines(query: str, max_count: int = 3) -> list:
    try:
        url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
        resp = requests.get(url, timeout=10)
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")[:max_count]
        return [item.find("title").text for item in items if item.find("title") is not None]
    except Exception as e:
        print(f"  [경고] 뉴스 조회 실패 ({query}): {e}")
        return []


# =========================================================
# 4. Claude API 호출 (JSON 응답 요청)
# =========================================================

def call_claude_json(client: Anthropic, system_prompt: str, user_prompt: str) -> dict:
    try:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = "".join(
            block.text for block in message.content if hasattr(block, "text")
        ).strip()
        cleaned = re.sub(r"```json|```", "", raw_text).strip()
        return json.loads(cleaned)
    except Exception as e:
        print(f"  [오류] Claude 분석 실패: {e}")
        return {"__error": str(e)}


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

    flow = determine_flow(price_info["momentum_20d"])

    search_name = re.sub(r"\(.*?\)", "", name).strip()
    headlines = get_news_headlines(search_name + " 주가", 3)

    system_prompt = (
        "당신은 주식 섹터 로테이션을 분석하는 애널리스트입니다. "
        "주어진 모멘텀 수치와 최근 뉴스 헤드라인을 참고해서, "
        "반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요.\n"
        '{"cycle": "현재 싸이클" 또는 "차기 싸이클 가능성" 또는 "-", '
        '"confidence": 1~5 사이 정수, '
        '"news_summary": "20자 이내 한국어 한 줄 요약"}'
    )
    user_prompt = (
        f"종목: {name}\n"
        f"등락률: {price_info['pct_change']}%\n"
        f"20일 모멘텀: {price_info['momentum_20d']}%\n"
        f"최근 뉴스:\n" + ("\n".join(f"- {h}" for h in headlines) if headlines else "(없음)")
    )

    ai_result = call_claude_json(client, system_prompt, user_prompt)
    time.sleep(0.3)  # API 호출 간격

    return {
        "group": group,
        "name": name,
        "ticker": ticker,
        **price_info,
        "flow": flow,
        "cycle": ai_result.get("cycle", "-"),
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
    }

    print("\n[주요지수]")
    for name, ticker in INDICES.items():
        result["indices"].append(analyze_item(client, "주요지수", name, ticker))

    print("\n[한국 섹터]")
    for name, ticker in KR_SECTORS.items():
        result["kr_sectors"].append(analyze_item(client, "한국 섹터", name, ticker))

    print("\n[미국 섹터]")
    for name, ticker in US_SECTORS.items():
        result["us_sectors"].append(analyze_item(client, "미국 섹터", name, ticker))

    print("\n[이달 주요일정]")
    for item in CALENDAR_EVENTS:
        result["calendar"].append(analyze_calendar_event(client, item["date"], item["event"]))

    # data.json 으로 저장 (대시보드가 이 파일을 읽어서 화면에 표시)
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("\n저장 완료: data.json")


if __name__ == "__main__":
    main()
