export function createTracking({api, on, element, confirmAction, toast, getState, refresh}) {
  const current = document.querySelector('#tracking-current');
  const lists = {ga4: document.querySelector('#tracking-ga4-list'), conversion: document.querySelector('#tracking-conversion-list')};
  let request = 0;
  let data = null;

  function line(slot, info) {
    if (!data) return `${slot} 正在发布：读取中`;
    const ga4 = info?.ga4 ? `GA4 ${info.ga4}` : '还没有 GA4';
    const conversion = info?.conversion ? `转化 ${info.conversion}` : '还没有转化代码';
    return `${slot} 正在发布：${ga4}，${conversion}`;
  }

  function render() {
    if (!current) return;
    current.replaceChildren();
    for (const slot of ['A', 'B']) {
      current.append(element('p', '', line(slot, data?.published?.[slot])));
    }
    for (const kind of ['ga4', 'conversion']) {
      const root = lists[kind];
      root.replaceChildren();
      const items = (data?.snippets || []).filter(item => item.kind === kind);
      if (!items.length) root.append(element('p', 'empty-state', '还没有保存。'));
      for (const item of items) {
        const row = element('div', 'tracking-row');
        const title = element('strong', '', `${item.note ? item.note + ' · ' : ''}${item.label}`);
        const actions = element('div', 'actions');
        for (const slot of ['A', 'B']) {
          const button = element('button', 'button primary small', `写入 ${slot}`);
          button.type = 'button';
          button.disabled = !data?.published?.[slot]?.version_id;
          on(button, 'click', () => apply(item, slot, button));
          actions.append(button);
        }
        const remove = element('button', 'button quiet small', '删除');
        remove.type = 'button';
        on(remove, 'click', () => removeSnippet(item));
        actions.append(remove);
        row.append(title, actions);
        root.append(row);
      }
    }
  }

  async function load() {
    if (!getState()) return;
    const mine = ++request;
    const next = await api('/api/tracking');
    if (mine !== request) return;
    data = next;
    render();
  }

  async function apply(item, slot, button) {
    const published = data?.published?.[slot]?.version_id;
    if (!published) throw new Error(`${slot} 还没有发布页面。`);
    button.disabled = true;
    try {
      const body = {slot, expected_published: published};
      body[item.kind === 'ga4' ? 'ga4_id' : 'conversion_id'] = item.id;
      const result = await api('/api/tracking/apply', {method: 'POST', body});
      const cache = result.cloudflare?.ok ? ' Cloudflare 缓存已清除。' : result.cloudflare ? ' 代码已写入，但 Cloudflare 缓存没清掉。' : '';
      toast(`已写入 ${slot} 并发布。${cache}`, Boolean(result.cloudflare && !result.cloudflare.ok));
      await refresh();
      await load();
    } finally {
      button.disabled = false;
    }
  }

  async function removeSnippet(item) {
    if (!await confirmAction('删掉这条保存的代码？已经写进页面的不会变。')) return;
    await api(`/api/tracking/${item.id}`, {method: 'DELETE'});
    toast('已从保存列表删除。');
    await load();
  }

  function bind(form, kind) {
    on(form, 'submit', async event => {
      event.preventDefault();
      const body = form.elements.body.value;
      const note = form.elements.note.value.trim();
      await api('/api/tracking', {method: 'POST', body: {kind, body, note}});
      form.elements.body.value = '';
      form.elements.note.value = '';
      toast(kind === 'ga4' ? 'GA4 已保存。' : '转化代码已保存。');
      await load();
    });
  }

  bind(document.querySelector('#tracking-ga4-form'), 'ga4');
  bind(document.querySelector('#tracking-conversion-form'), 'conversion');
  render();
  return {load};
}
