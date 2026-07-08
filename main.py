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
    first_logged = False
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
            if not first_logged and rows:
                print("  [NTIS 필드 진단] 첫 공고 전체 키:")
                for k, v in rows[0].items():
                    print(f"    {k}: {repr(v)}")
                first_logged = True
            items.extend(rows)
        except Exception as e:
            print(f"  [NTIS-Ajax] 페이지 {page} 실패: {e}")
            break
    return items

def _norm_ajax(row):
    raw_id = str(row.get("roRndUid") or row.get("uid") or "")
    title  = str(row.get("roRndNm") or row.get("title") or "").strip()
    if not raw_id or not title: return None
    org = str(row.get("mngtInsttNm") or row.get("jrsdInsttNm") or "").strip()

    # 등록일 후보 키 전부 시도
    reg_date = parse_date(
        row.get("regDt") or row.get("registDt") or row.get("crtDt") or
        row.get("pblancDt") or row.get("roRegistDt") or row.get("inptDt") or ""
    )
    # 자동 탐색: 아직 못 찾았으면 키명에 reg/crt 포함된 것 순회
    if not reg_date:
        for k, v in row.items():
            if any(x in k.lower() for x in ["reg", "regist", "crt", "inpt", "pblanc"]):
                reg_date = parse_date(str(v))
                if reg_date: break

    # 마감일 후보 키 전부 시도
    deadline = parse_date(
        row.get("rceptEndDt") or row.get("rcptEndDt") or row.get("endDt") or
        row.get("applyEndDt") or row.get("roRceptEndDt") or
        row.get("reqstEndDe") or row.get("closeDate") or ""
    )
    if not deadline:
        for k, v in row.items():
            if any(x in k.lower() for x in ["end", "close", "rcpt", "rcep"]):
                deadline = parse_date(str(v))
                if deadline: break

    url = f"{NTIS_BASE}/rndgate/eg/un/ra/view.do?roRndUid={raw_id}"
    return {"id": f"NTIS-{raw_id}", "title": title, "org": org, "url": url,
            "reg_date": reg_date, "deadline": deadline,
            "region": detect_region(org, title),
            "fields": detect_fields(title), "source": "NTIS"}

# ---- HTML 파싱 ----
def _ntis_html(max_pages=3):
    items = []
    col_idx = {}
    sess = requests.Session()
    sess.headers.update({**BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml"})
    for page in range(1, max_pages + 1):
        try:
            resp = sess.get(NTIS_LIST_URL, params={"pageIndex": str(page)}, timeout=30)
            if resp.status_code != 200: break
            soup = BeautifulSoup(resp.text, "html.parser")
            # 헤더 분석 (최초 1회)
            if not col_idx:
                headers = soup.select("table thead th, table th")
                for i, th in enumerate(headers):
                    txt = th.get_text(strip=True)
                    if any(k in txt for k in ["공고명","사업명","과제명","제목"]):
                        col_idx["title"] = i
                    elif any(k in txt for k in ["등록일","공고일","게시일"]):
                        col_idx["reg"] = i
                    elif any(k in txt for k in ["마감일","종료일","접수마감","신청마감"]):
                        col_idx["end"] = i
                    elif any(k in txt for k in ["기관","부처","주관"]):
                        col_idx["org"] = i
                if col_idx:
                    print(f"  [NTIS-HTML] 컬럼 매핑: {col_idx}")
                else:
                    print(f"  [NTIS-HTML] 헤더 자동 감지 실패 — 위치 기반 파싱 시도")
                    # 진단: 헤더 텍스트 출력
                    print(f"  [NTIS-HTML] 감지된 헤더: {[th.get_text(strip=True) for th in headers]}")
            rows = (soup.select("table.board-list tbody tr") or
                    soup.select("table tbody tr") or
                    soup.select(".list-item"))
            if not rows:
                print(f"  [NTIS-HTML] 행 없음 (페이지 {page})")
                print(f"  [NTIS-HTML] HTML 앞 800자: {resp.text[:800]}")
                break
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 2: continue
                # roRndUid 링크가 있는 실제 공고 행만 수집
                row_str = str(row)
                if "roRndUid" not in row_str:
                    continue
                # roRndUid 번호 추출 가능한지 확인
                uid_check = re.search(r"roRndUid[=,]+(\d+)", row_str)
                if not uid_check:
                    continue
                items.append({"_row": row, "_cols": cols, "_col_idx": dict(col_idx)})
        except Exception as e:
            print(f"  [NTIS-HTML] 페이지 {page} 실패: {e}")
            break
    return items

def _norm_html(item):
    row, cols = item["_row"], item["_cols"]
    col_idx = item.get("_col_idx", {})
    texts = [c.get_text(strip=True) for c in cols]

    # 제목 추출: 헤더 인덱스 → 링크 태그 순
    title = ""
    raw_id = ""
    if "title" in col_idx and col_idx["title"] < len(cols):
        a = cols[col_idx["title"]].find("a")
        if a:
            title = a.get_text(strip=True)
            href = a.get("href", "") + str(a.get("onclick", ""))
            m = re.search(r"roRndUid[=,]+(\d+)", href + str(row))
            if m: raw_id = m.group(1)
    if not title:
        for td in cols:
            a = td.find("a")
            if a and len(a.get_text(strip=True)) > 5:
                title = a.get_text(strip=True)
                href = a.get("href", "") + str(a.get("onclick", ""))
                m = re.search(r"roRndUid[=,]+(\d+)", href + str(row))
                if m: raw_id = m.group(1)
                break
    if not title: return None
    if not raw_id:
        m = re.search(r"roRndUid[=,]+(\d+)", str(row))
        raw_id = m.group(1) if m else str(abs(hash(title)))
    url = f"{NTIS_BASE}/rndgate/eg/un/ra/view.do?roRndUid={raw_id}"

    # 기관명
    STATUS_WORDS = {"접수중","마감","접수예정","접수마감","공고중","종료","준비중","전체"}
    org = ""
    if "org" in col_idx and col_idx["org"] < len(texts):
        org = texts[col_idx["org"]]
    if not org or org in STATUS_WORDS:
        candidates = [t for t in texts
                      if t and t != title
                      and t not in STATUS_WORDS
                      and not re.search(r"\d{4}", t)
                      and not re.search(r"^(접수|마감|공고|종료|전체)", t)
                      and 2 < len(t) < 30]
        if candidates: org = candidates[0]

    # 날짜: 헤더 인덱스 → 텍스트 전체 스캔
    all_dates = [parse_date(t) for t in texts if parse_date(t)]
    reg_date = ""
    deadline = ""
    if "reg" in col_idx and col_idx["reg"] < len(texts):
        reg_date = parse_date(texts[col_idx["reg"]])
    if "end" in col_idx and col_idx["end"] < len(texts):
        deadline = parse_date(texts[col_idx["end"]])
    if not reg_date and all_dates:
        reg_date = all_dates[0]
    if not deadline and len(all_dates) > 1:
        deadline = all_dates[-1]

    return {"id": f"NTIS-{raw_id}", "title": title, "org": org, "url": url,
            "reg_date": reg_date, "deadline": deadline,
            "region": detect_region(org, title),
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
# 3단계: SMTECH (중소기업기술정보진흥원)
# ==================================================================
SMTECH_BASE     = "https://www.smtech.go.kr"
SMTECH_LIST_URL = "https://www.smtech.go.kr/front/ifg/no/notice1List.do"
SMTECH_AJAX_URL = "https://www.smtech.go.kr/front/ifg/no/notice1ListAjax.do"

def fetch_smtech():
    """SMTECH 사업공고 목록 Ajax or HTML 파싱"""
    sess = requests.Session()
    sess.headers.update({**BROWSER_HEADERS,
        "Referer": SMTECH_LIST_URL,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })
    items = []
    # 방법1: Ajax JSON
    try:
        for page in range(1, 6):
            resp = sess.post(SMTECH_AJAX_URL, data={
                "pageIndex": str(page), "pageUnit": "20",
                "searchCondition": "", "searchKeyword": "",
                "bCd": "", "sFld": "", "sStr": "",
            }, timeout=30)
            if resp.status_code != 200: break
            data = resp.json()
            rows = (data.get("resultList") or data.get("list")
                    or data.get("items") or data.get("data") or [])
            if not rows: break
            if page == 1:
                print(f"  [SMTECH-Ajax] 첫 공고 키: {list(rows[0].keys())}")
            items.extend(rows)
        if items:
            print(f"  [SMTECH] Ajax 성공: {len(items)}건")
            return ("ajax", items)
    except Exception as e:
        print(f"  [SMTECH-Ajax] 실패: {e}")

    # 방법2: HTML 파싱
    print("  [SMTECH] HTML 파싱 시도...")
    sess.headers.update({"Accept": "text/html,application/xhtml+xml"})
    html_items = []
    try:
        for page in range(1, 6):
            resp = sess.get(SMTECH_LIST_URL,
                params={"pageIndex": str(page)}, timeout=30)
            if resp.status_code != 200: break
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = (soup.select("table.board_list tbody tr") or
                    soup.select("table.tbl_list tbody tr") or
                    soup.select("table tbody tr"))
            if not rows: break
            valid = [r for r in rows if r.find("a") and len(r.find_all("td")) >= 2]
            if not valid: break
            html_items.extend(valid)
        if html_items:
            print(f"  [SMTECH] HTML 성공: {len(html_items)}건")
            return ("html", html_items)
    except Exception as e:
        print(f"  [SMTECH-HTML] 실패: {e}")

    print("  [SMTECH] 수집 실패 — 건너뜁니다.")
    return ("none", [])

def normalize_smtech(mode, item):
    if mode == "ajax":
        raw_id = str(item.get("bIdx") or item.get("noticeId") or item.get("seq") or item.get("id") or "")
        title  = str(item.get("title") or item.get("noticeName") or item.get("subject") or "").strip()
        if not raw_id or not title: return None
        org      = str(item.get("instNm") or item.get("org") or "중소기업기술정보진흥원").strip()
        reg_date = parse_date(item.get("registDate") or item.get("regDt") or item.get("crtDt") or "")
        if not reg_date:
            for k, v in item.items():
                if any(x in k.lower() for x in ["reg","regist","crt","date","dt"]):
                    reg_date = parse_date(str(v))
                    if reg_date: break
        deadline = parse_date(item.get("endDate") or item.get("applyEndDt") or item.get("rceptEndDt") or "")
        if not deadline:
            for k, v in item.items():
                if any(x in k.lower() for x in ["end","close","rcpt","rcep"]):
                    deadline = parse_date(str(v))
                    if deadline: break
        # URL 구성
        url_key = item.get("bIdx") or raw_id
        url = f"{SMTECH_BASE}/front/ifg/no/notice1View.do?bIdx={url_key}"
        return {"id": f"SMT-{raw_id}", "title": title, "org": org,
                "url": url, "reg_date": reg_date, "deadline": deadline,
                "region": detect_region(org, title),
                "fields": detect_fields(title), "source": "SMTECH"}
    else:  # html
        row = item
        cols = row.find_all("td")
        a = None
        for td in cols:
            candidate = td.find("a")
            if candidate and len(candidate.get_text(strip=True)) > 5:
                a = candidate
                break
        if not a: return None
        title = a.get_text(strip=True)
        href  = a.get("href", "") or str(a.get("onclick", ""))
        # bIdx 추출
        bid_m = re.search(r"bIdx[=,]+(\d+)", href + str(row))
        raw_id = bid_m.group(1) if bid_m else str(abs(hash(title)))
        url = f"{SMTECH_BASE}/front/ifg/no/notice1View.do?bIdx={raw_id}"
        texts = [c.get_text(strip=True) for c in cols]
        STATUS = {"접수중","마감","접수예정","접수마감","종료","전체"}
        all_dates = [parse_date(t) for t in texts if parse_date(t)]
        org_cands = [t for t in texts if t and t != title and t not in STATUS
                     and not re.search(r"\d{4}", t) and 2 < len(t) < 30]
        return {"id": f"SMT-{raw_id}", "title": title,
                "org": org_cands[0] if org_cands else "중소기업기술정보진흥원",
                "url": url,
                "reg_date": all_dates[0] if all_dates else "",
                "deadline": all_dates[-1] if len(all_dates) > 1 else "",
                "region": detect_region(title),
                "fields": detect_fields(title), "source": "SMTECH"}

def collect_smtech():
    mode, raw = fetch_smtech()
    if mode == "none": return []
    return [n for n in (normalize_smtech(mode, r) for r in raw) if n]


# ==================================================================
# 3단계: 소진공 (소상공인시장진흥공단)
# ==================================================================
SEMAS_BASE     = "https://www.semas.or.kr"
SEMAS_LIST_URL = "https://www.semas.or.kr/web/board/webBoardList.kmdc"
SEMAS_BOARD_CD = "240"   # 사업공고 게시판 코드

def fetch_semas():
    """소진공 사업공고 목록 HTML 파싱"""
    sess = requests.Session()
    sess.headers.update({**BROWSER_HEADERS,
        "Referer": SEMAS_BASE,
        "Accept": "text/html,application/xhtml+xml",
    })
    items = []
    try:
        for page in range(1, 6):
            resp = sess.get(SEMAS_LIST_URL, params={
                "bCd": SEMAS_BOARD_CD,
                "pNm": "BOA0103",
                "page": str(page),
            }, timeout=30)
            if resp.status_code != 200: break
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = (soup.select("table.board_list tbody tr") or
                    soup.select("table.tbl_list tbody tr") or
                    soup.select("table tbody tr") or
                    soup.select(".board-list tr"))
            valid = [r for r in rows
                     if r.find("a") and len(r.find_all("td")) >= 2]
            if not valid:
                print(f"  [소진공-HTML] 페이지 {page}: 행 없음")
                # 진단
                if page == 1:
                    print(f"  [소진공] HTML 앞 800자: {resp.text[:800]}")
                break
            items.extend(valid)
        if items:
            print(f"  [소진공] HTML 성공: {len(items)}건")
    except Exception as e:
        print(f"  [소진공] 실패: {e}")
    return items

def normalize_semas(row):
    cols = row.find_all("td")
    a = None
    for td in cols:
        candidate = td.find("a")
        if candidate and len(candidate.get_text(strip=True)) > 5:
            a = candidate
            break
    if not a: return None
    title = a.get_text(strip=True)
    href  = a.get("href", "") or str(a.get("onclick", ""))
    # b_idx 추출
    bid_m = re.search(r"b_idx[=,]+(\d+)", href + str(row))
    raw_id = bid_m.group(1) if bid_m else str(abs(hash(title)))
    # 절대 URL
    if href.startswith("http"):
        url = href
    elif href.startswith("/"):
        url = SEMAS_BASE + href
    else:
        url = f"{SEMAS_BASE}/web/board/webBoardView.kmdc?bCd={SEMAS_BOARD_CD}&b_idx={raw_id}&pNm=BOA0103"

    texts = [c.get_text(strip=True) for c in cols]
    STATUS = {"접수중","마감","접수예정","접수마감","종료","전체","공지"}
    all_dates = [parse_date(t) for t in texts if parse_date(t)]
    org_cands = [t for t in texts if t and t != title and t not in STATUS
                 and not re.search(r"\d{4}", t) and 2 < len(t) < 30]
    return {"id": f"SEMAS-{raw_id}", "title": title,
            "org": org_cands[0] if org_cands else "소상공인시장진흥공단",
            "url": url,
            "reg_date": all_dates[0] if all_dates else "",
            "deadline": all_dates[-1] if len(all_dates) > 1 else "",
            "region": detect_region(title),
            "fields": detect_fields(title), "source": "소진공"}

def collect_semas():
    raw = fetch_semas()
    return [n for n in (normalize_semas(r) for r in raw) if n]

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

    print()
    print("[3단계] SMTECH 중소기업기술정보진흥원")
    notices_smt = collect_smtech()
    total += collect_source("SMTECH", notices_smt, existing)

    print()
    print("[3단계] 소진공 소상공인시장진흥공단")
    notices_semas = collect_semas()
    total += collect_source("소진공", notices_semas, existing)

    print(f"\n{'='*55}")
    print(f"  전체 신규 저장: {total}건  완료")
    print(f"{'='*55}\n")

if __name__ == "__main__":
    main()
