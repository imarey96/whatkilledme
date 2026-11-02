"""Analyze a Blood DK tank on one fight, pull by pull (built for Mythic+).

Usage:
    python analyze_blood.py <report url or code> [--fight N] [--player NAME] [--compare N]

--fight can be left out if the URL has ?fight=N. --compare lines up the last
pull of this fight with the pull of the same name in another fight.

analyze_fight() returns plain data, which both this CLI and the web app use.
"""

import argparse
import os
import re
import sys
from collections import Counter, defaultdict

from main import extract_report_code, load_env
from wcl_client import WCLClient

# Fired automatically, so they don't count as casts
AUTO_PROCS = {"Infliction of Sorrow"}

DEFENSIVES = (
    "Dancing Rune Weapon", "Vampiric Blood", "Icebound Fortitude",
    "Anti-Magic Shell", "Death Pact", "Rune Tap", "Tombstone",
)
GROUP_CONTROL = ("Blinding Sleet", "Gorefiend's Grasp", "Asphyxiate")
TRACKED_BUFFS = ("Bone Shield", "Death and Decay", "Vampiric Blood", "Dancing Rune Weapon", "Blood Shield")
RP_SPENDERS_NEAR_CAP = ("Heart Strike", "Blood Boil", "Vampiric Strike")

LOW_HP_PCT = 30
LOW_HP_RESET_S = 8
RECAP_WINDOW_MS = 5000
COMPARE_WINDOW_S = 5
COMPARE_LENGTH_S = 40

# Fewer casts than this gives too few gaps to estimate a cooldown from
MIN_CASTS_FOR_ESTIMATE = 3

# Finding thresholds. These are rules of thumb, not class theory.
WIPE_WINDOW_S = 10
MANY_DEFENSIVES_DOWN = 3
MANY_CLOSE_CALLS = 10
GOOD_BONE_SHIELD_PCT = 95
GOOD_DND_PCT = 70
HIGH_NEAR_CAP_SHARE = 0.25
LOW_NEAR_CAP_SHARE = 0.15
COMPARE_DAMAGE_RATIO = 1.5
COMPARE_ATTACKS_TOLERANCE = 0.15


class AnalysisError(Exception):
    """A problem to show the user. Carries the roster when they need to pick a player."""

    def __init__(self, message: str, players: list[dict] | None = None):
        super().__init__(message)
        self.players = players


def fight_from_url(url: str) -> int | None:
    match = re.search(r"[?&#]fight=(\d+)", url)
    return int(match.group(1)) if match else None


def total_damage(e: dict) -> int:
    return e.get("amount", 0) + e.get("absorbed", 0)


def get_rp(e: dict) -> tuple[int | None, int | None]:
    """Runic Power and its max. Many casts don't carry it, so this can be None."""
    for r in e.get("classResources") or []:
        if r.get("type") == 6:
            return r.get("amount", 0) // 10, r.get("max", 0) // 10
    return None, None


def get_pulls(fight: dict) -> list[dict]:
    """Dungeon pulls, de-duplicated by ID. A non-M+ fight counts as one pull."""
    pulls = {p["id"]: p for p in fight.get("dungeonPulls") or []}
    if not pulls:
        return [{"id": 1, "name": fight["name"], "startTime": fight["startTime"], "endTime": fight["endTime"]}]
    return sorted(pulls.values(), key=lambda p: p["startTime"])


def get_roster(summary: dict) -> dict[int, dict]:
    """Players in this fight. Actor lists cover the whole report, which can span several groups."""
    roster = {}
    for role, players in summary.get("playerDetails", {}).items():
        for p in players:
            roster[p["id"]] = {"name": p["name"], "specs": p.get("specs") or [], "role": role}
    return roster


def pick_tank(roster: dict[int, dict], name: str | None) -> int:
    players = list(roster.values())
    if name:
        for pid, p in roster.items():
            if p["name"].lower() == name.lower():
                return pid
        raise AnalysisError(f"{name} is not in this fight.", players)
    tanks = [pid for pid, p in roster.items() if p["role"] == "tanks"]
    if len(tanks) != 1:
        raise AnalysisError(f"This fight has {len(tanks)} tanks. Pick a player.", players)
    return tanks[0]


def buff_uptime_pct(buffs: list[dict], name: str, ability_names: dict, pulls: list[dict], fight: dict) -> float:
    """Percent of combat time the buff was up, ignoring time between pulls."""
    stream = [e for e in buffs if ability_names.get(e.get("abilityGameID")) == name and e["type"] in ("applybuff", "removebuff")]
    intervals = []
    on = None
    for i, e in enumerate(stream):
        if e["type"] == "applybuff":
            on = on or e["timestamp"]
        else:
            # A removal with no apply means it was up before the fight started
            start = fight["startTime"] if i == 0 else on
            if start:
                intervals.append((start, e["timestamp"]))
            on = None
    if on:
        intervals.append((on, fight["endTime"]))

    combat_ms = sum(p["endTime"] - p["startTime"] for p in pulls)
    up_ms = sum(
        max(0, min(end, p["endTime"]) - max(start, p["startTime"]))
        for start, end in intervals for p in pulls
    )
    return up_ms / combat_ms * 100 if combat_ms else 0


def estimated_cooldowns(cast_times: dict[str, list[float]]) -> dict[str, float]:
    """Shortest gap between uses of each spell, in ms. Spells cast rarely get no estimate."""
    return {
        name: min(b - a for a, b in zip(times, times[1:]))
        for name, times in cast_times.items() if len(times) >= MIN_CASTS_FOR_ESTIMATE
    }


def cooldown_state(cast_times: dict[str, list[float]], cooldowns: dict[str, float], pull: dict, fight_start: float) -> dict:
    """Sort spells with an estimate into on cooldown at the pull, or ready but never pressed."""
    waiting, unused = [], []
    for name in cooldowns:
        times = cast_times[name]
        before = [t for t in times if t < pull["startTime"]]
        ready_at = before[-1] + cooldowns[name] if before else None
        if ready_at and ready_at > pull["startTime"]:
            waiting.append({"name": name, "ready_s": (ready_at - fight_start) / 1000})
        elif not any(pull["startTime"] <= t <= pull["endTime"] for t in times):
            unused.append(name)
    return {"waiting": waiting, "unused": unused}


def tank_windows(dmg: list[dict], heals: list[dict], start: float, length_s: int, step_s: int) -> list[dict]:
    """Damage, healing and swings on the tank in fixed windows from a start time."""
    rows = []
    for offset in range(0, length_s, step_s):
        a, b = start + offset * 1000, start + (offset + step_s) * 1000
        d = [e for e in dmg if a <= e["timestamp"] < b]
        rows.append({
            "offset": offset,
            "damage": sum(total_damage(e) for e in d),
            "healing": sum(e.get("amount", 0) for e in heals if a <= e["timestamp"] < b),
            "attacks": len(d),
        })
    return rows


def low_hp_episodes(dmg: list[dict]) -> list[tuple[float, float, dict]]:
    """First hit of each dip below LOW_HP_PCT. Avoided hits report 0 HP, so only real hits count."""
    episodes = []
    last = None
    for e in dmg:
        if total_damage(e) <= 0 or not e.get("maxHitPoints"):
            continue
        hp = e.get("hitPoints", 0) / e["maxHitPoints"] * 100
        if hp < LOW_HP_PCT and hp > 0 and (last is None or e["timestamp"] - last > LOW_HP_RESET_S * 1000):
            episodes.append((e["timestamp"], hp, e))
            last = e["timestamp"]
    return episodes


def load_fight(client: WCLClient, code: str, fight_id: int) -> dict:
    fight = client.get_fight_details(code, fight_id)
    start, end = fight["startTime"], fight["endTime"]
    summary = client.get_fight_summary(code, fight_id)
    damage_taken = client.get_events(code, fight_id, "DamageTaken", start, end)
    healing = client.get_events(code, fight_id, "Healing", start, end)
    return {
        "fight": fight,
        "roster": get_roster(summary),
        "pulls": get_pulls(fight),
        "damage_taken": [e for e in damage_taken if e.get("type") == "damage"],
        "healing": [e for e in healing if e.get("type") == "heal"],
        "deaths": client.get_events(code, fight_id, "Deaths", start, end),
    }


def build_comparison(other: dict, source_id: int, last_pull: dict, tank_dmg: list[dict], tank_heals: list[dict]) -> dict:
    other_fight_id = other["fight"]["id"]
    result = {"fight_id": other_fight_id, "pull": last_pull["name"], "windows": []}
    match = next((p for p in other["pulls"] if p["name"] == last_pull["name"]), None)
    if match is None:
        result["missing"] = f"Fight {other_fight_id} has no pull named {last_pull['name']}."
        return result
    if source_id not in other["roster"]:
        result["missing"] = f"This tank isn't in fight {other_fight_id}."
        return result
    other_dmg = [e for e in other["damage_taken"] if e.get("targetID") == source_id]
    other_heals = [e for e in other["healing"] if e.get("targetID") == source_id]
    here = tank_windows(tank_dmg, tank_heals, last_pull["startTime"], COMPARE_LENGTH_S, COMPARE_WINDOW_S)
    there = tank_windows(other_dmg, other_heals, match["startTime"], COMPARE_LENGTH_S, COMPARE_WINDOW_S)
    for a, b in zip(here, there):
        result["windows"].append({
            "offset": a["offset"],
            "damage": a["damage"], "other_damage": b["damage"],
            "healing": a["healing"], "other_healing": b["healing"],
            "attacks": a["attacks"], "other_attacks": b["attacks"],
        })
    return result


def build_findings(r: dict) -> list[dict]:
    """Turn the numbers into short, plain statements, most serious first."""
    findings = []

    def add(severity: str, title: str, detail: str):
        findings.append({"severity": severity, "title": title, "detail": detail})

    tank_name = r["tank"]["name"]
    tank_death = next((d for d in r["deaths"] if d["is_tank"]), None)
    if tank_death:
        pull = next((p for p in r["pulls"] if p["start_s"] <= tank_death["t_s"] <= p["end_s"]), r["pulls"][-1])
        into = tank_death["t_s"] - pull["start_s"]
        recap = ", ".join(f"{x['ability']} {x['amount']:,}" for x in tank_death["recap"][:3])
        add("critical", f"{tank_name} died {into:.0f}s into {pull['name']}",
            f"Killed by {tank_death['killed_by']}. Last 5s: {recap}.")

        down = [w for w in pull["on_cooldown"] if w["name"] in DEFENSIVES]
        if down:
            rest = f", {pull['rest_s']:.0f}s after the previous pull" if pull["rest_s"] is not None else ""
            names = ", ".join(f"{w['name']} (back ~{w['ready_s']:.0f}s)" for w in down)
            add("critical", f"{len(down)} defensive{'s' if len(down) != 1 else ''} on cooldown at the fatal pull",
                f"The pull started at {pull['start_s']:.0f}s{rest}. On cooldown: {names}.")
        if pull["ready_unused"]:
            add("warning", "Ready but not used on the fatal pull",
                ", ".join(pull["ready_unused"]) + " looked available and wasn't pressed.")

        after = [d for d in r["deaths"] if 0 <= d["t_s"] - tank_death["t_s"] <= WIPE_WINDOW_S]
        if len(after) == len(r["group"]) and len(after) > 1:
            add("critical", "The group wiped after the tank fell",
                f"All {len(after)} players died within {after[-1]['t_s'] - tank_death['t_s']:.1f}s.")

    fatal_pull_id = pull["id"] if tank_death else None
    risky = [p for p in r["pulls"]
             if p["id"] != fatal_pull_id and sum(w["name"] in DEFENSIVES for w in p["on_cooldown"]) >= MANY_DEFENSIVES_DOWN]
    for p in risky:
        add("warning", f"Pull {p['id']} started with most defensives on cooldown",
            f"{p['name']} at {p['start_s']:.0f}s: "
            + ", ".join(w["name"] for w in p["on_cooldown"] if w["name"] in DEFENSIVES) + ".")

    death_counts = Counter(d["name"] for d in r["deaths"] if tank_death is None or d["t_s"] < tank_death["t_s"])
    for name, count in death_counts.items():
        if count >= 2:
            times = ", ".join(f"{d['t_s']:.0f}s" for d in r["deaths"] if d["name"] == name)
            add("warning", f"{name} died repeatedly", f"Deaths at {times}.")

    if len(r["low_hp"]) >= MANY_CLOSE_CALLS:
        add("warning", f"{len(r['low_hp'])} close calls",
            f"{tank_name} dropped below {LOW_HP_PCT}% health that many times.")

    windows = (r.get("compare") or {}).get("windows", [])
    for w in windows:
        similar = w["attacks"] and abs(w["attacks"] - w["other_attacks"]) <= w["attacks"] * COMPARE_ATTACKS_TOLERANCE
        if similar and w["other_damage"] and w["damage"] / w["other_damage"] >= COMPARE_DAMAGE_RATIO:
            add("warning", f"Took {w['damage'] / w['other_damage']:.1f}x the damage from similar attacks",
                f"{w['offset']}-{w['offset'] + COMPARE_WINDOW_S}s into {r['compare']['pull']}: "
                f"{w['damage'] / 1e6:.2f}M vs {w['other_damage'] / 1e6:.2f}M in fight {r['compare']['fight_id']}. "
                "Check which defensive was up in the other fight.")
            break

    uptime = {u["name"]: u["pct"] for u in r["uptime"]}
    if uptime.get("Bone Shield", 0) >= GOOD_BONE_SHIELD_PCT:
        add("good", "Bone Shield stayed up", f"{uptime['Bone Shield']:.0f}% of combat.")
    if uptime.get("Death and Decay", 0) >= GOOD_DND_PCT:
        add("good", "Strong Death and Decay uptime", f"{uptime['Death and Decay']:.0f}% of combat.")

    rp = r["runic_power"]
    if rp["known"]:
        share = rp["near_cap"] / rp["known"]
        sample = f"{rp['near_cap']} of {rp['known']} rune spenders with data were within 10 of the cap."
        if share >= HIGH_NEAR_CAP_SHARE:
            add("warning", "Runic Power often near the cap", sample)
        elif share <= LOW_NEAR_CAP_SHARE:
            add("good", "Little Runic Power wasted", sample)

    order = {"critical": 0, "warning": 1, "good": 2}
    return sorted(findings, key=lambda f: order[f["severity"]])


def analyze_fight(client: WCLClient, code: str, fight_id: int, player: str | None = None, compare: int | None = None) -> dict:
    report = client.get_report_fights_and_players(code)
    fight_ids = {f["id"] for f in report.get("fights") or []}
    if fight_id not in fight_ids:
        raise AnalysisError(f"This report has no fight {fight_id}.")
    if compare is not None and compare not in fight_ids:
        raise AnalysisError(f"This report has no fight {compare} to compare with.")

    ability_names = {}
    for a in report.get("masterData", {}).get("abilities", []):
        if a.get("gameID") and a.get("name"):
            ability_names[a["gameID"]] = a["name"]

    actor_map = {}
    for a in report["masterData"]["actors"]:
        actor_map[a["id"]] = a.get("name") or f'Actor({a["id"]})'

    def ability(e: dict) -> str:
        return ability_names.get(e.get("abilityGameID"), f'Unknown({e.get("abilityGameID")})')

    # Check the player first, since the event fetches below are the expensive part
    source_id = pick_tank(get_roster(client.get_fight_summary(code, fight_id)), player)

    data = load_fight(client, code, fight_id)
    fight, roster, pulls = data["fight"], data["roster"], data["pulls"]
    fight_start, fight_end = fight["startTime"], fight["endTime"]

    def rel(ts: float) -> float:
        return (ts - fight_start) / 1000

    tank = roster[source_id]

    events = client.get_events(code, fight_id, "Casts", fight_start, fight_end, source_id=source_id)
    buffs = [
        e for e in client.get_events(code, fight_id, "Buffs", fight_start, fight_end, source_id=source_id)
        if e.get("targetID") == source_id
    ]
    interrupts = client.get_events(code, fight_id, "Interrupts", fight_start, fight_end)
    enemy_casts = client.get_events(code, fight_id, "Casts", fight_start, fight_end, hostility="Enemies")

    casts = [e for e in events if e.get("type") == "cast" and ability(e) not in AUTO_PROCS]
    tank_dmg = [e for e in data["damage_taken"] if e.get("targetID") == source_id]
    tank_heals = [e for e in data["healing"] if e.get("targetID") == source_id]
    combat_s = sum(p["endTime"] - p["startTime"] for p in pulls) / 1000
    per_min = 60 / combat_s if combat_s else 0

    # Cooldown estimates come from this fight's own casts, so they follow the player's talents
    cast_times = defaultdict(list)
    for e in casts:
        if ability(e) in DEFENSIVES + GROUP_CONTROL:
            cast_times[ability(e)].append(e["timestamp"])
    cooldowns = estimated_cooldowns(cast_times)

    pull_rows = []
    for i, p in enumerate(pulls):
        dur = (p["endTime"] - p["startTime"]) / 1000
        dmg = sum(total_damage(e) for e in tank_dmg if p["startTime"] <= e["timestamp"] <= p["endTime"])
        heal = sum(e.get("amount", 0) for e in tank_heals if p["startTime"] <= e["timestamp"] <= p["endTime"])
        state = cooldown_state(cast_times, cooldowns, p, fight_start)
        pull_rows.append({
            "id": p["id"],
            "name": p["name"],
            "start_s": rel(p["startTime"]),
            "end_s": rel(p["endTime"]),
            "rest_s": (p["startTime"] - pulls[i - 1]["endTime"]) / 1000 if i else None,
            "dtps": dmg / dur if dur else 0,
            "hps": heal / dur if dur else 0,
            "on_cooldown": state["waiting"],
            "ready_unused": state["unused"],
        })

    deaths = []
    for d in data["deaths"]:
        who = d.get("targetID")
        if who not in roster:
            continue
        t = d["timestamp"]
        recent = Counter()
        for e in data["damage_taken"]:
            if e.get("targetID") == who and t - RECAP_WINDOW_MS <= e["timestamp"] <= t:
                recent[ability(e)] += total_damage(e)
        deaths.append({
            "t_s": rel(t),
            "name": roster[who]["name"],
            "is_tank": who == source_id,
            "killed_by": ability_names.get(d.get("killingAbilityGameID"), "Unknown"),
            "recap": [{"ability": n, "amount": v} for n, v in recent.most_common(4)],
        })

    death_strikes = [get_rp(e)[0] for e in casts if ability(e) == "Death Strike" and get_rp(e)[0] is not None]
    near_cap = known = 0
    for e in casts:
        rp, rp_max = get_rp(e)
        if ability(e) in RP_SPENDERS_NEAR_CAP and rp is not None:
            known += 1
            near_cap += rp >= rp_max - 10

    by_ability = defaultdict(lambda: {"total": 0, "hits": 0, "max": 0})
    for e in tank_dmg:
        row = by_ability[ability(e)]
        row["total"] += total_damage(e)
        row["hits"] += 1
        row["max"] = max(row["max"], total_damage(e))
    grand_total = sum(r["total"] for r in by_ability.values()) or 1

    started = Counter(ability(e) for e in enemy_casts if e.get("type") == "begincast")
    finished = Counter(ability(e) for e in enemy_casts if e.get("type") == "cast")
    kicks = [e for e in interrupts if e.get("type") == "interrupt"]
    kicked = Counter(ability_names.get(e.get("extraAbilityGameID"), "?") for e in kicks)

    result = {
        "report": {"code": code, "title": report.get("title")},
        "fight": {
            "id": fight_id,
            "name": fight["name"],
            "keystone": fight.get("keystoneLevel"),
            "kill": bool(fight.get("kill")),
            "duration_s": (fight_end - fight_start) / 1000,
            "combat_s": combat_s,
        },
        "tank": {"name": tank["name"], "specs": tank["specs"]},
        "group": list(roster.values()),
        "warning": None if "Blood" in tank["specs"] else
            f"{tank['name']} is {'/'.join(tank['specs']) or 'an unknown spec'} here. These checks assume Blood.",
        "casts": {"total": len(casts), "cpm": len(casts) * per_min},
        "pulls": pull_rows,
        "deaths": deaths,
        "abilities": [
            {"name": n, "count": c, "per_min": c * per_min}
            for n, c in Counter(ability(e) for e in casts).most_common()
        ],
        "cooldowns": [
            {
                "name": n,
                "group": "control" if n in GROUP_CONTROL else "defensive",
                "casts": [rel(t) for t in cast_times[n]],
                "estimate_s": cooldowns[n] / 1000 if n in cooldowns else None,
            }
            for n in DEFENSIVES + GROUP_CONTROL if n in cast_times
        ],
        "uptime": [
            {"name": n, "pct": buff_uptime_pct(buffs, n, ability_names, pulls, fight)}
            for n in TRACKED_BUFFS
        ],
        "runic_power": {
            "death_strikes": len(death_strikes),
            "death_strike_avg": sum(death_strikes) / len(death_strikes) if death_strikes else None,
            "near_cap": near_cap,
            "known": known,
        },
        "low_hp": [
            {"t_s": rel(ts), "hp_pct": hp, "ability": ability(e), "source": actor_map.get(e.get("sourceID"), "?")}
            for ts, hp, e in low_hp_episodes(tank_dmg)
        ],
        "damage_taken": [
            {"name": n, "total": r["total"], "pct": r["total"] / grand_total * 100, "hits": r["hits"], "max": r["max"]}
            for n, r in sorted(by_ability.items(), key=lambda x: -x[1]["total"])[:12]
        ],
        "enemy_casts": [
            {"name": n, "started": c, "finished": finished[n], "kicked": kicked[n]}
            for n, c in started.most_common(12)
        ],
        "kicks": [
            {"name": actor_map.get(pid, "?"), "count": n}
            for pid, n in Counter(e.get("sourceID") for e in kicks).most_common()
        ],
        "compare": None,
    }

    if compare is not None:
        other = load_fight(client, code, compare)
        result["compare"] = build_comparison(other, source_id, pulls[-1], tank_dmg, tank_heals)

    result["findings"] = build_findings(result)
    return result


def print_report(r: dict):
    fight = r["fight"]
    if r["warning"]:
        print(f"WARNING: {r['warning']}")
        print()
    key = f" +{fight['keystone']}" if fight["keystone"] else ""
    result = "kill" if fight["kill"] else "not completed"
    print(f"Fight {fight['id']}: {fight['name']}{key} ({fight['duration_s']:.0f}s, {result})")
    print(f"Tank: {r['tank']['name']} ({'/'.join(r['tank']['specs'])})")
    print("Group: " + ", ".join(f"{p['name']} ({'/'.join(p['specs'])})" for p in r["group"]))
    print(f"Casts: {r['casts']['total']} | CPM in combat: {r['casts']['cpm']:.1f}")
    print()

    print("=== FINDINGS ===")
    for f in r["findings"]:
        print(f"  [{f['severity'].upper()}] {f['title']}")
        print(f"     {f['detail']}")
    print()

    print("=== PULLS ===")
    print(f"  {'#':>2}  {'Pull':<24} {'Time':>11} {'DTPS':>9} {'HPS':>9}")
    for p in r["pulls"]:
        span = f"{p['start_s']:.0f}-{p['end_s']:.0f}s"
        print(f"  {p['id']:>2}  {p['name'][:24]:<24} {span:>11} {p['dtps']:>9,.0f} {p['hps']:>9,.0f}")
    print()

    print("=== COOLDOWNS AT EACH PULL (estimated from shortest gap between uses) ===")
    for p in r["pulls"]:
        rest = f", {p['rest_s']:.0f}s after last pull" if p["rest_s"] is not None else ""
        print(f"  Pull {p['id']} {p['name']} at {p['start_s']:.0f}s{rest}")
        waiting = ", ".join(f"{w['name']} (~{w['ready_s']:.0f}s)" for w in p["on_cooldown"])
        print(f"     on cooldown: {waiting or 'none'}")
        if p["ready_unused"]:
            print(f"     ready, not used: {', '.join(p['ready_unused'])}")
    print()

    print("=== DEATHS ===")
    if not r["deaths"]:
        print("  None!")
    for d in r["deaths"]:
        print(f"  {d['t_s']:>6.1f}s  {d['name']} killed by {d['killed_by']}")
        print("          last 5s: " + ", ".join(f"{x['ability']} {x['amount']:,}" for x in d["recap"]))
    print()

    print("=== ABILITY USAGE ===")
    for a in r["abilities"]:
        print(f"  {a['name']:<40} {a['count']:>4}  {a['per_min']:>5.1f}/min")
    print()

    print("=== DEFENSIVE AND CONTROL TIMINGS ===")
    for c in r["cooldowns"]:
        print(f"  {c['name']:<24} {[round(t) for t in c['casts']]}")
    print()

    print("=== BUFF UPTIME (in combat) ===")
    for u in r["uptime"]:
        print(f"  {u['name']:<24} {u['pct']:>5.1f}%")
    print()

    print("=== RUNIC POWER ===")
    rp = r["runic_power"]
    if rp["death_strikes"]:
        print(f"  Death Strike: {rp['death_strikes']} casts, avg {rp['death_strike_avg']:.0f} RP")
    # Only a share of casts report RP, so this is a sample rather than a total
    print(f"  Rune spenders within 10 RP of cap: {rp['near_cap']} of {rp['known']} with RP data")
    print()

    print(f"=== HEALTH BELOW {LOW_HP_PCT}% ===")
    for h in r["low_hp"]:
        print(f"  {h['t_s']:>6.1f}s  {h['hp_pct']:>4.0f}%  {h['ability']} from {h['source']}")
    print()

    print("=== DAMAGE TAKEN BY TANK ===")
    for d in r["damage_taken"]:
        print(f"  {d['name']:<28} {d['total']:>14,} {d['pct']:>5.1f}%  hits {d['hits']:>4}  max {d['max']:>10,}")
    print()

    print("=== ENEMY CASTS (can't tell which are interruptible) ===")
    print(f"  {'Spell':<28} {'Started':>7} {'Finished':>8} {'Kicked':>6}")
    for e in r["enemy_casts"]:
        print(f"  {e['name']:<28} {e['started']:>7} {e['finished']:>8} {e['kicked']:>6}")
    print("  Kicks: " + ", ".join(f"{k['name']} {k['count']}" for k in r["kicks"]))

    cmp = r["compare"]
    if cmp:
        print()
        print(f"=== COMPARE: {cmp['pull']} in fight {fight['id']} vs fight {cmp['fight_id']} ===")
        if cmp.get("missing"):
            print(f"  {cmp['missing']}")
            return
        print(f"  {'Window':>8}  {'Damage':>8} {'Other':>8}  {'Healing':>8} {'Other':>8}  {'Attacks':>7} {'Other':>6}")
        for w in cmp["windows"]:
            span = f"{w['offset']}-{w['offset'] + COMPARE_WINDOW_S}s"
            print(f"  {span:>8}  {w['damage'] / 1e6:>7.2f}M {w['other_damage'] / 1e6:>7.2f}M  "
                  f"{w['healing'] / 1e6:>7.2f}M {w['other_healing'] / 1e6:>7.2f}M  {w['attacks']:>7} {w['other_attacks']:>6}")


def main():
    parser = argparse.ArgumentParser(description="Analyze a Blood DK tank on one fight.")
    parser.add_argument("report", help="report URL or code")
    parser.add_argument("--fight", type=int, help="fight ID (default: from the URL)")
    parser.add_argument("--player", help="player name (default: the fight's only tank)")
    parser.add_argument("--compare", type=int, help="another fight to compare the last pull against")
    args = parser.parse_args()

    sys.stdout.reconfigure(errors="replace")
    load_env()
    client = WCLClient(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"])

    code = extract_report_code(args.report)
    fight_id = args.fight or fight_from_url(args.report)
    if fight_id is None:
        print("Pick a fight with --fight:")
        for f in client.get_report_fights_and_players(code)["fights"]:
            print(f"  {f['id']:>3}  {f['name']}  ({(f['endTime'] - f['startTime']) / 1000:.0f}s)")
        sys.exit(1)

    try:
        result = analyze_fight(client, code, fight_id, args.player, args.compare)
    except AnalysisError as e:
        players = f" Players: {', '.join(p['name'] for p in e.players)}" if e.players else ""
        sys.exit(f"{e}{players}")
    print_report(result)


if __name__ == "__main__":
    main()
