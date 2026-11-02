"""Analyze Steakdk on a single fight against the Unholy DK APL."""

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


def main():
    load_env()
    client = WCLClient(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"])

    report_code = "8dNHVQ97rhgTjwcY"
    fight_id = 32
    source_id = 14  # Steakdk

    report = client.get_report_fights_and_players(report_code)

    ability_names = {}
    for a in report.get("masterData", {}).get("abilities", []):
        if a.get("gameID") and a.get("name"):
            ability_names[a["gameID"]] = a["name"]

    actor_map = {}
    for a in report["masterData"]["actors"]:
        actor_map[a["id"]] = a.get("name") or f'Actor({a["id"]})'

    fight = [f for f in report["fights"] if f["id"] == fight_id][0]
    fight_start = fight["startTime"]
    fight_end = fight["endTime"]
    fight_dur_s = (fight_end - fight_start) / 1000

    print(f"Fight: {fight['name']} ({fight_dur_s:.0f}s)")
    print()

    events = client.get_cast_events(report_code, fight_id, source_id, fight_start, fight_end)
    dmg_events = client.get_damage_by_target(report_code, fight_id, source_id, fight_start, fight_end)

    # VP tick tracking
    vp_ids = [aid for aid, n in ability_names.items() if n == "Virulent Plague"]
    vp_ticks = sorted([e["timestamp"] for e in dmg_events if e.get("abilityGameID") in vp_ids])

    vp_total_downtime = 0
    vp_drops = []
    for i in range(1, len(vp_ticks)):
        gap_s = (vp_ticks[i] - vp_ticks[i - 1]) / 1000
        if gap_s > 6:
            at_s = (vp_ticks[i - 1] - fight_start) / 1000
            vp_drops.append((at_s, gap_s))
            vp_total_downtime += gap_s - 3

    if vp_ticks:
        vp_uptime_s = fight_dur_s - vp_total_downtime - (vp_ticks[0] - fight_start) / 1000
        vp_uptime_pct = max(0, min(100, vp_uptime_s / fight_dur_s * 100))
    else:
        vp_uptime_pct = 0

    auto_procs = {
        "Commander of the Dead", "Disease Cloud", "Charge!", "Light's Potential",
        "Sweeping Claws", "Shadow Bolt", "Infected Claws", "Frostbolt",
        "Pestilence", "Bombardments", "Ruptured Viscera", "Graveyard",
        "Coil of Devastation", "Undeath", "Trollbane's Icy Fury",
        "Virulent Plague", "Dread Plague", "Virulent Plague (Erupt)",
        "Dread Plague (Erupt)", "Melee",
    }

    player_casts = [
        e for e in events
        if ability_names.get(e.get("abilityGameID", 0), "") not in auto_procs
        and e.get("type") == "cast"
    ]

    total_casts = len(player_casts)
    if len(player_casts) >= 2:
        active_dur = (player_casts[-1]["timestamp"] - player_casts[0]["timestamp"]) / 1000
    else:
        active_dur = fight_dur_s
    cpm = total_casts / (active_dur / 60) if active_dur > 0 else 0

    print(f"Casts: {total_casts} | CPM: {cpm:.1f} | VP uptime: {vp_uptime_pct:.1f}%")
    print()

    # Ability counts
    ability_counts = Counter()
    for e in player_casts:
        name = ability_names.get(e.get("abilityGameID", 0), "?")
        ability_counts[name] += 1

    print("=== ABILITY USAGE ===")
    for name, count in ability_counts.most_common():
        print(f"  {name:<35} {count:>4}")
    print()

    # Track issues
    dt_active_until = None
    issues = Counter()
    issue_details = {
        "rp_cap": [],
        "sr_early": [],
        "outbreak_waste": [],
        "utility": [],
        "death_coil_low_rp": [],
    }

    last_army = None

    for e in player_casts:
        t_ms = e["timestamp"]
        t = (t_ms - fight_start) / 1000
        aid = e.get("abilityGameID", 0)
        name = ability_names.get(aid, f"Unknown({aid})")
        target = actor_map.get(e.get("targetID", 0), "AoE")

        runes = None
        rp = None
        for r in e.get("classResources", []):
            if r.get("type") == 5:
                runes = r.get("amount")
            elif r.get("type") == 6:
                rp = r.get("amount", 0) // 10

        if name == "Dark Transformation":
            dt_active_until = t_ms + 20000
        if name == "Army of the Dead":
            last_army = t_ms

        # RP capping: using rune spender at RP >= 90
        if name in ("Scourge Strike", "Festering Strike", "Festering Scythe", "Putrefy") and rp is not None and rp >= 90:
            issues["RP capped (>=90) on rune spender"] += 1
            issue_details["rp_cap"].append(f"  {t:>6.1f}s  {name} at RP={rp} (runes={runes})")

        # Soul Reaper too early in DT
        if name == "Soul Reaper" and dt_active_until and t_ms < dt_active_until:
            dt_remaining = (dt_active_until - t_ms) / 1000
            if dt_remaining > 8:
                issues["Soul Reaper too early in DT"] += 1
                issue_details["sr_early"].append(f"  {t:>6.1f}s  Soul Reaper with {dt_remaining:.0f}s left on DT")

        # Utility GCDs
        if name in ("Death Grip", "Death Strike", "Raise Ally", "Path of Frost"):
            issues["Non-damage utility GCD"] += 1
            issue_details["utility"].append(f"  {t:>6.1f}s  {name}")

        # Outbreak waste
        if name == "Outbreak":
            recent_vp = [tick for tick in vp_ticks if tick < t_ms and t_ms - tick < 10000]
            if recent_vp and (t_ms - recent_vp[-1]) / 1000 < 3:
                issues["Outbreak while VP still healthy"] += 1
                last_tick_ago = (t_ms - recent_vp[-1]) / 1000
                issue_details["outbreak_waste"].append(
                    f"  {t:>6.1f}s  Outbreak (last VP tick {last_tick_ago:.1f}s ago)"
                )

        # Death Coil at low RP without Sudden Doom (can't detect SD, but flag very low RP)
        if name in ("Death Coil", "Necrotic Coil") and rp is not None and rp < 40 and runes is not None and runes >= 3:
            issues["Death Coil at low RP with runes available"] += 1
            issue_details["death_coil_low_rp"].append(
                f"  {t:>6.1f}s  {name} at RP={rp} with {runes} runes (Sudden Doom proc?)"
            )

    # Print results
    print("=== VP DROPS ===")
    if vp_drops:
        for at_s, gap_s in vp_drops:
            print(f"  {gap_s:.1f}s gap starting at {at_s:.0f}s")
    else:
        print("  None!")
    print()

    print("=== ISSUES ===")
    for issue, count in sorted(issues.items(), key=lambda x: -x[1]):
        severity = "!!!" if count >= 5 else "! " if count >= 3 else "  "
        print(f"  {severity} {issue}: {count}x")
    print()

    for label, key in [
        ("RP CAPPING", "rp_cap"),
        ("SOUL REAPER TIMING", "sr_early"),
        ("OUTBREAK WASTE", "outbreak_waste"),
        ("UTILITY GCDS", "utility"),
        ("DEATH COIL AT LOW RP", "death_coil_low_rp"),
    ]:
        details = issue_details[key]
        if details:
            print(f"--- {label} ---")
            for d in details:
                print(d)
            print()

    # Opener
    print("=== OPENER (first 20 casts with resources) ===")
    for i, e in enumerate(player_casts[:20]):
        t = (e["timestamp"] - fight_start) / 1000
        name = ability_names.get(e.get("abilityGameID", 0), "?")
        target = actor_map.get(e.get("targetID", 0), "AoE")
        runes = None
        rp = None
        for r in e.get("classResources", []):
            if r.get("type") == 5:
                runes = r.get("amount")
            elif r.get("type") == 6:
                rp = r.get("amount", 0) // 10
        print(f"  {i+1:>2}. {t:>5.1f}s  {name:<30} -> {target:<25} runes={runes} rp={rp}")

    # Timeline
    print()
    print("=== TIMELINE (30s windows) ===")
    window = 30
    for start_s in range(0, int(fight_dur_s) + 1, window):
        end_s = start_s + window
        count = sum(
            1 for e in player_casts
            if start_s <= (e["timestamp"] - fight_start) / 1000 < end_s
        )
        bar = "|" * count
        print(f"  {start_s:>4}s: {count:>3} {bar}")


if __name__ == "__main__":
    main()
