import os
import re

def process_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # Remove sys.path.insert blocks
    # Looking for patterns like:
    # _LIBS_PATH = os.path.join(...)
    # if _LIBS_PATH not in sys.path:
    #     sys.path.insert(...)
    # Or just sys.path.insert(...)
    
    # Simple strategy: just remove lines with sys.path.insert, _REPO_ROOT, _LIBS_ROOT, _LIBS_PATH that are about path manipulation.
    lines = content.split('\n')
    new_lines = []
    skip_next = False
    for i, line in enumerate(lines):
        if skip_next:
            if line.strip().startswith('sys.path.insert'):
                continue
            skip_next = False
            
        if '_REPO_ROOT' in line and ('os.path.abspath' in line or 'os.path.join' in line):
            continue
        if '_LIBS_ROOT' in line and ('os.path.abspath' in line or 'os.path.join' in line):
            continue
        if '_LIBS_PATH' in line and ('os.path.abspath' in line or 'os.path.join' in line):
            continue
        if 'if _LIBS_PATH not in sys.path:' in line or 'if _REPO_ROOT not in sys.path:' in line or 'if _LIBS_ROOT not in sys.path:' in line:
            skip_next = True
            continue
        if 'sys.path.insert' in line:
            continue
            
        # Rewrite imports
        # "from common." -> "from defense.libs.common."
        # "from dlq " -> "from defense.libs.dlq "
        # "from progress " -> "from defense.libs.progress "
        # "from libs." -> "from defense.libs."
        # "import libs." -> "import defense.libs."
        # "from schemas." -> "from defense.schemas."
        # "from schemas " -> "from defense.schemas "
        # "from services." -> "from defense.services."
        
        l = line
        if l.startswith('from common.') or ' from common.' in l:
            l = l.replace('from common.', 'from defense.libs.common.')
        if l.startswith('from dlq ') or ' from dlq ' in l:
            l = l.replace('from dlq ', 'from defense.libs.dlq ')
        if l.startswith('from progress ') or ' from progress ' in l:
            l = l.replace('from progress ', 'from defense.libs.progress ')
        if l.startswith('from libs.') or ' from libs.' in l:
            l = l.replace('from libs.', 'from defense.libs.')
        if l.startswith('from libs ') or ' from libs ' in l:
            l = l.replace('from libs ', 'from defense.libs ')
        if l.startswith('import libs') or ' import libs' in l:
            l = l.replace('import libs', 'import defense.libs')
        if l.startswith('from schemas.') or ' from schemas.' in l:
            l = l.replace('from schemas.', 'from defense.schemas.')
        if l.startswith('from schemas ') or ' from schemas ' in l:
            l = l.replace('from schemas ', 'from defense.schemas ')
        if l.startswith('from services.') or ' from services.' in l:
            l = l.replace('from services.', 'from defense.services.')
        
        # also rewrite internal API imports which assume they are at root
        if l.startswith('from routers.') or ' from routers.' in l:
            l = l.replace('from routers.', 'from defense.services.api.routers.')
        if l.startswith('from routers ') or ' from routers ' in l:
            l = l.replace('from routers ', 'from defense.services.api.routers ')
            
        # and builder / persistence / assembler which assume they are root
        if l.startswith('from builder '): l = l.replace('from builder ', 'from defense.services.workers.assembler.builder ')
        if l.startswith('from persistence '): l = l.replace('from persistence ', 'from defense.services.workers.assembler.persistence ')
        
        # also ingestion normalizer
        if l.startswith('from normalizer '): l = l.replace('from normalizer ', 'from defense.services.ingestion.normalizer ')
            
        new_lines.append(l)

    with open(filepath, 'w') as f:
        f.write('\n'.join(new_lines))

for root, _, files in os.walk('src/defense'):
    for file in files:
        if file.endswith('.py'):
            process_file(os.path.join(root, file))
