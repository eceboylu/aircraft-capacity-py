import time
from app.queue import pipeline

t0 = time.time()
result = pipeline.run()
elapsed = time.time() - t0
print("PIPELINE_RESULT:", result)
print("WALL_CLOCK_SECONDS:", elapsed)
