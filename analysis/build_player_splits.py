"""球員層級分項 → output/player_splits.json

每一隊列出：
  打者（PA 前 14 名）：對左投 / 對右投 / 對速球 / 對變化球 / 對慢速球、日夜場
  投手（先發 + 主要後援）：球種使用率、對左打 / 對右打、日夜場、主客場、平均球速

這是「打者對左右投/球種/日夜場、投手對左右打/日夜/主客」最細的一層，
用來眼睛掃對位（例如對手先發是變化球型左投，我方打線對變化球特別弱）。
"""
import sys

import numpy as np
import pandas as pd

from build_splits import (ABBR2ID, GROUP_ZH, PITCH_GROUP, SWING_DESC, WHIFF_DESC,
                          agg, load_pitches)
from common import DATA, OUTPUT, TEAM_ZH, jdump, jload, log

MIN_BAT_PA = 80
MIN_PIT_PA = 60
SPLIT_MIN = 25


def small(d):
    """精簡指標集（球員層級不需要那麼多欄位）"""
    if not d:
        return None
    return {"pa": d["pa"], "woba": d["woba"], "xwoba": d["xwoba_con"],
            "k": d["k_pct"], "bb": d["bb_pct"], "hr": d["hr_pct"],
            "whiff": d["whiff_pct"], "hard": d["hardhit_pct"], "ev": d["ev"]}


def splits_for_player(g, day_col="day"):
    out = {"all": small(agg(g))}
    for key, mask in (("vsL", g["p_throws"].eq("L")), ("vsR", g["p_throws"].eq("R"))):
        sub = g[mask]
        if float(sub["woba_denom"].sum()) >= SPLIT_MIN:
            out[key] = small(agg(sub))
    for key, grp in (("速球", "fastball"), ("變化球", "breaking"), ("慢速球", "offspeed")):
        sub = g[g["pgroup"].eq(grp)]
        if float(sub["woba_denom"].sum()) >= SPLIT_MIN:
            out[key] = small(agg(sub))
    for key, mask in (("day", g[day_col]), ("night", ~g[day_col])):
        sub = g[mask]
        if float(sub["woba_denom"].sum()) >= SPLIT_MIN:
            out[key] = small(agg(sub))
    return out


def splits_for_pitcher(g, day_col="day"):
    out = {"all": small(agg(g))}
    for key, mask in (("vsL", g["stand"].eq("L")), ("vsR", g["stand"].eq("R"))):
        sub = g[mask]
        if float(sub["woba_denom"].sum()) >= SPLIT_MIN:
            out[key] = small(agg(sub))
    for key, mask in (("day", g[day_col]), ("night", ~g[day_col])):
        sub = g[mask]
        if float(sub["woba_denom"].sum()) >= SPLIT_MIN:
            out[key] = small(agg(sub))
    for key, mask in (("home", ~g["bat_home"]), ("away", g["bat_home"])):
        sub = g[mask]
        if float(sub["woba_denom"].sum()) >= SPLIT_MIN:
            out[key] = small(agg(sub))
    total = len(g)
    out["usage"] = {GROUP_ZH.get(k, k): round(100 * v / total, 1)
                    for k, v in g["pgroup"].value_counts().items() if v / total >= 0.03}
    fb = g[g["pgroup"].eq("fastball")]["release_speed"]
    out["fb_velo"] = round(float(fb.mean()), 1) if len(fb) > 20 else None
    return out


def main():
    df = load_pitches()
    people = jload(f"{DATA}/people.json")
    boxes = jload(f"{DATA}/boxes.json.gz")

    # 先發場次數（判斷是先發還是後援）
    starts = {}
    for b in boxes:
        for side in ("away", "home"):
            sp = b[side]["sp"]
            if sp:
                starts[sp["id"]] = starts.get(sp["id"], 0) + 1

    log(f"逐球 {len(df):,}，球員 {len(people)}")
    out_teams = {}
    for tid in sorted(TEAM_ZH):
        bat = df[df["bat_team"].eq(tid)]
        pit = df[df["pit_team"].eq(tid)]
        if bat.empty or pit.empty:
            continue
        batters = []
        for pid, g in bat.groupby("batter"):
            pa = float(g["woba_denom"].sum())
            if pa < MIN_BAT_PA:
                continue
            info = people.get(str(int(pid))) or {}
            batters.append({"id": int(pid), "name": info.get("name") or str(int(pid)),
                            "bats": info.get("bats"), "pos": info.get("pos"),
                            "splits": splits_for_player(g)})
        batters.sort(key=lambda b: -(b["splits"]["all"]["pa"] or 0))

        pitchers = []
        for pid, g in pit.groupby("pitcher"):
            pa = float(g["woba_denom"].sum())
            if pa < MIN_PIT_PA:
                continue
            info = people.get(str(int(pid))) or {}
            pitchers.append({"id": int(pid), "name": info.get("name") or str(int(pid)),
                             "throws": info.get("throws"),
                             "starts": starts.get(int(pid), 0),
                             "role": "SP" if starts.get(int(pid), 0) >= 5 else "RP",
                             "splits": splits_for_pitcher(g)})
        pitchers.sort(key=lambda p: (p["role"] != "SP", -(p["splits"]["all"]["pa"] or 0)))

        out_teams[str(tid)] = {"zh": TEAM_ZH[tid],
                              "batters": batters[:14],
                              "pitchers": pitchers[:12]}
        log(f"  {TEAM_ZH[tid]:<4} 打者 {len(batters)} 人、投手 {len(pitchers)} 人")

    p = jdump({"season": 2026, "teams": out_teams,
               "thresholds": {"bat_pa": MIN_BAT_PA, "pit_pa": MIN_PIT_PA,
                              "split_min": SPLIT_MIN}},
              f"{OUTPUT}/player_splits.json")
    import os
    log(f"寫出 {p}（{os.path.getsize(p)/1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
