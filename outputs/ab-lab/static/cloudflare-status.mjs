export function createCloudflareAutoCheck({check, onUpdate, isActive, now = () => Date.now() / 1000, interval = 20}) {
  let running = false;
  const attempts = new Map();
  return {
    async refresh(catalog) {
      if (running || !catalog?.cloudflare_configured || !isActive()) return;
      running = true;
      try {
        for (const site of catalog.sites) {
          if (!isActive()) break;
          if (site.cf_status !== 'pending' || !site.cf_zone_id) continue;
          const stamp = now();
          if (attempts.has(site.id) && stamp - attempts.get(site.id) < interval) continue;
          attempts.set(site.id, stamp);
          onUpdate(site.id, {checking: true});
          try {
            onUpdate(site.id, {result: await check(site.id)});
          } catch {
            onUpdate(site.id, {error: true});
          } finally {
            onUpdate(site.id, {checking: false});
          }
        }
      } finally {
        running = false;
      }
    },
  };
}
