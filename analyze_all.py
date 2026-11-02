"""Analyze all Steakdk kills against the Unholy DK APL."""

import os
from collections import Counter
from wcl_client import WCLClient

def load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

def analyze_fight(client, report_code, report, fight, source_id, ability_names, actor_map):
    fight_start = fight["startTime"]
    fight_end = fight["endTime"]
    fight_dur_s = (fight_end - fight_start) / 1000

    events = client.get_cast_events(report_code, fight["id"], source_id, fight_start, fight_end)
    dmg_events = client.get_damage_by_target(report_code, fight["id"], source_id, fight_start, fight_end)

    # Find boss IDs (NPCs that aren't pets/environment)
    boss_ids = set()
    for a in report["masterData"]["actors"]:
        if a.get("type") == "NPC" and a.get("subType") not in (None, "Unknown"):
            boss_ids.add(a["id"])
    # Fallback: any NPC that took significant damage
    if not boss_ids:
        from collections import defaultdict
        target_dmg = defaultdict(int)
        for e in dmg_events:
            target_dmg[e.get("targetID", 0)] += e.get("amount", 0)
        for tid, dmg in target_dmg.items():
            if dmg > 100000:
                boss_ids.add(tid)

    # Get VP tick times on any boss target
    vp_ids = [aid for aid, n in ability_names.items() if n == "Virulent Plague"]
    vp_ticks = sorted([e["timestamp"] for e in dmg_events if e.get("abilityGameID") in vp_ids and e.get("targetID") in boss_ids])

    auto_procs = {"Commander of the Dead", "Disease Cloud", "Charge!", "Light's Potential",
                  "Sweeping Claws", "Shadow Bolt", "Infected Claws", "Frostbolt",
                  "Pestilence", "Bombardments", "Ruptured Viscera", "Graveyard",
                  "Coil of Devastation", "Undeath", "Trollbane's Icy Fury",
                  "Virulent Plague", "Dread Plague", "Virulent Plague (Erupt)",
                  "Dread Plague (Erupt)"}

    # Track state
    dt_active_until = None
    issues = Counter()
    issue_details = []

    player_casts = [e for e in events if ability_names.get(e.get("abilityGameID", 0), "") not in auto_procs and e.get("type") == "cast"]

    total_casts = len(player_casts)
    fight_dur_active = (player_casts[-1]["timestamp"] - player_casts[0]["timestamp"]) / 1000 if len(player_casts) >= 2 else fight_dur_s
    cpm = total_casts / (fight_dur_active / 60) if fight_dur_active > 0 else 0

    for e in player_casts:
        t_ms = e["timestamp"]
        t = (t_ms - fight_start) / 1000
        aid = e.get("abilityGameID", 0)
        name = ability_names.get(aid, f"Unknown({aid})")

        runes = None
        rp = None
        for r in e.get("classResources", []):
            if r.get("type") == 5:
                runes = r.get("amount")
            elif r.get("type") == 6:
                rp = r.get("amount", 0) // 10

        if name == "Dark Transformation":
            dt_active_until = t_ms + 20000

        # CHECK: RP capping
        if name in ("Scourge Strike", "Festering Strike", "Festering Scythe", "Putrefy") and rp is not None and rp >= 90:
            issues["RP capped (>=90) on rune spender"] += 1
            issue_details.append(f"  {t:>6.1f}s  {name} at RP={rp}")

        # CHECK: Soul Reaper timing
        if name == "Soul Reaper" and dt_active_until and t_ms < dt_active_until:
            dt_remaining = (dt_active_until - t_ms) / 1000
            if dt_remaining > 8:
                issues["Soul Reaper too early in DT window"] += 1

        # CHECK: Utility GCDs
        if name in ("Death Grip", "Death Strike", "Raise Ally", "Path of Frost"):
            issues["Non-damage utility GCD"] += 1

        # CHECK: Outbreak too early (VP still healthy)
        if name == "Outbreak":
            recent_vp = [tick for tick in vp_ticks if tick < t_ms and t_ms - tick < 10000]
            if recent_vp and (t_ms - recent_vp[-1]) / 1000 < 3:
                issues["Outbreak while VP still healthy"] += 1

    # CHECK: VP drops
    vp_drops = 0
    vp_total_downtime = 0
    for i in range(1, len(vp_ticks)):
        gap_s = (vp_ticks[i] - vp_ticks[i - 1]) / 1000
        if gap_s > 6:
            vp_drops += 1
            vp_total_downtime += gap_s - 3  # subtract one tick interval
    issues["Virulent Plague drops"] = vp_drops

    # Estimate VP uptime
    if vp_ticks:
        vp_uptime_s = fight_dur_s - vp_total_downtime - (vp_ticks[0] - fight_start) / 1000
        vp_uptime_pct = max(0, min(100, vp_uptime_s / fight_dur_s * 100))
    else:
        vp_uptime_pct = 0

    # Count ability usage
    ability_counts = Counter(ability_names.get(e.get("abilityGameID", 0), "?") for e in player_casts)

    return {
        "fight_name": fight["name"],
        "fight_dur_s": fight_dur_s,
        "total_casts": total_casts,
        "cpm": cpm,
        "vp_uptime_pct": vp_uptime_pct,
        "vp_drops": vp_drops,
        "issues": issues,
        "issue_details": issue_details,
        "ability_counts": ability_counts,
    }


def main():
    load_env()
    client = WCLClient(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"])
    report_code = "x6vLW9fnmj74yBHp"
    report = client.get_report_fights_and_players(report_code)

    ability_names = {}
    for a in report.get("masterData", {}).get("abilities", []):
        if a.get("gameID") and a.get("name"):
            ability_names[a["gameID"]] = a["name"]

    actor_map = {}
    for a in report["masterData"]["actors"]:
        actor_map[a["id"]] = a.get("name") or f'Actor({a["id"]})'

    # Find Steakdk
    steakdk_id = None
    for a in report["masterData"]["actors"]:
        if a.get("name") == "Steakdk":
            steakdk_id = a["id"]
            break

    if not steakdk_id:
        print("Steakdk not found in this report!")
        return

    kills = [f for f in report["fights"] if f.get("encounterID", 0) > 0 and f.get("kill")]
    print(f"Found {len(kills)} kills. Steakdk = sourceID {steakdk_id}")
    print()

    all_results = []
    for fight in kills:
        print(f"Analyzing {fight['name']} (Fight #{fight['id']})...")
        result = analyze_fight(client, report_code, report, fight, steakdk_id, ability_names, actor_map)
        all_results.append(result)

    # Print results
    print()
    print("=" * 100)
    print(f"{'Fight':<30} {'Dur':>5} {'Casts':>6} {'CPM':>6} {'VP%':>6} {'VPdrop':>7} {'RPcap':>6} {'SRbad':>6} {'Util':>5} {'OBwst':>6}")
    print("-" * 100)

    totals = Counter()
    for r in all_results:
        issues = r["issues"]
        print(f"{r['fight_name']:<30} {r['fight_dur_s']:>5.0f} {r['total_casts']:>6} {r['cpm']:>6.1f} {r['vp_uptime_pct']:>5.1f}% {issues['Virulent Plague drops']:>7} {issues['RP capped (>=90) on rune spender']:>6} {issues['Soul Reaper too early in DT window']:>6} {issues['Non-damage utility GCD']:>5} {issues['Outbreak while VP still healthy']:>6}")
        for k, v in issues.items():
            totals[k] += v

    print("-" * 100)
    print(f"{'TOTALS':<30} {'':>5} {'':>6} {'':>6} {'':>6} {totals['Virulent Plague drops']:>7} {totals['RP capped (>=90) on rune spender']:>6} {totals['Soul Reaper too early in DT window']:>6} {totals['Non-damage utility GCD']:>5} {totals['Outbreak while VP still healthy']:>6}")

    # Detailed RP cap instances
    print()
    print("=" * 100)
    print("DETAILED RP CAP INSTANCES (rune spender at RP >= 90)")
    print("-" * 100)
    for r in all_results:
        if r["issue_details"]:
            print(f"\n--- {r['fight_name']} ---")
            for d in r["issue_details"]:
                print(d)

    # Per-fight breakdown
    print()
    print("=" * 100)
    print("PER-FIGHT ISSUE SUMMARY")
    print("=" * 100)
    for r in all_results:
        print(f"\n{'='*60}")
        print(f"  {r['fight_name']} ({r['fight_dur_s']:.0f}s)")
        print(f"  {r['total_casts']} casts | {r['cpm']:.1f} CPM | VP uptime: {r['vp_uptime_pct']:.1f}%")
        print(f"{'='*60}")
        if not r["issues"]:
            print("  No issues detected!")
        for issue, count in r["issues"].most_common():
            severity = "!!!" if count >= 5 else "! " if count >= 3 else "  "
            print(f"  {severity} {issue}: {count}x")


if __name__ == "__main__":
    main()
