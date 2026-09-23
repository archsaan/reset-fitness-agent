# Same reasoning as the main project's Dockerfile — Cloud Run needs a
# container image, and never hardcodes a port (Cloud Run injects $PORT).

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY routers ./routers
COPY tribe_app ./tribe_app
COPY pfc ./pfc
COPY main.py .

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
