document.querySelector('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button');
  const error = document.querySelector('#login-error');
  button.disabled = true; error.textContent = '';
  try {
    const response = await fetch('/api/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content },
      body: JSON.stringify(Object.fromEntries(new FormData(form))), signal: AbortSignal.timeout(20000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '登录失败，请刷新重试');
    window.location.replace('/');
  } catch (failure) { error.textContent = failure.message || '连接失败，请重试'; }
  finally { button.disabled = false; }
});
