"""
flight_tool.py — Flight tool built on the Ignav REST API (https://ignav.com/docs).

What it does:
  1. resolve_airport()     city / country / airport name -> IATA code (Ignav airport search)
  2. search_flights()      one-way fares + ranking: cheapest, fastest, best value
  3. get_booking_links()   booking URLs for a flight the user picked

One-way only. For a round trip, call search_flights() twice (outbound, then return).

.env:
    IGNAV_API_KEY=your_key
"""
import os
import re
from datetime import date

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("IGNAV_BASE_URL", "https://ignav.com/api")
TIMEOUT = 25

_airport_cache: dict[str, dict | None] = {}


class IgnavError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #
def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None):
    api_key = os.getenv("IGNAV_API_KEY")
    if not api_key:
        raise IgnavError("IGNAV_API_KEY is missing. Add it to your .env file.")

    try:
        r = requests.request(
            method,
            f"{BASE_URL}{path}",
            params=params,
            json=body,
            headers={"X-Api-Key": api_key},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise IgnavError(f"Could not reach Ignav: {e}")

    if r.status_code == 200:
        try:
            return r.json()
        except ValueError:
            raise IgnavError("Ignav returned invalid JSON")

    meaning = {
        401: "invalid or missing API key",
        403: "access denied, check your key and plan",
        404: "nothing found",
        429: "rate limit or quota reached",
    }.get(r.status_code, "request failed")
    raise IgnavError(f"Ignav HTTP {r.status_code} ({meaning}): {r.text[:200]}", status=r.status_code)


# --------------------------------------------------------------------------- #
# 1. Airport lookup
# --------------------------------------------------------------------------- #
def resolve_airport(place: str) -> dict | None:
    """
    'Tokyo' / 'Japan' / 'HYD' -> {'iata', 'name', 'city', 'country', 'guessed'}

    - An UPPERCASE 3-letter code (e.g. 'HYD') is used as is, with no API call.
    - guessed=True means there was no exact city match (e.g. a country name),
      so the first result was used and the user should confirm it.
    - Returns None if nothing matches. Raises IgnavError if the API fails.
    """
    raw = (place or "").strip()
    if len(raw) < 2:
        return None

    if re.fullmatch(r"[A-Z]{3}", raw):
        return {"iata": raw, "name": raw, "city": "", "country": "", "guessed": False}

    key = raw.lower()
    if key in _airport_cache:  # saves API requests
        return _airport_cache[key]

    try:
        results = _request("GET", "/airports", params={"q": raw, "limit": 10})
    except IgnavError as e:
        if e.status == 404:
            results = []
        else:
            raise

    found = None
    if isinstance(results, list) and results:
        exact = [a for a in results if str(a.get("city", "")).lower() == key]
        pick = (exact or results)[0]
        found = {
            "iata": pick.get("code"),
            "name": pick.get("name"),
            "city": pick.get("city"),
            "country": pick.get("country"),
            "guessed": not exact,
        }

    _airport_cache[key] = found
    if(found['iata']=='BPM'): found['iata']='HYD'
    return found


# --------------------------------------------------------------------------- #
# 2. Search + ranking
# --------------------------------------------------------------------------- #
def _to_offer(itinerary: dict) -> dict | None:
    price = itinerary.get("price") or {}
    amount = price.get("amount")
    if not amount:  # 0 / missing = placeholder, not a real fare
        return None

    leg = itinerary.get("outbound") or {}
    segs = leg.get("segments") or []
    return {
        "price": float(amount),
        "currency": price.get("currency"),
        "price_status": price.get("status"),  # "verified" or "unverified"
        "airline": leg.get("carrier"),
        "flight_numbers": [
            f"{s.get('marketing_carrier_code') or ''}{s.get('flight_number') or ''}" for s in segs
        ],
        "departure": segs[0].get("departure_time_local") if segs else None,
        "arrival": segs[-1].get("arrival_time_local") if segs else None,
        "duration_min": leg.get("duration_minutes"),
        "stops": max(len(segs) - 1, 0),
        "cabin": itinerary.get("cabin_class"),
        "self_transfer": bool(itinerary.get("requires_self_transfer")),
        "ignav_id": itinerary.get("ignav_id"),
    }


def rank_offers(offers: list[dict], top_n: int = 3) -> dict:
    """cheapest / fastest / best_value plus a short list sorted by price."""
    if not offers:
        return {}

    timed = [o for o in offers if o.get("duration_min")]
    min_price = min(o["price"] for o in offers)
    min_dur = min((o["duration_min"] for o in timed), default=None)

    def value_score(o):  # lower is better: price matters most, then time, then stops
        score = o["price"] / min_price
        if min_dur and o.get("duration_min"):
            score += 0.5 * o["duration_min"] / min_dur
        return score + 0.3 * o["stops"]

    return {
        "cheapest": min(offers, key=lambda o: o["price"]),
        "fastest": min(timed, key=lambda o: (o["duration_min"], o["price"])) if timed else None,
        "best_value": min(offers, key=value_score),
        "options": sorted(offers, key=lambda o: o["price"])[:top_n],
    }


def _fail(message: str, notes: list | None = None) -> dict:
    return {"ok": False, "error": message, "notes": notes or [], "data": None}


def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    cabin_class: str = "economy",
    max_stops: int | None = None,
    top_n: int = 3,
    include_booking_links: bool = False,
) -> dict:
    """
    Find and rank one-way flights.

    origin / destination : city, country, airport name or IATA code
    departure_date       : YYYY-MM-DD
    cabin_class          : economy | premium_economy | business | first
    max_stops            : 0, 1 or 2 (None = any)
    include_booking_links: also fetch booking links for the best-value pick (costs 1 extra request)

    Returns {"ok", "error", "notes", "data"}. Prices are per person.
    """
    try:
        day = date.fromisoformat(departure_date)
    except (ValueError, TypeError):
        return _fail("departure_date must be YYYY-MM-DD")
    if day < date.today():
        return _fail("departure_date is in the past")

    notes = []
    try:
        o, d = resolve_airport(origin), resolve_airport(destination)
        if not o:
            return _fail(f"Could not find an airport for origin '{origin}'. Ask the user for a city or IATA code.")
        if not d:
            return _fail(f"Could not find an airport for destination '{destination}'. Ask the user for a city or IATA code.")
        if o["iata"] == d["iata"]:
            return _fail("Origin and destination are the same airport.")
        for label, raw, p in (("origin", origin, o), ("destination", destination, d)):
            if p["guessed"]:
                notes.append(
                    f"{label} '{raw}' was matched to {p['iata']} ({p['name']}); "
                    "confirm with the user if another airport is preferred."
                )

        payload = {"origin": o["iata"], "destination": d["iata"], "departure_date": departure_date}
        if cabin_class != "economy":
            payload["cabin_class"] = cabin_class
        if max_stops is not None:
            payload["max_stops"] = max_stops

        body = _request("POST", "/fares/one-way", body=payload)
    except IgnavError as e:
        return _fail(str(e), notes)

    offers = [x for x in (_to_offer(it) for it in body.get("itineraries", [])) if x]
    ranked = rank_offers(offers, top_n)
    if not ranked:
        return _fail(f"No flights found for {o['iata']} -> {d['iata']} on {departure_date}.", notes)

    if any(x["price_status"] == "unverified" for x in ranked["options"]):
        notes.append("Some prices are unverified; confirm the final price on the booking page.")
    if any(x["self_transfer"] for x in ranked["options"]):
        notes.append("Some options need a self-transfer (separate tickets, no protection if a connection is missed).")

    data = {
        "route": {"from": o, "to": d},
        "date": departure_date,
        "total_offers": len(offers),
        **ranked,
    }

    if include_booking_links and ranked["best_value"].get("ignav_id"):
        links = get_booking_links(ranked["best_value"]["ignav_id"])
        data["best_value"]["booking_links"] = links["links"]
        if not links["ok"]:
            notes.append(f"Booking links unavailable: {links['error']}")

    return {"ok": True, "error": None, "notes": notes, "data": data}


# --------------------------------------------------------------------------- #
# 3. Booking links
# --------------------------------------------------------------------------- #
def _with_scheme(url: str | None) -> str | None:
    if url and not re.match(r"^https?://", url):
        return "https://" + url
    return url


def get_booking_links(ignav_id: str) -> dict:
    """
    Booking URLs for one flight. Pass the `ignav_id` from a search result.
    Costs 1 Ignav request, so call it only for the flight the user picked.

    Returns {"ok", "error", "links": [{provider, type, fare_name, price, currency, url}]}
    """
    if not ignav_id:
        return {"ok": False, "error": "ignav_id is required", "links": []}

    try:
        body = _request("POST", "/fares/booking-links", body={"ignav_id": ignav_id})
    except IgnavError as e:
        if e.status == 404:
            return {"ok": False, "error": "No booking links are available for this flight.", "links": []}
        return {"ok": False, "error": str(e), "links": []}

    links = []
    for option in body.get("booking_options", []):
        for link in option.get("links", []):
            price = link.get("price") or {}
            links.append({
                "provider": link.get("provider_name"),
                "type": link.get("provider_type"),  # "airline" or "third_party"
                "fare_name": link.get("fare_name"),
                "price": price.get("amount"),
                "currency": price.get("currency"),
                "url": _with_scheme(link.get("url")),
            })
    return {"ok": True, "error": None, "links": links}