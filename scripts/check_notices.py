"""우정사업본부(우선) + 국가공무원 채용시스템 계리직 공고 검사.

공개 HTML 모니터. 새 공고 메일은 SMTP 설정이 있을 때만 발송합니다.
어느 한쪽의 장애를 다른 쪽의 '새 공고 없음'으로 오판하지 않습니다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / 'docs/data/notices.json'
STATE_PATH = ROOT / 'state/seen.json'
POSTAL_URL = 'https://www.koreapost.go.kr/user/extra/kpost/95/294/bbs/recnotice/recNoticeList/jsp/ExtraUser.do'
# 같은 채용공고를 표시하는 우정사업본부 산하기관의 공식 공유 게시판
POSTAL_MIRRORS = [
    'https://www.koreapost.go.kr/user/extra/638/95/1933/bbs/recnotice/recNoticeList/jsp/ExtraUser.do',
    'https://www.koreapost.go.kr/user/extra/220c/95/3653/bbs/recnotice/recNoticeList/jsp/ExtraUser.do',
]
GOSI_URL = 'https://gongmuwon.gosi.kr/oprut/RpaRpTestPbancLst.do'
GOSI_MAIN = 'https://gongmuwon.gosi.kr/oprut/indexMain.do'
GOSI_DETAIL = 'https://gongmuwon.gosi.kr/oprut/RpaRpTestPbancDtl.do?pstId='
SOURCES = ['우정사업본부', '국가공무원 채용시스템']
DATE_RE = re.compile(r'20\d{2}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2}')
ID_RE = re.compile(r'(?:pstId|pst_id|pstNo|bbsId|postId|bbsDataId)\s*[=:,\'"(\s]+[\'\"]?(\d{3,9})', re.I)
GYE_RI = re.compile(r'계\s*리', re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    return json.loads(path.read_text(encoding='utf-8'))


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def normalize_date(value: str) -> str:
    match = DATE_RE.search(value)
    if not match:
        return ''
    y, m, d = (int(x) for x in re.findall(r'\d+', match.group()))
    return f'{y:04d}-{m:02d}-{d:02d}'


def normalize_title(value: str) -> str:
    return re.sub(r'\s+', ' ', value).replace(' NEW', '').strip()


def is_gyeri_title(title: str) -> bool:
    """집배/대체인력/공무직 오알림 방지; '계리'가 명시된 제목만 채택."""
    return bool(GYE_RI.search(title))


def extract_post_id(html: str) -> str:
    match = ID_RE.search(html)
    return match.group(1) if match else ''


def allow_official_url(url: str, base: str) -> str:
    absolute = urljoin(base, url)
    parsed = urlparse(absolute)
    if parsed.scheme == 'https' and (parsed.hostname == 'gongmuwon.gosi.kr' or parsed.hostname == 'www.koreapost.go.kr'):
        return absolute
    return base


def build_post(title: str, date: str, source: str, url: str = '') -> dict:
    title, date = normalize_title(title), normalize_date(date)
    if not title or not date or not is_gyeri_title(title):
        return {}
    fingerprint = 'notice:' + hashlib.sha256(f'{date}|{title}'.encode('utf-8')).hexdigest()[:20]
    home = POSTAL_URL if source == '우정사업본부' else GOSI_URL
    return {'id': fingerprint, 'title': title, 'date': date,
            'url': allow_official_url(url or home, home),
            'category': '계리직', 'source': source, 'sources': [source]}


def parse_postal_html(html: str) -> list[dict]:
    """우정사업본부 게시판의 공지 고정글 + 일반글 모두 검색."""
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for row in soup.select('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) < 5:
            continue
        fields = [normalize_title(td.get_text(' ', strip=True)) for td in cells]
        dates = [normalize_date(t) for t in fields]
        date = next((d for d in dates if d), '')
        if not date:
            continue
        # 공고분류/지역/제목/구분/담당부서/작성일 등이 표 컬럼으로 나타남.
        anchors = [a for a in row.find_all('a') if is_gyeri_title(a.get_text(' ', strip=True))]
        title = normalize_title(anchors[0].get_text(' ', strip=True)) if anchors else ''
        if not title:
            title = next((x for x in fields if is_gyeri_title(x) and len(x) > 14), '')
        if not title:
            continue
        url = POSTAL_URL
        if anchors:
            href = (anchors[0].get('href') or '').strip()
            if href and not href.lower().startswith(('javascript:', '#')):
                url = allow_official_url(href, POSTAL_URL)
            # onclick 형식의 링크는 상세 URL을 검증하기 전까지 목록 페이지로 연결.
        post = build_post(title, date, '우정사업본부', url)
        if post:
            records.append(post)
    return dedupe(records)


def parse_gosi_html(html: str) -> list[dict]:
    """표 형식이 달라도 제목+날짜+계리직 분류를 이용합니다."""
    soup = BeautifulSoup(html, 'html.parser')
    results = []
    for row in soup.select('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) < 2:
            continue
        texts = [normalize_title(c.get_text(' ', strip=True)) for c in cells]
        date = next((d for t in texts if (d := normalize_date(t))), '')
        if not date:
            continue
        category = any(t == '계리직' for t in texts)
        links = [normalize_title(a.get_text(' ', strip=True)) for a in row.select('a')]
        titles = [t for t in links + texts if is_gyeri_title(t) and t != '계리직' and len(t) > 12]
        if not titles or not (category or any('우정9급' in t and is_gyeri_title(t) for t in titles)):
            continue
        title = max(titles, key=len)
        pid = extract_post_id(str(row))
        detail = next((a.get('href', '') for a in row.select('a') if is_gyeri_title(a.get_text(' ', strip=True)) and a.get('href', '').startswith('https://gongmuwon.gosi.kr/oprut/')), '')
        post = build_post(title, date, '국가공무원 채용시스템', detail or (GOSI_DETAIL + pid if pid else GOSI_URL))
        if post:
            results.append(post)
    return dedupe(results)

def parse_gosi_main(html: str) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    results = []
    for node in soup.find_all(string=lambda s: bool(s and GYE_RI.search(str(s)))):
        title = normalize_title(str(node))
        if len(title) < 18 or not any(k in title for k in ('공고', '시험', '게시', '변경', '안내')):
            continue
        parent = node.parent
        for _ in range(5):
            if parent is None:
                break
            content = parent.get_text(' ', strip=True)
            date = normalize_date(content)
            if date and len(content) < 1500:
                pid = extract_post_id(str(parent))
                post = build_post(title, date, '국가공무원 채용시스템', GOSI_DETAIL + pid if pid else GOSI_URL)
                if post:
                    results.append(post)
                break
            parent = parent.parent
    return dedupe(results)


def dedupe(posts: list[dict]) -> list[dict]:
    result = {}
    for p in posts:
        key = p['id']
        if key not in result:
            result[key] = dict(p)
            result[key]['sources'] = list(p.get('sources', [p['source']]))
            continue
        old = result[key]
        for source in p.get('sources', [p['source']]):
            if source not in old['sources']:
                old['sources'].append(source)
        # 우정사업본부 우선, 단 실제 세부 링크가 제공된다면 그 링크를 선택.
        old_detail = old['url'] not in (POSTAL_URL, GOSI_URL)
        new_detail = p['url'] not in (POSTAL_URL, GOSI_URL)
        if (new_detail and not old_detail) or (new_detail == old_detail and p['source'] == '우정사업본부'):
            old['url'] = p['url']
            old['source'] = p['source']
    return sorted(result.values(), key=lambda x: (x['date'], x['id']), reverse=True)


# 봇 식별 UA만 보냈을 때 일부 기관이 403을 반환할 수 있어 일반 브라우저 헤더를 사용합니다.
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.7',
}


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    retry = Retry(total=2, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    return session


def load_html(session: requests.Session, url: str, **kwargs) -> str:
    response = session.get(url, timeout=18, **kwargs)
    response.raise_for_status()
    # 한국어 HTML을 자동 감지 결과가 잘못 디코딩하는 문제를 방지합니다.
    response.encoding = response.encoding if response.encoding and response.encoding.lower() != 'iso-8859-1' else (response.apparent_encoding or 'utf-8')
    text = response.text
    if '<html' not in text.lower() and '<table' not in text.lower():
        raise RuntimeError('HTML 문서가 아닌 응답')
    return text


def valid_postal_board(html: str) -> bool:
    soup = BeautifulSoup(html, 'html.parser')
    headers = soup.get_text(' ', strip=True)
    # 단순히 사이트 메뉴에 '채용공고'가 있다는 이유로 성공 처리하지 않습니다.
    return bool(soup.select('tr')) and '채용공고' in headers and '작성일' in headers and '제목' in headers


def valid_gosi_board(html: str) -> bool:
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ', strip=True)
    return bool(soup.select('table')) and '시험구분' in text and '게시일' in text and '공고명' in text


def display_error(exc: Exception) -> str:
    """공개 JSON/로그에는 민감한 값 없이 오류 종류와 상태 코드만 기록합니다."""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return f'HTTP {exc.response.status_code}'
    if isinstance(exc, requests.exceptions.Timeout):
        return '응답 시간 초과'
    if isinstance(exc, requests.exceptions.ConnectionError):
        return '연결 오류 (DNS/SSL/접속 차단 가능)'
    return type(exc).__name__ + (f': {str(exc)[:100]}' if isinstance(exc, RuntimeError) else '')


def query_postal() -> list[dict]:
    errors = []
    session = make_session()
    # 우정사업본부 본부 게시판이 차단되면 공식 공유 게시판을 순서대로 확인합니다.
    for url in [POSTAL_URL, *POSTAL_MIRRORS]:
        try:
            html = load_html(session, url)
            if not valid_postal_board(html):
                raise RuntimeError('채용공고 목록 표를 찾지 못함')
            return parse_postal_html(html)
        except Exception as exc:
            errors.append(f'{urlparse(url).path.split("/")[3]}: {display_error(exc)}')
    # 요청 방식이 막힌 경우 브라우저 렌더링을 마지막으로 시도합니다.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page = browser.new_page(locale='ko-KR', user_agent=BROWSER_HEADERS['User-Agent'])
                page.goto(POSTAL_URL, wait_until='domcontentloaded', timeout=25000)
                page.locator('table').first.wait_for(timeout=8000)
                html = page.content()
                if not valid_postal_board(html):
                    raise RuntimeError('브라우저로도 채용공고 표 확인 실패')
                return parse_postal_html(html)
            finally:
                browser.close()
    except Exception as exc:
        errors.append('브라우저: '+display_error(exc))
    raise RuntimeError('우정사업본부 공식 채용공고 조회 불가 / ' + ' / '.join(errors))


def gosi_filter(html: str) -> tuple[str, str]:
    """필터 HTML의 select에서 '계리직' 옵션 값과 필드 이름을 찾습니다."""
    soup = BeautifulSoup(html, 'html.parser')
    for select in soup.select('select'):
        for option in select.select('option'):
            if normalize_title(option.get_text(' ', strip=True)) == '계리직':
                return (select.get('name') or select.get('id') or 'testGrpCd', option.get('value', ''))
    return ('', '')


def filtered_gosi_result(html: str) -> list[dict] | None:
    """계리 게시글 또는 진짜 0건 응답만 유효. 미적용 전체 목록은 실패로 취급."""
    if not valid_gosi_board(html):
        return None
    posts = parse_gosi_html(html)
    if posts:
        return posts
    if any(x in BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
           for x in ('조회된 결과가 없습니다', '검색 결과가 없습니다', '검색결과가 없습니다')):
        return []
    return None


def query_gosi() -> list[dict]:
    errors = []
    try:
        session = make_session()
        original_html = load_html(session, GOSI_URL)
        field, value = gosi_filter(original_html)
        if field and value:
            # 공식 홈페이지에서 제공한 계리직 필터 값을 재사용합니다.
            params = {field: value, 'testGrpCd': value, 'sGubun': '', 'sKeyword': ''}
            try:
                filtered_html = load_html(session, GOSI_URL, params=params)
                filtered = filtered_gosi_result(filtered_html)
                if filtered is not None:
                    return filtered
            except Exception as exc:
                errors.append('HTML 필터: ' + display_error(exc))
        # 필터가 없는 일반 목록에서 계리 게시물이 노출된 경우는 수집합니다.
        posts = parse_gosi_html(original_html)
        if posts:
            return posts
        errors.append('HTML: 계리직 항목/유효 필터 조회 불가')
    except Exception as exc:
        errors.append('HTML 요청: '+display_error(exc))

    # 계리직 필터를 자동 선택하는 브라우저 경로. 검색 버튼 형태가 달라도 대응.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page = browser.new_page(locale='ko-KR', user_agent=BROWSER_HEADERS['User-Agent'])
                page.goto(GOSI_URL, wait_until='domcontentloaded', timeout=35000)
                page.wait_for_timeout(1600)
                selected = False
                for dropdown in page.locator('select').all():
                    try:
                        options = dropdown.locator('option').all()
                        match = next((o for o in options if o.inner_text().strip() == '계리직'), None)
                        if match is None:
                            continue
                        dropdown.select_option(value=match.get_attribute('value'), timeout=3000)
                        selected = True
                        break
                    except Exception:
                        continue
                if selected:
                    for selector in ['button:has-text("검색")', 'input[type="submit"]', 'a:has-text("검색")', '[onclick*="search"]', '[onclick*="Search"]']:
                        try:
                            target = page.locator(selector).first
                            if target.count() and target.is_visible():
                                target.click(timeout=3500)
                                break
                        except Exception:
                            continue
                    page.wait_for_timeout(2200)
                    filtered = filtered_gosi_result(page.content())
                    if filtered is not None:
                        return filtered
                # 필터 조작 실패해도 게시판에 계리직 항목이 있으면 수집합니다.
                results = parse_gosi_html(page.content())
                if results:
                    return results
                errors.append('브라우저: 필터 결과 검증 실패')
            finally:
                browser.close()
    except Exception as exc:
        errors.append('브라우저: '+display_error(exc))
    raise RuntimeError('국가공무원 채용시스템 조회 불가 / ' + ' / '.join(errors))

def query_both() -> tuple[list[dict], dict]:
    posts = []
    results = {}
    for label, function in [('우정사업본부', query_postal), ('국가공무원 채용시스템', query_gosi)]:
        try:
            current = function()
            posts.extend(current)
            results[label] = {'ok': True, 'count': len(current)}
        except Exception as exc:
            results[label] = {'ok': False, 'error': display_error(exc)}
            print(f'[수집 오류] {label}: {display_error(exc)}', file=sys.stderr)
    return dedupe(posts), results


def send_email(post: dict, config: dict) -> None:
    """수신 이메일/SMTP 비밀번호는 GitHub Secret에서만 읽습니다."""
    message = EmailMessage()
    message['Subject'] = '[계리직 공고 알림] ' + post['title']
    message['From'] = config['username']
    message['To'] = config['recipient']
    message.set_content('계리직 공식 공고가 새로 확인되었습니다.\n\n'
                        f"제목: {post['title']}\n게시일: {post['date']}\n"
                        f"출처: {', '.join(post.get('sources', [post['source']]))}\n"
                        f"공식 공고: {post['url']}\n\n"
                        '이 알림은 개인 계리직 공고 알리미에서 발송되었습니다.\n')
    with smtplib.SMTP_SSL(config['host'], int(config['port']), timeout=25,
                          context=ssl.create_default_context()) as server:
        server.login(config['username'], config['password'])
        server.send_message(message)


def email_settings() -> dict:
    return {'host': os.environ.get('SMTP_HOST', 'smtp.gmail.com').strip(),
            'port': os.environ.get('SMTP_PORT', '465').strip(),
            'username': os.environ.get('SMTP_USERNAME', '').strip(),
            'password': os.environ.get('SMTP_PASSWORD', '').strip(),
            'recipient': os.environ.get('ALERT_EMAIL_TO', '').strip()}


def merge_items(previous: list[dict], fresh: list[dict]) -> list[dict]:
    historical = [p for p in previous if not p.get('example')]
    return dedupe(fresh + historical)[:80]


def run_with_posts(posts: list[dict], config: dict, source_results: dict | None = None) -> dict:
    """데이터 병합, 이메일 재시도, 공개 상태 저장. 테스트에서는 발송함수를 교체할 수 있습니다."""
    source_results = source_results or {name: {'ok': True, 'count': len(posts)} for name in SOURCES}
    previous = load_json(DATA_PATH, {'items': []})
    state = load_json(STATE_PATH, {'initialized': False, 'known_ids': [], 'pending_ids': [], 'sent_ids': []})
    email_enabled = all(config.values())
    first = not state.get('initialized', False)
    known = set(state.get('known_ids', []))
    pending = set(state.get('pending_ids', []))
    sent = set(state.get('sent_ids', []))
    newly_found = []
    for p in posts:
        if p['id'] not in known:
            newly_found.append(p['id'])
            if not first or '2027' in p['title']:
                pending.add(p['id'])
            known.add(p['id'])
    all_items = merge_items(previous.get('items', []), posts)
    id_to_post = {p['id']: p for p in all_items}
    issues = [f'{name} 확인 실패' for name, result in source_results.items() if not result['ok']]
    if email_enabled:
        for post_id in sorted(pending):
            post = id_to_post.get(post_id)
            if not post:
                continue
            try:
                send_email(post, config)
                sent.add(post_id)
                pending.discard(post_id)
            except Exception as exc:
                issues.append(f'이메일 전송 실패 ({type(exc).__name__})')
    save_json(STATE_PATH, {'initialized': True, 'known_ids': sorted(known),
                           'pending_ids': sorted(pending), 'sent_ids': sorted(sent)})
    result = {'checked_at': utc_now(), 'status': 'warning' if issues else 'ok',
              'message': ' / '.join(issues) if issues else '공식 계리직 채용공고 확인 완료',
              'alerts_enabled': email_enabled, 'email_enabled': email_enabled,
              'sources': source_results, 'new_ids': newly_found,
              'source_url': POSTAL_URL, 'items': all_items}
    save_json(DATA_PATH, result)
    return result


def main() -> int:
    config = email_settings()
    posts, results = query_both()
    if not any(x.get('ok') for x in results.values()):
        previous = load_json(DATA_PATH, {'items': []})
        previous.update({'checked_at': utc_now(), 'status': 'error',
                         'message': '공식 사이트 2곳 모두 확인 실패. 이전 공고만 표시하며, 새 공고 없음으로 판정하지 않습니다.',
                         'alerts_enabled': all(config.values()),
                         'email_enabled': all(config.values()), 'sources': results,
                         'new_ids': []})
        save_json(DATA_PATH, previous)
        print('두 사이트 모두 확인 실패: ' + repr(results), file=sys.stderr)
        return 0
    result = run_with_posts(posts, config, results)
    print(f"계리직 공고 {len(posts)}건, 상태={result['status']}, 이메일={result['email_enabled']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
