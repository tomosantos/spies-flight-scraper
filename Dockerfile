FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
# routes.json and results.json are written to /app/result, mount a volume there to keep them
ENTRYPOINT ["python", "src/scraper.py"]
