#!/usr/bin/env node
/**
 * verify_gone.cjs — 失蹤幣驗屍（查 DexScreener 現價，實測「斷訊=死亡?」）
 * 用法: node verify_gone.cjs [csv路徑] [抽樣數=60]
 * 對象: 帶64-89訊號、掛號通過、之後從資料中消失的幣
 * 輸出: 失蹤者現況分佈 → 用來校準 樂觀/悲觀 兩版之間的真實死亡率
 */
const fs = require('fs');
const CSV = process.argv[2] || './data/research.csv';
const SAMPLE = +(process.argv[3] || 60);
const TURN_MAX = 10, LIQ_MIN = 15000, REG_HOURS = 2, REG_VOID = 12, REG_FLOOR = 0.55, GONE_GAP_H = 6;
const H = 3600e3;

const rows = fs.readFileSync(CSV, 'utf8').trim().split('\n');
const head = rows[0].split(',');
const idx = Object.fromEntries(head.map((h, i) => [h, i]));
const tokens = new Map();
for (let i = 1; i < rows.length; i++) {
  const c = rows[i].split(',');
  if (c.length < head.length) continue;
  const r = { t: Date.parse(c[idx.time]), price: +c[idx.price], score: +c[idx.score], turn: +c[idx.turnover], liq: +c[idx.liq], sym: c[idx.symbol] };
  if (!isFinite(r.t) || !(r.price > 0)) continue;
  const a = c[idx.addr];
  if (!tokens.has(a)) tokens.set(a, []);
  tokens.get(a).push(r);
}
for (const arr of tokens.values()) arr.sort((x, y) => x.t - y.t);

// 找出基準策略下「進場後斷訊」的幣
const gone = [];
for (const [addr, arr] of tokens) {
  const i0 = arr.findIndex(r => r.score >= 64 && r.score <= 89 && r.turn < TURN_MAX && r.liq >= LIQ_MIN);
  if (i0 < 0) continue;
  const sig = arr[i0];
  let ei = -1;
  for (let j = i0 + 1; j < arr.length; j++) {
    const dt = (arr[j].t - sig.t) / H;
    if (dt > REG_VOID) { ei = -2; break; }
    if (dt >= REG_HOURS) { ei = arr[j].price > sig.price * REG_FLOOR ? j : -2; break; }
  }
  if (ei < 0) continue;
  const entry = arr[ei];
  let exited = false, lastP = entry.price;
  for (let k = ei + 1; k < arr.length; k++) {
    if ((arr[k].t - arr[k - 1].t) / H > GONE_GAP_H) { lastP = arr[k - 1].price; break; }
    const ret = arr[k].price / entry.price - 1;
    lastP = arr[k].price;
    if (ret <= -0.35 || ret >= 0.8) { exited = true; break; }
  }
  if (!exited) gone.push({ addr, sym: entry.sym || arr[0].sym, entryP: entry.price, lastP });
}
console.log(`基準策略斷訊幣共 ${gone.length} 隻，抽樣 ${Math.min(SAMPLE, gone.length)} 隻查現價...\n`);
const pick = gone.slice(0, SAMPLE);

const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const buckets = { 'API查無(下架=死亡)': 0, '≤-90%': 0, '-70~-90%': 0, '-35~-70%': 0, '>-35%(存活)': 0 };
  const survivors = [];
  for (const g of pick) {
    try {
      const res = await fetch(`https://api.dexscreener.com/latest/dex/tokens/${g.addr}`);
      const j = await res.json();
      const pairs = (j && j.pairs) || [];
      if (!pairs.length) { buckets['API查無(下架=死亡)']++; }
      else {
        const now = Math.max(...pairs.map(p => +p.priceUsd || 0));
        const ret = now / g.entryP - 1;
        if (ret <= -0.9) buckets['≤-90%']++;
        else if (ret <= -0.7) buckets['-70~-90%']++;
        else if (ret <= -0.35) buckets['-35~-70%']++;
        else { buckets['>-35%(存活)']++; survivors.push(`${g.sym} ${(ret * 100).toFixed(0)}%`); }
      }
    } catch (e) { buckets['API查無(下架=死亡)']++; }
    await sleep(350); // 守 DexScreener 限流
  }
  const n = pick.length;
  console.log('═══ 失蹤者驗屍報告 ═══');
  for (const [k, v] of Object.entries(buckets))
    console.log(`  ${k.padEnd(18)} ${String(v).padStart(3)} 隻  (${(v / n * 100).toFixed(1)}%)`);
  const dead = buckets['API查無(下架=死亡)'] + buckets['≤-90%'] + buckets['-70~-90%'];
  console.log(`\n  實測失蹤死亡率: ${(dead / n * 100).toFixed(1)}%  (悲觀版假設=100%, 樂觀版≈0%)`);
  if (survivors.length) console.log(`  存活者: ${survivors.join(', ')}`);
  console.log('\n  用法: 把死亡率D代入 → 真實期望 ≈ 悲觀版×D + 樂觀版×(1-D) 之間再按機器人停損上修');
})();
