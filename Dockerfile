FROM postgres:16-alpine

# Install AGE and pgvector
RUN apk add --no-cache \
    build-base \
    git \
    postgresql-dev \
    && git clone --depth 1 --branch AGE-Postgres-16 https://github.com/apache/age.git /tmp/age \
    && cd /tmp/age \
    && make PG_CONFIG=/usr/local/pgsql/bin/pg_config \
    && make install PG_CONFIG=/usr/local/pgsql/bin/pg_config \
    && cd / \
    && rm -rf /tmp/age \
    && apk del build-base postgresql-dev

# Install pgvector
RUN git clone --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && cd /tmp/pgvector \
    && make \
    && make install \
    && cd / \
    && rm -rf /tmp/pgvector

COPY example/init.sql /docker-entrypoint-initdb.d/01-init.sql

EXPOSE 5432
CMD ["postgres"]
