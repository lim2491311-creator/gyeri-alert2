import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_notices as m

POSTAL_HTML = '''
<html><h3>채용공고</h3><table><thead><tr><th>번호</th><th>공고</th><th>지역</th><th>제목</th><th>구분</th><th>담당부서</th><th>작성일</th></tr></thead>
<tr><td>공지</td><td>경력경쟁</td><td>전국</td><td><a href="#">2027년 우정사업본부 대체인력뱅크 사전공고</a></td><td>계약직</td><td>운영지원과</td><td>2026-10-16</td></tr>
<tr><td>공지</td><td>공개경쟁</td><td>전국</td><td><a href="javascript:void(0)" onclick="openPost('5234')">2027년도 우정9급(계리) 공개경쟁채용시험 일정 사전공고</a></td><td>우정직</td><td>인재채용팀</td><td>2026-10-22</td></tr>
<tr><td>공지</td><td>공개경쟁</td><td>전국</td><td><a href="#">2026년도 우정9급(계리) 시행계획 공고</a></td><td>우정직</td><td>인재채용팀</td><td>2025-12-24</td></tr>
<tr><td>9200</td><td>경력경쟁</td><td>경기</td><td><a>2027년 우정9급(집배) 경채 공고</a></td><td>우정직</td><td>지역</td><td>2026-10-23</td></tr>
</table></html>'''

GOSI_HTML = '''
<table><tr><th>번호</th><th>시험구분</th><th>공고명</th><th>게시일</th></tr>
<tr><td>1</td><td>9급 공채</td><td>2027년도 9급 공채 공고</td><td>2026-10-19</td></tr>
<tr data-pst-id="5011"><td>2</td><td>계리직</td><td><a href="/oprut/RpaRpTestPbancDtl.do?pstId=5011">2027년도 우정9급(계리) 공개경쟁채용시험 일정 사전공고</a></td><td>2026-10-22</td></tr>
<tr><td>3</td><td>계리직</td><td>2026년도 우정9급(계리) 최종합격자 발표</td><td>2026-06-12</td></tr>
</table>'''


def config():
    return {'username':'sender@gmail.com','password':'test-pass','recipient':'recipient@example.com',
            'host':'smtp.gmail.com','port':'465'}


def test_postal_pinned_and_filter_ignores_other_job_types():
    posts=m.parse_postal_html(POSTAL_HTML)
    assert len(posts)==2
    assert posts[0]['date']=='2026-10-22'
    assert posts[0]['source']=='우정사업본부'
    assert posts[0]['url']==m.POSTAL_URL  # 확인되지 않은 상세 URL을 지어내지 않음
    assert all('계리' in p['title'] for p in posts)


def test_gosi_category_only():
    posts=m.parse_gosi_html(GOSI_HTML)
    assert len(posts)==2
    assert posts[0]['url'].endswith('pstId=5011')


def test_dedupe_merges_two_official_sources():
    posts=m.dedupe(m.parse_postal_html(POSTAL_HTML)+m.parse_gosi_html(GOSI_HTML))
    assert len(posts)==3
    combined=[p for p in posts if '2027' in p['title']][0]
    assert len(combined['sources'])==2
    assert combined['url'].endswith('pstId=5011')


def setup_fake_files(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'DATA_PATH',tmp_path/'data.json')
    monkeypatch.setattr(m,'STATE_PATH',tmp_path/'seen.json')


def test_first_run_emails_2027_only_and_prevents_duplicates(tmp_path,monkeypatch):
    setup_fake_files(tmp_path,monkeypatch)
    sent=[]
    monkeypatch.setattr(m,'send_email',lambda p,c: sent.append(p['title']))
    posts=m.dedupe(m.parse_postal_html(POSTAL_HTML)+m.parse_gosi_html(GOSI_HTML))
    first=m.run_with_posts(posts,config())
    assert first['status']=='ok'
    assert len(sent)==1 and '2027' in sent[0]
    m.run_with_posts(posts,config())
    assert len(sent)==1


def test_failed_email_is_retried(tmp_path,monkeypatch):
    setup_fake_files(tmp_path,monkeypatch)
    def fail(p,c): raise RuntimeError('mail failure')
    monkeypatch.setattr(m,'send_email',fail)
    posts=m.parse_postal_html(POSTAL_HTML)
    result=m.run_with_posts(posts,config())
    assert result['status']=='warning'
    assert len(m.load_json(m.STATE_PATH,{})['pending_ids'])==1
    sent=[]
    monkeypatch.setattr(m,'send_email',lambda p,c:sent.append(p['id']))
    m.run_with_posts(posts,config())
    assert len(sent)==1
    assert not m.load_json(m.STATE_PATH,{})['pending_ids']


def test_no_email_keeps_pending_for_later(tmp_path,monkeypatch):
    setup_fake_files(tmp_path,monkeypatch)
    result=m.run_with_posts(m.parse_postal_html(POSTAL_HTML), {**config(),'password':''})
    assert result['email_enabled'] is False
    assert len(m.load_json(m.STATE_PATH,{})['pending_ids'])==1


def test_partial_official_failure_is_visible_but_still_emails(tmp_path,monkeypatch):
    setup_fake_files(tmp_path,monkeypatch)
    sent=[]
    monkeypatch.setattr(m,'send_email',lambda p,c:sent.append(p['title']))
    sources={'우정사업본부':{'ok':True,'count':2},'국가공무원 채용시스템':{'ok':False,'error':'TimeoutError'}}
    result=m.run_with_posts(m.parse_postal_html(POSTAL_HTML),config(),sources)
    assert result['status']=='warning'
    assert '국가공무원 채용시스템 확인 실패' in result['message']
    assert len(sent)==1


def test_only_new_post_after_initial_is_mailed(tmp_path,monkeypatch):
    setup_fake_files(tmp_path,monkeypatch)
    sent=[]
    monkeypatch.setattr(m,'send_email',lambda p,c:sent.append(p['title']))
    old=m.parse_gosi_html(GOSI_HTML)[1:]
    m.run_with_posts(old,config())
    assert not sent
    m.run_with_posts(m.parse_gosi_html(GOSI_HTML),config())
    assert len(sent)==1 and '2027' in sent[0]


def test_only_official_urls_allowed():
    assert m.allow_official_url('javascript:alert(1)',m.POSTAL_URL)==m.POSTAL_URL
    assert m.allow_official_url('https://evil.test/attack',m.POSTAL_URL)==m.POSTAL_URL
