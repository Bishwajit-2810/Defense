import os

for root, _, files in os.walk('src/defense/services/api/routers'):
    for file in files:
        if file.endswith('.py'):
            filepath = os.path.join(root, file)
            with open(filepath, 'r') as f:
                content = f.read()
            
            lines = content.split('\n')
            new_lines = []
            for l in lines:
                if l.startswith('from deps import '):
                    l = l.replace('from deps import ', 'from defense.services.api.deps import ')
                elif l.startswith('from models import '):
                    l = l.replace('from models import ', 'from defense.services.api.models import ')
                new_lines.append(l)
            
            with open(filepath, 'w') as f:
                f.write('\n'.join(new_lines))

# Also fix `main.py` which might have `from routers import` instead of `from defense.services.api.routers import`
with open('src/defense/services/api/main.py', 'r') as f:
    content = f.read()

lines = content.split('\n')
new_lines = []
for l in lines:
    if l.startswith('from routers import '):
        l = l.replace('from routers import ', 'from defense.services.api.routers import ')
    elif l.startswith('from routers.'):
        l = l.replace('from routers.', 'from defense.services.api.routers.')
    new_lines.append(l)

with open('src/defense/services/api/main.py', 'w') as f:
    f.write('\n'.join(new_lines))
