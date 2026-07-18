#!/usr/bin/env node
/**
 * arena.js — 方法擂台離線回測（讀 research.csv，不碰交易引擎）
 * 用法: node arena.js [csv路徑]   預設 ./data/research.csv
 * 原型: 大砲(現役v2.0近似) / 短刀矩陣 / 快槍近似(小時級)
 * 輸出: 對齊二審三關的指標表（溫和/嚴苛兩種死亡假設）
 */
const fs = require('fs');

const CSV = process.argv[2] || './data/research.csv';
const STAKE = 0.05;            // 每筆模擬倉位 SOL
const BAND = [64, 89];         // 收錄帶
const TURN_MAX = 10;           // 換手 <1000%
const LIQ_MIN = 15000;         // 流動性下限（candidate 濾網近似）
const REG_HOURS = 2;           // 掛號存活確認
const REG_VOID = 12;           // 掛號逾時作廢
const REG_FLOOR = 0.55;        // 掛號存活價格線
const GONE_GAP_H = 6;          // 斷訊 >6h 視為下架

// ── 讀檔 ──
const rows = fs.readFileSync(CSV, 'utf8').trim().split('\n');
const head = rows[0].split(',');
const idx = Object.fromEntries(head.map((h, i) => [h, i]));
const tokens = new Map(); // addr -> [{t,price,score,turn,liq,sym}]
for (let i = 1; i < rows.length; i++) {
  const c = rows[i].split(',');
  if (c.length < head.length) continue;
  const rec = {
    t: Date.parse(c[idx.time]),
    price: +c[idx.price],
    score: +c[idx.score],
    turn: +c[idx.turnover],
    liq: +c[idx.liq],
    sym: c[idx.symbol],
  };
  if (!isFinite(rec.t) || !(rec.price > 0)) continue;
  const a = c[idx.addr];
  if (!tokens.has(a)) tokens.set(a, []);
  tokens.get(a).push(rec);
}
for (const arr of tokens.values()) arr.sort((x, y) => x.t - y.t);
const H = 3600e3;

// ── 單幣模擬 ──
// cfg: {reg:bool, sl:-0.35, tp:1.0(=×2)|0.5|..., timeH:null|12|24|48, holdH:null(快槍強制平倉)}
function simulate(arr, cfg) {
  // 1) 找訊號
  let i0 = -1;
  for (let i = 0; i < arr.length; i++) {
    const r = arr[i];
    if (r.score >= BAND[0] && r.score <= BAND[1] && r.turn < TURN_MAX && r.liq >= LIQ_MIN) { i0 = i; break; }
  }
  if (i0 < 0) return null;
  const sig = arr[i0];

  // 2) 進場點：掛號制 or 立即(快槍)
  let ei = i0;
  if (cfg.reg) {
    ei = -1;
    for (let j = i0 + 1; j < arr.length; j++) {
      const dt = (arr[j].t - sig.t) / H;
      if (dt > REG_VOID) return { void: true };
      if (dt >= REG_HOURS) {
        if (arr[j].price > sig.price * REG_FLOOR) { ei = j; break; }
        return { void: true, deadInReg: true }; // 掛號期死亡＝閘門救命
      }
    }
    if (ei < 0) return { void: true, gone: true }; // 掛號期斷訊
  }
  const entry = arr[ei];

  // 3) 逐快照走出場
  for (let k = ei + 1; k < arr.length; k++) {
    const r = arr[k];
    const gap = (r.t - arr[k - 1].t) / H;
    const heldH = (r.t - entry.t) / H;
    const ret = r.price / entry.price - 1;
    if (gap > GONE_GAP_H) return { ret: null, gone: true, lastRet: arr[k - 1].price / entry.price - 1 };
    if (ret <= cfg.sl) return { ret, exit: 'SL', heldH };
    if (cfg.tp != null && ret >= cfg.tp) return { ret, exit: 'TP', heldH };
    if (cfg.timeH != null && heldH >= cfg.timeH) return { ret, exit: 'TIME', heldH };
    if (cfg.holdH != null && heldH >= cfg.holdH) return { ret, exit: 'HOLD', heldH };
  }
  const last = arr[arr.length - 1];
  return { ret: null, gone: true, lastRet: last.price / entry.price - 1 }; // 資料尾端斷訊
}

// ── 整場統計 ──
function run(cfg) {
  const trades = [], voids = { total: 0, saved: 0 };
  let goneN = 0;
  for (const arr of tokens.values()) {
    const s = simulate(arr, cfg);
    if (!s) continue;
    if (s.void) { voids.total++; if (s.deadInReg) voids.saved++; continue; }
    if (s.ret == null && s.gone) { goneN++; trades.push({ ret: s.lastRet, gone: true }); continue; }
    if (s.ret != null) trades.push({ ret: s.ret, exit: s.exit });
  }
  const mk = (severe) => {
    const rets = trades.map(t => (t.gone && severe) ? -0.99 : t.ret);
    if (!rets.length) return null;
    const pnl = rets.map(r => r * STAKE);
    const wins = rets.filter(r => r > 0);
    const total = pnl.reduce((a, b) => a + b, 0);
    const best = Math.max(...pnl);
    const fullLoss = rets.filter(r => r <= -0.7).length;
    return {
      n: rets.length,
      winRate: wins.length / rets.length,
      avgRet: rets.reduce((a, b) => a + b, 0) / rets.length,
      totalPnL: total,
      exBest: total - best,
      maxLoss: Math.min(...pnl),
      fullLossRate: fullLoss / rets.length,
    };
  };
  return { cfg, voids, goneN, temperate: mk(false), severe: mk(true) };
}

// ── 策略清單 ──
const strategies = [];
strategies.push({ name: '大砲 v2.0基準 (SL-35/TP×2)', cfg: { reg: true, sl: -0.35, tp: 1.0, timeH: null } });
for (const tp of [0.3, 0.5, 0.8])
  for (const th of [12, 24, 48])
    for (const sl of [-0.25, -0.35])
      strategies.push({ name: `短刀 TP+${tp * 100}/T${th}h/SL${sl * 100}`, cfg: { reg: true, sl, tp, timeH: th } });
for (const hh of [1, 3, 6])
  strategies.push({ name: `快槍近似 T0進/強平${hh}h/SL-35`, cfg: { reg: false, sl: -0.35, tp: null, timeH: null, holdH: hh } });

// ── 輸出 ──
const pct = x => (x * 100).toFixed(1) + '%';
const sol = x => (x >= 0 ? '+' : '') + x.toFixed(4);
console.log(`═══ 方法擂台回測 ═══  樣本幣數: ${tokens.size}  快照: ${rows.length - 1}`);
console.log(`規則: 帶${BAND[0]}-${BAND[1]} 換手<${TURN_MAX * 100}% LIQ>=${LIQ_MIN} 掛號${REG_HOURS}h`);
console.log(`死亡假設: [溫和]=斷訊以最後報價結算 / [嚴苛]=斷訊視同-99%(安靜下架條款)\n`);

const results = strategies.map(s => ({ name: s.name, ...run(s.cfg) }));
results.sort((a, b) => ((b.severe?.totalPnL) ?? -1e9) - ((a.severe?.totalPnL) ?? -1e9));

const line = (r, m, tag) => m && console.log(
  `  [${tag}] N=${m.n} 勝率${pct(m.winRate)} 均報酬${pct(m.avgRet)} ` +
  `總PnL ${sol(m.totalPnL)} | 扣最大單筆 ${sol(m.exBest)} | 最大虧 ${sol(m.maxLoss)} | 全損率${pct(m.fullLossRate)}`
);
for (const r of results) {
  console.log(`▶ ${r.name}   (掛號作廢${r.voids.total} 其中閘門救命${r.voids.saved} | 斷訊${r.goneN})`);
  line(r, r.severe, '嚴苛');
  line(r, r.temperate, '溫和');
  console.log('');
}
console.log('三關對照: 淨期望為正=總PnL>0 | 扣最大單筆不深負=exBest | 結構健康=全損率<15%');
console.log('注意: 小時級資料，快槍組僅為方向性近似；盤中極值不可見，停損成交價=快照價(悲觀)。');
