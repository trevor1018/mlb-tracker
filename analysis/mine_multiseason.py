"""跨球季條件挖掘與驗證 → output/multiseason.json

單季 2000 場的統計力不足（這是 2026 單季只找到 11 組 Tier A 的根本原因）。
把 2024+2025 當訓練期、2026 當測試期，就有兩個好處：
  1. 訓練樣本翻倍 → 條件的命中率估得更準
  2. 「跨年還有效」是最硬的驗證：球員陣容、聯盟環境都變了還撐得住，
     才可能是真的規律，而不是某一季的巧合

分位數門檻各季分別計算（因為聯盟得分環境每年不同），
所以「對右投 wOBA 前 30%」在每一季都是相對該季聯盟的前 30%。
"""
import argparse
import os
import subprocess
import sys

import numpy as np
import pandas as pd
from scipy import stats

from common import ROOT, jdump, log
from mine_conditions import (TG_MARKETS, G_MARKETS, bh_qvalues, prep_markets,
                             wilson_lb_vec)
from predicates import add_derived, build_predicates_full


def load_season(season, kind):
    """讀某一季的資料集（需要先跑過該季的 build_dataset）。"""
    f = os.path.join(ROOT, "data", str(season),
                     "teamgames.parquet" if kind == "tg" else "gamesds.parquet")
    if not os.path.exists(f):
        return None
    df = pd.read_parquet(f)
    gp = ("my_gp", "op_gp") if kind == "tg" else ("home_gp", "away_gp")
    df = df[(df[gp[0]] >= 25) & (df[gp[1]] >= 25)].copy()
    df["season"] = season
    return add_derived(prep_markets(df, kind), kind)


def build_pooled(seasons, kind):
    """各季分別算門檻 → 合併成一個大矩陣（條件名稱對齊）。"""
    frames, mask_by_season, name_sets = [], {}, []
    for s in seasons:
        df = load_season(s, kind)
        if df is None or df.empty:
            log(f"  {s} 沒有資料，跳過")
            continue
        names, labels, M, specs = build_predicates_full(df, kind)
        frames.append(df)
        mask_by_season[s] = (names, labels, M)
        name_sets.append(set(names))
        log(f"  {s}: {len(df)} 列、{len(names)} 個條件基元")
    if not frames:
        return None
    common = sorted(set.intersection(*name_sets))
    log(f"  各季共有的條件基元：{len(common)}")
    label_map = {}
    blocks = []
    for s in mask_by_season:
        names, labels, M = mask_by_season[s]
        idx = {n: i for i, n in enumerate(names)}
        blocks.append(np.vstack([M[idx[n]] for n in common]))
        for n in common:
            label_map.setdefault(n, labels[idx[n]].split("(")[0].strip())
    pooled = pd.concat(frames, ignore_index=True)
    Mall = np.hstack(blocks)
    assert Mall.shape[1] == len(pooled), (Mall.shape, len(pooled))
    return pooled, common, [label_map[n] for n in common], Mall


def enumerate_combos(M, names, row_mask, min_n, max_depth, max_parents=4000):
    combos = [(i,) for i in range(len(names))]
    keep_c, keep_m = [], []
    for c in combos:
        m = M[c[0]]
        if (m & row_mask).sum() >= min_n:
            keep_c.append(c)
            keep_m.append(m)
    out_c, out_m = list(keep_c), list(keep_m)
    level_c, level_m = keep_c, keep_m
    for depth in range(2, max_depth + 1):
        sup = np.array([(m & row_mask).sum() for m in level_m])
        order = np.argsort(-sup)[:max_parents]
        new_c, new_m = [], []
        for oi in order:
            pred, pm = level_c[oi], level_m[oi]
            feats = {names[p].split(":")[0] for p in pred}
            for i in range(pred[-1] + 1, len(names)):
                if names[i].split(":")[0] in feats:
                    continue
                m = pm & M[i]
                if (m & row_mask).sum() < min_n:
                    continue
                new_c.append(pred + (i,))
                new_m.append(m)
        if not new_c:
            break
        log(f"    深度 {depth}: {len(new_c):,} 組")
        out_c += new_c
        out_m += new_m
        level_c, level_m = new_c, new_m
    return out_c, (np.vstack(out_m) if out_m else np.zeros((0, M.shape[1]), bool))


def counts(masks, Y, row_mask=None):
    if row_mask is not None:
        masks = masks & row_mask
    Mf = masks.astype(np.float32)
    return Mf @ Y, Mf.sum(axis=1)


def chance_line(masks, Y, row_mask, min_n, iters, seed=13):
    rng = np.random.default_rng(seed)
    idx = np.nonzero(row_mask)[0]
    Msub, Ysub = masks[:, idx], Y[idx]
    best_all = []
    for _ in range(iters):
        Yp = Ysub[rng.permutation(len(idx))]
        best = np.zeros(Y.shape[1])
        for s in range(0, len(Msub), 20000):
            h, n = counts(Msub[s:s + 20000], Yp)
            with np.errstate(invalid="ignore", divide="ignore"):
                r = h / n[:, None]
            r[n < min_n] = 0
            best = np.maximum(best, np.nan_to_num(r).max(axis=0))
        best_all.append(best)
    return np.percentile(np.vstack(best_all), 95, axis=0)


def run(kind, markets, seasons_train, season_test, args, tag):
    log(f"[{tag}] 讀取資料")
    packed = build_pooled(sorted(set(seasons_train) | {season_test}), kind)
    if packed is None:
        return None
    pooled, names, labels, M = packed
    mkeys = [m for m, _ in markets if m in pooled.columns]
    mlabels = dict(markets)
    Y = np.column_stack([pooled[k].astype(bool).to_numpy() for k in mkeys]).astype(np.float32)
    train = pooled["season"].isin(seasons_train).to_numpy()
    test = (pooled["season"] == season_test).to_numpy()
    log(f"[{tag}] 訓練 {train.sum()} 列（{seasons_train}）、測試 {test.sum()} 列（{season_test}）")

    combos, masks = enumerate_combos(M, names, train, args.min_n, args.depth)
    log(f"[{tag}] 候選 {len(combos):,} 組 × {len(mkeys)} 玩法")
    h_tr, n_tr = counts(masks, Y, train)
    r_tr = h_tr / np.maximum(n_tr[:, None], 1)
    base_tr = Y[train].mean(axis=0)
    base_te = Y[test].mean(axis=0)
    ok = (n_tr[:, None] >= args.min_n) & (r_tr >= args.target)
    ci, mi = np.nonzero(ok)
    log(f"[{tag}] 訓練期達 {args.target:.0%} 的組合：{len(ci):,}")

    chance = chance_line(masks, Y, train, args.min_n, args.perm_iters)
    h_te, n_te = counts(masks, Y, test)
    r_te = h_te / np.maximum(n_te[:, None], 1)

    # 各季分別的命中率（看穩定度）
    season_masks = {s: (pooled["season"] == s).to_numpy()
                    for s in sorted(set(seasons_train) | {season_test})}
    per_season = {}
    for s, sm in season_masks.items():
        h_s, n_s = counts(masks, Y, sm)
        per_season[s] = (h_s / np.maximum(n_s[:, None], 1), n_s)

    rows = []
    pv = stats.binom.sf(np.maximum(h_tr[ci, mi] - 1, 0), n_tr[ci], base_tr[mi])
    qv = bh_qvalues(pv)
    for k in range(len(ci)):
        c, m = int(ci[k]), int(mi[k])
        if n_te[c] < args.min_test_n:
            continue
        rows.append({
            "market": mkeys[m], "market_zh": mlabels[mkeys[m]],
            "label": " + ".join(labels[i] for i in combos[c]),
            "pred_names": [names[i] for i in combos[c]],
            "depth": len(combos[c]),
            "train_n": int(n_tr[c]), "train_rate": round(float(r_tr[c, m]), 4),
            "train_base": round(float(base_tr[m]), 4),
            "test_n": int(n_te[c]), "test_rate": round(float(r_te[c, m]), 4),
            "test_base": round(float(base_te[m]), 4),
            "wilson_train": round(float(wilson_lb_vec(h_tr[c, m], n_tr[c])), 4),
            "wilson_test": round(float(wilson_lb_vec(h_te[c, m], n_te[c])), 4),
            "q": round(float(qv[k]), 5),
            "chance_p95": round(float(chance[m]), 4),
            "beats_chance": bool(r_tr[c, m] > chance[m]),
            "by_season": {str(s): {"rate": round(float(per_season[s][0][c, m]), 3),
                                   "n": int(per_season[s][1][c])}
                          for s in season_masks},
            "be_odds": round(1 / max(float(r_te[c, m]), 1e-6), 3),
        })
    # 通關標準：訓練期顯著 + 勝過運氣線 + 測試期仍 ≥ target-0.05 + 每季都不崩
    for r in rows:
        seas = [v["rate"] for v in r["by_season"].values() if v["n"] >= 12]
        r["min_season_rate"] = round(min(seas), 3) if seas else None
        r["holds"] = bool(r["q"] < 0.05 and r["beats_chance"]
                          and r["test_rate"] >= args.target - 0.05
                          and (r["min_season_rate"] or 0) >= args.target - 0.12)
    rows.sort(key=lambda r: (-r["holds"], -min(r["train_rate"], r["test_rate"])))
    holds = [r for r in rows if r["holds"]]
    log(f"[{tag}] 訓練+測試都撐住的條件：{len(holds)} 組（候選 {len(rows)}）")
    for r in holds[:12]:
        by = " ".join(f"{k}:{v['rate']:.0%}" for k, v in r["by_season"].items())
        log(f"  {r['market_zh']:<18} 訓練 {r['train_rate']:.0%}({r['train_n']}) "
            f"→ {r['test_n']} 場測試 {r['test_rate']:.0%} | 各季 {by} "
            f"← {r['label'][:56]}")
    return {"tag": tag, "train_seasons": seasons_train, "test_season": season_test,
            "candidates": len(rows), "holds": len(holds),
            "rows": holds[:120] + [r for r in rows if not r["holds"]][:60],
            "base_train": {k: round(float(b), 4) for k, b in zip(mkeys, base_tr)},
            "base_test": {k: round(float(b), 4) for k, b in zip(mkeys, base_te)}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default="2024,2025")
    ap.add_argument("--test", type=int, default=2026)
    ap.add_argument("--min-n", type=int, default=60)
    ap.add_argument("--min-test-n", type=int, default=20)
    ap.add_argument("--target", type=float, default=0.72)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--perm-iters", type=int, default=20)
    args = ap.parse_args()
    train = [int(x) for x in args.train.split(",") if x.strip()]

    out = {"train_seasons": train, "test_season": args.test, "params": vars(args)}
    out["teamgame"] = run("tg", TG_MARKETS, train, args.test, args, "隊伍視角")
    out["game"] = run("g", G_MARKETS, train, args.test, args, "全場總分")
    p = jdump(out, os.path.join(ROOT, "output", "multiseason.json"))
    log(f"寫出 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
