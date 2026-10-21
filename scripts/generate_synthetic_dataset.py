"""Write a SYNTHETIC NIFTY + weekly-options dataset as CSVs for trying out the pipeline.

The output is generated data, not market data. Import it with "This is synthetic data"
ticked (or --import) so every report built on it carries a synthetic-data warning.

    python scripts/generate_synthetic_dataset.py --days 20 --out data/market/synthetic
    python scripts/generate_synthetic_dataset.py --days 20 --import   # also imports via the running API
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
import uuid
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.market_data.synthetic import SyntheticConfig, generate, to_csv_rows  # noqa: E402

CANDLE_FIELDS = ["timestamp", "tradingsymbol", "exchange", "open", "high", "low", "close", "volume"]
INSTRUMENT_FIELDS = ["tradingsymbol", "exchange", "segment", "instrument_type", "underlying", "strike", "expiry", "lot_size", "tick_size"]


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


def import_via_api(api: str, name: str, candles: Path, instruments: Path) -> dict:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in (("name", name), ("source_label", "SYNTHETIC generator (scripts/generate_synthetic_dataset.py)"), ("is_synthetic", "true")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for key, path in (("candles_file", candles), ("instruments_file", instruments)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{path.name}"\r\nContent-Type: text/csv\r\n\r\n'.encode())
        parts.append(path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"{api}/api/market-data/import", data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=20)
    p.add_argument("--start", type=date.fromisoformat, default=date(2025, 1, 6))
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--strikes-each-side", type=int, default=10)
    p.add_argument("--out", type=Path, default=Path("data/market/synthetic"))
    p.add_argument("--import", dest="do_import", action="store_true", help="Import into the running backend")
    p.add_argument("--api", default="http://127.0.0.1:8000")
    args = p.parse_args()

    instruments, bars = generate(SyntheticConfig(start_date=args.start, days=args.days, seed=args.seed,
                                                 strikes_each_side=args.strikes_each_side))
    inst_rows, bar_rows = to_csv_rows(instruments, bars)
    args.out.mkdir(parents=True, exist_ok=True)
    candles, insts = args.out / "candles.csv", args.out / "instruments.csv"
    write_csv(candles, bar_rows, CANDLE_FIELDS)
    write_csv(insts, inst_rows, INSTRUMENT_FIELDS)
    print(f"Wrote {len(bar_rows):,} bars and {len(inst_rows)} instruments to {args.out} (SYNTHETIC)")
    if args.do_import:
        ds = import_via_api(args.api, f"SYNTHETIC NIFTY {args.days}d seed {args.seed}", candles, insts)
        print(f"Imported dataset #{ds['id']} ({ds['row_count']:,} bars)")


if __name__ == "__main__":
    main()
