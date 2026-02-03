FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY chomik.py .
COPY app.py .

RUN mkdir -p /app/downloads

EXPOSE 5000

ENV DOWNLOAD_FOLDER=/app/downloads

CMD ["python", "app.py"]
