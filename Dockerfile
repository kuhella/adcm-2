FROM golang:1.23 AS go_builder
COPY ./go /code
WORKDIR /code
RUN sh -c "make"


FROM node:20.9.0-alpine AS ui_builder
ARG ADCM_VERSION
ENV ADCM_VERSION=$ADCM_VERSION
COPY ./adcm-web/app /code
WORKDIR /code
RUN . build.sh


FROM python:3.12-alpine3.23 AS python_builder

RUN apk add --no-cache --virtual .build-deps \
    build-base \
    linux-headers \
    openldap-dev

WORKDIR /adcm

# Prepare ADCM venv (Python 3.12, from system)
RUN --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --python 3.12 --group run --locked

# Prepare self-contained Ansible venv (Python 3.10)
# 1. Copy pre-built ansible venv and Python 3.10 runtime
COPY --from=hub.adsw.io/ansible/ansible:2.16.4-python3.10-develop /venv/2.16 /venv/2.16
COPY --from=hub.adsw.io/ansible/ansible:2.16.4-python3.10-develop /usr/local/bin/python3.10 /tmp/python3.10-real
COPY --from=hub.adsw.io/ansible/ansible:2.16.4-python3.10-develop /usr/local/lib/python3.10 /venv/2.16/lib/python3.10
COPY --from=hub.adsw.io/ansible/ansible:2.16.4-python3.10-develop /usr/local/lib/libpython3.10.so* /venv/2.16/lib/
COPY --from=hub.adsw.io/ansible/ansible:2.16.4-python3.10-develop /usr/local/include/python3.10 /venv/2.16/include/python3.10
# 2. Replace venv symlinks with embedded Python 3.10 binary
RUN rm -f /venv/2.16/bin/python /venv/2.16/bin/python3 /venv/2.16/bin/python3.10 && \
    mv /tmp/python3.10-real /venv/2.16/bin/python3.10 && \
    chmod +x /venv/2.16/bin/python3.10 && \
    ln -s python3.10 /venv/2.16/bin/python3 && \
    ln -s python3 /venv/2.16/bin/python && \
    sed -i 's|^home = .*|home = /venv/2.16/bin|' /venv/2.16/pyvenv.cfg
# 3. Install ADCM modules into the Ansible venv
RUN --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
    --mount=type=bind,source=ansible-2.16-python3.10-dependencies.txt,target=ansible-2.16-python3.10-dependencies.txt \
    LD_LIBRARY_PATH=/venv/2.16/lib \
    uv pip install --python /venv/2.16/bin/python --no-cache -r ansible-2.16-python3.10-dependencies.txt

RUN python3.12 -m pip uninstall -y pip && \
    rm -rf /root/.cache/pip


FROM python:3.12-alpine3.23

RUN apk update && \
    apk upgrade && \
    apk add --no-cache \
    bash \
    gnupg \
    nginx \
    openldap \
    openssh-client \
    openssh-keygen \
    openssl \
    rsync \
    runit \
    sshpass && \
    apk cache clean --purge

COPY os/etc /etc
COPY os/etc/crontabs/root /var/spool/cron/crontabs/root
COPY --from=go_builder /code/bin/runstatus /adcm/go/bin/runstatus
COPY --from=ui_builder /wwwroot /adcm/wwwroot
# ADCM venv (Python 3.12, uses system Python from base image)
COPY --from=python_builder /adcm/.venv /adcm/.venv
# Ansible venv (Python 3.10, fully self-contained)
COPY --from=python_builder /venv/2.16 /venv/2.16
COPY --from=hub.adsw.io/ansible/ansible:2.16.4-python3.10-develop /root/.ansible/collections /root/.ansible/collections
COPY conf /adcm/conf
COPY python/ansible_collections/arenadata/adcm/plugins /usr/share/ansible/plugins
COPY python/ansible_collections/arenadata/adcm /root/.ansible/collections/ansible_collections/arenadata/adcm
COPY python /adcm/python

RUN ln -s -f /usr/local/bin/python3 /usr/bin/python3 && \
    ln -s -f /usr/bin/python3 /usr/bin/python

RUN ln -s /adcm/python/application/scripts/manage_secrets.py /adcm/python/manage_secrets.py

RUN mkdir -p /adcm/data/log

RUN DJANGO_SETTINGS_MODULE=adcm.settings_setups.build /adcm/.venv/bin/python /adcm/python/manage.py collectstatic --noinput

ENV PYTHONPATH=/adcm/python

ARG ADCM_VERSION
ENV ADCM_VERSION=$ADCM_VERSION
EXPOSE 8000
CMD ["/etc/startup.sh"]
