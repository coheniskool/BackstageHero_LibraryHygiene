# BackstageHero resolver - tiny FastAPI service, SQLite on a mounted volume.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py app.py suggest.py ./

# SQLite file lives on a mounted volume so data survives container rebuilds.
ENV BACKSTAGEHERO_DB=/data/resolver.sqlite3
VOLUME ["/data"]

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
