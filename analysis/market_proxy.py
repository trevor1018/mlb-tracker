"""市場代理測試 → output/market_proxy.json

沒有真實賠率時，最接近真相的做法：自己造一個「莊家代理」。

代理模型只用莊家一定會定價的資訊：球場、氣溫、屋頂、雙方先發本季 R/9、
雙方場均得分/失分、主客場。任何一家運動彩券都不可能漏掉這些。

然後問一個尖銳的問題：
    我們的完整模型（多了 Statcast 分項、對左右投/球種對位、牛棚被打 wOBA、
    近期滾動、球速變化…）有沒有打敗這個代理？

如果有 → 那些細分項真的知道市場不知道的事，值得投資
如果沒有 → 我們只是把已經在盤口裡的資訊重算一遍

定價也改用代理模型的機率（× 返還率），這比「聯盟平均」現實得多。
"""
import argparse
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import log_loss, roc_auc_score

from common import DATA, OUTPUT, jdump, log
from model_markets import REFIT_DATES, design
from model_runs import fit_alpha, market_probs, nb_pmf
from predicates import add_derived

# 莊家一定知道的資訊
PROXY_COLS = ["park_factor", "temp", "roof", "wind_speed",
              "my_sp_r9", "op_sp_r9", "my_sp_ip_per_start", "op_sp_ip_per_start",
              "my_rpg", "op_rpg", "my_rapg", "op_rapg", "is_home", "day_game",
              "my_win_pct", "op_win_pct"]
LINES = (7.5, 8.5, 9.5, 10.5)
TEAM_LINES = (3.5, 4.5, 5.5)


def load_history(seasons, min_gp=20):
    import os
    from common import ROOT
    frames = []
    for sea in seasons:
        f = os.path.join(ROOT, "data", str(sea).strip(), "teamgames.parquet")
        if not os.path.exists(f):
            continue
        h = pd.read_parquet(f)
        h = h[(h["my_gp"] >= min_gp) & (h["op_gp"] >= min_gp)]
        frames.append(add_derived(h.copy(), "tg"))
    return pd.concat(frames, ignore_index=True) if frames else None


def walk_forward(tg, X, seed=7, hist=None, cols=None):
    y = tg["runs"].astype(float).to_numpy()
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
        if hist is not None and len(hist):
            Xh = design(hist, "tg").reindex(columns=X.columns)
            Xfit = pd.concat([Xh, X[tr]], ignore_index=True)
            yfit = np.concatenate([hist["runs"].astype(float).to_numpy(), y[tr]])
        else:
            Xfit, yfit = X[tr], y[tr]
        m.fit(Xfit, yfit)
        mu[te] = m.predict(X[te])
        alphas[cut] = fit_alpha(yfit, m.predict(Xfit))
    return mu, float(np.median(list(alphas.values()))) if alphas else 0.25


def to_pairs(tg, mu):
    d = tg[["pk", "date", "is_home", "runs"]].copy()
    d["mu"] = mu
    d = d.dropna(subset=["mu"])
    home = d[d["is_home"]].set_index("pk")
    away = d[~d["is_home"]].set_index("pk")
    common = home.index.intersection(away.index)
    return pd.DataFrame({
        "pk": common, "date": home.loc[common, "date"].to_numpy(),
        "mu_home": home.loc[common, "mu"].to_numpy(),
        "mu_away": away.loc[common, "mu"].to_numpy(),
        "runs_home": home.loc[common, "runs"].to_numpy(),
        "runs_away": away.loc[common, "runs"].to_numpy()})


def probs_and_truth(pairs, alpha):
    P = market_probs(nb_pmf(pairs["mu_away"].to_numpy(), alpha),
                     nb_pmf(pairs["mu_home"].to_numpy(), alpha),
                     total_lines=LINES, team_lines=TEAM_LINES)
    ra, rb = pairs["runs_away"].to_numpy(), pairs["runs_home"].to_numpy()
    tot, diff = ra + rb, ra - rb
    truth = {}
    for line in LINES:
        truth[f"over_{line}"] = tot > line
        truth[f"under_{line}"] = tot < line
    truth["a_win"], truth["b_win"] = diff > 0, diff < 0
    truth["a_cover_m15"], truth["b_cover_m15"] = diff >= 2, diff <= -2
    truth["a_cover_p15"], truth["b_cover_p15"] = diff >= -1, diff <= 1
    truth["a_cover_m25"], truth["b_cover_m25"] = diff >= 3, diff <= -3
    truth["a_cover_p25"], truth["b_cover_p25"] = diff >= -2, diff <= 2
    for line in TEAM_LINES:
        truth[f"a_tt_over_{line}"], truth[f"b_tt_over_{line}"] = ra > line, rb > line
        truth[f"a_tt_under_{line}"], truth[f"b_tt_under_{line}"] = ra < line, rb < line
    return P, truth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payout", type=float, default=0.90)
    ap.add_argument("--edge", type=float, default=1.05)
    ap.add_argument("--history", default="2024,2025")
    args = ap.parse_args()

    tg = pd.read_parquet(f"{DATA}/teamgames.parquet")
    tg = add_derived(tg[(tg["my_gp"] >= 20) & (tg["op_gp"] >= 20)].copy(), "tg")
    tg = tg.sort_values(["date", "pk"]).reset_index(drop=True)
    Xall = design(tg, "tg")
    proxy_cols = [c for c in PROXY_COLS if c in Xall.columns]
    log(f"完整模型 {len(Xall.columns)} 欄；代理模型 {len(proxy_cols)} 欄：{proxy_cols}")

    past = load_history([x for x in args.history.split(",") if x.strip()]) if args.history else None
    if past is not None:
        log(f"併入過去球季訓練資料：{args.history} 共 {len(past)} 列（兩個模型都併）")
    mu_full, a_full = walk_forward(tg, Xall, hist=past)
    mu_prox, a_prox = walk_forward(tg, Xall[proxy_cols], hist=past)
    ok = ~np.isnan(mu_full) & ~np.isnan(mu_prox)
    y = tg["runs"].astype(float).to_numpy()
    mae_full = float(np.mean(np.abs(y[ok] - mu_full[ok])))
    mae_prox = float(np.mean(np.abs(y[ok] - mu_prox[ok])))
    log(f"樣本外單邊得分 MAE：完整 {mae_full:.4f}｜代理 {mae_prox:.4f}"
        f"（差 {mae_prox - mae_full:+.4f}）")

    pf = to_pairs(tg, mu_full)
    pp = to_pairs(tg, mu_prox)
    common = pf.set_index("pk").index.intersection(pp.set_index("pk").index)
    pf = pf[pf["pk"].isin(common)].sort_values("pk").reset_index(drop=True)
    pp = pp[pp["pk"].isin(common)].sort_values("pk").reset_index(drop=True)
    Pf, truth = probs_and_truth(pf, a_full)
    Pp, _ = probs_and_truth(pp, a_prox)
    log(f"共同場次 {len(pf)}")

    rows = []
    total_stake = total_ret = 0.0
    for mk in Pf:
        yt = truth[mk].astype(int)
        p_full, p_prox = np.clip(Pf[mk], 1e-6, 1 - 1e-6), np.clip(Pp[mk], 1e-6, 1 - 1e-6)
        ll_full = float(log_loss(yt, p_full))
        ll_prox = float(log_loss(yt, p_prox))
        try:
            auc_full = float(roc_auc_score(yt, p_full))
            auc_prox = float(roc_auc_score(yt, p_prox))
        except ValueError:
            continue
        # 用代理當市場：賠率 = (1/代理機率) × 返還率；只在完整模型認為有 edge 時下注
        edge = p_full / p_prox
        sel = edge >= args.edge
        n = int(sel.sum())
        r = {"market": mk, "n_games": int(len(yt)),
             "logloss_full": round(ll_full, 5), "logloss_proxy": round(ll_prox, 5),
             "logloss_gain": round(ll_prox - ll_full, 5),
             "auc_full": round(auc_full, 4), "auc_proxy": round(auc_prox, 4),
             "auc_gain": round(auc_full - auc_prox, 4),
             "bets": n}
        if n >= 25:
            odds = (1 / p_prox[sel]) * args.payout
            hit = float(yt[sel].mean())
            stake = float(n)
            ret = float((yt[sel] * odds).sum())
            r.update({"hit": round(hit, 4),
                      "avg_odds": round(float(odds.mean()), 3),
                      "roi": round(ret / stake - 1, 4),
                      "avg_edge": round(float(edge[sel].mean()), 4)})
            total_stake += stake
            total_ret += ret
        rows.append(r)
    rows.sort(key=lambda r: -(r.get("roi") if r.get("roi") is not None else -9))

    overall = (total_ret / total_stake - 1) if total_stake else None
    log(f"── 用代理當市場定價（返還率 {args.payout:.0%}、只押 edge≥{args.edge}）──")
    log(f"整體：{int(total_stake)} 注，ROI {overall:+.1%}" if overall is not None else "整體：無足夠樣本")
    for r in rows[:14]:
        if r.get("roi") is None:
            continue
        log(f"  {r['market']:<16} {r['bets']:>4}注 命中 {r['hit']:.1%} "
            f"平均賠率 {r['avg_odds']:.2f} ROI {r['roi']:+.1%} | "
            f"logloss 進步 {r['logloss_gain']:+.5f} AUC 進步 {r['auc_gain']:+.4f}")

    gains = [r["logloss_gain"] for r in rows]
    log(f"── 完整模型 vs 代理模型：logloss 平均進步 {np.mean(gains):+.5f}、"
        f"{sum(1 for g in gains if g > 0)}/{len(gains)} 個盤口有進步 ──")

    out = {"payout": args.payout, "edge_threshold": args.edge,
           "proxy_cols": proxy_cols, "n_full_cols": len(Xall.columns),
           "mae": {"full": round(mae_full, 4), "proxy": round(mae_prox, 4),
                   "gain": round(mae_prox - mae_full, 4)},
           "overall_roi": None if overall is None else round(overall, 4),
           "overall_bets": int(total_stake),
           "markets": rows,
           "logloss_gain_mean": round(float(np.mean(gains)), 5),
           "markets_improved": int(sum(1 for g in gains if g > 0)),
           "markets_total": len(gains),
           "note": ("代理模型只用莊家一定會定價的資訊（球場/天氣/先發 R9/球隊得失分/主客）。"
                    "完整模型多出來的是 Statcast 分項、對位、牛棚被打 wOBA、滾動狀態等。"
                    "ROI 以代理機率當公正賠率、再乘返還率計算 —— 真實莊家比這個代理更精準，"
                    "所以這裡的 ROI 是樂觀上限。")}
    p = jdump(out, f"{OUTPUT}/market_proxy.json")
    log(f"寫出 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
