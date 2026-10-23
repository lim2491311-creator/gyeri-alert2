from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest
import gosi_rescue as rescue

HOME_HTML = """<html><title>국가공무원 채용시스템</title><body>
<ul><li><a href='#'>2026년도 우정9급(계리) 공무원 공개경쟁채용 최종합격자 발표 및 채용후보자 등록 공고</a>
<span>2026-06-12</span></li>
<li><a href='#'>2026년도 9급 공채 일반 공고</a><span>2026-06-13</span></li>
<li><a href='#'>2026년도 우정9급(계리) 공무원 공개경쟁채용시험 시행계획 공고</a>
<span>2025-12-24</span></li></ul></body></html>"""


def test_official_home_fallback_collects_only_gyeri():
    posts = rescue._parse_official_home(HOME_HTML)
    assert len(posts) == 2
    assert all("계리" in p["title"] for p in posts)
    assert posts[0]["date"] == "2026-06-12"
    assert all(p["url"].startswith("https://gongmuwon.gosi.kr/") for p in posts)


def test_home_with_no_gyeri_does_not_fake_success():
    assert rescue._parse_official_home("<html>국가공무원 채용시스템</html>") == []


def test_official_home_http_path(monkeypatch):
    monkeypatch.setattr(rescue.monitor, "make_session", lambda: object())
    monkeypatch.setattr(rescue.monitor, "load_html", lambda *args, **kwargs: HOME_HTML)
    assert len(rescue._query_official_home()) == 2


def test_fallback_when_primary_fails(monkeypatch):
    def failed():
        raise RuntimeError("필터 결과 검증 실패")
    monkeypatch.setattr(rescue, "_ORIGINAL_QUERY_GOSI", failed)
    monkeypatch.setattr(rescue, "_query_official_home", lambda: rescue._parse_official_home(HOME_HTML))
    assert len(rescue.query_gosi_with_fallback()) == 2


def test_primary_result_is_preferred(monkeypatch):
    expected = rescue._parse_official_home(HOME_HTML)[:1]
    monkeypatch.setattr(rescue, "_ORIGINAL_QUERY_GOSI", lambda: expected)
    monkeypatch.setattr(rescue, "_query_official_home", lambda: pytest.fail("fallback should not run"))
    assert rescue.query_gosi_with_fallback() == expected


def test_both_fail_does_not_claim_zero(monkeypatch):
    def failed():
        raise RuntimeError("blocked")
    monkeypatch.setattr(rescue, "_ORIGINAL_QUERY_GOSI", failed)
    monkeypatch.setattr(rescue, "_query_official_home", failed)
    with pytest.raises(RuntimeError, match="모두 실패"):
        rescue.query_gosi_with_fallback()
