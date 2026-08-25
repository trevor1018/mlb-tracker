"""條件挖掘：找出「滿足某些賽前條件時，某玩法命中率特別高」的組合。

為什麼要這麼囉嗦：搜尋空間上萬組 × 20 種玩法，光靠運氣就能刷出 90%+ 的
命中率（本檔的 permutation 對照會把這條線算出來）。所以每一組條件都要過四關：

  1. 樣本量  n ≥ min_n
  2. 統計顯著 BH-FDR 校正後 q < 0.05（對照該玩法的聯盟基準率）
  3. 勝過運氣 命中率 > 同一搜尋空間下 permutation 的 95 百分位最高命中率
  4. 時間穩定 切成 4 個時段，每段命中率都不能崩（min block rate）
     ＋ 真正的樣本外：用 7/15 之前挖掘、7/15 之後驗證

輸出 output/conditions.json
"""
import argparse
import sys
from itertools import combinations

import numpy as np
import pandas as pd
from scipy import stats

from common import DATA, OUTPUT, jdump, log
from predicates import add_derived, build_predicates

TG_MARKETS = [
    ("win", "不讓分（我隊贏）"),
    ("cover_m15", "讓分 1.5（我隊贏2分以上）"),
    ("cover_p15", "受讓 1.5（我隊輸不到2分）"),
    ("cover_m25", "讓分 2.5（我隊贏3分以上）"),
    ("cover_p25", "受讓 2.5（我隊輸不到3分）"),
    ("tt_over_2.5", "單隊大分 2.5"),
    ("tt_over_3.5", "單隊大分 3.5"),
    ("tt_over_4.5", "單隊大分 4.5"),
    ("tt_under_3.5", "單隊小分 3.5"),
    ("tt_under_4.5", "單隊小分 4.5"),
    ("tt_under_5.5", "單隊小分 5.5"),
    ("f5_lead", "前5局領先"),
    ("f5_no_trail", "前5局不落後"),
]
G_MARKETS = [
    ("over_7.5", "大分 7.5"),
    ("over_8.5", "大分 8.5"),
    ("over_9.5", "大分 9.5"),
    ("over_10.5", "大分 10.5"),
    ("under_7.5", "小分 7.5"),
    ("under_8.5", "小分 8.5"),
    ("under_9.5", "小分 9.5"),
    ("under_10.5", "小分 10.5"),
    ("f5_over_4.5", "前5局大分 4.5"),
    ("f5_under_4.5", "前5局小分 4.5"),
    ("nrfi", "首局雙方無得分 (NRFI)"),
]


def wilson_lb_vec(hits, n, z=1.96):
    n = np.maximum(np.asarray(n, float), 1e-9)
    p = np.asarray(hits, float) / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * np.sqrt(np.maximum(p * (1 - p), 0) / n + z * z / (4 * n * n))
    return (c - r) / d


def bh_qvalues(p):
    p = np.asarray(p, float)
    n = len(p)
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(n) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.clip(q, 0, 1)
    return out


def prep_markets(df, kind):
    df = df.copy()
    if kind == "g":
        for line in (7.5, 8.5, 9.5, 10.5):
            df[f"under_{line}"] = ~df[f"over_{line}"].astype(bool)
        df["f5_under_4.5"] = ~df["f5_over_4.5"].astype(bool)
    return df


def enumerate_combos(M, names, row_mask, min_n, max_depth, max_parents=6000):
    """在 row_mask 的列上列舉條件組合，回傳 (combos, masks_on_all_rows)。

    masks 是「全部列」的遮罩（之後才好套到測試期／各時段）。
    """
    sub = row_mask
    combos = [(i,) for i in range(len(names))]
    masks = [M[i] for i in range(len(names))]
    keep_c, keep_m = [], []
    for c, m in zip(combos, masks):
        if (m & sub).sum() >= min_n:
            keep_c.append(c)
            keep_m.append(m)
    out_c, out_m = list(keep_c), list(keep_m)

    level_c, level_m = keep_c, keep_m
    for depth in range(2, max_depth + 1):
        # 只從 support 最大的父節點往下長，控制爆炸
        sup = np.array([(m & sub).sum() for m in level_m])
        order = np.argsort(-sup)[:max_parents]
        new_c, new_m = [], []
        for oi in order:
            pred, pm = level_c[oi], level_m[oi]
            feats = {names[p].split(":")[0] for p in pred}
            for i in range(pred[-1] + 1, len(names)):
                if names[i].split(":")[0] in feats:
                    continue
                m = pm & M[i]
                if (m & sub).sum() < min_n:
                    continue
                new_c.append(pred + (i,))
                new_m.append(m)
        if not new_c:
            break
        log(f"    深度 {depth}: {len(new_c):,} 組")
        out_c += new_c
        out_m += new_m
        level_c, level_m = new_c, new_m
    return out_c, np.vstack(out_m) if out_m else np.zeros((0, M.shape[1]), bool)


def counts(masks, Y, row_mask=None):
    """masks (k,n) bool, Y (n,m) float32 → hits (k,m), n (k,)"""
    if row_mask is not None:
        masks = masks & row_mask
    Mf = masks.astype(np.float32)
    return Mf @ Y, Mf.sum(axis=1)


def chance_line(masks, Y, row_mask, min_n, iters, seed=7):
    """permutation：同一搜尋空間下，每個玩法純靠運氣的最高命中率分布。"""
    rng = np.random.default_rng(seed)
    idx = np.nonzero(row_mask)[0]
    Msub = masks[:, idx]
    Ysub = Y[idx]
    per_market = []
    for _ in range(iters):
        Yp = Ysub[rng.permutation(len(idx))]
        best = np.zeros(Y.shape[1])
        CH = 20000
        for s in range(0, len(Msub), CH):
            ch = Msub[s:s + CH]
            h, n = counts(ch, Yp)
            with np.errstate(invalid="ignore", divide="ignore"):
                r = h / n[:, None]
            r[n < min_n] = 0
            best = np.maximum(best, np.nan_to_num(r).max(axis=0))
        per_market.append(best)
    P = np.vstack(per_market)
    return {"mean": P.mean(axis=0), "p95": np.percentile(P, 95, axis=0), "iters": iters}


def block_rates(masks, Y, blocks):
    """回傳 (k, m, n_blocks) 的命中率與樣本數。"""
    rates, ns = [], []
    for b in blocks:
        h, n = counts(masks, Y, row_mask=b)
        with np.errstate(invalid="ignore", divide="ignore"):
            rates.append(np.where(n[:, None] > 0, h / np.maximum(n[:, None], 1), np.nan))
        ns.append(n)
    return np.stack(rates, axis=2), np.stack(ns, axis=1)


def dedupe(rows, masks, combo_index, max_jaccard=0.75, limit=200):
    """同一玩法內，刪掉覆蓋列高度重疊的近似重複條件。"""
    kept, kept_masks = [], []
    for r in rows:
        m = masks[combo_index[r["_ci"]]]
        dup = False
        for km in kept_masks:
            inter = np.logical_and(m, km).sum()
            union = np.logical_or(m, km).sum()
            if union and inter / union > max_jaccard:
                dup = True
                break
        if dup:
            continue
        kept.append(r)
        kept_masks.append(m)
        if len(kept) >= limit:
            break
    return kept


def run_side(df, kind, markets, args, tag):
    df = add_derived(prep_markets(df, kind), kind)
    dates = pd.to_datetime(df["date"])
    n_rows = len(df)
    train_mask = (dates < pd.Timestamp(args.train_cut)).to_numpy()
    test_mask = ~train_mask
    all_mask = np.ones(n_rows, bool)

    # 門檻一律用「訓練期」分位數 → 訓練/測試共用同一組條件定義
    names, labels, M = build_predicates(df, kind, train_mask=train_mask)
    mkeys = [m for m, _ in markets if m in df.columns]
    mlabels = dict(markets)
    Y = np.column_stack([df[k].astype(bool).to_numpy() for k in mkeys]).astype(np.float32)
    base_all = Y.mean(axis=0)
    log(f"[{tag}] {n_rows} 列、條件基元 {len(names)}、玩法 {len(mkeys)}")

    # 時段分塊（4 段等量）
    qs = np.quantile(dates.astype("int64"), [0.25, 0.5, 0.75])
    di = dates.astype("int64").to_numpy()
    blocks = [di < qs[0], (di >= qs[0]) & (di < qs[1]),
              (di >= qs[1]) & (di < qs[2]), di >= qs[2]]

    # ── 全季挖掘 ──
    log(f"[{tag}] 列舉條件（全季，min_n={args.min_n}, depth≤{args.depth}）")
    combos, masks = enumerate_combos(M, names, all_mask, args.min_n, args.depth)
    log(f"[{tag}] 候選 {len(combos):,} 組 × {len(mkeys)} 玩法 = {len(combos)*len(mkeys):,} 個假設")

    hits, ns = counts(masks, Y)
    rates = hits / np.maximum(ns[:, None], 1)
    ok = (ns[:, None] >= args.min_n) & (rates >= args.target)
    ci_idx, mi_idx = np.nonzero(ok)
    log(f"[{tag}] 命中率 ≥{args.target:.0%} 且 n≥{args.min_n}：{len(ci_idx):,} 個")

    # permutation 運氣線
    log(f"[{tag}] permutation 對照（{args.perm_iters} 次）")
    chance = chance_line(masks, Y, all_mask, args.min_n, args.perm_iters)
    for mi, mk in enumerate(mkeys):
        log(f"    {mlabels[mk]:<22} 基準 {base_all[mi]:.1%} → 運氣線(p95) {chance['p95'][mi]:.1%}")

    # 時段穩定度
    brates, bns = block_rates(masks, Y, blocks)

    rows = []
    pvals = stats.binom.sf(np.maximum(hits[ci_idx, mi_idx] - 1, 0),
                           ns[ci_idx], base_all[mi_idx])
    qv = bh_qvalues(pvals)
    for k in range(len(ci_idx)):
        ci, mi = int(ci_idx[k]), int(mi_idx[k])
        br = brates[ci, mi, :]
        bn = bns[ci, :]
        valid = bn >= 5
        rows.append({
            "_ci": ci,
            "market": mkeys[mi], "market_zh": mlabels[mkeys[mi]],
            "label": " + ".join(labels[i] for i in combos[ci]),
            "pred_names": [names[i] for i in combos[ci]],
            "depth": len(combos[ci]),
            "n": int(ns[ci]), "hits": int(round(hits[ci, mi])),
            "rate": float(rates[ci, mi]), "base": float(base_all[mi]),
            "lift": float(rates[ci, mi] - base_all[mi]),
            "wilson": float(wilson_lb_vec(hits[ci, mi], ns[ci])),
            "p": float(pvals[k]), "q": float(qv[k]),
            "be_odds": float(1 / max(rates[ci, mi], 1e-9)),
            "chance_p95": float(chance["p95"][mi]),
            "beats_chance": bool(rates[ci, mi] > chance["p95"][mi]),
            "block_rates": [None if not v else round(float(x), 3)
                            for x, v in zip(br, valid)],
            "block_min": float(np.nanmin(np.where(valid, br, np.nan))) if valid.any() else None,
            "blocks_ok": int(np.nansum(np.where(valid, br >= args.target - 0.1, False))),
        })

    # 分級
    for r in rows:
        strong = (r["q"] < 0.05 and r["beats_chance"]
                  and (r["block_min"] is not None and r["block_min"] >= args.target - 0.15)
                  and r["blocks_ok"] >= 3)
        r["tier"] = "A" if strong else ("B" if r["q"] < 0.10 else "C")

    rows.sort(key=lambda r: (-r["wilson"], -r["n"]))
    tiers = {}
    for t in ("A", "B", "C"):
        sel = [r for r in rows if r["tier"] == t]
        # 每個玩法各自去重
        packed = []
        for mk in mkeys:
            mrows = [r for r in sel if r["market"] == mk]
            packed += dedupe(mrows, masks, {r["_ci"]: r["_ci"] for r in mrows},
                             max_jaccard=args.jaccard, limit=args.per_market)
        packed.sort(key=lambda r: -r["wilson"])
        tiers[t] = packed
        log(f"[{tag}] Tier {t}: {len(sel):,} 個（去重後 {len(packed)}）")

    # ── 真樣本外：訓練期挖掘 → 測試期驗證 ──
    log(f"[{tag}] 樣本外驗證：{args.train_cut} 之前挖掘 → 之後驗證")
    tr_min = max(12, int(args.min_n * 0.6))
    tr_combos, tr_masks = enumerate_combos(M, names, train_mask, tr_min, args.depth)
    h_tr, n_tr = counts(tr_masks, Y, row_mask=train_mask)
    r_tr = h_tr / np.maximum(n_tr[:, None], 1)
    ok_tr = (n_tr[:, None] >= tr_min) & (r_tr >= args.target)
    ci2, mi2 = np.nonzero(ok_tr)
    h_te, n_te = counts(tr_masks, Y, row_mask=test_mask)
    log(f"[{tag}] 訓練期符合門檻 {len(ci2):,} 個，逐一看測試期表現")
    oos = []
    for k in range(len(ci2)):
        ci, mi = int(ci2[k]), int(mi2[k])
        if n_te[ci] < 8:
            continue
        oos.append({
            "market": mkeys[mi], "market_zh": mlabels[mkeys[mi]],
            "label": " + ".join(labels[i] for i in tr_combos[ci]),
            "depth": len(tr_combos[ci]),
            "train_n": int(n_tr[ci]), "train_rate": round(float(r_tr[ci, mi]), 3),
            "test_n": int(n_te[ci]),
            "test_rate": round(float(h_te[ci, mi] / max(n_te[ci], 1)), 3),
            "base": round(float(base_all[mi]), 3),
        })
    if oos:
        odf = pd.DataFrame(oos)
        summ = (odf.groupby("market")
                .agg(cands=("test_rate", "size"),
                     mean_train=("train_rate", "mean"),
                     mean_test=("test_rate", "mean"),
                     base=("base", "first"),
                     hold75=("test_rate", lambda s: float((s >= 0.75).mean())))
                .round(3).reset_index())
        for _, s in summ.iterrows():
            log(f"    {mlabels[s['market']]:<22} 候選{int(s['cands']):>5} "
                f"訓練均值 {s['mean_train']:.1%} → 測試均值 {s['mean_test']:.1%} "
                f"(基準 {s['base']:.1%}, 測試仍≥75% 比例 {s['hold75']:.0%})")
        oos_summary = summ.to_dict("records")
        odf = odf.sort_values(["test_rate", "test_n"], ascending=False)
        oos_top = odf.head(args.per_market * 2).to_dict("records")
    else:
        oos_summary, oos_top = [], []

    for t in tiers:
        for r in tiers[t]:
            r.pop("_ci", None)
    return {
        "meta": {"rows": int(n_rows), "predicates": len(names),
                 "hypotheses": int(len(combos) * len(mkeys)),
                 "base_rates": {k: round(float(b), 4) for k, b in zip(mkeys, base_all)},
                 "chance_p95": {k: round(float(v), 4) for k, v in zip(mkeys, chance["p95"])}},
        "tierA": tiers["A"], "tierB": tiers["B"][:args.per_market * 3],
        "oos": {"cut": args.train_cut, "summary": oos_summary, "top": oos_top},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=40)
    ap.add_argument("--target", type=float, default=0.75)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--train-cut", default="2026-07-15")
    ap.add_argument("--perm-iters", type=int, default=25)
    ap.add_argument("--jaccard", type=float, default=0.7)
    ap.add_argument("--per-market", type=int, default=25)
    ap.add_argument("--min-gp", type=int, default=25)
    args = ap.parse_args()

    tg = pd.read_parquet(f"{DATA}/teamgames.parquet")
    gd = pd.read_parquet(f"{DATA}/gamesds.parquet")
    tg = tg[(tg["my_gp"] >= args.min_gp) & (tg["op_gp"] >= args.min_gp)].copy()
    gd = gd[(gd["home_gp"] >= args.min_gp) & (gd["away_gp"] >= args.min_gp)].copy()

    out = {"season": 2026, "params": vars(args)}
    out["teamgame"] = run_side(tg, "tg", TG_MARKETS, args, "隊伍視角")
    out["game"] = run_side(gd, "g", G_MARKETS, args, "全場總分")
    p = jdump(out, f"{OUTPUT}/conditions.json")
    log(f"寫出 {p}")

    for side in ("teamgame", "game"):
        A = out[side]["tierA"]
        log(f"── {side} Tier A（過四關）{len(A)} 組 ──")
        for r in A[:15]:
            log(f"  {r['market_zh']:<20} {r['rate']:.1%} ({r['hits']}/{r['n']}) "
                f"LB{r['wilson']:.0%} 基準{r['base']:.1%} 運氣線{r['chance_p95']:.0%} "
                f"分段{r['block_rates']} ← {r['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
