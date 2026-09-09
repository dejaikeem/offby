"""Synthetic product reviews for batch experiments. Deterministic for a seed; no real data.

  uv run python examples/gen_reviews.py --n 6000 --out /tmp/reviews-6k.jsonl
"""

from __future__ import annotations

import argparse
import json
import random

PRODUCTS = ["headphones", "blender", "running shoes", "standing desk", "phone case", "air purifier", "backpack",
            "mechanical keyboard", "coffee grinder", "monitor", "electric kettle", "yoga mat", "smart plug", "desk lamp"]
POS = ["Battery lasts two days and the build feels premium.", "Does exactly what it says; setup took five minutes.",
       "Great value for the money, shipping was fast.", "Quiet, sturdy, and the app just works.",
       "Replaced a much pricier one and I can't tell the difference.", "Second one I've bought; the first is still going after three years."]
NEG = ["Cracked in a week and support was slow.", "Louder than expected and the app crashes on login.",
       "Arrived damaged; the replacement was worse.", "Stopped charging after a month.",
       "Smells like chemicals and the smell never fades.", "The description says metal; it is painted plastic."]
MIX = ["Good hardware, terrible software.", "Love the design, hate the price.", "Works, but the manual is useless.",
       "Fast when it works, which is most of the time.", "Fine for the price, nothing more."]
TAILS = ["", " Would recommend to a friend.", " Returning it.", " Still deciding.", " Bought it for my parents.",
         " Update after two months: same opinion.", " Compared it with the previous model in the store first."]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=3)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    with open(args.out, "w") as f:
        for i in range(args.n):
            pool = rng.choice([POS, POS, NEG, MIX])
            text = f"{rng.choice(pool)}{rng.choice(TAILS)}"
            f.write(json.dumps({"id": i + 1, "product": rng.choice(PRODUCTS), "text": text}, ensure_ascii=False) + "\n")
    print(f"wrote {args.n} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
