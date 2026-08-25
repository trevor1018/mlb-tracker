"""把各個分析輸出壓成一份給網頁用的精簡 JSON → output/app_data.json

網頁只讀這一支檔案，所以要控制大小（目標 < 400KB）。
"""
import sys
from datetime import datetime

from common import OUTPUT, jdump, jload, log

COND_KEYS = ["market", "market_zh", "label", "n", "hits", "rate", "base", "lift",
             "wilson", "q", "be_odds", "depth", "chance_p95", "beats_chance",
             "block_rates"]
TEAM_COND_KEYS = ["market", "market_zh", "label", "n", "hits", "rate", "base_team",
                  "base_league", "wilson", "q", "be_odds", "depth", "beats_chance"]

SPLIT_KEYS = ["pa", "woba", "xwoba_con", "k_pct", "bb_pct", "hr_pct", "whiff_pct",
              "hardhit_pct", "barrel_pct", "ev"]


def slim_split(d):
    if not d:
        return None
    return {k: d.get(k) for k in SPLIT_KEYS if d.get(k) is not None}


def slim_group(d, keys=None):
    if not d:
        return {}
    out = {}
    for k, v in d.items():
        if keys and k not in keys:
            continue
        out[k] = slim_split(v)
    return out


def pick(d, keys):
    return {k: d.get(k) for k in keys if k in d}


def main():
    C = jload(f"{OUTPUT}/conditions.json")
    T = jload(f"{OUTPUT}/team_conditions.json")
    S = jload(f"{OUTPUT}/team_splits.json")
    try:
        M = jload(f"{OUTPUT}/models.json")
    except Exception:
        M = {"models": {}, "importance": {}}
    try:
        SL = jload(f"{OUTPUT}/slate.json")
    except Exception:
        SL = None

    conds = {"tierA": [], "tierB": [], "oos": []}
    for scope, key in (("隊伍", "teamgame"), ("全場", "game")):
        for tier in ("tierA", "tierB"):
            for r in C[key][tier]:
                row = pick(r, COND_KEYS)
                row["scope"] = scope
                conds[tier].append(row)
        for s in C[key]["oos"]["summary"]:
            conds["oos"].append({"scope": scope, **s})
    conds["tierA"].sort(key=lambda r: -r["wilson"])
    conds["tierB"].sort(key=lambda r: -r["wilson"])
    conds["tierB"] = conds["tierB"][:40]
    conds["cut"] = C["teamgame"]["oos"]["cut"]
    conds["hypotheses"] = (C["teamgame"]["meta"]["hypotheses"]
                           + C["game"]["meta"]["hypotheses"])

    teams = {}
    for tid, t in S["teams"].items():
        tc = T["teams"].get(tid, {})
        bat, pit = t["bat"], t["pit"]
        teams[tid] = {
            "zh": t["zh"], "abbr": t["abbr"],
            "bat": {
                "all": slim_split(bat["all"]),
                "vsL": slim_split(bat["vs_LHP"]), "vsR": slim_split(bat["vs_RHP"]),
                "day": slim_split(bat["day"]), "night": slim_split(bat["night"]),
                "home": slim_split(bat["home"]), "away": slim_split(bat["away"]),
                "group": slim_group(bat["by_group"], {"速球", "變化球", "慢速球", "卡特"}),
                "vsL_group": slim_group(bat.get("vsL_by_group"), {"速球", "變化球", "慢速球"}),
                "vsR_group": slim_group(bat.get("vsR_by_group"), {"速球", "變化球", "慢速球"}),
                "pitch": slim_group(bat.get("by_pitch")),
            },
            "pit": {
                "all": slim_split(pit["all"]),
                "vsL": slim_split(pit["vs_LHB"]), "vsR": slim_split(pit["vs_RHB"]),
                "day": slim_split(pit["day"]), "night": slim_split(pit["night"]),
                "home": slim_split(pit["home"]), "away": slim_split(pit["away"]),
                "group": slim_group(pit["by_group"], {"速球", "變化球", "慢速球", "卡特"}),
                "usage": pit.get("usage"),
            },
            "ranks": t.get("ranks", {}),
            "games": tc.get("games"),
            "chance_line": tc.get("chance_max_rate_p95"),
            "best": pick(tc.get("best") or {}, TEAM_COND_KEYS),
            "top": [pick(r, TEAM_COND_KEYS) for r in (tc.get("top_by_wilson") or [])[:6]],
        }

    models = []
    for mk, m in M.get("models", {}).items():
        models.append({
            "market": mk, "market_zh": m["market_zh"], "kind": m["kind"],
            "oos_n": m["oos_n"], "base": m["base"], "auc": m["auc"],
            "brier_skill": m["brier_skill"], "p_max": m["p_max"],
            "thresholds": m["thresholds"], "calibration": m["calibration"],
        })
    models.sort(key=lambda m: -m["auc"])

    slate = None
    if SL:
        slate = {
            "dates": SL["generated_for"],
            "n_picks": SL["n_picks"],
            "picks": [{k: v for k, v in p.items() if k != "reliability"}
                      | {"auc": (p.get("reliability") or {}).get("auc")}
                      for p in SL["picks"][:60]],
            "games": [{
                "pk": g["pk"], "date": g["date"], "away": g["away"], "home": g["home"],
                "day_game": g["day_game"], "sp_known": g["sp_known"],
                "home_sp_hand": g["home_sp_hand"], "away_sp_hand": g["away_sp_hand"],
                "home_sp_r9": g["home_sp_r9"], "away_sp_r9": g["away_sp_r9"],
                "total_expect": g["total_expect"],
                "game_markets": g["game_markets"],
                "team_markets": [{"team": t["team"], "is_home": t["is_home"],
                                  "markets": t["markets"]} for t in g["team_markets"]],
            } for g in SL["games"]],
        }

    out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "season": 2026,
        "data": {"games": S["meta"]["games"], "pitches": S["meta"]["pitches"],
                 "through": "2026-08-24"},
        "league": {"bat_all": slim_split(S["league"]["bat_all"]),
                   "vsL": slim_split(S["league"]["vs_LHP"]),
                   "vsR": slim_split(S["league"]["vs_RHP"]),
                   "group": slim_group(S["league"]["by_group"]),
                   "day": slim_split(S["league"]["day"]),
                   "night": slim_split(S["league"]["night"])},
        "conditions": conds,
        "teams": teams,
        "models": models,
        "importance": M.get("importance", {}),
        "slate": slate,
    }
    p = jdump(out, f"{OUTPUT}/app_data.json")
    import os
    log(f"寫出 {p}（{os.path.getsize(p)/1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
