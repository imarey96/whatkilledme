FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    TRUST_PROXY=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py analyze.py tanks.py wcl_client.py ./
COPY static ./static

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

CMD ["python", "app.py"]
