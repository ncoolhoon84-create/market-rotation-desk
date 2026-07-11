"""
DART Open API가 제공하는 전체 기업 코드 목록(zip)을 받아서,
종목코드(005930 등) -> DART 고유번호(corp_code) 매핑만 뽑아
corp_code_map.json으로 저장하는 스크립트.

실행 방법: GitHub Actions에서 한 번만 수동 실행 (workflow_dispatch)
환경변수: DART_API_KEY (GitHub Secrets에 등록 필요 — Cloudflare Secret과는 별개로
          이 저장소의 Settings > Secrets and variables > Actions 에도 등록해야 함)
"""
import io
import json
import os
import zipfile
import xml.etree.ElementTree as ET

import requests

DART_API_KEY = os.environ["DART_API_KEY"]
CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"


def main():
    res = requests.get(CORP_CODE_URL, params={"crtfc_key": DART_API_KEY}, timeout=30)
    res.raise_for_status()

    # 응답은 zip 파일(바이너리) 안에 CORPCODE.xml 하나가 들어있는 구조
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")

    root = ET.fromstring(xml_bytes)

    mapping = {}
    for item in root.findall("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        corp_name = (item.findtext("corp_name") or "").strip()

        # 상장사만 필요함 (비상장사는 stock_code가 빈 문자열)
        if stock_code:
            mapping[stock_code] = {"corp_code": corp_code, "corp_name": corp_name}

    with open("corp_code_map.json", "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

    print(f"완료: 상장사 {len(mapping)}개 매핑 저장함 (corp_code_map.json)")


if __name__ == "__main__":
    main()
