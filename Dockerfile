FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY flightrec ./flightrec
ENV HOST=0.0.0.0
EXPOSE 8080
CMD ["python", "-m", "flightrec", "serve"]
