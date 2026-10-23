"""국가공무원 채용시스템 조회 실패에 대비한 실행기.

기존 scripts/check_notices.py, state/seen.json, docs/data/notices.json,
SMTP Secrets를 변경하지 않습니다. 정규 계리직 필터 조회에 실패할 때만
공식 국가공무원 채용시스템 홈페이지의 시험종류별 공고 목록을 사용합니다.
"""
from __future__ import annotations

import sys
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import check_notices as monitor

GOSI_HOME_BY_TYPE = "https://gongmuwon.gosi.kr/oprut/indexMain.do?paramTap=sp"
_ORIGINAL_QUERY_GOSI = monitor.query_gosi


def _parse_official_home(html: str) -> list[dict]:
    """홈페이지 '계리직' 영역의 제목과 게시일을 안전하게 추출합니다.

    제목에 '계리'가 명시되고 게시일을 찾을 수 있을 때만 저장합니다.
    웹페이지가 정상적으로 출력됐다는 이유만으로 0건 성공 처리하지 않습니다.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    if not any(term in text for term in ("국가공무원", "시험공고", "계리")):
        return []

    posts = monitor.parse_gosi_main(html)

    # 홈페이지 목록의 DOM 구조가 변경될 때를 대비한 좁은 범위 백업 파서
    # 노드의 상위 컨테이너에 제목+날짜가 함께 있는 경우만 수집합니다.
    for anchor in soup.find_all("a"):
        title = monitor.normalize_title(anchor.get_text(" ", strip=True))
        if not monitor.is_gyeri_title(title) or len(title) < 18:
            continue
        if not any(word in title for word in ("공고", "시험", "게시", "변경", "안내", "발표")):
            continue
        parent = anchor
        for _ in range(6):
            parent = parent.parent
            if parent is None:
                break
            nearby = parent.get_text(" ", strip=True)
            if len(nearby) > 650:
                break
            date = monitor.normalize_date(nearby)
            if not date:
                continue
            post = monitor.build_post(title, date, "국가공무원 채용시스템", GOSI_HOME_BY_TYPE)
            if post:
                posts.append(post)
            break

    result = monitor.dedupe(posts)
    # 파싱이 확인된 공고만 수집하며 링크는 실제 확인한 공식 목록으로 안내합니다.
    for p in result:
        if p["url"] == monitor.GOSI_URL:
            p["url"] = GOSI_HOME_BY_TYPE
    return result


def _query_official_home() -> list[dict]:
    errors = []
    try:
        html = monitor.load_html(monitor.make_session(), GOSI_HOME_BY_TYPE)
        posts = _parse_official_home(html)
        if posts:
            return posts
        errors.append("HTML: 공식 계리직 게시물의 제목·게시일 쌍을 찾지 못함")
    except Exception as exc:
        errors.append("HTML: " + monitor.display_error(exc))

    # 요청 결과가 부족할 때만 실제 렌더링된 공식 홈페이지에서 재시도합니다.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page(locale="ko-KR", user_agent=monitor.BROWSER_HEADERS["User-Agent"])
                page.goto(GOSI_HOME_BY_TYPE, wait_until="domcontentloaded", timeout=35000)
                page.wait_for_timeout(1300)
                posts = _parse_official_home(page.content())
                if posts:
                    return posts
                errors.append("브라우저: 계리직 공고/게시일 확인 실패")
            finally:
                browser.close()
    except Exception as exc:
        errors.append("브라우저: " + monitor.display_error(exc))
    raise RuntimeError("공식 홈페이지 예비 조회 실패 / " + " / ".join(errors))


def query_gosi_with_fallback() -> list[dict]:
    """기존 필터를 우선 사용하고 오류 발생 시 다른 공식 경로로 조회합니다."""
    try:
        return _ORIGINAL_QUERY_GOSI()
    except Exception as first_error:
        try:
            posts = _query_official_home()
            print(f"[예비 경로 정상] 국가공무원 채용시스템 계리직 공고 {len(posts)}건")
            return posts
        except Exception as backup_error:
            raise RuntimeError(
                "국가공무원 채용시스템 원래 필터와 공식 홈페이지 예비 경로 모두 실패 / "
                f"원래 경로: {monitor.display_error(first_error)} / "
                f"예비 경로: {monitor.display_error(backup_error)}"
            ) from backup_error


def main() -> int:
    monitor.query_gosi = query_gosi_with_fallback
    return monitor.main()


if __name__ == "__main__":
    sys.exit(main())
