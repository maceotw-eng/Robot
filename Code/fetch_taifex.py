# -*- coding: utf-8 -*-
"""
fetch_taifex.py — 下載 TAIFEX 期貨逐筆成交資料並組成 1 分 K
資料來源: https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_YYYY_MM_DD.zip
(期交所「每日交易行情下載 > 期貨每筆成交資料」)

用法:
    python fetch_taifex.py --start 2025-07-01 --end 2026-07-17
只會下載結算日(預設週三,可加週五),已下載的會跳過(快取在 data/raw/)。
產出: data/bars/TX_1min_YYYY-MM-DD.csv
"""
import argparse
import io
import os
import sys
import time
import zipfile
from datetime import date, timedelta

import pandas as pd

RAW_DIR = os.path.join(os.path.dirname(__file__), "data", "raw")
BAR_DIR = os.path.join(os.path.dirname(__file__), "data", "bars")
URL_TPL = ("https://www.taifex.com.tw/file/taifex/Dailydownload/"
           "DailydownloadCSV/Daily_{y}_{m:02d}_{d:02d}.zip")

# 逐筆檔常見欄名(Big5)。期交所偶爾微調格式,若解析失敗請對照實際檔頭修改這裡。
COL_ALIASES = {
    "date":    ["成交日期"],
    "product": ["商品代號"],
    "expiry":  ["到期月份(週別)", "到期月份(週別) "],
    "time":    ["成交時間"],
    "price":   ["成交價格"],
    "volume":  ["成交數量(B+S)", "成交數量(B or S)", "成交數量"],
}


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def download_day(d: date, session=None) -> str | None:
    """下載某日 zip,回傳本地路徑;失敗回傳 None。"""
    import requests
    os.makedirs(RAW_DIR, exist_ok=True)
    local = os.path.join(RAW_DIR, f"Daily_{d:%Y_%m_%d}.zip")
    if os.path.exists(local) and os.path.getsize(local) > 1024:
        return local
    url = URL_TPL.format(y=d.year, m=d.month, d=d.day)
    try:
        r = (session or requests).get(url, timeout=30)
        if r.status_code != 200 or len(r.content) < 1024:
            return None
        with open(local, "wb") as f:
            f.write(r.content)
        time.sleep(1.0)  # 對期交所伺服器客氣一點
        return local
    except Exception as e:
        print(f"  [warn] {d} 下載失敗: {e}", file=sys.stderr)
        return None


def _pick(cols, names):
    for n in names:
        if n in cols:
            return n
    return None


def parse_zip_to_ticks(zpath: str, product: str = "TX") -> pd.DataFrame:
    """讀 zip 內 CSV(Big5),過濾指定商品近月,回傳逐筆 DataFrame。"""
    with zipfile.ZipFile(zpath) as z:
        name = z.namelist()[0]
        raw = z.read(name)
    df = pd.read_csv(io.BytesIO(raw), encoding="big5", low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    c = {k: _pick(df.columns, v) for k, v in COL_ALIASES.items()}
    missing = [k for k, v in c.items() if v is None]
    if missing:
        raise ValueError(f"欄位對不上: 缺 {missing}, 實際欄位={list(df.columns)}")
    df = df.rename(columns={c[k]: k for k in c})
    df["product"] = df["product"].astype(str).str.strip()
    df = df[df["product"] == product].copy()
    # 近月: 到期月份最小者(排除價差單,價差的到期欄含 '/')
    df["expiry"] = df["expiry"].astype(str).str.strip()
    df = df[~df["expiry"].str.contains("/")]
    near = df["expiry"].min()
    df = df[df["expiry"] == near].copy()
    # 時間
    t = df["time"].astype(str).str.replace(":", "", regex=False).str.zfill(6)
    ds = df["date"].astype(str).str.replace("/", "-", regex=False)
    df["ts"] = pd.to_datetime(ds + " " + t, format="%Y-%m-%d %H%M%S", errors="coerce")
    df = df.dropna(subset=["ts"])
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0) / 2  # B+S 除以 2 = 口數
    return df[["ts", "price", "volume"]].sort_values("ts")


def ticks_to_1min(ticks: pd.DataFrame, day_session_only=True) -> pd.DataFrame:
    """逐筆 -> 1 分 K(日盤 08:45–13:45)。"""
    t = ticks.set_index("ts")
    if day_session_only:
        t = t.between_time("08:45", "13:45")
    o = t["price"].resample("1min").first()
    h = t["price"].resample("1min").max()
    l = t["price"].resample("1min").min()
    cl = t["price"].resample("1min").last()
    v = t["volume"].resample("1min").sum()
    bars = pd.DataFrame({"open": o, "high": h, "low": l, "close": cl, "volume": v}).dropna(
        subset=["close"])
    return bars


def build_bars_for_day(d: date, product="TX") -> str | None:
    os.makedirs(BAR_DIR, exist_ok=True)
    out = os.path.join(BAR_DIR, f"{product}_1min_{d:%Y-%m-%d}.csv")
    if os.path.exists(out):
        return out
    z = download_day(d)
    if z is None:
        return None
    try:
        ticks = parse_zip_to_ticks(z, product)
        if ticks.empty:
            return None
        bars = ticks_to_1min(ticks)
        bars.to_csv(out)
        print(f"  [ok] {d} -> {len(bars)} bars")
        return out
    except Exception as e:
        print(f"  [warn] {d} 解析失敗: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--product", default="TX")
    ap.add_argument("--weekdays", nargs="*", type=int, default=[2],
                    help="0=一 1=二 2=三 3=四 4=五 (預設只抓週三結算日;要含週五加 4)")
    args = ap.parse_args()
    s = date.fromisoformat(args.start)
    e = date.fromisoformat(args.end)
    days = [d for d in daterange(s, e) if d.weekday() in set(args.weekdays)]
    print(f"目標 {len(days)} 個交易日 (weekdays={args.weekdays})")
    ok = 0
    for d in days:
        if build_bars_for_day(d, args.product):
            ok += 1
    print(f"完成: {ok}/{len(days)} 天有資料 (國定假日/颱風假無檔屬正常)")


if __name__ == "__main__":
    main()
