"""Seed the synthetic tenant graphs (Acme, Globex) in FalkorDB.

Usage (from the repo root, with the backend venv active):
    python data/seed.py            # add the seed data (safe to re-run)
    python data/seed.py --reset    # drop the tenant graphs first, then seed
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db import get_client  # noqa: E402
from app.seed import seed_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="drop the tenant graphs first")
    args = parser.parse_args()
    try:
        result = seed_all(get_client(), reset=args.reset)
    except Exception as exc:
        print(f"Seeding failed ({type(exc).__name__}). Is FalkorDB running? (docker compose up -d)")
        return 1
    for tenant, info in result.items():
        print(f"\n{tenant} -> graph '{info['graph']}'")
        print("  nodes:", ", ".join(f"{k}={v}" for k, v in sorted(info["nodes"].items())))
        print("  edges:", ", ".join(f"{k}={v}" for k, v in sorted(info["edges"].items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
