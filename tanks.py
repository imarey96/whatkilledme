"""What the analyzer tracks for each tank spec. Names match the log's ability names."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Resource:
    name: str
    type: int  # classResources type in WCL events
    divisor: int  # Rage and Runic Power are logged at 10x their in-game value
    near_cap: int  # Within this much of the max counts as near the cap
    builders: tuple[str, ...]


@dataclass(frozen=True)
class TankSpec:
    label: str = ""
    defensives: tuple[str, ...] = ()
    control: tuple[str, ...] = ()
    taunts: tuple[str, ...] = ()
    # Buff name, and the combat uptime worth calling out as good
    uptimes: tuple[tuple[str, int], ...] = ()
    resource: Resource | None = None
    # Fired automatically, so they don't count as casts
    auto_procs: frozenset[str] = frozenset()


# Used for a player who isn't in a tank spec, so only the generic checks run
NO_SPEC = TankSpec()

SPECS = {
    ("DeathKnight", "Blood"): TankSpec(
        label="Blood Death Knight",
        defensives=("Dancing Rune Weapon", "Vampiric Blood", "Icebound Fortitude", "Anti-Magic Shell",
                    "Death Pact", "Rune Tap", "Tombstone"),
        control=("Blinding Sleet", "Gorefiend's Grasp", "Asphyxiate"),
        taunts=("Dark Command",),
        uptimes=(("Bone Shield", 95), ("Death and Decay", 70), ("Vampiric Blood", 0),
                 ("Dancing Rune Weapon", 0), ("Blood Shield", 0)),
        resource=Resource("Runic Power", 6, 10, 10, ("Heart Strike", "Blood Boil", "Vampiric Strike")),
        auto_procs=frozenset({"Infliction of Sorrow"}),
    ),
    ("DemonHunter", "Vengeance"): TankSpec(
        label="Vengeance Demon Hunter",
        defensives=("Metamorphosis", "Fiery Brand", "Fel Devastation", "Darkness", "Soul Barrier"),
        control=("Sigil of Misery", "Sigil of Silence", "Sigil of Chains", "Chaos Nova"),
        taunts=("Torment",),
        uptimes=(("Demon Spikes", 90), ("Immolation Aura", 75), ("Metamorphosis", 0)),
        resource=Resource("Fury", 17, 1, 0, ("Fracture", "Felblade")),
        auto_procs=frozenset({"Soul Fragment"}),
    ),
    ("Druid", "Guardian"): TankSpec(
        label="Guardian Druid",
        defensives=("Survival Instincts", "Barkskin", "Incarnation: Guardian of Ursoc", "Rage of the Sleeper",
                    "Frenzied Regeneration", "Renewal"),
        control=("Incapacitating Roar", "Typhoon", "Mass Entanglement", "Ursol's Vortex"),
        taunts=("Growl",),
        uptimes=(("Ironfur", 85), ("Frenzied Regeneration", 0), ("Barkskin", 0)),
        resource=Resource("Rage", 1, 10, 15, ("Mangle", "Thrash")),
    ),
    ("Monk", "Brewmaster"): TankSpec(
        label="Brewmaster Monk",
        defensives=("Fortifying Brew", "Celestial Infusion", "Dampen Harm", "Diffuse Magic", "Zen Meditation",
                    "Invoke Niuzao, the Black Ox", "Black Ox Brew"),
        control=("Leg Sweep", "Ring of Peace", "Paralysis"),
        taunts=("Provoke",),
        uptimes=(("Shuffle", 95), ("Fortifying Brew", 0)),
        auto_procs=frozenset({"Gift of the Ox"}),
    ),
    ("Paladin", "Protection"): TankSpec(
        label="Protection Paladin",
        defensives=("Ardent Defender", "Guardian of Ancient Kings", "Sentinel", "Divine Shield", "Lay on Hands",
                    "Blessing of Spellwarding", "Holy Bulwark"),
        control=("Hammer of Justice", "Blinding Light"),
        taunts=("Hand of Reckoning",),
        uptimes=(("Shield of the Righteous", 90), ("Consecration", 85), ("Ardent Defender", 0)),
    ),
    ("Warrior", "Protection"): TankSpec(
        label="Protection Warrior",
        defensives=("Shield Wall", "Last Stand", "Demoralizing Shout", "Spell Reflection", "Rallying Cry"),
        control=("Shockwave", "Storm Bolt", "Intimidating Shout"),
        taunts=("Taunt",),
        uptimes=(("Shield Block", 85), ("Ignore Pain", 80), ("Shield Wall", 0)),
        resource=Resource("Rage", 1, 10, 15, ("Shield Slam", "Thunder Clap", "Thunder Blast")),
    ),
}


def find_spec(player: dict) -> TankSpec:
    """The player's tank spec, or NO_SPEC. A player can show several specs across a fight."""
    for spec in player["specs"]:
        if (player["class"], spec) in SPECS:
            return SPECS[(player["class"], spec)]
    return NO_SPEC
