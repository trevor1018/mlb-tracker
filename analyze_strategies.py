"""
MLB 投注策略回測腳本
用法: python analyze_strategies.py <報告檔案路徑>
範例: python analyze_strategies.py C:/Users/user/Downloads/mlb-report-2026-04-10.md
"""
import re, sys
from math import comb

if len(sys.argv) < 2:
    print("用法: python analyze_strategies.py <報告MD檔路徑>")
    sys.exit(1)

filepath = sys.argv[1]
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# === 解析所有賽果 ===
game_pattern = re.compile(
    r'### (\d{4}-\d{2}-\d{2}) (.+?) \(.+?\) @ (.+?) \(.+?\)\s*\n\s*\n\*\*(\d+) : (\d+)\*\*.*?勝：(.+)',
    re.MULTILINE
)

games = []
for m in game_pattern.finditer(content):
    games.append({
        'date': m.group(1),
        'away': m.group(2).strip(),
        'home': m.group(3).strip(),
        'away_score': int(m.group(4)),
        'home_score': int(m.group(5)),
        'winner': m.group(6).strip()
    })

games.sort(key=lambda x: x['date'])
print(f'=== MLB 策略回測 ===')
print(f'資料: {len(games)} 場 ({games[0]["date"]} ~ {games[-1]["date"]})')

games_by_date = {}
for g in games:
    games_by_date.setdefault(g['date'], []).append(g)

# === 逐日追蹤勝率並模擬 ===
records = {}
results_home = []
results_better = []
results_60 = []

for date in sorted(games_by_date.keys()):
    day_home, day_better, day_60 = [], [], []

    for g in games_by_date[date]:
        away, home, winner = g['away'], g['home'], g['winner']
        records.setdefault(away, {'w': 0, 'l': 0})
        records.setdefault(home, {'w': 0, 'l': 0})

        aw, al = records[away]['w'], records[away]['l']
        hw, hl = records[home]['w'], records[home]['l']
        a_games, h_games = aw + al, hw + hl
        a_pct = aw / a_games if a_games > 0 else 0.5
        h_pct = hw / h_games if h_games > 0 else 0.5

        # 主場策略
        day_home.append({'won': winner == home})

        # 策略一：押勝率較高的隊
        if a_games > 0 or h_games > 0:
            if a_pct > h_pct:
                bt, bp = away, a_pct
            else:
                bt, bp = home, h_pct
            day_better.append({'team': bt, 'pct': bp, 'won': winner == bt})

        # 策略二：只押勝率 > 60%（至少打 3 場）
        for team, pct, tg in [(away, a_pct, a_games), (home, h_pct, h_games)]:
            if tg >= 3 and pct > 0.6:
                day_60.append({'team': team, 'pct': pct, 'won': winner == team})

        # 更新戰績
        if winner == away:
            records[away]['w'] += 1
            records[home]['l'] += 1
        else:
            records[home]['w'] += 1
            records[away]['l'] += 1

    results_home.append((date, day_home))
    results_better.append((date, day_better))
    results_60.append((date, day_60))


def calc_stats(results, label, est_odds_range):
    """計算單場命中率、串2關命中率、模擬ROI"""
    total_p = sum(len(dr) for _, dr in results)
    total_w = sum(sum(1 for r in dr if r['won']) for _, dr in results)

    if total_p == 0:
        print(f'\n{label}: 無符合條件的場次')
        return

    single_rate = total_w / total_p

    tc, twc = 0, 0
    for _, dr in results:
        n = len(dr)
        if n < 2:
            continue
        h = sum(1 for r in dr if r['won'])
        c = comb(n, 2)
        wc = comb(h, 2) if h >= 2 else 0
        tc += c
        twc += wc

    parlay_rate = twc / tc if tc > 0 else 0
    avail_days = sum(1 for _, dr in results if len(dr) >= 2)

    print(f'\n--- {label} ---')
    print(f'單場命中率: {total_w}/{total_p} = {single_rate*100:.1f}%')
    if tc > 0:
        print(f'串2關命中率: {twc}/{tc} = {parlay_rate*100:.1f}%')
        print(f'損益平衡合計賠率: {1/parlay_rate:.2f}')
        print(f'可下注天數: {avail_days}')
        print(f'模擬 ROI:')
        for odds in est_odds_range:
            combined = odds ** 2
            roi = (parlay_rate * combined - 1) * 100
            marker = ' <-- 最可能' if odds == est_odds_range[len(est_odds_range)//2] else ''
            print(f'  賠率 {odds:.2f} -> 合計 {combined:.2f} -> ROI: {roi:+.1f}%{marker}')

    return single_rate, parlay_rate


print('\n' + '='*50)
calc_stats(results_home, '盲押主場', [1.60, 1.65, 1.70, 1.75])
calc_stats(results_better, '押勝率較高的隊', [1.45, 1.50, 1.55, 1.60])
calc_stats(results_60, '只押勝率>60%的隊', [1.30, 1.35, 1.40, 1.45, 1.50])

# === 最終比較表 ===
print('\n' + '='*50)
print('=== 策略比較總表 ===')
print('='*50)
print(f'{"策略":<20s} | 單場命中 | 串2關命中 | 估計ROI')
print('-'*60)

for results, label, mid_odds in [
    (results_home, '盲押主場', 1.67),
    (results_better, '押高勝率隊', 1.53),
    (results_60, '只押>60%隊', 1.40),
]:
    tp = sum(len(dr) for _, dr in results)
    tw = sum(sum(1 for r in dr if r['won']) for _, dr in results)
    sr = tw / tp if tp > 0 else 0

    tc, twc = 0, 0
    for _, dr in results:
        n = len(dr)
        if n < 2: continue
        h = sum(1 for r in dr if r['won'])
        tc += comb(n, 2)
        twc += comb(h, 2) if h >= 2 else 0

    pr = twc / tc if tc > 0 else 0
    roi = (pr * mid_odds**2 - 1) * 100
    print(f'{label:<20s} | {sr*100:5.1f}%   | {pr*100:5.1f}%    | {roi:+.1f}%')
