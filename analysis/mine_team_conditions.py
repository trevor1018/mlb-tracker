"""每一隊各自找出命中率最高的「條件 × 玩法」→ output/team_conditions.json

重點在誠實：單隊只有約 100 場可用樣本，而搜尋空間有上萬組條件，
所以一定會有 100% 命中的組合出現。因此每一隊都額外跑 permutation
（把結果標籤打亂）估出「純運氣能刷到的最高命中率」，
並用 Wilson 95% 下界排序，而不是用生命中率排序。
"""
import argparse
import sys

import numpy as np
import pandas as pd
from scipy import stats

from common import DATA, OUTPUT, TEAM_ZH, jdump, log
from mine_conditions import TG_MARKETS, bh_qvalues, wilson_lb_vec
from predicates import add_derived, build_predicates

EXTRA_MARKETS = [
    ("over_7.5", "全場大分 7.5"),
    ("over_8.5", "全場大分 8.5"),
    ("over_9.5", "全場大分 9.5"),
]


def pair_masks(M, names, min_n, sub):
    """在子集合 sub（bool 陣列）內，產生深度 1 與深度 2 的條件遮罩。"""
    idx = np.nonzero(sub)[0]
    Ms = M[:, idx]
    combos, masks = [], []
    ok1 = []
    for i in range(len(names)):
        if Ms[i].sum() >= min_n:
            ok1.append(i)
            combos.append((i,))
            masks.append(Ms[i])
    for a in range(len(ok1)):
        for b in range(a + 1, len(ok1)):
            i, j = ok1[a], ok1[b]
            if names[i].split(":")[0] == names[j].split(":")[0]:
                continue
            m = Ms[i] & Ms[j]
            if m.sum() >= min_n:
                combos.append((i, j))
                masks.append(m)
    return combos, (np.vstack(masks) if masks else np.zeros((0, len(idx)), bool)), idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=14)
    ap.add_argument("--perm-iters", type=int, default=20)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    tg = pd.read_parquet(f"{DATA}/teamgames.parquet")
    tg = tg[(tg["my_gp"] >= 25) & (tg["op_gp"] >= 25)].copy().reset_index(drop=True)
    tg = add_derived(tg, "tg")
    markets = TG_MARKETS + EXTRA_MARKETS
    mkeys = [m for m, _ in markets if m in tg.columns]
    mlabels = dict(markets)
    log(f"資料 {len(tg)} 列、玩法 {len(mkeys)} 種")

    # 門檻用全聯盟計算 → 各隊條件標籤可互相比較
    names, labels, M = build_predicates(tg, "tg", min_support=60)
    log(f"條件基元 {len(names)} 個")
    Y = np.column_stack([tg[k].astype(bool).to_numpy() for k in mkeys]).astype(np.float32)
    base_all = Y.mean(axis=0)

    rng = np.random.default_rng(11)
    teams_out = {}
    for tid in sorted(TEAM_ZH):
        sub = (tg["team"] == tid).to_numpy()
        n_games = int(sub.sum())
        if n_games < 40:
            continue
        combos, masks, idx = pair_masks(M, names, args.min_n, sub)
        if masks.size == 0:
            continue
        Ysub = Y[idx]
        base_team = Ysub.mean(axis=0)
        Mf = masks.astype(np.float32)
        hits = Mf @ Ysub               # (k, m)
        ns = Mf.sum(axis=1)            # (k,)
        rates = hits / ns[:, None]
        wl = wilson_lb_vec(hits, np.repeat(ns[:, None], len(mkeys), axis=1))
        pvals = stats.binom.sf(np.maximum(hits - 1, 0), ns[:, None], base_all[None, :])

        # permutation：同樣搜尋空間下，純運氣的最高命中率
        perm_max = []
        for _ in range(args.perm_iters):
            Yp = Ysub[rng.permutation(len(idx))]
            h = Mf @ Yp
            r = h / ns[:, None]
            perm_max.append(float(np.nanmax(r)))
        perm_mean = float(np.mean(perm_max))
        perm_p95 = float(np.percentile(perm_max, 95))

        flat = []
        for ci in range(len(combos)):
            for mi, mk in enumerate(mkeys):
                if ns[ci] < args.min_n:
                    continue
                flat.append({
                    "market": mk, "market_zh": mlabels[mk],
                    "label": " + ".join(labels[i] for i in combos[ci]),
                    "pred_names": [names[i] for i in combos[ci]],
                    "n": int(ns[ci]), "hits": int(round(hits[ci][mi])),
                    "rate": float(rates[ci][mi]),
                    "base_team": float(base_team[mi]),
                    "base_league": float(base_all[mi]),
                    "wilson": float(wl[ci][mi]),
                    "p": float(pvals[ci][mi]),
                    "depth": len(combos[ci]),
                })
        f = pd.DataFrame(flat)
        f["q"] = bh_qvalues(f["p"].to_numpy())
        f["lift"] = f["rate"] - f["base_league"]
        f["be_odds"] = 1 / f["rate"].clip(lower=1e-6)
        f["beats_chance"] = f["rate"] > perm_p95

        by_wilson = f.sort_values("wilson", ascending=False).head(args.top)
        by_rate = f[f["n"] >= args.min_n].sort_values(["rate", "n"], ascending=[False, False]).head(args.top)
        best = by_wilson.iloc[0] if len(by_wilson) else None

        cols = ["market", "market_zh", "label", "n", "hits", "rate", "base_team",
                "base_league", "lift", "wilson", "p", "q", "be_odds", "depth", "beats_chance"]
        teams_out[str(tid)] = {
            "zh": TEAM_ZH[tid], "games": n_games,
            "search_space": int(len(combos) * len(mkeys)),
            "chance_max_rate_mean": round(perm_mean, 3),
            "chance_max_rate_p95": round(perm_p95, 3),
            "best": (best[cols].round(4).to_dict() if best is not None else None),
            "top_by_wilson": by_wilson[cols].round(4).to_dict("records"),
            "top_by_rate": by_rate[cols].round(4).to_dict("records"),
        }
        b = teams_out[str(tid)]["best"]
        log(f"  {TEAM_ZH[tid]:<4} {n_games:>3}場 空間{len(combos)*len(mkeys):>7,} "
            f"運氣線{perm_p95:.0%} | 最佳(Wilson) {b['market_zh']} "
            f"{b['rate']:.0%} ({b['hits']}/{b['n']}) LB{b['wilson']:.0%} ← {b['label'][:40]}")

    out = {"season": 2026, "params": vars(args), "teams": teams_out}
    p = jdump(out, f"{OUTPUT}/team_conditions.json")
    log(f"寫出 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
