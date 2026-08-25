"""特徵族群消融實驗 → output/ablation.json

直接回答：「打者對左右投 / 對球種 / 日夜場」這些細分項，到底對預測有沒有用？

做法：用得分模型（Poisson + 滾動重訓）跑三種設定
  1. 全部特徵（baseline）
  2. 拿掉某一族群（drop-one）→ 掉多少代表那一族群的獨立貢獻
  3. 只用「基本盤 + 該族群」（only）→ 那一族群單獨能做到多少

指標：樣本外單邊得分 MAE、以及 over_8.5 / 不讓分的 AUC。
"""
import argparse
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import roc_auc_score

from common import DATA, OUTPUT, jdump, log
from model_markets import REFIT_DATES, design
from model_runs import fit_alpha, market_probs, nb_pmf
from predicates import add_derived

GROUPS = {
    "基本盤（勝率/得失分/Elo/主客/日夜）": [
        "my_win_pct", "op_win_pct", "my_rpg", "op_rpg", "my_rapg", "op_rapg",
        "my_elo", "op_elo", "elo_diff", "rpg_diff", "is_home", "day_game", "month"],
    "先發投手品質": [
        "my_sp_r9", "op_sp_r9", "my_sp_k9", "op_sp_k9", "my_sp_bb9", "op_sp_bb9",
        "my_sp_hr9", "op_sp_hr9", "my_sp_ip_per_start", "op_sp_ip_per_start",
        "my_sp_whiff", "op_sp_whiff", "my_sp_xwoba", "op_sp_xwoba",
        "my_sp_last3_r9", "op_sp_last3_r9", "my_sp_last3_ip", "op_sp_last3_ip",
        "sp_r9_diff", "my_sp_starts", "op_sp_starts"],
    "左右投打對位": [
        "my_woba_vsL", "my_woba_vsR", "op_woba_vsL", "op_woba_vsR",
        "my_bat_vs_oppSP_hand_woba", "op_bat_vs_oppSP_hand_woba",
        "my_sp_woba_vsL", "my_sp_woba_vsR", "op_sp_woba_vsL", "op_sp_woba_vsR",
        "my_sp_woba_vs_us", "op_sp_woba_vs_us",
        "my_woba_vsL_l15", "my_woba_vsR_l15", "op_woba_vsL_l15", "op_woba_vsR_l15",
        "my_sp_hand=L", "my_sp_hand=R", "op_sp_hand=L", "op_sp_hand=R"],
    "球種對位": [
        "my_woba_fast", "my_woba_break", "my_woba_off",
        "op_woba_fast", "op_woba_break",
        "my_bat_vs_oppSP_main_woba", "op_bat_vs_oppSP_main_woba",
        "my_bat_vs_oppSP_2nd_woba", "op_bat_vs_oppSP_2nd_woba",
        "my_oppSP_profile=FB", "my_oppSP_profile=BRK", "my_oppSP_profile=OFF",
        "my_oppSP_profile=BAL", "my_sp_profile=FB", "my_sp_profile=BRK",
        "my_sp_profile=OFF", "my_sp_profile=BAL"],
    "日夜場與主客分項": [
        "my_woba_daypart", "op_woba_daypart", "my_woba_venueside", "op_woba_venueside",
        "my_sp_woba_daypart", "op_sp_woba_daypart",
        "my_sp_woba_venueside", "op_sp_woba_venueside"],
    "牛棚": ["my_bp_r9_14", "op_bp_r9_14", "my_bp_ip14", "my_bp_woba_30d",
             "op_bp_woba_30d"],
    "主審傾向": ["ump_runs_avg", "ump_k_avg", "ump_games"],
    "球場與天氣": ["park_factor", "temp", "wind_speed", "roof",
                   "wind_dir=out", "wind_dir=in", "wind_dir=cross", "wind_dir=none"],
    "近期狀態": ["my_rpg_l10", "op_rpg_l10", "my_rapg_l10", "op_rapg_l10",
                 "my_win_pct_l10", "op_win_pct_l10", "form_diff",
                 "my_woba_l15", "op_woba_l15", "my_runs_std15",
                 "my_xwoba", "op_xwoba", "my_hard_pct", "my_barrel_pct",
                 "my_k_pct", "op_k_pct", "my_bb_pct"],
    "休息與對位熟悉度": ["my_rest", "my_sp_rest", "op_sp_rest", "my_away_streak",
                         "faced_opp_sp", "series_game", "my_sp_velo_delta"],
}
BASE_GROUP = "基本盤（勝率/得失分/Elo/主客/日夜）"

# 精簡版：每個概念只留一兩個欄位，測試「降維後是否比全套更好」
LEAN = {
    "對位精簡": ["my_bat_vs_oppSP_hand_woba", "op_bat_vs_oppSP_hand_woba",
                 "my_sp_woba_vs_us", "op_sp_woba_vs_us"],
    "先發品質精簡": ["sp_r9_diff", "my_sp_r9", "op_sp_r9", "my_sp_k9", "op_sp_k9",
                     "my_sp_ip_per_start", "op_sp_ip_per_start"],
    "球場天氣": ["park_factor", "temp", "wind_speed", "roof"],
    "主審": ["ump_runs_avg", "ump_k_avg"],
    "球種精簡": ["my_bat_vs_oppSP_main_woba", "op_bat_vs_oppSP_main_woba",
                 "my_bat_vs_oppSP_2nd_woba", "op_bat_vs_oppSP_2nd_woba"],
    "牛棚精簡": ["my_bp_r9_14", "op_bp_r9_14"],
    "近期精簡": ["my_rpg_l10", "op_rpg_l10", "my_rapg_l10", "op_rapg_l10",
                 "my_woba_l15", "op_woba_l15"],
}


def walk_forward(tg, X, target="runs", seed=7):
    y = tg[target].astype(float).to_numpy()
    dates = tg["date"].to_numpy()
    mu = np.full(len(tg), np.nan)
    alphas = {}
    bounds = REFIT_DATES + ["2026-12-31"]
    for i, cut in enumerate(REFIT_DATES):
        nxt = bounds[i + 1]
        tr = dates < cut
        te = (dates >= cut) & (dates < nxt)
        if tr.sum() < 400 or te.sum() == 0:
            continue
        m = HistGradientBoostingRegressor(loss="poisson", max_depth=3, max_iter=300,
                                          learning_rate=0.05, min_samples_leaf=40,
                                          l2_regularization=1.0, early_stopping=True,
                                          validation_fraction=0.15, random_state=seed)
        m.fit(X[tr], y[tr])
        mu[te] = m.predict(X[te])
        alphas[cut] = fit_alpha(y[tr], m.predict(X[tr]))
    return mu, alphas


def repeat_eval(tg, X, seeds):
    """多種子重跑，取平均與標準差 —— 用來判斷差異是不是隨機波動。"""
    evs = []
    for sd in seeds:
        mu, al = walk_forward(tg, X, seed=sd)
        evs.append(evaluate(tg, mu, al))
    keys = [k for k in evs[0] if isinstance(evs[0][k], (int, float))]
    out = {k: round(float(np.mean([e[k] for e in evs])), 4) for k in keys}
    out["sd"] = {k: round(float(np.std([e[k] for e in evs])), 4) for k in keys}
    return out


def evaluate(tg, mu, alphas):
    ok = ~np.isnan(mu)
    y = tg["runs"].astype(float).to_numpy()
    mae = float(np.mean(np.abs(y[ok] - mu[ok])))
    d = tg.loc[ok, ["pk", "date", "is_home", "runs"]].copy()
    d["mu"] = mu[ok]
    home = d[d["is_home"]].set_index("pk")
    away = d[~d["is_home"]].set_index("pk")
    common = home.index.intersection(away.index)
    alpha = float(np.median(list(alphas.values()))) if alphas else 0.25
    pa = nb_pmf(away.loc[common, "mu"].to_numpy(), alpha)
    pb = nb_pmf(home.loc[common, "mu"].to_numpy(), alpha)
    P = market_probs(pa, pb, total_lines=(7.5, 8.5, 9.5), team_lines=(3.5, 4.5))
    ra = away.loc[common, "runs"].to_numpy()
    rb = home.loc[common, "runs"].to_numpy()
    tot, diff = ra + rb, ra - rb
    out = {"mae": round(mae, 4), "games": int(len(common))}
    for key, y_true, p in (("over_8.5", tot > 8.5, P["over_8.5"]),
                           ("over_7.5", tot > 7.5, P["over_7.5"]),
                           ("away_win", diff > 0, P["a_win"]),
                           ("away_cover_m15", diff >= 2, P["a_cover_m15"]),
                           ("away_tt_over_3.5", ra > 3.5, P["a_tt_over_3.5"])):
        try:
            out[f"auc_{key}"] = round(float(roc_auc_score(y_true.astype(int), p)), 4)
        except ValueError:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-gp", type=int, default=20)
    ap.add_argument("--seeds", default="7,17,27")
    args = ap.parse_args()
    tg = pd.read_parquet(f"{DATA}/teamgames.parquet")
    tg = add_derived(tg[(tg["my_gp"] >= args.min_gp) & (tg["op_gp"] >= args.min_gp)].copy(), "tg")
    tg = tg.sort_values(["date", "pk"]).reset_index(drop=True)
    Xall = design(tg, "tg")
    cols = list(Xall.columns)
    log(f"資料 {len(tg)} 列、特徵 {len(cols)} 欄")

    # 檢查族群覆蓋率
    assigned = set()
    for g, feats in GROUPS.items():
        have = [c for c in feats if c in cols]
        assigned |= set(have)
        log(f"  {g}: {len(have)}/{len(feats)} 欄可用")
    missing = [c for c in cols if c not in assigned]
    if missing:
        log(f"  未分類欄位（{len(missing)}）：{missing[:12]}")

    seeds = [int(x) for x in args.seeds.split(",")]
    results = {}
    log(f"── 全部特徵（{len(seeds)} 個種子平均）──")
    full = repeat_eval(tg, Xall, seeds)
    results["full"] = full
    log(f"  MAE {full['mae']} | AUC over_8.5 {full.get('auc_over_8.5')} "
        f"away_win {full.get('auc_away_win')}")

    base_cols = [c for c in GROUPS[BASE_GROUP] if c in cols]
    log("── 只有基本盤 ──")
    results["base_only"] = repeat_eval(tg, Xall[base_cols], seeds)
    log(f"  MAE {results['base_only']['mae']} | AUC over_8.5 "
        f"{results['base_only'].get('auc_over_8.5')}")

    drops, onlys = {}, {}
    for g, feats in GROUPS.items():
        have = [c for c in feats if c in cols]
        if not have:
            continue
        # drop-one
        keep = [c for c in cols if c not in have]
        d = repeat_eval(tg, Xall[keep], seeds)
        d["mae_delta"] = round(d["mae"] - full["mae"], 4)
        d["auc_delta_over85"] = round((d.get("auc_over_8.5") or 0) - (full.get("auc_over_8.5") or 0), 4)
        d["auc_delta_win"] = round((d.get("auc_away_win") or 0) - (full.get("auc_away_win") or 0), 4)
        drops[g] = d
        # base + only this group
        if g != BASE_GROUP:
            sel = sorted(set(base_cols) | set(have))
            o = repeat_eval(tg, Xall[sel], seeds)
            o["mae_vs_base"] = round(o["mae"] - results["base_only"]["mae"], 4)
            onlys[g] = o
        log(f"  拿掉「{g}」→ MAE {d['mae']} ({d['mae_delta']:+.4f}) "
            f"AUC over_8.5 {d.get('auc_over_8.5')} ({d['auc_delta_over85']:+.4f})"
            + (f" | 只用基本盤+它 MAE {onlys[g]['mae']} ({onlys[g]['mae_vs_base']:+.4f})"
               if g in onlys else ""))

    # ── 精簡版對照 ──
    variants = {}
    lean_all = list(base_cols)
    for name, feats in LEAN.items():
        have = [c for c in feats if c in cols]
        if not have:
            continue
        sel = sorted(set(base_cols) | set(have))
        v = repeat_eval(tg, Xall[sel], seeds)
        v["mae_vs_base"] = round(v["mae"] - results["base_only"]["mae"], 4)
        v["cols"] = len(sel)
        variants[f"基本盤+{name}"] = v
        lean_all += have
        log(f"  基本盤+{name}（{len(sel)} 欄）→ MAE {v['mae']} ({v['mae_vs_base']:+.4f})")
    lean_all = sorted(set(lean_all))
    v = repeat_eval(tg, Xall[lean_all], seeds)
    v["mae_vs_base"] = round(v["mae"] - results["base_only"]["mae"], 4)
    v["mae_vs_full"] = round(v["mae"] - full["mae"], 4)
    v["cols"] = len(lean_all)
    variants["精簡全套"] = v
    log(f"  精簡全套（{len(lean_all)} 欄，全套是 {len(cols)} 欄）→ MAE {v['mae']} "
        f"（vs 基本盤 {v['mae_vs_base']:+.4f}、vs 全套 {v['mae_vs_full']:+.4f}）")
    variants["_lean_columns"] = lean_all

    out = {"season": 2026, "seeds": seeds, "noise_mae_sd": full["sd"].get("mae"),
           "variants": variants,
           "full": full, "base_only": results["base_only"],
           "drop_one": drops, "base_plus_one": onlys,
           "groups": {g: [c for c in f if c in cols] for g, f in GROUPS.items()},
           "note": "MAE 越低越好；drop-one 的 mae_delta 為正表示拿掉會變差（該族群有貢獻）。"}
    p = jdump(out, f"{OUTPUT}/ablation.json")
    log(f"寫出 {p}")

    noise = full["sd"].get("mae", 0)
    log(f"── 結論排序（依 drop-one 對 MAE 的傷害；種子間標準差 {noise:.4f} 是雜訊尺度）──")
    for g, d in sorted(drops.items(), key=lambda kv: -kv[1]["mae_delta"]):
        sig = "有貢獻" if d["mae_delta"] > 2 * max(noise, 1e-6) else (
            "看不出貢獻" if abs(d["mae_delta"]) <= 2 * max(noise, 1e-6) else "拿掉反而更好")
        log(f"  {g:<28} MAE {d['mae_delta']:+.4f}  over_8.5 AUC {d['auc_delta_over85']:+.4f}  {sig}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
