# -*- coding: utf-8 -*-
"""
노션 BizinfoData 중복 정리 스크립트 (1회성 청소용)
같은 (출처, 제목) 조합이 여러 건이면 '가장 먼저 저장된 것' 하나만 남기고
나머지를 휴지통으로 보냅니다. (노션 휴지통에서 30일간 복구 가능)

필요 환경변수: NOTION_API_KEY, NOTION_DATABASE_ID
"""

import os, sys, time
import requests

NOTION_API_KEY     = os.environ.get("NOTION_API_KEY", "").strip()
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()

HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

def fetch_all_pages():
    pages = []
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    payload = {"page_size": 100}
    while True:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        pages.extend(data.get("results", []))
        if data.get("has_more"):
            payload["start_cursor"] = data["next_cursor"]
        else:
            break
    return pages

def page_key(page):
    props = page.get("properties", {})
    title = "".join(t.get("plain_text", "")
                    for t in props.get("제목", {}).get("title", [])).strip()
    sources = [o.get("name", "") for o in
               props.get("출처", {}).get("multi_select", [])]
    source = sources[0] if sources else ""
    return f"{source}|{title}", title, source

def archive_page(page_id):
    resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS, json={"archived": True}, timeout=60)
    return resp.status_code == 200

def main():
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        print("[오류] NOTION_API_KEY / NOTION_DATABASE_ID 환경변수가 필요합니다.")
        sys.exit(1)

    print("1) 전체 페이지 조회 중...")
    pages = fetch_all_pages()
    print(f"   총 {len(pages)}건")

    # (출처|제목) 기준으로 묶기
    groups = {}
    for pg in pages:
        key, title, source = page_key(pg)
        if not title:
            continue
        groups.setdefault(key, []).append(pg)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    total_dup = sum(len(v) - 1 for v in dup_groups.values())
    print(f"2) 중복 그룹: {len(dup_groups)}개, 삭제 대상: {total_dup}건")

    if total_dup == 0:
        print("   중복 없음 — 정리할 것이 없습니다.")
        return

    deleted = 0
    for key, group in dup_groups.items():
        # 가장 먼저 저장된 것(created_time 최소)만 남김
        group.sort(key=lambda p: p.get("created_time", ""))
        keep, extras = group[0], group[1:]
        title_short = key.split("|", 1)[-1][:40]
        for pg in extras:
            if archive_page(pg["id"]):
                deleted += 1
                print(f"   [삭제] {title_short} ({deleted}/{total_dup})")
            else:
                print(f"   [실패] {title_short}")
            time.sleep(0.4)  # 노션 API 속도 제한 준수

    print(f"3) 완료: {deleted}건 휴지통 이동 (노션 휴지통에서 30일간 복구 가능)")

if __name__ == "__main__":
    main()
