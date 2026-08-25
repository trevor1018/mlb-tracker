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
    try:
        MR = jload(f"{OUTPUT}/models_runs.json")
    except Exception:
        MR = None
    try:
        BT = jload(f"{OUTPUT}/backtest.json")
    except Exception:
        BT = None
    try:
        OR = jload(f"{OUTPUT}/over_rule.json")
    except Exception:
        OR = None
    try:
        AB = jload(f"{OUTPUT}/ablation.json")
    except Exception:
        AB = None
    try:
        MS = jload(f"{OUTPUT}/multiseason.json")
    except Exception:
        MS = None

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
            "chance_line_single": tc.get("chance_max_rate_p95_single"),
            "best": pick(tc.get("best") or {}, TEAM_COND_KEYS),
            "top": [pick(r, TEAM_COND_KEYS) for r in (tc.get("top_by_wilson") or [])[:6]],
            "best_single": pick(tc.get("best_single") or {}, TEAM_COND_KEYS),
            "top_single": [pick(r, TEAM_COND_KEYS) for r in (tc.get("top_single") or [])[:6]],
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
            "dates": SL["generated_for"], "n_picks": SL["n_picks"],
            "engine": SL.get("engine"), "payout": SL.get("payout_assumed"),
            "picks": SL["picks"][:80],
            "games": [{
                "pk": g["pk"], "date": g["date"], "away": g["away"], "home": g["home"],
                "day_game": g["day_game"], "sp_known": g["sp_known"],
                "home_sp_hand": g["home_sp_hand"], "away_sp_hand": g["away_sp_hand"],
                "home_sp_r9": g["home_sp_r9"], "away_sp_r9": g["away_sp_r9"],
                "park_factor": g.get("park_factor"),
                "mu_home": g.get("mu_home"), "mu_away": g.get("mu_away"),
                "mu_total": g.get("mu_total"),
                "markets": g["markets"][:14],
                "matchup_detail": g.get("matchup_detail"),
                "conditions": g.get("conditions", [])[:4],
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
        "run_model": None if not MR else {
            "alpha_full": MR["alpha_full"], "alpha_f5": MR["alpha_f5"],
            "mae": MR["mae"],
            "markets": [{k: v for k, v in r.items() if k != "brier"}
                        for r in MR["markets_full"][:24]],
            "markets_f5": MR["markets_f5"],
        },
        "backtest": None if not BT else {
            "oos_range": BT["oos_range"], "payout": BT["payout_assumed"],
            "note": BT["note"],
            "two_stage": BT.get("two_stage"),
            "payouts": BT.get("payouts"),
            "singles": BT["singles"][:60],
            "parlays": {k: {kk: vv for kk, vv in v.items() if kk != "log"}
                        for k, v in BT["parlays"].items()},
            "parlay_log": {k: v.get("log", [])[-6:] for k, v in BT["parlays"].items()},
        },
        "over_rule": None if not OR else {
            "oos_range": OR["oos_range"], "payout": OR["payout"],
            "lines": OR["lines"], "by_park": OR.get("by_park"),
            "park_control": OR.get("park_control"), "caveat": OR.get("caveat"),
            "recommended": OR.get("recommended", []),
        },
        "ablation": None if not AB else {
            "noise_mae_sd": AB["noise_mae_sd"], "seeds": AB["seeds"],
            "full": {k: v for k, v in AB["full"].items() if k != "sd"},
            "base_only": {k: v for k, v in AB["base_only"].items() if k != "sd"},
            "drop_one": {g: {"mae": d["mae"], "mae_delta": d["mae_delta"],
                             "auc_over85": d.get("auc_over_8.5"),
                             "auc_delta_over85": d["auc_delta_over85"],
                             "auc_delta_win": d["auc_delta_win"],
                             "n_features": len(AB["groups"].get(g, []))}
                         for g, d in AB["drop_one"].items()},
            "base_plus_one": {g: {"mae": o["mae"], "mae_vs_base": o["mae_vs_base"]}
                              for g, o in AB["base_plus_one"].items()},
        },
        "multiseason": None if not MS else {
            "train": MS["train_seasons"], "test": MS["test_season"],
            "teamgame": None if not MS.get("teamgame") else {
                "candidates": MS["teamgame"]["candidates"],
                "holds": MS["teamgame"]["holds"],
                "rows": [{k: v for k, v in r.items() if k != "pred_names"}
                         for r in MS["teamgame"]["rows"][:40]]},
            "game": None if not MS.get("game") else {
                "candidates": MS["game"]["candidates"],
                "holds": MS["game"]["holds"],
                "rows": [{k: v for k, v in r.items() if k != "pred_names"}
                         for r in MS["game"]["rows"][:40]]},
        },
        "importance": M.get("importance", {}),
        "slate": slate,
    }
    p = jdump(out, f"{OUTPUT}/app_data.json")
    import os
    log(f"寫出 {p}（{os.path.getsize(p)/1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
