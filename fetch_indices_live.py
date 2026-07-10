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

[2026-07-11 수정사항]
KOSPI/코스닥이 화면에 NaN으로 뜨는 버그 발견. 원인은 fetch_price_only()가
hist가 비어있는지/행 개수만 체크하고, 그 안의 실제 종가(Close) 값 자체가
NaN인 경우는 걸러내지 않았던 것. Yahoo Finance가 한국 장 시간대 등
특정 타이밍에 "행은 있는데 Close가 NaN인" 불완전한 데이터를 줄 때가
있는데, 이게 그대로 round(float(nan), 2) -> nan 으로 계산되고,
json.dump()가 기본적으로 NaN을 허용하다 보니 indices_live.json 파일에
파이썬 리터럴 NaN이 그대로 찍혀서 저장됐음. 자바스크립트의 JSON.parse는
NaN을 유효한 토큰으로 인식하지 못하므로, 프론트엔드가 파일 전체를
파싱하는 데 실패해서 화면이 아예 갱신되지 않는 문제로 이어졌음.
-> (1) today_close/prev_close가 NaN인지 math.isnan()으로 반드시 검증하고,
       NaN이면 그 시도는 실패로 간주해 재시도.
   (2) 재시도를 다 써도 실패하면 그 지수는 아예 결과 딕셔너리에 넣지
       않음(이전 값을 프론트엔드가 그대로 유지하도록). None/NaN을
       절대 파일에 쓰지 않음.
   (3) json.dump(..., allow_nan=False)로 안전장치를 걸어서, 혹시라도
       NaN이 다시 섞여 들어오면 파일 저장 자체가 명확한 에러로 실패하게
       만듦 (조용히 깨진 JSON이 배포되는 것을 방지).
"""
import json
import math
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


def _is_valid_number(x) -> bool:
    """NaN/None/inf가 아닌 실제로 쓸 수 있는 숫자인지 확인"""
    try:
        return x is not None and not math.isnan(x) and not math.isinf(x)
    except TypeError:
        return False


def fetch_price_only(ticker: str, retries: int = 3):
    """현재가/전일대비/등락률만 가볍게 조회 (모멘텀 계산 없음, 5일치면 충분).
    Close 값 자체가 NaN인 '불완전한 데이터'도 실패로 간주하고 재시도함."""
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

            # 행은 있어도 그 안의 값 자체가 NaN인 경우(장 시간대/데이터 지연 등)를 반드시 걸러냄
            if not _is_valid_number(today_close) or not _is_valid_number(prev_close):
                print(f"  [경고] {ticker} 시도 {attempt}/{retries}: 종가가 NaN/유효하지 않음 "
                      f"(today={today_close}, prev={prev_close})")
                time.sleep(attempt * 2)
                continue

            change = today_close - prev_close
            pct_change = (change / prev_close) * 100

            result = {
                "price": round(float(today_close), 2),
                "change": round(float(change), 2),
                "pct_change": round(float(pct_change), 2),
            }

            # 마지막 안전장치: 계산 과정에서 혹시라도 NaN/inf가 섞였으면 실패 처리
            if not all(_is_valid_number(v) for v in result.values()):
                print(f"  [경고] {ticker} 시도 {attempt}/{retries}: 계산 결과에 NaN/inf 포함")
                time.sleep(attempt * 2)
                continue

            return result

        except Exception as e:
            print(f"  [오류] {ticker} 시도 {attempt}/{retries}: {e}")
            time.sleep(attempt * 2)

    print(f"  [실패] {ticker}: {retries}번 재시도했지만 유효한 가격을 가져오지 못했습니다. "
          f"(이 지수는 이번 파일에서 제외 -> 프론트엔드가 이전 값을 유지)")
    return None


def main():
    print("[지수 실시간 갱신] 실행 시각:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "indices": {},
    }

    failed = []
    for name, ticker in INDICES.items():
        info = fetch_price_only(ticker)
        if info:
            result["indices"][name] = info
            print(f"  {name}: {info['price']} ({info['pct_change']}%)")
        else:
            failed.append(name)

    if failed:
        print(f"\n[요약] 이번 실행에서 실패한 지수 ({len(failed)}개): {', '.join(failed)}")
        print("       -> 이 지수들은 파일에 아예 포함하지 않음. 프론트엔드는 data.json/이전 값 유지 필요.")

    # allow_nan=False: 혹시라도 NaN/inf가 남아있으면 여기서 명확한 예외를 던지게 해서
    # "조용히 깨진 JSON"이 그대로 배포되는 사고를 막는 마지막 안전장치.
    with open("indices_live.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)

    print("저장 완료: indices_live.json")


if __name__ == "__main__":
    main()
