"""Views: JSON API + a small Leaflet demo page for the Loom walkthrough."""
import json

import requests
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from . import services


@csrf_exempt  # stateless JSON API; no session auth in this assessment build
def optimize(request):
    if request.method != "POST":
        return JsonResponse(
            {"error": "Use POST with JSON body: {\"start\": ..., \"finish\": ...}"},
            status=405,
        )
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "JSON body must be an object."}, status=400)

    start, finish = body.get("start"), body.get("finish")
    if not isinstance(start, str) or not start.strip():
        return JsonResponse(
            {"error": '"start" is required and must be a non-empty string.'},
            status=400,
        )
    if not isinstance(finish, str) or not finish.strip():
        return JsonResponse(
            {"error": '"finish" is required and must be a non-empty string.'},
            status=400,
        )
    try:
        result = services.plan_route(start, finish)
    except ValueError as e:
        # known domain errors: bad locations, same start/finish, infeasible
        return JsonResponse({"error": str(e)}, status=422)
    except requests.RequestException:
        # upstream geocoding/routing failure; keep the message generic
        return JsonResponse(
            {"error": "Upstream geocoding/routing service error."}, status=502)
    except Exception:  # noqa: BLE001 - never leak tracebacks to API clients
        return JsonResponse({"error": "Internal server error."}, status=500)
    return JsonResponse(result)


def demo(request):
    """Interactive map page (Leaflet) used in the Loom demo."""
    return render(request, "fuelapi/demo.html")
