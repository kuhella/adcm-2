FROM golang:1.26 AS go_builder
COPY ./go /code
WORKDIR /code
RUN sh -c "make"


FROM node:20.9.0-alpine AS ui_builder
ARG ADCM_VERSION
ENV ADCM_VERSION=$ADCM_VERSION
COPY ./adcm-web/app /code
WORKDIR /code
RUN . build.sh


FROM registry.red-soft.ru/ubi8/ubi-minimal:latest AS python_builder

RUN microdnf install -y --nodocs --setopt=install_weak_deps=0 \
        gcc \
        gcc-c++ \
        kernel-headers \
        libffi-devel \
        libxml2-devel \
        make \
        openldap-devel \
        openssl-devel \
        pcre-devel \
        python3.12 \
        python3.12-devel && \
    microdnf clean all

WORKDIR /adcm

RUN --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv venv -p /usr/bin/python3.12 /adcm/.venv && \
    UV_PROJECT_ENVIRONMENT=/adcm/.venv uv sync --group run --locked

RUN --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv venv -p /usr/bin/python3.12 /venv/2.16 && \
    UV_PROJECT_ENVIRONMENT=/venv/2.16 uv sync --group ansible --locked


FROM registry.red-soft.ru/ubi8/ubi-minimal:latest

RUN microdnf update -y && \
    microdnf install -y --nodocs --setopt=install_weak_deps=0 \
        bash \
        gnupg2 \
        nginx \
        openldap \
        openssh-clients \
        openssl \
        python3.12 \
#        rsync \
        runit \
        shadow-utils \
        sshpass \
        wget && \
    microdnf clean all && \
    rm -rf /var/cache/dnf
#
## rsync/crypto-policies drag in RED OS's glibc platform python (3.11) for its /usr/bin/python3
RUN rpm -e --nodeps python3 python3-libs

# Non-root runtime user. Writable state is relocated off root-owned paths (/run, /root) onto
# /adcm/data and the user's home. The uid/gid are build args so they are declared and stable: existing installs
# upgrading from a root-based image must `chown -R ${ADCM_UID}:${ADCM_GID}` their /adcm/data volume once.
ARG ADCM_UID=1001
ARG ADCM_GID=1001
RUN groupadd -g "${ADCM_GID}" adcm && \
    useradd -m -u "${ADCM_UID}" -g adcm -d /home/adcm -s /bin/sh adcm

COPY os/etc /etc
# Point each runit service's supervise/ dir at the ephemeral runtime dir: the
# service run-scripts stay root-owned, and only /adcm/run is writable. The
# target is a fixed path with no uid in it, so the image also works when the
# platform assigns an arbitrary runtime uid (e.g. OpenShift).
RUN for svc in /etc/sv/*/; do \
        ln -s "/adcm/run/runit/$(basename "${svc}")" "${svc}supervise"; \
    done
COPY --from=go_builder /code/bin/runstatus /adcm/go/bin/runstatus
COPY --from=ui_builder /wwwroot /adcm/wwwroot
COPY --from=python_builder /adcm/.venv /adcm/.venv
COPY --from=python_builder /venv/2.16 /venv/2.16
COPY --from=arenadata/ansible:2.16.4-python3.10 /root/.ansible/collections /usr/share/ansible/collections
COPY conf /adcm/conf
COPY python/ansible_collections/arenadata/adcm/plugins /usr/share/ansible/plugins
COPY python/ansible_collections/arenadata/adcm /usr/share/ansible/collections/ansible_collections/arenadata/adcm
COPY python /adcm/python

RUN ln -sf /usr/bin/python3.12 /usr/bin/python3 && \
    ln -sf /usr/bin/python3 /usr/bin/python && \
    ln -s /tmp/.ansible /home/adcm/.ansible  && \
    ln -s /adcm/python/application/scripts/manage_secrets.py /adcm/python/manage_secrets.py

# Hand only the runtime-writable paths to the non-root user; the enabled ssl vhost
# is written to /adcm/data (see make_nginx_default_config).
#   /adcm      - code, wwwroot/static, and /adcm/data
#   /adcm/run  - ephemeral runtime state (uwsgi pidfile + wsgi socket, runit
#                supervise dirs); mode 0700 (the supervise control FIFOs allow
#                signalling services); tmpfs it under a read-only rootfs
RUN mkdir -p /adcm/data/log /adcm/run && \
    chmod 700 /adcm/run && \
    chown -R adcm:adcm /adcm

RUN DJANGO_SETTINGS_MODULE=adcm.settings_setups.build /adcm/.venv/bin/python /adcm/python/manage.py collectstatic --noinput

ENV PYTHONPATH=/adcm/python
ENV HOME=/home/adcm
ENV LANG=en_US.UTF-8
# Everything ansible writes under ~/.ansible by default is rebased onto /tmp,
# so HOME needs no writable mount under a read-only rootfs and the ephemeral
# files stay off the data volume.The symlink covers `remote_tmp`
# for connection=local plays: it always expands literally to ~/.ansible/tmp and
# cannot be redirected globally without also breaking remote (ssh) targets.
ENV ANSIBLE_HOME=/tmp/.ansible
ARG ADCM_VERSION
ENV ADCM_VERSION=$ADCM_VERSION
WORKDIR /adcm
EXPOSE 8000
USER adcm
CMD ["/etc/startup.sh"]
