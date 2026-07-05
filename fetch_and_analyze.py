# -*- coding: utf-8 -*-
"""
지수 전용 초경량 갱신 스크립트 (5분마다 실행)
------------------------------------------------
전체 파이프라인(fetch_and_analyze.py)은 하루 4번만 도는데, 주요지수
가격만큼은 최대한 자주 보고 싶어서 별도로 분리한 스크립트.

이 스크립트가 하는 일:
- 주요지수 6개(KOSPI/코스닥/나스닥/나스닥선물/S&P500/다우존스)의
  현재가·전일대비·등락률만 가져와서 indices_live.json 에 저장
- Claude API 호출 없음 (비용 0원)
- 개인/기관/외국인 수급(KRX/pykrx) 조회 없음 (KRX 쪽 요청 부담을 늘리지 않기 위함)
- 20일 모멘텀, AI 신뢰도, 뉴스요약, 수급 등은 여전히 하루 4번 도는
  fetch_and_analyze.py + data.json이 담당함

index.html은 이 파일을 60~90초 간격으로 따로 불러와서 주요지수 카드의
가격/등락률 부분만 갱신하고, 나머지(뉴스/수급/AI분석)는 기존 data.json
값을 그대로 유지함.

로컬 테스트:
    pip install yfinance curl_cffi
    python fetch_indices_live.py
"""

import json
import time
from datetime import datetime, timezone

import yfinance as yf

try:
    from curl_cffi import requests as cffi_requests
    # Yahoo Finance가 데이터센터/클라우드 IP(GitHub Actions 등)를 봇으로
    # 차단하는 경우가 많아, 실제 크롬 브라우저처럼 보이는 세션을 사용함.
    # (fetch_and_analyze.py와 동일한 방식)
    _YF_SESSION = cffi_requests.Session(impersonate="chrome")
except ImportError:
    print("[경고] curl_cffi가 설치되어 있지 않습니다. requirements.txt에 curl_cffi를 추가하세요.")
    _YF_SESSION = None

INDICES = {
    "KOSPI": "^KS11",
    "코스닥": "^KQ11",
    "나스닥": "^IXIC",
    "나스닥 선물": "NQ=F",
    "S&P 500": "^GSPC",
    "다우존스": "^DJI",
}


def fetch_price_only(ticker: str, retries: int = 2):
    """현재가/전일대비/등락률만 가볍게 조회 (모멘텀 계산 없음, 5일치면 충분)"""
    for attempt in range(1, retries + 1):
        try:
            t = yf.Ticker(ticker, session=_YF_SESSION) if _YF_SESSION else yf.Ticker(ticker)
            hist = t.history(period="5d")

            if hist.empty or len(hist) < 2:
                print(f"  [경고] {ticker} 시도 {attempt}/{retries}: 데이터가 비어있음")
                time.sleep(attempt * 2)
                continue

            today_close = hist["Close"].iloc[-1]
            prev_close = hist["Close"].iloc[-2]
            change = today_close - prev_close
            pct_change = (change / prev_close) * 100

            return {
                "price": round(float(today_close), 2),
                "change": round(float(change), 2),
                "pct_change": round(float(pct_change), 2),
            }
        except Exception as e:
            print(f"  [오류] {ticker} 시도 {attempt}/{retries}: {e}")
            time.sleep(attempt * 2)

    print(f"  [실패] {ticker}: 가격 조회 실패")
    return None


def main():
    print("[지수 실시간 갱신] 실행 시각:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "indices": {},
    }

    for name, ticker in INDICES.items():
        info = fetch_price_only(ticker)
        if info:
            result["indices"][name] = info
            print(f"  {name}: {info['price']} ({info['pct_change']}%)")

    with open("indices_live.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("저장 완료: indices_live.json")


if __name__ == "__main__":
    main()
