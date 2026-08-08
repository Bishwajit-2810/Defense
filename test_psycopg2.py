import psycopg2
conn = psycopg2.connect("postgresql://postgres:postgres@localhost:5432/defense")
curr = conn.cursor()
try:
    curr.execute("SELECT %s", ())
except Exception as e:
    import traceback
    traceback.print_exc()
