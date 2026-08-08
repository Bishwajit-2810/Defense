import ast
import re
import os

for root, _, files in os.walk("src/defense"):
    for file in files:
        if file.endswith(".py"):
            path = os.path.join(root, file)
            with open(path, "r") as f:
                try:
                    tree = ast.parse(f.read())
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                        if node.func.value.id in ("log", "logger"):
                            if node.args and isinstance(node.args[0], ast.Constant):
                                fmt = node.args[0].value
                                if not isinstance(fmt, str):
                                    continue
                                num_specs = len(re.findall(r'%[a-zA-Z]', fmt))
                                num_args = len(node.args) - 1
                                if num_args > 0 and num_specs == 0:
                                    print(f"Zero '%s' but passed {num_args} args in {path}:{node.lineno}. Fmt: {fmt}")
