// Cosmetic only: called after the server confirms the current site's configuration.
const images = {
  angel: '/static/angel-v2.png',
  sleep: '/static/demon-rest-v02.png',
  demon: '/static/demon-v02.png',
};
let ready = false;
Promise.all(Object.values(images).map(src => new Promise(resolve => {
  const image = new Image();
  image.onload = () => resolve(true);
  image.onerror = () => resolve(false);
  image.src = src;
}))).then(results => { ready = results.every(Boolean); });
let cleanup;
export function applyTheme(next) {
  const root = document.documentElement;
  const previous = root.dataset.theme;
  if (previous === next) return;
  const overlay = document.getElementById('theme-transition');
  clearTimeout(cleanup);
  overlay.classList.remove('is-active');
  const animate = ready && images[previous] && images[next] && !matchMedia('(prefers-reduced-motion: reduce)').matches;
  root.classList.toggle('theme-morphing', Boolean(animate));
  root.dataset.theme = next;
  // First load / site loading / reduced motion never play a transformation.
  if (!animate) return;
  overlay.dataset.target = next;
  overlay.querySelector('.transition-from').src = images[previous];
  overlay.querySelector('.transition-art').src = images[next];
  // Restart safely if another confirmed update arrives during a transition.
  void overlay.offsetWidth;
  overlay.classList.add('is-active');
  cleanup = setTimeout(() => { overlay.classList.remove('is-active'); root.classList.remove('theme-morphing'); }, 4200);
}
