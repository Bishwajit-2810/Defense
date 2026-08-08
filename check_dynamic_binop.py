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
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
                    if not isinstance(node.left, ast.Constant):
                        print(f"Dynamic % formatting in {path}:{node.lineno}")
