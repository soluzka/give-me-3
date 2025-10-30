import json
from collections.abc import Mapping, Sequence

def sanitize_for_json(obj, seen=None):
    """
    Recursively sanitize an object for JSON serialization:
    - Handles circular references by replacing them with a string.
    - Converts only serializable types (dict, list, str, int, float, bool, None).
    - Replaces non-serializable objects with a string placeholder.
    """
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return '<circular-reference>'
    seen.add(obj_id)

    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    elif isinstance(obj, Mapping):
        # Defensive: force 'author', 'content', and 'channel' fields to string
        out = {}
        for k, v in obj.items():
            key = sanitize_for_json(k, seen) if not isinstance(k, str) else k
            if key in ('author', 'content', 'channel'):
                out[key] = str(v)
            elif key == 'timeout_enabled':
                # Always output a boolean for timeout_enabled
                out[key] = v if isinstance(v, bool) else True
            else:
                out[key] = sanitize_for_json(v, seen)
        return out
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
        return [sanitize_for_json(item, seen) for item in obj]
    else:
        return f'<non-serializable: {type(obj).__qualname__}>'

def safe_json_dump(obj, fp, **kwargs):
    """Safely dump an object to JSON, avoiding circular references."""
    sanitized = sanitize_for_json(obj)
    json.dump(sanitized, fp, **kwargs)


def safe_json_dumps(obj, **kwargs):
    """Safely convert an object to a JSON string, avoiding circular references."""
    sanitized = sanitize_for_json(obj)
    return json.dumps(sanitized, **kwargs)
