"""模型層：對每個玩法訓練機率模型，用「滾動重訓」產生真正的樣本外預測。

條件挖掘（mine_conditions）容易挑到運氣好的組合；這裡改成：
  1. 每月月初用「該日之前的所有比賽」重新訓練
  2. 只對訓練期之後的比賽做預測 → 全部都是樣本外
  3. 評估 AUC / Brier / 校準表，並統計「模型說 ≥X% 時實際命中率」
     這才是能拿來下注的東西：機率高的那一格實際上到不到 75%
  4. permutation importance 看哪些分項特徵真的有用

輸出 output/models.json（+ data/oos_predictions.parquet）
"""
import argparse
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from common import DATA, OUTPUT, jdump, log
from mine_conditions import TG_MARKETS, G_MARKETS, prep_markets
from predicates import NUM_FEATURES_G, NUM_FEATURES_TG, add_derived

CAT_TG_COLS = ["is_home", "day_game", "my_sp_hand", "op_sp_hand",
               "my_oppSP_profile", "my_sp_profile"]
CAT_G_COLS = ["day_game", "home_sp_hand", "away_sp_hand", "hand_matchup",
              "home_sp_profile", "away_sp_profile"]
REFIT_DATES = ["2026-06-01", "2026-06-15", "2026-07-01", "2026-07-15",
               "2026-08-01", "2026-08-12"]


def design(df, kind):
    nums = [c for c, _ in (NUM_FEATURES_TG if kind == "tg" else NUM_FEATURES_G)
            if c in df.columns]
    cats = CAT_TG_COLS if kind == "tg" else CAT_G_COLS
    X = df[nums].apply(pd.to_numeric, errors="coerce").copy()
    for c in cats:
        if c not in df.columns:
            continue
        s = df[c]
        if s.dtype == bool:
            X[c] = s.astype(float)
        else:
            for v in [x for x in s.dropna().unique() if str(x) != "?"][:6]:
                X[f"{c}={v}"] = (s == v).astype(float)
    X["month"] = pd.to_numeric(df["month"], errors="coerce")
    return X


def walk_forward(df, kind, market, model="gb"):
    """回傳樣本外預測 DataFrame(date, y, p) 與每次重訓的訓練量。"""
    df = df.sort_values("date").reset_index(drop=True)
    X = design(df, kind)
    y = df[market].astype(int).to_numpy()
    dates = df["date"].to_numpy()
    preds = np.full(len(df), np.nan)
    folds = []
    bounds = REFIT_DATES + ["2026-12-31"]
    for i, cut in enumerate(REFIT_DATES):
        nxt = bounds[i + 1]
        tr = dates < cut
        te = (dates >= cut) & (dates < nxt)
        if tr.sum() < 300 or te.sum() == 0:
            continue
        if model == "gb":
            clf = HistGradientBoostingClassifier(
                max_depth=3, max_iter=250, learning_rate=0.05,
                min_samples_leaf=40, l2_regularization=1.0,
                early_stopping=True, validation_fraction=0.15, random_state=7)
            clf.fit(X[tr], y[tr])
        else:
            clf = make_pipeline(StandardScaler(),
                                LogisticRegression(max_iter=2000, C=0.3))
            clf.fit(X[tr].fillna(X[tr].median()), y[tr])
        Xte = X[te] if model == "gb" else X[te].fillna(X[tr].median())
        preds[te] = clf.predict_proba(Xte)[:, 1]
        folds.append({"cut": cut, "train": int(tr.sum()), "test": int(te.sum())})
    out = pd.DataFrame({"date": dates, "y": y, "p": preds})
    if kind == "tg":
        out["team"] = df["team"].to_numpy()
        out["team_zh"] = df["team_zh"].to_numpy()
        out["opp_zh"] = df["opp_zh"].to_numpy()
    out["pk"] = df["pk"].to_numpy()
    return out.dropna(subset=["p"]), folds


def calib_table(oos, edges=(0, .35, .45, .55, .6, .65, .7, .75, .8, 1.01)):
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (oos["p"] >= lo) & (oos["p"] < hi)
        if m.sum() == 0:
            continue
        rows.append({"bucket": f"{lo:.0%}-{hi:.0%}", "n": int(m.sum()),
                     "pred": round(float(oos.loc[m, "p"].mean()), 3),
                     "actual": round(float(oos.loc[m, "y"].mean()), 3)})
    return rows


def threshold_table(oos, thresholds=(0.55, 0.6, 0.65, 0.7, 0.75, 0.8)):
    rows = []
    for t in thresholds:
        m = oos["p"] >= t
        if m.sum() < 10:
            continue
        rate = float(oos.loc[m, "y"].mean())
        rows.append({"thr": t, "n": int(m.sum()), "rate": round(rate, 3),
                     "be_odds": round(1 / rate, 3) if rate > 0 else None})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default="")
    ap.add_argument("--importance", action="store_true")
    args = ap.parse_args()

    tg = pd.read_parquet(f"{DATA}/teamgames.parquet")
    gd = pd.read_parquet(f"{DATA}/gamesds.parquet")
    tg = add_derived(tg[(tg["my_gp"] >= 20) & (tg["op_gp"] >= 20)].copy(), "tg")
    gd = add_derived(prep_markets(gd[(gd["home_gp"] >= 20) & (gd["away_gp"] >= 20)].copy(), "g"), "g")

    jobs = []
    for mk, zh in TG_MARKETS:
        if mk in tg.columns:
            jobs.append(("tg", tg, mk, zh))
    for mk, zh in G_MARKETS:
        if mk in gd.columns:
            jobs.append(("g", gd, mk, zh))
    if args.markets:
        want = set(args.markets.split(","))
        jobs = [j for j in jobs if j[2] in want]

    results = {}
    all_oos = []
    for kind, df, mk, zh in jobs:
        oos, folds = walk_forward(df, kind, mk)
        if oos.empty:
            continue
        y, p = oos["y"].to_numpy(), oos["p"].to_numpy()
        try:
            auc = roc_auc_score(y, p)
        except ValueError:
            auc = float("nan")
        base = float(y.mean())
        res = {
            "market": mk, "market_zh": zh, "kind": kind,
            "oos_n": int(len(oos)), "base": round(base, 4),
            "auc": round(float(auc), 4),
            "brier": round(float(brier_score_loss(y, p)), 4),
            "brier_base": round(float(brier_score_loss(y, np.full_like(p, base))), 4),
            "logloss": round(float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))), 4),
            "calibration": calib_table(oos),
            "thresholds": threshold_table(oos),
            "folds": folds,
            "p_max": round(float(p.max()), 3),
        }
        res["brier_skill"] = round(1 - res["brier"] / res["brier_base"], 4)
        results[mk] = res
        oos = oos.copy()
        oos["market"] = mk
        all_oos.append(oos)
        log(f"{zh:<24} AUC {auc:.3f} Brier技巧 {res['brier_skill']:+.3f} "
            f"基準 {base:.3f} 最高機率 {p.max():.3f} "
            f"| ≥70%格: " + (lambda t: f"{t['n']}場 {t['rate']:.1%}" if t else "無")(
                next((t for t in res["thresholds"] if t["thr"] == 0.7), None)))

    if all_oos:
        pd.concat(all_oos).to_parquet(f"{DATA}/oos_predictions.parquet", index=False)

    # 特徵重要度（挑幾個代表性玩法）
    imp = {}
    if args.importance:
        for kind, df, mk, zh in jobs:
            if mk not in ("win", "over_8.5", "tt_over_3.5", "cover_m15"):
                continue
            d = df.sort_values("date").reset_index(drop=True)
            X, y = design(d, kind), d[mk].astype(int).to_numpy()
            cut = "2026-07-15"
            tr = (d["date"] < cut).to_numpy()
            clf = HistGradientBoostingClassifier(max_depth=3, max_iter=250,
                                                 learning_rate=0.05, min_samples_leaf=40,
                                                 l2_regularization=1.0, random_state=7)
            clf.fit(X[tr], y[tr])
            r = permutation_importance(clf, X[~tr], y[~tr], n_repeats=5,
                                       random_state=7, scoring="roc_auc")
            order = np.argsort(-r.importances_mean)[:15]
            imp[mk] = [{"feature": X.columns[i], "auc_drop": round(float(r.importances_mean[i]), 4)}
                       for i in order]
            log(f"重要特徵 {zh}: " + ", ".join(X.columns[i] for i in order[:6]))

    out = {"season": 2026, "models": results, "importance": imp,
           "refit_dates": REFIT_DATES}
    p = jdump(out, f"{OUTPUT}/models.json")
    log(f"寫出 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
