import csv
import json
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "fuel-prices-for-be-assessment.csv"
OUT_PATH = HERE / "stations.json"
CITIES_URL = ("https://raw.githubusercontent.com/kelvins/US-Cities-Database"
              "/main/csv/us_cities.csv")

CANADIAN = {"AB", "ON", "BC", "MB", "SK", "QC", "NB", "NS", "NL", "PE",
            "NT", "YT", "NU"}
# neighbourhood / alternate spellings -> "city,state" present in the dataset
ALIASES = {
    ("elizabethport", "NJ"): ("elizabeth", "NJ"),
}

USER_AGENT = "spotter-fuel-assessment/1.0 (contact: arif.hussain.cswhiz@gmail.com)"


def load_cities():
    cities = {}
    req = urllib.request.Request(CITIES_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")
    for row in csv.DictReader(text.splitlines()):
        cities[(row["CITY"].strip().lower(), row["STATE_CODE"].strip())] = (
            float(row["LATITUDE"]), float(row["LONGITUDE"]))
    nospace = {(c.replace(" ", ""), s): v for (c, s), v in cities.items()}
    return cities, nospace


def nominatim_city(city, state):
    """One-time lookup for cities missing from the dataset (build-time only)."""
    import urllib.parse
    q = urllib.parse.quote(f"{city}, {state}, USA")
    url = (f"https://nominatim.openstreetmap.org/search?q={q}&format=json"
           f"&limit=1&countrycodes=us")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        time.sleep(1.1)  # Nominatim usage policy
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:  # noqa: BLE001 - build script; warn and continue
        print(f"  WARN: Nominatim lookup failed for {city}, {state}: {e}")
    return None


def main():
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    print(f"CSV rows: {len(rows)}")

    by_id = defaultdict(list)
    for r in rows:
        by_id[r["OPIS Truckstop ID"].strip()].append(r)
    print(f"unique truckstop IDs: {len(by_id)}")

    cities, nospace = load_cities()
    print(f"cities in dataset: {len(cities)}")

    stations, dropped_ca, unmatched = [], 0, []
    for sid, group in by_id.items():
        first = group[0]
        state = first["State"].strip()
        if state in CANADIAN:
            dropped_ca += 1
            continue
        prices = [float(r["Retail Price"]) for r in group if r["Retail Price"]]
        price = round(sum(prices) / len(prices), 3)
        key = (first["City"].strip().lower(), state)
        key = ALIASES.get(key, key)
        hit = cities.get(key) or nospace.get((key[0].replace(" ", ""), key[1]))
        if hit is None:
            hit = nominatim_city(first["City"].strip(), state)
        if hit is None:
            unmatched.append((first["City"], state, first["Truckstop Name"]))
            continue
        lat, lng = hit
        stations.append({
            "id": sid,
            "name": first["Truckstop Name"].strip(),
            "address": first["Address"].strip(),
            "city": first["City"].strip(),
            "state": state,
            "price": price,
            "price_observations": len(prices),
            "lat": lat,
            "lng": lng,
        })

    print(f"dropped Canadian: {dropped_ca}")
    print(f"unmatched (dropped): {len(unmatched)}")
    for u in unmatched:
        print("  UNMATCHED:", u)
    print(f"stations written: {len(stations)}")

    with open(OUT_PATH, "w") as f:
        json.dump(stations, f)
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
