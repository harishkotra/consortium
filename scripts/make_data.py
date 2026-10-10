"""Generate `data/vendor_records.csv` — VendorCorp's dataset, once, deterministically.

Why a documented generator instead of a public dataset
------------------------------------------------------
The spec allows "a real public source **or** a documented generator". A public dataset with a
usable `customer_name` + `customer_email` pair would be *real people's* personal data, and this
demo is about refusing to hand that over — so the PII columns here are synthetic and provably
not real: every address is at `example.com`, the domain IANA reserves for documentation
(RFC 2606), with a numeric suffix. Nothing in the file corresponds to a real customer.

Determinism
-----------
`random.Random(20261010)`, no wall-clock input, no third-party distribution. Re-running this
script on any machine byte-for-byte reproduces the committed CSV (the sha256 is printed at the
end and recorded in the README).

Columns (8)
-----------
    order_id        string  "ORD-100001" ...
    customer_name   string  PII-ish: first + last from the embedded pools
    customer_email  string  PII-ish: local-part @ example.com
    region          string  one of NA-East | NA-West | EMEA | APAC | LATAM
    channel         string  one of online | retail | partner
    order_date      string  ISO-8601 YYYY-MM-DD, 2025-04-01 .. 2025-12-31
    units           int     1..12
    order_value     float   2dp; per-region mean so regions genuinely differ in AOV

Run:  .venv/bin/python scripts/make_data.py
"""

from __future__ import annotations

import csv
import hashlib
import random
import statistics
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "vendor_records.csv"

SEED = 20261010
N_ROWS = 624
START = date(2025, 4, 1)
END = date(2025, 12, 31)

FIRST_NAMES = [
    "Aaliyah", "Amara", "Anders", "Beatriz", "Bilal", "Camille", "Chidi", "Dara", "Diego", "Elin",
    "Emeka", "Farida", "Fenwick", "Giovanna", "Gustav", "Hana", "Hugo", "Ida", "Imani", "Ines",
    "Jasper", "Jonas", "Kailani", "Kenji", "Leilani", "Lennart", "Malika", "Marek", "Naomi", "Nils",
    "Olivia", "Omar", "Paloma", "Priya", "Quentin", "Rafael", "Rania", "Sanne", "Sofia", "Tariq",
    "Tomas", "Ursula", "Valentin", "Xiomara", "Yara", "Yuki", "Zainab", "Zora",
]

LAST_NAMES = [
    "Adeyemi", "Almeida", "Bergstrom", "Castellanos", "Chaudhary", "Delacroix", "Dvorak", "Esposito",
    "Fitzgerald", "Grigoryan", "Halvorsen", "Ibarra", "Jorgensen", "Kaminski", "Kowalczyk", "Lindqvist",
    "Machado", "Majumdar", "Nakamura", "Novikova", "Okonkwo", "Oyelaran", "Petrov", "Quiroga",
    "Rasmussen", "Reynoso", "Sandoval", "Sarkar", "Schneider", "Silva", "Takahashi", "Toth",
    "Vasquez", "Virtanen", "Weber", "Yilmaz", "Zielinski", "Zu",
]

REGIONS = ["NA-East", "NA-West", "EMEA", "APAC", "LATAM"]
CHANNELS = ["online", "retail", "partner"]

# Mean order value per region (currency units); AOV genuinely differs by region, which is
# what makes "average order value by region" a non-trivial answer.
REGION_MEAN = {"NA-East": 148.0, "NA-West": 172.0, "EMEA": 121.0, "APAC": 96.0, "LATAM": 133.0}
REGION_SD = {"NA-East": 55.0, "NA-West": 62.0, "EMEA": 44.0, "APAC": 38.0, "LATAM": 51.0}


def build_rows() -> list[dict[str, object]]:
    rng = random.Random(SEED)
    span_days = (END - START).days
    rows: list[dict[str, object]] = []
    for index in range(N_ROWS):
        region = rng.choices(REGIONS, weights=[26, 22, 21, 18, 13])[0]
        channel = rng.choices(CHANNELS, weights=[58, 27, 15])[0]
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        order_date = START + timedelta(days=rng.randint(0, span_days))
        units = rng.randint(1, 12)
        # Log-normal-ish spend: most orders near the regional mean, a long right tail.
        value = max(4.0, rng.gauss(REGION_MEAN[region], REGION_SD[region]) * (1 + 0.04 * units))
        rows.append(
            {
                "order_id": f"ORD-{100001 + index}",
                "customer_name": f"{first} {last}",
                "customer_email": f"{first.lower()}.{last.lower()}.{index:03d}@example.com",
                "region": region,
                "channel": channel,
                "order_date": order_date.isoformat(),
                "units": units,
                "order_value": round(value, 2),
            }
        )
    return rows


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = build_rows()
    fields = list(rows[0])
    with open(OUT, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    q3 = [r for r in rows if "2025-07-01" <= str(r["order_date"]) <= "2025-09-30"]

    print(f"wrote {OUT.relative_to(ROOT)}: {len(rows)} rows, {len(fields)} columns")
    print(f"sha256 {digest}")
    print(f"date range {min(str(r['order_date']) for r in rows)} .. {max(str(r['order_date']) for r in rows)}")
    print(f"unique customers {len({str(r['customer_email']) for r in rows})}")
    print()
    print("ground truth for the demo question -- average order_value by region, 2025 Q3")
    print(f"{'region':<9} {'n':>4} {'aov':>9}")
    for region in REGIONS:
        values = [float(r["order_value"]) for r in q3 if r["region"] == region]
        print(f"{region:<9} {len(values):>4} {statistics.fmean(values):>9.4f}")
    print(f"{'ALL':<9} {len(q3):>4} {statistics.fmean([float(r['order_value']) for r in q3]):>9.4f}")


if __name__ == "__main__":
    main()
