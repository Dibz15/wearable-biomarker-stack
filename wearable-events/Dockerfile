FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY schema.sql .

EXPOSE 8080

# Deliberately a single process (no --workers): the background scheduler
# and calendar_points_lock both assume it - see docs/REFERENCE.md.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
