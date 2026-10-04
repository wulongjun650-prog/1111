export function reputationDue(result, now) {
  if (['unchecked','expired'].includes(result?.status)) return true;
  if (['clean','flagged'].includes(result?.status)) return !(result.expires_at > now);
  return result?.status === 'error' && !(result.checked_at > now - 60);
}

export function createReputationAutoCheck({check, onUpdate, isActive, now = () => Date.now() / 1000}) {
  let running = false;
  const attempts = new Map();
  return {
    async refresh(catalog) {
      if (running || !catalog?.google_reputation_configured || !isActive()) return;
      running = true;
      try {
        // One request at a time; repeated renders and tabs share the server cache.
        for (const site of catalog.sites) {
          if (!isActive()) break;
          const stamp = now();
          if (!reputationDue(site.google_reputation, stamp) || (attempts.has(site.id) && stamp - attempts.get(site.id) < 60)) continue;
          attempts.set(site.id, stamp);
          onUpdate(site.id, {checking:true});
          try {
            onUpdate(site.id, {result:await check(site.id)});
          } catch {
            onUpdate(site.id, {result:{status:'error',checked_at:now(),expires_at:null,threats:[],detail:'暂时无法完成检测，将自动重试'}});
          } finally {
            onUpdate(site.id, {checking:false});
          }
        }
      } finally {
        running = false;
      }
    },
  };
}
