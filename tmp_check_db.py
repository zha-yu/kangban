import psycopg
conn = psycopg.connect("postgresql://silvermate:silvermate@localhost:5432/postgres", autocommit=True)
with conn.cursor() as cur:
    cur.execute("SELECT datname FROM pg_database WHERE datname LIKE 'silvermate%'")
    for row in cur.fetchall():
        print("DB:", row[0])
conn.close()
