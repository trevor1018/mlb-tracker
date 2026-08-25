"""把逐球資料壓成「逐場 × 分項」聚合表，供 build_dataset 做 as-of 滾動特徵。

輸出（都在 data/）：
  tg_hand.parquet    每場每隊打擊 vs 左投/右投
  tg_group.parquet   每場每隊打擊 vs 球種族群
  pg_pitcher.parquet 每場每位投手 vs 左打/右打
  pg_usage.parquet   每場每位投手的球種使用數
"""
import sys

import numpy as np
import pandas as pd

from common import DATA, log
from build_splits import ABBR2ID, PITCH_GROUP, SWING_DESC, WHIFF_DESC


def prep(df):
    df["bat_team"] = np.where(df["inning_topbot"].eq("Top"), df["away_team"], df["home_team"])
    df["pit_team"] = np.where(df["inning_topbot"].eq("Top"), df["home_team"], df["away_team"])
    df["bat_team"] = df["bat_team"].map(ABBR2ID)
    df["pit_team"] = df["pit_team"].map(ABBR2ID)
    df["pgroup"] = df["pitch_type"].map(PITCH_GROUP).fillna("other")
    df["swing"] = df["description"].isin(SWING_DESC)
    df["whiff"] = df["description"].isin(WHIFF_DESC)
    df["bip"] = df["launch_speed"].notna()
    df["hard"] = df["bip"] & (df["launch_speed"] >= 95)
    df["barrel"] = df["bip"] & (df["launch_speed"] >= 98) & df["launch_angle"].between(26, 30)
    df["k"] = df["events"].isin(["strikeout", "strikeout_double_play"])
    df["bb"] = df["events"].isin(["walk", "hit_by_pitch"])
    df["hr"] = df["events"].eq("home_run")
    df["xwoba"] = df["estimated_woba_using_speedangle"]
    return df.dropna(subset=["bat_team", "pit_team"])


AGGS = {
    "pitches": ("pitch_type", "size"),
    "pa": ("woba_denom", "sum"),
    "woba_sum": ("woba_value", "sum"),
    "xwoba_sum": ("xwoba", "sum"),
    "xwoba_n": ("xwoba", "count"),
    "k": ("k", "sum"),
    "bb": ("bb", "sum"),
    "hr": ("hr", "sum"),
    "swings": ("swing", "sum"),
    "whiffs": ("whiff", "sum"),
    "bip": ("bip", "sum"),
    "hard": ("hard", "sum"),
    "barrel": ("barrel", "sum"),
    "ev_sum": ("launch_speed", "sum"),
}


def group(df, keys):
    g = df.groupby(keys, dropna=False).agg(**AGGS).reset_index()
    return g


def main():
    log("讀取 pitches.parquet …")
    df = pd.read_parquet(f"{DATA}/pitches.parquet")
    log(f"  {len(df):,} 球")
    df = prep(df)

    tg_hand = group(df, ["game_pk", "game_date", "bat_team", "p_throws"])
    tg_hand.to_parquet(f"{DATA}/tg_hand.parquet", index=False)
    log(f"tg_hand {len(tg_hand):,} 列")

    tg_group = group(df, ["game_pk", "game_date", "bat_team", "pgroup"])
    tg_group.to_parquet(f"{DATA}/tg_group.parquet", index=False)
    log(f"tg_group {len(tg_group):,} 列")

    pg_pitcher = group(df, ["game_pk", "game_date", "pitcher", "pit_team", "stand"])
    pg_pitcher.to_parquet(f"{DATA}/pg_pitcher.parquet", index=False)
    log(f"pg_pitcher {len(pg_pitcher):,} 列")

    usage = (df.groupby(["game_pk", "pitcher", "pgroup"], dropna=False)
               .size().rename("n").reset_index())
    usage.to_parquet(f"{DATA}/pg_usage.parquet", index=False)
    log(f"pg_usage {len(usage):,} 列")

    # 投手逐場對「該場打擊方」的球種平均球速，用於觀察狀態
    velo = (df[df["pgroup"].eq("fastball")]
            .groupby(["game_pk", "pitcher"])["release_speed"].mean()
            .rename("fb_velo").reset_index())
    velo.to_parquet(f"{DATA}/pg_velo.parquet", index=False)
    log(f"pg_velo {len(velo):,} 列")
    return 0


if __name__ == "__main__":
    sys.exit(main())
