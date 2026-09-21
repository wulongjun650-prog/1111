import { searchOptions, normalizeSelection, options } from './locale.mjs';

export function createSelection(input, kind) {
  const wrapper = document.createElement('div'); wrapper.className = 'search-select';
  // Keep original named input as the canonical form value.
  input.type = 'hidden'; input.after(wrapper);
  const tags = document.createElement('div'); tags.className = 'selection-tags';
  const search = document.createElement('input'); search.type = 'text';
  search.placeholder = kind === 'countries' ? '搜索国家，如 韩国、美国、KR' : '搜索语言，如 韩语、中文、ko';
  search.setAttribute('aria-label', kind === 'countries' ? '搜索允许的国家或地区' : '搜索允许的语言');
  search.autocomplete = 'off';
  const results = document.createElement('div'); results.className = 'selection-results'; results.hidden = true;
  const message = document.createElement('small'); message.setAttribute('role','status');
  wrapper.append(tags,search,results,message);
  let selected = [];
  function commit() {
    input.value = selected.join(', '); renderTags(); input.dispatchEvent(new Event('input',{bubbles:true}));
  }
  function renderTags() {
    tags.replaceChildren();
    for (const code of selected) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'selection-tag';
      const name = options[kind].find(item => item.code === code)?.name || code;
      button.textContent = `${name} ${code} ×`; button.setAttribute('aria-label', `移除 ${name}`);
      button.addEventListener('click', () => { selected = selected.filter(item => item !== code); commit(); });
      tags.append(button);
    }
  }
  function choose(code) {
    selected = [...new Set([...selected,code])]; search.value = ''; results.hidden = true; message.textContent = ''; commit(); search.focus();
  }
  function suggest() {
    results.replaceChildren(); message.textContent = '';
    if (!search.value.trim()) { results.hidden = true; return; }
    const found = searchOptions(kind,search.value).filter(item => !selected.includes(item.code));
    for (const item of found) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = `${item.name} · ${item.code}`;
      button.addEventListener('click', () => choose(item.code)); results.append(button);
    }
    results.hidden = !found.length;
    if (!found.length) message.textContent = '没有匹配项，支持中文名称和标准代码。';
  }
  search.addEventListener('input', () => { suggest(); input.dispatchEvent(new Event('input',{bubbles:true})); });
  search.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'Escape') { results.hidden = true; }
    if (event.key === 'ArrowDown') { const first = results.querySelector('button'); if (first) { event.preventDefault(); first.focus(); } }
    if (event.key === 'Enter') {
      event.preventDefault();
      try { const codes = normalizeSelection(kind,search.value); codes.forEach(choose); }
      catch (error) { message.textContent = error.message; }
    }
  });
  return {
    set(values) { selected = [...values]; search.value = ''; message.textContent = ''; results.hidden = true; input.value = selected.join(', '); renderTags(); },
    flush() { if (search.value.trim()) normalizeSelection(kind,search.value).forEach(choose); },
  };
}
