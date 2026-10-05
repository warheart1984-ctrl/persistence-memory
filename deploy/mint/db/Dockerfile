# Postgres with the configuration baked in (no host bind mounts needed).
# Pin the base by digest once you have chosen one:  docker build --build-arg POSTGRES_IMAGE=postgres:16@sha256:<digest> ...
ARG POSTGRES_IMAGE=postgres:16
FROM ${POSTGRES_IMAGE}

COPY postgresql.conf pg_hba.conf pg_ident.conf /etc/postgresql/
COPY initdb/ /docker-entrypoint-initdb.d/

RUN chown postgres:postgres /etc/postgresql/postgresql.conf /etc/postgresql/pg_hba.conf /etc/postgresql/pg_ident.conf \
 && chmod 640 /etc/postgresql/postgresql.conf /etc/postgresql/pg_hba.conf /etc/postgresql/pg_ident.conf \
 && chmod 755 /docker-entrypoint-initdb.d/*.sh
