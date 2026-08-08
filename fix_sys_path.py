import os
import glob
import re

for filepath in glob.glob("/home/bk/code/defense/tests/**/*.py", recursive=True):
    with open(filepath, "r") as f:
        content = f.read()
    
    # Replace /home/bk/code/defense with /home/bk/code/defense/src/defense inside sys.path.insert
    new_content = re.sub(
        r"sys\.path\.insert\(\d+,\s*['\"]/home/bk/code/defense([^'\"]*)['\"]\)",
        r"sys.path.insert(0, '/home/bk/code/defense/src/defense\1')",
        content
    )
    # Handle str(_REPO)
    new_content = re.sub(
        r"sys\.path\.insert\(\d+,\s*str\(_REPO\)\)",
        r"sys.path.insert(0, str(_REPO / 'src/defense'))",
        new_content
    )
    # Handle str(_REPO / "something")
    new_content = re.sub(
        r"sys\.path\.insert\(\d+,\s*str\(_REPO / ['\"](.*?)['\"]\)\)",
        r"sys.path.insert(0, str(_REPO / 'src/defense/\1'))",
        new_content
    )
    
    if content != new_content:
        with open(filepath, "w") as f:
            f.write(new_content)
        print(f"Updated {filepath}")
