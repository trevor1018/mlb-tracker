"""對「還沒開打的場次」產生預測 → output/slate.json

主引擎＝得分期望值模型（model_runs 的同一套）：
  預測雙方得分 μ → 負二項分布 → 卷積 → 所有玩法機率（互相一致）

排序依據不是原始機率，而是**相對基準率的優勢** edge = p / 基準率。
因為回測顯示：押基準率本來就高的盤（例如受讓 2.5）命中率再高也賺不到，
台彩會把賠率壓到 1.2 附近；真正能賺的是 edge 明顯大於 1/返還率（≈1.11）的盤。

另外附上條件比對（Tier A/B 今天有沒有觸發）與該玩法在回測中的實際表現。
"""
import argparse
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from backtest import ALLOWED, DEFAULT_PAYOUT, zh
from common import DATA, OUTPUT, TEAM_ZH, jdump, jload, log
from mine_conditions import G_MARKETS, TG_MARKETS, prep_markets
from model_markets import design
from model_runs import fit_alpha, market_probs, nb_pmf
from predicates import add_derived, apply_specs, build_predicates_full

TW_TOTAL_LINES = (7.5, 8.5, 9.5, 10.5)
TW_TEAM_LINES = (3.5, 4.5, 5.5)


def pending_to_tg(pend):
    rows = []
    for _, g in pend.iterrows():
        for side, other in (("home", "away"), ("away", "home")):
            r = {"pk": g["pk"], "date": g["date"], "month": g["month"], "dow": g["dow"],
                 "day_game": g["day_game"], "venue": g["venue"], "temp": g["temp"],
                 "series_game": g["series_game"], "series_len": g["series_len"],
                 "park_factor": g.get("park_factor"), "wind_speed": g.get("wind_speed"),
                 "wind_dir": g.get("wind_dir"), "roof": g.get("roof"),
                 "team": g[f"{side}_team"], "opp": g[f"{other}_team"],
                 "team_zh": g[f"{side}_team_zh"], "opp_zh": g[f"{other}_team_zh"],
                 "is_home": side == "home", "sp_known": g.get("sp_known", False),
                 "faced_opp_sp": g.get(f"{side}_faced_opp_sp", 0)}
            for k, v in g.items():
                if isinstance(k, str) and k.startswith(side + "_"):
                    r["my_" + k[len(side) + 1:]] = v
                elif isinstance(k, str) and k.startswith(other + "_"):
                    r["op_" + k[len(other) + 1:]] = v
            rows.append(r)
    return pd.DataFrame(rows)


def fit_run_model(hist, fut, target):
    X = design(hist, "tg")
    Xf = design(fut, "tg").reindex(columns=X.columns)
    y = hist[target].astype(float).to_numpy()
    m = HistGradientBoostingRegressor(loss="poisson", max_depth=3, max_iter=300,
                                      learning_rate=0.05, min_samples_leaf=40,
                                      l2_regularization=1.0, early_stopping=True,
                                      validation_fraction=0.15, random_state=7)
    m.fit(X, y)
    alpha = fit_alpha(y, m.predict(X))
    return m.predict(Xf), alpha


def fired_conditions(fut, kind, hist, conds, limit=8):
    names, labels, M, specs = build_predicates_full(hist, kind)
    spec_by_name = {s["name"]: s for s in specs}
    masks = apply_specs(fut, specs)
    out = [[] for _ in range(len(fut))]
    for c in conds:
        pn = c.get("pred_names") or []
        if not pn or any(p not in spec_by_name for p in pn):
            continue
        m = np.ones(len(fut), bool)
        for p in pn:
            m &= masks[p]
        for i in np.nonzero(m)[0]:
            if len(out[i]) < limit:
                out[i].append({"market": c["market"], "market_zh": c["market_zh"],
                               "label": c["label"], "rate": c["rate"], "n": c["n"],
                               "base": c["base"], "tier": c.get("tier", "?"),
                               "lift": c.get("lift") or (round(c["rate"] / c["base"], 3)
                                                         if c.get("base") else None)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payout", type=float, default=DEFAULT_PAYOUT)
    ap.add_argument("--min-edge", type=float, default=1.03)
    args = ap.parse_args()

    tg = pd.read_parquet(f"{DATA}/teamgames.parquet")
    gd = pd.read_parquet(f"{DATA}/gamesds.parquet")
    pend = pd.read_parquet(f"{DATA}/pending.parquet")
    tg = add_derived(tg[(tg["my_gp"] >= 20) & (tg["op_gp"] >= 20)].copy(), "tg")
    gd = add_derived(prep_markets(gd[(gd["home_gp"] >= 20) & (gd["away_gp"] >= 20)].copy(), "g"), "g")
    people = jload(f"{DATA}/people.json")
    pend_g = add_derived(pend.copy(), "g")
    pend_tg = add_derived(pending_to_tg(pend), "tg")
    log(f"未開打 {len(pend)} 場（{pend['date'].min()} ~ {pend['date'].max()}）")

    # ── 得分模型 ──
    mu9, alpha9 = fit_run_model(tg, pend_tg, "runs")
    mu5, alpha5 = fit_run_model(tg, pend_tg, "runs_f5")
    pend_tg = pend_tg.copy()
    pend_tg["mu9"] = mu9
    pend_tg["mu5"] = mu5
    log(f"得分模型：α(全場)={alpha9:.3f}, α(前5局)={alpha5:.3f}, "
        f"μ 範圍 {mu9.min():.2f}~{mu9.max():.2f}")

    home = pend_tg[pend_tg["is_home"]].set_index("pk")
    away = pend_tg[~pend_tg["is_home"]].set_index("pk")
    pks = [pk for pk in pend_g["pk"] if pk in home.index and pk in away.index]
    mu_h = home.loc[pks, "mu9"].to_numpy()
    mu_a = away.loc[pks, "mu9"].to_numpy()
    P = market_probs(nb_pmf(mu_a, alpha9), nb_pmf(mu_h, alpha9),
                     total_lines=TW_TOTAL_LINES, team_lines=TW_TEAM_LINES)
    P5 = market_probs(nb_pmf(away.loc[pks, "mu5"].to_numpy(), alpha5),
                      nb_pmf(home.loc[pks, "mu5"].to_numpy(), alpha5),
                      total_lines=(4.5,), team_lines=(1.5,))
    ren = {"a": "away", "b": "home"}
    probs = {}
    for k, v in P.items():
        probs[ren[k[0]] + k[1:] if k[:2] in ("a_", "b_") else k] = v
    probs["f5_over_4.5"] = P5["over_4.5"]
    probs["f5_under_4.5"] = P5["under_4.5"]

    # ── 歷史基準率與回測表現 ──
    try:
        BT = jload(f"{OUTPUT}/backtest.json")
        bases = BT["bases"]
        bt_rows = {(r["market"], r["thr"]): r for r in BT["singles"]}
    except Exception:
        bases, bt_rows = {}, {}

    def bt_for(market, p):
        best = None
        for thr in (0.8, 0.75, 0.7, 0.65, 0.6):
            if p >= thr and (market, thr) in bt_rows:
                best = bt_rows[(market, thr)]
                break
        return best

    # ── 條件觸發 ──
    try:
        C = jload(f"{OUTPUT}/conditions.json")
        tg_conds = C["teamgame"]["tierA"] + C["teamgame"]["tierB"]
        g_conds = C["game"]["tierA"] + C["game"]["tierB"]
    except Exception:
        tg_conds, g_conds = [], []
    # 跨季驗證存活的條件標成 tier "跨季"（最值得看的一批）
    try:
        MS = jload(f"{OUTPUT}/multiseason.json")
        for key, bucket in (("teamgame", tg_conds), ("game", g_conds)):
            for r in (MS.get(key) or {}).get("rows", []):
                if not r.get("holds"):
                    continue
                bucket.insert(0, {"market": r["market"], "market_zh": r["market_zh"],
                                  "label": r["label"], "rate": r["test_rate"],
                                  "n": r["test_n"], "base": r["test_base"],
                                  "tier": "跨季", "pred_names": r.get("pred_names", []),
                                  "be_odds": r["be_odds"], "lift": r["test_lift"]})
        log(f"納入跨季存活條件：隊伍 {sum(1 for c in tg_conds if c.get('tier')=='跨季')}、"
            f"全場 {sum(1 for c in g_conds if c.get('tier')=='跨季')}")
    except Exception as e:
        log(f"讀不到 multiseason.json（{e}）")
    tg_fired = fired_conditions(pend_tg, "tg", tg, tg_conds) if tg_conds else [[]] * len(pend_tg)
    g_fired = fired_conditions(pend_g, "g", gd, g_conds) if g_conds else [[]] * len(pend_g)
    tg_pos = {(r["pk"], r["team"]): i for i, (_, r) in enumerate(pend_tg.iterrows())}
    g_pos = {r["pk"]: i for i, (_, r) in enumerate(pend_g.iterrows())}

    games, picks = [], []
    gmeta = pend_g.set_index("pk")
    for gi, pk in enumerate(pks):
        g = gmeta.loc[pk]
        h_zh, a_zh = g["home_team_zh"], g["away_team_zh"]
        entry = {
            "pk": int(pk), "date": g["date"],
            "away": a_zh, "home": h_zh,
            "day_game": bool(g["day_game"]),
            "sp_known": bool(g.get("sp_known", False)),
            "home_sp_hand": g.get("home_sp_hand"), "away_sp_hand": g.get("away_sp_hand"),
            "home_sp_r9": None if pd.isna(g.get("home_sp_r9")) else round(float(g["home_sp_r9"]), 2),
            "away_sp_r9": None if pd.isna(g.get("away_sp_r9")) else round(float(g["away_sp_r9"]), 2),
            "park_factor": None if pd.isna(g.get("park_factor")) else round(float(g["park_factor"]), 2),
            "matchup_detail": {
                side: {
                    "sp_name": ((people.get(str(int(g[f"{side}_sp_id"]))) or {}).get("name")
                                if pd.notna(g.get(f"{side}_sp_id")) else None),
                    "sp_hand": g.get(f"{side}_sp_hand"),
                    "sp_profile": g.get(f"{side}_sp_profile"),
                    "sp_r9": (None if pd.isna(g.get(f"{side}_sp_r9"))
                              else round(float(g[f"{side}_sp_r9"]), 2)),
                    "sp_xwoba": (None if pd.isna(g.get(f"{side}_sp_xwoba"))
                                 else round(float(g[f"{side}_sp_xwoba"]), 3)),
                    "sp_woba_vsL": (None if pd.isna(g.get(f"{side}_sp_woba_vsL"))
                                    else round(float(g[f"{side}_sp_woba_vsL"]), 3)),
                    "sp_woba_vsR": (None if pd.isna(g.get(f"{side}_sp_woba_vsR"))
                                    else round(float(g[f"{side}_sp_woba_vsR"]), 3)),
                    "bat_vs_opp_hand": (None if pd.isna(g.get(f"{side}_bat_vs_oppSP_hand_woba"))
                                        else round(float(g[f"{side}_bat_vs_oppSP_hand_woba"]), 3)),
                    "bat_vs_opp_2nd": (None if pd.isna(g.get(f"{side}_bat_vs_oppSP_2nd_woba"))
                                       else round(float(g[f"{side}_bat_vs_oppSP_2nd_woba"]), 3)),
                    "woba_daypart": (None if pd.isna(g.get(f"{side}_woba_daypart"))
                                     else round(float(g[f"{side}_woba_daypart"]), 3)),
                    "bp_r9_14": (None if pd.isna(g.get(f"{side}_bp_r9_14"))
                                 else round(float(g[f"{side}_bp_r9_14"]), 2)),
                    "team": g[f"{side}_team_zh"],
                } for side in ("away", "home")},
            "mu_home": round(float(mu_h[gi]), 2), "mu_away": round(float(mu_a[gi]), 2),
            "mu_total": round(float(mu_h[gi] + mu_a[gi]), 2),
            "markets": [],
            "conditions": g_fired[g_pos.get(pk, 0)] if g_pos else [],
        }
        for market, arr in probs.items():
            if market not in ALLOWED and not market.startswith("f5_"):
                continue
            p = float(arr[gi])
            base = bases.get(market)
            edge = (p / base) if base else None
            bt = bt_for(market, p)
            side_team = None
            if market.startswith("home_"):
                side_team = h_zh
            elif market.startswith("away_"):
                side_team = a_zh
            row = {"market": market, "market_zh": zh(market) if not market.startswith("f5_")
                   else ("前5局大分 4.5" if "over" in market else "前5局小分 4.5"),
                   "team": side_team, "p": round(p, 3),
                   "base": None if base is None else round(base, 3),
                   "edge": None if edge is None else round(edge, 3),
                   "be_odds": round(1 / max(p, 1e-6), 2),
                   "assumed_odds": None if not base else round((1 / base) * args.payout, 2),
                   "bt_hit": None if not bt else bt["hit"],
                   "bt_n": None if not bt else bt["n"],
                   "bt_roi": None if not bt else bt["roi"][str(args.payout)]}
            entry["markets"].append(row)
            if edge and edge >= args.min_edge and p >= 0.5:
                tgc = []
                key = (pk, g[f"{'home' if market.startswith('home_') else 'away'}_team"]) \
                    if market.startswith(("home_", "away_")) else None
                if key and key in tg_pos:
                    tgc = [c for c in tg_fired[tg_pos[key]] if c["market"] in market]
                picks.append({"pk": int(pk), "date": g["date"],
                              "matchup": f"{a_zh} @ {h_zh}",
                              **row, "conditions": tgc,
                              "cond_support": len(tgc)})
        entry["markets"].sort(key=lambda r: -(r["edge"] or 0))
        games.append(entry)

    picks.sort(key=lambda x: (-(x["edge"] or 0), -x["p"]))
    out = {"generated_for": sorted(set(pend_g["date"])), "payout_assumed": args.payout,
           "engine": "得分期望值模型（負二項卷積）", "alpha": {"full": alpha9, "f5": alpha5},
           "games": games, "picks": picks[:150], "n_picks": len(picks)}
    p = jdump(out, f"{OUTPUT}/slate.json")
    log(f"寫出 {p}：{len(games)} 場、{len(picks)} 個 edge≥{args.min_edge} 的候選")
    for x in picks[:14]:
        log(f"  {x['date'][5:]} {x['matchup']:<16} {x['market_zh']:<14}"
            f"{(x['team'] or ''):<4} 機率 {x['p']:.1%} 基準 {(x['base'] or 0):.1%} "
            f"edge {x['edge']:.2f} 假設賠率 {x['assumed_odds']} "
            + (f"回測 {x['bt_n']}注 {x['bt_hit']:.0%} ROI {x['bt_roi']:+.0%}" if x['bt_hit'] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
