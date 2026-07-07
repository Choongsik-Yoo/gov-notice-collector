# -*- coding: utf-8 -*-
"""
지원사업 공고 자동 수집 -> 노션 BizinfoData DB
  1단계: 기업마당 공식 API
  2단계: NTIS 국가R&D통합공고 (Ajax→HTML→RSS 3중 폴백)

환경변수:
  NOTION_API_KEY     : 노션 통합 시크릿 키
  NOTION_DATABASE_ID : BizinfoData DB ID
  BIZINFO_API_KEY    : 기업마당 API 인증키
"""

import os, re, sys, time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

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

NTIS_BASE      = "https://www.ntis.go.kr"
NTIS_AJAX_URL  = "https://www.ntis.go.kr/rndgate/eg/un/ra/selectUnRaListAjax.do"
NTIS_LIST_URL  = "https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do"
NTIS_RSS_URLS  = [
    "https://www.ntis.go.kr/rndgate/eg/un/ra/rss.do",
    "https://www.ntis.go.kr/ThMain.do?rss=Y",
]

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://www.ntis.go.kr/",
}

REGIONS = [
    "서울","부산","대구","인천","광주","대전","울산","세종",
    "경기","강원","충북","충남","전북","전남","경북","경남","제주",
]
FIELD_NAMES = ["금융","기술","인력","수출","내수","창업","경영","기타"]

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
    if not text: return ""
    m = re.search(r"(\d{4})[-./년\s]?(\d{2})[-./월\s]?(\d{2})", str(text))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""

def parse_deadline(text):
    if not text: return ""
    parts = re.split(r"[~∼]", text)
    end = parse_date(parts[-1]) if parts else ""
    return end if end else text.strip()

def detect_region(*texts):
    joined = " ".join(t for t in texts if t)
    for r in REGIONS:
        if r in joined: return r
    return "전국"

def detect_fields(*texts):
    joined = " ".join(t for t in texts if t)
    return [f for f in FIELD_NAMES if f in joined] or ["기타"]

def is_recent(notice):
    if not notice.get("reg_date"): return True
    try:
        d = datetime.strptime(notice["reg_date"], "%Y-%m-%d")
        return d >= datetime.now() - timedelta(days=DAYS_BACK)
    except ValueError:
        return True


# ------------------------------------------------------------------
# 노션 API (재시도 포함)
# ------------------------------------------------------------------
def notion_post(url, payload):
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = requests.post(url, headers=NOTION_HEADERS,
                                 json=payload, timeout=60)
            if resp.status_code in (429, 500, 502, 503):
                raise requests.exceptions.ConnectionError(f"노션 오류 {resp.status_code}")
            return resp
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_err = e
            print(f"  [재시도 {attempt}/{MAX_RETRY}] 노션 지연 {RETRY_WAIT}초 대기...")
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
        "제목":     {"title":       [{"text": {"content": n["title"][:200]}}]},
        "공고ID":   {"rich_text":   [{"text": {"content": n["id"]}}]},
        "지역":     {"select":      {"name": n["region"]}},
        "지원분야": {"multi_select":[{"name": f} for f in n["fields"]]},
        "출처":     {"multi_select":[{"name": n["source"]}]},
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
    params = {"crtfcKey": BIZINFO_API_KEY, "dataType": "json",
              "searchCnt": str(FETCH_COUNT)}
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
            print(f"  [재시도 {attempt}/{MAX_RETRY}] 기업마당 지연 {RETRY_WAIT}초 대기...")
            time.sleep(RETRY_WAIT)
    else:
        print("  [경고] 기업마당 연결 실패 — 이번 회차 건너뜁니다.")
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
    if not raw_id or not title: return None
    url = pick(item, "pblancUrl", "link", "url")
    if url.startswith("/"): url = BIZINFO_BASE + url
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
# 2단계: NTIS — Ajax→HTML→RSS 3중 폴백
# ==================================================================

# ---- Ajax JSON ----
def _ntis_ajax(max_pages=5):
    items = []
    sess = requests.Session()
    sess.headers.update({**BROWSER_HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })
    for page in range(1, max_pages + 1):
        try:
            resp = sess.post(NTIS_AJAX_URL, data={
                "pageIndex": str(page), "recordCountPerPage": "20",
                "searchGbnCd": "", "searchWord": "",
            }, timeout=30)
            if resp.status_code != 200: break
            data = resp.json()
            rows = (data.get("resultList") or data.get("list")
                    or data.get("items") or [])
            if not rows: break
            items.extend(rows)
        except Exception as e:
            print(f"  [NTIS-Ajax] 페이지 {page} 실패: {e}")
            break
    return items

def _norm_ajax(row):
    raw_id = str(row.get("roRndUid") or row.get("uid") or "")
    title  = str(row.get("roRndNm") or row.get("title") or "").strip()
    if not raw_id or not title: return None
    org      = str(row.get("mngtInsttNm") or "").strip()
    reg_date = parse_date(row.get("regDt") or "")
    deadline = parse_date(row.get("rceptEndDt") or "")
    url      = f"{NTIS_BASE}/rndgate/eg/un/ra/view.do?roRndUid={raw_id}"
    return {"id": f"NTIS-{raw_id}", "title": title, "org": org, "url": url,
            "reg_date": reg_date, "deadline": deadline,
            "region": detect_region(org, title),
            "fields": detect_fields(title), "source": "NTIS"}

# ---- HTML 파싱 ----
def _ntis_html(max_pages=3):
    items = []
    sess = requests.Session()
    sess.headers.update({**BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml"})
    for page in range(1, max_pages + 1):
        try:
            resp = sess.get(NTIS_LIST_URL, params={"pageIndex": str(page)}, timeout=30)
            if resp.status_code != 200: break
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = (soup.select("table.board-list tbody tr") or
                    soup.select("table tbody tr") or
                    soup.select(".list-item"))
            if not rows:
                print(f"  [NTIS-HTML] 행 없음 (페이지 {page}) — 구조 변경 가능성")
                break
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 2: continue
                items.append({"_row": row, "_cols": cols})
        except Exception as e:
            print(f"  [NTIS-HTML] 페이지 {page} 실패: {e}")
            break
    return items

def _norm_html(item):
    row, cols = item["_row"], item["_cols"]
    a = row.find("a")
    if not a: return None
    title = a.get_text(strip=True)
    href  = a.get("href", "") or str(a.get("onclick", ""))
    uid_m = re.search(r"roRndUid[=,'\"\s]+(\d+)", href + str(row))
    raw_id = uid_m.group(1) if uid_m else str(abs(hash(title)))
    url    = f"{NTIS_BASE}/rndgate/eg/un/ra/view.do?roRndUid={raw_id}"
    texts  = [c.get_text(strip=True) for c in cols]
    dates  = [parse_date(t) for t in texts if re.search(r"\d{4}", t)]
    orgs   = [t for t in texts if t and not re.search(r"\d{4}", t) and t != title]
    return {"id": f"NTIS-{raw_id}", "title": title,
            "org": orgs[0] if orgs else "",
            "url": url,
            "reg_date": dates[0] if dates else "",
            "deadline": dates[-1] if len(dates) > 1 else "",
            "region": detect_region(title),
            "fields": detect_fields(title), "source": "NTIS"}

# ---- RSS ----
def _ntis_rss():
    for rss_url in NTIS_RSS_URLS:
        try:
            resp = requests.get(rss_url, headers={**BROWSER_HEADERS,
                "Accept": "application/rss+xml,application/xml,text/xml,*/*"},
                timeout=30)
            if resp.status_code != 200: continue
            if "html" in resp.headers.get("Content-Type", ""):
                print(f"  [NTIS-RSS] {rss_url} -> HTML 응답(로그인 필요), 건너뜁니다.")
                continue
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")
            if items:
                print(f"  [NTIS-RSS] {rss_url} -> {len(items)}건 수신")
                return items
        except ET.ParseError as e:
            print(f"  [NTIS-RSS] {rss_url} XML 파싱 오류: {e}")
        except Exception as e:
            print(f"  [NTIS-RSS] {rss_url} 실패: {e}")
    return []

def _norm_rss(item):
    def tag(n):
        el = item.find(n)
        return el.text.strip() if el is not None and el.text else ""
    title = tag("title"); link = tag("link"); guid = tag("guid") or link
    if not title: return None
    uid_m  = re.search(r"roRndUid=(\d+)", guid + link)
    raw_id = uid_m.group(1) if uid_m else str(abs(hash(guid)))
    url    = link if link.startswith("http") else (
             f"{NTIS_BASE}/rndgate/eg/un/ra/view.do?roRndUid={raw_id}")
    reg_date = parse_date(tag("pubDate"))
    desc     = tag("description")
    dl_m     = re.search(r"(?:마감|종료)[^\d]*(\d{4}[.\-]\d{1,2}[.\-]\d{1,2})", desc)
    deadline = parse_date(dl_m.group(1)) if dl_m else ""
    return {"id": f"NTIS-{raw_id}", "title": title, "org": tag("author"),
            "url": url, "reg_date": reg_date, "deadline": deadline,
            "region": detect_region(title),
            "fields": detect_fields(title, desc), "source": "NTIS"}

# ---- NTIS 진입점 ----
def fetch_ntis():
    print("  [NTIS] 방법1: Ajax JSON 시도...")
    raw = _ntis_ajax()
    if raw:
        result = [n for n in (_norm_ajax(r) for r in raw) if n]
        print(f"  [NTIS] Ajax 성공: {len(raw)}건 수신")
        return result

    print("  [NTIS] 방법2: HTML 파싱 시도...")
    raw = _ntis_html()
    if raw:
        result = [n for n in (_norm_html(r) for r in raw) if n]
        print(f"  [NTIS] HTML 성공: {len(raw)}건 수신")
        return result

    print("  [NTIS] 방법3: RSS 피드 시도...")
    raw = _ntis_rss()
    if raw:
        return [n for n in (_norm_rss(r) for r in raw) if n]

    print("  [NTIS] 세 가지 방법 모두 실패 — 건너뜁니다.")
    return []


# ==================================================================
# 수집 공통 처리
# ==================================================================
def collect_source(name, notices, existing):
    recent = [n for n in notices if is_recent(n)]
    new    = [n for n in recent  if n["id"] not in existing]
    print(f"  정규화 {len(notices)}건 → 최근 {DAYS_BACK}일 {len(recent)}건 → 신규 {len(new)}건")
    saved = 0
    for n in new:
        if create_page(n):
            saved += 1
            existing.add(n["id"])
            print(f"    [저장] {n.get('reg_date','')} | {n['title'][:45]}")
        time.sleep(0.4)
    print(f"  [{name}] 저장 완료: {saved}건")
    return saved


# ==================================================================
# 메인
# ==================================================================
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

    print("[1단계] 기업마당")
    raw_biz = fetch_bizinfo()
    notices_biz = [n for n in (normalize_bizinfo(i) for i in raw_biz) if n]
    total += collect_source("기업마당", notices_biz, existing)

    print()
    print("[2단계] NTIS 국가R&D통합공고")
    notices_ntis = fetch_ntis()
    total += collect_source("NTIS", notices_ntis, existing)

    print(f"\n{'='*55}")
    print(f"  전체 신규 저장: {total}건  완료")
    print(f"{'='*55}\n")

if __name__ == "__main__":
    main()
