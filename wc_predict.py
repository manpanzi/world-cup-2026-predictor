#!/usr/bin/env python3
"""
World Cup 2026 Match Prediction & WeChat Work Alert System.
Uses ELO ratings + recent form to predict match outcomes,
handicap results, and likely scorelines.
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# Fix Windows console encoding for emoji output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------- config ----------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
MATCHES_FILE = os.path.join(DATA_DIR, "worldcup_raw.json")
ELO_FILE = os.path.join(DATA_DIR, "elo_ratings.json")
FORM_FILE = os.path.join(DATA_DIR, "recent_form.json")

WEBHOOK_URL = os.getenv(
    "WECHAT_WEBHOOK_URL",
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=bce95ffc-b9c3-482f-bd15-1c764f4c7892",
)

CST = timezone(timedelta(hours=8))

# ---------- data loading ----------


def load_data():
    with open(MATCHES_FILE, encoding="utf-8") as f:
        matches_data = json.load(f)
    with open(ELO_FILE, encoding="utf-8") as f:
        elo_data = json.load(f)
    with open(FORM_FILE, encoding="utf-8") as f:
        form_data = json.load(f)
    return matches_data["matches"], elo_data, form_data


def resolve_elo(name, elo_data):
    """Resolve team name to ELO rating, with name mapping."""
    name_map = elo_data.get("name_map", {})
    resolved = name_map.get(name, name)
    return elo_data["teams"].get(resolved)


def resolve_form(name, elo_data, form_data):
    """Resolve team name to form string."""
    name_map = elo_data.get("name_map", {})
    resolved = name_map.get(name, name)
    return form_data["forms"].get(resolved, "?-?-?-?-?")


# ---------- match filtering ----------


def get_tomorrow_matches_cst(all_matches):
    """Return matches scheduled for tomorrow in CST (Asia/Shanghai)."""
    now_cst = datetime.now(CST)
    tomorrow = (now_cst + timedelta(days=1)).date()

    result = []
    for m in all_matches:
        try:
            match_date = datetime.strptime(m["date"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        if match_date == tomorrow:
            result.append(m)
    return result


# ---------- prediction engine ----------


def elo_win_probability(elo_a, elo_b):
    """Expected win probability for team A based on ELO difference."""
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def calc_match_probabilities(elo_a, elo_b):
    """Return (win_A, draw, win_B) probabilities in [0,1]."""
    e_a = elo_win_probability(elo_a, elo_b)
    diff = abs(elo_a - elo_b)

    # Draw probability peaks at ~26% for equal teams, decays with ELO gap
    draw = 0.26 * math.exp(-((diff / 250.0) ** 2))

    win_a = e_a * (1.0 - draw)
    win_b = (1.0 - e_a) * (1.0 - draw)

    return win_a, draw, win_b


def calc_handicap(elo_a, elo_b, handicap):
    """
    Calculate handicap-adjusted win probability for team A.
    handicap is the line, e.g. -1.0 means team A gives 1 goal.
    Returns (cover_A, not_cover_A) where cover_A means A wins after handicap.
    """
    # Each goal ~ 80 ELO points
    elo_adj = handicap * 80
    p_cover = elo_win_probability(elo_a + elo_adj, elo_b)
    # Adjust for push (handicap is integer, push chance reduces cover)
    if handicap == int(handicap) and handicap != 0:
        # Estimate push probability
        goal_diff_elo = abs(elo_a - elo_b)
        push = 0.12 * math.exp(-((goal_diff_elo / 200.0) ** 2))
        p_cover = p_cover * (1.0 - push)
        return p_cover, 1.0 - p_cover - push, push
    return p_cover, 1.0 - p_cover, 0.0


def poisson_pmf(lmbda, k):
    """Poisson probability mass function."""
    if lmbda <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lmbda) * (lmbda ** k) / math.factorial(k)


def predict_scores(elo_a, elo_b):
    """
    Generate 2 most likely score predictions using Poisson model.
    Returns list of (score_a, score_b, probability) tuples.
    """
    avg_goals = 1.5  # baseline expected goals

    exp_g_a = avg_goals * math.exp((elo_a - elo_b) / 400.0 * 0.85)
    exp_g_b = avg_goals * math.exp((elo_b - elo_a) / 400.0 * 0.85)

    # Cap at reasonable values
    exp_g_a = max(0.3, min(exp_g_a, 5.0))
    exp_g_b = max(0.3, min(exp_g_b, 5.0))

    # Generate all score permutations 0-6
    scores = []
    for ga in range(0, 7):
        for gb in range(0, 7):
            prob = poisson_pmf(exp_g_a, ga) * poisson_pmf(exp_g_b, gb)
            scores.append((ga, gb, prob))

    scores.sort(key=lambda x: x[2], reverse=True)

    # Return top 2 unique scores (skip 0-0 if it's #1, it's boring)
    result = []
    for s in scores:
        if len(result) >= 2:
            break
        # skip redundant draws when there's a clear favorite
        if s[0] == s[1] and abs(elo_a - elo_b) > 200 and len(result) > 0:
            continue
        result.append(s)

    return result


def form_score(form_str):
    """
    Convert form string 'W-D-W-L-W' to a numeric momentum score.
    Returns value in [-5, 5] representing recent momentum.
    """
    points = {"W": 3, "w": 3, "D": 1, "d": 1, "L": 0, "l": 0}
    results = form_str.strip().split("-")
    if len(results) != 5:
        return 0.0

    # Weight: most recent game has highest weight
    weights = [0.35, 0.25, 0.20, 0.12, 0.08]
    score = 0
    for i, r in enumerate(results):
        p = points.get(r.strip(), 1)
        score += p * weights[i]

    # Normalize: 3.0 max (all wins) → 5, 0.0 min (all losses) → -5
    normalized = (score / 3.0) * 10 - 5
    return round(normalized, 1)


def form_to_display(form_str):
    """Convert W-D-L-W format to display-friendly string."""
    mapping = {"W": "胜", "D": "平", "L": "负",
               "w": "胜", "d": "平", "l": "负"}
    results = form_str.strip().split("-")
    return "".join(mapping.get(r.strip(), "?") for r in results)


def form_summary(form_str):
    """Return '近5场X胜Y平Z负' string."""
    results = form_str.strip().split("-")
    w = sum(1 for r in results if r.strip().upper() == "W")
    d = sum(1 for r in results if r.strip().upper() == "D")
    l_count = sum(1 for r in results if r.strip().upper() == "L")
    return f"近5场{w}胜{d}平{l_count}负"


# ---------- analysis pipeline ----------


def analyze_match(match, elo_data, form_data):
    """Full analysis for a single match."""
    team1 = match["team1"]
    team2 = match["team2"]

    elo1 = resolve_elo(team1, elo_data)
    elo2 = resolve_elo(team2, elo_data)

    if elo1 is None or elo2 is None:
        return {
            "skip": True,
            "team1": team1,
            "team2": team2,
            "reason": "TBD (knockout placeholder or unknown team)",
        }

    form1_str = resolve_form(team1, elo_data, form_data)
    form2_str = resolve_form(team2, elo_data, form_data)

    # Win/draw/loss
    w1, d, w2 = calc_match_probabilities(elo1, elo2)

    # Form momentum adjustment (±6% max)
    fs1 = form_score(form1_str)
    fs2 = form_score(form2_str)
    adj = (fs1 - fs2) * 0.012  # 6% max adjustment
    w1 = max(0.02, min(0.98, w1 + adj))
    w2 = max(0.02, min(0.98, w2 - adj))
    d = 1.0 - w1 - w2

    # Handicap suggestions
    handicap_lines = [-0.5, -1.0, -1.5, -2.0]
    hcaps = []
    for hcap in handicap_lines:
        cov, nc, push = calc_handicap(elo1, elo2, hcap)
        hcaps.append({"line": hcap, "cover": cov, "not_cover": nc, "push": push})

    # Best handicap line (closest to 50/50)
    best_hcap = min(hcaps, key=lambda h: abs(h["cover"] - 0.5))

    # Score predictions
    scores = predict_scores(elo1, elo2)

    # Determine favorite
    if w1 > w2 + 0.05:
        favorite = team1
        fav_pct = w1
    elif w2 > w1 + 0.05:
        favorite = team2
        fav_pct = w2
    else:
        favorite = None

    return {
        "skip": False,
        "team1": team1,
        "team2": team2,
        "elo1": elo1,
        "elo2": elo2,
        "group": match.get("group", ""),
        "date": match.get("date", ""),
        "time": match.get("time", ""),
        "ground": match.get("ground", ""),
        "win1": w1,
        "draw": d,
        "win2": w2,
        "handicaps": hcaps,
        "best_handicap": best_hcap,
        "scores": scores,
        "form1": form1_str,
        "form2": form2_str,
        "form1_score": fs1,
        "form2_score": fs2,
        "favorite": favorite,
        "fav_pct": fav_pct if favorite else 0,
    }


# ---------- WeChat formatting ----------

FLAGS = {
    "Mexico": "🇲🇽", "South Africa": "🇿🇦", "South Korea": "🇰🇷",
    "Czech Republic": "🇨🇿", "Canada": "🇨🇦", "Bosnia & Herzegovina": "🇧🇦",
    "Qatar": "🇶🇦", "Switzerland": "🇨🇭", "Brazil": "🇧🇷", "Morocco": "🇲🇦",
    "Haiti": "🇭🇹", "Scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "USA": "🇺🇸", "United States": "🇺🇸",
    "Turkey": "🇹🇷", "Australia": "🇦🇺", "Paraguay": "🇵🇾", "Germany": "🇩🇪",
    "Ecuador": "🇪🇨", "Ivory Coast": "🇨🇮", "Curacao": "🇨🇼",
    "Curaçao": "🇨🇼", "Netherlands": "🇳🇱", "Japan": "🇯🇵",
    "Sweden": "🇸🇪", "Tunisia": "🇹🇳", "Belgium": "🇧🇪", "Iran": "🇮🇷",
    "Egypt": "🇪🇬", "New Zealand": "🇳🇿", "Spain": "🇪🇸",
    "Uruguay": "🇺🇾", "Cape Verde": "🇨🇻", "Saudi Arabia": "🇸🇦",
    "France": "🇫🇷", "Norway": "🇳🇴", "Senegal": "🇸🇳", "Iraq": "🇮🇶",
    "Argentina": "🇦🇷", "Austria": "🇦🇹", "Jordan": "🇯🇴", "Algeria": "🇩🇿",
    "Portugal": "🇵🇹", "Colombia": "🇨🇴", "Uzbekistan": "🇺🇿",
    "DR Congo": "🇨🇩", "England": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "Croatia": "🇭🇷",
    "Panama": "🇵🇦", "Ghana": "🇬🇭",
}


def flag(team_name):
    return FLAGS.get(team_name, "")


def format_wechat(results, match_date_str):
    """Build WeChat Work markdown message."""
    lines = [
        f"# ⚽ 世界杯预测 | {match_date_str}比赛日",
        "",
    ]

    skipped = [r for r in results if r.get("skip")]
    analyzed = [r for r in results if not r.get("skip")]

    for r in analyzed:
        t1, t2 = r["team1"], r["team2"]
        group = r.get("group", "")
        time_str = r.get("time", "")

        w1 = r["win1"] * 100
        d = r["draw"] * 100
        w2 = r["win2"] * 100

        # Favorite indicator
        if r["favorite"] == t1:
            fav_line = f"{flag(t1)} **{t1}** (热门)"
        elif r["favorite"] == t2:
            fav_line = f"{flag(t2)} **{t2}** (热门)"
        else:
            fav_line = "势均力敌"

        # Build match header
        lines.append(
            f"## {flag(t1)} {t1} vs {flag(t2)} {t2}"
        )
        lines.append(f"> {group} | {time_str} | {r.get('ground', '')}")
        lines.append("")

        # Win/Draw/Loss
        lines.append(
            f"- 📊 **胜负**: {t1}胜 {w1:.0f}% / 平 {d:.0f}% / {t2}胜 {w2:.0f}%"
        )

        # Handicap
        bh = r["best_handicap"]
        if bh["push"] > 0.01:
            lines.append(
                f"- 🎯 **让球** ({t1}{bh['line']:+.1f}): "
                f"赢盘 {bh['cover']*100:.0f}% / 走水 {bh['push']*100:.0f}% "
                f"/ 输盘 {bh['not_cover']*100:.0f}%"
            )
        else:
            lines.append(
                f"- 🎯 **让球** ({t1}{bh['line']:+.1f}): "
                f"赢盘 {bh['cover']*100:.0f}% / 输盘 {bh['not_cover']*100:.0f}%"
            )

        # Scores
        score_strs = []
        for ga, gb, prob in r["scores"]:
            score_strs.append(f"{ga}-{gb} ({prob*100:.1f}%)")
        lines.append(f"- ⚽ **比分**: {' / '.join(score_strs)}")

        # Form
        fd1 = form_to_display(r["form1"])
        fd2 = form_to_display(r["form2"])
        fs1 = form_summary(r["form1"])
        fs2 = form_summary(r["form2"])
        lines.append(f"- 💡 **状态**: {t1} {fs1}({fd1}) | {t2} {fs2}({fd2})")

        # ELO comparison
        lines.append(f"- 📈 **ELO**: {t1} {r['elo1']} vs {t2} {r['elo2']} (差{r['elo1']-r['elo2']:+d})")

        lines.append("")

    if skipped:
        lines.append("---")
        lines.append("### ⏳ 待定场次")
        for s in skipped:
            lines.append(f"- {flag(s['team1'])} {s['team1']} vs {flag(s['team2'])} {s['team2']}: {s['reason']}")
        lines.append("")

    lines.append("---")
    lines.append("> 🤖 分析模型: ELO Ratings + Poisson分布 + 近期状态加权")
    lines.append(f"> 📊 数据来源: ESPN/DTAI | 仅供参考，不构成投注建议")

    return "\n".join(lines)


# ---------- WeChat send ----------


def send_wechat(content):
    """Push markdown to WeChat Work bot, auto-split on 4096 byte limit."""
    max_bytes = 3900

    if len(content.encode("utf-8")) <= max_bytes:
        return _do_send(content)

    # Split on match boundaries (## headers)
    sections = content.split("\n## ")
    sections[0] = sections[0]  # header stays as-is

    for i, sec in enumerate(sections):
        if i == 0:
            chunk = sec
        else:
            chunk = "## " + sec

        tag = f" ({i + 1}/{len(sections)})" if len(sections) > 1 else ""
        _do_send(chunk + tag)


def _do_send(content):
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        result = r.json()
        if result.get("errcode") != 0:
            print(f"  [WARN] WeChat API: {result}")
            return False
        print("  [OK] Sent to WeChat")
        return True
    except Exception as e:
        print(f"  [ERROR] WeChat send failed: {e}")
        return False


# ---------- main ----------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="World Cup 2026 Prediction Engine")
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: tomorrow CST)")
    parser.add_argument("--dry-run", action="store_true", help="Print only, don't send to WeChat")
    args = parser.parse_args()

    print("=" * 56)
    print("  ⚽ World Cup 2026 Prediction Engine")
    print("=" * 56)

    # Load data
    all_matches, elo_data, form_data = load_data()
    print(f"[数据] 加载 {len(all_matches)} 场比赛, "
          f"{len(elo_data['teams'])} 支球队ELO, "
          f"{len(form_data['forms'])} 条近期状态")

    # Determine target date
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        target_str = target_date.strftime("%m月%d日")
    else:
        now_cst = datetime.now(CST)
        target_date = (now_cst + timedelta(days=1)).date()
        target_str = target_date.strftime("%m月%d日")

    # Filter matches
    target_matches = [
        m for m in all_matches
        if m.get("date") == target_date.strftime("%Y-%m-%d")
    ]

    if not target_matches:
        print(f"\n[信息] {target_str} 无比赛安排，跳过")
        return

    print(f"\n[赛程] {target_str} 共 {len(target_matches)} 场比赛")

    # Analyze each match
    results = []
    for m in target_matches:
        t1, t2 = m["team1"], m["team2"]
        print(f"\n[分析] {t1} vs {t2}")
        result = analyze_match(m, elo_data, form_data)
        results.append(result)
        if result.get("skip"):
            print(f"  ⏭ {result['reason']}")
        else:
            print(f"  ELO: {result['elo1']} vs {result['elo2']}")
            print(f"  胜负: {result['win1']*100:.0f}% / "
                  f"{result['draw']*100:.0f}% / {result['win2']*100:.0f}%")
            score_strs = [f"{g[0]}-{g[1]}" for g in result["scores"]]
            print(f"  比分: {' / '.join(score_strs)}")

    # Format & send
    msg = format_wechat(results, target_str)
    print(f"\n{'=' * 56}")
    print(msg)
    print(f"{'=' * 56}")

    if args.dry_run:
        print("\n🔇 dry-run模式，跳过推送")
        return

    print("\n📤 发送到企业微信...")
    ok = send_wechat(msg)
    if ok:
        print("✅ 推送完成")
    else:
        print("❌ 推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
