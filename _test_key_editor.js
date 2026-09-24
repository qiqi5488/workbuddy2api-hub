/* Drive the dashboard's per-key editor controls in Node.
 *
 * Covers the two restrictions an operator sets per key, both of which fail
 * silently if the panel gets them wrong:
 *
 *   - the model allowlist: a restriction that fails to render as checked
 *     widens the key on the next save, and a model stored on the key but
 *     absent from the exit on screen must not be dropped from the row;
 *   - the validity deadline: a deadline that renders in the wrong timezone,
 *     or that is read back as 0, either expires a key early or grants it
 *     unlimited life.
 *
 * The dashboard itself is loaded from the file next to this test, so the
 * assertions run against the shipped code, not a copy of it.
 *
 * Requires node (no other dependency); the rest of the suite is Python only.
 *
 *   node _test_key_editor.js
 */
const path = require('path');
const fs = require('fs');
const html = fs.readFileSync(path.join(__dirname, 'dashboard.html'), 'utf8');
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = blocks.join('\n');

/* A minimal DOM good enough for the picker: getElementById plus the two
   querySelectorAll shapes the picker uses (a container's checkboxes, and a
   checkbox's enclosing <label>). */
function mkEl(id){
  return {
    id, innerHTML: '', textContent: '', value: '', checked: false,
    attrs: {},
    classList: { add(){}, remove(){}, contains(){ return false; } },
    style: {}, children: [], _boxes: [],
    focus(){}, blur(){}, click(){},
    appendChild(c){ this.children.push(c); },
    querySelectorAll(sel){ return sel && sel.indexOf('checkbox') !== -1 ? this._boxes : []; },
    querySelector(){ return null; },
    closest(){ return this._label || null; },
    addEventListener(){}, setAttribute(){}, getAttribute(n){ return this.attrs[n] || ''; },
    insertAdjacentHTML(){}, removeChild(){}, remove(){},
  };
}
const els = {};
/* Ids the test declares absent. The real getElementById returns null for an
   element that is not in the document - the editor for a row that is not open,
   or a card that was just re-rendered - so the harness has to be able to say
   that too, rather than conjuring an empty element on every lookup. */
const detached = new Set();
global.document = {
  getElementById: (id) => {
    if(!id || detached.has(id)) return null;
    return (els[id] = els[id] || mkEl(id));
  },
  querySelectorAll: () => [], querySelector: () => null,
  addEventListener: () => {}, createElement: (t) => mkEl(t + Math.random()),
  body: mkEl('body'), head: mkEl('head'), documentElement: mkEl('html'),
};
global.window = { addEventListener(){}, location:{ href:'' }, matchMedia: () => ({ matches:false, addEventListener(){} }) };
global.localStorage = { getItem(){return null;}, setItem(){}, removeItem(){} };
global.sessionStorage = global.localStorage;
global.fetch = () => Promise.resolve({ ok:true, status:200, json: () => Promise.resolve({}) });
global.navigator = { userAgent: 'node' };
global.setInterval = () => 0; global.clearInterval = () => {};
global.setTimeout = () => 0; global.clearTimeout = () => {};
global.location = { href: '', search: '', hash: '' };
global.alert = () => {}; global.confirm = () => false;

let api;
try {
  api = new Function(code + `
    ; return {
        modelChoicesHtml,
        readKeyModels,
        onKeyModelToggle,
        toggleAllKeyModels,
        toLocalInput,
        fromLocalInput,
        setKeyExpiry,
        setKeyExpiryIn,
        readKeyExpiry,
        expiryText,
        keyIsExpired,
        expiryBadge,
        fmtTokens,
        tokenBadge,
        readKeyTokenLimit,
        setRows: (r) => { API_KEY_ROWS = r; },
        setModels: (ids) => { MODELS_DATA = ids.map(id => ({ id })); },
      };`)();
} catch (e) {
  console.log('LOAD ERROR:', e.message);
  process.exit(1);
}

let PASS = 0, FAIL = 0;
const check = (label, ok, extra) => {
  if(ok){ PASS++; console.log('  [PASS] ' + label); }
  else { FAIL++; console.log('  [FAIL] ' + label + (extra !== undefined ? '  ' + JSON.stringify(extra) : '')); }
};

/* Parse the chips back out of the rendered HTML and wire each checkbox into
   the fake DOM, mirroring what the browser would hand the handlers. */
function mount(index, htmlText){
  const box = els['editKeyModels_' + index] || (els['editKeyModels_' + index] = mkEl('editKeyModels_' + index));
  box.innerHTML = htmlText;
  box._boxes = [];
  const re = /<input type="checkbox" data-model="([^"]*)"( checked)?[^>]*>/g;
  let m;
  while((m = re.exec(htmlText))){
    const cb = mkEl('cb');
    cb.checked = !!m[2];
    cb.attrs['data-model'] = m[1];
    const label = mkEl('label');
    label.style = {};
    cb._label = label;
    box._boxes.push(cb);
  }
  return box;
}

const idsOf = (htmlText) => [...htmlText.matchAll(/data-model="([^"]*)"/g)].map(m => m[1]);
const checkedIn = (htmlText) => [...htmlText.matchAll(/data-model="([^"]*)" checked/g)].map(m => m[1]);

console.log('[1] the picker offers the models of the exit on screen');
api.setModels(['deepseek-v4.1-flash', 'glm-5.3', 'gpt-6-astra']);
let out = api.modelChoicesHtml(0, { models: [] });
check('every loaded model is offered in order',
      idsOf(out).join(',') === 'deepseek-v4.1-flash,glm-5.3,gpt-6-astra', idsOf(out));
check('an unrestricted key renders nothing checked', checkedIn(out).length === 0,
      checkedIn(out));

console.log();
console.log('[2] a stored restriction renders as checked');
out = api.modelChoicesHtml(0, { models: ['glm-5.3'] });
check('the stored model is checked', checkedIn(out).join(',') === 'glm-5.3', checkedIn(out));
out = api.modelChoicesHtml(0, { models: ['GLM-5.3'] });
check('matching ignores case, so the box is still ticked',
      checkedIn(out).join(',') === 'glm-5.3', checkedIn(out));

console.log();
console.log('[3] a model stored on the key but absent from this exit survives');
api.setModels(['deepseek-v4.1-flash']);
out = api.modelChoicesHtml(0, { models: ['deepseek-v4.1-flash', 'gpt-6-astra'] });
check('the other exit\'s model is still rendered',
      idsOf(out).join(',') === 'deepseek-v4.1-flash,gpt-6-astra', idsOf(out));
check('and is marked as belonging to the other exit', out.indexOf('(另一出口)') !== -1, out);
check('and stays checked', checkedIn(out).join(',') === 'deepseek-v4.1-flash,gpt-6-astra',
      checkedIn(out));

console.log();
console.log('[4] readKeyModels returns the ticked boxes, not the stored list');
api.setModels(['a', 'b', 'c']);
api.setRows([{ models: ['a'] }]);
mount(0, api.modelChoicesHtml(0, { models: ['a'] }));
check('nothing ticked yet beyond the stored one',
      api.readKeyModels(0).join(',') === 'a', api.readKeyModels(0));
els['editKeyModels_0']._boxes[2].checked = true;
check('a newly ticked box is read back',
      api.readKeyModels(0).join(',') === 'a,c', api.readKeyModels(0));
els['editKeyModels_0']._boxes[0].checked = false;
check('an unticked box is dropped', api.readKeyModels(0).join(',') === 'c',
      api.readKeyModels(0));

console.log();
console.log('[5] the container is only trusted when it is actually on the page');
// saveSingleKey reads the picker for a row that was just opened; a missing
// container must fall back to the row's stored list rather than clear it.
api.setRows([{ models: ['keep-me'] }]);
delete els['editKeyModels_0'];
detached.add('editKeyModels_0');
api.setModels(['a']);
check('a missing picker falls back to the stored list',
      api.readKeyModels(0).join(',') === 'keep-me', api.readKeyModels(0));

console.log();
console.log('[6] 全选 / 清空 drive every box');
detached.clear();
api.setModels(['a', 'b', 'c']);
mount(0, api.modelChoicesHtml(0, { models: [] }));
check('nothing ticked before', api.readKeyModels(0).length === 0, api.readKeyModels(0));
api.toggleAllKeyModels(0, true);
check('全选 ticks every box', api.readKeyModels(0).join(',') === 'a,b,c',
      api.readKeyModels(0));
api.toggleAllKeyModels(0, false);
check('清空 clears every box', api.readKeyModels(0).length === 0, api.readKeyModels(0));

console.log();
console.log('[7] an empty catalog says so instead of silently allowing everything');
api.setModels([]);
out = api.modelChoicesHtml(0, { models: [] });
check('the empty state explains the fallback', out.indexOf('留空表示不限制') !== -1, out);
check('no checkboxes are drawn', idsOf(out).length === 0, idsOf(out));
api.setRows([{ models: ['stored'] }]);
check('a stored model still renders even with nothing loaded',
      idsOf(api.modelChoicesHtml(0, { models: ['stored'] })).join(',') === 'stored',
      api.modelChoicesHtml(0, { models: ['stored'] }));

console.log();
console.log('[8] the expiry field round-trips local time, not UTC');

// A datetime-local value is read as the operator's wall clock. Round-tripping
// has to land on the same instant whatever the machine's zone is, or a key
// expires hours early or late.
const stampSec = 1893456000; // 2030-01-01T00:00:00Z
const local = api.toLocalInput(stampSec);
check('a deadline renders as a datetime-local value',
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(local), local);
check('and reads back to the same instant', api.fromLocalInput(local) === stampSec,
      [local, api.fromLocalInput(local)]);
check('the rendered value is the local wall clock, not UTC',
      local.slice(0, 10) === new Date(stampSec * 1000).toLocaleDateString('en-CA'),
      [local, new Date(stampSec * 1000).toLocaleDateString('en-CA')]);
check('no deadline renders as empty (the 永久有效 state)', api.toLocalInput(0) === '');
check('an empty field reads back as "never"', api.fromLocalInput('') === 0);
check('a garbage field reads back as "never", not as "now"',
      api.fromLocalInput('not-a-date') === 0, api.fromLocalInput('not-a-date'));

console.log();
console.log('[9] the editor writes and reads the field');

detached.clear();
api.setRows([{ expires_at: 0 }]);
api.setKeyExpiry(0, stampSec);
check('setting a deadline fills the input',
      els['editKeyExpiry_0'].value === api.toLocalInput(stampSec), els['editKeyExpiry_0'].value);
check('and reads back as the same instant', api.readKeyExpiry(0) === stampSec,
      api.readKeyExpiry(0));
api.setKeyExpiry(0, 0);
check('the 永久 button clears the input', els['editKeyExpiry_0'].value === '');
check('which reads back as "never"', api.readKeyExpiry(0) === 0, api.readKeyExpiry(0));

// The quick presets must land on the requested number of days from now.
// datetime-local carries minutes, not seconds, so the round trip through the
// input intentionally shaves off up to 59s - hence the 60s tolerance.
const before = Math.floor(Date.now() / 1000);
api.setKeyExpiryIn(0, 7);
const got = api.readKeyExpiry(0);
check('the 7-day preset is 7 days out',
      Math.abs(got - (before + 7 * 86400)) <= 60, [got - before]);
api.setKeyExpiryIn(0, 1);
check('the 1-day preset is 1 day out',
      Math.abs(api.readKeyExpiry(0) - (before + 86400)) <= 60,
      api.readKeyExpiry(0) - before);

// A row opened from storage with a deadline must show it, not blank it.
api.setRows([{ expires_at: stampSec }]);
delete els['editKeyExpiry_1'];
api.setKeyExpiry(1, stampSec);
check('an existing deadline is shown when the row is opened',
      els['editKeyExpiry_1'].value === api.toLocalInput(stampSec),
      els['editKeyExpiry_1'].value);
// With no input on the page, the read must fall back to the stored value
// rather than report 0 - that would silently make the key permanent.
detached.add('editKeyExpiry_3');
api.setRows([{}, {}, {}, { expires_at: stampSec }]);
check('a missing input falls back to the stored deadline',
      api.readKeyExpiry(3) === stampSec, api.readKeyExpiry(3));
api.setRows([{}, {}, {}, { expires_at: 0 }]);
check('a missing input on a permanent key stays "never"',
      api.readKeyExpiry(3) === 0, api.readKeyExpiry(3));

console.log();
console.log('[10] an expired key is shown as such, and never counted as live');

const nowSec = Math.floor(Date.now() / 1000);
check('a past deadline is expired', api.keyIsExpired({ expires_at: nowSec - 60 }));
check('the exact deadline is expired', api.keyIsExpired({ expires_at: nowSec }));
check('a future deadline is not', !api.keyIsExpired({ expires_at: nowSec + 3600 }));
check('no deadline never expires', !api.keyIsExpired({ expires_at: 0 }));
check('a missing field never expires', !api.keyIsExpired({}));
check('the server verdict wins even if the clocks disagree',
      api.keyIsExpired({ expires_at: nowSec + 86400, expired: true }));
check('null rows are safe', !api.keyIsExpired(null));

check('an expired key gets the red 已过期 badge',
      api.expiryBadge({ expires_at: nowSec - 60 }).indexOf('已过期') !== -1,
      api.expiryBadge({ expires_at: nowSec - 60 }));
check('a live key shows when it lapses',
      api.expiryBadge({ expires_at: nowSec + 86400 }).indexOf('有效至') !== -1,
      api.expiryBadge({ expires_at: nowSec + 86400 }));
check('a key with no deadline reads 永久有效',
      api.expiryBadge({ expires_at: 0 }).indexOf('永久有效') !== -1,
      api.expiryBadge({ expires_at: 0 }));
check('the badge escapes its content',
      api.expiryBadge({ expires_at: 0 }).indexOf('<script') === -1);

check('the human text names the moment', /^\d{4}\/\d{2}\/\d{2} \d{2}:\d{2}$/.test(
        api.expiryText(stampSec)), api.expiryText(stampSec));
check('no deadline has no text', api.expiryText(0) === '');

console.log();
console.log('[11] the token limit badge reads the server\'s used count');

check('a key with no limit and no usage reads 不限',
      api.tokenBadge({ token_limit: 0, token_used: 0 }).indexOf('Token 不限') !== -1,
      api.tokenBadge({ token_limit: 0, token_used: 0 }));
check('a key with no limit but some usage still shows the used count',
      api.tokenBadge({ token_limit: 0, token_used: 12345 }).indexOf('Token 已用') !== -1,
      api.tokenBadge({ token_limit: 0, token_used: 12345 }));
check('a key under its cap shows used/limit without the exhausted mark',
      (api.tokenBadge({ token_limit: 500, token_used: 400 }).indexOf('400') !== -1)
      && api.tokenBadge({ token_limit: 500, token_used: 400 }).indexOf('已用尽') === -1,
      api.tokenBadge({ token_limit: 500, token_used: 400 }));
check('a key at its cap is flagged exhausted',
      api.tokenBadge({ token_limit: 500, token_used: 500 }).indexOf('已用尽') !== -1,
      api.tokenBadge({ token_limit: 500, token_used: 500 }));
check('a key over its cap is flagged exhausted too',
      api.tokenBadge({ token_limit: 500, token_used: 600 }).indexOf('已用尽') !== -1,
      api.tokenBadge({ token_limit: 500, token_used: 600 }));

check('token numbers are abbreviated for display',
      api.fmtTokens(1234).indexOf('k') !== -1 && api.fmtTokens(1500000).indexOf('M') !== -1,
      [api.fmtTokens(1234), api.fmtTokens(1500000)]);
check('small token counts stay exact', api.fmtTokens(42) === '42', api.fmtTokens(42));

console.log();
console.log('[12] the token limit input reads a non-negative integer');

detached.clear();
api.setRows([{ token_limit: 0 }]);
els['editKeyTokenLimit_0'] = mkEl('editKeyTokenLimit_0');
els['editKeyTokenLimit_0'].value = '123456';
check('a typed limit is read back', api.readKeyTokenLimit(0) === 123456,
      api.readKeyTokenLimit(0));
els['editKeyTokenLimit_0'].value = '';
check('an empty field means unlimited', api.readKeyTokenLimit(0) === 0,
      api.readKeyTokenLimit(0));
els['editKeyTokenLimit_0'].value = 'garbage';
check('a non-numeric field means unlimited', api.readKeyTokenLimit(0) === 0,
      api.readKeyTokenLimit(0));
els['editKeyTokenLimit_0'].value = '-5';
check('a negative field means unlimited', api.readKeyTokenLimit(0) === 0,
      api.readKeyTokenLimit(0));

api.setRows([{ token_limit: 999 }]);
detached.add('editKeyTokenLimit_0');
check('a missing input falls back to the stored limit',
      api.readKeyTokenLimit(0) === 999, api.readKeyTokenLimit(0));
api.setRows([{ token_limit: 0 }]);
check('a missing input on an unlimited key stays 0',
      api.readKeyTokenLimit(0) === 0, api.readKeyTokenLimit(0));

console.log();
console.log('PASS=%d FAIL=%d', PASS, FAIL);
process.exit(FAIL ? 1 : 0);
