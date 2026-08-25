"""單場對位分析：這支打線碰上這位先發會怎樣 → output/matchups.json

為什麼不直接用「打線 vs 這位投手」的對戰史？
實測三季資料：一支打線對一位先發平均只有 2-38 個打席，個別打者中位數 2 PA。
9 PA 打出 .456 完全是雜訊。所以直接對戰史只附註（並標明樣本），不當主要依據。

真正有統計基礎的做法：把打線拆開，逐一算每位打者對「這位投手的慣用手」與
「他最常投的球種」的表現（每位打者這兩項各有數百 PA），再依近期打席數加權
還原成打線層級。這樣既有樣本量，又能明確指出是哪幾位打者對他有利或不利。

輸出每場每一邊：
  投手：姓名、左右手、球種使用率、對左右打被打 wOBA、揮空率、R/9
  打線：加權對位分數（相對聯盟）、對該手別 wOBA、對其主球種 wOBA
  逐打者：最有利 / 最不利各 2-3 位（含姓名、PA、wOBA）
  直接對戰史：合計 PA 與 wOBA（樣本不足會標記）
  一段中文敘述
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

from build_splits import PITCH_GROUP
from common import DATA, OUTPUT, ROOT, TEAM_ZH, jdump, jload, log

GROUP_ZH = {"fastball": "速球", "breaking": "變化球", "offspeed": "慢速球",
            "cutter": "卡特", "other": "其他"}
HAND_ZH = {"L": "左投", "R": "右投"}
PRIOR_PA = 80          # 打者分項收縮用的先驗權重
LINEUP_N = 9           # 取近期打席最多的前 9 位當預設打線
RECENT_DAYS = 30


def load_pitches(seasons):
    cols = ["game_pk", "game_date", "batter", "pitcher", "stand", "p_throws",
            "pitch_type", "woba_value", "woba_denom", "description", "events"]
    frames = []
    for s in seasons:
        f = os.path.join(ROOT, "data", str(s), "pitches.parquet")
        if not os.path.exists(f):
            continue
        d = pd.read_parquet(f, columns=cols)
        d["season"] = s
        frames.append(d)
    p = pd.concat(frames, ignore_index=True)
    p["pgroup"] = p["pitch_type"].map(PITCH_GROUP).fillna("other")
    return p


def agg_woba(df, keys):
    g = df.groupby(keys, dropna=False).agg(
        pa=("woba_denom", "sum"), wsum=("woba_value", "sum")).reset_index()
    g = g[g["pa"] > 0]
    g["woba"] = g["wsum"] / g["pa"]
    return g


def shrunk(w, pa, league, prior=PRIOR_PA):
    return (w * pa + league * prior) / (pa + prior)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2025,2026",
                    help="計算打者分項用的球季（近兩季兼顧樣本量與時效）")
    ap.add_argument("--h2h-seasons", default="auto",
                    help="直接對戰史用的球季；auto = 自動用 data/ 底下所有有資料的球季")
    ap.add_argument("--w-h2h", type=float, default=0.5,
                    help="直接對戰史在對位分數裡的權重（使用者指定拉高）")
    ap.add_argument("--w-hand", type=float, default=0.3)
    ap.add_argument("--w-group", type=float, default=0.2)
    ap.add_argument("--h2h-prior", type=float, default=10.0,
                    help="對戰史收縮的先驗 PA（越小越相信小樣本；設 0 = 完全不收縮）")
    args = ap.parse_args()

    seasons = [int(x) for x in args.seasons.split(",")]
    log(f"讀取逐球資料（打者分項用 {seasons}）")
    p = load_pitches(seasons)
    log(f"  {len(p):,} 球")

    # ── 聯盟基準 ──
    lg_hand = agg_woba(p, ["p_throws"]).set_index("p_throws")["woba"].to_dict()
    lg_group = agg_woba(p, ["pgroup"]).set_index("pgroup")["woba"].to_dict()
    lg_all = float(p["woba_value"].sum() / p["woba_denom"].sum())
    log(f"  聯盟 wOBA {lg_all:.3f}｜對左投 {lg_hand.get('L', 0):.3f}、"
        f"對右投 {lg_hand.get('R', 0):.3f}")

    # ── 打者分項 ──
    bat_hand = agg_woba(p, ["batter", "p_throws"])
    bat_group = agg_woba(p, ["batter", "pgroup"])
    bh = {(int(r.batter), r.p_throws): (float(r.pa), float(r.woba))
          for r in bat_hand.itertuples()}
    bg = {(int(r.batter), r.pgroup): (float(r.pa), float(r.woba))
          for r in bat_group.itertuples()}

    # ── 投手：球種使用率、對左右打被打 wOBA、揮空率 ──
    cur = p[p["season"] == max(seasons)]
    usage = (cur.groupby(["pitcher", "pgroup"]).size().rename("n").reset_index())
    tot = usage.groupby("pitcher")["n"].sum().rename("tot").reset_index()
    usage = usage.merge(tot, on="pitcher")
    usage["pct"] = 100 * usage["n"] / usage["tot"]
    pit_use = {}
    for pid, g in usage.groupby("pitcher"):
        pit_use[int(pid)] = {GROUP_ZH.get(r.pgroup, r.pgroup): round(float(r.pct), 1)
                             for r in g.sort_values("pct", ascending=False).itertuples()}
    pit_main = {}
    for pid, g in usage.groupby("pitcher"):
        s = g.sort_values("pct", ascending=False)
        rows = [(r.pgroup, float(r.pct)) for r in s.itertuples()]
        pit_main[int(pid)] = rows

    pit_hand_split = agg_woba(cur, ["pitcher", "stand"])
    ph = {(int(r.pitcher), r.stand): (float(r.pa), float(r.woba))
          for r in pit_hand_split.itertuples()}

    # ── 直接對戰史（能抓多少年就用多少年）──
    if args.h2h_seasons == "auto":
        h2h_seasons = sorted(int(d) for d in os.listdir(os.path.join(ROOT, "data"))
                             if d.isdigit()
                             and os.path.exists(os.path.join(ROOT, "data", d, "pitches.parquet")))
    else:
        h2h_seasons = [int(x) for x in args.h2h_seasons.split(",")]
    log(f"直接對戰史使用球季：{h2h_seasons}")
    ph2 = load_pitches(h2h_seasons) if set(h2h_seasons) - set(seasons) else p
    log(f"  對戰史資料 {len(ph2):,} 球")
    h2h_full = (ph2.groupby(["batter", "pitcher"])
                .agg(pa=("woba_denom", "sum"), wsum=("woba_value", "sum"),
                     pitches=("pitch_type", "size"),
                     k=("events", lambda s: int(s.isin(["strikeout",
                                                        "strikeout_double_play"]).sum())),
                     hr=("events", lambda s: int((s == "home_run").sum())),
                     hit=("events", lambda s: int(s.isin(["single", "double",
                                                          "triple", "home_run"]).sum())),
                     last=("game_date", "max"))
                .reset_index())
    h2h_full = h2h_full[h2h_full["pa"] > 0]
    h2h_full["woba"] = h2h_full["wsum"] / h2h_full["pa"]
    h2h_map = {(int(r.batter), int(r.pitcher)): (float(r.pa), float(r.woba))
               for r in h2h_full.itertuples()}
    h2h_detail = {(int(r.batter), int(r.pitcher)): {
        "pa": float(r.pa), "woba": float(r.woba), "k": int(r.k), "hr": int(r.hr),
        "hit": int(r.hit), "last": str(r.last)} for r in h2h_full.itertuples()}
    log(f"  打者-投手配對 {len(h2h_map):,} 組")

    # ── 近期打線（近 30 天打席最多的球員）──
    cur2 = p[p["season"] == max(seasons)].copy()
    last_date = cur2["game_date"].max()
    cutoff = (pd.Timestamp(last_date) - pd.Timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    recent = cur2[cur2["game_date"] >= cutoff]
    games = jload(f"{DATA}/games.json")
    # 打者所屬球隊：用該季最後出賽的球隊（由 people.json 的 currentTeam 補強）
    people = jload(f"{DATA}/people.json")
    bat_team = {}
    for b, g in recent.groupby("batter"):
        info = people.get(str(int(b))) or {}
        t = info.get("team")
        if t and int(t) in TEAM_ZH:      # 只留 30 支大聯盟球隊（避開小聯盟 id）
            bat_team[int(b)] = int(t)
    recent_pa = (recent.groupby("batter")["woba_denom"].sum().rename("pa").reset_index())
    recent_pa["team"] = recent_pa["batter"].map(bat_team)
    recent_pa = recent_pa.dropna(subset=["team"])
    lineup_by_team = {}
    for tid, g in recent_pa.groupby("team"):
        top = g.sort_values("pa", ascending=False).head(LINEUP_N)
        lineup_by_team[int(tid)] = [(int(r.batter), float(r.pa)) for r in top.itertuples()]
    log(f"  近 {RECENT_DAYS} 天打線（{cutoff} 之後）：{len(lineup_by_team)} 隊")

    def bat_split(bid, hand, group):
        """回傳 (對該手別 wOBA, PA, 對該球種 wOBA, PA)（收縮後）"""
        pa_h, w_h = bh.get((bid, hand), (0.0, lg_hand.get(hand, lg_all)))
        pa_g, w_g = bg.get((bid, group), (0.0, lg_group.get(group, lg_all)))
        return (shrunk(w_h, pa_h, lg_hand.get(hand, lg_all)), pa_h,
                shrunk(w_g, pa_g, lg_group.get(group, lg_all)), pa_g)

    # ── 逐場產生對位分析 ──
    slate = jload(f"{OUTPUT}/slate.json")
    name2id = {v["name"]: int(k) for k, v in people.items() if v.get("name")}
    id2name = {int(k): v.get("name") for k, v in people.items()}
    zh2id = {}
    try:
        splits = jload(f"{OUTPUT}/team_splits.json")["teams"]
        zh2id = {v["zh"]: int(k) for k, v in splits.items()}
    except Exception:
        pass

    out = {}
    for g in slate["games"]:
        md = g.get("matchup_detail") or {}
        entry = {}
        for side, other in (("home", "away"), ("away", "home")):
            sp = md.get(side) or {}
            spn = sp.get("sp_name")
            bat_zh = g[other]                      # 面對這位先發的是對手打線
            tid = zh2id.get(bat_zh)
            if not spn or tid is None or tid not in lineup_by_team:
                continue
            spid = name2id.get(spn)
            hand = sp.get("sp_hand") or "R"
            arsenal = pit_main.get(spid, [])
            main_group = arsenal[0][0] if arsenal else "fastball"
            # 主要「非速球」球種（對打者來說這才是關鍵武器）
            sec = next((k for k, v in arsenal if k not in ("fastball", "cutter")), None)
            key_group = sec or main_group

            rows, tot_pa = [], 0.0
            for bid, rpa in lineup_by_team[tid]:
                wh, pah, wg, pag = bat_split(bid, hand, key_group)
                det = h2h_detail.get((bid, spid))
                h2h_pa = det["pa"] if det else 0.0
                h2h_raw = det["woba"] if det else None
                # 對戰史收縮（prior 可調；使用者要求相信小樣本 → 預設只給 10 PA 先驗）
                def _sh(prior):
                    if not det:
                        return lg_all
                    if prior <= 0:
                        return h2h_raw
                    return (h2h_raw * h2h_pa + lg_all * prior) / (h2h_pa + prior)
                h2h_variants = {"raw": _sh(0), "light": _sh(10), "safe": _sh(40)}
                h2h_sh = h2h_variants["light" if args.h2h_prior == 10 else
                                      "raw" if args.h2h_prior <= 0 else "safe"]
                d_h2h = h2h_sh - lg_all
                d_hand = wh - lg_hand.get(hand, lg_all)
                d_group = wg - lg_group.get(key_group, lg_all)
                delta = (args.w_h2h * d_h2h + args.w_hand * d_hand
                         + args.w_group * d_group)
                rows.append({
                    "id": bid, "name": id2name.get(bid, str(bid)),
                    "recent_pa": round(rpa, 1),
                    "vs_hand": round(wh, 3), "vs_hand_pa": round(pah),
                    "vs_group": round(wg, 3), "vs_group_pa": round(pag),
                    "delta": round(delta, 4),
                    "d_h2h": round(d_h2h, 4), "d_hand": round(d_hand, 4),
                    "d_h2h_raw": round(h2h_variants["raw"] - lg_all, 4),
                    "d_h2h_light": round(h2h_variants["light"] - lg_all, 4),
                    "d_h2h_safe": round(h2h_variants["safe"] - lg_all, 4),
                    "d_group": round(d_group, 4),
                    "h2h_pa": round(h2h_pa), "h2h_woba": (round(h2h_raw, 3) if det else None),
                    "h2h_woba_sh": round(h2h_sh, 3) if det else None,
                    "h2h_k": det["k"] if det else 0,
                    "h2h_hr": det["hr"] if det else 0,
                    "h2h_hit": det["hit"] if det else 0,
                    "h2h_last": det["last"] if det else None,
                })
                tot_pa += rpa
            lineup_delta = (sum(r["delta"] * r["recent_pa"] for r in rows) / tot_pa
                            if tot_pa else 0.0)
            def _variant(key):
                if not tot_pa:
                    return 0.0
                return sum((args.w_h2h * r[key] + args.w_hand * r["d_hand"]
                            + args.w_group * r["d_group"]) * r["recent_pa"]
                           for r in rows) / tot_pa
            delta_variants = {"raw": round(_variant("d_h2h_raw"), 4),
                              "light": round(_variant("d_h2h_light"), 4),
                              "safe": round(_variant("d_h2h_safe"), 4),
                              "no_h2h": round(sum((r["d_hand"] * 0.6 + r["d_group"] * 0.4)
                                                  * r["recent_pa"] for r in rows) / tot_pa, 4)}
            lineup_hand = (sum(r["vs_hand"] * r["recent_pa"] for r in rows) / tot_pa
                           if tot_pa else lg_all)
            lineup_group = (sum(r["vs_group"] * r["recent_pa"] for r in rows) / tot_pa
                            if tot_pa else lg_all)
            best = sorted(rows, key=lambda r: -r["delta"])[:3]
            worst = sorted(rows, key=lambda r: r["delta"])[:2]
            h2h_tot = sum(r["h2h_pa"] for r in rows)
            h2h_woba = (sum((r["h2h_woba"] or 0) * r["h2h_pa"] for r in rows) / h2h_tot
                        if h2h_tot else None)
            h2h_faced = [r for r in rows if r["h2h_pa"] > 0]
            h2h_k = sum(r["h2h_k"] for r in rows)
            h2h_hr = sum(r["h2h_hr"] for r in rows)
            h2h_hit = sum(r["h2h_hit"] for r in rows)
            h2h_best = sorted([r for r in h2h_faced if r["h2h_pa"] >= 2],
                              key=lambda r: -(r["h2h_woba"] or 0))[:3]
            h2h_worst = sorted([r for r in h2h_faced if r["h2h_pa"] >= 2],
                               key=lambda r: (r["h2h_woba"] or 0))[:3]
            h2h_delta = ((h2h_woba - lg_all) if h2h_woba is not None else None)

            pa_l, w_l = ph.get((spid, "L"), (0, None))
            pa_r, w_r = ph.get((spid, "R"), (0, None))
            use = pit_use.get(spid, {})
            use_txt = "、".join(f"{k} {v:.0f}%" for k, v in list(use.items())[:3])

            verdict = ("對位明顯有利" if lineup_delta >= 0.015 else
                       "對位偏有利" if lineup_delta >= 0.006 else
                       "對位明顯不利" if lineup_delta <= -0.015 else
                       "對位偏不利" if lineup_delta <= -0.006 else "對位中性")
            narrative = (
                f"{bat_zh}打線對上 {spn}（{HAND_ZH.get(hand, hand)}"
                + (f"、{use_txt}" if use_txt else "") + f"）：{verdict}"
                f"（打線加權 {lineup_delta:+.3f}）。"
            )
            # 對戰史擺第一（使用者指定為首要指標）
            if h2h_tot > 0:
                narrative += (
                    f"【直接對戰史】這條打線有 {len(h2h_faced)} 人對過他，"
                    f"合計 {h2h_tot:.0f} 打席、wOBA {h2h_woba:.3f}"
                    f"（聯盟 {lg_all:.3f}，{'吃得下' if (h2h_delta or 0) > 0.02 else '被壓制' if (h2h_delta or 0) < -0.02 else '互有來回'}）"
                    f"，{h2h_hit} 支安打、{h2h_hr} 轟、被三振 {h2h_k} 次。"
                )
                if h2h_best:
                    narrative += "打得最好的是 " + "、".join(
                        f"{b['name']}（{b['h2h_pa']:.0f} PA、wOBA {b['h2h_woba']:.3f}"
                        + (f"、{b['h2h_hr']} 轟" if b["h2h_hr"] else "") + "）"
                        for b in h2h_best[:3]) + "；"
                if h2h_worst:
                    narrative += "被吃最死的是 " + "、".join(
                        f"{w['name']}（{w['h2h_pa']:.0f} PA、wOBA {w['h2h_woba']:.3f}"
                        + (f"、{w['h2h_k']} K" if w["h2h_k"] else "") + "）"
                        for w in h2h_worst[:2]) + "。"
            else:
                narrative += "【直接對戰史】這條打線沒有人對過他。"
            narrative += (
                f"【類型對位】對{HAND_ZH.get(hand, hand)} wOBA {lineup_hand:.3f}"
                f"（聯盟 {lg_hand.get(hand, lg_all):.3f}）、"
                f"對他最常用的{GROUP_ZH.get(key_group, key_group)} {lineup_group:.3f}"
                f"（聯盟 {lg_group.get(key_group, lg_all):.3f}）。"
            )
            if best:
                narrative += "類型上最吃得下的是 " + "、".join(
                    f"{b['name']}（對{HAND_ZH.get(hand, hand)} {b['vs_hand']:.3f}／"
                    f"對{GROUP_ZH.get(key_group, key_group)} {b['vs_group']:.3f}）"
                    for b in best[:2]) + "。"
            if w_l is not None and w_r is not None:
                narrative += (f"【投手本季】對左打被打 {w_l:.3f}（{pa_l:.0f} PA）、"
                              f"對右打 {w_r:.3f}（{pa_r:.0f} PA）")
                if sp.get("sp_r9") is not None:
                    narrative += f"，R/9 {sp['sp_r9']}"
                narrative += "。"

            entry[side] = {
                "sp_name": spn, "sp_hand": hand, "sp_usage": use,
                "sp_key_group": GROUP_ZH.get(key_group, key_group),
                "sp_vs_L": (round(w_l, 3) if w_l is not None else None),
                "sp_vs_L_pa": round(pa_l),
                "sp_vs_R": (round(w_r, 3) if w_r is not None else None),
                "sp_vs_R_pa": round(pa_r),
                "bat_team": bat_zh,
                "lineup_delta": round(lineup_delta, 4),
                "delta_variants": delta_variants,
                "lineup_vs_hand": round(lineup_hand, 3),
                "lineup_vs_group": round(lineup_group, 3),
                "league_hand": round(lg_hand.get(hand, lg_all), 3),
                "league_group": round(lg_group.get(key_group, lg_all), 3),
                "verdict": verdict,
                "best": best, "worst": worst,
                "h2h_pa": round(h2h_tot),
                "h2h_woba": (round(h2h_woba, 3) if h2h_woba else None),
                "h2h_delta": (round(h2h_delta, 4) if h2h_delta is not None else None),
                "h2h_faced": len(h2h_faced), "h2h_k": h2h_k, "h2h_hr": h2h_hr,
                "h2h_hit": h2h_hit,
                "h2h_best": h2h_best, "h2h_worst": h2h_worst,
                "h2h_rows": sorted(h2h_faced, key=lambda r: -r["h2h_pa"]),
                "narrative": narrative,
                "lineup": rows,
            }
        if entry:
            out[str(g["pk"])] = entry

    meta = {"seasons": seasons, "h2h_seasons": h2h_seasons,
            "weights": {"h2h": args.w_h2h, "hand": args.w_hand, "group": args.w_group},
            "h2h_prior_pa": args.h2h_prior,
            "league_woba": round(lg_all, 3),
            "league_hand": {k: round(v, 3) for k, v in lg_hand.items() if k in ("L", "R")},
            "league_group": {GROUP_ZH.get(k, k): round(v, 3) for k, v in lg_group.items()},
            "lineup_n": LINEUP_N, "recent_days": RECENT_DAYS, "prior_pa": PRIOR_PA,
            "note": ("打線 = 近 30 天打席最多的 9 位；分項 wOBA 以 80 PA 先驗收縮到聯盟平均；"
                     f"對位分數 = {args.w_h2h}×直接對戰史偏差 + {args.w_hand}×對慣用手偏差 "
                     f"+ {args.w_group}×對主要球種偏差，依近期打席加權。"
                     f"直接對戰史以 {args.h2h_prior:.0f} PA 先驗輕度收縮（使用者指定要相信小樣本），"
                     "樣本數一律標示在旁邊供判讀。")}
    p_out = jdump({"meta": meta, "games": out}, f"{OUTPUT}/matchups.json")
    log(f"寫出 {p_out}（{len(out)} 場）")

    # ── 寫回 slate：每場帶上對位摘要，每個推薦帶上對位指標 ──
    slim_keys = ("sp_name", "sp_hand", "sp_usage", "sp_key_group", "sp_vs_L", "sp_vs_R",
                 "bat_team", "lineup_delta", "lineup_vs_hand", "lineup_vs_group",
                 "league_hand", "league_group", "verdict", "best", "worst",
                 "h2h_pa", "h2h_woba", "narrative", "delta_variants",
                 "h2h_delta", "h2h_faced", "h2h_k", "h2h_hr", "h2h_hit",
                 "h2h_best", "h2h_worst", "h2h_rows")
    by_pk = {}
    for g in slate["games"]:
        m = out.get(str(g["pk"]))
        if not m:
            continue
        g["matchup"] = {side: {k: v[k] for k in slim_keys if k in v}
                        for side, v in m.items()}
        # 面對主隊先發的是客隊打線，反之亦然
        away_delta = (m.get("home") or {}).get("lineup_delta")   # 客隊打線 vs 主隊先發
        home_delta = (m.get("away") or {}).get("lineup_delta")   # 主隊打線 vs 客隊先發
        av = (m.get("home") or {}).get("delta_variants") or {}
        hv = (m.get("away") or {}).get("delta_variants") or {}
        by_pk[g["pk"]] = {"away": away_delta, "home": home_delta,
                          "total": (away_delta or 0) + (home_delta or 0),
                          "variants": {k: {"away": av.get(k), "home": hv.get(k),
                                           "total": (av.get(k) or 0) + (hv.get(k) or 0)}
                                       for k in ("raw", "light", "safe", "no_h2h")}}
        g["matchup_score"] = by_pk[g["pk"]]

    def pick_matchup(p):
        d = by_pk.get(p.get("pk"))
        if not d:
            return None, None
        mk = p["market"]
        key = ("home" if mk.startswith("home_") else
               "away" if mk.startswith("away_") else "total")
        who = {"home": "主隊打線", "away": "客隊打線", "total": "雙方打線"}[key]
        v = d[key]
        variants = {k: (d.get("variants", {}).get(k) or {}).get(key)
                    for k in ("raw", "light", "safe", "no_h2h")}
        if v is None:
            return None, None
        # 玩法方向：小分/under 類的盤，打線越弱越有利 → 反向
        under = ("under" in mk) or mk.endswith(("_cover_p15", "_cover_p25"))
        aligned = (v > 0) if not under else (v < 0)
        return round(v, 4), {"who": who, "aligned": bool(aligned),
                             "variants": variants, "under": bool(under)}

    n_aligned = 0
    for p_ in slate["picks"]:
        v, info = pick_matchup(p_)
        p_["matchup_delta"] = v
        p_["matchup_who"] = info["who"] if info else None
        p_["matchup_aligned"] = info["aligned"] if info else None
        p_["matchup_variants"] = info["variants"] if info else None
        p_["matchup_under"] = info["under"] if info else None
        if info and info["aligned"] and abs(v) >= 0.006:
            n_aligned += 1
    jdump(slate, f"{OUTPUT}/slate.json")
    log(f"回寫 slate.json：{len(by_pk)} 場帶對位分析、"
        f"{n_aligned}/{len(slate['picks'])} 個推薦的對位方向一致")

    # 印幾個例子
    shown = 0
    for pk, e in out.items():
        for side, d in e.items():
            log(f"  {d['narrative'][:150]}")
            shown += 1
            if shown >= 4:
                return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
