import json
import urllib.request


def get_weather_forecast(lat=29.55, lon=-95.81, days_ahead=0):
    """Daily forecast from Open-Meteo. days_ahead: 0 = today, 1 = tomorrow ... up to 6.
    Defaults to the Rosenberg/Richmond area (77469)."""
    days_ahead = max(0, min(int(days_ahead), 6))
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=America%2FChicago&forecast_days=7"
    )

    req = urllib.request.Request(url, headers={"User-Agent": "EnterpriseAgent/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    daily = data.get("daily", {})
    i = days_ahead
    return {
        "date": daily["time"][i],
        "max_temp_c": daily["temperature_2m_max"][i],
        "min_temp_c": daily["temperature_2m_min"][i],
        "precip_prob_percent": daily["precipitation_probability_max"][i],
    }


if __name__ == "__main__":
    print(get_weather_forecast())
