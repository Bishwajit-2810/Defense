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
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
                    if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                        fmt = node.left.value
                        num_specs = len(re.findall(r'%[a-zA-Z]', fmt))
                        
                        num_args = 0
                        if isinstance(node.right, ast.Tuple):
                            num_args = len(node.right.elts)
                        else:
                            num_args = 1
                            
                        if num_specs != num_args:
                            print(f"BinOp Mismatch in {path}:{node.lineno}: fmt has {num_specs} '%X' but passed {num_args} args. Fmt: {fmt}")
