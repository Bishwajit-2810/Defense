import ast
import re
import os

for root, dirs, files in os.walk("src/defense"):
    for file in files:
        if file.endswith(".py"):
            filepath = os.path.join(root, file)
            with open(filepath, "r") as f:
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
                                if not isinstance(fmt, str): continue
                                num_specs = len(re.findall(r'%\(?[a-zA-Z_]*\)?[a-zA-Z]', fmt))
                                num_specs -= len(re.findall(r'%%', fmt)) * 2 # subtract escaped
                                num_args = len(node.args) - 1
                                if num_specs != num_args:
                                    if num_specs == 0 and num_args == 0 and node.keywords:
                                        continue
                                    print(f"Mismatch at {filepath}:{node.lineno}: fmt has {num_specs} specs but passed {num_args} args. Fmt: {fmt!r}")
