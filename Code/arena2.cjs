#!/usr/bin/env node
/**
 * arena2.cjs — 進場變體擂台（讀 research.csv，唯讀離線，不碰交易引擎）
 * 用法: node arena2.cjs [csv路徑]   預設 ./data/research.csv
 * 考題: 現框架內四個未拉的槓桿 —— 收帶 / 蟬聯 / 行情濾網 / 分層倉位
 * 出場統一: 掛號2h + SL-35% + TP+80%（前輪重放冠軍），另附×2基準對照
 */
const fs = require('fs');

const CSV = process.argv[2] || './data/research.csv';
const STAKE = 0.05;
const TURN_MAX = 10, LIQ_MIN = 15000;
const REG_HOURS = 2, REG_VOID = 12, REG_FLOOR = 0.55;
const GONE_GAP_H = 6;
const H = 3600e3;

// ── 讀檔 ──
const rows = fs.readFileSync(CSV, 'utf8').trim().split('\n');
const head = rows[0].split(',');
const idx = Object.fromEntries(head.map((h, i) => [h, i]));
const tokens = new Map();
for (let i = 1; i < rows.length; i++) {
  const c = rows[i].split(',');
  if (c.length < head.length) continue;
  const rec = { t: Date.parse(c[idx.time]), price: +c[idx.price], score: +c[idx.score], turn: +c[idx.turnover], liq: +c[idx.liq] };
  if (!isFinite(rec.t) || !(rec.price > 0)) continue;
  const a = c[idx.addr];
  if (!tokens.has(a)) tokens.set(a, []);
  tokens.get(a).push(rec);
}
for (const arr of tokens.values()) arr.sort((x, y) => x.t - y.t);

// ── 模擬：signalPick 回傳訊號索引或 -1 ──
function simulate(arr, signalPick, exit) {
  const i0 = signalPick(arr);
  if (i0 < 0) return null;
  const sig = arr[i0];
  let ei = -1;
  for (let j = i0 + 1; j < arr.length; j++) {
    const dt = (arr[j].t - sig.t) / H;
    if (dt > REG_VOID) return { void: 1 };
    if (dt >= REG_HOURS) {
      if (arr[j].price > sig.price * REG_FLOOR) { ei = j; break; }
      return { void: 1, saved: 1 };
    }
  }
  if (ei < 0) return { void: 1 };
  const entry = arr[ei];
  for (let k = ei + 1; k < arr.length; k++) {
    const r = arr[k];
    if ((r.t - arr[k - 1].t) / H > GONE_GAP_H) return { gone: 1, lastRet: arr[k - 1].price / entry.price - 1, sig };
    const ret = r.price / entry.price - 1;
    if (ret <= exit.sl) return { ret, sig };
    if (ret >= exit.tp) return { ret, sig };
  }
  return { gone: 1, lastRet: arr[arr.length - 1].price / entry.price - 1, sig };
}

function inBand(r, lo, hi) { return r.score >= lo && r.score <= hi && r.turn < TURN_MAX && r.liq >= LIQ_MIN; }
const firstIn = (lo, hi) => arr => arr.findIndex(r => inBand(r, lo, hi));
const seasonedIn = (lo, hi, seasonH) => arr => {
  const born = arr[0].t;
  return arr.findIndex(r => inBand(r, lo, hi) && (r.t - born) / H >= seasonH);
};

// ── 統計 ──
function collect(signalPick, exit, stakeFn) {
  const trades = []; let voids = 0, saved = 0;
  for (const arr of tokens.values()) {
    const s = simulate(arr, signalPick, exit);
    if (!s) continue;
    if (s.void) { voids++; if (s.saved) saved++; continue; }
    const st = stakeFn ? stakeFn(s.sig) : STAKE;
    if (s.gone) trades.push({ ret: s.lastRet, gone: 1, st, day: dayOf(s.sig.t) });
    else trades.push({ ret: s.ret, st, day: dayOf(s.sig.t) });
  }
  return { trades, voids, saved };
}
const dayOf = t => new Date(t).toISOString().slice(0, 10);
function stats(trades, severe) {
  if (!trades.length) return null;
  const pnl = trades.map(t => ((t.gone && severe) ? -0.99 : t.ret) * t.st);
  const rets = trades.map(t => (t.gone && severe) ? -0.99 : t.ret);
  const total = pnl.reduce((a, b) => a + b, 0);
  return {
    n: trades.length,
    winRate: rets.filter(r => r > 0).length / rets.length,
    avgRet: rets.reduce((a, b) => a + b, 0) / rets.length,
    total, exBest: total - Math.max(...pnl),
    fullLossRate: rets.filter(r => r <= -0.7).length / rets.length,
  };
}
const pct = x => (x * 100).toFixed(1) + '%';
const sol = x => (x >= 0 ? '+' : '') + x.toFixed(4);
function report(name, col) {
  console.log(`▶ ${name}   (掛號作廢${col.voids} 閘門救命${col.saved})`);
  for (const [tag, sev] of [['嚴苛', true], ['溫和', false]]) {
    const m = stats(col.trades, sev);
    if (m) console.log(`  [${tag}] N=${m.n} 勝率${pct(m.winRate)} 均報酬${pct(m.avgRet)} 總PnL ${sol(m.total)} | 扣最大單筆 ${sol(m.exBest)} | 全損率${pct(m.fullLossRate)}`);
  }
  console.log('');
}

// ── 開賽 ──
const EXIT80 = { sl: -0.35, tp: 0.8 }, EXIT2X = { sl: -0.35, tp: 1.0 };
console.log(`═══ 進場變體擂台 ═══  幣數:${tokens.size} 快照:${rows.length - 1}`);
console.log(`統一出場: 掛號2h + SL-35 + TP+80（×2基準另列）\n`);

console.log('── A. 分數帶解剖 ──');
report('基準 帶64-89 / TP+80', collect(firstIn(64, 89), EXIT80));
report('對照 帶64-89 / TP×2(現役)', collect(firstIn(64, 89), EXIT2X));
report('低段 帶64-74', collect(firstIn(64, 74), EXIT80));
report('中段 帶75-84', collect(firstIn(75, 84), EXIT80));
report('精華 帶85-89', collect(firstIn(85, 89), EXIT80));

console.log('── B. 蟬聯進場（幣齡滿N小時後才認訊號）──');
report('蟬聯12h+ 帶64-89', collect(seasonedIn(64, 89, 12), EXIT80));
report('蟬聯24h+ 帶64-89', collect(seasonedIn(64, 89, 24), EXIT80));
report('蟬聯24h+ 帶85-89(組合技)', collect(seasonedIn(85, 89, 24), EXIT80));

console.log('── C. 行情濾網（按日切開的基準組期望值）──');
{
  const col = collect(firstIn(64, 89), EXIT80);
  const byDay = new Map();
  for (const t of col.trades) { if (!byDay.has(t.day)) byDay.set(t.day, []); byDay.get(t.day).push(t); }
  const days = [...byDay.entries()].sort();
  console.log('  日期 | 筆數 | 嚴苛日PnL | 溫和日PnL');
  for (const [d, ts] of days) {
    const s1 = stats(ts, true), s2 = stats(ts, false);
    console.log(`  ${d} | ${String(ts.length).padStart(3)} | ${sol(s1.total)} | ${sol(s2.total)}`);
  }
  const counts = days.map(([, ts]) => ts.length).sort((a, b) => a - b);
  const med = counts[Math.floor(counts.length / 2)];
  const hot = [], cold = [];
  for (const [, ts] of days) (ts.length > med ? hot : cold).push(...ts);
  console.log('');
  report(`熱日(訊號數>中位${med})`, { trades: hot, voids: 0, saved: 0 });
  report('冷日(≤中位)', { trades: cold, voids: 0, saved: 0 });
}

console.log('── D. 分層倉位（85+×1.5 / 75-84×1.0 / 64-74×0.5）──');
const tiered = sig => STAKE * (sig.score >= 85 ? 1.5 : sig.score >= 75 ? 1.0 : 0.5);
report('分層倉位 帶64-89 / TP+80', collect(firstIn(64, 89), EXIT80, tiered));

console.log('讀法: A答「砍不砍低段」B答「等不等蟬聯」C答「選不選日子」D答「加不加權」');
console.log('注意: 小時級資料限制同前；蟬聯組幣齡以資料首見時間近似，早於資料期的幣會低估幣齡。');
