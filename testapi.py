"""Minimal smoke test for POST /api/pipeline/run using mockProfile.json."""

import json
from pathlib import Path

import requests

BASE_URL = "http://127.0.0.1:5000"
MOCK_PATH = Path(__file__).resolve().parent / "mockProfile.json"

with MOCK_PATH.open(encoding="utf-8") as f:
    counselor_form = json.load(f)

response = requests.post(
    f"{BASE_URL}/api/pipeline/run",
    json={
        "counselor_form": counselor_form,
        "counselorid": 1,
        "schoolid": 1,
        "parentid": 1,
        "current_services": ["cbt", "school counseling", "therapy family caregiver"],
        "parent_form": {"zip": "30301", "location": "Atlanta, GA"},
    },
    timeout=600,
)

print(response.status_code)
print(json.dumps(response.json(), indent=2))
