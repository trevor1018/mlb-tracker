"""建立分析主資料集（嚴格 as-of，不使用該場之後的資訊）。

輸出：
  data/teamgames.parquet  一場一隊一列：該隊視角的特徵 + 玩法結果
  data/gamesds.parquet    一場一列：雙方特徵 + 全場/前5局大小分、NRFI
  data/pending.parquet    未開打場次的同款特徵（供今日推薦用）

作法：按時間順序走過每一場，先「快照」目前累積到的狀態當特徵，
再用該場結果更新狀態。所以每一列的特徵都只含該場開打前已知的資訊。
未開打場次則用「跑完所有完賽比賽後的最終狀態」加上先發預告來快照。
"""
import sys
from collections import defaultdict, deque

import numpy as np
import pandas as pd

from common import DATA, TEAM_ZH, jload, log

LEAGUE_WOBA = 0.315   # 累積樣本不足時的先驗
PRIOR_PA = 60         # 先驗權重（PA）
LEAGUE_R9 = 4.4
PRIOR_IP = 15.0


def shrunk(num, den, prior_rate, prior_w):
    """貝氏收縮：樣本少時往聯盟平均靠。"""
    return (num + prior_rate * prior_w) / (den + prior_w) if (den + prior_w) > 0 else prior_rate


class Roll:
    """單隊的累積狀態。"""

    def __init__(self):
        self.pa = defaultdict(float)
        self.woba = defaultdict(float)
        self.k = defaultdict(float)
        self.bb = defaultdict(float)
        self.hard = defaultdict(float)
        self.bip = defaultdict(float)
        self.barrel = defaultdict(float)
        self.runs = []
        self.allowed = []
        self.results = []
        self.home_runs = []
        self.away_runs = []
        self.elo = 1500.0
        self.last_date = None
        self.bp_log = deque()      # (date, ip, runs, pitches)
        self.bp_bat = deque()      # (date, pa, woba_sum) 牛棚被打
        self.game_log = defaultdict(list)   # key → [(pa, woba_sum), ...] 逐場
        self.away_streak = 0

    def woba_for(self, key):
        return shrunk(self.woba[key], self.pa[key], LEAGUE_WOBA, PRIOR_PA)

    def woba_last(self, key, n=15):
        """近 n 場的 wOBA（樣本不足往聯盟平均收縮）"""
        rows = self.game_log[key][-n:]
        pa = sum(r[0] for r in rows)
        wb = sum(r[1] for r in rows)
        return shrunk(wb, pa, LEAGUE_WOBA, PRIOR_PA)


class PitcherRoll:
    def __init__(self):
        self.starts = 0
        self.ip = 0.0
        self.r = 0.0
        self.er = 0.0
        self.k = 0.0
        self.bb = 0.0
        self.hr = 0.0
        self.pa = defaultdict(float)
        self.woba = defaultdict(float)
        self.whiff = 0.0
        self.swing = 0.0
        self.usage = defaultdict(float)
        self.velos = []
        self.last_date = None
        self.day_starts = 0
        self.night_starts = 0
        self.home_starts = 0


def ip_to_float(ip):
    if ip is None:
        return 0.0
    try:
        s = str(ip)
        if "." in s:
            whole, frac = s.split(".")
            return int(whole) + (int(frac) / 3.0 if frac else 0.0)
        return float(s)
    except Exception:
        return 0.0


def dat(d):
    return pd.Timestamp(d)


def parse_wind(w):
    """'8 mph, Out To CF' → (8, 'out')；室內或無風 → (0, 'none')"""
    if not w:
        return 0.0, "none"
    try:
        sp = float(str(w).split(" ")[0])
    except Exception:
        sp = 0.0
    t = str(w).lower()
    if "out to" in t:
        d = "out"
    elif "in from" in t:
        d = "in"
    elif " to " in t:
        d = "cross"
    else:
        d = "none"
    return sp, d


def pct(a, b):
    return (a / b) if b else np.nan


def snap_side(side, tid, spid, opp_spid, teams, pitchers, people, d, day_game):
    """單邊（主或客）的賽前特徵快照。"""
    R = teams[tid]
    P = pitchers.get(spid) if spid else None
    sp_hand = (people.get(str(spid)) or {}).get("throws") if spid else None
    opp_hand = (people.get(str(opp_spid)) or {}).get("throws") if opp_spid else None
    n = len(R.results)
    l10 = R.results[-10:]
    f = {
        f"{side}_gp": n,
        f"{side}_win_pct": np.mean(R.results) if n else np.nan,
        f"{side}_win_pct_l10": np.mean(l10) if l10 else np.nan,
        f"{side}_elo": R.elo,
        f"{side}_rpg": np.mean(R.runs) if R.runs else np.nan,
        f"{side}_rpg_l10": np.mean(R.runs[-10:]) if R.runs else np.nan,
        f"{side}_rapg": np.mean(R.allowed) if R.allowed else np.nan,
        f"{side}_rapg_l10": np.mean(R.allowed[-10:]) if R.allowed else np.nan,
        f"{side}_rest": (d - dat(R.last_date)).days if R.last_date else np.nan,
        f"{side}_woba_vsL": R.woba_for("L"),
        f"{side}_woba_vsR": R.woba_for("R"),
        f"{side}_woba_fast": R.woba_for("g:fastball"),
        f"{side}_woba_break": R.woba_for("g:breaking"),
        f"{side}_woba_off": R.woba_for("g:offspeed"),
        f"{side}_k_pct": pct(R.k["all"], R.pa["all"]),
        f"{side}_bb_pct": pct(R.bb["all"], R.pa["all"]),
        f"{side}_hard_pct": pct(R.hard["all"], R.bip["all"]),
        f"{side}_barrel_pct": pct(R.barrel["all"], R.bip["all"]),
        f"{side}_pa_vsL": R.pa["L"],
        f"{side}_pa_vsR": R.pa["R"],
        f"{side}_woba_day": R.woba_for("D"),
        f"{side}_woba_night": R.woba_for("N"),
        f"{side}_woba_home": R.woba_for("H"),
        f"{side}_woba_away": R.woba_for("A"),
        f"{side}_woba_daypart": R.woba_for("D" if day_game else "N"),
        f"{side}_woba_l15": R.woba_last("all"),
        f"{side}_woba_vsL_l15": R.woba_last("L"),
        f"{side}_woba_vsR_l15": R.woba_last("R"),
        f"{side}_xwoba": shrunk(R.woba["x:all"], R.pa["x:all"], LEAGUE_WOBA, PRIOR_PA),
        f"{side}_woba_venueside": R.woba_for("H" if side == "home" else "A"),
        f"{side}_sp_id": spid,
        f"{side}_sp_hand": sp_hand,
        f"{side}_opp_sp_hand": opp_hand,
        f"{side}_away_streak": R.away_streak,
    }
    bp_ip = sum(x[1] for x in R.bp_log)
    bp_r = sum(x[2] for x in R.bp_log)
    f[f"{side}_bp_ip14"] = bp_ip
    f[f"{side}_bp_r9_14"] = shrunk(bp_r * 9, bp_ip, LEAGUE_R9, PRIOR_IP) if bp_ip else np.nan
    bp_pa = sum(x[1] for x in R.bp_bat)
    bp_wb = sum(x[2] for x in R.bp_bat)
    f[f"{side}_bp_woba_30d"] = shrunk(bp_wb, bp_pa, LEAGUE_WOBA, 80)

    if P and P.starts > 0:
        f.update({
            f"{side}_sp_starts": P.starts,
            f"{side}_sp_ip_per_start": P.ip / P.starts,
            f"{side}_sp_r9": shrunk(P.r * 9, P.ip, LEAGUE_R9, PRIOR_IP),
            f"{side}_sp_k9": shrunk(P.k * 9, P.ip, 8.5, PRIOR_IP),
            f"{side}_sp_bb9": shrunk(P.bb * 9, P.ip, 3.1, PRIOR_IP),
            f"{side}_sp_hr9": shrunk(P.hr * 9, P.ip, 1.2, PRIOR_IP),
            f"{side}_sp_woba_vsL": shrunk(P.woba["L"], P.pa["L"], LEAGUE_WOBA, 40),
            f"{side}_sp_woba_vsR": shrunk(P.woba["R"], P.pa["R"], LEAGUE_WOBA, 40),
            f"{side}_sp_whiff": pct(P.whiff, P.swing),
            f"{side}_sp_rest": (d - dat(P.last_date)).days if P.last_date else np.nan,
            f"{side}_sp_fb_velo": np.mean(P.velos[-3:]) if P.velos else np.nan,
            f"{side}_sp_velo_delta": (np.mean(P.velos[-1:]) - np.mean(P.velos)) if len(P.velos) >= 4 else np.nan,
            f"{side}_sp_day_starts": P.day_starts,
            f"{side}_sp_woba_day": shrunk(P.woba["D"], P.pa["D"], LEAGUE_WOBA, 40),
            f"{side}_sp_woba_night": shrunk(P.woba["N"], P.pa["N"], LEAGUE_WOBA, 40),
            f"{side}_sp_woba_daypart": shrunk(P.woba["D" if day_game else "N"],
                                              P.pa["D" if day_game else "N"], LEAGUE_WOBA, 40),
            f"{side}_sp_woba_venueside": shrunk(P.woba["H" if side == "home" else "A"],
                                                P.pa["H" if side == "home" else "A"],
                                                LEAGUE_WOBA, 40),
        })
        tot_p = sum(P.usage.values()) or 1
        for grp in ("fastball", "breaking", "offspeed", "cutter"):
            f[f"{side}_sp_{grp}_pct"] = P.usage[grp] / tot_p
        f[f"{side}_sp_main"] = max(P.usage, key=P.usage.get) if P.usage else None
        fb = f[f"{side}_sp_fastball_pct"] + f[f"{side}_sp_cutter_pct"]
        brk = f[f"{side}_sp_breaking_pct"]
        off = f[f"{side}_sp_offspeed_pct"]
        f[f"{side}_sp_profile"] = ("BRK" if brk >= 0.35 else
                                   "OFF" if off >= 0.25 else
                                   "FB" if fb >= 0.62 else "BAL")
        # 最常用的「非速球」球種族群
        sec = {"breaking": brk, "offspeed": off}
        f[f"{side}_sp_secondary"] = max(sec, key=sec.get) if max(sec.values()) > 0.1 else None
    else:
        for k in ("sp_starts", "sp_ip_per_start", "sp_r9", "sp_k9", "sp_bb9", "sp_hr9",
                  "sp_woba_vsL", "sp_woba_vsR", "sp_whiff", "sp_rest", "sp_fb_velo",
                  "sp_velo_delta", "sp_day_starts", "sp_fastball_pct", "sp_breaking_pct",
                  "sp_offspeed_pct", "sp_cutter_pct", "sp_woba_day", "sp_woba_night",
                  "sp_woba_daypart", "sp_woba_venueside"):
            f[f"{side}_{k}"] = np.nan
        f[f"{side}_sp_main"] = None
        f[f"{side}_sp_profile"] = None
        f[f"{side}_sp_secondary"] = None
    return f


def snap_game(g, sp, teams, pitchers, people):
    """整場（主+客+交叉特徵）的賽前特徵快照。"""
    d = dat(g["date"])
    feat = {}
    for side, tid in (("home", g["home"]), ("away", g["away"])):
        other = "away" if side == "home" else "home"
        feat.update(snap_side(side, tid, sp[side], sp[other], teams, pitchers,
                              people, d, g.get("dayGame")))
    for side, other in (("home", "away"), ("away", "home")):
        oh = feat.get(f"{other}_sp_hand")
        feat[f"{side}_bat_vs_oppSP_hand_woba"] = (
            feat[f"{side}_woba_vsL"] if oh == "L" else feat[f"{side}_woba_vsR"])
        main = feat.get(f"{other}_sp_main")
        key = {"fastball": "woba_fast", "breaking": "woba_break",
               "offspeed": "woba_off", "cutter": "woba_fast"}.get(main)
        feat[f"{side}_bat_vs_oppSP_main_woba"] = feat.get(f"{side}_{key}") if key else np.nan
        feat[f"{side}_oppSP_main"] = main
        feat[f"{side}_oppSP_profile"] = feat.get(f"{other}_sp_profile")
        sec = feat.get(f"{other}_sp_secondary")
        skey = {"breaking": "woba_break", "offspeed": "woba_off"}.get(sec)
        feat[f"{side}_bat_vs_oppSP_2nd_woba"] = feat.get(f"{side}_{skey}") if skey else np.nan
        paL, paR = feat[f"{side}_pa_vsL"], feat[f"{side}_pa_vsR"]
        feat[f"{side}_oppSP_woba_vs_us"] = (
            feat.get(f"{other}_sp_woba_vsR") if (paR or 0) >= (paL or 0)
            else feat.get(f"{other}_sp_woba_vsL"))
        feat[f"{side}_sp_woba_vs_us"] = feat[f"{side}_oppSP_woba_vs_us"]
    return feat


def base_row(g):
    d = dat(g["date"])
    wsp, wdir = parse_wind(g.get("wind"))
    cond = (g.get("cond") or "")
    return {
        "wind_speed": wsp, "wind_dir": wdir,
        "wind_out": wdir == "out", "wind_in": wdir == "in",
        "roof": cond in ("Dome", "Roof Closed"),
        "pk": g["pk"], "date": g["date"], "month": d.month, "dow": d.dayofweek,
        "day_game": bool(g.get("dayGame")), "venue": g.get("venueId"),
        "temp": pd.to_numeric(g.get("temp"), errors="coerce"),
        "cond": g.get("cond"), "wind": g.get("wind"),
        "series_game": g.get("seriesGame"), "series_len": g.get("seriesLen"),
    }


def main():
    games = jload(f"{DATA}/games.json")
    boxes = {b["pk"]: b for b in jload(f"{DATA}/boxes.json.gz")}
    people = jload(f"{DATA}/people.json")

    tg_hand = pd.read_parquet(f"{DATA}/tg_hand.parquet")
    tg_group = pd.read_parquet(f"{DATA}/tg_group.parquet")
    pg_pitcher = pd.read_parquet(f"{DATA}/pg_pitcher.parquet")
    pg_usage = pd.read_parquet(f"{DATA}/pg_usage.parquet")
    pg_velo = pd.read_parquet(f"{DATA}/pg_velo.parquet")

    hand_by_game = {k: v for k, v in tg_hand.groupby("game_pk")}
    group_by_game = {k: v for k, v in tg_group.groupby("game_pk")}
    pit_by_game = {k: v for k, v in pg_pitcher.groupby("game_pk")}
    usage_by_game = {k: v for k, v in pg_usage.groupby("game_pk")}
    velo_by_game = {k: dict(zip(v["pitcher"], v["fb_velo"])) for k, v in pg_velo.groupby("game_pk")}

    teams = {t: Roll() for t in TEAM_ZH}
    pitchers = defaultdict(PitcherRoll)
    venue_runs = defaultdict(list)          # venueId → 該場地歷史總分
    faced = defaultdict(int)                # (team, pitcher) → 本季已對過幾次

    tg_rows, g_rows = [], []
    seen = set()
    for g in games:
        pk = g["pk"]
        if pk in seen:
            continue
        seen.add(pk)
        box = boxes.get(pk)
        if not box:
            continue
        hs, as_ = g["homeScore"], g["awayScore"]
        if hs is None or as_ is None:
            continue
        date, d = g["date"], dat(g["date"])
        home, away = g["home"], g["away"]
        sp = {}
        for side in ("home", "away"):
            s = box[side]["sp"]
            sp[side] = s["id"] if s else g.get(f"{side}SpProb")

        feat = snap_game(g, sp, teams, pitchers, people)

        innings = g.get("innings") or []
        h_in = [(i.get("h") or 0) for i in innings]
        a_in = [(i.get("a") or 0) for i in innings]
        f5_h, f5_a = sum(h_in[:5]), sum(a_in[:5])
        total = hs + as_
        common = base_row(g)
        common.update({
            "extra": len(innings) > 9,
            "home_score": hs, "away_score": as_, "total": total,
            "f5_total": f5_h + f5_a,
            "f1_total": (h_in[0] if h_in else 0) + (a_in[0] if a_in else 0),
        })

        vr = venue_runs[g.get("venueId")]
        common["park_factor"] = ((sum(vr) + 8.94 * 15) / (len(vr) + 15)) if True else 8.94
        common["park_games"] = len(vr)
        common["home_faced_opp_sp"] = faced[(home, sp["away"])] if sp["away"] else 0
        common["away_faced_opp_sp"] = faced[(away, sp["home"])] if sp["home"] else 0

        g_row = dict(common)
        g_row.update(feat)
        g_row["home_team"], g_row["away_team"] = home, away
        g_row["home_team_zh"], g_row["away_team_zh"] = TEAM_ZH[home], TEAM_ZH[away]
        for line in (6.5, 7.5, 8.5, 9.5, 10.5, 11.5):
            g_row[f"over_{line}"] = total > line
        for line in (3.5, 4.5, 5.5):
            g_row[f"f5_over_{line}"] = (f5_h + f5_a) > line
        g_row["nrfi"] = ((h_in[:1] or [0])[0] + (a_in[:1] or [0])[0]) == 0
        g_rows.append(g_row)

        for side, tid, opp in (("home", home, away), ("away", away, home)):
            me = hs if side == "home" else as_
            them = as_ if side == "home" else hs
            my_f5 = f5_h if side == "home" else f5_a
            opp_f5 = f5_a if side == "home" else f5_h
            other = "away" if side == "home" else "home"
            r = dict(common)
            r.update({
                "team": tid, "opp": opp, "is_home": side == "home",
                "team_zh": TEAM_ZH[tid], "opp_zh": TEAM_ZH[opp],
                "runs": me, "runs_allowed": them, "margin": me - them,
                "runs_f5": my_f5, "runs_allowed_f5": opp_f5,
                "win": me > them,
                "cover_m15": (me - them) >= 2,
                "cover_p15": (them - me) <= 1,
                "cover_m25": (me - them) >= 3,
                "cover_p25": (them - me) <= 2,
                "f5_lead": my_f5 > opp_f5,
                "f5_no_trail": my_f5 >= opp_f5,
                "faced_opp_sp": common[f"{side}_faced_opp_sp"],
            })
            for line in (2.5, 3.5, 4.5, 5.5, 6.5):
                r[f"tt_over_{line}"] = me > line
                r[f"tt_under_{line}"] = me < line
            for line in (6.5, 7.5, 8.5, 9.5, 10.5):
                r[f"over_{line}"] = total > line
            for k, v in feat.items():
                if k.startswith(side + "_"):
                    r["my_" + k[len(side) + 1:]] = v
                elif k.startswith(other + "_"):
                    r["op_" + k[len(other) + 1:]] = v
            tg_rows.append(r)

        # ── 更新狀態 ──
        hb = hand_by_game.get(pk)
        if hb is not None:
            for _, x in hb.iterrows():
                R = teams.get(int(x["bat_team"]))
                if R is None:
                    continue
                hand = x["p_throws"] if x["p_throws"] in ("L", "R") else "R"
                dn = "D" if g.get("dayGame") else "N"
                ha = "H" if int(x["bat_team"]) == home else "A"
                for key in (hand, "all", dn, ha):
                    R.pa[key] += x["pa"]
                    R.woba[key] += x["woba_sum"]
                for key in (hand, "all"):
                    R.game_log[key].append((float(x["pa"]), float(x["woba_sum"])))
                if x["xwoba_n"]:
                    R.pa["x:all"] += x["xwoba_n"]
                    R.woba["x:all"] += x["xwoba_sum"]
                    R.k[key] += x["k"]
                    R.bb[key] += x["bb"]
                    R.hard[key] += x["hard"]
                    R.bip[key] += x["bip"]
                    R.barrel[key] += x["barrel"]
        gb = group_by_game.get(pk)
        if gb is not None:
            for _, x in gb.iterrows():
                R = teams.get(int(x["bat_team"]))
                if R is None:
                    continue
                key = "g:" + str(x["pgroup"])
                R.pa[key] += x["pa"]
                R.woba[key] += x["woba_sum"]

        pb = pit_by_game.get(pk)
        p_pa = defaultdict(lambda: defaultdict(float))
        p_woba = defaultdict(lambda: defaultdict(float))
        p_swing, p_whiff = defaultdict(float), defaultdict(float)
        if pb is not None:
            for _, x in pb.iterrows():
                pid = int(x["pitcher"])
                stand = x["stand"] if x["stand"] in ("L", "R") else "R"
                p_pa[pid][stand] += x["pa"]
                p_woba[pid][stand] += x["woba_sum"]
                p_swing[pid] += x["swings"]
                p_whiff[pid] += x["whiffs"]
        ub = usage_by_game.get(pk)
        usage_now = defaultdict(lambda: defaultdict(float))
        if ub is not None:
            for _, x in ub.iterrows():
                usage_now[int(x["pitcher"])][str(x["pgroup"])] += x["n"]
        velos = velo_by_game.get(pk, {})

        for side, tid in (("home", home), ("away", away)):
            R = teams[tid]
            sside = box[side]
            if sside["sp"]:
                s = sside["sp"]
                P = pitchers[s["id"]]
                P.starts += 1
                P.ip += ip_to_float(s.get("ip"))
                P.r += s.get("r") or 0
                P.er += s.get("er") or 0
                P.k += s.get("so") or 0
                P.bb += s.get("bb") or 0
                P.hr += s.get("hr") or 0
                P.last_date = date
                if g.get("dayGame"):
                    P.day_starts += 1
                else:
                    P.night_starts += 1
                if side == "home":
                    P.home_starts += 1
                for st in ("L", "R"):
                    P.pa[st] += p_pa[s["id"]][st]
                    P.woba[st] += p_woba[s["id"]][st]
                tot_pa = p_pa[s["id"]]["L"] + p_pa[s["id"]]["R"]
                tot_wb = p_woba[s["id"]]["L"] + p_woba[s["id"]]["R"]
                for key in ("D" if g.get("dayGame") else "N",
                            "H" if side == "home" else "A"):
                    P.pa[key] += tot_pa
                    P.woba[key] += tot_wb
                P.swing += p_swing[s["id"]]
                P.whiff += p_whiff[s["id"]]
                for grp, nn in usage_now[s["id"]].items():
                    P.usage[grp] += nn
                v = velos.get(s["id"])
                if v is not None and not pd.isna(v):
                    P.velos.append(float(v))
            sp_id = sside["sp"]["id"] if sside["sp"] else None
            bp_pa = bp_wb = 0.0
            if pb is not None:
                for _, x in pb.iterrows():
                    if int(x["pitcher"]) == sp_id:
                        continue
                    if int(x["pit_team"]) != tid:
                        continue
                    bp_pa += float(x["pa"])
                    bp_wb += float(x["woba_sum"])
            R.bp_bat.append((date, bp_pa, bp_wb))
            while R.bp_bat and (d - dat(R.bp_bat[0][0])).days > 30:
                R.bp_bat.popleft()
            bp_ip = sum(ip_to_float(p.get("ip")) for p in sside["bp"])
            bp_r = sum((p.get("r") or 0) for p in sside["bp"])
            bp_p = sum((p.get("pitches") or 0) for p in sside["bp"])
            R.bp_log.append((date, bp_ip, bp_r, bp_p))
            while R.bp_log and (d - dat(R.bp_log[0][0])).days > 14:
                R.bp_log.popleft()
            for p in sside["bp"]:
                P = pitchers[p["id"]]
                P.ip += ip_to_float(p.get("ip"))
                P.r += p.get("r") or 0
                P.last_date = date

        for side, tid in (("home", home), ("away", away)):
            R = teams[tid]
            me = hs if side == "home" else as_
            them = as_ if side == "home" else hs
            R.runs.append(me)
            R.allowed.append(them)
            R.results.append(1 if me > them else 0)
            R.last_date = date
            if side == "home":
                R.home_runs.append(me)
                R.away_streak = 0
            else:
                R.away_runs.append(me)
                R.away_streak += 1

        venue_runs[g.get("venueId")].append(hs + as_)
        if sp["home"]:
            faced[(away, sp["home"])] += 1
        if sp["away"]:
            faced[(home, sp["away"])] += 1

        eh, ea = teams[home].elo, teams[away].elo
        exp_h = 1 / (1 + 10 ** (-((eh + 25) - ea) / 400))
        act_h = 1.0 if hs > as_ else 0.0
        teams[home].elo = eh + 20 * (act_h - exp_h)
        teams[away].elo = ea + 20 * ((1 - act_h) - (1 - exp_h))

    tg = pd.DataFrame(tg_rows)
    gd = pd.DataFrame(g_rows)
    tg.to_parquet(f"{DATA}/teamgames.parquet", index=False)
    gd.to_parquet(f"{DATA}/gamesds.parquet", index=False)
    log(f"teamgames {tg.shape}  gamesds {gd.shape}")
    log(f"日期範圍 {tg['date'].min()} ~ {tg['date'].max()}")

    # ── 未開打場次：用最終狀態 + 先發預告 ──
    try:
        pending = jload(f"{DATA}/pending.json")
    except Exception:
        pending = []
    prows = []
    for g in pending:
        sp = {"home": g.get("homeSpProb"), "away": g.get("awaySpProb")}
        feat = snap_game(g, sp, teams, pitchers, people)
        r = base_row(g)
        vr = venue_runs[g.get("venueId")]
        r["park_factor"] = (sum(vr) + 8.94 * 15) / (len(vr) + 15)
        r["park_games"] = len(vr)
        r["home_faced_opp_sp"] = faced[(g["home"], sp["away"])] if sp["away"] else 0
        r["away_faced_opp_sp"] = faced[(g["away"], sp["home"])] if sp["home"] else 0
        r.update(feat)
        r["home_team"], r["away_team"] = g["home"], g["away"]
        r["home_team_zh"], r["away_team_zh"] = TEAM_ZH[g["home"]], TEAM_ZH[g["away"]]
        r["sp_known"] = bool(sp["home"] and sp["away"])
        prows.append(r)
    if prows:
        pd.DataFrame(prows).to_parquet(f"{DATA}/pending.parquet", index=False)
        log(f"pending {len(prows)} 場（先發已公布 {sum(r['sp_known'] for r in prows)} 場）")

    log("市場基準率：")
    for m in ("win", "cover_m15", "cover_p15", "tt_over_3.5", "tt_over_4.5", "f5_lead"):
        log(f"  {m:<14} {tg[m].mean():.3f}")
    for m in ("over_7.5", "over_8.5", "over_9.5", "f5_over_4.5", "nrfi"):
        log(f"  {m:<14} {gd[m].mean():.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
