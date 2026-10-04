export function createSourceEditor({api, on, element, confirmAction, getSite, onSaved}) {
  let session = null;
  const dialog = element('dialog', 'source-editor'); dialog.id = 'source-editor';
  const header = element('header', 'source-editor-header');
  const heading = element('h2', '', '编辑源码'); heading.id = 'source-editor-title';
  const context = element('p', 'muted'); context.id = 'source-editor-context';
  const base = element('p', 'source-editor-base');
  header.append(heading, context, base);
  const toolbar = element('div', 'source-editor-toolbar');
  const fileLabel = element('label', '', '文件');
  const file = element('select'); file.id = 'source-editor-file';
  fileLabel.append(file);
  const currentPath = element('p', 'source-editor-path'); currentPath.id = 'source-editor-path';
  toolbar.append(fileLabel, currentPath);
  const contentLabel = element('label', 'sr-only', '文件源码'); contentLabel.htmlFor = 'source-editor-content';
  const content = element('textarea'); content.id = 'source-editor-content';
  content.spellcheck = false; content.wrap = 'off';
  content.setAttribute('autocapitalize', 'off'); content.setAttribute('autocorrect', 'off');
  content.setAttribute('aria-describedby', 'source-editor-path source-editor-hint');
  const errorMessage = element('p', 'source-editor-error'); errorMessage.id = 'source-editor-error';
  errorMessage.setAttribute('role', 'alert'); errorMessage.hidden = true;
  const footer = element('footer', 'source-editor-footer');
  const summary = element('div');
  const status = element('p'); status.id = 'source-editor-status'; status.setAttribute('role', 'status');
  const hint = element('small', '', '每次保存一个文件，立即发布为新版本；旧版本保留。Ctrl / ⌘ + S 保存。'); hint.id = 'source-editor-hint';
  summary.append(status, hint);
  const actions = element('div', 'actions');
  const cancel = element('button', 'button secondary', '取消'); cancel.type = 'button';
  const save = element('button', 'button primary', '保存并发布'); save.type = 'button';
  actions.append(cancel, save); footer.append(summary, actions);
  dialog.append(header, toolbar, contentLabel, content, errorMessage, footer);
  dialog.setAttribute('aria-labelledby', heading.id);
  document.body.append(dialog);

  const isCurrent = value => session === value && value?.site.id === getSite()?.id;
  const isDirty = () => Boolean(session?.path && content.value !== session.original);
  const endpoint = value => `/api/source/${value.version.slot}/${encodeURIComponent(value.version.id)}`;

  function showError(message = '') {
    errorMessage.textContent = message;
    errorMessage.hidden = !message;
  }

  function updateControls(message) {
    const loading = session?.loading || session?.saving;
    file.disabled = !session || loading || !file.options.length;
    content.readOnly = !session?.path || loading;
    save.disabled = !session || loading || !isDirty();
    cancel.disabled = Boolean(session?.saving);
    dialog.setAttribute('aria-busy', String(Boolean(loading)));
    if (message) status.textContent = message;
    else if (session?.saving) status.textContent = '正在保存并发布…';
    else if (session?.loading) status.textContent = '正在读取源码…';
    else status.textContent = isDirty() ? '有未保存的修改' : session?.path ? '已加载 · 修改后可保存并发布' : '请选择可编辑文件';
  }

  function clear() {
    session = null;
    dialog.close();
    content.value = ''; file.replaceChildren();
    context.textContent = ''; base.textContent = ''; currentPath.textContent = ''; status.textContent = '';
    showError(); updateControls();
  }

  async function allowDiscard(value) {
    if (isDirty() && !await confirmAction('当前文件有未保存的源码修改，确认放弃？')) return false;
    return isCurrent(value);
  }

  async function close() {
    const value = session;
    if (!value || value.saving || !await allowDiscard(value)) return;
    clear();
  }

  async function loadFile(value, nextPath) {
    value.loading = true; showError(); updateControls();
    try {
      const result = await api(`${endpoint(value)}?path=${encodeURIComponent(nextPath)}`);
      if (!isCurrent(value)) return;
      // The expected published version belongs to the original file list, never this later read.
      value.path = nextPath; value.original = result.content;
      content.value = result.content; file.value = nextPath;
      currentPath.textContent = nextPath;
    } catch (error) {
      if (isCurrent(value) && !error.stale) {
        file.value = value.path || nextPath;
        showError(`读取失败，当前草稿仍保留。${error.message}`);
      }
    } finally {
      if (isCurrent(value)) {value.loading = false; updateControls();}
    }
  }

  async function open(version) {
    if (session && !await allowDiscard(session)) return;
    clear();
    const site = getSite();
    if (!site) return;
    const value = {site, version, expectedPublished:null, path:null, original:'', loading:true, saving:false};
    session = value;
    context.textContent = `${site.domain || '当前站点'} · ${version.slot} 内容`;
    base.textContent = `基于版本：${version.name}`;
    updateControls(); dialog.showModal(); cancel.focus();
    try {
      const result = await api(endpoint(value));
      if (!isCurrent(value)) return;
      value.expectedPublished = result.published_version;
      for (const item of result.files) {
        const option = element('option', '', item.path); option.value = item.path; file.append(option);
      }
      if (!result.files.length) {value.loading = false; updateControls('此版本没有可编辑的 HTML、JS 或 CSS 文件。'); return;}
      const first = result.files.find(item => /^index\.html?$/i.test(item.path)) || result.files[0];
      file.value = first.path;
      await loadFile(value, first.path);
      if (isCurrent(value) && value.path) content.focus();
    } catch (error) {
      if (isCurrent(value) && !error.stale) {
        value.loading = false; updateControls('无法打开此版本');
        showError(`读取失败，请关闭后重新打开。${error.message}`);
      }
    }
  }

  async function publish() {
    const value = session;
    if (!value || value.loading || value.saving || !isDirty() || !isCurrent(value)) return;
    const draft = content.value;
    value.saving = true; showError(); updateControls();
    let published = false;
    try {
      const result = await api(endpoint(value), {method:'POST', body:{path:value.path, content:draft, expected_published:value.expectedPublished}});
      if (!isCurrent(value)) return;
      value.version = result.version;
      value.expectedPublished = result.published_version;
      value.original = draft;
      base.textContent = `基于版本：${result.version.name}`;
      published = true;
      try {await onSaved(result);}
      catch (error) {if (isCurrent(value) && !error.stale) showError('源码已保存并发布，但状态刷新失败。请关闭编辑器后刷新状态。');}
    } catch (error) {
      if (isCurrent(value) && !error.stale) showError(error.status === 409
        ? '当前发布版本已变化，未覆盖新版本。草稿仍保留；请复制修改，关闭并重新打开最新版本后再保存。'
        : `保存未确认，草稿仍保留。${error.message}`);
    } finally {
      if (isCurrent(value)) {value.saving = false; updateControls(published ? '已保存并发布 · 旧版本已保留' : undefined);}
    }
  }

  content.addEventListener('input', () => updateControls());
  dialog.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {event.preventDefault(); save.click();}
  });
  on(cancel, 'click', close);
  on(dialog, 'cancel', event => {event.preventDefault(); return close();});
  on(save, 'click', publish);
  on(file, 'change', async () => {
    const value = session, nextPath = file.value;
    if (!value || value.loading || value.saving) return;
    file.value = value.path || nextPath;
    if (!await allowDiscard(value)) return;
    await loadFile(value, nextPath);
  });
  updateControls();
  return {open, clear, isDirty};
}
