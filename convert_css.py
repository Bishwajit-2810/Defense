import re

with open('dashboard_legacy/styles.css', 'r') as f:
    css = f.read()

# Define the new color tokens
# We'll replace the --blue-* tokens with green ones, and define the rest using light-dark()

new_root = """:root {
  /* Green ramp */
  --green-50:  #ecfdf5;
  --green-100: #d1fae5;
  --green-200: #a7f3d0;
  --green-300: #6ee7b7;
  --green-400: #34d399;
  --green-500: #10b981;
  --green-600: #059669;
  --green-700: #047857;
  --green-800: #065f46;
  --green-900: #064e3b;
  --green-950: #022c22;

  /* ---- Data colours ---- */
  --color-positive: #059669; 
  --color-negative: #dc2626; 
  --color-neutral:  #94a3b8; 
  --color-mixed:    #d97706; 

  /* Facebook reaction types */
  --color-like:   #1d4ed8; 
  --color-love:   #db2777;
  --color-haha:   #d97706;
  --color-wow:    #7c3aed;
  --color-sad:    #0891b2; 
  --color-angry:  #dc2626;
  --color-care:   #ea580c;

  /* ---- Surfaces ---- */
  --bg-page:      light-dark(#ffffff, #000000);
  --bg-card:      light-dark(#ffffff, #09090b);
  --bg-header:    light-dark(#ffffff, #09090b);
  --bg-nav:       light-dark(#f1f5f9, #121214);
  --bg-table-row: light-dark(#f8fafc, #121214);
  --bg-modal:     light-dark(#ffffff, #09090b);
  --bg-overlay:   light-dark(rgba(0, 0, 0, 0.2), rgba(0, 0, 0, 0.7));
  --bg-input:     light-dark(#ffffff, #09090b);
  --bg-status-bar: light-dark(#f1f5f9, #121214);
  --bg-subtle:    light-dark(#f8fafc, #121214);

  /* ---- Text ---- */
  --text-primary:   light-dark(#0f172a, #f8fafc);
  --text-secondary: light-dark(#475569, #cbd5e1);
  --text-inverse:   light-dark(#ffffff, #000000);
  --text-on-chrome: light-dark(#0f172a, #f8fafc);
  --text-muted:     light-dark(#64748b, #94a3b8);
  --text-on-accent: light-dark(#ffffff, #000000);

  /* ---- Lines & shape ---- */
  --border-color:   light-dark(#e2e8f0, #27272a);
  --border-strong:  light-dark(#cbd5e1, #3f3f46);
  --border-radius:  8px;
  --border-radius-lg: 12px;

  --shadow-sm:  light-dark(0 1px 2px 0 rgba(0,0,0,0.05), 0 1px 2px 0 rgba(0,0,0,0.35));
  --shadow-md:  light-dark(0 4px 6px -1px rgba(0,0,0,0.1), 0 4px 8px -2px rgba(0,0,0,0.45));
  --shadow-lg:  light-dark(0 10px 15px -3px rgba(0,0,0,0.1), 0 12px 20px -6px rgba(0,0,0,0.5));
  --shadow-xl:  light-dark(0 20px 25px -5px rgba(0,0,0,0.1), 0 24px 32px -8px rgba(0,0,0,0.55));
  --focus-ring: light-dark(0 0 0 3px rgba(16, 185, 129, 0.28), 0 0 0 3px rgba(16, 185, 129, 0.35));

  /* ---- Interaction ---- */
  --accent:         light-dark(var(--green-600), var(--green-500));
  --accent-hover:   light-dark(var(--green-700), var(--green-400));
  --accent-active:  light-dark(var(--green-800), var(--green-300));
  --accent-light:   light-dark(var(--green-50), #064e3b);
  --accent-border:  light-dark(var(--green-200), #065f46);

  --danger:         light-dark(#dc2626, #f87171);
  --danger-hover:   light-dark(#b91c1c, #fca5a5);
  --success:        light-dark(#059669, #34d399);
  --warning:        light-dark(#d97706, #fbbf24);
  --info:           light-dark(var(--green-600), var(--green-500));

  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  --font-mono: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;

  --transition: 150ms ease;

  color-scheme: light dark;
}
"""

# Find the :root block and the @media (prefers-color-scheme: dark) block to remove
root_pattern = re.compile(r':root\s*\{.*?\n\}\n', re.DOTALL)
media_pattern = re.compile(r'@media\s*\(prefers-color-scheme:\s*dark\)\s*\{.*?\n\}\n', re.DOTALL)

css = re.sub(root_pattern, new_root + '\n', css, count=1)
css = re.sub(media_pattern, '', css, count=1)

with open('dashboard/src/index.css', 'w') as f:
    f.write(css)

print("CSS converted successfully.")
