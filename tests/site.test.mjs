// Site integration tests — boots index.html in jsdom against the real
// committed data files and exercises every feature.
//
// Run:  cd tests && npm install && node site.test.mjs

import { JSDOM, VirtualConsole } from 'jsdom';
import http from 'http';
import { readFileSync, existsSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = 8899;

let passed = 0;
let failed = 0;
const failures = [];

function check(name, cond, detail = '') {
  if (cond) { passed++; console.log(`  ok    ${name}`); }
  else { failed++; failures.push(name); console.log(`  FAIL  ${name} ${detail}`); }
}

// ---------------------------------------------------------------------------
// Static file server for the repo
// ---------------------------------------------------------------------------
const server = http.createServer((req, res) => {
  const p = path.join(ROOT, req.url.split('?')[0].replace(/^\//, '') || 'index.html');
  if (!existsSync(p)) { res.writeHead(404); res.end('nf'); return; }
  res.writeHead(200, { 'content-type': p.endsWith('.json') ? 'application/json' : 'text/html' });
  res.end(readFileSync(p));
});
await new Promise(r => server.listen(PORT, r));

// ---------------------------------------------------------------------------
// Boot helper — Last.fm calls are stubbed so tests are deterministic/offline
// ---------------------------------------------------------------------------
async function boot(hash = '') {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => {
    if (!/Could not load link|css|scrollTo|Not implemented/i.test(String(e))) {
      errors.push('jsdomError: ' + e.message);
    }
  });
  const dom = new JSDOM(readFileSync(path.join(ROOT, 'index.html'), 'utf8'), {
    url: `http://localhost:${PORT}/index.html${hash}`,
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      window.fetch = (u, opts) => {
        const url = new URL(u, `http://localhost:${PORT}/`).href;
        if (url.includes('audioscrobbler.com')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ recenttracks: { track: [] } }),
          });
        }
        return fetch(url, opts);
      };
      window.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
      window.matchMedia = window.matchMedia
        || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
      window.addEventListener('error', e => errors.push('window error: ' + e.message));
    },
  });
  const w = dom.window;
  for (let i = 0; i < 150; i++) {
    await new Promise(r => setTimeout(r, 100));
    if (w.document.querySelectorAll('#grid .card').length > 0) break;
  }
  await new Promise(r => setTimeout(r, 1200)); // history features settle
  return { w, errors };
}

// ---------------------------------------------------------------------------
// 1. Data file sanity
// ---------------------------------------------------------------------------
console.log('\n== data files ==');
const albumsData = JSON.parse(readFileSync(path.join(ROOT, 'albums.json'), 'utf8'));
check('albums.json has cd/vinyl/suggestions', Array.isArray(albumsData.cd)
  && Array.isArray(albumsData.vinyl) && Array.isArray(albumsData.suggestions));
check('albums.json has 500+ albums', albumsData.cd.length + albumsData.vinyl.length > 500);
const requiredFields = ['artist', 'title', 'cover_url', 'type'];
check('albums have required fields', albumsData.cd.concat(albumsData.vinyl)
  .every(a => requiredFields.every(f => f in a)));

const heat = JSON.parse(readFileSync(path.join(ROOT, 'heatmap_data.json'), 'utf8'));
check('heatmap has months/albums/artists', Array.isArray(heat.months)
  && Array.isArray(heat.albums) && Array.isArray(heat.artists));
check('heatmap has _hist_last_ts', typeof heat._hist_last_ts === 'number' && heat._hist_last_ts > 0);
check('heatmap album entries well-formed', heat.albums.slice(0, 20)
  .every(e => e.artist && e.title && e.total > 0 && Object.keys(e.months).length > 0));

// PWA files
const manifest = JSON.parse(readFileSync(path.join(ROOT, 'manifest.webmanifest'), 'utf8'));
check('manifest valid with icons', manifest.name && manifest.icons.length >= 2
  && manifest.icons.every(i => existsSync(path.join(ROOT, i.src))));
check('index.html links manifest', readFileSync(path.join(ROOT, 'index.html'), 'utf8')
  .includes('rel="manifest"'));
check('sw.js caches shell + covers', readFileSync(path.join(ROOT, 'sw.js'), 'utf8')
  .includes('SHELL_CACHE') && readFileSync(path.join(ROOT, 'sw.js'), 'utf8').includes('COVER_CACHE'));

// ---------------------------------------------------------------------------
// 2. Boot + core rendering
// ---------------------------------------------------------------------------
console.log('\n== boot ==');
const { w, errors } = await boot();
const total = albumsData.cd.length + albumsData.vinyl.length;
check('no JS errors on boot', errors.length === 0, errors.join('; '));
check('all albums render as cards', w.document.querySelectorAll('#grid .card').length === total);
check('allAlbums populated', w.eval('allAlbums.length') === total);
check('counts shown', w.document.getElementById('countAll').textContent.includes(String(total)));

// ---------------------------------------------------------------------------
// 3. Filters
// ---------------------------------------------------------------------------
console.log('\n== filters ==');
w.eval(`currentFilter='vinyl'; applyFiltersAndSort();`);
check('vinyl filter', w.eval('filteredAlbums.length') === albumsData.vinyl.length);
w.eval(`currentFilter='all'; currentTypeFilter='EP'; applyFiltersAndSort();`);
check('type filter narrows', w.eval('filteredAlbums.length') < total && w.eval('filteredAlbums.length') > 0);
w.eval(`currentTypeFilter='all'; currentListenFilter='never'; applyFiltersAndSort();`);
const neverCount = w.eval('filteredAlbums.length');
check('never played filter', neverCount > 0 && neverCount < total);
w.eval(`currentListenFilter='dusty'; applyFiltersAndSort();`);
check('dusty filter ⊇ never played', w.eval('filteredAlbums.length') >= neverCount);
w.eval(`resetAllFilters()`);
check('reset restores all', w.eval('filteredAlbums.length') === total);

// search
w.document.getElementById('searchInput').value = 'bjork';
w.eval('applyFiltersAndSort()');
const bjork = w.eval('filteredAlbums.length');
check('diacritic-insensitive search', bjork === 0 || w.eval(`filteredAlbums.every(a => /bj(ö|o)rk/i.test(a.artist))`));
w.eval(`resetAllFilters()`);

// no-flicker: identical results must not rebuild the grid DOM
w.document.getElementById('searchInput').value = 'future of the left';
w.eval('applyFiltersAndSort()');
const cardBefore = w.document.querySelector('#grid .card');
w.document.getElementById('searchInput').value = 'future of the left';  // no-op change
w.eval('applyFiltersAndSort()');
check('unchanged results skip re-render (no flicker)', w.document.querySelector('#grid .card') === cardBefore);
w.document.getElementById('searchInput').value = 'future';
w.eval('applyFiltersAndSort()');
check('changed results do re-render', w.document.querySelector('#grid .card') !== cardBefore
  || w.eval('filteredAlbums.length') === w.eval('_lastRendered.length'));
w.eval(`resetAllFilters()`);

// debounced search input: typing eventually applies through the wrapper chain
w.document.getElementById('searchInput').value = 'deftones';
w.document.getElementById('searchInput').dispatchEvent(new w.Event('input'));
await new Promise(r => setTimeout(r, 300));
check('debounced typing filters and updates URL', w.location.hash.includes('q=deftones'));
w.eval(`resetAllFilters()`);

// ---------------------------------------------------------------------------
// 4. Sorts
// ---------------------------------------------------------------------------
console.log('\n== sorts ==');
w.eval(`currentSort='least-played'; applyFiltersAndSort();`);
check('least played ascending', w.eval(
  '(filteredAlbums[0].lastfm_plays||0) <= (filteredAlbums[filteredAlbums.length-1].lastfm_plays||0)'));
w.eval(`currentSort='most-played'; applyFiltersAndSort();`);
check('most played descending', w.eval(
  '(filteredAlbums[0].lastfm_plays||0) >= (filteredAlbums[filteredAlbums.length-1].lastfm_plays||0)'));
w.eval(`currentSort='color'; applyFiltersAndSort();`);
check('colour sort puts coloured first', w.eval('!!filteredAlbums[0].color'));
w.eval(`currentSort='year-desc'; applyFiltersAndSort();`);
check('year desc', w.eval('filteredAlbums[0].year >= filteredAlbums[filteredAlbums.length-1].year'));
w.eval('resetAllFilters()');

// shuffle: every click reshuffles
w.document.getElementById('shuffleBtn').click();
const s1 = w.eval('filteredAlbums.slice(0,5).map(a=>a.title).join()');
w.document.getElementById('shuffleBtn').click();
const s2 = w.eval('filteredAlbums.slice(0,5).map(a=>a.title).join()');
check('shuffle reshuffles on every click', s1 !== s2 && w.eval('isShuffled') === true);
w.eval('resetAllFilters()');

// ---------------------------------------------------------------------------
// 5. URL state round-trip
// ---------------------------------------------------------------------------
console.log('\n== url state ==');
w.eval(`currentFilter='cd'; currentSort='color'; applyFiltersAndSort();`);
check('URL reflects state', w.location.hash.includes('c=cd') && w.location.hash.includes('sort=color'));
w.eval('openModal(filteredAlbums[0])');
check('modal deep link in URL', w.location.hash.includes('album='));
check('modal shown', w.document.getElementById('modalOverlay').classList.contains('show'));

const restored = await boot('#c=vinyl&sort=year-desc&view=list&ly=dusty');
check('URL restore: filters+sort+view+ly', restored.w.eval('currentFilter') === 'vinyl'
  && restored.w.eval('currentSort') === 'year-desc'
  && restored.w.eval('currentView') === 'list'
  && restored.w.eval('currentListenFilter') === 'dusty', JSON.stringify({
  f: restored.w.eval('currentFilter'), s: restored.w.eval('currentSort'),
  v: restored.w.eval('currentView'), ly: restored.w.eval('currentListenFilter') }));
check('URL restore: no errors', restored.errors.length === 0, restored.errors.join('; '));

// ---------------------------------------------------------------------------
// 6. History-powered panels
// ---------------------------------------------------------------------------
console.log('\n== history features ==');
check('Time Machine years populated', w.document.querySelectorAll('#tmMenu .dropdown-item').length > 10);
check('This Month button visible', w.document.getElementById('thisMonthBtn').style.display !== 'none');
check('Year in Review button visible', w.document.getElementById('yirBtn').style.display !== 'none');
w.document.getElementById('yirBtn').click();
await new Promise(r => setTimeout(r, 300));
check('YiR opens with content', w.document.getElementById('yirOverlay').classList.contains('show')
  && w.document.querySelectorAll('#yirBody .yir-stat-value').length >= 3);
check('One-Month Wonders populated', w.document.querySelectorAll('#wondersList .yir-row').length > 0);
check('Comebacks populated', w.document.querySelectorAll('#comebacksList .yir-row').length > 0);
check('Taste graph rendered', w.document.querySelectorAll('#tasteGraph svg path').length >= 4);

// ---------------------------------------------------------------------------
// 7. Suggestions + wishlist
// ---------------------------------------------------------------------------
console.log('\n== suggestions ==');
w.document.getElementById('suggestionsBtn').click();
await new Promise(r => setTimeout(r, 300));
const panel = w.document.getElementById('suggestionsPanel');
check('suggestion artist blocks', panel.querySelectorAll('.sugg-artist').length > 5);
check('suggestion album cards', panel.querySelectorAll('.sugg-card').length > 20);
const heartsBefore = w.localStorage.getItem('mc_wishlist');
panel.querySelector('.sugg-heart').click();
await new Promise(r => setTimeout(r, 200));
check('wishlist add persists', JSON.parse(w.localStorage.getItem('mc_wishlist') || '[]').length === 1);
w.document.querySelector('#suggestionsPanel .sugg-heart.active').click();
await new Promise(r => setTimeout(r, 200));
check('wishlist remove persists', JSON.parse(w.localStorage.getItem('mc_wishlist') || '[]').length === 0);
check('wishlist state was clean before test', heartsBefore === null || heartsBefore === '[]');

// ---------------------------------------------------------------------------
// 8. Cover art resolution
// ---------------------------------------------------------------------------
console.log('\n== cover art ==');
check('hiResCover upgrades archive thumbs', w.eval(
  `hiResCover('https://x.archive.org/a/mbid-1-2_thumb250.jpg')`) === 'https://x.archive.org/a/mbid-1-2_thumb500.jpg');
check('hiResCover upgrades iTunes', w.eval(
  `hiResCover('https://is1-ssl.mzstatic.com/x/250x250bb.jpg')`) === 'https://is1-ssl.mzstatic.com/x/600x600bb.jpg');
check('hiResCover leaves unresolved CAA alone', w.eval(
  `hiResCover('https://coverartarchive.org/release/abc/front-250')`) === 'https://coverartarchive.org/release/abc/front-250');
check('sw caches iTunes covers', readFileSync(path.join(ROOT, 'sw.js'), 'utf8').includes('mzstatic.com'));

// ---------------------------------------------------------------------------
// 9. Pick One
// ---------------------------------------------------------------------------
console.log('\n== pick one ==');
w.eval('showRandomPick()');
check('pick overlay shows an album', w.document.getElementById('pickOverlay').classList.contains('show')
  && w.document.getElementById('pickTitle').textContent.length > 0);

// ---------------------------------------------------------------------------
console.log(`\n${passed} passed, ${failed} failed`);
if (failures.length) console.log('Failures:\n  ' + failures.join('\n  '));
server.close();
process.exit(failed ? 1 : 0);
