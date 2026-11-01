
from app.tools.flight_tool import (
    resolve_airport,
    search_flights,
    rank_offers,
    get_booking_links,
)


def main():
    print("\n========== IGNAV FLIGHT TOOL TESTING ==========\n")

    # 1. Resolve origin airport
    print("1. Resolving origin airport...")
    origin = resolve_airport("Hyderabad")
    print(origin)

    # 2. Resolve destination airport
    print("\n2. Resolving destination airport...")
    destination = resolve_airport("Dubai")
    print(destination)

    # 3. Search real flights using Ignav
    print("\n3. Searching flights through Ignav...")

    result = search_flights(
        origin="Hyderabad",
        destination="Dubai",
        departure_date="2027-01-15",
        cabin_class="economy",
        top_n=3,
    )

    if not result["ok"]:
        print("Flight search failed:", result["error"])
        return

    data = result["data"]

    print("\nTotal offers:", data["total_offers"])

    # 4. Display actual flight offers
    print("\n4. Flight offers returned by Ignav:")

    for index, flight in enumerate(data["options"], start=1):
        print(f"\nFlight {index}")
        print("Airline:", flight.get("airline"))
        print("Price:", flight.get("price"))
        print("Duration:", flight.get("duration"))
        print("Stops:", flight.get("stops"))
        print("Flight ID:", flight.get("ignav_id"))

    # 5. Rank actual Ignav offers
    print("\n5. Ranking actual Ignav offers...")

    ranking = rank_offers(data["options"])

    print("\nCheapest:", ranking["cheapest"])
    print("\nFastest:", ranking["fastest"])
    print("\nBest value:", ranking["best_value"])

    # 6. Get booking links for the best-value flight
    print("\n6. Fetching booking links...")

    best_flight = ranking["best_value"]
    flight_id = best_flight.get("ignav_id")

    if flight_id:
        links = get_booking_links(flight_id)
        print("Booking links:", links)
    else:
        print("No Ignav flight ID available.")

    print("\n========== TESTING COMPLETED ==========\n")


if __name__ == "__main__":
    main()
