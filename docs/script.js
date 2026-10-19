'use strict';
let currentFilter = 'all';
let currentData = null;
const byId = (id) => document.getElementById(id);
const KST = { timeZone: 'Asia/Seoul', hour12: false };
const POSTAL = 'https://www.koreapost.go.kr/user/extra/kpost/95/294/bbs/recnotice/recNoticeList/jsp/ExtraUser.do';
const GOSI = 'https://gongmuwon.gosi.kr/oprut/RpaRpTestPbancLst.do';
const WEB_ENABLED_KEY = 'gyeri-web-notice-enabled-v2';
const WEB_SEEN_KEY = 'gyeri-web-notice-seen-v2';
const storage = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); return true; } catch (_) { return false; } }
};
function safeSourceUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' && ['gongmuwon.gosi.kr', 'www.koreapost.go.kr'].includes(parsed.hostname)
      ? parsed.href : POSTAL;
  } catch (_) { return POSTAL; }
}
function formatKstDate(value, includeTime = false) {
  if (!value) return '미확인';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '미확인';
  const settings = includeTime ? {month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'}
    : {year:'numeric', month:'long', day:'numeric', weekday:'long'};
  return new Intl.DateTimeFormat('ko-KR', {...KST, ...settings}).format(date);
}
function kstToday() {
  const parts = new Intl.DateTimeFormat('en-US', {timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
  const map = Object.fromEntries(parts.map(x=>[x.type,Number(x.value)]));
  return Date.UTC(map.year,map.month-1,map.day);
}
function setupDday() {
  const difference = Math.round((Date.UTC(2027,3,17) - kstToday()) / 86400000);
  byId('exam-dday').textContent = difference > 0 ? `D-${difference}` : difference === 0 ? 'D-DAY' : `D+${-difference}`;
}
function create(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else node.setAttribute(key, value);
  }
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function displayNotices() {
  const area = byId('notices-list');
  area.replaceChildren();
  const all = Array.isArray(currentData?.items) ? currentData.items : [];
  const items = all.filter((p) => currentFilter === 'all' || String(p.title).includes('2027'));
  if (!items.length) {
    const empty = create('div', {class: 'empty-message'});
    empty.append(create('strong', {}, '아직 확인된 공고가 없습니다.'), create('span', {}, '공식 공고가 확인되면 이곳에 표시됩니다.'));
    area.append(empty); return;
  }
  for (const p of items) {
    const card = create('article', {class:'notice-card'});
    const content = create('div', {class:'notice-card-info'});
    const meta = create('div', {class:'notice-meta'});
    meta.append(create('span',{class:'category-pill'}, '계리직'));
    meta.append(create('time',{},p.date || '날짜 미확인'));
    if (String(p.title).includes('2027')) meta.append(create('span',{class:'year-pill'},'2027'));
    if (p.example) meta.append(create('span',{class:'example-pill'},'초기 예시'));
    const names = Array.isArray(p.sources) ? p.sources : [p.source || '공식 출처'];
    meta.append(create('span',{class:'source-brand'},names.join(' · ')));
    content.append(meta, create('h3', {class:'notice-title'},p.title || '제목 없음'));
    const open = create('a', {class:'notice-open', href:safeSourceUrl(p.url),target:'_blank',rel:'noopener noreferrer','aria-label':'공식 공고 원문 열기'},'↗');
    card.append(content,open);
    area.append(card);
  }
}
function updateStatus(data) {
  const badge = byId('monitor-status'),caption = byId('monitor-caption');
  const info = byId('data-message');
  info.className='data-message';
  if (data.status === 'ok') {badge.textContent='정상 확인';caption.textContent='우정사업본부 · 국가공무원 채용시스템';}
  else if (data.status === 'error') {badge.textContent='확인 실패';caption.textContent='공식 사이트에 접근하지 못했습니다';info.classList.add('error');}
  else if (data.status === 'warning') {badge.textContent='일부 오류';caption.textContent='한 곳의 확인 또는 이메일 발송이 실패했습니다';info.classList.add('warning');}
  else {badge.textContent='설정 대기';caption.textContent='GitHub 자동 실행을 활성화해 주세요';}
  const items = Array.isArray(data.items) ? data.items : [];
  const count = byId('count-2027');
  count.replaceChildren(document.createTextNode(data.status === 'setup' ? '—' : String(items.filter(x=>String(x.title).includes('2027')).length)),create('small',{},'건'));
  byId('last-checked').textContent = data.checked_at ? formatKstDate(data.checked_at,true) : '미확인';
  info.textContent = data.message || '';
  byId('email-status').replaceChildren(create('span',{class:'small-indicator'}),
    document.createTextNode(data.email_enabled ? '이메일 알림 연결됨' : '이메일 알림 미설정'));
  const sourceEl = byId('source-health'); sourceEl.replaceChildren();
  for (const [name,value] of Object.entries(data.sources || {})) {
    sourceEl.append(create('span',{class:data.status === 'setup' ? '' : value.ok ? '' : 'source-failure'},
      `${name} · ${data.status === 'setup' ? '연결 대기' : value.ok ? `확인 완료 (${value.count}건)` : `확인 실패: ${value.error || '원인 미확인'}`}`));
  }
  const newIds = Array.isArray(data.new_ids) ? data.new_ids : [];
  const unseen = items.filter(x=>newIds.includes(x.id));
  const banner=byId('new-notice-banner');
  banner.hidden = !unseen.length;
  banner.textContent = unseen.length ? `최근 점검에서 신규 계리직 공고 ${unseen.length}건이 확인되었습니다. 아래에서 공고를 확인하세요.` : '';
}
function refreshWebStatus() {
  const el = byId('browser-status');
  const available = 'Notification' in window && window.isSecureContext;
  const enabled = storage.get(WEB_ENABLED_KEY) === 'yes';
  const msg = !available ? '웹 알림을 지원하지 않는 환경입니다 (HTTPS 필요)'
    : !enabled ? '이 브라우저의 웹 알림 꺼짐'
    : Notification.permission === 'granted' ? '웹 알림 켜짐 · 사이트 방문 시 알림'
    : Notification.permission === 'denied' ? '브라우저에서 알림이 차단됨' : '웹 알림 권한 허용 필요';
  el.replaceChildren(create('span',{class:'small-indicator'}),document.createTextNode(msg));
  byId('enable-web-notifications').textContent=enabled ? '이 브라우저에서 알림 다시 설정 ↗':'이 브라우저에서 알림 켜기 ↗';
}
function compareWithPrevious(items) {
  if (storage.get(WEB_ENABLED_KEY) !== 'yes') return;
  const ids = items.filter(p=>!p.example).map(p=>p.id);
  const previousRaw=storage.get(WEB_SEEN_KEY);
  if (previousRaw === null) {storage.set(WEB_SEEN_KEY, JSON.stringify(ids)); return;}
  let prior=[];
  try { prior=JSON.parse(previousRaw); if(!Array.isArray(prior)) prior=[]; } catch(_){prior=[];}
  const previousSet = new Set(prior);
  const newItems = items.filter(p=>!p.example && !previousSet.has(p.id));
  // 기존 기록과 누적하여 일시적인 오류/게시판 페이지 차이에도 중복 방지.
  storage.set(WEB_SEEN_KEY,JSON.stringify([...new Set([...prior,...ids])].slice(-300)));
  if (!newItems.length) return;
  const banner=byId('new-notice-banner');banner.hidden=false;
  banner.textContent=`지난 방문 이후 계리직 새 공고 ${newItems.length}건이 등록되었습니다: ${newItems[0].title}`;
  if ('Notification' in window && Notification.permission==='granted') {
    try { new Notification('계리직 공식 새 공고', {body:`${newItems.length}건 · ${newItems[0].title}`,tag:'gyeri-notice'}); } catch (_) { /* 표시 배너로 대체 */ }
  }
}
async function enableBrowserNotifications() {
  if (!('Notification' in window) || !window.isSecureContext) {refreshWebStatus();return;}
  const result=await Notification.requestPermission();
  if (result==='granted') {
    storage.set(WEB_ENABLED_KEY,'yes');
    const ids=(currentData?.items||[]).filter(p=>!p.example).map(p=>p.id);
    storage.set(WEB_SEEN_KEY,JSON.stringify(ids));
  }
  refreshWebStatus();
}
async function loadData() {
  const button=byId('reload-button');button.disabled=true;
  try {
    const response=await fetch(`data/notices.json?t=${Date.now()}`,{cache:'no-store'});
    if (!response.ok) throw Error(`HTTP ${response.status}`);
    currentData=await response.json();
    updateStatus(currentData);
    displayNotices();
    if (currentData.status !== 'setup' && currentData.status !== 'error') compareWithPrevious(currentData.items || []);
  } catch (_) {
    const el=byId('data-message'); el.className='data-message error';
    el.textContent='웹사이트 데이터 로딩 실패. GitHub Pages 배포 상태를 확인해 주세요.';
    if (!currentData) byId('notices-list').replaceChildren(create('div',{class:'empty-message'},'공고 데이터를 불러오지 못했습니다.'));
  } finally {button.disabled=false;}
}
byId('today').textContent=formatKstDate(new Date().toISOString());
setupDday(); refreshWebStatus();
byId('reload-button').addEventListener('click', loadData);
byId('enable-web-notifications').addEventListener('click', enableBrowserNotifications);
for (const tab of document.querySelectorAll('.filter-tab')) {
  tab.addEventListener('click',()=>{
    currentFilter=tab.dataset.filter;
    for (const other of document.querySelectorAll('.filter-tab')) {
      const active=tab===other;other.classList.toggle('active',active);other.setAttribute('aria-pressed',String(active));
    }
    displayNotices();
  });
}
loadData();
// 웹페이지를 열어둔 동안 저장된 공고 데이터를 1분마다 갱신 (실제 공식 검사 주기와 별도)
window.setInterval(loadData,60_000);
