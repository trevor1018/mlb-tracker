"""條件挖掘：找出「滿足某些賽前條件時，某玩法命中率特別高」的組合。

作法
  1. 用 predicates.py 把特徵離散化成布林條件（含中文標籤）
  2. 逐層擴展（1→2→3 個條件的 AND），用 support/命中率剪枝
  3. 每個候選算：n、命中率、Wilson 95% 下界、對基準率的提升、單尾二項式 p 值
  4. 多重檢定：BH-FDR 校正 q 值；另外跑標籤重排（permutation）估計
     「純靠運氣能刷到的最高命中率」，作為誠實的對照線
  5. 時間切分驗證：以 7/15 為界，前段挖掘、後段驗證（out-of-sample）

輸出 output/conditions.json
"""
import argparse
import sys

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
    ("tt_over_2.5", "單隊大分 2.5（我隊得分>2.5）"),
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


def wilson_lb(hits, n, z=1.96):
    if n == 0:
        return 0.0
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return float((c - r) / d)


def wilson_lb_vec(hits, n, z=1.96):
    n = np.maximum(n, 1e-9)
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - r) / d


def bh_qvalues(p):
    p = np.asarray(p, float)
    n = len(p)
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


def counts_for(masks, Y):
    """masks: (k, n) bool；Y: (n, m) float32 → 回傳 (k, m) 命中數與 (k,) 樣本數"""
    Mf = masks.astype(np.float32)
    hits = Mf @ Y
    n = Mf.sum(axis=1)
    return hits, n


def mine(df, kind, markets, min_n=25, max_depth=3, target=0.72, top_k=4000,
         train_cut=None, label_prefix=""):
    df = add_derived(prep_markets(df, kind), kind)
    n_rows = len(df)
    dates = pd.to_datetime(df["date"])
    train_mask = (dates < pd.Timestamp(train_cut)).to_numpy() if train_cut else np.ones(n_rows, bool)

    names, labels, M = build_predicates(df, kind, train_mask=train_mask)
    log(f"{label_prefix}條件基元 {len(names)} 個，資料 {n_rows} 列")

    mkeys = [m for m, _ in markets if m in df.columns]
    mlabels = dict(markets)
    Y = np.column_stack([df[k].astype(bool).to_numpy() for k in mkeys]).astype(np.float32)
    base = Y.mean(axis=0)

    # ── 逐層擴展 ──
    level = [(i,) for i in range(len(names))]
    all_rows = []
    cur_masks = M.copy()
    for depth in range(1, max_depth + 1):
        if len(level) == 0:
            break
        log(f"{label_prefix}深度 {depth}：候選 {len(level):,} 組")
        keep_idx = []
        # 分塊算，避免記憶體爆掉
        CH = 20000
        for s in range(0, len(level), CH):
            chunk = cur_masks[s:s + CH]
            hits, n = counts_for(chunk, Y)
            ok = n >= min_n
            for j in np.nonzero(ok)[0]:
                gi = s + j
                nj = n[j]
                rates = hits[j] / nj
                best = int(np.argmax(rates))
                keep_idx.append(gi)
                for mi, mk in enumerate(mkeys):
                    r = rates[mi]
                    if r < target and (1 - r) < target:
                        continue  # 兩個方向都不夠極端就不記錄
                    all_rows.append({
                        "market": mk, "depth": depth,
                        "preds": level[gi], "n": int(nj),
                        "hits": int(round(hits[j][mi])),
                        "rate": float(r), "base": float(base[mi]),
                    })
        if depth == max_depth:
            break
        # 擴展：只從仍有足夠 support 的組合往下長
        keep = [i for i in keep_idx]
        if not keep:
            break
        # 只保留 support 前 top_k 大的，控制爆炸
        supports = cur_masks[keep].sum(axis=1)
        order = np.argsort(-supports)[:top_k]
        parents = [(keep[i], level[keep[i]]) for i in order]
        new_level, new_masks = [], []
        for pidx, pred in parents:
            last = pred[-1]
            for i in range(last + 1, len(names)):
                # 同一個特徵不要自己 AND 自己（例如 hi15 AND hi30）
                if names[i].split(":")[0] == names[last].split(":")[0]:
                    continue
                if any(names[i].split(":")[0] == names[p].split(":")[0] for p in pred):
                    continue
                m = cur_masks[pidx] & M[i]
                s = m.sum()
                if s < min_n:
                    continue
                new_level.append(pred + (i,))
                new_masks.append(m)
        if not new_level:
            break
        level = new_level
        cur_masks = np.vstack(new_masks)
        log(f"{label_prefix}  → 下一層 {len(level):,} 組")

    if not all_rows:
        log(f"{label_prefix}沒有符合門檻的條件")
        return pd.DataFrame(), {}

    res = pd.DataFrame(all_rows)
    # 反向（小於 target 的用反面玩法表達）在 markets 已包含 under，故只保留正向高命中
    res = res[res["rate"] >= target].copy()
    res["wilson"] = wilson_lb_vec(res["hits"].to_numpy(), res["n"].to_numpy())
    res["lift"] = res["rate"] - res["base"]
    res["p"] = stats.binom.sf(res["hits"] - 1, res["n"], res["base"])
    res["q"] = bh_qvalues(res["p"].to_numpy())
    res["label"] = res["preds"].apply(lambda ps: " + ".join(labels[i] for i in ps))
    res["market_zh"] = res["market"].map(mlabels)
    res["be_odds"] = 1 / res["rate"]
    res = res.sort_values(["rate", "n"], ascending=[False, False]).reset_index(drop=True)
    log(f"{label_prefix}命中率 ≥{target:.0%} 的條件：{len(res)} 組")
    meta = {"predicates": len(names), "rows": n_rows,
            "base_rates": {k: float(b) for k, b in zip(mkeys, base)}}
    return res, meta


def permutation_max_rate(df, kind, markets, min_n=25, iters=8, max_depth=2, target=0.0):
    """把結果標籤打亂後重跑，看『純運氣』能刷出多高的命中率。"""
    df = add_derived(prep_markets(df, kind), kind)
    names, labels, M = build_predicates(df, kind)
    mkeys = [m for m, _ in markets if m in df.columns]
    Y0 = np.column_stack([df[k].astype(bool).to_numpy() for k in mkeys]).astype(np.float32)
    rng = np.random.default_rng(7)
    maxes = []
    # 只用深度 1-2（深度 3 太慢），足以估計量級
    pairs, masks = [], []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if names[i].split(":")[0] == names[j].split(":")[0]:
                continue
            m = M[i] & M[j]
            if m.sum() >= min_n:
                pairs.append((i, j))
                masks.append(m)
    if not masks:
        return None
    MM = np.vstack(masks)
    log(f"  permutation 空間：{len(masks):,} 組（深度2）")
    for it in range(iters):
        Y = Y0[rng.permutation(len(df))]
        best = 0.0
        CH = 20000
        for s in range(0, len(MM), CH):
            hits, n = counts_for(MM[s:s + CH], Y)
            with np.errstate(invalid="ignore", divide="ignore"):
                rates = hits / n[:, None]
            rates[n < min_n] = 0
            best = max(best, float(np.nanmax(rates)))
        maxes.append(best)
        log(f"  permutation {it + 1}/{iters}: 最高命中率 {best:.3f}")
    return {"iters": iters, "max_rates": maxes,
            "mean_max": float(np.mean(maxes)), "p95_max": float(np.percentile(maxes, 95))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=25)
    ap.add_argument("--target", type=float, default=0.72)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--train-cut", default="2026-07-15")
    ap.add_argument("--perm-iters", type=int, default=6)
    args = ap.parse_args()

    tg = pd.read_parquet(f"{DATA}/teamgames.parquet")
    gd = pd.read_parquet(f"{DATA}/gamesds.parquet")
    # 前 25 場左右的滾動統計太雜訊，排除球隊出賽 <25 場的列
    tg = tg[(tg["my_gp"] >= 25) & (tg["op_gp"] >= 25)].copy()
    gd = gd[(gd["home_gp"] >= 25) & (gd["away_gp"] >= 25)].copy()
    log(f"可用資料：team-game {len(tg)} 列、game {len(gd)} 列")

    out = {"season": 2026, "params": vars(args)}

    # ── 全季挖掘（描述性：使用者要的「命中率 75% 以上」）──
    tg_res, tg_meta = mine(tg, "tg", TG_MARKETS, min_n=args.min_n, target=args.target,
                           max_depth=args.depth, label_prefix="[隊伍視角] ")
    g_res, g_meta = mine(gd, "g", G_MARKETS, min_n=args.min_n, target=args.target,
                         max_depth=args.depth, label_prefix="[全場總分] ")

    # ── 時間切分驗證 ──
    cut = args.train_cut
    tg_tr = tg[tg["date"] < cut]
    tg_te = tg[tg["date"] >= cut]
    gd_tr = gd[gd["date"] < cut]
    gd_te = gd[gd["date"] >= cut]
    log(f"訓練/測試切分 {cut}：team-game {len(tg_tr)}/{len(tg_te)}、game {len(gd_tr)}/{len(gd_te)}")
    tg_train_res, _ = mine(tg_tr, "tg", TG_MARKETS, min_n=max(15, args.min_n // 2),
                           target=args.target, max_depth=args.depth, label_prefix="[驗證-隊伍] ")
    g_train_res, _ = mine(gd_tr, "g", G_MARKETS, min_n=max(15, args.min_n // 2),
                          target=args.target, max_depth=args.depth, label_prefix="[驗證-全場] ")

    def evaluate_on(res, df_test, kind, markets):
        """把訓練期找到的條件套到測試期。"""
        if res.empty:
            return res
        df_test = add_derived(prep_markets(df_test, kind), kind)
        names, labels, M = build_predicates(df_test, kind, min_support=1)
        name_to_i = {n: i for i, n in enumerate(names)}
        # 需要同一組 predicate 名稱；訓練期的 predicate 用訓練期分位數，
        # 這裡重新以訓練期門檻切分測試期 → 由呼叫端傳入相同 df 的 mask 較穩，
        # 因此改用「重算 label 對應」的近似：以名稱對齊。
        rows = []
        for _, r in res.iterrows():
            miss = False
            mask = np.ones(len(df_test), bool)
            for nm in r["pred_names"]:
                i = name_to_i.get(nm)
                if i is None:
                    miss = True
                    break
                mask &= M[i]
            if miss or mask.sum() == 0:
                rows.append((np.nan, 0))
                continue
            y = df_test[r["market"]].astype(bool).to_numpy()[mask]
            rows.append((float(y.mean()), int(mask.sum())))
        res = res.copy()
        res["test_rate"] = [a for a, _ in rows]
        res["test_n"] = [b for _, b in rows]
        return res

    # pred_names 供跨期對齊
    def attach_names(res, df, kind):
        if res.empty:
            return res
        names, labels, M = build_predicates(df, kind)
        res = res.copy()
        res["pred_names"] = res["preds"].apply(lambda ps: [names[i] for i in ps])
        return res

    tg_train_res = attach_names(tg_train_res, add_derived(prep_markets(tg_tr, "tg"), "tg"), "tg")
    g_train_res = attach_names(g_train_res, add_derived(prep_markets(gd_tr, "g"), "g"), "g")
    tg_val = evaluate_on(tg_train_res, tg_te, "tg", TG_MARKETS)
    g_val = evaluate_on(g_train_res, gd_te, "g", G_MARKETS)

    # ── permutation 對照 ──
    log("permutation 對照（隊伍視角）")
    perm_tg = permutation_max_rate(tg, "tg", TG_MARKETS, min_n=args.min_n, iters=args.perm_iters)

    def pack(res, limit=400):
        if res is None or res.empty:
            return []
        cols = ["market", "market_zh", "label", "n", "hits", "rate", "base", "lift",
                "wilson", "p", "q", "be_odds", "depth"]
        extra = [c for c in ("test_rate", "test_n") if c in res.columns]
        r = res.head(limit)[cols + extra]
        return r.round(4).to_dict("records")

    out["teamgame"] = {"meta": tg_meta, "conditions": pack(tg_res)}
    out["game"] = {"meta": g_meta, "conditions": pack(g_res)}
    out["validation"] = {
        "cut": cut,
        "teamgame": pack(tg_val, 300),
        "game": pack(g_val, 300),
    }
    out["permutation"] = perm_tg
    p = jdump(out, f"{OUTPUT}/conditions.json")
    log(f"寫出 {p}")

    # 摘要
    for tag, res in (("隊伍視角", tg_res), ("全場總分", g_res)):
        if res.empty:
            continue
        hi = res[(res["rate"] >= 0.75)]
        log(f"{tag}：命中率≥75% 共 {len(hi)} 組（q<0.05：{int((hi['q'] < 0.05).sum())} 組）")
        for _, r in hi.head(8).iterrows():
            log(f"  {r['market_zh']:<22} {r['rate']:.1%} ({r['hits']}/{r['n']}) "
                f"基準{r['base']:.1%} q={r['q']:.3g} ← {r['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
