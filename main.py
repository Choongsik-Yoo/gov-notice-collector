# -*- coding: utf-8 -*-
"""
지원사업 공고 자동 수집 -> 노션 BizinfoData DB
  1단계: 기업마당 공식 API
  2단계: NTIS 국가R&D통합공고 RSS

환경변수:
  NOTION_API_KEY     : 노션 통합 시크릿 키
  NOTION_DATABASE_ID : BizinfoData DB ID
  BIZINFO_API_KEY    : 기업마당 API 인증키 (crtfcKey)
"""

import os, re, sys, time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ------------------------------------------------------------------
# 설정
# ------------------------------------------------------------------
NOTION_API_KEY     = os.environ.get("NOTION_API_KEY", "").strip()
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
BIZINFO_API_KEY    = os.environ.get("BIZINFO_API_KEY", "").strip()

DAYS_BACK   = int(os.environ.get("DAYS_BACK",   "3"))
FETCH_COUNT = int(os.environ.get("FETCH_COUNT", "300"))

BIZINFO_URL  = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"
BIZINFO_BASE = "https://www.bizinfo.go.kr"

# NTIS 국가R&D통합공고 RSS (공식 제공, 인증키 불필요)
NTIS_RSS_URL = "https://www.ntis.go.kr/rndgate/eg/un/ra/rss.do"
NTIS_BASE    = "https://www.ntis.go.kr"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

REGIONS = [
    "서울","부산","대구","인천","광주","대전","울산","세종",
    "경기","강원","충북","충남","전북","전남","경북","경남","제주",
]
FIELD_NAMES = ["금융","기술","인력","수출","내수","창업","경영","기타"]

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}
RSS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

MAX_RETRY  = 3
RETRY_WAIT = 20


# ------------------------------------------------------------------
# 공통 유틸
# ------------------------------------------------------------------
def pick(item, *keys):
    for k in keys:
        v = item.get(k)
        if v not in (None, "", "null"):
            return str(v).strip()
    return ""

def parse_date(text):
    if not text:
        return ""
    m = re.search(r"(\d{4})[-./년\s]?(\d{2})[-./월\s]?(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""

def parse_deadline(text):
    if not text:
        return ""
    parts = re.split(r"[~∼\-–]", text)
    end = parse_date(parts[-1]) if parts else ""
    return end if end else text.strip()

def detect_region(*texts):
    joined = " ".join(t for t in texts if t)
    for r in REGIONS:
        if r in joined:
            return r
    return "전국"

def is_recent(notice):
    if not notice["reg_date"]:
        return True
    try:
        d = datetime.strptime(notice["reg_date"], "%Y-%m-%d")
        return d >= datetime.now() - timedelta(days=DAYS_BACK)
    except ValueError:
        return True


# ------------------------------------------------------------------
# 노션 API 공통 (재시도 포함)
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
            print(f"  [재시도 {attempt}/{MAX_RETRY}] 노션 접속 지연 {RETRY_WAIT}초 대기...")
            time.sleep(RETRY_WAIT)
    raise last_err

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

def create_page(n):
    props = {
        "제목":    {"title":       [{"text": {"content": n["title"][:200]}}]},
        "공고ID":  {"rich_text":   [{"text": {"content": n["id"]}}]},
        "지역":    {"select":      {"name": n["region"]}},
        "지원분야":{"multi_select":[{"name": f} for f in n["fields"]]},
        "출처":    {"multi_select":[{"name": n["source"]}]},
    }
    if n.get("org"):
        props["공고기관"] = {"rich_text": [{"text": {"content": n["org"][:200]}}]}
    if n.get("deadline"):
        props["접수마감일"] = {"rich_text": [{"text": {"content": n["deadline"][:100]}}]}
    if n.get("url"):
        props["공고URL"] = {"url": n["url"]}
    if n.get("reg_date"):
        props["등록일"] = {"date": {"start": n["reg_date"]}}

    resp = notion_post(
        "https://api.notion.com/v1/pages",
        {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props},
    )
    if resp.status_code != 200:
        print(f"    [실패] {n['title'][:30]} -> {resp.status_code}: {resp.text[:150]}")
        return False
    return True


# ==================================================================
# 1단계: 기업마당 API
# ==================================================================
def fetch_bizinfo():
    params = {
        "crtfcKey":  BIZINFO_API_KEY,
        "dataType":  "json",
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
            print(f"  [재시도 {attempt}/{MAX_RETRY}] 기업마당 접속 지연 {RETRY_WAIT}초 대기...")
            time.sleep(RETRY_WAIT)
    else:
        print("  [경고] 기업마당 서버 연결 실패 — 이번 회차 건너뜁니다.")
        return []

    data = resp.json()
    if isinstance(data, dict):
        items = (data.get("jsonArray") or data.get("item")
                 or data.get("items") or data.get("data") or [])
    elif isinstance(data, list):
        items = data
    else:
        items = []

    if not items:
        print("  [경고] 기업마당 응답에 공고 없음. 키:",
              list(data.keys()) if isinstance(data, dict) else type(data))
    return items

def normalize_bizinfo(item):
    raw_id = pick(item, "pblancId", "seq", "id")
    title  = pick(item, "pblancNm", "title")
    if not raw_id or not title:
        return None
    url = pick(item, "pblancUrl", "link", "url")
    if url.startswith("/"):
        url = BIZINFO_BASE + url
    org      = pick(item, "jrsdInsttNm", "excInsttNm", "creatorNm")
    reg_date = parse_date(pick(item, "creatPnttm", "regDt", "pubDate"))
    deadline = parse_deadline(pick(item, "reqstBeginEndDe", "reqstDe"))
    hashtags = pick(item, "hashtags", "hashTags")
    field_raw = pick(item, "pldirSportRealmLclasCodeNm",
                     "pldirSportRealmMlsfcCodeNm", "category")
    fields = [f for f in FIELD_NAMES if f in field_raw] or ["기타"]
    return {
        "id": f"BIZ-{raw_id}", "title": title, "org": org,
        "url": url, "reg_date": reg_date, "deadline": deadline,
        "region": detect_region(hashtags, org, title),
        "fields": fields, "source": "기업마당",
    }


# ==================================================================
# 2단계: NTIS 국가R&D통합공고 RSS
# ==================================================================
def fetch_ntis():
    """NTIS RSS 피드를 파싱해 공고 목록 반환"""
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = requests.get(NTIS_RSS_URL, headers=RSS_HEADERS, timeout=60)
            resp.raise_for_status()
            break
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_err = e
            print(f"  [재시도 {attempt}/{MAX_RETRY}] NTIS 접속 지연 {RETRY_WAIT}초 대기...")
            time.sleep(RETRY_WAIT)
    else:
        print("  [경고] NTIS 서버 연결 실패 — 이번 회차 건너뜁니다.")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"  [경고] NTIS RSS XML 파싱 실패: {e}")
        return []

    # RSS 네임스페이스 처리
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    items = root.findall(".//item")
    if not items:
        print("  [경고] NTIS RSS에 item이 없습니다.")
    return items

def normalize_ntis(item):
    """RSS <item> -> 통일된 공고 딕셔너리"""
    def tag(name):
        el = item.find(name)
        return el.text.strip() if el is not None and el.text else ""

    def dc_tag(name):
        el = item.find(f"{{http://purl.org/dc/elements/1.1/}}{name}")
        return el.text.strip() if el is not None and el.text else ""

    title = tag("title")
    link  = tag("link")
    guid  = tag("guid") or link   # 고유 식별자

    if not title or not guid:
        return None

    # NTIS guid 예: https://www.ntis.go.kr/rndgate/eg/un/ra/view.do?roRndUid=1253946
    raw_id = re.search(r"roRndUid=(\d+)", guid)
    notice_id = f"NTIS-{raw_id.group(1)}" if raw_id else f"NTIS-{abs(hash(guid))}"

    pub_date = tag("pubDate") or dc_tag("date")
    reg_date = parse_date(pub_date)

    # 마감일: <description>에 포함된 경우가 많음
    desc = tag("description")
    deadline_match = re.search(
        r"(?:마감|접수마감|신청마감|접수종료)[^\d]*(\d{4}[년.\-]\d{1,2}[월.\-]\d{1,2})", desc)
    deadline = parse_date(deadline_match.group(1)) if deadline_match else ""

    org = dc_tag("creator") or tag("author") or ""
    category = tag("category") or dc_tag("subject") or ""

    fields = [f for f in FIELD_NAMES if f in title + desc + category] or ["기술"]

    url = link if link.startswith("http") else (NTIS_BASE + link if link else "")

    return {
        "id": notice_id, "title": title, "org": org,
        "url": url, "reg_date": reg_date, "deadline": deadline,
        "region": detect_region(title, org, category),
        "fields": fields, "source": "NTIS",
    }


# ==================================================================
# 메인
# ==================================================================
def collect_source(name, raw_items, normalize_fn, existing):
    """수집 → 정규화 → 최근 필터 → 중복 제거 → 저장"""
    notices = [n for n in (normalize_fn(i) for i in raw_items) if n]
    recent  = [n for n in notices if is_recent(n)]
    new     = [n for n in recent  if n["id"] not in existing]

    print(f"  수신 {len(raw_items)}건 → 최근 {DAYS_BACK}일 {len(recent)}건 "
          f"→ 신규 {len(new)}건")

    saved = 0
    for n in new:
        if create_page(n):
            saved += 1
            existing.add(n["id"])   # 같은 실행 내 중복 방지
            print(f"    [저장] {n['reg_date']} | {n['title'][:45]}")
        time.sleep(0.4)
    print(f"  [{name}] 저장 완료: {saved}건")
    return saved

def main():
    missing = [k for k, v in {
        "NOTION_API_KEY":     NOTION_API_KEY,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
        "BIZINFO_API_KEY":    BIZINFO_API_KEY,
    }.items() if not v]
    if missing:
        print(f"[오류] 환경변수가 없습니다: {', '.join(missing)}")
        sys.exit(1)

    print(f"\n{'='*55}")
    print(f"  지원사업 공고 자동 수집  {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'='*55}")

    existing = get_existing_ids()
    print(f"노션 기존 저장분: {len(existing)}건\n")

    total = 0

    # --- 1단계: 기업마당 ---
    print("[1단계] 기업마당 수집 시작")
    biz_items = fetch_bizinfo()
    total += collect_source("기업마당", biz_items, normalize_bizinfo, existing)

    print()

    # --- 2단계: NTIS ---
    print("[2단계] NTIS 국가R&D통합공고 수집 시작")
    ntis_items = fetch_ntis()
    total += collect_source("NTIS", ntis_items, normalize_ntis, existing)

    print(f"\n{'='*55}")
    print(f"  전체 신규 저장: {total}건  완료")
    print(f"{'='*55}\n")

if __name__ == "__main__":
    main()
