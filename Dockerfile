FROM python:3.12-slim
WORKDIR /app

# Dedicated non-root user the app actually runs as (see entrypoint). Fixed
# uid/gid so ownership matches predictably regardless of image rebuilds.
RUN groupadd -g 1000 appuser \
    && useradd -u 1000 -g appuser -M -s /usr/sbin/nologin appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY organizer.py config.yaml ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV DATA_DIR=/data
RUN mkdir -p /data && chown -R appuser:appuser /app /data
VOLUME /data

# Stay root here: the entrypoint chowns /data if the bind-mounted host
# directory needs it, then drops to appuser before exec'ing the real command.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "organizer.py", "run"]
