import cProfile
import pstats
import io
import time
from sqlalchemy import event

from app.db import engine
from app.queue import pipeline

query_count = {"SELECT": 0, "INSERT": 0, "UPDATE": 0, "DELETE": 0, "OTHER": 0, "total_time": 0.0}
query_times = []

@event.listens_for(engine, "before_cursor_execute")
def _before(conn, cursor, statement, parameters, context, executemany):
    conn.info.setdefault("query_start_time", []).append(time.perf_counter())
    verb = statement.strip().split(None, 1)[0].upper()
    query_count[verb] = query_count.get(verb, 0) + 1

@event.listens_for(engine, "after_cursor_execute")
def _after(conn, cursor, statement, parameters, context, executemany):
    start = conn.info["query_start_time"].pop(-1)
    elapsed = time.perf_counter() - start
    query_count["total_time"] += elapsed
    query_times.append((elapsed, statement[:120]))

wall_start = time.perf_counter()
profiler = cProfile.Profile()
profiler.enable()
summary = pipeline.run()
profiler.disable()
wall_elapsed = time.perf_counter() - wall_start

print("=== PIPELINE SUMMARY ===")
import json
print(json.dumps(summary, indent=2, default=str))

print("\n=== WALL CLOCK ===")
print(f"Total wall time: {wall_elapsed:.2f}s")

print("\n=== SQL QUERY COUNTS ===")
for k, v in query_count.items():
    print(f"{k}: {v}")
print(f"Total DB time (sum of query durations): {query_count['total_time']:.2f}s")
print(f"Total distinct query executions: {sum(v for k,v in query_count.items() if k not in ('total_time',))}")

query_times.sort(reverse=True)
print("\n=== TOP 15 SLOWEST INDIVIDUAL QUERIES ===")
for elapsed, stmt in query_times[:15]:
    print(f"{elapsed*1000:.1f}ms  {stmt}")

print("\n=== CPROFILE TOP 25 BY CUMULATIVE TIME ===")
s = io.StringIO()
ps = pstats.Stats(profiler, stream=s).sort_stats("cumulative")
ps.print_stats(25)
print(s.getvalue())

print("\n=== CPROFILE TOP 25 BY SELF (TOTTIME) ===")
s2 = io.StringIO()
ps2 = pstats.Stats(profiler, stream=s2).sort_stats("tottime")
ps2.print_stats(25)
print(s2.getvalue())
