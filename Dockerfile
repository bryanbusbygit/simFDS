FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends graphviz \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /simFDS
COPY . .

# ensure simFDS is executable inside the container
RUN chmod +x bin/unix

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:$PORT"]
