"""The eleven example cards of the Decode card in fixtures_decode_cards.json, made from the signal report's own
rules: the page's screenshots and the tests then show what the rules say today, not what somebody once typed.

  python3 test/make_decode_fixtures.py            rewrite the file's "cards" section in place
  python3 test/make_decode_fixtures.py --check    change nothing; status 1 and the cases that differ when it is out of date

A card is its old keys as the file has them (everything not listed in "new_keys": frequency, verdict, band, dongle
lines ..., written by hand as an investigation gives them) plus the new keys from
arislib.sigreport.card_fields(build(example_facts(case))). "about", "new_keys", "titles", "extra" and "old_card"
are kept as they are: those are written by hand. shot52.py builds its cards with example_cards() too."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from arislib import sigreport  # noqa: E402

PATH = os.path.join(HERE, "fixtures_decode_cards.json")


def load(path=PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def example_cards(fix) -> dict:
    """{case: card} for every example case of the rules as they are now."""
    new_keys = set(fix.get("new_keys") or [])
    out = {}
    for case in sigreport.EXAMPLE_CASES:
        old = {k: v for k, v in ((fix.get("cards") or {}).get(case) or {}).items() if k not in new_keys}
        facts = sigreport.example_facts(case)
        new = sigreport.card_fields(sigreport.build(facts), facts["dongles"], facts.get("strongest"), card=dict(old))
        card = dict(old)
        card.update(new)
        out[case] = json.loads(json.dumps(card))            # as the page gets it: lists, never tuples
    return out


def differing(fix, cards) -> list:
    """One line per case whose card is not the file's, with the keys that differ."""
    have, lines = fix.get("cards") or {}, []
    for case in list(cards) + [c for c in have if c not in cards]:
        a, b = have.get(case), cards.get(case)
        if a == b:
            continue
        if a is None:
            lines.append(f"{case}: not in the file")
        elif b is None:
            lines.append(f"{case}: in the file, no longer an example case")
        else:
            keys = [k for k in list(b) + [k for k in a if k not in b] if a.get(k, KeyError) != b.get(k, KeyError)]
            lines.append(f"{case}: {', '.join(keys)}")
    return lines


def main(argv) -> int:
    fix = load()
    cards = example_cards(fix)
    diff = differing(fix, cards)
    name = os.path.basename(PATH)
    if "--check" in argv:
        if not diff:
            print(f"{name}: the {len(cards)} example cards are what the rules give")
            return 0
        print(f"{name} is out of date (python3 test/make_decode_fixtures.py rewrites it). Cases that differ:")
        for line in diff:
            print("  " + line)
        return 1
    if not diff:
        print(f"{name}: nothing to change")
        return 0
    fix["cards"] = cards
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fix, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, PATH)
    print(f"{name}: rewrote " + "; ".join(diff))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
