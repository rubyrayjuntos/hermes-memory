"""Shared helpers."""
import json

def format_name(first, last):
    return f"{first.title()} {last.title()}"

def to_json(obj):
    return json.dumps(obj, sort_keys=True)
