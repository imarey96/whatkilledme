"""CLI for Warcraft Logs rotation comparison."""

import os
import re
import sys

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt
from rich.table import Table

from wcl_client import WCLClient
from analyzer import CastEvent, PlayerRotation, RotationComparison

console = Console()


def load_env():
    """Load .env file if it exists (simple key=value parser)."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


def extract_report_code(url_or_code: str) -> str:
    """Extract report code from a URL or return as-is if already a code."""
    match = re.search(r"warcraftlogs\.com/reports/((?:a:)?[A-Za-z0-9]+)", url_or_code)
    if match:
        return match.group(1)
    # Strip any URL fragments like #fight=...
    return url_or_code.split("#")[0].split("?")[0].strip()


def pick_fight(fights: list[dict]) -> dict:
    """Let user pick a fight from the list."""
    # Filter to boss encounters (encounterID > 0)
    boss_fights = [f for f in fights if f.get("encounterID", 0) > 0]
    display_fights = boss_fights if boss_fights else fights

    table = Table(title="Fights")
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Name", style="bold")
    table.add_column("Duration", justify="right")
    table.add_column("Result", justify="center")

    for i, fight in enumerate(display_fights):
        dur_s = (fight["endTime"] - fight["startTime"]) / 1000
        minutes, seconds = divmod(int(dur_s), 60)
        result = "[green]Kill[/green]" if fight.get("kill") else "[red]Wipe[/red]"
        table.add_row(str(i + 1), escape(fight["name"]), f"{minutes}:{seconds:02d}", result)

    console.print(table)
    choice = IntPrompt.ask("Select a fight", default=1)
    return display_fights[choice - 1]


def pick_players(actors: list[dict], count: int = 2) -> list[dict]:
    """Let user pick players from the roster."""
    table = Table(title="Players")
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Name", style="bold")
    table.add_column("Class/Spec")
    table.add_column("Server")

    for i, actor in enumerate(actors):
        table.add_row(str(i + 1), escape(actor.get("name") or "?"), escape(actor.get("subType") or "?"), escape(actor.get("server") or "?"))

    console.print(table)
    selected = []
    labels = ["your character", "the character to compare against"]
    for idx in range(count):
        choice = IntPrompt.ask(f"Select {labels[idx]}")
        selected.append(actors[choice - 1])
    return selected


def display_results(comparison: RotationComparison):
    """Print the full rotation comparison."""
    pa, pb = comparison.player_a, comparison.player_b

    # --- Summary ---
    summary = Table(title="Overview", show_header=True)
    summary.add_column("Metric")
    summary.add_column(escape(pa.name), justify="right", style="cyan")
    summary.add_column(escape(pb.name), justify="right", style="magenta")
    summary.add_row("Spec", escape(pa.spec), escape(pb.spec))
    summary.add_row("Total Casts", str(pa.total_casts), str(pb.total_casts))
    summary.add_row("Fight Duration (s)", f"{pa.fight_duration_s:.1f}", f"{pb.fight_duration_s:.1f}")
    summary.add_row("Casts per Minute", f"{pa.casts_per_minute:.1f}", f"{pb.casts_per_minute:.1f}")
    console.print(summary)
    console.print()

    # --- Ability Usage ---
    usage = Table(title="Ability Usage Comparison")
    usage.add_column("Ability", style="bold")
    usage.add_column(escape(pa.name), justify="right", style="cyan")
    usage.add_column(escape(pb.name), justify="right", style="magenta")
    usage.add_column("Diff", justify="right")

    for row in comparison.ability_usage_diff():
        diff = row["diff"]
        diff_str = str(diff)
        if diff > 0:
            diff_str = f"[green]+{diff}[/green]"
        elif diff < 0:
            diff_str = f"[red]{diff}[/red]"
        usage.add_row(escape(row["ability"]), str(row["a_count"]), str(row["b_count"]), diff_str)

    console.print(usage)
    console.print()

    # --- Opener ---
    opener_a, opener_b = comparison.opener_comparison(15)
    opener_table = Table(title="Opener (First 15 Casts)")
    opener_table.add_column("#", style="dim", justify="right")
    opener_table.add_column(escape(pa.name), style="cyan")
    opener_table.add_column(escape(pb.name), style="magenta")

    for i in range(max(len(opener_a), len(opener_b))):
        a_spell = escape(opener_a[i]) if i < len(opener_a) else ""
        b_spell = escape(opener_b[i]) if i < len(opener_b) else ""
        match = a_spell == b_spell
        a_cell = a_spell if match else f"[bold]{a_spell}[/bold]"
        b_cell = b_spell if match else f"[bold]{b_spell}[/bold]"
        opener_table.add_row(str(i + 1), a_cell, b_cell)

    console.print(opener_table)
    console.print()

    # --- Gap Analysis ---
    gaps_a, gaps_b = comparison.gap_analysis()
    for player, gaps in [(pa, gaps_a), (pb, gaps_b)]:
        if not gaps:
            continue
        gap_table = Table(title=f"Longest Gaps — {escape(player.name)}")
        gap_table.add_column("After", style="bold")
        gap_table.add_column("Before", style="bold")
        gap_table.add_column("Gap (s)", justify="right", style="red")
        gap_table.add_column("At (s into fight)", justify="right", style="dim")

        for g in gaps[:5]:
            gap_table.add_row(escape(g["after_ability"]), escape(g["before_ability"]), f"{g['gap_s']:.2f}", f"{g['timestamp_s']:.0f}")
        console.print(gap_table)
        console.print()

    # --- Timeline ---
    timeline = comparison.cast_timeline(window_s=30)
    if timeline:
        tl_table = Table(title="Cast Activity (30s windows)")
        tl_table.add_column("Window", justify="right")
        tl_table.add_column(escape(pa.name), justify="right", style="cyan")
        tl_table.add_column(escape(pb.name), justify="right", style="magenta")
        tl_table.add_column("Visual", min_width=40)

        for w in timeline:
            start = int(w["window_start_s"])
            bar_a = "[cyan]" + "|" * w["a_casts"] + "[/cyan]"
            bar_b = "[magenta]" + "|" * w["b_casts"] + "[/magenta]"
            tl_table.add_row(f"{start}s", str(w["a_casts"]), str(w["b_casts"]), f"{bar_a}\n{bar_b}")

        console.print(tl_table)


def main():
    load_env()

    client_id = os.environ.get("WCL_CLIENT_ID")
    client_secret = os.environ.get("WCL_CLIENT_SECRET")

    if not client_id or not client_secret:
        console.print("[red]Set WCL_CLIENT_ID and WCL_CLIENT_SECRET in .env or environment.[/red]")
        sys.exit(1)

    client = WCLClient(client_id, client_secret)

    console.print(Panel("[bold]Warcraft Logs Rotation Analyzer[/bold]", subtitle="v1.0"))

    # Step 1: Get report
    report_input = Prompt.ask("Enter report URL or code")
    report_code = extract_report_code(report_input)
    console.print(f"Using report code: [bold]{report_code}[/bold]")

    with console.status("Fetching report data..."):
        report = client.get_report_fights_and_players(report_code)

    console.print(f"[bold]{escape(report['title'])}[/bold]")
    console.print()

    # Step 2: Pick fight
    fight = pick_fight(report["fights"])
    console.print(f"Selected: [bold]{escape(fight['name'])}[/bold]")
    console.print()

    # Step 3: Pick two players
    actors = report["masterData"]["actors"]
    player_a_info, player_b_info = pick_players(actors)
    console.print(f"Comparing [cyan]{escape(player_a_info['name'])}[/cyan] vs [magenta]{escape(player_b_info['name'])}[/magenta]")
    console.print()

    # Build ability ID -> name map from report masterData
    ability_names = {}
    for ability in report.get("masterData", {}).get("abilities", []):
        if ability.get("gameID") and ability.get("name"):
            ability_names[ability["gameID"]] = ability["name"]

    # Step 4: Fetch cast events for both
    with console.status("Fetching cast events..."):
        events_a = client.get_cast_events(
            report_code, fight["id"], player_a_info["id"],
            fight["startTime"], fight["endTime"],
        )
        events_b = client.get_cast_events(
            report_code, fight["id"], player_b_info["id"],
            fight["startTime"], fight["endTime"],
        )

    rotation_a = PlayerRotation(
        name=player_a_info.get("name") or "Unknown",
        spec=player_a_info.get("subType") or "Unknown",
        casts=[CastEvent.from_raw(e, ability_names) for e in events_a],
    )
    rotation_b = PlayerRotation(
        name=player_b_info.get("name") or "Unknown",
        spec=player_b_info.get("subType") or "Unknown",
        casts=[CastEvent.from_raw(e, ability_names) for e in events_b],
    )

    console.print(f"Loaded [cyan]{len(events_a)}[/cyan] events for {escape(player_a_info['name'])}, "
                  f"[magenta]{len(events_b)}[/magenta] events for {escape(player_b_info['name'])}")
    console.print()

    # Step 5: Analyze and display
    comparison = RotationComparison(rotation_a, rotation_b)
    display_results(comparison)


if __name__ == "__main__":
    main()
