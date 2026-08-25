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
    ap.add_argument("--h2h-seasons", default="2024,2025,2026")
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

    # ── 直接對戰史（跨三季）──
    h2h_seasons = [int(x) for x in args.h2h_seasons.split(",")]
    ph2 = load_pitches(h2h_seasons) if set(h2h_seasons) - set(seasons) else p
    h2h = agg_woba(ph2, ["batter", "pitcher"])
    h2h_map = {(int(r.batter), int(r.pitcher)): (float(r.pa), float(r.woba))
               for r in h2h.itertuples()}

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
                delta = (0.6 * (wh - lg_hand.get(hand, lg_all))
                         + 0.4 * (wg - lg_group.get(key_group, lg_all)))
                h2h_pa, h2h_w = h2h_map.get((bid, spid), (0.0, None))
                rows.append({
                    "id": bid, "name": id2name.get(bid, str(bid)),
                    "recent_pa": round(rpa, 1),
                    "vs_hand": round(wh, 3), "vs_hand_pa": round(pah),
                    "vs_group": round(wg, 3), "vs_group_pa": round(pag),
                    "delta": round(delta, 4),
                    "h2h_pa": round(h2h_pa), "h2h_woba": (round(h2h_w, 3) if h2h_pa >= 3 else None),
                })
                tot_pa += rpa
            lineup_delta = (sum(r["delta"] * r["recent_pa"] for r in rows) / tot_pa
                            if tot_pa else 0.0)
            lineup_hand = (sum(r["vs_hand"] * r["recent_pa"] for r in rows) / tot_pa
                           if tot_pa else lg_all)
            lineup_group = (sum(r["vs_group"] * r["recent_pa"] for r in rows) / tot_pa
                            if tot_pa else lg_all)
            best = sorted(rows, key=lambda r: -r["delta"])[:3]
            worst = sorted(rows, key=lambda r: r["delta"])[:2]
            h2h_tot = sum(r["h2h_pa"] for r in rows)
            h2h_woba = (sum((r["h2h_woba"] or 0) * r["h2h_pa"] for r in rows) / h2h_tot
                        if h2h_tot else None)

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
                f"這條打線對{HAND_ZH.get(hand, hand)} wOBA {lineup_hand:.3f}"
                f"（聯盟 {lg_hand.get(hand, lg_all):.3f}）、"
                f"對他最常用的{GROUP_ZH.get(key_group, key_group)} {lineup_group:.3f}"
                f"（聯盟 {lg_group.get(key_group, lg_all):.3f}）。"
            )
            if best:
                narrative += "最吃得下的是 " + "、".join(
                    f"{b['name']}（對{HAND_ZH.get(hand, hand)} {b['vs_hand']:.3f}／"
                    f"對{GROUP_ZH.get(key_group, key_group)} {b['vs_group']:.3f}）"
                    for b in best[:2]) + "；"
            if worst:
                narrative += "最吃虧的是 " + "、".join(
                    f"{w['name']}（{w['vs_hand']:.3f}／{w['vs_group']:.3f}）"
                    for w in worst[:1]) + "。"
            if w_l is not None and w_r is not None:
                narrative += (f" {spn} 本季對左打被打 {w_l:.3f}（{pa_l:.0f} PA）、"
                              f"對右打 {w_r:.3f}（{pa_r:.0f} PA）")
                if sp.get("sp_r9") is not None:
                    narrative += f"，R/9 {sp['sp_r9']}"
                narrative += "。"
            if h2h_tot >= 20 and h2h_woba is not None:
                narrative += (f" 直接對戰史 {h2h_tot:.0f} PA wOBA {h2h_woba:.3f}"
                              f"（樣本仍偏小，僅供參考）。")
            elif h2h_tot > 0:
                narrative += f" 直接對戰史只有 {h2h_tot:.0f} PA，樣本不足不列入判斷。"

            entry[side] = {
                "sp_name": spn, "sp_hand": hand, "sp_usage": use,
                "sp_key_group": GROUP_ZH.get(key_group, key_group),
                "sp_vs_L": (round(w_l, 3) if w_l is not None else None),
                "sp_vs_L_pa": round(pa_l),
                "sp_vs_R": (round(w_r, 3) if w_r is not None else None),
                "sp_vs_R_pa": round(pa_r),
                "bat_team": bat_zh,
                "lineup_delta": round(lineup_delta, 4),
                "lineup_vs_hand": round(lineup_hand, 3),
                "lineup_vs_group": round(lineup_group, 3),
                "league_hand": round(lg_hand.get(hand, lg_all), 3),
                "league_group": round(lg_group.get(key_group, lg_all), 3),
                "verdict": verdict,
                "best": best, "worst": worst,
                "h2h_pa": round(h2h_tot), "h2h_woba": (round(h2h_woba, 3) if h2h_woba else None),
                "narrative": narrative,
                "lineup": rows,
            }
        if entry:
            out[str(g["pk"])] = entry

    meta = {"seasons": seasons, "h2h_seasons": h2h_seasons,
            "league_woba": round(lg_all, 3),
            "league_hand": {k: round(v, 3) for k, v in lg_hand.items() if k in ("L", "R")},
            "league_group": {GROUP_ZH.get(k, k): round(v, 3) for k, v in lg_group.items()},
            "lineup_n": LINEUP_N, "recent_days": RECENT_DAYS, "prior_pa": PRIOR_PA,
            "note": ("打線 = 近 30 天打席最多的 9 位；分項 wOBA 以 80 PA 先驗收縮到聯盟平均；"
                     "對位分數 = 0.6×對慣用手偏差 + 0.4×對主要球種偏差，依近期打席加權。"
                     "直接對戰史樣本普遍不足（一支打線平均只有 2-38 PA），只附註不採信。")}
    p_out = jdump({"meta": meta, "games": out}, f"{OUTPUT}/matchups.json")
    log(f"寫出 {p_out}（{len(out)} 場）")

    # ── 寫回 slate：每場帶上對位摘要，每個推薦帶上對位指標 ──
    slim_keys = ("sp_name", "sp_hand", "sp_usage", "sp_key_group", "sp_vs_L", "sp_vs_R",
                 "bat_team", "lineup_delta", "lineup_vs_hand", "lineup_vs_group",
                 "league_hand", "league_group", "verdict", "best", "worst",
                 "h2h_pa", "h2h_woba", "narrative")
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
        by_pk[g["pk"]] = {"away": away_delta, "home": home_delta,
                          "total": (away_delta or 0) + (home_delta or 0)}
        g["matchup_score"] = by_pk[g["pk"]]

    def pick_matchup(p):
        d = by_pk.get(p.get("pk"))
        if not d:
            return None, None
        mk = p["market"]
        if mk.startswith("home_"):
            v, who = d["home"], "主隊打線"
        elif mk.startswith("away_"):
            v, who = d["away"], "客隊打線"
        else:                       # 全場大小分：兩條打線一起看
            v, who = d["total"], "雙方打線"
        if v is None:
            return None, None
        # 玩法方向：小分/under 類的盤，打線越弱越有利 → 反向
        under = ("under" in mk) or mk.endswith(("_cover_p15", "_cover_p25"))
        aligned = (v > 0) if not under else (v < 0)
        return round(v, 4), {"who": who, "aligned": bool(aligned)}

    n_aligned = 0
    for p_ in slate["picks"]:
        v, info = pick_matchup(p_)
        p_["matchup_delta"] = v
        p_["matchup_who"] = info["who"] if info else None
        p_["matchup_aligned"] = info["aligned"] if info else None
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
