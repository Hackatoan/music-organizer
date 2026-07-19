FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY organizer.py config.yaml ./
ENV DATA_DIR=/data
VOLUME /data
CMD ["python", "organizer.py", "run"]
