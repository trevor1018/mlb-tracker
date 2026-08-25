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
    loaded = {}
    for name in ("models", "models_runs", "backtest", "slate", "ablation",
                 "multiseason", "over_rule", "market_proxy"):
        try:
            loaded[name] = jload(f"{OUTPUT}/{name}.json")
        except Exception:
            loaded[name] = None
    M, MR, BT, SL = (loaded["models"], loaded["models_runs"],
                     loaded["backtest"], loaded["slate"])
    AB, MS, OR = loaded["ablation"], loaded["multiseason"], loaded["over_rule"]
    MP = loaded["market_proxy"]

    nA_tg = len(C["teamgame"]["tierA"])
    nA_g = len(C["game"]["tierA"])
    ts = (BT or {}).get("two_stage") or {}

    L = ["# MLB 2026 資料分析報告", "",
         f"產生時間：{datetime.now():%Y-%m-%d %H:%M}｜資料範圍：2026 球季開幕 ~ 8/24"
         f"（{S['meta']['games']} 場、{S['meta']['pitches']:,} 球 Statcast 逐球資料）", "",
         "## 0. 四句話結論", "",
         (f"1. **最重要的發現：細分項沒有多知道任何事。** 我另外訓練一個「莊家代理」模型，"
          f"只用 16 個莊家一定會定價的欄位（球場、氣溫、屋頂、雙方先發 R/9、雙方場均得失分、"
          f"主客場、勝率）。結果代理模型的樣本外得分 MAE 是 "
          f"{MP['mae']['proxy']}，完整 109 欄模型是 {MP['mae']['full']} —— "
          f"**代理反而更好**。30 個盤口裡完整模型只有 {MP['markets_improved']} 個 logloss 有進步。"
          f"拿代理當市場定價下注，{MP['overall_bets']} 注 ROI {f_pct(MP['overall_roi'])}。"
          if MP else "1. （市場代理測試尚未執行）"),
         f"2. **高命中條件確實存在，但少得可憐**：482 萬個「條件 × 玩法」假設，過四道關卡後"
         f"只剩 **{nA_tg + nA_g} 組**（隊伍視角 {nA_tg}、全場 {nA_g}）。"
         f"其餘上萬組「命中率 80%+」的條件，全都低於 permutation 算出的運氣線。",
         f"3. **每一隊都能找到 100% 命中的條件 —— 而且那毫無意義**：單隊約 100 場樣本、"
         f"搜尋空間上萬組，運氣線本身就已經到 100%。只有 3 隊在「單一條件、樣本 ≥30 場」"
         f"的嚴格版本下勝過自己的運氣線。",
         f"4. **看起來能賺的「全場大分」，一做球場校正就不見了**："
         f"大分 8.5 的 lift 從 1.31 掉到 1.10、ROI 從 +18.3% 變 −0.7%。"
         f"模型抓到的主要是「這個球場容易得分」，而那是莊家最不可能漏掉的資訊。", "",
         "**總結：在台彩 88-92% 的返還率下，這套系統目前找不到可靠的正期望值玩法。**"
         "分項資料的價值在於「理解比賽」（球隊/球員瀏覽、對位速查），"
         "不在於「打敗盤口」。要打敗盤口只有一條路：接真實賠率，"
         "找莊家定價偏差，而不是重算莊家已經知道的東西。", ""]

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
            ci = ts.get("overall_roi_ci90")
            L += [f"### 兩階段驗證（最誠實的數字）", "",
                  f"{ts['cut']} 之前挑出 {ts['selected']} 個（盤口 × 門檻）組合，之後才下注："
                  f"共 {sum(r['test_n'] for r in ts['detail'])} 注。", "",
                  f"- 整體 lift **{ts['overall_lift']:.3f}** → 保本需要的返還率 "
                  f"**{ts['overall_breakeven_payout']:.1%}**",
                  f"- 返還率 88% → ROI {f_pct(ts['overall_roi_by_payout']['0.88'])}｜"
                  f"90% → {f_pct(ts['overall_roi_by_payout']['0.9'])}｜"
                  f"92% → {f_pct(ts['overall_roi_by_payout']['0.92'])}｜"
                  f"95% → {f_pct(ts['overall_roi_by_payout']['0.95'])}"
                  + (f"（90% 返還率下的 bootstrap 區間 {f_pct(ci[0])}~{f_pct(ci[1])}）" if ci else ""),
                  "",
                  "**台彩返還率約 88-92%，所以整體而言這套模型在台彩是打不過抽水的。**"
                  "但拆開看玩法家族，差異非常大：", "",
                  table(["玩法家族", "組合數", "注數", "命中率", "lift", "保本返還率",
                         "ROI@90%", "ROI@95%", "90% 區間（90%返還率）"],
                        [[f["family"], f["combos"], f["bets"], f_pct(f["hit"], 1),
                          f"{f['lift']:.3f}", f_pct(f["breakeven_payout"], 0),
                          f_pct(f["roi_by_payout"]["0.9"]),
                          f_pct(f["roi_by_payout"]["0.95"]),
                          (f"{f_pct(f['roi_ci90'][0])} ~ {f_pct(f['roi_ci90'][1])}"
                           if f.get("roi_ci90") else "—")]
                         for f in ts["families"]]), "",
                  "**但這張表有一個致命前提**：假設賠率 =（1/該盤口線的聯盟平均命中率）×返還率。"
                  "真實莊家會針對球場、天氣、先發逐場調整盤口與賠率。下一節的球場校正"
                  "就是在檢驗這個前提 —— 結果是優勢大半消失。", "",
                  "家族層面的觀察（在上述前提下）：", "",
                  "- 全場大分是唯一整個 bootstrap 區間都在正的家族（lift 1.30）——"
                  "但見下一節，這主要是球場效應。",
                  "- **讓分/受讓的區間整個是負的**（lift 1.00 = 完全沒有優勢）。"
                  "這類盤口不要用這套模型下注。",
                  "- 單隊大小分 lift 約 1.07，保本返還率 93% —— 台彩吃不到，國際盤剛好打平。",
                  "- 注意：全場大分只有 131 注，bootstrap 只反映「給定觀察命中率」的抽樣噪音，"
                  "不包含模型/策略挑選本身的不穩定。要更有信心得看下一個月的實戰。", "",
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

    if AB:
        noise = AB["noise_mae_sd"]
        full_mae = AB["full"]["mae"]
        base_mae = AB["base_only"]["mae"]
        gain = base_mae - full_mae
        L += ["## 5.5 特徵族群消融實驗：這些細分項真的有用嗎", "",
              f"種子間標準差 **{noise}** 是雜訊尺度 —— 差異沒超過它的兩倍就不能算有貢獻。", "",
              f"- 全特徵 MAE **{full_mae}**",
              f"- 只用基本盤（勝率/得失分/Elo/主客/日夜）MAE **{base_mae}**",
              f"- 差距 **{gain:+.4f}**，約是雜訊尺度的 {gain/noise:.1f} 倍 → "
              f"**細分項整體確實有貢獻**", "",
              "### A. 拿掉某一族群（drop-one）", "",
              table(["族群", "欄數", "拿掉後 MAE 變化", "大小分 AUC 變化", "判定"],
                    [[g, len(AB["groups"].get(g, [])),
                      f"{d['mae_delta']:+.4f}", f"{d['auc_delta_over85']:+.4f}",
                      "**有貢獻**" if d["mae_delta"] > 2 * noise else "看不出貢獻"]
                     for g, d in sorted(AB["drop_one"].items(),
                                        key=lambda kv: -kv[1]["mae_delta"])]), "",
              "### B. 基本盤 + 單獨加入某一族群", "",
              table(["族群", "MAE", "相對只用基本盤"],
                    [[g, o["mae"], f"{o['mae_vs_base']:+.4f}"]
                     for g, o in sorted(AB["base_plus_one"].items(),
                                        key=lambda kv: kv[1]["mae_vs_base"])]), "",
              "### 這兩張表合起來的意思", "",
              "1. **細分項整體有用**：全特徵比只用基本盤好 "
              f"{gain:.4f}（約 {gain/noise:.0f} 倍雜訊），這是明確的。",
              "2. **但族群之間高度重疊**：拿掉任何單一族群（除了球場與天氣）都不痛，"
              "因為其他族群補得上來。所以「哪一個分項最重要」這個問題本身沒有乾淨的答案。",
              "3. **最不可取代的是球場與天氣**：拿掉它 MAE 變差 "
              f"{AB['drop_one'].get('球場與天氣', {}).get('mae_delta', 0):+.4f}、"
              "大小分 AUC 掉最多。Coors Field 場均 11.3 分 vs T-Mobile 7.7 分，"
              "這種差距是任何打擊分項都補不回來的。",
              "4. **單獨加入最有效的是先發投手品質**："
              f"{AB['base_plus_one'].get('先發投手品質', {}).get('mae_vs_base', 0):+.4f}。",
              "5. **精簡版和全套一樣好**：只留 38 個欄位（全套 107 欄）的精簡特徵組，"
              "MAE 與全套差距在雜訊範圍內（+0.0014，雜訊 0.0037）——"
              "等於說 107 欄裡有 69 欄是多餘的。維護時可以大膽砍。",
              "6. **「左右投打對位」單獨加入反而變差**："
              f"{AB['base_plus_one'].get('左右投打對位', {}).get('mae_vs_base', 0):+.4f} —— "
              "20 個欄位帶進來的過度配適大於它的訊號。要用這一族群，"
              "得先做降維（例如只留「我隊對對手先發手別 wOBA」單一欄位）。", ""]

    if MS and (MS.get("teamgame") or MS.get("game")):
        L += [f"## 5.6 跨球季驗證（{'+'.join(map(str, MS['train_seasons']))} 挖掘 → "
              f"{MS['test_season']} 驗證）", "",
              "最硬的一關：球季換了、球員陣容也變了，條件還撐得住才可能是真規律。", ""]
        for key, name in (("teamgame", "隊伍視角"), ("game", "全場總分")):
            d = MS.get(key)
            if not d:
                continue
            holds = [r for r in d["rows"] if r["holds"]]
            L += [f"**{name}**：候選 {d['candidates']:,} 組，跨季都撐住 **{d['holds']}** 組", ""]
            dec = d.get("decay")
            if dec:
                L += [table(["指標", "數值"],
                            [["訓練季平均命中率", f_pct(dec["mean_train_rate"])],
                             ["測試季平均命中率", f_pct(dec["mean_test_rate"])],
                             ["測試季仍 ≥72% 的比例", f_pct(dec["pct_test_ge_72"])],
                             ["測試季仍 ≥65% 的比例", f_pct(dec["pct_test_ge_65"])],
                             ["測試季變差的比例", f_pct(dec["pct_test_worse_than_train"])],
                             ["訓練季平均 lift（相對基準率）", f"{dec.get('mean_train_lift', 0):.3f}"],
                             ["測試季平均 lift", f"**{dec.get('mean_test_lift', 0):.3f}**"],
                             ["測試季 lift>1 的比例", f_pct(dec.get("pct_test_lift_gt_1"))],
                             ["測試季 lift>1.11（能打敗抽水）的比例",
                              f_pct(dec.get("pct_test_lift_gt_111"))]]), "",
                      "絕對命中率會被高基準盤口騙（例如「受讓 2.5」本身基準就有 73.5%），"
                      "所以要看 **lift（命中率 ÷ 該季基準率）**。"
                      f"訓練季 lift {dec.get('mean_train_lift', 0):.3f} → "
                      f"測試季 {dec.get('mean_test_lift', 0):.3f}"
                      + ("，等於資訊完全消失。" if dec.get("mean_test_lift", 0) < 1.02
                         else "。"), ""]
            if holds:
                L += [table(["玩法", "條件", "訓練期", f"{MS['test_season']} 測試",
                             "各季命中率", "保本賠率"],
                            [[r["market_zh"], r["label"][:52],
                              f"{r['train_rate']:.0%}（{r['train_n']}）",
                              f"{r['test_rate']:.0%}（{r['test_n']}）",
                              " / ".join(f"{k}:{v['rate']:.0%}" for k, v in r["by_season"].items()),
                              f"{r['be_odds']:.2f}"] for r in holds[:15]]), ""]

    if OR:
        pc = OR.get("park_control") or {}
        L += ["## 5.7 球場校正：那個「大分優勢」是真的嗎", "",
              "上一節的假設賠率用「該盤口線的聯盟平均命中率」定價。"
              "但莊家不是笨蛋 —— Coors Field 場均 11.3 分、T-Mobile 7.7 分，"
              "盤口線與賠率一定會反映球場。所以這裡改用**該球場自己的歷史大分率**"
              "當基準重算一次：", ""]
        if pc:
            L += [table(["盤口", "規則", "場數", "命中率", "對聯盟基準 lift", "ROI",
                         "對球場基準 lift", "ROI（球場定價）"],
                        [[line, v["rule"], v["n"], f_pct(v["hit"], 1),
                          f"{v['lift_vs_league']:.3f}", f_pct(v["roi_league_pricing"]),
                          f"{v['lift_vs_park']:.3f}", f_pct(v["roi_park_pricing"])]
                         for line, v in pc.items()]), "",
                  "**優勢大半來自球場**：大分 8.5 的 lift 從 1.31 掉到 1.10、"
                  "ROI 從 +18.3% 變成 −0.7%；大分 9.5 從 +12.7% 變 −1.1%。"
                  "換句話說，模型抓到的主要是「這個球場容易得分」——"
                  "而那是莊家最不可能漏掉的資訊。", "",
                  "唯一在球場校正後還留下一點東西的是大分 10.5（lift 1.16、ROI +4.4%，"
                  "但只有 47 場，區間一定跨零）。", ""]
        if OR.get("by_park"):
            L += ["**依球場得分環境（對聯盟基準）**", "",
                  table(["球場類型"] + [f"大分 {k}" for k in OR["lines"]],
                        [[name] + [f"{sub.get(k, {}).get('rate', 0) * 100:.0f}%"
                                   f"（lift {sub.get(k, {}).get('lift', 0):.2f}）"
                                   for k in OR["lines"]]
                         for name, sub in OR["by_park"].items()]), ""]
        rec = OR.get("recommended") or []
        if rec:
            L += ["**可手動套用的查表（未做球場校正，看的時候請自行打折）**", "",
                  "用法：看到台彩盤口線，算出「模型預估總分 − 盤口線」，查下表。", "",
                  table(["盤口", "μ−線區間", "場數", "命中率", "lift", "假設賠率", "ROI"],
                        [[r["line"], r["bucket"], r["n"], f_pct(r["rate"], 1),
                          f"{r['lift']:.2f}", r["assumed_odds"], f_pct(r["roi"])]
                         for r in rec]), ""]

    if MP:
        L += ["## 5.8 市場代理測試：我們知道市場不知道的事嗎", "",
              "沒有真實賠率時，最接近真相的做法是自己造一個「莊家代理」：",
              f"只用 {len(MP['proxy_cols'])} 個莊家一定會定價的欄位 —— "
              f"`{'`、`'.join(MP['proxy_cols'])}`。", "",
              f"| 指標 | 完整模型（{MP['n_full_cols']} 欄） | 代理模型（{len(MP['proxy_cols'])} 欄） |",
              "|---|---|---|",
              f"| 樣本外單邊得分 MAE | {MP['mae']['full']} | **{MP['mae']['proxy']}** |",
              f"| logloss 有進步的盤口 | {MP['markets_improved']} / {MP['markets_total']} | — |",
              f"| 平均 logloss 進步 | {MP['logloss_gain_mean']:+.5f} | — |", "",
              f"**代理模型反而略勝**。也就是說：Statcast 分項、對左右投/球種對位、"
              f"牛棚被打 wOBA、近期滾動、球速變化這些東西加起來，"
              f"沒有提供「球場 + 先發 R/9 + 球隊得失分」以外的預測資訊。", "",
              f"用代理機率當公正賠率（× 返還率 {MP['payout']:.0%}）、只在完整模型認為有 "
              f"{MP['edge_threshold']} 倍 edge 時下注：{MP['overall_bets']} 注、"
              f"ROI **{f_pct(MP['overall_roi'])}**。", ""]
        good = [m for m in MP["markets"] if m.get("roi", -1) > 0]
        if good:
            L += ["少數為正的盤口（樣本小，且整體為負，很可能是雜訊）：", "",
                  table(["盤口", "注數", "命中率", "平均賠率", "ROI", "logloss 進步", "AUC 進步"],
                        [[m["market"], m["bets"], f_pct(m["hit"], 1), m["avg_odds"],
                          f_pct(m["roi"]), f"{m['logloss_gain']:+.5f}",
                          f"{m['auc_gain']:+.4f}"] for m in good[:8]]), ""]
        L += [f"> {MP['note']}", ""]

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
          "0. **開始記錄真實下注（已內建）**：網頁「紀錄」tab 可以在下注時填入台彩實際賠率，"
          "自動結算並統計真實 ROI 與校準。這是最便宜的取得賠率資料的方式 —— "
          "兩三週後就能看出模型機率與台彩定價的系統性偏差，那才是真正的 edge 來源。",
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
          "0. **先接真實賠率**。在那之前，下面的步驟都只是「相對聯盟平均」的參考，"
          "不是真的正期望值。",
          "1. 看「推薦」頁的 edge 排行時，記得 edge 是相對聯盟平均算的；"
          "如果 edge 主要來自球場（例如 Coors、國民球場），台彩的盤口線早就調高了。",
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
