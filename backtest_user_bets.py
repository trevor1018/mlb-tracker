"""回測使用者下注紀錄 — 對每個 leg, 重建當時的指標訊號方向, 統計各指標命中率。

用法:
    python -X utf8 backtest_user_bets.py <betting_log.md>

輸出:
    1. 各指標命中率排名 (分 ML / Total / Spread)
    2. 與使用者實際選擇的方向比對 (使用者跟對 vs 跟反方向)
    3. 哪些 leg 用哪個指標下會贏
"""
import sys, re, json, os
from collections import defaultdict
from math import sqrt

# 重用 mlb_analyzer 的邏輯
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mlb_analyzer import (
    load_cached_games, build_current_trackers, load_baseline,
    build_elo_from_games, compute_today_series_context,
    MIN_GAMES, MIN_STARTS, COMPOSITES_DEF, TOTAL_COMPOSITES_DEF,
    HOME_ADV_BONUS, SERIES_LEAD_BONUS, TEAM_ZH,
    fetch_team_xstats,
)

ZH_TO_EN = {v: k for k, v in TEAM_ZH.items() if k != "Athletics"}
ZH_TO_EN["運動家"] = "Athletics"  # 用新版名 (2025+)


def parse_betting_log(path):
    """回傳 list of leg dicts."""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    legs = []
    entries = content.split('### ')
    for entry in entries[1:]:
        m = re.match(r'(\d{4}-\d{2}-\d{2}) 串\d關 \[(WIN|LOSE)\]', entry)
        if not m:
            continue
        bet_date = m.group(1)
        # 抓出所有 leg blocks
        leg_pat = re.compile(
            r'\*\*第(\d+)關\*\* (.+?) @ (.+?) \[(WIN|LOSE)\]\s*\n'
            r'- 玩法：(.+?)\n'
            r'- 賠率：([\d.]+)\n'
            r'- 比數：(\d+):(\d+)',
            re.MULTILINE
        )
        for lm in leg_pat.finditer(entry):
            n, away_zh, home_zh, result, play, odds, as_, hs = lm.groups()
            legs.append({
                'date': bet_date,
                'away_zh': away_zh.strip(),
                'home_zh': home_zh.strip(),
                'result': result,
                'play': play.strip(),
                'odds': float(odds),
                'away_score': int(as_),
                'home_score': int(hs),
            })
    return legs


def classify_play(play):
    """('不讓分 → 洋基', ...) → (bet_key, side)
    side: 'home'/'away'/'over'/'under'
    """
    play = play.strip()
    if play.startswith('不讓分'):
        team_zh = play.split('→')[1].strip()
        return ('ml', team_zh, 'team')
    if play.startswith('大小分'):
        m = re.match(r'大小分 ([\d.]+) → (大|小)', play)
        if m:
            line = float(m.group(1))
            side = 'over' if m.group(2) == '大' else 'under'
            return (f'total_{line}', side, 'total')
    if play.startswith('讓分'):
        m = re.match(r'讓分 (-?[\d.]+) → (.+)', play)
        if m:
            line = abs(float(m.group(1)))
            team_zh = m.group(2).strip()
            return (f'spread_{line}', team_zh, 'team')
    return None, None, None


def actual_winner(bet_key, away_score, home_score, away_en, home_en):
    """回傳該下注盤口的實際贏家方向 ('home'/'away'/'over'/'under')"""
    if bet_key == 'ml':
        return 'home' if home_score > away_score else 'away'
    if bet_key.startswith('total_'):
        line = float(bet_key.split('_')[1])
        return 'over' if (home_score + away_score) > line else 'under'
    if bet_key.startswith('spread_'):
        line = float(bet_key.split('_')[1])  # 0.5 increments, 不會 push
        # +line 的方向: 客 +line vs 主 -line. 主 cover 條件: home - away >= line+0.5
        return 'home' if (home_score - away_score) >= line + 0.5 else 'away'
    return None


def user_pick_side(bet_key, side_or_team, away_zh, home_zh):
    """把使用者的選擇 (可能是中文隊名或 over/under) 標準化為 'home'/'away'/'over'/'under'"""
    if bet_key.startswith('total_'):
        return side_or_team  # 'over' 或 'under'
    # ML / Spread: side_or_team 是中文隊名
    if side_or_team == away_zh:
        return 'away'
    if side_or_team == home_zh:
        return 'home'
    return None


def compute_directional_composite(m, components, use_home_adv, stat_means, stat_stds):
    """從 baseline 算 directional 複合分數 (跟 mlb_analyzer 一致)"""
    score = 0
    for stat_key, weight, source in components:
        if source == "elo":
            elo_diff_val = m.get('elo_diff', 0)
            elo_std = stat_stds.get(('elo', 'elo_diff'), 60) or 60
            score += (elo_diff_val / elo_std) * weight
            continue
        if source == "x":
            x_val = m.get(stat_key)
            if x_val is None:
                return None
            x_std = stat_stds.get(('x', stat_key), 0.02) or 0.02
            score += (x_val / x_std) * weight
            continue
        if source == "team":
            a_snap = m['away_snap']; h_snap = m['home_snap']
        else:
            a_snap = m.get('away_sp_snap'); h_snap = m.get('home_sp_snap')
            if a_snap is None or h_snap is None:
                return None
            if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                return None
        a_val = a_snap.get(stat_key); h_val = h_snap.get(stat_key)
        if a_val is None or h_val is None:
            return None
        std = stat_stds.get((source, stat_key), 1) or 1
        mean = stat_means.get((source, stat_key), 0)
        a_z = (a_val - mean) / std; h_z = (h_val - mean) / std
        score += (h_z - a_z) * weight
    if use_home_adv:
        score += HOME_ADV_BONUS
    si = m.get('series', {})
    if si.get('home_series_wins_before', 0) > si.get('away_series_wins_before', 0):
        score += SERIES_LEAD_BONUS
    return score


def compute_total_composite(m, components, stat_means, stat_stds):
    """從 baseline 算 total 複合分數"""
    score = 0
    for stat_key, weight, source in components:
        if source == "pf":
            pf_val = m.get('park_factor', 1.0)
            pf_mean = stat_means.get(('pf', 'park_factor'), 1.0)
            pf_std = stat_stds.get(('pf', 'park_factor'), 0.1) or 0.1
            z = (pf_val - pf_mean) / pf_std
            score += z * weight; continue
        if source == "x":
            x_val = m.get(stat_key)
            if x_val is None:
                return None
            x_std = stat_stds.get(('x', stat_key), 0.02) or 0.02
            score += (x_val / x_std) * weight; continue
        if source == "team":
            a_snap = m['away_snap']; h_snap = m['home_snap']
        else:
            a_snap = m.get('away_sp_snap'); h_snap = m.get('home_sp_snap')
            if a_snap is None or h_snap is None:
                return None
            if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                return None
        a_val = a_snap.get(stat_key); h_val = h_snap.get(stat_key)
        if a_val is None or h_val is None:
            return None
        combined = a_val + h_val
        mean_c = stat_means.get((source, stat_key), 0) * 2
        std_c = (stat_stds.get((source, stat_key), 1) or 1) * sqrt(2)
        z = (combined - mean_c) / std_c
        score += z * weight
    return score


def main(log_path):
    print(f"=== 回測 {log_path} ===\n")

    # 解析下注紀錄
    legs = parse_betting_log(log_path)
    print(f"解析到 {len(legs)} 個 legs")

    # 載入 baseline 和 cached games
    baseline = load_baseline()
    if not baseline:
        print("[X] 找不到 baseline. 請先跑 correlate range")
        return
    stat_means = baseline['stat_means']
    stat_stds = baseline['stat_stds']

    games = load_cached_games(2026)
    print(f"載入 {len(games)} 場 2026 比賽\n")

    # xStats (當前快取, 雖然點時間不準但比沒有好)
    xstats = fetch_team_xstats(2026)
    pf_2026 = {}
    # 簡化的 park_factor 計算 (重用 mlb_analyzer 邏輯太繁)
    from collections import defaultdict as dd
    venue_runs = dd(lambda: [0, 0])
    for g in games:
        venue_runs[g.home_name][0] += g.away_score + g.home_score
        venue_runs[g.home_name][1] += 1
    league_avg = sum(v[0] for v in venue_runs.values()) / max(1, sum(v[1] for v in venue_runs.values()))
    for team, (runs, gms) in venue_runs.items():
        if gms >= 5:
            pf_2026[team] = runs / gms / league_avg if league_avg > 0 else 1.0
    # 用 baseline park factors 蓋過
    pf_baseline = baseline.get('park_factors', {})
    park_factors = {**pf_2026, **pf_baseline}

    # 按日期分組 legs
    legs_by_date = defaultdict(list)
    for leg in legs:
        legs_by_date[leg['date']].append(leg)

    # 統計容器
    # indicator → {'predicted_total': N, 'predicted_correct': N}
    # 「指標預測正確」= 指標方向 = 真實結果方向
    indicator_stats = defaultdict(lambda: {'total': 0, 'correct': 0, 'category': 'ml'})

    # 也統計「使用者跟對指標 = 命中, 跟反 = 沒命中」
    user_align_stats = defaultdict(lambda: {'total': 0, 'aligned_won': 0, 'opposite_won': 0})

    # 跑每個日期
    elo = build_elo_from_games(games, until_date=min(legs_by_date.keys()))
    sorted_dates = sorted(legs_by_date.keys())

    for date in sorted_dates:
        # 重建 trackers (cutoff = 當天)
        team_trackers, pitcher_trackers = build_current_trackers(games, cutoff_date=date)

        # Elo: 每處理新一天前, 把前一天的所有比賽推進
        # build_elo_from_games 是從 0 跑到 until_date, 我們每次重建確保正確
        elo = build_elo_from_games(games, until_date=date)

        for leg in legs_by_date[date]:
            away_en = ZH_TO_EN.get(leg['away_zh'])
            home_en = ZH_TO_EN.get(leg['home_zh'])
            if not away_en or not home_en:
                continue
            if away_en not in team_trackers or home_en not in team_trackers:
                continue

            bet_key, side_raw, mode = classify_play(leg['play'])
            if not bet_key:
                continue
            user_side = user_pick_side(bet_key, side_raw, leg['away_zh'], leg['home_zh'])
            if user_side is None:
                continue
            actual = actual_winner(bet_key, leg['away_score'], leg['home_score'], away_en, home_en)
            if actual is None:
                continue

            # 取 snapshots
            away_snap = team_trackers[away_en].snapshot(date)
            home_snap = team_trackers[home_en].snapshot(date)
            if away_snap['games'] < MIN_GAMES or home_snap['games'] < MIN_GAMES:
                continue

            # 找比賽 / SP info
            game = next((g for g in games
                        if g.date == date and g.away_name == away_en and g.home_name == home_en), None)
            away_sp_snap = home_sp_snap = None
            if game:
                if game.away_starter.pitcher_id > 0 and game.away_starter.pitcher_id in pitcher_trackers:
                    away_sp_snap = pitcher_trackers[game.away_starter.pitcher_id].snapshot()
                if game.home_starter.pitcher_id > 0 and game.home_starter.pitcher_id in pitcher_trackers:
                    home_sp_snap = pitcher_trackers[game.home_starter.pitcher_id].snapshot()

            # Elo
            home_elo = elo.get(home_en)
            away_elo = elo.get(away_en)
            elo_diff = home_elo + elo.HFA - away_elo

            # 系列賽
            series_ctx = compute_today_series_context(games, away_en, home_en, date)
            series_dict = {
                'home_series_wins_before': series_ctx.get('home_series_wins_before', 0),
                'away_series_wins_before': series_ctx.get('away_series_wins_before', 0),
            }

            # xStats
            away_bat_x = xstats['batting'].get(away_en, {}).get('woba_diff')
            home_bat_x = xstats['batting'].get(home_en, {}).get('woba_diff')
            away_pit_x = xstats['pitching'].get(away_en, {}).get('woba_diff')
            home_pit_x = xstats['pitching'].get(home_en, {}).get('woba_diff')
            x_ml_edge = None
            x_total_signal = None
            if None not in (away_bat_x, home_bat_x, away_pit_x, home_pit_x):
                x_ml_edge = (home_bat_x - home_pit_x) - (away_bat_x - away_pit_x)
                x_total_signal = away_bat_x + home_bat_x - away_pit_x - home_pit_x

            # matchup dict (用 mlb_analyzer 內部複合函式吃的格式)
            m = {
                'away_snap': away_snap, 'home_snap': home_snap,
                'away_sp_snap': away_sp_snap, 'home_sp_snap': home_sp_snap,
                'park_factor': park_factors.get(home_en, 1.0),
                'series': series_dict,
                'elo_diff': elo_diff,
                'x_ml_edge': x_ml_edge,
                'x_total_signal': x_total_signal,
            }

            # ──────────────── 收集各指標預測 ────────────────
            preds = {}  # indicator_name → predicted_side

            if mode == 'team':  # ML or Spread
                # Elo
                if abs(elo_diff) >= 5:
                    preds['elo_advantage'] = 'home' if elo_diff > 0 else 'away'
                # home advantage 基線
                preds['home_adv'] = 'home'
                # 系列賽領先
                hw = series_dict['home_series_wins_before']; aw = series_dict['away_series_wins_before']
                if hw > aw:
                    preds['home_series_leading'] = 'home'
                elif aw > hw:
                    preds['away_series_leading'] = 'away'
                # 單一指標
                stat_predictors = [
                    ('win_pct', 'higher'), ('run_diff_per_game', 'higher'),
                    ('pyth_pct', 'higher'),
                    ('ops', 'higher'), ('era', 'lower'), ('whip', 'lower'), ('fip', 'lower'),
                    ('runs_per_game_recent10', 'higher'), ('ra_per_game_recent10', 'lower'),
                    ('ops_recent10', 'higher'), ('win_pct_recent10', 'higher'),
                    ('bp_era_14d', 'lower'), ('bp_whip_14d', 'lower'),
                ]
                for stat, direction in stat_predictors:
                    a = away_snap.get(stat); h = home_snap.get(stat)
                    if a is None or h is None or a == h:
                        continue
                    if direction == 'higher':
                        preds[stat] = 'home' if h > a else 'away'
                    else:
                        preds[stat] = 'home' if h < a else 'away'
                # SP stat
                if away_sp_snap and home_sp_snap and away_sp_snap.get('sp_starts', 0) >= MIN_STARTS \
                        and home_sp_snap.get('sp_starts', 0) >= MIN_STARTS:
                    for stat, direction in [('sp_era', 'lower'), ('sp_whip', 'lower'),
                                             ('sp_fip', 'lower'), ('sp_k_bb', 'higher')]:
                        a = away_sp_snap.get(stat); h = home_sp_snap.get(stat)
                        if a is None or h is None or a == h:
                            continue
                        if direction == 'higher':
                            preds[stat] = 'home' if h > a else 'away'
                        else:
                            preds[stat] = 'home' if h < a else 'away'
                # xStats
                if x_ml_edge is not None:
                    if abs(x_ml_edge) > 0.005:
                        preds['xstats_ml'] = 'home' if x_ml_edge > 0 else 'away'
                # 複合指標 (從 baseline COMPOSITES_DEF)
                for cname, comps, use_h in COMPOSITES_DEF:
                    if use_h and bet_key != 'ml':
                        continue
                    sc = compute_directional_composite(m, comps, use_h, stat_means, stat_stds)
                    if sc is None or sc == 0:
                        continue
                    preds[f'comp:{cname}'] = 'home' if sc > 0 else 'away'

            elif mode == 'total':
                line = float(bet_key.split('_')[1])
                # park factor
                pf = park_factors.get(home_en, 1.0)
                preds['park_factor_hi'] = 'over' if pf > 1.0 else 'under'
                # 天真 RPG sum
                est_rpg = away_snap.get('runs_per_game', 0) + home_snap.get('runs_per_game', 0)
                if abs(est_rpg - line) > 0.1:
                    preds['naive_rpg_sum'] = 'over' if est_rpg > line else 'under'
                # 近 10 場 RPG sum
                rpg_r10 = away_snap.get('runs_per_game_recent10', 0) + home_snap.get('runs_per_game_recent10', 0)
                if abs(rpg_r10 - line) > 0.1:
                    preds['rpg_r10_sum'] = 'over' if rpg_r10 > line else 'under'
                # team ERA sum (低 ERA → 偏小)
                era_sum = away_snap.get('era', 4.0) + home_snap.get('era', 4.0)
                preds['era_sum_hi'] = 'over' if era_sum > 8.0 else 'under'
                # ops_recent10 sum
                ops_r10_sum = away_snap.get('ops_recent10', 0) + home_snap.get('ops_recent10', 0)
                preds['ops_r10_sum_hi'] = 'over' if ops_r10_sum > 1.4 else 'under'
                # bp_era_14d 高 → 偏大
                bp_sum = away_snap.get('bp_era_14d', 4.0) + home_snap.get('bp_era_14d', 4.0)
                preds['bp_era_14d_sum_hi'] = 'over' if bp_sum > 8.0 else 'under'
                # SP 平均 ERA 低 → 偏小
                if away_sp_snap and home_sp_snap:
                    sp_era_avg = (away_sp_snap.get('sp_era', 4.0) + home_sp_snap.get('sp_era', 4.0)) / 2
                    if sp_era_avg > 0:
                        preds['sp_era_avg_hi'] = 'over' if sp_era_avg > 4.0 else 'under'
                # xStats total
                if x_total_signal is not None and abs(x_total_signal) > 0.01:
                    preds['xstats_total'] = 'over' if x_total_signal > 0 else 'under'
                # 複合指標 (從 baseline TOTAL_COMPOSITES_DEF)
                for cname, comps in TOTAL_COMPOSITES_DEF:
                    sc = compute_total_composite(m, comps, stat_means, stat_stds)
                    if sc is None or sc == 0:
                        continue
                    preds[f'comp:{cname}'] = 'over' if sc > 0 else 'under'

            # ──────────────── 統計 ────────────────
            # mode 標籤 (ml/total/spread)
            if bet_key == 'ml':
                cat = 'ml'
            elif bet_key.startswith('total_'):
                cat = 'total'
            else:
                cat = 'spread'

            for ind, pred in preds.items():
                key = (cat, ind)
                indicator_stats[key]['total'] += 1
                indicator_stats[key]['category'] = cat
                if pred == actual:
                    indicator_stats[key]['correct'] += 1

                # 使用者選擇 vs 指標
                ua_key = (cat, ind)
                if pred == user_side:
                    user_align_stats[ua_key]['total'] += 1
                    if leg['result'] == 'WIN':
                        user_align_stats[ua_key]['aligned_won'] += 1
                else:
                    # 跟反方向時, leg lose 反而是指標對
                    if leg['result'] == 'LOSE':
                        user_align_stats[ua_key]['opposite_won'] += 1

    # ──────────────── 輸出報告 ────────────────
    print("="*82)
    print("各指標命中率 — 預測方向 vs 比賽真實結果 (n >= 10)")
    print("="*82)

    for cat_filter, cat_name in [('ml', '不讓分'), ('total', '大小分'), ('spread', '讓分')]:
        rows = [(k[1], v['correct'], v['total'])
                for k, v in indicator_stats.items()
                if k[0] == cat_filter and v['total'] >= 10]
        if not rows:
            print(f"\n【{cat_name}】無足夠樣本\n")
            continue
        rows.sort(key=lambda x: -x[1]/max(1, x[2]))
        print(f"\n【{cat_name}】 (n={sum(r[2] for r in rows)//max(1,len(rows))} avg)")
        print(f"{'指標':<35s} {'命中':>6s} {'總數':>6s} {'命中率':>7s}  顯著")
        print("-" * 72)
        for ind, c, t in rows[:25]:
            pct = c / t * 100
            se = sqrt(0.5*0.5/t)
            z = (c/t - 0.5) / se if se > 0 else 0
            sig = '*' if abs(z) > 1.96 else ''
            star = '🏆' if pct >= 65 else ('★' if pct >= 60 else (' ' if pct >= 50 else '⚠️ '))
            print(f"{star}{ind:<33s} {c:>6d} {t:>6d}  {pct:>5.1f}%   {sig}")

    return indicator_stats, user_align_stats


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python -X utf8 backtest_user_bets.py <betting_log.md>")
        sys.exit(1)
    main(sys.argv[1])
