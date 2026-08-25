"""得分期望值模型：先預測「每一隊這場會得幾分」，再由分布推出所有玩法機率。

為什麼這樣比較好：
  直接對「大分 8.5」訓練一個分類器，等於把 8.5 這條線以外的資訊全部丟掉；
  而且每條線各訓練一個模型，彼此還會不一致（例如 P(>8.5) < P(>9.5)）。
  改成先估得分期望值 μ，再用負二項分布（棒球得分過度分散，Var>μ）展開成
  完整分布，就能一次算出：全場大小分各線、單隊大小分各線、不讓分、讓分 1.5/2.5、
  前 5 局大小分…而且全部互相一致。

流程
  1. HistGradientBoostingRegressor(loss="poisson") 預測單邊得分（全場 & 前5局）
  2. 用訓練期殘差估過度分散係數 α（Var = μ + αμ²）→ 負二項分布
  3. 兩邊分布做卷積 → 總分分布、勝負機率、分差機率
  4. 滾動重訓（只預測訓練期之後的比賽）→ 全部都是樣本外
  5. 與直接分類器比較 AUC / Brier

輸出 output/models_runs.json、data/run_preds.parquet
"""
import argparse
import sys

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import brier_score_loss, roc_auc_score

from common import DATA, OUTPUT, jdump, log
from model_markets import REFIT_DATES, design
from predicates import add_derived

MAXR = 25  # 得分分布截斷


def fit_alpha(y, mu):
    """動差法估過度分散：Var(y) = mu + alpha*mu^2"""
    resid2 = (y - mu) ** 2
    num = float(np.mean(resid2 - mu))
    den = float(np.mean(mu ** 2))
    return max(num / den, 1e-4) if den > 0 else 0.05


def nb_pmf(mu, alpha, maxr=MAXR):
    """負二項 PMF 矩陣：(n, maxr+1)"""
    mu = np.clip(np.asarray(mu, float), 0.05, None)
    r = 1.0 / alpha
    p = r / (r + mu)
    ks = np.arange(maxr + 1)
    return stats.nbinom.pmf(ks[None, :], r, p[:, None])


def market_probs(pmf_a, pmf_b, total_lines=(6.5, 7.5, 8.5, 9.5, 10.5, 11.5),
                 team_lines=(2.5, 3.5, 4.5, 5.5, 6.5)):
    """由兩邊得分分布推出各玩法機率。a=客隊, b=主隊（或 my/opp）。"""
    n, K = pmf_a.shape
    # 總分分布：卷積
    tot = np.zeros((n, 2 * K - 1))
    for k in range(K):
        tot[:, k:k + K] += pmf_a[:, [k]] * pmf_b
    tot_cum = np.cumsum(tot, axis=1)

    def p_total_over(line):
        idx = int(np.floor(line))
        return 1 - tot_cum[:, min(idx, tot_cum.shape[1] - 1)]

    # 勝負與分差：P(a - b = d)
    diff = np.zeros((n, 2 * K - 1))   # index d+K-1
    for k in range(K):
        diff[:, (K - 1) + k - np.arange(K)] += 0  # placeholder（下面用矩陣做）
    # 用外積一次算完
    outer = pmf_a[:, :, None] * pmf_b[:, None, :]        # (n, K, K)
    ii = np.arange(K)[:, None] - np.arange(K)[None, :]    # a-b
    for d in range(-(K - 1), K):
        diff[:, d + K - 1] = outer[:, ii == d].sum(axis=1)
    d_idx = lambda d: d + K - 1

    out = {}
    for line in total_lines:
        out[f"over_{line}"] = p_total_over(line)
        out[f"under_{line}"] = 1 - p_total_over(line)
    # a 方（客/我）視角
    out["a_win"] = diff[:, d_idx(1):].sum(axis=1)
    out["b_win"] = diff[:, :d_idx(0)].sum(axis=1)
    out["a_cover_m15"] = diff[:, d_idx(2):].sum(axis=1)
    out["b_cover_m15"] = diff[:, :d_idx(-1)].sum(axis=1)
    out["a_cover_p15"] = 1 - diff[:, :d_idx(-1)].sum(axis=1)
    out["b_cover_p15"] = 1 - diff[:, d_idx(2):].sum(axis=1)
    out["a_cover_m25"] = diff[:, d_idx(3):].sum(axis=1)
    out["b_cover_m25"] = diff[:, :d_idx(-2)].sum(axis=1)
    out["a_cover_p25"] = 1 - diff[:, :d_idx(-2)].sum(axis=1)
    out["b_cover_p25"] = 1 - diff[:, d_idx(3):].sum(axis=1)
    # 單隊大小分
    ca, cb = np.cumsum(pmf_a, axis=1), np.cumsum(pmf_b, axis=1)
    for line in team_lines:
        i = int(np.floor(line))
        out[f"a_tt_over_{line}"] = 1 - ca[:, i]
        out[f"b_tt_over_{line}"] = 1 - cb[:, i]
        out[f"a_tt_under_{line}"] = ca[:, i]
        out[f"b_tt_under_{line}"] = cb[:, i]
    return out


FEATURE_SETS = {
    # 16 欄「莊家一定知道」的資訊（market_proxy 實測樣本外 MAE 最好）
    "proxy": ["park_factor", "temp", "roof", "wind_speed",
              "my_sp_r9", "op_sp_r9", "my_sp_ip_per_start", "op_sp_ip_per_start",
              "my_rpg", "op_rpg", "my_rapg", "op_rapg", "is_home", "day_game",
              "my_win_pct", "op_win_pct"],
}


def walk_forward_runs(tg, target="runs", hist=None, features=None):
    """回傳每個 team-game 的預測得分（樣本外）與各折 alpha。

    hist：可選的「過去球季」資料（例如 2024+2025），會併進每一折的訓練集。
    因為是過去的球季，不會造成資訊洩漏。
    """
    tg = tg.sort_values(["date", "pk"]).reset_index(drop=True)
    X = design(tg, "tg")
    if features:
        keep = [c for c in features if c in X.columns]
        X = X[keep]
    y = tg[target].astype(float).to_numpy()
    dates = tg["date"].to_numpy()
    Xh = yh = None
    if hist is not None and len(hist):
        Xh = design(hist, "tg").reindex(columns=X.columns)
        yh = hist[target].astype(float).to_numpy()
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
                                          validation_fraction=0.15, random_state=7)
        if Xh is not None:
            Xfit = pd.concat([Xh, X[tr]], ignore_index=True)
            yfit = np.concatenate([yh, y[tr]])
        else:
            Xfit, yfit = X[tr], y[tr]
        m.fit(Xfit, yfit)
        mu[te] = m.predict(X[te])
        alphas[cut] = fit_alpha(yfit, m.predict(Xfit))
    return tg, mu, alphas


def pair_up(tg, mu, alpha_by_cut, runs_col="runs"):
    """把 team-game 的預測收成「一場一列（客/主）」，方便做卷積。"""
    d = tg[["pk", "date", "team", "opp", "is_home", "runs", "runs_f5"]].copy()
    d["y"] = d[runs_col]
    d["mu"] = mu
    d = d.dropna(subset=["mu"])
    home = d[d["is_home"]].set_index("pk")
    away = d[~d["is_home"]].set_index("pk")
    common = home.index.intersection(away.index)
    out = pd.DataFrame({
        "pk": common,
        "date": home.loc[common, "date"].to_numpy(),
        "mu_home": home.loc[common, "mu"].to_numpy(),
        "mu_away": away.loc[common, "mu"].to_numpy(),
        "runs_home": home.loc[common, "y"].to_numpy(),
        "runs_away": away.loc[common, "y"].to_numpy(),
        "home_team": home.loc[common, "team"].to_numpy(),
        "away_team": away.loc[common, "team"].to_numpy(),
    })
    # 每列取所屬折的 alpha（用日期找最近的重訓點）
    cuts = sorted(alpha_by_cut)
    def a_for(dt):
        best = alpha_by_cut[cuts[0]]
        for c in cuts:
            if dt >= c:
                best = alpha_by_cut[c]
        return best
    out["alpha"] = [a_for(x) for x in out["date"]]
    return out


def evaluate(pairs, label=""):
    """算出各玩法機率並與實際結果比較。"""
    alpha = float(np.median(pairs["alpha"]))
    pa = nb_pmf(pairs["mu_away"].to_numpy(), alpha)
    pb = nb_pmf(pairs["mu_home"].to_numpy(), alpha)
    P = market_probs(pa, pb)
    ra, rb = pairs["runs_away"].to_numpy(), pairs["runs_home"].to_numpy()
    tot = ra + rb
    diff = ra - rb

    truth = {}
    for line in (6.5, 7.5, 8.5, 9.5, 10.5, 11.5):
        truth[f"over_{line}"] = tot > line
        truth[f"under_{line}"] = tot < line
    truth["a_win"] = diff > 0
    truth["b_win"] = diff < 0
    truth["a_cover_m15"] = diff >= 2
    truth["b_cover_m15"] = diff <= -2
    truth["a_cover_p15"] = diff >= -1
    truth["b_cover_p15"] = diff <= 1
    truth["a_cover_m25"] = diff >= 3
    truth["b_cover_m25"] = diff <= -3
    truth["a_cover_p25"] = diff >= -2
    truth["b_cover_p25"] = diff <= 2
    for line in (2.5, 3.5, 4.5, 5.5, 6.5):
        truth[f"a_tt_over_{line}"] = ra > line
        truth[f"b_tt_over_{line}"] = rb > line
        truth[f"a_tt_under_{line}"] = ra < line
        truth[f"b_tt_under_{line}"] = rb < line

    rows = []
    for k, p in P.items():
        y = truth[k].astype(int)
        if y.sum() in (0, len(y)):
            continue
        base = float(y.mean())
        try:
            auc = float(roc_auc_score(y, p))
        except ValueError:
            continue
        br = float(brier_score_loss(y, np.clip(p, 0, 1)))
        br0 = float(brier_score_loss(y, np.full_like(p, base)))
        thr = []
        for t in (0.6, 0.65, 0.7, 0.75, 0.8):
            m = p >= t
            if m.sum() >= 10:
                thr.append({"thr": t, "n": int(m.sum()),
                            "rate": round(float(y[m].mean()), 3),
                            "be_odds": round(1 / max(float(y[m].mean()), 1e-6), 2)})
        rows.append({"market": k, "n": int(len(y)), "base": round(base, 4),
                     "auc": round(auc, 4), "brier": round(br, 4),
                     "brier_skill": round(1 - br / br0, 4),
                     "p_max": round(float(p.max()), 3), "thresholds": thr})
    # 存成長表：pk / date / market / p / y（給回測用）
    long_rows = []
    ren = {"a": "away", "b": "home"}
    for k, p in P.items():
        name = k
        if k[:2] in ("a_", "b_"):
            name = ren[k[0]] + k[1:]
        long_rows.append(pd.DataFrame({
            "pk": pairs["pk"].to_numpy(), "date": pairs["date"].to_numpy(),
            "market": name, "p": p, "y": truth[k].astype(int)}))
    long_df = pd.concat(long_rows, ignore_index=True) if long_rows else pd.DataFrame()

    rows.sort(key=lambda r: -r["auc"])
    log(f"── {label} 樣本外 {len(pairs)} 場、過度分散 α={alpha:.3f} ──")
    for r in rows[:14]:
        t70 = next((t for t in r["thresholds"] if t["thr"] == 0.7), None)
        log(f"  {r['market']:<16} AUC {r['auc']:.3f} Brier技巧 {r['brier_skill']:+.3f} "
            f"基準 {r['base']:.3f} 最高 {r['p_max']:.2f}"
            + (f" | ≥70%: {t70['n']}場 {t70['rate']:.1%}" if t70 else ""))
    return rows, alpha, P, long_df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-gp", type=int, default=20)
    ap.add_argument("--features", default="full",
                    help="full = 全部欄位；proxy = 16 欄精簡（樣本外 MAE 較好）")
    ap.add_argument("--history", default="2024,2025",
                    help="併進訓練集的過去球季（實測平均 AUC +0.011）；設成空字串則只用本季")
    args = ap.parse_args()

    tg = pd.read_parquet(f"{DATA}/teamgames.parquet")
    tg = add_derived(tg[(tg["my_gp"] >= args.min_gp) & (tg["op_gp"] >= args.min_gp)].copy(), "tg")
    hist = None
    if args.history:
        import os
        from common import ROOT
        frames = []
        for sea in args.history.split(","):
            f = os.path.join(ROOT, "data", sea.strip(), "teamgames.parquet")
            if os.path.exists(f):
                h = pd.read_parquet(f)
                h = h[(h["my_gp"] >= args.min_gp) & (h["op_gp"] >= args.min_gp)]
                frames.append(add_derived(h.copy(), "tg"))
        if frames:
            hist = pd.concat(frames, ignore_index=True)
            log(f"併入過去球季訓練資料：{args.history} 共 {len(hist)} 列")
    log(f"資料 {len(tg)} 列")

    feats = FEATURE_SETS.get(args.features)
    if feats:
        log(f"使用特徵組：{args.features}（{len(feats)} 欄）")
    tg9, mu9, a9 = walk_forward_runs(tg, "runs", hist=hist, features=feats)
    ok = ~np.isnan(mu9)
    mae = float(np.mean(np.abs(tg9["runs"].to_numpy()[ok] - mu9[ok])))
    base_mae = float(np.mean(np.abs(tg9["runs"].to_numpy()[ok] - tg9["runs"].to_numpy()[ok].mean())))
    log(f"全場得分模型：樣本外 MAE {mae:.3f}（只押平均 {base_mae:.3f}）")
    pairs9 = pair_up(tg9, mu9, a9)
    rows9, alpha9, _, long9 = evaluate(pairs9, "全場")
    long9.to_parquet(f"{DATA}/oos_market_probs.parquet", index=False)
    log(f"寫出 data/oos_market_probs.parquet（{len(long9):,} 列）")

    tg5, mu5, a5 = walk_forward_runs(tg, "runs_f5", hist=hist, features=feats)
    ok5 = ~np.isnan(mu5)
    mae5 = float(np.mean(np.abs(tg5["runs_f5"].to_numpy()[ok5] - mu5[ok5])))
    log(f"前5局得分模型：樣本外 MAE {mae5:.3f}")
    pairs5 = pair_up(tg5, mu5, a5, runs_col="runs_f5")
    alpha5 = float(np.median(pairs5["alpha"]))
    pa5 = nb_pmf(pairs5["mu_away"].to_numpy(), alpha5)
    pb5 = nb_pmf(pairs5["mu_home"].to_numpy(), alpha5)
    P5 = market_probs(pa5, pb5, total_lines=(3.5, 4.5, 5.5), team_lines=(1.5, 2.5, 3.5))
    f5_tot = pairs5["runs_away"].to_numpy() + pairs5["runs_home"].to_numpy()
    rows5 = []
    for line in (3.5, 4.5, 5.5):
        for side, key in ((f"f5_over_{line}", f"over_{line}"), (f"f5_under_{line}", f"under_{line}")):
            p = P5[key]
            y = ((f5_tot > line) if "over" in side else (f5_tot < line)).astype(int)
            base = float(y.mean())
            rows5.append({"market": side, "n": int(len(y)), "base": round(base, 4),
                          "auc": round(float(roc_auc_score(y, p)), 4),
                          "brier_skill": round(1 - brier_score_loss(y, p) / brier_score_loss(y, np.full_like(p, base)), 4),
                          "p_max": round(float(p.max()), 3),
                          "thresholds": [{"thr": t, "n": int((p >= t).sum()),
                                          "rate": round(float(y[p >= t].mean()), 3)}
                                         for t in (0.6, 0.65, 0.7, 0.75) if (p >= t).sum() >= 10]})
    log("── 前5局 ──")
    for r in rows5:
        log(f"  {r['market']:<16} AUC {r['auc']:.3f} Brier技巧 {r['brier_skill']:+.3f} 基準 {r['base']:.3f}")

    # 存下逐場預測，供比較與回測
    pairs9.to_parquet(f"{DATA}/run_preds.parquet", index=False)
    out = {"season": 2026, "alpha_full": alpha9, "alpha_f5": alpha5,
           "mae": {"full": round(mae, 3), "full_baseline": round(base_mae, 3),
                   "f5": round(mae5, 3)},
           "markets_full": rows9, "markets_f5": rows5,
           "refit_dates": REFIT_DATES}
    p = jdump(out, f"{OUTPUT}/models_runs.json")
    log(f"寫出 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
