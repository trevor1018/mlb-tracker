"""回測：把樣本外機率換成期望值 → output/backtest.json

關鍵設計：沒有真實賠率時，怎麼算 ROI 才不會自欺？
  假設賠率 = (1 / 該玩法的基準命中率) × 返還率
  例：單隊小分 5.5 全聯盟命中 68% → 公正賠率 1.47 → 台彩開 1.47×0.90 ≈ 1.32
  這樣一來，只有當我們的選注命中率「相對基準率」高出超過 1/返還率（約 11%）
  才會賺 —— 那才是真正的優勢。用固定 1.85 去押基準率 78% 的盤是自欺欺人。

兩種下法：
  A. 單注：機率 ≥ 門檻就下
  B. 台彩 4 選 3 串關：每天挑機率最高的 4 腳（同場只取一腳，符合台彩
     「同場不同玩法不能同一單」），組成 C(4,3)=4 張三關票

只保留台彩實際會開的盤口線。
"""
import argparse
import sys
from itertools import combinations

import numpy as np
import pandas as pd
from scipy import stats

from common import DATA, OUTPUT, jdump, log

MARKET_ZH = {
    "over": "大分", "under": "小分",
    "win": "不讓分", "cover_m15": "讓分1.5", "cover_p15": "受讓1.5",
    "cover_m25": "讓分2.5", "cover_p25": "受讓2.5",
    "tt_over": "單隊大分", "tt_under": "單隊小分",
}
# 台彩 MLB 常見盤口
ALLOWED = set()
for line in ("7.5", "8.5", "9.5", "10.5"):
    ALLOWED |= {f"over_{line}", f"under_{line}"}
for side in ("home", "away"):
    ALLOWED |= {f"{side}_win", f"{side}_cover_m15", f"{side}_cover_p15",
                f"{side}_cover_m25", f"{side}_cover_p25"}
    for line in ("3.5", "4.5", "5.5"):
        ALLOWED |= {f"{side}_tt_over_{line}", f"{side}_tt_under_{line}"}

PAYOUTS = [0.88, 0.90, 0.92, 0.95]
DEFAULT_PAYOUT = 0.90


def zh(market):
    if market.startswith(("over_", "under_")):
        kind, line = market.split("_")
        return f"{MARKET_ZH[kind]} {line}"
    side, rest = market.split("_", 1)
    s = "主" if side == "home" else "客"
    if rest.startswith("tt_"):
        kind, line = rest.rsplit("_", 1)
        return f"{s}隊{MARKET_ZH[kind]} {line}"
    return f"{s}隊{MARKET_ZH.get(rest, rest)}"


def market_bases(df):
    return df.groupby("market")["y"].mean().to_dict()


def single_bets(df, bases, thresholds=(0.6, 0.65, 0.7, 0.75, 0.8)):
    rows = []
    for market, g in df.groupby("market"):
        base = bases[market]
        if base <= 0 or base >= 1:
            continue
        fair = 1 / base
        for t in thresholds:
            sel = g[g["p"] >= t]
            if len(sel) < 20:
                continue
            hit = float(sel["y"].mean())
            lift = hit / base
            rows.append({
                "market": market, "market_zh": zh(market), "thr": t,
                "n": int(len(sel)), "hit": round(hit, 4), "base": round(base, 4),
                "lift": round(lift, 4),
                "fair_odds": round(fair, 3),
                "assumed_odds": round(fair * DEFAULT_PAYOUT, 3),
                "be_hit": round(base / DEFAULT_PAYOUT, 4),   # 需要的命中率
                "roi": {str(p): round(lift * p - 1, 4) for p in PAYOUTS},
                "avg_p": round(float(sel["p"].mean()), 4),
                "p_value": round(float(stats.binom.sf(int(sel["y"].sum()) - 1, len(sel), base)), 5),
            })
    rows.sort(key=lambda r: (-r["lift"], -r["n"]))
    return rows


def parlay_backtest(df, bases, legs_per_ticket=3, pool=4, min_p=0.6,
                    payout=DEFAULT_PAYOUT, stake=100, market_filter=None,
                    min_lift=1.0):
    d = df.copy()
    if market_filter:
        d = d[d["market"].isin(market_filter)]
    d = d[d["p"] >= min_p].copy()
    # 依「相對基準率的優勢」排序，而不是原始機率（否則永遠挑基準率最高的盤）
    d["base"] = d["market"].map(bases)
    d["edge"] = d["p"] / d["base"]
    d = d[d["edge"] >= min_lift]
    days, total_cost, total_payout, tickets_won, tickets_all = [], 0.0, 0.0, 0, 0
    for date, g in d.groupby("date"):
        g = g.sort_values("edge", ascending=False)
        picked, used = [], set()
        for _, r in g.iterrows():
            if r["pk"] in used:
                continue
            picked.append(r)
            used.add(r["pk"])
            if len(picked) == pool:
                break
        if len(picked) < legs_per_ticket:
            continue
        legs = pd.DataFrame(picked)
        legs["odds"] = (1 / legs["base"]) * payout
        n_t, won, pay = 0, 0, 0.0
        for combo in combinations(range(len(legs)), legs_per_ticket):
            n_t += 1
            odds = float(np.prod([legs.iloc[i]["odds"] for i in combo]))
            if all(legs.iloc[i]["y"] == 1 for i in combo):
                won += 1
                pay += stake * odds
        cost = n_t * stake
        total_cost += cost
        total_payout += pay
        tickets_won += won
        tickets_all += n_t
        days.append({
            "date": date, "legs": len(legs), "tickets": n_t, "won": won,
            "cost": cost, "payout": round(pay, 1),
            "hit_legs": int(legs["y"].sum()),
            "picks": [{"market_zh": zh(r["market"]), "p": round(float(r["p"]), 3),
                       "odds": round(float(r["odds"]), 2), "y": int(r["y"])}
                      for _, r in legs.iterrows()],
        })
    if not days:
        return None
    leg_hit = float(np.mean([x["hit_legs"] / x["legs"] for x in days]))
    return {
        "params": {"legs_per_ticket": legs_per_ticket, "pool": pool, "min_p": min_p,
                   "payout": payout, "stake": stake, "min_lift": min_lift,
                   "market_filter": sorted(market_filter) if market_filter else None},
        "days": len(days), "cost": round(total_cost, 1), "payout": round(total_payout, 1),
        "roi": round(total_payout / total_cost - 1, 4) if total_cost else None,
        "leg_hit_rate": round(leg_hit, 4),
        "ticket_hit_rate": round(tickets_won / tickets_all, 4) if tickets_all else None,
        "roi_by_payout": {}, "log": days[-12:],
    }


def two_stage(df, bases, cut="2026-07-20", payout=DEFAULT_PAYOUT,
              min_lift=1.10, min_n=30):
    """策略挑選也要樣本外：cut 之前挑出有優勢的（盤口, 門檻），cut 之後才下注。"""
    tr = df[df["date"] < cut]
    te = df[df["date"] >= cut]
    if tr.empty or te.empty:
        return None
    picked = []
    for r in single_bets(tr, bases, thresholds=(0.6, 0.65, 0.7, 0.75, 0.8)):
        if r["n"] >= min_n and r["lift"] >= min_lift:
            picked.append((r["market"], r["thr"], r["lift"], r["n"]))
    if not picked:
        return {"cut": cut, "selected": 0, "note": "訓練期沒有任何組合達到門檻"}
    rows, tot_stake, tot_ret = [], 0.0, 0.0
    for market, thr, lift_tr, n_tr in picked:
        g = te[(te["market"] == market) & (te["p"] >= thr)]
        if len(g) == 0:
            continue
        base = bases[market]
        odds = (1 / base) * payout
        hit = float(g["y"].mean())
        stake = len(g) * 100.0
        ret = float(g["y"].sum()) * 100.0 * odds
        tot_stake += stake
        tot_ret += ret
        rows.append({"market": market, "market_zh": zh(market), "thr": thr,
                     "train_lift": round(lift_tr, 3), "train_n": n_tr,
                     "test_n": int(len(g)), "test_hit": round(hit, 4),
                     "base": round(base, 4), "assumed_odds": round(odds, 3),
                     "test_roi": round(hit * odds - 1, 4)})
    rows.sort(key=lambda r: -r["test_roi"])
    return {"cut": cut, "payout": payout, "min_lift": min_lift, "min_n": min_n,
            "selected": len(picked), "evaluated": len(rows),
            "total_stake": round(tot_stake, 1), "total_return": round(tot_ret, 1),
            "roi": round(tot_ret / tot_stake - 1, 4) if tot_stake else None,
            "detail": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-p", type=float, default=0.6)
    ap.add_argument("--payout", type=float, default=DEFAULT_PAYOUT)
    args = ap.parse_args()

    df = pd.read_parquet(f"{DATA}/oos_market_probs.parquet")
    df = df[df["market"].isin(ALLOWED)].copy()
    bases = market_bases(df)
    log(f"樣本外 {len(df):,} 列（僅台彩常見盤口）、{df['pk'].nunique()} 場、"
        f"{df['date'].min()} ~ {df['date'].max()}")

    singles = single_bets(df, bases)
    log(f"── 單注：依「相對基準率優勢」排序（返還率 {args.payout:.0%} 下需 lift > "
        f"{1/args.payout:.3f} 才賺）──")
    for r in singles[:14]:
        log(f"  {r['market_zh']:<14} 機率≥{r['thr']:.0%} → {r['n']:>4}注 命中 {r['hit']:.1%} "
            f"(基準 {r['base']:.1%}, lift {r['lift']:.3f}) 假設賠率 {r['assumed_odds']:.2f} "
            f"ROI {r['roi'][str(args.payout)]:+.1%}")

    profitable = [r for r in singles if r["roi"][str(args.payout)] > 0 and r["n"] >= 40]
    log(f"在 {args.payout:.0%} 返還率下，樣本外仍為正期望值的組合：{len(profitable)} 個")
    for r in profitable[:10]:
        log(f"  ✅ {r['market_zh']:<14} 機率≥{r['thr']:.0%} {r['n']}注 命中 {r['hit']:.1%} "
            f"ROI {r['roi'][str(args.payout)]:+.1%}")

    good = sorted({r["market"] for r in singles
                   if r["lift"] >= 1.05 and r["n"] >= 40})
    parlays = {}
    for tag, mf, ml in (("全部盤口", None, 1.0),
                        ("有優勢盤口", good or None, 1.0),
                        ("嚴選(lift≥1.1)", good or None, 1.1)):
        for pool, legs in ((4, 3), (3, 2), (2, 2)):
            r = parlay_backtest(df, bases, legs_per_ticket=legs, pool=pool,
                                min_p=args.min_p, payout=args.payout,
                                market_filter=mf, min_lift=ml)
            if r:
                key = f"{tag}｜{pool}選{legs}"
                parlays[key] = r
                log(f"  {key:<22} {r['days']}天 單腳命中 {r['leg_hit_rate']:.1%} "
                    f"票命中 {r['ticket_hit_rate']:.1%} ROI {r['roi']:+.1%}")

    ts = two_stage(df, bases, payout=args.payout)
    log("── 兩階段驗證（策略挑選也不准偷看未來）──")
    if ts and ts.get("roi") is not None:
        log(f"  7/20 之前挑出 {ts['selected']} 個組合 → 7/20 之後實際下 "
            f"{sum(r['test_n'] for r in ts['detail'])} 注，ROI {ts['roi']:+.1%}")
        for r in ts["detail"][:8]:
            log(f"    {r['market_zh']:<14} 機率≥{r['thr']:.0%} 訓練lift {r['train_lift']:.2f} "
                f"→ 測試 {r['test_n']}注 命中 {r['test_hit']:.1%} ROI {r['test_roi']:+.1%}")
    else:
        log(f"  {ts}")

    out = {"season": 2026, "two_stage": ts, "oos_range": [str(df["date"].min()), str(df["date"].max())],
           "payout_assumed": args.payout, "payouts": PAYOUTS,
           "bases": {k: round(float(v), 4) for k, v in bases.items()},
           "singles": singles[:150], "parlays": parlays,
           "profitable_count": len(profitable),
           "note": "假設賠率 =（1/基準命中率）×返還率。命中率相對基準率的 lift 要大於 "
                   "1/返還率（約 1.11）才有正期望值。真實台彩賠率請自行確認。"}
    p = jdump(out, f"{OUTPUT}/backtest.json")
    log(f"寫出 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
