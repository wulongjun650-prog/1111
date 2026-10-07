(function () {
  var gate = document.getElementById('observer-welcome');
  if (!gate) return;
  var key = 'ab-observer-entered';
  function entered() {
    try { return sessionStorage.getItem(key) === '1'; } catch (error) { return false; }
  }
  function mark() {
    try { sessionStorage.setItem(key, '1'); } catch (error) {}
  }
  if (entered()) {
    gate.remove();
    return;
  }
  document.body.classList.add('observer-gated');
  var button = document.getElementById('observer-enter');
  if (!button) return;
  button.addEventListener('click', function () {
    mark();
    document.body.classList.remove('observer-gated');
    gate.remove();
  });
})();
