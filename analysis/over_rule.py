"""把「全場大分」這個唯一有優勢的家族，做成一條看得懂、能手動套用的規則。

回測顯示全場大分是唯一 bootstrap 區間全正的家族。但「模型機率 ≥ 65%」這種說法
沒辦法離開程式使用。這支腳本改用最直覺的量：

    差值 = 模型預估總分 μ − 盤口線

然後看「差值落在某個區間時，大分實際命中率是多少」。這樣你在台彩看到 8.5 的線、
知道模型預估 9.8 分（差 +1.3），就能直接查表。

輸出 output/over_rule.json
"""
import argparse
import sys

import numpy as np
import pandas as pd
from scipy import stats

from common import DATA, OUTPUT, jdump, jload, log

LINES = (7.5, 8.5, 9.5, 10.5)
EDGES = (-99, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 99)


def wilson(h, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = h / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - r) / d, (c + r) / d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payout", type=float, default=0.90)
    args = ap.parse_args()

    df = pd.read_parquet(f"{DATA}/run_preds.parquet")
    df["mu_total"] = df["mu_home"] + df["mu_away"]
    df["total"] = df["runs_home"] + df["runs_away"]
    log(f"樣本外 {len(df)} 場（{df['date'].min()} ~ {df['date'].max()}）"
        f"，預估總分 {df['mu_total'].min():.1f}~{df['mu_total'].max():.1f}")

    # 球場得分環境（給第二層切分用）
    try:
        gd = pd.read_parquet(f"{DATA}/gamesds.parquet")[["pk", "park_factor", "temp",
                                                         "day_game", "roof"]]
        df = df.merge(gd, on="pk", how="left")
    except Exception:
        df["park_factor"] = np.nan

    out = {"oos_range": [str(df["date"].min()), str(df["date"].max())],
           "games": int(len(df)), "payout": args.payout, "lines": {}}

    for line in LINES:
        base = float((df["total"] > line).mean())
        fair = 1 / base if base else None
        rows = []
        d = df.copy()
        d["diff"] = d["mu_total"] - line
        d["bucket"] = pd.cut(d["diff"], EDGES)
        for b, g in d.groupby("bucket", observed=True):
            n = len(g)
            if n < 15:
                continue
            h = int((g["total"] > line).sum())
            rate = h / n
            lo, hi = wilson(h, n)
            lift = rate / base if base else None
            rows.append({
                "bucket": f"{b.left:+.1f} ~ {b.right:+.1f}".replace("-99.0", "更低")
                          .replace("+99.0", "更高"),
                "n": n, "hits": h, "rate": round(rate, 4),
                "ci95": [round(lo, 4), round(hi, 4)],
                "lift": round(lift, 4) if lift else None,
                "roi": round(lift * args.payout - 1, 4) if lift else None,
                "p_value": round(float(stats.binom.sf(h - 1, n, base)), 5),
                "mu_avg": round(float(g["mu_total"].mean()), 2),
            })
        out["lines"][str(line)] = {"base": round(base, 4),
                                   "fair_odds": round(fair, 3) if fair else None,
                                   "assumed_odds": round(fair * args.payout, 3) if fair else None,
                                   "buckets": rows}
        log(f"── 大分 {line}（基準 {base:.1%}，假設賠率 {fair * args.payout:.2f}）──")
        for r in rows:
            log(f"  μ−線 {r['bucket']:<14} {r['n']:>4}場 命中 {r['rate']:.1%} "
                f"(95%CI {r['ci95'][0]:.0%}~{r['ci95'][1]:.0%}) lift {r['lift']:.3f} "
                f"ROI {r['roi']:+.1%}")

    # 加上球場切分：高得分球場 vs 低得分球場
    if df["park_factor"].notna().any():
        pf_hi = df["park_factor"] >= df["park_factor"].quantile(0.7)
        pf_lo = df["park_factor"] <= df["park_factor"].quantile(0.3)
        park = {}
        for name, mask in (("高得分球場(前30%)", pf_hi), ("低得分球場(後30%)", pf_lo)):
            sub = {}
            for line in LINES:
                g = df[mask]
                base = float((df["total"] > line).mean())
                n = len(g)
                h = int((g["total"] > line).sum())
                if n < 30:
                    continue
                rate = h / n
                sub[str(line)] = {"n": n, "rate": round(rate, 4),
                                  "lift": round(rate / base, 4) if base else None,
                                  "roi": round(rate / base * args.payout - 1, 4) if base else None}
            park[name] = sub
        out["by_park"] = park
        log("── 依球場得分環境 ──")
        for name, sub in park.items():
            txt = "、".join(f"{k}: {v['rate']:.0%}(lift {v['lift']:.2f})" for k, v in sub.items())
            log(f"  {name}：{txt}")

    # ── 關鍵對照：把「球場」這件事扣掉之後，優勢還在嗎 ──
    # 我的假設賠率是「該盤口線的聯盟平均命中率」，但莊家一定會針對球場調整盤口。
    # 所以這裡改用「該球場自己的歷史大分率」當基準，看優勢是否消失。
    park_ctrl = {}
    if df["park_factor"].notna().any() and "venue" in pd.read_parquet(f"{DATA}/gamesds.parquet").columns:
        gd2 = pd.read_parquet(f"{DATA}/gamesds.parquet")[["pk", "venue"]]
        dd = df.merge(gd2, on="pk", how="left")
        for line in LINES:
            over = dd["total"] > line
            league = float(over.mean())
            # 各球場的大分率（收縮到聯盟，權重 20 場）
            grp = dd.assign(o=over).groupby("venue")["o"].agg(["sum", "count"])
            park_rate = ((grp["sum"] + league * 20) / (grp["count"] + 20)).to_dict()
            dd[f"pb_{line}"] = dd["venue"].map(park_rate).fillna(league)
            diff = dd["mu_total"] - line
            sel = diff >= 1.5 if line <= 8.5 else diff >= 0.0
            n = int(sel.sum())
            if n < 30:
                continue
            hit = float(over[sel].mean())
            lift_league = hit / league
            lift_park = hit / float(dd.loc[sel, f"pb_{line}"].mean())
            park_ctrl[str(line)] = {
                "rule": "μ−線 ≥ +1.5" if line <= 8.5 else "μ−線 ≥ 0",
                "n": n, "hit": round(hit, 4),
                "league_base": round(league, 4),
                "park_base_avg": round(float(dd.loc[sel, f"pb_{line}"].mean()), 4),
                "lift_vs_league": round(lift_league, 4),
                "lift_vs_park": round(lift_park, 4),
                "roi_league_pricing": round(lift_league * args.payout - 1, 4),
                "roi_park_pricing": round(lift_park * args.payout - 1, 4),
            }
        out["park_control"] = park_ctrl
        log("── 球場校正對照（把莊家會調盤口這件事納入）──")
        for line, v in park_ctrl.items():
            log(f"  大分 {line}（{v['rule']}）{v['n']} 場命中 {v['hit']:.1%}｜"
                f"對聯盟基準 lift {v['lift_vs_league']:.3f} (ROI {v['roi_league_pricing']:+.1%})｜"
                f"對球場基準 lift {v['lift_vs_park']:.3f} (ROI {v['roi_park_pricing']:+.1%})")

    # 綜合建議：找出 lift 最高且樣本足夠的規則
    best = []
    for line, d in out["lines"].items():
        for b in d["buckets"]:
            if b["n"] >= 40 and (b["lift"] or 0) >= 1.11:
                best.append({"line": line, **b,
                             "assumed_odds": d["assumed_odds"]})
    best.sort(key=lambda r: -(r["lift"] or 0))
    out["recommended"] = best[:8]
    log("── 建議規則（樣本≥40 且 lift≥1.11）──")
    for r in best[:8]:
        log(f"  盤口 {r['line']} 大分，當「模型預估總分 − 盤口線」在 {r['bucket']}："
            f"{r['n']} 場命中 {r['rate']:.1%}、lift {r['lift']:.2f}、"
            f"假設賠率 {r['assumed_odds']} → ROI {r['roi']:+.1%}")
    if not best:
        log("  沒有樣本足夠且 lift 達標的規則")

    out["caveat"] = ("假設賠率用的是「該盤口線的聯盟平均命中率」，但真實莊家會針對"
                     "球場、天氣、先發逐場調整盤口與賠率。park_control 區塊就是在檢查："
                     "把球場效應扣掉之後優勢還剩多少。剩得不多，就表示表面上的優勢"
                     "大半是我的定價假設過於簡化造成的，必須接真實賠率才能確認。")
    p = jdump(out, f"{OUTPUT}/over_rule.json")
    log(f"寫出 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
