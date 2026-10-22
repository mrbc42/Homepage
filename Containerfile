FROM docker.io/library/python:3.13-alpine

LABEL org.opencontainers.image.title="Home Base" \
      org.opencontainers.image.description="Colourful homelab start page with weather, headlines and app links" \
      org.opencontainers.image.source="https://github.com/mrbc42/homepage"

ENV PYTHONUNBUFFERED=1 \
    HOMEPAGE_DATA=/data \
    HOMEPAGE_PORT=8080

RUN adduser -D -H -u 1000 homepage && mkdir /data && chown homepage:homepage /data
COPY --chown=homepage:homepage app/ /app/

USER homepage
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "/app/server.py"]
