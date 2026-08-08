import ast
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
                            if node.args and not isinstance(node.args[0], ast.Constant):
                                print(f"Dynamic log call in {path}:{node.lineno}")
