# -*- coding: utf-8 -*-
"""
기업마당(bizinfo.go.kr) 지원사업 공고 -> 노션 BizinfoData DB 자동 수집
1단계: 기업마당 공식 API 연동 (매일 실행, 신규 공고만 저장)

필요한 환경변수 3개:
  NOTION_API_KEY      : 노션 통합(Integration) 시크릿 키 (ntn_... 또는 secret_...)
  NOTION_DATABASE_ID  : 09ad73bb5325838993ab01b32bc76f9e (BizinfoData DB)
  BIZINFO_API_KEY     : 기업마당 API 인증키 (crtfcKey)
"""

import os
import re
import sys
import time
import requests
from datetime import datetime, timedelta

# ------------------------------------------------------------------
# 설정
# ------------------------------------------------------------------
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "").strip()
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
BIZINFO_API_KEY = os.environ.get("BIZINFO_API_KEY", "").strip()

DAYS_BACK = int(os.environ.get("DAYS_BACK", "3"))   # 최근 며칠치 공고를 볼지
FETCH_COUNT = int(os.environ.get("FETCH_COUNT", "300"))  # API에서 가져올 최대 건수

BIZINFO_URL = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"
BIZINFO_BASE = "https://www.bizinfo.go.kr"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

REGIONS = [
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
]

FIELD_NAMES = ["금융", "기술", "인력", "수출", "내수", "창업", "경영", "기타"]


# ------------------------------------------------------------------
# 1. 기업마당 API 호출
# ------------------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}

MAX_RETRY = 3          # 접속 실패 시 재시도 횟수
RETRY_WAIT = 20        # 재시도 간 대기(초)


def fetch_bizinfo():
    params = {
        "crtfcKey": BIZINFO_API_KEY,
        "dataType": "json",
        "searchCnt": str(FETCH_COUNT),
    }
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = requests.get(BIZINFO_URL, params=params,
                                headers=BROWSER_HEADERS, timeout=60)
            resp.raise_for_status()
            break
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_err = e
            print(f"[재시도 {attempt}/{MAX_RETRY}] 기업마당 접속 지연... "
                  f"{RETRY_WAIT}초 후 다시 시도합니다.")
            time.sleep(RETRY_WAIT)
    else:
        print("[오류] 기업마당 서버에 연결하지 못했습니다. "
              "해외(GitHub) 접속이 일시 차단된 것으로 보이며, "
              "잠시 후 다시 실행하면 대부분 해결됩니다.")
        raise last_err

    data = resp.json()

    # 응답 형태가 {"jsonArray":[...]} / [...] / {"item":[...]} 등으로
    # 올 수 있어 모두 대응
    if isinstance(data, dict):
        items = (
            data.get("jsonArray")
            or data.get("item")
            or data.get("items")
            or data.get("data")
            or []
        )
    elif isinstance(data, list):
        items = data
    else:
        items = []

    if not items:
        print("[경고] API 응답에서 공고 목록을 찾지 못했습니다. 원본 응답 키:",
              list(data.keys()) if isinstance(data, dict) else type(data))
    return items


# ------------------------------------------------------------------
# 2. 항목 정규화 (기업마당 필드 -> 노션 필드)
# ------------------------------------------------------------------
def pick(item, *keys):
    """여러 후보 키 중 값이 있는 첫 번째를 반환"""
    for k in keys:
        v = item.get(k)
        if v not in (None, "", "null"):
            return str(v).strip()
    return ""


def parse_date(text):
    """'2026-07-06 17:20:53', '20260706' 등 -> '2026-07-06'"""
    if not text:
        return ""
    m = re.search(r"(\d{4})[-./]?(\d{2})[-./]?(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def parse_deadline(text):
    """'20260101 ~ 20260731' -> '2026-07-31' (실패 시 원문 유지)"""
    if not text:
        return ""
    parts = re.split(r"[~∼]", text)
    end = parse_date(parts[-1]) if parts else ""
    return end if end else text.strip()


def detect_region(*texts):
    """기관명/해시태그/제목에서 광역 지자체명을 찾고 없으면 '전국'"""
    joined = " ".join(t for t in texts if t)
    for r in REGIONS:
        if r in joined:
            return r
    return "전국"


def normalize(item):
    raw_id = pick(item, "pblancId", "seq", "id")
    title = pick(item, "pblancNm", "title")
    if not raw_id or not title:
        return None

    url = pick(item, "pblancUrl", "link", "url")
    if url.startswith("/"):
        url = BIZINFO_BASE + url

    org = pick(item, "jrsdInsttNm", "excInsttNm", "creatorNm")
    reg_date = parse_date(pick(item, "creatPnttm", "regDt", "pubDate"))
    deadline = parse_deadline(pick(item, "reqstBeginEndDe", "reqstDe"))
    hashtags = pick(item, "hashtags", "hashTags")

    field_raw = pick(item, "pldirSportRealmLclasCodeNm",
                     "pldirSportRealmMlsfcCodeNm", "category")
    fields = [f for f in FIELD_NAMES if f in field_raw] or ["기타"]

    return {
        "id": f"BIZ-{raw_id}",
        "title": title,
        "org": org,
        "url": url,
        "reg_date": reg_date,
        "deadline": deadline,
        "region": detect_region(hashtags, org, title),
        "fields": fields,
    }


def is_recent(notice):
    """최근 DAYS_BACK일 이내 등록 공고만 (등록일 파싱 실패 시 포함)"""
    if not notice["reg_date"]:
        return True
    try:
        d = datetime.strptime(notice["reg_date"], "%Y-%m-%d")
        return d >= datetime.now() - timedelta(days=DAYS_BACK)
    except ValueError:
        return True


# ------------------------------------------------------------------
# 노션 API 호출 공통 함수 (일시 오류 시 재시도)
# ------------------------------------------------------------------
def notion_post(url, payload):
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = requests.post(url, headers=NOTION_HEADERS,
                                 json=payload, timeout=60)
            if resp.status_code in (429, 500, 502, 503):
                raise requests.exceptions.ConnectionError(
                    f"노션 일시 오류 {resp.status_code}")
            return resp
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_err = e
            print(f"[재시도 {attempt}/{MAX_RETRY}] 노션 접속 지연... "
                  f"{RETRY_WAIT}초 후 다시 시도합니다.")
            time.sleep(RETRY_WAIT)
    raise last_err


# ------------------------------------------------------------------
# 3. 노션: 기존 공고ID 조회 (중복 방지)
# ------------------------------------------------------------------
def get_existing_ids():
    ids = set()
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    payload = {"page_size": 100}
    while True:
        resp = notion_post(url, payload)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            prop = page.get("properties", {}).get("공고ID", {})
            for rt in prop.get("rich_text", []):
                ids.add(rt.get("plain_text", "").strip())
        if data.get("has_more"):
            payload["start_cursor"] = data["next_cursor"]
        else:
            break
    return ids


# ------------------------------------------------------------------
# 4. 노션: 신규 공고 페이지 생성
# ------------------------------------------------------------------
def create_page(n):
    props = {
        "제목": {"title": [{"text": {"content": n["title"][:200]}}]},
        "공고ID": {"rich_text": [{"text": {"content": n["id"]}}]},
        "지역": {"select": {"name": n["region"]}},
        "지원분야": {"multi_select": [{"name": f} for f in n["fields"]]},
        "출처": {"multi_select": [{"name": "기업마당"}]},
    }
    if n["org"]:
        props["공고기관"] = {"rich_text": [{"text": {"content": n["org"][:200]}}]}
    if n["deadline"]:
        props["접수마감일"] = {"rich_text": [{"text": {"content": n["deadline"][:100]}}]}
    if n["url"]:
        props["공고URL"] = {"url": n["url"]}
    if n["reg_date"]:
        props["등록일"] = {"date": {"start": n["reg_date"]}}

    resp = notion_post(
        "https://api.notion.com/v1/pages",
        {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props},
    )
    if resp.status_code != 200:
        print(f"  [실패] {n['title'][:30]} -> {resp.status_code}: {resp.text[:200]}")
        return False
    return True


# ------------------------------------------------------------------
# 메인
# ------------------------------------------------------------------
def main():
    missing = [k for k, v in {
        "NOTION_API_KEY": NOTION_API_KEY,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
        "BIZINFO_API_KEY": BIZINFO_API_KEY,
    }.items() if not v]
    if missing:
        print(f"[오류] 환경변수가 없습니다: {', '.join(missing)}")
        sys.exit(1)

    print(f"=== 기업마당 공고 수집 시작 ({datetime.now():%Y-%m-%d %H:%M}) ===")

    items = fetch_bizinfo()
    print(f"1) API 수신: {len(items)}건")

    notices = [n for n in (normalize(i) for i in items) if n]
    recent = [n for n in notices if is_recent(n)]
    print(f"2) 최근 {DAYS_BACK}일 이내 공고: {len(recent)}건")

    existing = get_existing_ids()
    print(f"3) 노션 기존 저장분: {len(existing)}건")

    new_notices = [n for n in recent if n["id"] not in existing]
    print(f"4) 신규 공고: {len(new_notices)}건")

    saved = 0
    for n in new_notices:
        if create_page(n):
            saved += 1
            print(f"  [저장] {n['reg_date']} | {n['title'][:40]}")
        time.sleep(0.4)  # 노션 API 속도 제한(초당 3회) 준수

    print(f"=== 완료: 신규 {saved}건 저장 ===")


if __name__ == "__main__":
    main()
