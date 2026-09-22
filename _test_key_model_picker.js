/* Drive the dashboard's per-key model picker in Node.
 *
 * The picker is the only place an operator can restrict a key's models, so its
 * two failure modes both matter: a stored restriction that fails to render as
 * checked silently widens the key on the next save, and a model offered by the
 * exit on screen but stored on the key must not be dropped from the row.
 *
 * The dashboard itself is loaded from the file next to this test, so the
 * assertions run against the shipped code, not a copy of it.
 *
 * Requires node (no other dependency); the rest of the suite is Python only.
 *
 *   node _test_key_model_picker.js
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
console.log('PASS=%d FAIL=%d', PASS, FAIL);
process.exit(FAIL ? 1 : 0);
