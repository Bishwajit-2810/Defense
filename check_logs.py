import ast
import re

with open("src/defense/services/workers/stage2_llm/worker.py") as f:
    tree = ast.parse(f.read())

for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if node.func.value.id in ("log", "logger"):
                if node.args and isinstance(node.args[0], ast.Constant):
                    fmt = node.args[0].value
                    num_specs = len(re.findall(r'%s', fmt))
                    num_args = len(node.args) - 1
                    if num_specs != num_args:
                        print(f"Mismatch at line {node.lineno}: fmt has {num_specs} '%s' but passed {num_args} args. Fmt: {fmt}")
