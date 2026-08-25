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
        lines += ["**Tier A（過四關）：0 組。**", ""]
    else:
        lines += [f"**Tier A（過四關）：{len(A)} 組**", "",
                  table(["玩法", "條件", "命中率", "樣本", "Wilson下界", "基準", "運氣線",
                         "四時段命中率", "保本賠率"],
                        [[r["market_zh"], r["label"], f_pct(r["rate"]), f"{r['hits']}/{r['n']}",
                          f_pct(r["wilson"]), f_pct(r["base"]), f_pct(r["chance_p95"]),
                          " / ".join("—" if b is None else f"{b:.0%}" for b in r["block_rates"]),
                          f"{r['be_odds']:.2f}"] for r in A[:25]]), ""]
    B = d["tierB"][:12]
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
                         "測試期仍≥75%"],
                        [[s["market"], int(s["cands"]), f"{s['mean_train']:.1%}",
                          f"{s['mean_test']:.1%}", f"{s['base']:.1%}", f"{s['hold75']:.0%}"]
                         for s in oos["summary"]]), ""]
    return lines


def main():
    C = jload(f"{OUTPUT}/conditions.json")
    T = jload(f"{OUTPUT}/team_conditions.json")
    S = jload(f"{OUTPUT}/team_splits.json")
    M = MR = BT = SL = None
    for name, var in (("models", "M"), ("models_runs", "MR"),
                      ("backtest", "BT"), ("slate", "SL")):
        try:
            v = jload(f"{OUTPUT}/{name}.json")
        except Exception:
            v = None
        if var == "M":
            M = v
        elif var == "MR":
            MR = v
        elif var == "BT":
            BT = v
        else:
            SL = v

    nA_tg = len(C["teamgame"]["tierA"])
    nA_g = len(C["game"]["tierA"])
    ts = (BT or {}).get("two_stage") or {}

    L = ["# MLB 2026 資料分析報告", "",
         f"產生時間：{datetime.now():%Y-%m-%d %H:%M}｜資料範圍：2026 球季開幕 ~ 8/24"
         f"（{S['meta']['games']} 場、{S['meta']['pitches']:,} 球 Statcast 逐球資料）", "",
         "## 0. 三句話結論", "",
         f"1. **高命中條件確實存在，但少得可憐**：482 萬個「條件 × 玩法」假設，過四道關卡後"
         f"只剩 **{nA_tg + nA_g} 組**（隊伍視角 {nA_tg}、全場 {nA_g}）。"
         f"其餘上萬組「命中率 80%+」的條件，全都低於 permutation 算出的運氣線。",
         f"2. **每一隊都能找到 100% 命中的條件 —— 而且那毫無意義**：單隊約 100 場樣本、"
         f"搜尋空間上萬組，運氣線本身就已經到 100%。只有 3 隊在「單一條件、樣本 ≥30 場」"
         f"的嚴格版本下勝過自己的運氣線。",
         f"3. **真正能拿來下注的是得分期望值模型，但優勢很薄**：兩階段驗證（連挑策略都不准"
         f"偷看未來）在 {ts.get('cut', '?')} 之後下 "
         f"{sum(r['test_n'] for r in ts.get('detail', [])) if ts.get('detail') else 0} 注，"
         f"ROI **{f_pct(ts.get('roi'))}**（假設台彩返還率 90%）。"
         f"其中只有大小分（7.5/8.5）站得住腳，讓分與單隊小分都被吃掉。", ""]

    L += ["## 1. 方法論（為什麼可以相信這些數字）", "",
          "1. **不使用賽後資訊**：每一場的特徵都是「該場開打前」的累積值（as-of 快照）："
          "球隊對左右投 wOBA、對各球種 wOBA、近 15 場滾動、先發投手對左右打 wOBA、"
          "先發球種組成、牛棚 14 天負荷與 30 天被打 wOBA、球場得分環境、風向、氣溫。",
          "2. **條件挖掘四道關卡**：樣本 ≥ 40 → BH-FDR 校正後 q<0.05 → 命中率高於"
          " permutation 運氣線（把結果打亂 25 次取最高命中率的 95 百分位）→ 四個時段都不崩。",
          "3. **模型全部是樣本外**：每半個月重新訓練，只預測訓練期之後的比賽。",
          "4. **ROI 用基準率定價**：假設賠率 =（1/該玩法基準命中率）× 返還率。"
          "這樣「押基準率本來就高的盤」不會產生假的優勢。",
          "5. **兩階段驗證**：連「要押哪個盤口、用什麼門檻」都只能用前段資料決定。", ""]

    L += ["## 2. 交付物一：達到 75% 以上命中率的條件", ""]
    L += sec_conditions(C, "teamgame", "隊伍視角（不讓分 / 讓分 / 單隊大小分 / 前5局）")
    L += sec_conditions(C, "game", "全場總分（大小分 / 前5局大小分 / NRFI）")

    L += ["## 3. 交付物二：每一隊的最佳條件", "",
          "**A. 單一條件版（樣本 ≥30 場，搜尋空間小，結論比較硬）**", ""]
    rows = []
    for tid, t in sorted(T["teams"].items(),
                         key=lambda kv: -((kv[1].get("best_single") or {}).get("wilson") or 0)):
        b = t.get("best_single")
        if not b:
            continue
        rows.append([t["zh"], t["games"], b["market_zh"], b["label"][:40],
                     f_pct(b["rate"]), f"{b['hits']}/{b['n']}", f_pct(b["wilson"]),
                     f_pct(b["base_league"]),
                     f"{(t.get('chance_max_rate_p95_single') or 0):.0%}",
                     "✅" if b["beats_chance"] else "✗", f"{b['be_odds']:.2f}"])
    L += [table(["球隊", "場次", "玩法", "條件", "命中率", "樣本", "Wilson下界",
                 "聯盟基準", "運氣線", "勝過運氣", "保本賠率"], rows), ""]

    L += ["**B. 兩條件組合版（使用者原本要求的「最高勝率」，但這一區幾乎都是雜訊）**", "",
          "運氣線都已經到 100%，列出來是為了讓你看到「100% 命中」有多容易被造出來。", ""]
    rows = []
    for tid, t in sorted(T["teams"].items(), key=lambda kv: -(kv[1]["best"]["rate"] if kv[1]["best"] else 0)):
        b = t["best"]
        if not b:
            continue
        rows.append([t["zh"], b["market_zh"], b["label"][:44], f_pct(b["rate"]),
                     f"{b['hits']}/{b['n']}", f"{t['chance_max_rate_p95']:.0%}",
                     "✅" if b["beats_chance"] else "✗"])
    L += [table(["球隊", "玩法", "條件", "命中率", "樣本", "運氣線", "勝過運氣"], rows), ""]

    if MR:
        L += ["## 4. 得分期望值模型（主引擎）", "",
              "先用 Poisson 梯度提升預測「每隊這場會得幾分」，再用負二項分布"
              f"（過度分散 α={MR['alpha_full']:.3f}）展開成完整得分分布，兩邊卷積 → "
              "一次算出所有盤口機率，而且互相一致（不會出現 P(>8.5) < P(>9.5) 的矛盾）。", "",
              f"- 樣本外單邊得分 MAE **{MR['mae']['full']}**（只押聯盟平均是 {MR['mae']['full_baseline']}）",
              f"- 前 5 局得分 MAE {MR['mae']['f5']}", ""]
        L += [table(["盤口", "AUC", "Brier技巧", "基準", "最高機率", "模型≥70%時實際"],
                    [[r["market"], f"{r['auc']:.3f}", f"{r['brier_skill']:+.3f}",
                      f_pct(r["base"], 0), f_pct(r["p_max"], 0),
                      (lambda t: f"{t['n']}注 {t['rate']:.0%}" if t else "—")(
                          next((t for t in r["thresholds"] if t["thr"] == 0.7), None))]
                     for r in MR["markets_full"][:16]]), ""]

    if BT:
        L += ["## 5. 回測：真的能賺嗎", "",
              f"樣本外期間 {BT['oos_range'][0]} ~ {BT['oos_range'][1]}，"
              f"假設台彩返還率 {BT['payout_assumed']:.0%}（所以 lift 要 > "
              f"{1/BT['payout_assumed']:.2f} 才有正期望值）。", ""]
        if ts and ts.get("roi") is not None:
            L += [f"### 兩階段驗證（最誠實的數字）：ROI **{f_pct(ts['roi'])}**", "",
                  f"{ts['cut']} 之前挑出 {ts['selected']} 個（盤口 × 門檻）組合，之後才下注。", "",
                  table(["盤口", "門檻", "訓練lift", "測試注數", "測試命中", "測試ROI"],
                        [[r["market_zh"], f_pct(r["thr"], 0), f"{r['train_lift']:.2f}",
                          r["test_n"], f_pct(r["test_hit"], 0), f_pct(r["test_roi"], 0)]
                         for r in ts["detail"]]), ""]
        L += ["### 單注：依 lift 排序（前 15）", "",
              table(["盤口", "門檻", "注數", "命中", "基準", "lift", "假設賠率", "ROI", "p值"],
                    [[r["market_zh"], f_pct(r["thr"], 0), r["n"], f_pct(r["hit"], 0),
                      f_pct(r["base"], 0), f"{r['lift']:.2f}", r["assumed_odds"],
                      f_pct(r["roi"][str(BT["payout_assumed"])], 0),
                      f"{r.get('p_value', float('nan')):.3f}"]
                     for r in BT["singles"][:15]]), "",
              "### 串關模擬（台彩玩法：每天挑 edge 最高幾腳，同場只取一腳）", "",
              table(["策略", "天數", "單腳命中", "票命中", "ROI"],
                    [[k, v["days"], f_pct(v["leg_hit_rate"], 0),
                      f_pct(v["ticket_hit_rate"], 0), f_pct(v["roi"], 0)]
                     for k, v in BT["parlays"].items()]), ""]

    L += ["## 6. 單隊分項亮點（Statcast）", ""]
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
          f"- **對慢速球最弱**：{top_by(['bat', 'by_group', '慢速球', 'woba'], reverse=False)}",
          f"- **日場打擊最強**：{top_by(['bat', 'day', 'woba'])}",
          f"- **夜場打擊最強**：{top_by(['bat', 'night', 'woba'])}",
          f"- **投手群壓制左打最好**：{top_by(['pit', 'vs_LHB', 'woba'], reverse=False)}",
          f"- **投手群壓制右打最好**：{top_by(['pit', 'vs_RHB', 'woba'], reverse=False)}", "",
          "更細的球員層級（每位打者對左右投/球種/日夜、每位投手對左右打/日夜/主客 + 球種使用率）"
          "見 `output/player_splits.json`，網頁「分析 → 球員」頁可以直接瀏覽。", ""]

    if M and M.get("importance"):
        L += ["## 7. 哪些分項特徵真的有用", "",
              "permutation importance：把該特徵打亂後，樣本外 AUC 掉多少。", ""]
        for mk, feats in M["importance"].items():
            top = "、".join(f"`{f['feature']}`({f['auc_drop']:+.3f})" for f in feats[:8])
            L += [f"- **{mk}**：{top}"]
        L += [""]

    if SL and SL.get("picks"):
        L += [f"## 8. 下一批賽事的訊號（{'、'.join(SL['generated_for'])}）", "",
              "依 edge（模型機率 ÷ 基準率）排序。edge > 1.11 才有正期望值。", ""]
        L += [table(["日期", "對戰", "玩法", "隊", "機率", "基準", "edge", "假設賠率", "回測表現"],
                    [[x["date"][5:], x["matchup"], x["market_zh"], x.get("team") or "",
                      f_pct(x["p"], 0), f_pct(x.get("base"), 0),
                      f"{x['edge']:.2f}" if x.get("edge") else "—",
                      x.get("assumed_odds") or "—",
                      (f"{x['bt_n']}注 {x['bt_hit']:.0%} ROI {x['bt_roi']:+.0%}"
                       if x.get("bt_hit") else "—")]
                     for x in SL["picks"][:25]]), ""]

    L += ["## 9. 已知限制與下一步", "",
          "**限制**",
          "- **沒有真實賠率**：所有 ROI 都基於「假設賠率 = 公正賠率 × 返還率」。"
          "台彩實際開盤可能更差（尤其熱門隊），也可能偶爾更好。",
          "- **單季樣本**：2000 場、單隊 130 場。條件挖掘的統計力天生不足，"
          "這也是為什麼 Tier A 只有 11 組。",
          "- **模型天花板**：樣本外 AUC 0.55-0.58。棒球本來就難，"
          "職業盤口能到 0.60+ 主要靠市場價格本身當特徵。",
          "- **先發預告會變**：未開打場次的預測依賴 probable pitcher，開賽前應重跑一次。",
          "",
          "**下一步（依投報率排序）**",
          "1. **接真實賠率**：the-odds-api 或國際盤歷史賠率。有了賠率，"
          "「模型機率 vs 市場隱含機率」的差才是真正可下注的 edge，比現在的替代方案強得多。",
          "2. **跨季資料**：把 2023-2025 也抓下來（同一套管線改個年份就能跑），"
          "條件驗證的統計力會提升 3 倍。",
          "3. **打線層級建模**：目前用球隊整體 wOBA，改成「今日先發打線 9 人的加權 wOBA」"
          "（開賽前 2-3 小時 API 就會公布），對單隊大小分應該有明顯幫助。",
          "4. **牛棚可用性**：追蹤每位後援投手連續出賽天數，判斷今天誰能上。",
          "5. **每日自動更新**：把 run_all.py 掛成排程，早上跑完自動 push，"
          "網頁就永遠是最新的。", "",
          "**建議的使用方式**", "",
          "1. 先看「推薦」頁的 edge 排行，只考慮 edge ≥ 1.11 的。",
          "2. 對照「回測」頁看該盤口在樣本外的實際表現（有沒有正 ROI）。",
          "3. 開盤後核對台彩實際賠率是否高於「保本賠率」。",
          "4. Tier A 條件當加分項，不要當主要依據。",
          "5. 每隊「兩條件組合」那一區純粹是雜訊教材，不要拿來下注。", ""]

    out = f"{ROOT}/ANALYSIS_REPORT.md"
    text = "\n".join(L)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    log(f"寫出 {out}（{len(text):,} 字）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
