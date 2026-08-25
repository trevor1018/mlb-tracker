"""把所有分析結果整理成一份可讀的 Markdown → ANALYSIS_REPORT.md"""
import sys
from datetime import datetime

from common import OUTPUT, ROOT, jload, log


def f_pct(x, nd=1):
    return "—" if x is None else f"{x * 100:.{nd}f}%"


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def sec_conditions(C, side_key, title):
    d = C[side_key]
    meta = d["meta"]
    lines = [f"### {title}", "",
             f"- 資料列數：{meta['rows']:,}｜條件基元：{meta['predicates']}｜"
             f"檢定假設數：{meta['hypotheses']:,}", ""]
    A = d["tierA"]
    if not A:
        lines += ["**Tier A（過四關）：0 組。** 也就是說在這個範圍內，"
                  "沒有任何條件能同時做到「≥75%、統計顯著、勝過運氣線、四個時段都穩」。", ""]
    else:
        lines += [f"**Tier A（過四關）：{len(A)} 組**", "",
                  table(["玩法", "條件", "命中率", "樣本", "Wilson下界", "基準", "運氣線",
                         "分段命中率", "保本賠率"],
                        [[r["market_zh"], r["label"], f_pct(r["rate"]), f"{r['hits']}/{r['n']}",
                          f_pct(r["wilson"]), f_pct(r["base"]), f_pct(r["chance_p95"]),
                          " / ".join("—" if b is None else f"{b:.0%}" for b in r["block_rates"]),
                          f"{r['be_odds']:.2f}"] for r in A[:25]]), ""]
    B = d["tierB"][:15]
    if B:
        lines += ["<details><summary>Tier B（顯著但沒完全過關，僅供觀察）</summary>", "",
                  table(["玩法", "條件", "命中率", "樣本", "基準", "運氣線"],
                        [[r["market_zh"], r["label"], f_pct(r["rate"]), f"{r['hits']}/{r['n']}",
                          f_pct(r["base"]), f_pct(r["chance_p95"])] for r in B]),
                  "", "</details>", ""]
    oos = d["oos"]
    if oos["summary"]:
        lines += [f"**樣本外檢驗（{oos['cut']} 之前挖掘 → 之後驗證）**", "",
                  table(["玩法", "候選數", "訓練期均值", "測試期均值", "基準",
                         "測試期仍≥75% 的比例"],
                        [[s["market"], int(s["cands"]), f"{s['mean_train']:.1%}",
                          f"{s['mean_test']:.1%}", f"{s['base']:.1%}", f"{s['hold75']:.0%}"]
                         for s in oos["summary"]]), ""]
    return lines


def main():
    C = jload(f"{OUTPUT}/conditions.json")
    T = jload(f"{OUTPUT}/team_conditions.json")
    S = jload(f"{OUTPUT}/team_splits.json")
    try:
        M = jload(f"{OUTPUT}/models.json")
    except Exception:
        M = None
    try:
        SL = jload(f"{OUTPUT}/slate.json")
    except Exception:
        SL = None

    L = []
    L += [f"# MLB 2026 資料分析報告", "",
          f"產生時間：{datetime.now():%Y-%m-%d %H:%M}｜資料範圍：2026 球季開幕 ~ 8/24"
          f"（{S['meta']['games']} 場、{S['meta']['pitches']:,} 球 Statcast 逐球資料）", "",
          "## 0. 一句話結論", ""]

    nA_tg = len(C["teamgame"]["tierA"])
    nA_g = len(C["game"]["tierA"])
    L += [f"用嚴格的統計把關之後，全季 ~2000 場、上萬組條件裡真正站得住腳的高命中條件"
          f"只有 **{nA_tg + nA_g} 組**（隊伍視角 {nA_tg}、全場總分 {nA_g}）。"
          "網路上常見的「某條件命中 80%」多半是搜尋空間太大造成的錯覺 —— "
          "本報告用 permutation（把結果打亂重跑）算出「純運氣能刷到的最高命中率」當對照線，"
          "沒超過那條線的一律不算。", ""]

    L += ["## 1. 方法論（為什麼可以相信這份數字）", "",
          "1. **不使用賽後資訊**：每一場的特徵都是「該場開打前」的累積值（as-of 快照），"
          "包含球隊對左右投 wOBA、對各球種 wOBA、先發投手對左右打 wOBA、牛棚近 14 天負荷等。",
          "2. **四道關卡**：樣本量 ≥ 40 → BH-FDR 校正後 q<0.05 → 命中率高於 permutation 運氣線 "
          "→ 四個時段都不崩（min block rate）。",
          "3. **真樣本外**：另外用 7/15 前的資料挖掘、7/15 後驗證，看條件會不會失效。",
          "4. **模型交叉驗證**：每半個月重新訓練一次模型，只對訓練期之後的比賽預測，"
          "所以模型的 AUC / 校準表全都是樣本外。", ""]

    L += ["## 2. 交付物一：達到 75% 以上命中率的條件", ""]
    L += sec_conditions(C, "teamgame", "隊伍視角（不讓分 / 讓分 / 單隊大小分 / 前5局）")
    L += sec_conditions(C, "game", "全場總分（大小分 / 前5局大小分 / NRFI）")

    L += ["## 3. 交付物二：每一隊的最佳條件", "",
          "同一隊只有約 100 場可用樣本，而搜尋空間上萬組，所以**一定**會出現 100% 命中的組合。"
          "下表同時列出該隊的「運氣線」（permutation 下純運氣能達到的最高命中率）："
          "命中率沒超過運氣線的，就當成雜訊看。", ""]
    rows = []
    for tid, t in sorted(T["teams"].items(), key=lambda kv: -(kv[1]["best"]["wilson"] if kv[1]["best"] else 0)):
        b = t["best"]
        if not b:
            continue
        rows.append([t["zh"], t["games"], b["market_zh"], b["label"][:46],
                     f_pct(b["rate"]), f"{b['hits']}/{b['n']}", f_pct(b["wilson"]),
                     f_pct(b["base_league"]), f"{t['chance_max_rate_p95']:.0%}",
                     "✅" if b["beats_chance"] else "✗", f"{b['be_odds']:.2f}"])
    L += [table(["球隊", "場次", "玩法", "條件", "命中率", "樣本", "Wilson下界",
                 "聯盟基準", "運氣線", "勝過運氣", "保本賠率"], rows), ""]

    if M:
        L += ["## 4. 模型層（樣本外表現）", "",
              "滾動重訓（6 次重訓，只預測訓練期之後的比賽）。Brier 技巧分數 >0 表示比"
              "「一律押基準率」更好；AUC 0.5 = 沒有資訊。", ""]
        mrows = []
        for mk, m in sorted(M["models"].items(), key=lambda kv: -kv[1]["auc"]):
            t70 = next((t for t in m["thresholds"] if t["thr"] == 0.7), None)
            t75 = next((t for t in m["thresholds"] if t["thr"] == 0.75), None)
            mrows.append([m["market_zh"], m["oos_n"], f_pct(m["base"]), f"{m['auc']:.3f}",
                          f"{m['brier_skill']:+.3f}", f"{m['p_max']:.0%}",
                          f"{t70['n']}場 {t70['rate']:.0%}" if t70 else "—",
                          f"{t75['n']}場 {t75['rate']:.0%}" if t75 else "—"])
        L += [table(["玩法", "樣本外場數", "基準率", "AUC", "Brier技巧", "最高機率",
                     "模型≥70%時實際", "模型≥75%時實際"], mrows), ""]
        if M.get("importance"):
            L += ["**哪些分項特徵真的有用**（permutation importance，AUC 掉多少）", ""]
            for mk, feats in M["importance"].items():
                top = ", ".join(f"{f['feature']}({f['auc_drop']:+.3f})" for f in feats[:8])
                L += [f"- `{mk}`：{top}"]
            L += [""]

    L += ["## 5. 單隊分項亮點（Statcast）", ""]
    tm = S["teams"]
    def top_by(path, n=5, reverse=True, fmt=lambda v: f"{v:.3f}"):
        vals = []
        for tid, t in tm.items():
            v = t
            for k in path:
                v = (v or {}).get(k) if isinstance(v, dict) else None
            if v is not None:
                vals.append((t["zh"], v))
        vals.sort(key=lambda x: x[1], reverse=reverse)
        return "、".join(f"{z} {fmt(v)}" for z, v in vals[:n])

    L += [f"- **對左投最強**：{top_by(['bat', 'vs_LHP', 'woba'])}",
          f"- **對左投最弱**：{top_by(['bat', 'vs_LHP', 'woba'], reverse=False)}",
          f"- **對右投最強**：{top_by(['bat', 'vs_RHP', 'woba'])}",
          f"- **對右投最弱**：{top_by(['bat', 'vs_RHP', 'woba'], reverse=False)}",
          f"- **對變化球最弱**：{top_by(['bat', 'by_group', '變化球', 'woba'], reverse=False)}",
          f"- **對速球最強**：{top_by(['bat', 'by_group', '速球', 'woba'])}",
          f"- **日場打擊最強**：{top_by(['bat', 'day', 'woba'])}",
          f"- **投手群壓制左打最好**：{top_by(['pit', 'vs_LHB', 'woba'], reverse=False)}",
          f"- **投手群壓制右打最好**：{top_by(['pit', 'vs_RHB', 'woba'], reverse=False)}", ""]

    if SL and SL.get("picks"):
        L += [f"## 6. 下一批賽事的訊號（{'、'.join(SL['generated_for'])}）", "",
              "「條件數」是有幾組 Tier A/B 條件同時觸發；模型機率與條件同時看才有意義。"
              "**保本賠率** = 1/機率，台彩賠率要高於這個數字才有正期望值。", ""]
        prows = []
        for x in SL["picks"][:30]:
            prows.append([x["date"], x["matchup"], f"{x['scope']}{x['team'] or ''}",
                          x["market_zh"], f_pct(x["p"]), f"{x['be_odds']:.2f}",
                          x["cond_support"]])
        L += [table(["日期", "對戰", "範圍", "玩法", "模型機率", "需要賠率", "條件數"], prows), ""]

    L += ["## 7. 已知限制與下一步", "",
          "- **沒有真實賠率**：所有「命中率」都不是 ROI。台彩賠率若低於保本賠率，"
          "命中率再高也是虧的。下一步應接國際盤歷史賠率來算真正的期望值。",
          "- **樣本仍偏小**：單季 2000 場、單隊 130 場。跨季（2023-2025）可讓條件驗證更硬，"
          "但球員陣容變動會稀釋單隊特性。",
          "- **先發預告會變**：未開打場次的預測依賴 probable pitcher，開賽前要重抓一次。",
          "- **牛棚細節可以更深**：目前只用 14 天負荷與 R/9，可加入個別後援投手可用性。",
          "- **建議的使用方式**：把 Tier A 條件當「觀察名單」，模型機率當「排序」，"
          "實際下注前確認台彩賠率高於保本賠率。", ""]

    out = f"{ROOT}/ANALYSIS_REPORT.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    log(f"寫出 {out}（{len('\n'.join(L)):,} 字）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
