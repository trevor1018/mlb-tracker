"""Layer 3：Baseball Savant 逐球資料（Statcast）→ data/pitches.parquet

以 3 天為一個窗口抓 CSV（含 pitch_type、打者左右 stand、投手左右 p_throws、
xwOBA、揮棒速度、第幾輪打序等），只留分析用得到的欄位存成 parquet。
這是「打者對左右投/球種」與「投手對左右打」分析的底層資料。
"""
import datetime as dt
import gzip
import io
import os
import sys

import pandas as pd

from common import DATA, DATA_THROUGH, SEASON, SEASON_START, fetch_text, log

SAVANT = ("https://baseballsavant.mlb.com/statcast_search/csv?all=true"
          "&hfSea={season}%7C&hfGT=R%7C&player_type=pitcher"
          "&game_date_gt={start}&game_date_lt={end}&type=details")

KEEP = [
    "game_pk", "game_date", "pitcher", "batter", "stand", "p_throws",
    "pitch_type", "pitch_name", "release_speed", "release_spin_rate",
    "events", "description", "type", "zone", "plate_x", "plate_z",
    "launch_speed", "launch_angle", "bb_type",
    "estimated_woba_using_speedangle", "estimated_ba_using_speedangle",
    "woba_value", "woba_denom", "delta_run_exp",
    "inning", "inning_topbot", "home_team", "away_team",
    "bat_score", "fld_score", "balls", "strikes",
    "at_bat_number", "pitch_number", "n_thruorder_pitcher",
    "bat_speed", "swing_length", "arm_angle",
    "pitcher_days_since_prev_game",
]

RAW_DIR = os.path.join(DATA, "savant_raw")
os.makedirs(RAW_DIR, exist_ok=True)


# 窗口邊界必須固定！否則改個起始日就會讓整季快取全部失效重抓。
# 一律以「球季 3/20」為格線錨點切 3 天一段。
def windows(start, end, days=3, anchor=None):
    s = dt.date.fromisoformat(start)
    e = dt.date.fromisoformat(end)
    a = dt.date.fromisoformat(anchor or f"{s.year}-03-20")
    off = ((s - a).days // days) * days          # 往下取整到格線
    cur = a + dt.timedelta(days=off)
    out = []
    while cur <= e:
        w_end = cur + dt.timedelta(days=days - 1)
        out.append((cur.isoformat(), w_end.isoformat()))
        cur = w_end + dt.timedelta(days=1)
    return out


def get_window(season, start, end):
    """回傳該窗口的 DataFrame（優先讀本機 gz 快取）。"""
    path = os.path.join(RAW_DIR, f"{start}_{end}.csv.gz")
    if os.path.exists(path) and os.path.getsize(path) > 200:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            txt = f.read()
    else:
        txt = fetch_text(SAVANT.format(season=season, start=start, end=end))
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(txt)
    if txt.count("\n") < 2:
        return None
    df = pd.read_csv(io.StringIO(txt), low_memory=False)
    df.columns = [c.strip().strip('"').lstrip("﻿") for c in df.columns]
    cols = [c for c in KEEP if c in df.columns]
    return df[cols]


def main():
    season = int(sys.argv[1]) if len(sys.argv) > 1 else SEASON
    start = sys.argv[2] if len(sys.argv) > 2 else SEASON_START
    end = sys.argv[3] if len(sys.argv) > 3 else DATA_THROUGH
    wins = windows(start, end)
    log(f"Savant 逐球資料：{len(wins)} 個窗口 {start}~{end}")
    frames = []
    for i, (s, e) in enumerate(wins, 1):
        try:
            df = get_window(season, s, e)
        except Exception as ex:
            log(f"  窗口 {s}~{e} 失敗：{repr(ex)[:120]}")
            continue
        if df is None or df.empty:
            log(f"  {s}~{e}: 0 球")
            continue
        frames.append(df)
        log(f"  [{i}/{len(wins)}] {s}~{e}: {len(df):,} 球")
    if not frames:
        log("沒有資料")
        return 1
    allp = pd.concat(frames, ignore_index=True)
    allp = allp.drop_duplicates(subset=["game_pk", "at_bat_number", "pitch_number"])
    out = os.path.join(DATA, "pitches.parquet")
    allp.to_parquet(out, index=False, compression="zstd")
    log(f"寫出 {out}：{len(allp):,} 球, {allp['game_pk'].nunique()} 場, "
        f"{os.path.getsize(out)/1e6:.0f} MB")
    log(f"球種分布 top10：\n{allp['pitch_type'].value_counts().head(10)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
