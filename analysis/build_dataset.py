"""建立分析主資料集（嚴格 as-of，不使用該場之後的資訊）。

輸出：
  data/teamgames.parquet  一場一隊一列：該隊視角的特徵 + 玩法結果
                          （ML、讓分 1.5/2.5、單隊大小分、前5局領先…）
  data/gamesds.parquet    一場一列：雙方特徵 + 全場大小分/前5局大小分/NRFI

作法：按時間順序走過每一場，先「快照」目前累積到的狀態當特徵，
再用該場結果更新狀態。所以每一列的特徵都只含該場開打前已知的資訊。
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
        self.pa = defaultdict(float)       # key → PA
        self.woba = defaultdict(float)     # key → woba_value 合計
        self.k = defaultdict(float)
        self.bb = defaultdict(float)
        self.hard = defaultdict(float)
        self.bip = defaultdict(float)
        self.barrel = defaultdict(float)
        self.runs = []                     # 每場得分
        self.allowed = []                  # 每場失分
        self.results = []                  # 1 勝 0 敗
        self.home_runs = []
        self.away_runs = []
        self.elo = 1500.0
        self.last_date = None
        self.bp_log = deque()              # (date, ip, runs, pitches)
        self.away_streak = 0

    def woba_for(self, key):
        return shrunk(self.woba[key], self.pa[key], LEAGUE_WOBA, PRIOR_PA)

    def rate(self, num_d, key, prior):
        return shrunk(num_d[key], self.pa[key], prior, PRIOR_PA)


class PitcherRoll:
    def __init__(self):
        self.starts = 0
        self.ip = 0.0
        self.r = 0.0
        self.er = 0.0
        self.k = 0.0
        self.bb = 0.0
        self.hr = 0.0
        self.pa = defaultdict(float)     # 'L'/'R' → PA
        self.woba = defaultdict(float)
        self.whiff = 0.0
        self.swing = 0.0
        self.usage = defaultdict(float)  # pgroup → pitches
        self.velos = []
        self.last_date = None
        self.day_starts = 0
        self.night_starts = 0
        self.home_starts = 0
        self.game_r = []                 # 每場失分
        self.game_ip = []


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


def pct(a, b):
    return (a / b) if b else np.nan


def main():
    games = jload(f"{DATA}/games.json")
    boxes = {b["pk"]: b for b in jload(f"{DATA}/boxes.json.gz")}
    people = jload(f"{DATA}/people.json")

    tg_hand = pd.read_parquet(f"{DATA}/tg_hand.parquet")
    tg_group = pd.read_parquet(f"{DATA}/tg_group.parquet")
    pg_pitcher = pd.read_parquet(f"{DATA}/pg_pitcher.parquet")
    pg_usage = pd.read_parquet(f"{DATA}/pg_usage.parquet")
    pg_velo = pd.read_parquet(f"{DATA}/pg_velo.parquet")

    # 索引化：game_pk → 該場的分項聚合
    hand_by_game = {k: v for k, v in tg_hand.groupby("game_pk")}
    group_by_game = {k: v for k, v in tg_group.groupby("game_pk")}
    pit_by_game = {k: v for k, v in pg_pitcher.groupby("game_pk")}
    usage_by_game = {k: v for k, v in pg_usage.groupby("game_pk")}
    velo_by_game = {k: dict(zip(v["pitcher"], v["fb_velo"])) for k, v in pg_velo.groupby("game_pk")}

    teams = {t: Roll() for t in TEAM_ZH}
    pitchers = defaultdict(PitcherRoll)

    tg_rows = []   # team-game 列
    g_rows = []    # game 列

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
        date = g["date"]
        d = dat(date)

        home, away = g["home"], g["away"]
        sp = {}
        for side, tid in (("home", home), ("away", away)):
            s = box[side]["sp"]
            sp[side] = s["id"] if s else g.get(f"{side}SpProb")

        # ── 快照特徵（賽前狀態）──
        feat = {}
        for side, tid, opp in (("home", home, away), ("away", away, home)):
            R = teams[tid]
            spid = sp[side]
            opp_spid = sp["away" if side == "home" else "home"]
            P = pitchers.get(spid) if spid else None
            OP = pitchers.get(opp_spid) if opp_spid else None
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
                f"{side}_sp_id": spid,
                f"{side}_sp_hand": sp_hand,
                f"{side}_opp_sp_hand": opp_hand,
                f"{side}_away_streak": R.away_streak,
            }
            # 牛棚近 14 天
            bp_ip = sum(x[1] for x in R.bp_log)
            bp_r = sum(x[2] for x in R.bp_log)
            f[f"{side}_bp_ip14"] = bp_ip
            f[f"{side}_bp_r9_14"] = shrunk(bp_r * 9, bp_ip, LEAGUE_R9, PRIOR_IP) if bp_ip else np.nan

            # 自隊先發投手（as-of）
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
                })
                tot_p = sum(P.usage.values()) or 1
                for grp in ("fastball", "breaking", "offspeed", "cutter"):
                    f[f"{side}_sp_{grp}_pct"] = P.usage[grp] / tot_p
                f[f"{side}_sp_main"] = max(P.usage, key=P.usage.get) if P.usage else None
            else:
                for k in ("sp_starts", "sp_ip_per_start", "sp_r9", "sp_k9", "sp_bb9", "sp_hr9",
                          "sp_woba_vsL", "sp_woba_vsR", "sp_whiff", "sp_rest", "sp_fb_velo",
                          "sp_velo_delta", "sp_fastball_pct", "sp_breaking_pct",
                          "sp_offspeed_pct", "sp_cutter_pct"):
                    f[f"{side}_{k}"] = np.nan
                f[f"{side}_sp_main"] = None
            feat.update(f)

        # 交叉特徵：我方打擊 vs 對手先發慣用手 / 主要球種
        for side, other in (("home", "away"), ("away", "home")):
            oh = feat.get(f"{other}_sp_hand")
            feat[f"{side}_bat_vs_oppSP_hand_woba"] = (
                feat[f"{side}_woba_vsL"] if oh == "L" else feat[f"{side}_woba_vsR"])
            main = feat.get(f"{other}_sp_main")
            key = {"fastball": "woba_fast", "breaking": "woba_break",
                   "offspeed": "woba_off", "cutter": "woba_fast"}.get(main)
            feat[f"{side}_bat_vs_oppSP_main_woba"] = feat.get(f"{side}_{key}") if key else np.nan
            feat[f"{side}_oppSP_main"] = main
            # 對手先發 vs 我隊主要打擊左右手（用球隊 PA 佔比近似）
            paL, paR = feat[f"{side}_pa_vsL"], feat[f"{side}_pa_vsR"]
            feat[f"{side}_oppSP_woba_vs_us"] = (
                feat.get(f"{other}_sp_woba_vsR") if (paR or 0) >= (paL or 0)
                else feat.get(f"{other}_sp_woba_vsL"))

        # ── 玩法結果 ──
        innings = g.get("innings") or []
        h_in = [(i.get("h") or 0) for i in innings]
        a_in = [(i.get("a") or 0) for i in innings]
        f5_h, f5_a = sum(h_in[:5]), sum(a_in[:5])
        total = hs + as_
        row_common = {
            "pk": pk, "date": date, "month": d.month, "dow": d.dayofweek,
            "day_game": bool(g.get("dayGame")), "venue": g.get("venueId"),
            "temp": pd.to_numeric(g.get("temp"), errors="coerce"),
            "cond": g.get("cond"), "wind": g.get("wind"),
            "series_game": g.get("seriesGame"), "series_len": g.get("seriesLen"),
            "extra": len(innings) > 9,
            "home_score": hs, "away_score": as_, "total": total,
            "f5_total": f5_h + f5_a, "f1_total": (h_in[0] if h_in else 0) + (a_in[0] if a_in else 0),
        }
        g_row = dict(row_common)
        g_row.update({k: v for k, v in feat.items()})
        g_row["home_team"] = home
        g_row["away_team"] = away
        for line in (6.5, 7.5, 8.5, 9.5, 10.5, 11.5):
            g_row[f"over_{line}"] = total > line
        for line in (3.5, 4.5, 5.5):
            g_row[f"f5_over_{line}"] = (f5_h + f5_a) > line
        g_row["nrfi"] = (h_in[:1] or [0])[0] + (a_in[:1] or [0])[0] == 0
        g_rows.append(g_row)

        # team-game 兩列
        for side, tid, opp in (("home", home, away), ("away", away, home)):
            me = hs if side == "home" else as_
            them = as_ if side == "home" else hs
            my_f5 = f5_h if side == "home" else f5_a
            opp_f5 = f5_a if side == "home" else f5_h
            other = "away" if side == "home" else "home"
            r = dict(row_common)
            r.update({
                "team": tid, "opp": opp, "is_home": side == "home",
                "team_zh": TEAM_ZH[tid], "opp_zh": TEAM_ZH[opp],
                "runs": me, "runs_allowed": them,
                "margin": me - them,
                "win": me > them,
                "cover_m15": (me - them) >= 2,     # 讓分 1.5 過關
                "cover_p15": (them - me) <= 1,     # 受讓 1.5 過關
                "cover_m25": (me - them) >= 3,
                "cover_p25": (them - me) <= 2,
                "f5_lead": my_f5 > opp_f5,
                "f5_no_trail": my_f5 >= opp_f5,
            })
            for line in (2.5, 3.5, 4.5, 5.5, 6.5):
                r[f"tt_over_{line}"] = me > line
                r[f"tt_under_{line}"] = me < line
            for line in (6.5, 7.5, 8.5, 9.5, 10.5):
                r[f"over_{line}"] = total > line
            # 我方視角特徵（去掉 home_/away_ 前綴）
            for k, v in feat.items():
                if k.startswith(side + "_"):
                    r["my_" + k[len(side) + 1:]] = v
                elif k.startswith(other + "_"):
                    r["op_" + k[len(other) + 1:]] = v
            tg_rows.append(r)

        # ── 用該場結果更新狀態（之後的比賽才看得到）──
        # 1) 逐球分項
        hb = hand_by_game.get(pk)
        if hb is not None:
            for _, x in hb.iterrows():
                R = teams.get(int(x["bat_team"]))
                if R is None:
                    continue
                hand = x["p_throws"] if x["p_throws"] in ("L", "R") else "R"
                for key in (hand, "all"):
                    R.pa[key] += x["pa"]
                    R.woba[key] += x["woba_sum"]
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
        # 2) 投手（先發 + 牛棚）
        pb = pit_by_game.get(pk)
        pitcher_pa = defaultdict(lambda: defaultdict(float))
        pitcher_woba = defaultdict(lambda: defaultdict(float))
        pitcher_swing = defaultdict(float)
        pitcher_whiff = defaultdict(float)
        if pb is not None:
            for _, x in pb.iterrows():
                pid = int(x["pitcher"])
                stand = x["stand"] if x["stand"] in ("L", "R") else "R"
                pitcher_pa[pid][stand] += x["pa"]
                pitcher_woba[pid][stand] += x["woba_sum"]
                pitcher_swing[pid] += x["swings"]
                pitcher_whiff[pid] += x["whiffs"]
        ub = usage_by_game.get(pk)
        usage_now = defaultdict(lambda: defaultdict(float))
        if ub is not None:
            for _, x in ub.iterrows():
                usage_now[int(x["pitcher"])][str(x["pgroup"])] += x["n"]
        velos = velo_by_game.get(pk, {})

        for side, tid in (("home", home), ("away", away)):
            R = teams[tid]
            sside = box[side]
            spid = sp[side]
            # 先發
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
                P.game_r.append(s.get("r") or 0)
                P.game_ip.append(ip_to_float(s.get("ip")))
                P.last_date = date
                if g.get("dayGame"):
                    P.day_starts += 1
                else:
                    P.night_starts += 1
                if side == "home":
                    P.home_starts += 1
                for st in ("L", "R"):
                    P.pa[st] += pitcher_pa[s["id"]][st]
                    P.woba[st] += pitcher_woba[s["id"]][st]
                P.swing += pitcher_swing[s["id"]]
                P.whiff += pitcher_whiff[s["id"]]
                for grp, n in usage_now[s["id"]].items():
                    P.usage[grp] += n
                if s["id"] in velos and not pd.isna(velos[s["id"]]):
                    P.velos.append(float(velos[s["id"]]))
            # 牛棚
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

        # 3) 球隊層級
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
        # 4) Elo
        eh, ea = teams[home].elo, teams[away].elo
        exp_h = 1 / (1 + 10 ** (-((eh + 25) - ea) / 400))
        act_h = 1.0 if hs > as_ else 0.0
        k = 20
        teams[home].elo = eh + k * (act_h - exp_h)
        teams[away].elo = ea + k * ((1 - act_h) - (1 - exp_h))

    tg = pd.DataFrame(tg_rows)
    gd = pd.DataFrame(g_rows)
    tg.to_parquet(f"{DATA}/teamgames.parquet", index=False)
    gd.to_parquet(f"{DATA}/gamesds.parquet", index=False)
    log(f"teamgames {tg.shape}  gamesds {gd.shape}")
    log(f"日期範圍 {tg['date'].min()} ~ {tg['date'].max()}")
    log("市場基準率：")
    for m in ("win", "cover_m15", "cover_p15", "tt_over_3.5", "tt_over_4.5", "f5_lead"):
        log(f"  {m:<14} {tg[m].mean():.3f}")
    for m in ("over_7.5", "over_8.5", "over_9.5", "f5_over_4.5", "nrfi"):
        log(f"  {m:<14} {gd[m].mean():.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
