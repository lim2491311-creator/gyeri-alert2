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

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / 'docs/data/notices.json'
STATE_PATH = ROOT / 'state/seen.json'
POSTAL_URL = 'https://www.koreapost.go.kr/user/extra/kpost/95/294/bbs/recnotice/recNoticeList/jsp/ExtraUser.do'
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
    soup = BeautifulSoup(html, 'html.parser')
    results = []
    for row in soup.select('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) < 3:
            continue
        texts = [normalize_title(c.get_text(' ', strip=True)) for c in cells]
        # 시험종류에 '계리직'이 명시된 항목만 채택.
        if not any(text == '계리직' for text in texts[:3]):
            continue
        date = next((normalize_date(t) for t in texts if normalize_date(t)), '')
        titles = [t for t in texts if is_gyeri_title(t) and t != '계리직' and len(t) > 12]
        title = max(titles, key=len, default='')
        pid = extract_post_id(str(row))
        result = build_post(title, date, '국가공무원 채용시스템', GOSI_DETAIL + pid if pid else GOSI_URL)
        if result:
            results.append(result)
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


def query_postal() -> list[dict]:
    response = requests.get(POSTAL_URL, timeout=25,
                            headers={'User-Agent': 'Mozilla/5.0 (compatible; GyerijiNoticeCheck/1.0)'})
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    if not any(word in response.text for word in ('채용공고', '게시판 리스트', '인재채용팀')):
        raise RuntimeError('채용공고 게시판 HTML이 아닌 응답')
    # 공지 고정글을 포함하여 계리 관련 게시물 추출. 없더라도 게시판이면 유효한 0건.
    return parse_postal_html(response.text)


def query_gosi() -> list[dict]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(locale='ko-KR', user_agent='Mozilla/5.0 Chrome/125.0.0.0 Safari/537.36')
        try:
            page.goto(GOSI_URL, wait_until='domcontentloaded', timeout=35000)
            page.wait_for_timeout(1200)
            found_category = False
            for select in page.locator('select').all():
                try:
                    for option in select.locator('option').all():
                        if option.inner_text().strip() == '계리직':
                            select.select_option(value=option.get_attribute('value'), timeout=3000)
                            found_category = True
                            break
                except Exception:
                    pass
                if found_category:
                    break
            if found_category:
                try:
                    button = page.get_by_role('button', name=re.compile('검색')).first
                    if button.count() and button.is_visible():
                        button.click(timeout=3500)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
            results = parse_gosi_html(page.content())
            if results:
                return results
            # 검색에서 '계리직' 선택했고 표가 정상 로딩된 상태라면 새 글이 0건일 수 있습니다.
            if found_category and page.locator('table').count():
                return []
            page.goto(GOSI_MAIN, wait_until='domcontentloaded', timeout=35000)
            page.wait_for_timeout(1200)
            fallback = parse_gosi_main(page.content())
            if fallback:
                return fallback
            raise RuntimeError('계리직 게시판을 판독하지 못했습니다')
        finally:
            browser.close()


def query_both() -> tuple[list[dict], dict]:
    posts = []
    results = {}
    for label, function in [('우정사업본부', query_postal), ('국가공무원 채용시스템', query_gosi)]:
        try:
            current = function()
            posts.extend(current)
            results[label] = {'ok': True, 'count': len(current)}
        except Exception as exc:
            results[label] = {'ok': False, 'error': type(exc).__name__}
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
                         'message': '공식 사이트 2곳 모두 확인 실패. 공고 없음으로 판정하지 않습니다.',
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
