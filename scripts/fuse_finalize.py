import json
import re
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The canonical 27 boons, verbatim as supplied by the operator.
BOONS = [
    ("Boon of Combat Prowess", "When you miss with a melee weapon attack, you can choose to hit instead. Once you use this boon, you can't use it again until the start of your next turn."),
    ("Boon of Dimensional Travel", "Immediately after you take the Attack action or cast a spell, you can teleport up to 30 feet to an unoccupied space you can see."),
    ("Boon of Energy Resistance", "You gain resistance to two damage types of your choice from: Acid, Cold, Fire, Lightning, Necrotic, Poison, Radiant, or Thunder. You can change these choices whenever you finish a Short or Long Rest."),
    ("Boon of Fate", "When another creature that you can see within 60 feet of you makes an ability check, an attack roll, or a saving throw, you can roll a d10 and apply the result as a bonus or penalty to the roll. Once you use this boon, you can't use it again until you finish a short rest."),
    ("Boon of Fortitude", "Your hit point maximum increases by 40. Whenever you regain Hit Points, you can regain additional Hit Points equal to your Constitution modifier (once per turn)."),
    ("Boon of High Magic", "You gain one 9th-level spell slot, provided that you already have one."),
    ("Boon of Immortality", "You stop aging. You are immune to any effect that would age you, and you can't die from old age."),
    ("Boon of Invincibility", "When you take damage from any source, you can reduce that damage to 0. Once you use this boon, you can't use it again until you finish a short rest."),
    ("Boon of Irresistible Offense", "You can bypass the damage resistances of any creature."),
    ("Boon of Luck", "You can add a d10 roll to any ability check, attack roll, or saving throw you make. Once you use this boon, you can't use it again until you finish a short rest."),
    ("Boon of Magic Resistance", "You have advantage on saving throws against spells and other magical effects."),
    ("Boon of Peerless Aim", "You can give yourself a +20 bonus to a ranged attack roll you make. Once you use this boon, you can't use it again until you finish a short rest."),
    ("Boon of Perfect Health", "You are immune to all diseases and poisons, and you have advantage on Constitution saving throws."),
    ("Boon of Planar Travel", "When you gain this boon, choose a plane of existence other than the Material Plane. You can now use an action to cast the Plane Shift spell (no spell slot or components required), targeting yourself only, and travel to the chosen plane, or from that plane back to the Material Plane. Once you use this boon, you can't use it again until you finish a short rest."),
    ("Boon of Quick Casting", "Choose one of your spells of 1st through 3rd level that has a casting time of 1 action. That spell's casting time is now 1 bonus action for you."),
    ("Boon of Recovery", "When you drop to 0 Hit Points, you can instantly regain Hit Points equal to half your Hit Point maximum. Once used, you cannot use this again until a Long Rest."),
    ("Boon of Resilience", "You have resistance to bludgeoning, piercing, and slashing damage from nonmagical weapons."),
    ("Boon of Skill Proficiency", "You gain proficiency in all skills."),
    ("Boon of Speed", "Your walking speed increases by 30 feet. In addition, you can use a bonus action to take the Dash or Disengage action. Once you do so, you can't do so again until you finish a short rest."),
    ("Boon of Spell Mastery", "Choose one 1st-level sorcerer, warlock, or wizard spell that you can cast. You can now cast that spell at its lowest level without expending a spell slot."),
    ("Boon of Spell Recall", "You can cast any spell you know or have prepared without expending a spell slot. Once you do so, you can't use this boon again until you finish a long rest."),
    ("Boon of the Fire Soul", "You have immunity to fire damage. You can also cast Burning Hands (save DC 15) at will, without using a spell slot or any components."),
    ("Boon of the Night Spirit", "While completely in an area of dim light or darkness, you can become invisible as an action. You remain invisible until you take an action or a reaction."),
    ("Boon of the Stormborn", "You have immunity to lightning and thunder damage. You can also cast Thunderwave (save DC 15) at will, without using a spell slot or any components."),
    ("Boon of the Unfettered", "You have advantage on ability checks made to resist being grappled. In addition, you can use an action to automatically escape a grapple or free yourself of restraints of any kind."),
    ("Boon of Truesight", "You have truesight out to a range of 60 feet."),
    ("Boon of Undetectability", "You gain a +10 bonus to Dexterity (Stealth) checks, and you can't be detected or targeted by divination magic, including scrying sensors."),
]

# DMG epic boons are a 20th-level feature.
PREREQ = "Level 20+"
BOON_TYPE = "epic_boon"
BOON_SOURCE = "dmg"

# Levels 20+ can take an epic boon; below that the ASI popup offers feats only.
BOON_MIN_LEVEL = 20


def slugify(name):
    return name.lower().replace(" ", "_").replace("'", "")


def xp_table(max_level):
    """Original 2014 5e thresholds through 20, then +30000 per level.

    Note: the shipped 5e table uses 195000 for level 16 (not the 190000 of the
    2014 PHB). That value is preserved here rather than silently "corrected",
    because existing characters may already be levelled against it.
    """
    base = [
        ("0", 1),
        ("300", 2),
        ("900", 3),
        ("2700", 4),
        ("6500", 5),
        ("14000", 6),
        ("23000", 7),
        ("34000", 8),
        ("48000", 9),
        ("64000", 10),
        ("85000", 11),
        ("100000", 12),
        ("120000", 13),
        ("140000", 14),
        ("165000", 15),
        ("195000", 16),
        ("225000", 17),
        ("265000", 18),
        ("305000", 19),
        ("355000", 20),
    ]
    table = dict(base)
    # The app needs a floor entry so any XP total maps to a level.
    table["-999999999"] = 1

    xp = 355000
    for lvl in range(21, max_level + 1):
        xp += 30000
        table[str(xp)] = lvl

    return dict(sorted(table.items(), key=lambda kv: int(kv[0])))


def boon_payload(name, description, system):
    return {
        "system": system,
        "resource_id": "feat",
        "stats": {
            "name": {"value": name},
            "source": {"value": BOON_SOURCE},
            "type": {"value": BOON_TYPE},
            "prerequisites": {"value": PREREQ},
            "descriptions": {
                "value": [
                    {
                        "system": system,
                        "resource_id": "levelled_description",
                        "stats": {
                            "description": {"value": description},
                            "level": {"value": BOON_MIN_LEVEL},
                        },
                    }
                ]
            },
        },
    }


def upsert_boons(system):
    """Write exactly the 27 canonical boons, removing any other feat_boon file."""
    d = os.path.join(REPO, "systems", system, "resource_instances")
    wanted = set()
    for name, description in BOONS:
        fname = "feat_%s.rpg.json" % slugify(name)
        wanted.add(fname)
        path = os.path.join(d, fname)
        payload = boon_payload(name, description, system)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    removed = 0
    for fname in os.listdir(d):
        if fname.startswith("feat_boon") and fname not in wanted:
            os.remove(os.path.join(d, fname))
            removed += 1
    return len(wanted), removed


def extend_xp(system, max_level):
    """Rewrite only the XP threshold block, leaving the rest of the file byte-identical.

    The block is anchored on the unique "355000": 20 line and ends at the closing
    brace, so the patch cannot disturb any other part of system.rpg.json.
    """
    path = os.path.join(REPO, "systems", system, "system", "system.rpg.json")
    with open(path, "rb") as fh:
        raw = fh.read()
    # Preserve the file's existing line endings (CRLF in this repo) byte-for-byte.
    newline = "\r\n" if b"\r\n" in raw else "\n"
    lines = raw.decode("utf-8").splitlines()

    # The block is a contiguous run of "xp": level pairs. Match on structure, not on
    # an assumed entry count, so the patch survives any pre-existing corruption.
    entry = re.compile(r'^(\s+)"(-?\d+)": (\d+)(,?)$')
    start = next(
        (i for i, line in enumerate(lines)
         if entry.match(line) and entry.match(line).group(2) == "-999999999"),
        None,
    )
    if start is None:
        raise RuntimeError("%s: XP table anchor '\"-999999999\"' not found" % system)

    end = start
    while end + 1 < len(lines) and entry.match(lines[end + 1]):
        end += 1

    indent = entry.match(lines[start]).group(1)
    items = list(xp_table(max_level).items())
    rebuilt = ['%s"%s": %d,' % (indent, xp, lvl) for xp, lvl in items[:-1]]
    rebuilt.append("%s\"%s\": %d" % (indent, items[-1][0], items[-1][1]))

    lines[start:end + 1] = rebuilt
    patched = newline.join(lines)

    # Fail loudly rather than leave a corrupt system file behind.
    result = json.loads(patched)
    table = result["progression_systems"]["experience_levelling_system"]["tables"][0][
        "experience_to_level_table"
    ]
    if table != dict(xp_table(max_level)):
        raise RuntimeError("%s: XP table did not round-trip cleanly" % system)
    # max_level XP rows plus the app's "-999999999" floor row.
    if len(table) != max_level + 1:
        raise RuntimeError(
            "%s: expected %d XP entries, wrote %d" % (system, max_level + 1, len(table))
        )

    with open(path, "wb") as fh:
        fh.write(patched.encode("utf-8"))
    return max(table.values())


if __name__ == "__main__":
    max_level = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    for system in ("5e", "5e2024"):
        count, removed = upsert_boons(system)
        levels = extend_xp(system, max_level)
        print("%-7s boons=%d (stray removed=%d)  max level=%d"
              % (system, count, removed, levels))