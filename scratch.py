import json
import re

def _parse_json(content: str):
    s = (content or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        inner = lines[1:-1] if lines and lines[-1].startswith("```") else lines[1:]
        s = "\n".join(inner).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        objects = []
        for match in re.finditer(r'\{[^{}]*"i"\s*:[^{}]*\}', s):
            obj_str = match.group(0)
            # fix trailing commas
            obj_str = re.sub(r',\s*\}', '}', obj_str)
            obj_str = re.sub(r',\s*\]', ']', obj_str)
            try:
                objects.append(json.loads(obj_str))
            except Exception:
                continue
        if objects:
            return {"labels": objects}
        raise e

print(_parse_json('{"labels":[{"i":1,"s":"pos","k":["hi"],}, {"i":2,"s":"neg"}]}'))
print(_parse_json('{"labels":[{"i":1,"s":"pos"} {"i":2,"s":"neg"}]}')) # missing comma
