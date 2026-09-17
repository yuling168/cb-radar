/* Shared, cache-aware reader for the versioned Dashboard data shards. */
(function (global) {
  "use strict";
  const root = "./data/v2";
  const cache = new Map();

  function read(path) {
    if (!cache.has(path)) {
      cache.set(path, fetch(`${root}/${path}`, { cache: "no-store" }).then(response => {
        if (!response.ok) throw new Error(`HTTP ${response.status}: ${path}`);
        return response.json();
      }).then(value => {
        if (value.schema_version !== 2) throw new Error(`不支援的資料版本：${path}`);
        return value;
      }));
    }
    return cache.get(path);
  }

  async function manifest() { return read("manifest.json"); }
  async function latest() { return read("latest.json"); }
  async function month(kind, tradeDate) {
    const index = await manifest();
    const months = index[`${kind}_months`];
    const entry = months && months[String(tradeDate).slice(0, 7)];
    if (!entry || !entry.path) throw new Error(`沒有 ${tradeDate} 的${kind}資料分片`);
    return read(entry.path);
  }
  async function strategy(code) {
    const index = await manifest();
    const entry = index.strategies && index.strategies[String(code).toUpperCase()];
    if (!entry || !entry.path) throw new Error(`沒有策略 ${code} 資料分片`);
    return read(entry.path);
  }
  global.CBRadarData = { manifest, latest, month, strategy };
})(window);
