import json
def canonical_json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n'
