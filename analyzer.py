"""Rotation analysis engine — compares cast sequences between two players."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field


@dataclass
class CastEvent:
    timestamp: float
    ability_id: int
    ability_name: str
    event_type: str  # "cast" or "begincast"

    @classmethod
    def from_raw(cls, raw: dict, ability_names: dict[int, str] | None = None) -> "CastEvent":
        ability_id = raw.get("abilityGameID", 0)
        # Try embedded name first, then lookup map, then fallback
        name = (raw.get("ability") or {}).get("name")
        if not name and ability_names:
            name = ability_names.get(ability_id)
        if not name:
            name = f"Unknown({ability_id})"
        return cls(
            timestamp=raw["timestamp"],
            ability_id=ability_id,
            ability_name=name,
            event_type=raw.get("type", "cast"),
        )


@dataclass
class PlayerRotation:
    name: str
    spec: str
    casts: list[CastEvent] = field(default_factory=list)

    @property
    def cast_sequence(self) -> list[str]:
        """Only completed casts (not begincast), ordered by time."""
        return [c.ability_name for c in self.casts if c.event_type == "cast"]

    @property
    def ability_counts(self) -> Counter:
        return Counter(c.ability_name for c in self.casts if c.event_type == "cast")

    @property
    def ability_id_map(self) -> dict[str, int]:
        """Map ability name -> ability ID."""
        return {c.ability_name: c.ability_id for c in self.casts}

    @property
    def total_casts(self) -> int:
        return sum(1 for c in self.casts if c.event_type == "cast")

    @property
    def fight_duration_s(self) -> float:
        if len(self.casts) < 2:
            return 0
        return (self.casts[-1].timestamp - self.casts[0].timestamp) / 1000

    @property
    def casts_per_minute(self) -> float:
        dur = self.fight_duration_s
        if dur == 0:
            return 0
        return self.total_casts / (dur / 60)


@dataclass
class RotationComparison:
    player_a: PlayerRotation
    player_b: PlayerRotation

    def ability_usage_diff(self) -> list[dict]:
        """Compare ability usage counts between the two players."""
        all_abilities = sorted(
            set(self.player_a.ability_counts.keys()) | set(self.player_b.ability_counts.keys())
        )
        id_map = {**self.player_b.ability_id_map, **self.player_a.ability_id_map}
        rows = []
        for ability in all_abilities:
            a_count = self.player_a.ability_counts.get(ability, 0)
            b_count = self.player_b.ability_counts.get(ability, 0)
            rows.append({
                "ability": ability,
                "ability_id": id_map.get(ability, 0),
                "a_count": a_count,
                "b_count": b_count,
                "diff": a_count - b_count,
            })
        # Sort by total usage descending
        rows.sort(key=lambda r: r["a_count"] + r["b_count"], reverse=True)
        return rows

    def opener_comparison(self, n: int = 10) -> tuple[list[str], list[str]]:
        """Compare the first N casts (opener) of each player."""
        return (
            self.player_a.cast_sequence[:n],
            self.player_b.cast_sequence[:n],
        )

    def gap_analysis(self) -> tuple[list[dict], list[dict]]:
        """Find longest gaps between casts for each player (potential downtime)."""
        def _gaps(rotation: PlayerRotation) -> list[dict]:
            completed = [c for c in rotation.casts if c.event_type == "cast"]
            gaps = []
            for i in range(1, len(completed)):
                gap_ms = completed[i].timestamp - completed[i - 1].timestamp
                gaps.append({
                    "after_ability": completed[i - 1].ability_name,
                    "before_ability": completed[i].ability_name,
                    "gap_s": gap_ms / 1000,
                    "timestamp_s": (completed[i - 1].timestamp - completed[0].timestamp) / 1000,
                })
            gaps.sort(key=lambda g: g["gap_s"], reverse=True)
            return gaps[:10]  # top 10 longest gaps

        return _gaps(self.player_a), _gaps(self.player_b)

    def cast_timeline(self, window_s: float = 30) -> list[dict]:
        """Break the fight into windows and compare casts-per-window."""
        if not self.player_a.casts or not self.player_b.casts:
            return []
        fight_start = min(self.player_a.casts[0].timestamp, self.player_b.casts[0].timestamp)
        fight_end = max(self.player_a.casts[-1].timestamp, self.player_b.casts[-1].timestamp)
        window_ms = window_s * 1000
        windows = []
        t = fight_start
        while t < fight_end:
            t_end = t + window_ms
            a_casts = sum(1 for c in self.player_a.casts if c.event_type == "cast" and t <= c.timestamp < t_end)
            b_casts = sum(1 for c in self.player_b.casts if c.event_type == "cast" and t <= c.timestamp < t_end)
            windows.append({
                "window_start_s": (t - fight_start) / 1000,
                "a_casts": a_casts,
                "b_casts": b_casts,
            })
            t = t_end
        return windows


def build_damage_by_target(
    damage_events: list[dict],
    actor_map: dict[int, str],
    ability_names: dict[int, str] | None = None,
) -> dict:
    """Aggregate damage events by target, with per-ability breakdown."""
    by_target = defaultdict(lambda: {"total": 0, "hits": 0, "abilities": defaultdict(lambda: {"total": 0, "hits": 0})})

    for event in damage_events:
        target_id = event.get("targetID", 0)
        target_name = actor_map.get(target_id, f"Unknown({target_id})")
        amount = event.get("amount", 0) + event.get("absorbed", 0)
        ability_id = event.get("abilityGameID", 0)

        ability_name = None
        if ability_names:
            ability_name = ability_names.get(ability_id)
        if not ability_name:
            ability_name = f"Unknown({ability_id})"

        by_target[target_name]["total"] += amount
        by_target[target_name]["hits"] += 1
        by_target[target_name]["abilities"][ability_name]["total"] += amount
        by_target[target_name]["abilities"][ability_name]["hits"] += 1

    # Sort targets by total damage descending
    sorted_targets = sorted(by_target.items(), key=lambda x: x[1]["total"], reverse=True)
    return sorted_targets


def compare_cleave(
    targets_a: list,
    targets_b: list,
    player_a_name: str,
    player_b_name: str,
) -> list[dict]:
    """Compare damage-by-target between two players."""
    a_map = {name: data for name, data in targets_a}
    b_map = {name: data for name, data in targets_b}
    all_targets = sorted(set(a_map.keys()) | set(b_map.keys()))

    a_total = sum(d["total"] for d in a_map.values())
    b_total = sum(d["total"] for d in b_map.values())

    rows = []
    for target in all_targets:
        a_dmg = a_map.get(target, {}).get("total", 0)
        b_dmg = b_map.get(target, {}).get("total", 0)
        a_hits = a_map.get(target, {}).get("hits", 0)
        b_hits = b_map.get(target, {}).get("hits", 0)
        rows.append({
            "target": target,
            "a_dmg": a_dmg,
            "b_dmg": b_dmg,
            "a_pct": (a_dmg / a_total * 100) if a_total else 0,
            "b_pct": (b_dmg / b_total * 100) if b_total else 0,
            "a_hits": a_hits,
            "b_hits": b_hits,
        })
    rows.sort(key=lambda r: r["a_dmg"] + r["b_dmg"], reverse=True)
    return rows
