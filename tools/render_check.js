// 월 탭이 브라우저에서 오류 없이 렌더링되는지 확인하고 스크린샷을 저장한다.
// 사용: node tools/render_check.js <html경로> <월키:oct> <출력png> [chart.umd.js 경로]
//  - 샌드박스는 cdnjs 접근이 막혀 있으므로 chart.js 를 npm 으로 받아 로컬 파일로 대체한다.
//    (cd /tmp && npm i chart.js@4.4.1 playwright 후 경로 전달)
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const [html, key, out, chartPath] = process.argv.slice(2);
  const b = await chromium.launch();
  const pg = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errs = [];
  pg.on('pageerror', e => errs.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/ERR_FAILED|fonts/.test(m.text())) errs.push('console: ' + m.text()); });
  if (chartPath) await pg.route('**/chart.umd.js', r => r.fulfill({ path: path.resolve(chartPath), contentType: 'application/javascript' }));
  await pg.route(/fonts\.(googleapis|gstatic)/, r => r.abort());
  await pg.goto('file://' + path.resolve(html));
  await pg.waitForTimeout(1200);
  const info = await pg.evaluate(() => ({
    active: [...document.querySelectorAll('.tab-content.active')].map(e => e.id),
    header: document.getElementById('headerMeta').innerText.replace(/\n/g, ' '),
    tabs: [...document.querySelectorAll('.month-tab-btn')].map(b => b.innerText + (b.classList.contains('active') ? '*' : '')),
    canvases: [...document.querySelectorAll('.tab-content.active canvas')].length,
  }));
  // 모든 월 탭을 순회해 전환 오류가 없는지 확인
  const keys = await pg.evaluate(() => [...document.querySelectorAll('.tab-content')].map(e => e.id.replace('tab-', '')));
  for (const k of keys) { await pg.evaluate(k => switchMonthTab(k), k); await pg.waitForTimeout(250); }
  await pg.evaluate(k => switchMonthTab(k), key);
  await pg.waitForTimeout(500);
  await pg.screenshot({ path: out, fullPage: true });
  console.log(JSON.stringify({ ...info, errors: errs }, null, 1));
  await b.close();
  process.exit(errs.length ? 1 : 0);
})();
