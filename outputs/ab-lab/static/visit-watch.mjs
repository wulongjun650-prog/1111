const KEY = 'ab-log-watch';

export function clampThreshold(value) {
  const number = Math.round(Number(value));
  if (!Number.isFinite(number) || number < 1) return 20;
  return Math.min(10000, number);
}

export function clampRefresh(value) {
  const number = Math.round(Number(value));
  if (!Number.isFinite(number) || number <= 0) return 0;
  return Math.min(300, Math.max(5, number));
}

export function alarmDecision(enabled, silenced, count, threshold) {
  if (!enabled || count < threshold) return {play: false, silenced: false};
  if (silenced) return {play: false, silenced: true};
  return {play: true, silenced: false};
}

export function createVisitWatch({api, getSite, getDomain, isLogsOpen, reloadLogs, storage = localStorage}) {
  const enabled = document.querySelector('#watch-enabled');
  const threshold = document.querySelector('#watch-threshold');
  const refresh = document.querySelector('#log-refresh');
  const countLabel = document.querySelector('#watch-count');
  const alarm = document.querySelector('#watch-alarm');
  const alarmText = document.querySelector('#watch-alarm-text');
  const silence = document.querySelector('#watch-silence');
  let settings = read(storage);
  let silencedSite = '';
  let alarmSite = '';
  let rateTimer = 0;
  let refreshTimer = 0;
  let rateFlight = false;
  let refreshFlight = false;
  const audio = createAlarm();

  function paint() {
    if (!enabled) return;
    enabled.checked = settings.enabled;
    threshold.value = String(settings.threshold);
    refresh.value = settings.refresh ? String(settings.refresh) : '';
    if (!settings.enabled) {
      countLabel.textContent = '';
      alarm.hidden = true;
      audio.stop();
    }
  }

  function save() {
    storage.setItem(KEY, JSON.stringify(settings));
  }

  function schedule() {
    clearInterval(rateTimer);
    clearInterval(refreshTimer);
    rateTimer = refreshTimer = 0;
    paint();
    if (settings.enabled) {
      checkRate();
      rateTimer = setInterval(checkRate, 10000);
    }
    if (settings.refresh >= 5) refreshTimer = setInterval(refreshList, settings.refresh * 1000);
  }

  async function checkRate() {
    const site = getSite();
    if (alarmSite && alarmSite !== site) {
      alarmSite = '';
      alarm.hidden = true;
      audio.stop();
    }
    if (document.hidden || !settings.enabled || !site || rateFlight) return;
    rateFlight = true;
    try {
      const data = await api('/api/logs/rate');
      if (site !== getSite() || !settings.enabled) return;
      const count = Number(data.count) || 0;
      countLabel.textContent = `最近 1 分钟 ${count} 次`;
      const decision = alarmDecision(true, silencedSite === site, count, settings.threshold);
      if (!decision.silenced) silencedSite = '';
      if (decision.play) {
        alarmSite = site;
        alarm.hidden = false;
        alarmText.textContent = `${getDomain() || '当前域名'} 1 分钟 ${count} 次，超过 ${settings.threshold}`;
        audio.start();
      } else if (!decision.silenced) {
        alarmSite = '';
        alarm.hidden = true;
        audio.stop();
      }
    } catch {
      /* The next check retries. A missed count must not interrupt the page. */
    } finally {
      rateFlight = false;
    }
  }

  async function refreshList() {
    if (document.hidden || !isLogsOpen() || !getSite() || refreshFlight) return;
    refreshFlight = true;
    try { await reloadLogs(); }
    catch { /* The log loader already explains a failed refresh. */ }
    finally { refreshFlight = false; }
  }

  enabled?.addEventListener('change', () => {
    settings.enabled = enabled.checked;
    if (settings.enabled) audio.unlock();
    else { silencedSite = ''; alarmSite = ''; }
    save();
    schedule();
  });
  threshold?.addEventListener('change', () => {
    settings.threshold = clampThreshold(threshold.value);
    save();
    schedule();
  });
  refresh?.addEventListener('change', () => {
    settings.refresh = clampRefresh(refresh.value);
    save();
    schedule();
  });
  silence?.addEventListener('click', () => {
    silencedSite = getSite() || '';
    alarmSite = '';
    alarm.hidden = true;
    audio.stop();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      checkRate();
      refreshList();
    }
  });

  schedule();
  return {checkRate, refreshList};
}

function read(storage) {
  try {
    const saved = JSON.parse(storage.getItem(KEY) || '{}');
    return {enabled: Boolean(saved.enabled), threshold: clampThreshold(saved.threshold ?? 20), refresh: clampRefresh(saved.refresh ?? 0)};
  } catch {
    return {enabled: false, threshold: 20, refresh: 0};
  }
}

function createAlarm() {
  let context = null;
  let timer = 0;
  function contextOrNull() {
    const Factory = window.AudioContext || window.webkitAudioContext;
    if (!Factory) return null;
    context = context || new Factory();
    return context;
  }
  return {
    unlock() {
      const current = contextOrNull();
      if (current?.state === 'suspended') current.resume();
    },
    start() {
      const current = contextOrNull();
      if (!current || timer) return;
      if (current.state === 'suspended') current.resume();
      const beep = () => {
        const tone = current.createOscillator();
        const gain = current.createGain();
        tone.type = 'square';
        tone.frequency.value = 880;
        gain.gain.setValueAtTime(0.0001, current.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.05, current.currentTime + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, current.currentTime + 0.28);
        tone.connect(gain).connect(current.destination);
        tone.start();
        tone.stop(current.currentTime + 0.3);
      };
      beep();
      timer = window.setInterval(beep, 700);
    },
    stop() {
      clearInterval(timer);
      timer = 0;
    },
  };
}
