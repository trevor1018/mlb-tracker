"""單隊詳細分項統計 → output/team_splits.json

用 Statcast 逐球資料算出每一隊的：
  打擊面：對左投/右投、對四大球種族群、日場/夜場、主場/客場、對每一種細球種
  投球面：對左打/右打、日場/夜場、主場/客場、各球種使用率與成效
指標：PA、wOBA、xwOBA(contact)、K%、BB%、Whiff%、HardHit%、Barrel%、avg EV。

球隊代碼用 Savant 的縮寫（home_team/away_team），透過 TEAM_ABBR 對回 MLB id。
打擊方 = inning_topbot=='Top' → away_team，否則 home_team。
"""
import sys

import numpy as np
import pandas as pd

from common import DATA, OUTPUT, TEAM_ABBR, TEAM_ZH, jdump, jload, log

# ─── 球種分族 ───
PITCH_GROUP = {
    "FF": "fastball", "FA": "fastball", "SI": "fastball", "FC": "cutter",
    "SL": "breaking", "ST": "breaking", "SV": "breaking", "CU": "breaking",
    "KC": "breaking", "CS": "breaking", "SC": "offspeed",
    "CH": "offspeed", "FS": "offspeed", "FO": "offspeed", "KN": "other",
    "EP": "other", "PO": "other", "IN": "other", "UN": "other",
}
GROUP_ZH = {"fastball": "速球", "cutter": "卡特", "breaking": "變化球",
            "offspeed": "慢速球", "other": "其他"}
PITCH_ZH = {
    "FF": "四縫線", "SI": "伸卡", "FC": "卡特", "SL": "滑球", "ST": "橫掃球",
    "SV": "斜滑球", "CU": "曲球", "KC": "彈指曲球", "CH": "變速球",
    "FS": "指叉球", "KN": "蝴蝶球", "FA": "速球", "EP": "慢速曲球",
}
ABBR2ID = {v: k for k, v in TEAM_ABBR.items()}
# Savant 用的舊縮寫對照（保險）
ABBR2ID.update({"CHW": 145, "KCR": 118, "SDP": 135, "SFG": 137, "TBR": 139,
                "WSN": 120, "ATH": 133, "AZ": 109})

SWING_DESC = {"swinging_strike", "swinging_strike_blocked", "foul", "foul_tip",
              "hit_into_play", "hit_into_play_score", "hit_into_play_no_out",
              "foul_bunt", "missed_bunt", "bunt_foul_tip"}
WHIFF_DESC = {"swinging_strike", "swinging_strike_blocked", "foul_tip",
              "missed_bunt", "bunt_foul_tip"}


def load_pitches():
    df = pd.read_parquet(f"{DATA}/pitches.parquet")
    games = {g["pk"]: g for g in jload(f"{DATA}/games.json")}
    df = df[df["game_pk"].isin(games.keys())].copy()

    df["bat_abbr"] = np.where(df["inning_topbot"].eq("Top"), df["away_team"], df["home_team"])
    df["pit_abbr"] = np.where(df["inning_topbot"].eq("Top"), df["home_team"], df["away_team"])
    df["bat_team"] = df["bat_abbr"].map(ABBR2ID)
    df["pit_team"] = df["pit_abbr"].map(ABBR2ID)
    df["bat_home"] = df["inning_topbot"].ne("Top")

    day = {pk: bool(g.get("dayGame")) for pk, g in games.items()}
    df["day"] = df["game_pk"].map(day)
    df["month"] = pd.to_datetime(df["game_date"]).dt.month
    df["pgroup"] = df["pitch_type"].map(PITCH_GROUP).fillna("other")

    # 揮棒 / 揮空 / 界內球
    df["swing"] = df["description"].isin(SWING_DESC)
    df["whiff"] = df["description"].isin(WHIFF_DESC)
    df["bip"] = df["launch_speed"].notna()
    df["hard"] = df["bip"] & (df["launch_speed"] >= 95)
    la = df["launch_angle"]
    df["barrel"] = df["bip"] & (df["launch_speed"] >= 98) & la.between(26, 30)
    ev = df["launch_speed"]
    df["sweetspot"] = df["bip"] & la.between(8, 32)
    df["ev"] = ev
    df["in_zone"] = df["zone"].between(1, 9)
    df["chase"] = df["swing"] & ~df["in_zone"].fillna(False)
    df["k"] = df["events"].eq("strikeout") | df["events"].eq("strikeout_double_play")
    df["bb"] = df["events"].isin(["walk", "hit_by_pitch"])
    df["hr"] = df["events"].eq("home_run")
    df["hit"] = df["events"].isin(["single", "double", "triple", "home_run"])
    unmapped = df["bat_team"].isna().sum()
    if unmapped:
        log(f"警告：{unmapped} 球的球隊縮寫無法對應：{sorted(set(df.loc[df['bat_team'].isna(),'bat_abbr']))[:10]}")
    return df.dropna(subset=["bat_team", "pit_team"])


def agg(g):
    """一組逐球資料 → 指標 dict"""
    pa = float(g["woba_denom"].sum())
    bip = int(g["bip"].sum())
    swings = int(g["swing"].sum())
    out = {
        "pitches": int(len(g)),
        "pa": round(pa, 1),
        "woba": round(float(g["woba_value"].sum()) / pa, 3) if pa >= 1 else None,
        "xwoba_con": round(float(g["estimated_woba_using_speedangle"].mean()), 3) if bip else None,
        "k_pct": round(100 * float(g["k"].sum()) / pa, 1) if pa >= 1 else None,
        "bb_pct": round(100 * float(g["bb"].sum()) / pa, 1) if pa >= 1 else None,
        "hr_pct": round(100 * float(g["hr"].sum()) / pa, 2) if pa >= 1 else None,
        "whiff_pct": round(100 * float(g["whiff"].sum()) / swings, 1) if swings >= 5 else None,
        "hardhit_pct": round(100 * float(g["hard"].sum()) / bip, 1) if bip >= 5 else None,
        "barrel_pct": round(100 * float(g["barrel"].sum()) / bip, 1) if bip >= 5 else None,
        "sweetspot_pct": round(100 * float(g["sweetspot"].sum()) / bip, 1) if bip >= 5 else None,
        "ev": round(float(g["ev"].mean()), 1) if bip else None,
        "chase_pct": round(100 * float(g["chase"].sum()) / max(1, int((~g["in_zone"].fillna(False)).sum())), 1),
    }
    return out


def splits_for(df, key_col, label_map=None, min_pa=25):
    """依 key_col 分組，回傳 {key: 指標}"""
    out = {}
    for k, g in df.groupby(key_col, dropna=True):
        if float(g["woba_denom"].sum()) < min_pa:
            continue
        name = label_map.get(k, str(k)) if label_map else str(k)
        out[name] = agg(g)
    return out


def build():
    df = load_pitches()
    log(f"逐球資料 {len(df):,} 球，{df['game_pk'].nunique()} 場")

    league = {
        "bat_all": agg(df),
        "vs_LHP": agg(df[df["p_throws"].eq("L")]),
        "vs_RHP": agg(df[df["p_throws"].eq("R")]),
        "by_group": splits_for(df, "pgroup", GROUP_ZH, min_pa=200),
        "day": agg(df[df["day"]]),
        "night": agg(df[~df["day"]]),
    }

    teams = {}
    for tid in sorted(TEAM_ZH):
        bat = df[df["bat_team"].eq(tid)]
        pit = df[df["pit_team"].eq(tid)]
        if bat.empty or pit.empty:
            continue
        t = {
            "id": tid, "zh": TEAM_ZH[tid], "abbr": TEAM_ABBR[tid],
            "bat": {
                "all": agg(bat),
                "vs_LHP": agg(bat[bat["p_throws"].eq("L")]),
                "vs_RHP": agg(bat[bat["p_throws"].eq("R")]),
                "day": agg(bat[bat["day"]]),
                "night": agg(bat[~bat["day"]]),
                "home": agg(bat[bat["bat_home"]]),
                "away": agg(bat[~bat["bat_home"]]),
                "by_group": splits_for(bat, "pgroup", GROUP_ZH, min_pa=60),
                "by_pitch": splits_for(bat, "pitch_type", PITCH_ZH, min_pa=40),
                # 交叉：對左投的球種、對右投的球種
                "vsL_by_group": splits_for(bat[bat["p_throws"].eq("L")], "pgroup", GROUP_ZH, min_pa=40),
                "vsR_by_group": splits_for(bat[bat["p_throws"].eq("R")], "pgroup", GROUP_ZH, min_pa=40),
                "by_month": splits_for(bat, "month", None, min_pa=60),
            },
            "pit": {
                "all": agg(pit),
                "vs_LHB": agg(pit[pit["stand"].eq("L")]),
                "vs_RHB": agg(pit[pit["stand"].eq("R")]),
                "day": agg(pit[pit["day"]]),
                "night": agg(pit[~pit["day"]]),
                "home": agg(pit[~pit["bat_home"]]),   # 我隊投球=對手打擊，主場時對手是客隊打
                "away": agg(pit[pit["bat_home"]]),
                "by_group": splits_for(pit, "pgroup", GROUP_ZH, min_pa=60),
                "by_pitch": splits_for(pit, "pitch_type", PITCH_ZH, min_pa=40),
                "by_month": splits_for(pit, "month", None, min_pa=60),
                "usage": {GROUP_ZH.get(k, k): round(100 * v / len(pit), 1)
                          for k, v in pit["pgroup"].value_counts().items()},
            },
        }
        teams[str(tid)] = t
        log(f"  {TEAM_ZH[tid]:<4} 打擊 {t['bat']['all']['pa']:.0f} PA wOBA {t['bat']['all']['woba']} | "
            f"投球 {t['pit']['all']['pa']:.0f} PA wOBA {t['pit']['all']['woba']}")

    # 聯盟排名（打擊 wOBA 對左/右投、各球種）
    def rank(metric_path, reverse=True):
        vals = []
        for tid, t in teams.items():
            v = t
            for k in metric_path:
                v = (v or {}).get(k) if isinstance(v, dict) else None
            if v is not None:
                vals.append((tid, v))
        vals.sort(key=lambda x: x[1], reverse=reverse)
        return {tid: i + 1 for i, (tid, _) in enumerate(vals)}

    ranks = {
        "bat_vsL_woba": rank(["bat", "vs_LHP", "woba"]),
        "bat_vsR_woba": rank(["bat", "vs_RHP", "woba"]),
        "bat_fastball_woba": rank(["bat", "by_group", "速球", "woba"]),
        "bat_breaking_woba": rank(["bat", "by_group", "變化球", "woba"]),
        "bat_offspeed_woba": rank(["bat", "by_group", "慢速球", "woba"]),
        "bat_day_woba": rank(["bat", "day", "woba"]),
        "bat_night_woba": rank(["bat", "night", "woba"]),
        "pit_vsL_woba": rank(["pit", "vs_LHB", "woba"], reverse=False),
        "pit_vsR_woba": rank(["pit", "vs_RHB", "woba"], reverse=False),
    }
    for key, r in ranks.items():
        for tid, v in r.items():
            teams[tid].setdefault("ranks", {})[key] = v

    out = {"season": 2026, "league": league, "teams": teams,
           "meta": {"pitches": int(len(df)), "games": int(df["game_pk"].nunique())}}
    p = jdump(out, f"{OUTPUT}/team_splits.json")
    log(f"寫出 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(build())
