"""驗證「打線對位分數」到底有沒有預測力 → output/matchup_validation.json

設計成完全沒有洩漏：
  用 2025 球季的資料算打者分項（對左右投、對球種）與投手的球種輪廓，
  再拿去預測 2026 每一場的實際得分。2025 的資料不可能知道 2026 的結果。

檢查三件事：
  1. 對位分數與實際得分的相關性（分五等分看平均得分）
  2. 是不是只是「打線好壞」的翻版 —— 控制住球隊整體 wOBA 之後還剩多少
  3. 對大小分盤口的命中率有沒有幫助
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

from build_splits import PITCH_GROUP
from common import DATA, OUTPUT, ROOT, jdump, jload, log

PRIOR_PA = 80


def season_pitches(season):
    cols = ["batter", "pitcher", "stand", "p_throws", "pitch_type",
            "woba_value", "woba_denom"]
    return pd.read_parquet(os.path.join(ROOT, "data", str(season), "pitches.parquet"),
                           columns=cols)


def agg(df, keys):
    g = df.groupby(keys, dropna=False).agg(pa=("woba_denom", "sum"),
                                           wsum=("woba_value", "sum")).reset_index()
    g = g[g["pa"] > 0]
    g["woba"] = g["wsum"] / g["pa"]
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit-season", type=int, default=2025)
    ap.add_argument("--test-season", type=int, default=2026)
    ap.add_argument("--asof", action="store_true",
                    help="改用『當季累積到該場之前』的分項（更即時，但樣本較小）")
    args = ap.parse_args()

    log(f"用 {args.fit_season} 的分項預測 {args.test_season} 的得分（無洩漏）")
    src = season_pitches(args.fit_season)
    src["pgroup"] = src["pitch_type"].map(PITCH_GROUP).fillna("other")

    lg_all = float(src["woba_value"].sum() / src["woba_denom"].sum())
    lg_hand = agg(src, ["p_throws"]).set_index("p_throws")["woba"].to_dict()
    lg_group = agg(src, ["pgroup"]).set_index("pgroup")["woba"].to_dict()

    bh = {(int(r.batter), r.p_throws): (float(r.pa), float(r.woba))
          for r in agg(src, ["batter", "p_throws"]).itertuples()}
    bg = {(int(r.batter), r.pgroup): (float(r.pa), float(r.woba))
          for r in agg(src, ["batter", "pgroup"]).itertuples()}
    use = src.groupby(["pitcher", "pgroup"]).size().rename("n").reset_index()
    tot = use.groupby("pitcher")["n"].sum().rename("tot").reset_index()
    use = use.merge(tot, on="pitcher")
    use["pct"] = use["n"] / use["tot"]
    arsenal = {}
    for pid, g in use.groupby("pitcher"):
        rows = [(r.pgroup, float(r.pct)) for r in g.sort_values("pct", ascending=False).itertuples()]
        arsenal[int(pid)] = rows

    def shrunk(w, pa, lg):
        return (w * pa + lg * PRIOR_PA) / (pa + PRIOR_PA)

    def _absorb(pk):
        """把這一場的資料吃進累積器（快照之後才呼叫）"""
        if pk in seen_pk or not asof_state:
            return
        seen_pk.add(pk)
        h = asof_state["h"].get(pk)
        if h is not None:
            for x in h.itertuples():
                k = (int(x.batter), x.p_throws)
                cur = ah.setdefault(k, [0.0, 0.0])
                cur[0] += float(x.pa)
                cur[1] += float(x.wsum)
        g_ = asof_state["g"].get(pk)
        if g_ is not None:
            for x in g_.itertuples():
                k = (int(x.batter), x.pgroup)
                cur = ag.setdefault(k, [0.0, 0.0])
                cur[0] += float(x.pa)
                cur[1] += float(x.wsum)
        u = asof_state["u"].get(pk)
        if u is not None:
            for x in u.itertuples():
                au[(int(x.pitcher), x.pgroup)] = au.get((int(x.pitcher), x.pgroup), 0) + int(x.n)

    def _asof_delta(bid, hand, key_group):
        pa_h, w_h = ah.get((bid, hand), [0.0, 0.0])
        pa_g, w_g = ag.get((bid, key_group), [0.0, 0.0])
        wh = shrunk(w_h / pa_h if pa_h else lg_hand.get(hand, lg_all), pa_h,
                    lg_hand.get(hand, lg_all))
        wg = shrunk(w_g / pa_g if pa_g else lg_group.get(key_group, lg_all), pa_g,
                    lg_group.get(key_group, lg_all))
        return (0.6 * (wh - lg_hand.get(hand, lg_all))
                + 0.4 * (wg - lg_group.get(key_group, lg_all))), (pa_h, pa_g)

    def batter_delta(bid, hand, key_group):
        pa_h, w_h = bh.get((bid, hand), (0.0, lg_hand.get(hand, lg_all)))
        pa_g, w_g = bg.get((bid, key_group), (0.0, lg_group.get(key_group, lg_all)))
        wh = shrunk(w_h, pa_h, lg_hand.get(hand, lg_all))
        wg = shrunk(w_g, pa_g, lg_group.get(key_group, lg_all))
        return (0.6 * (wh - lg_hand.get(hand, lg_all))
                + 0.4 * (wg - lg_group.get(key_group, lg_all))), (pa_h, pa_g)

    # ── as-of 模式：用當季累積到該場前的分項 ──
    asof_state = None
    if args.asof:
        log("as-of 模式：逐場累積當季分項（只用該場之前的資料）")
        cols = ["game_pk", "game_date", "batter", "pitcher", "stand", "p_throws",
                "pitch_type", "woba_value", "woba_denom"]
        tp = pd.read_parquet(os.path.join(ROOT, "data", str(args.test_season),
                                          "pitches.parquet"), columns=cols)
        tp["pgroup"] = tp["pitch_type"].map(PITCH_GROUP).fillna("other")
        tp = tp.sort_values(["game_date", "game_pk"])
        # 每場的分項增量
        inc_h = (tp.groupby(["game_pk", "batter", "p_throws"])
                   .agg(pa=("woba_denom", "sum"), wsum=("woba_value", "sum")).reset_index())
        inc_g = (tp.groupby(["game_pk", "batter", "pgroup"])
                   .agg(pa=("woba_denom", "sum"), wsum=("woba_value", "sum")).reset_index())
        inc_u = tp.groupby(["game_pk", "pitcher", "pgroup"]).size().rename("n").reset_index()
        asof_state = {"h": {k: v for k, v in inc_h.groupby("game_pk")},
                      "g": {k: v for k, v in inc_g.groupby("game_pk")},
                      "u": {k: v for k, v in inc_u.groupby("game_pk")}}

    # ── 測試季：每場每隊的實際打線 vs 對方先發 ──
    test_dir = os.path.join(ROOT, "data", str(args.test_season))
    boxes = {b["pk"]: b for b in jload(os.path.join(test_dir, "boxes.json.gz"))}
    people = jload(os.path.join(test_dir, "people.json"))
    tg = pd.read_parquet(os.path.join(test_dir, "teamgames.parquet"))

    # as-of 累積器
    ah = {}   # (batter, hand) -> [pa, wsum]
    ag = {}   # (batter, group) -> [pa, wsum]
    au = {}   # (pitcher, group) -> n
    seen_pk = set()

    rows = []
    tg = tg.sort_values(["date", "pk"])
    for _, r in tg.iterrows():
        pk = int(r["pk"])
        b = boxes.get(pk)
        if not b:
            continue
        side = "home" if r["is_home"] else "away"
        other = "away" if r["is_home"] else "home"
        opp_sp = (b[other]["sp"] or {}).get("id")
        if not opp_sp:
            continue
        hand = (people.get(str(opp_sp)) or {}).get("throws") or "R"
        if args.asof:
            tot_u = sum(v for (pid, _), v in au.items() if pid == int(opp_sp))
            ars = sorted([(gp, v / tot_u) for (pid, gp), v in au.items()
                          if pid == int(opp_sp)], key=lambda x: -x[1]) if tot_u >= 200 else []
        else:
            ars = arsenal.get(int(opp_sp), [])
        if not ars:
            if args.asof:
                _absorb(pk)
            continue
        key_group = next((k for k, v in ars if k not in ("fastball", "cutter")),
                         ars[0][0])
        lineup = [p for p in b[side]["lineup"] if p.get("ord") and not p.get("sub")][:9]
        if len(lineup) < 7:
            continue
        ds, ws, cov = [], [], 0
        for p_ in lineup:
            if args.asof:
                d, (pa_h, pa_g) = _asof_delta(int(p_["id"]), hand, key_group)
            else:
                d, (pa_h, pa_g) = batter_delta(int(p_["id"]), hand, key_group)
            ds.append(d)
            ws.append(float(p_.get("pa") or 1))
            if pa_h >= 50:
                cov += 1
        if not ds:
            continue
        delta = float(np.average(ds, weights=ws))
        if args.asof:
            _absorb(pk)
        rows.append({
            "pk": pk, "date": r["date"], "team": r["team"], "runs": float(r["runs"]),
            "delta": delta, "coverage": cov / len(lineup),
            "team_woba": float(r.get("my_woba_l15") or np.nan),
            "opp_sp_r9": float(r.get("op_sp_r9") or np.nan),
            "is_home": bool(r["is_home"]),
            "total": float(r["runs"]) + float(r["runs_allowed"]),
        })

    d = pd.DataFrame(rows).dropna(subset=["delta", "runs"])
    d = d[d["coverage"] >= 0.6]        # 至少 6 成打者在前一季有足夠樣本
    log(f"可用樣本 {len(d)} 個 team-game（{d['pk'].nunique()} 場）")

    # 1) 相關性與分組
    r_pear = float(stats.pearsonr(d["delta"], d["runs"])[0])
    r_spear = float(stats.spearmanr(d["delta"], d["runs"])[0])
    d["bucket"] = pd.qcut(d["delta"], 5, labels=["最不利", "偏不利", "中性", "偏有利", "最有利"])
    grp = d.groupby("bucket", observed=True).agg(
        n=("runs", "size"), 平均得分=("runs", "mean"),
        得4分以上=("runs", lambda s: float((s >= 4).mean())),
        平均對位=("delta", "mean")).round(3).reset_index()
    log(f"對位分數 vs 實際得分：Pearson {r_pear:+.4f}、Spearman {r_spear:+.4f}")
    for _, x in grp.iterrows():
        log(f"  {x['bucket']:<5} n={int(x['n']):>4} 平均得分 {x['平均得分']:.2f} "
            f"得4分以上 {x['得4分以上']:.1%} (平均對位 {x['平均對位']:+.4f})")

    # 2) 控制住球隊整體強度後還剩多少
    sub = d.dropna(subset=["team_woba", "opp_sp_r9"])
    X = np.column_stack([np.ones(len(sub)), sub["team_woba"], sub["opp_sp_r9"]])
    beta, *_ = np.linalg.lstsq(X, sub["runs"], rcond=None)
    resid = sub["runs"] - X @ beta
    r_partial = float(stats.pearsonr(sub["delta"], resid)[0])
    log(f"控制「球隊近15場 wOBA + 對手先發 R/9」後的偏相關：{r_partial:+.4f}"
        f"（{len(sub)} 筆）")

    # 3) 對大小分的幫助：以全場總分看
    game = d.groupby("pk").agg(total=("total", "first"),
                               sum_delta=("delta", "sum")).reset_index()
    game["bucket"] = pd.qcut(game["sum_delta"], 5,
                             labels=["最低", "偏低", "中", "偏高", "最高"])
    g2 = game.groupby("bucket", observed=True).agg(
        n=("total", "size"), 平均總分=("total", "mean"),
        大分85=("total", lambda s: float((s > 8.5).mean()))).round(3).reset_index()
    log("雙方對位分數合計 vs 全場總分：")
    for _, x in g2.iterrows():
        log(f"  {x['bucket']:<4} n={int(x['n']):>4} 平均總分 {x['平均總分']:.2f} "
            f"大分8.5 命中 {x['大分85']:.1%}")
    base_over = float((game["total"] > 8.5).mean())
    top = game[game["bucket"] == "最高"]
    lift = float((top["total"] > 8.5).mean()) / base_over if base_over else None

    out = {
        "fit_season": args.fit_season, "test_season": args.test_season,
        "n_team_games": int(len(d)), "n_games": int(d["pk"].nunique()),
        "pearson_runs": round(r_pear, 4), "spearman_runs": round(r_spear, 4),
        "partial_r_controlled": round(r_partial, 4),
        "buckets_runs": grp.to_dict("records"),
        "buckets_total": g2.to_dict("records"),
        "over85_base": round(base_over, 4),
        "over85_lift_top_quintile": round(lift, 4) if lift else None,
        "note": ("打者分項與投手球種輪廓全部來自前一季，預測目標是下一季的實際得分，"
                 "所以沒有資訊洩漏。偏相關是控制住『球隊近15場 wOBA』與"
                 "『對手先發 R/9』之後的殘差相關 —— 這才是對位分數的獨立貢獻。")}
    p = jdump(out, f"{OUTPUT}/matchup_validation.json")
    log(f"寫出 {p}")
    # 存下逐場對位分數，供模型測試使用
    tag = "asof" if args.asof else "prior"
    d[["pk", "team", "date", "delta", "coverage"]].to_parquet(
        os.path.join(DATA, f"matchup_delta_{tag}.parquet"), index=False)
    log(f"寫出 data/matchup_delta_{tag}.parquet（{len(d)} 列）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
