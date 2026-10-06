FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-cloud.txt cloud/constraints.txt /app/
RUN pip install --no-cache-dir -r requirements-cloud.txt -c constraints.txt \
    && groupadd --gid 1040 w2f && useradd --uid 1040 --gid 1040 --no-create-home w2f \
    && mkdir -p /var/lib/w2f && chown 1040:1040 /var/lib/w2f
COPY cloud/*.py /app/cloud/
COPY w2f_protocol/*.py /app/w2f_protocol/
ARG RELEASE=local
ENV W2F_RELEASE=${RELEASE}
LABEL org.opencontainers.image.revision=${RELEASE} w2f.project=v040
USER 1040:1040
EXPOSE 8080
CMD ["python", "-m", "cloud.server"]
