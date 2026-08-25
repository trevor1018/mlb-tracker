"""對「還沒開打的場次」產生預測 → output/slate.json

用兩套獨立方法交叉看：
  A. 模型：用全季資料重新訓練，輸出各玩法機率（模型的樣本外表現見 models.json）
  B. 條件：檢查 Tier A / Tier B 條件今天有沒有觸發，附上該條件的歷史命中率

兩者同時指向同一個玩法時，才是真正值得下注的訊號。
"""
import argparse
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from common import DATA, OUTPUT, jdump, jload, log
from mine_conditions import G_MARKETS, TG_MARKETS, prep_markets
from model_markets import design
from predicates import add_derived, apply_specs, build_predicates_full


def pending_to_tg(pend):
    """未開打的 game 列 → 兩個隊伍視角列（欄位對齊 teamgames）。"""
    rows = []
    for _, g in pend.iterrows():
        for side, other in (("home", "away"), ("away", "home")):
            r = {"pk": g["pk"], "date": g["date"], "month": g["month"], "dow": g["dow"],
                 "day_game": g["day_game"], "venue": g["venue"], "temp": g["temp"],
                 "series_game": g["series_game"], "series_len": g["series_len"],
                 "team": g[f"{side}_team"], "opp": g[f"{other}_team"],
                 "team_zh": g[f"{side}_team_zh"], "opp_zh": g[f"{other}_team_zh"],
                 "is_home": side == "home", "sp_known": g.get("sp_known", False)}
            for k, v in g.items():
                if isinstance(k, str) and k.startswith(side + "_"):
                    r["my_" + k[len(side) + 1:]] = v
                elif isinstance(k, str) and k.startswith(other + "_"):
                    r["op_" + k[len(other) + 1:]] = v
            rows.append(r)
    return pd.DataFrame(rows)


def fit_predict(hist, fut, kind, market):
    Xh = design(hist, kind)
    Xf = design(fut, kind).reindex(columns=Xh.columns)
    y = hist[market].astype(int).to_numpy()
    clf = HistGradientBoostingClassifier(max_depth=3, max_iter=250, learning_rate=0.05,
                                         min_samples_leaf=40, l2_regularization=1.0,
                                         early_stopping=True, validation_fraction=0.15,
                                         random_state=7)
    clf.fit(Xh, y)
    return clf.predict_proba(Xf)[:, 1]


def fired_conditions(fut, kind, hist, conds, limit_per_row=6):
    """回傳 list[list[dict]]：每一列觸發了哪些條件。"""
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
            if len(out[i]) < limit_per_row:
                out[i].append({
                    "market": c["market"], "market_zh": c["market_zh"],
                    "label": c["label"], "rate": c["rate"], "n": c["n"],
                    "base": c["base"], "tier": c.get("tier", "?"),
                    "be_odds": round(c["be_odds"], 3),
                })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-model-prob", type=float, default=0.62)
    args = ap.parse_args()

    tg = pd.read_parquet(f"{DATA}/teamgames.parquet")
    gd = pd.read_parquet(f"{DATA}/gamesds.parquet")
    pend = pd.read_parquet(f"{DATA}/pending.parquet")
    tg = add_derived(tg[(tg["my_gp"] >= 20) & (tg["op_gp"] >= 20)].copy(), "tg")
    gd = add_derived(prep_markets(gd[(gd["home_gp"] >= 20) & (gd["away_gp"] >= 20)].copy(), "g"), "g")

    pend_g = add_derived(pend.copy(), "g")
    pend_tg = add_derived(pending_to_tg(pend), "tg")
    log(f"未開打 {len(pend)} 場（{pend['date'].min()} ~ {pend['date'].max()}）")

    # ── 模型機率 ──
    tg_probs, g_probs = {}, {}
    for mk, zh in TG_MARKETS:
        if mk in tg.columns:
            tg_probs[mk] = fit_predict(tg, pend_tg, "tg", mk)
    for mk, zh in G_MARKETS:
        if mk in gd.columns:
            g_probs[mk] = fit_predict(gd, pend_g, "g", mk)
    log(f"模型完成：隊伍視角 {len(tg_probs)} 種、全場 {len(g_probs)} 種")

    # ── 條件觸發 ──
    try:
        C = jload(f"{OUTPUT}/conditions.json")
        tg_conds = C["teamgame"]["tierA"] + C["teamgame"]["tierB"]
        g_conds = C["game"]["tierA"] + C["game"]["tierB"]
    except Exception as e:
        log(f"讀不到 conditions.json（{e}），跳過條件比對")
        tg_conds, g_conds = [], []
    tg_fired = fired_conditions(pend_tg, "tg", tg, tg_conds) if tg_conds else [[]] * len(pend_tg)
    g_fired = fired_conditions(pend_g, "g", gd, g_conds) if g_conds else [[]] * len(pend_g)

    # ── 模型樣本外可信度（來自 models.json）──
    try:
        MD = jload(f"{OUTPUT}/models.json")["models"]
    except Exception:
        MD = {}

    def reliability(mk):
        m = MD.get(mk)
        if not m:
            return None
        return {"auc": m["auc"], "brier_skill": m["brier_skill"],
                "oos_n": m["oos_n"],
                "thr70": next((t for t in m["thresholds"] if t["thr"] == 0.7), None)}

    games_out = []
    tg_idx = {(r["pk"], r["team"]): i for i, r in pend_tg.iterrows()}
    for gi, (_, g) in enumerate(pend_g.iterrows()):
        entry = {
            "pk": int(g["pk"]), "date": g["date"],
            "away": g["away_team_zh"], "home": g["home_team_zh"],
            "day_game": bool(g["day_game"]),
            "sp_known": bool(g.get("sp_known", False)),
            "home_sp_hand": g.get("home_sp_hand"), "away_sp_hand": g.get("away_sp_hand"),
            "home_sp_r9": None if pd.isna(g.get("home_sp_r9")) else round(float(g["home_sp_r9"]), 2),
            "away_sp_r9": None if pd.isna(g.get("away_sp_r9")) else round(float(g["away_sp_r9"]), 2),
            "total_expect": None if pd.isna(g.get("total_expect")) else round(float(g["total_expect"]), 2),
            "game_markets": [], "team_markets": [], "conditions": g_fired[gi],
        }
        for mk, zh in G_MARKETS:
            if mk not in g_probs:
                continue
            entry["game_markets"].append({
                "market": mk, "market_zh": zh, "p": round(float(g_probs[mk][gi]), 3),
                "be_odds": round(1 / max(float(g_probs[mk][gi]), 1e-6), 2),
            })
        for side in ("away", "home"):
            tid = g[f"{side}_team"]
            i = tg_idx.get((g["pk"], tid))
            if i is None:
                continue
            tm = {"team": g[f"{side}_team_zh"], "is_home": side == "home", "markets": [],
                  "conditions": tg_fired[i]}
            for mk, zh in TG_MARKETS:
                if mk not in tg_probs:
                    continue
                p = float(tg_probs[mk][i])
                tm["markets"].append({"market": mk, "market_zh": zh, "p": round(p, 3),
                                      "be_odds": round(1 / max(p, 1e-6), 2)})
            entry["team_markets"].append(tm)
        games_out.append(entry)

    # ── 推薦清單：模型機率高 + 有條件加持 ──
    picks = []
    for e in games_out:
        for m in e["game_markets"]:
            if m["p"] >= args.min_model_prob:
                cond = [c for c in e["conditions"] if c["market"] == m["market"]]
                picks.append({"date": e["date"], "matchup": f"{e['away']} @ {e['home']}",
                              "scope": "全場", "team": None, **m,
                              "conditions": cond, "cond_support": len(cond),
                              "reliability": reliability(m["market"])})
        for tm in e["team_markets"]:
            for m in tm["markets"]:
                if m["p"] >= args.min_model_prob:
                    cond = [c for c in tm["conditions"] if c["market"] == m["market"]]
                    picks.append({"date": e["date"], "matchup": f"{e['away']} @ {e['home']}",
                                  "scope": "單隊", "team": tm["team"], **m,
                                  "conditions": cond, "cond_support": len(cond),
                                  "reliability": reliability(m["market"])})
    picks.sort(key=lambda x: (-x["cond_support"], -x["p"]))

    out = {"generated_for": sorted(set(pend_g["date"])), "games": games_out,
           "picks": picks[:120], "n_picks": len(picks)}
    p = jdump(out, f"{OUTPUT}/slate.json")
    log(f"寫出 {p}：{len(games_out)} 場、{len(picks)} 個候選（機率≥{args.min_model_prob:.0%}）")
    for x in picks[:12]:
        rel = x["reliability"]
        log(f"  {x['date']} {x['matchup']:<18} {x['scope']}{x['team'] or '':<4} "
            f"{x['market_zh']:<18} 模型 {x['p']:.1%} 需賠率>{x['be_odds']:.2f} "
            f"條件 {x['cond_support']} 個 " + (f"AUC {rel['auc']:.3f}" if rel else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
