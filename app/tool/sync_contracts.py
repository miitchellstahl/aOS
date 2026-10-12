"""Copy authoritative contracts into the installed asset bundle, or check drift."""

import argparse
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
SOURCE = APP.parent / "contracts"
FILES = [
    "dashboard.v1.schema.json",
    "input.v1.schema.json",
    "custom-events.schema.json",
    "ag-ui/schema.json",
    "ag-ui/LICENSE",
    "ag-ui/provenance.json",
    *[str(path.relative_to(SOURCE)) for path in sorted((SOURCE / "examples").glob("*.json"))],
    *[str(path.relative_to(SOURCE)) for path in sorted((SOURCE / "fixtures").glob("*.json"))],
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    stale = []
    for name in FILES:
        source = SOURCE / name
        target = APP / "assets" / "contracts" / name
        if options.check:
            if not target.is_file() or target.read_bytes() != source.read_bytes():
                stale.append(name)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    if stale:
        parser.exit(1, "stale bundled contracts: " + ", ".join(stale) + "\n")
    print(f"contracts: {len(FILES)} files {'checked' if options.check else 'synced'}")


if __name__ == "__main__":
    main()
