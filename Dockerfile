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


FROM registry.red-soft.ru/ubi8/python-313:3.13 AS python_builder

USER 0

# Toolchain for the compiled extensions in the venvs below (uwsgi, python-ldap,
# psycopg). Both venvs are copied verbatim into the runtime, so they are built
# here against the same libc and the same interpreters the runtime installs.
RUN dnf install -y --setopt=install_weak_deps=False --setopt=tsflags=nodocs \
        gcc \
        make \
        openldap-devel \
        python3.10 \
        python3.10-devel \
        python3.12 \
        python3.12-devel && \
    dnf clean all

# ADCM does not support the base image's 3.13 yet (pyproject: requires-python
# <3.13), so the app runs on the distro's 3.12 and ansible 2.16 on its 3.10.
# Both are RED OS rpms installed at identical paths in the runtime stage, so the
# venvs resolve there; a downloaded standalone build would not, hence no fallback.
ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /adcm

# Prepare venv Python 3.12 for ADCM
RUN --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --python /usr/bin/python3.12 --group run --locked

# Prepare venv Python 3.10 for Ansible 2.16
RUN --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
    --mount=type=bind,source=ansible-2.16-python3.10-dependencies.txt,target=ansible-2.16-python3.10-dependencies.txt \
    uv venv -p /usr/bin/python3.10 /venv/2.16 && \
    uv pip install -p /venv/2.16/bin/python -r ansible-2.16-python3.10-dependencies.txt


FROM registry.red-soft.ru/ubi8/python-313:3.13

USER 0

RUN dnf -y upgrade && \
    dnf install -y --setopt=install_weak_deps=False --setopt=tsflags=nodocs \
        bash \
        gnupg2 \
        nginx \
        openldap \
        openssh-clients \
        openssl \
        python3.10 \
        python3.12 \
        rsync \
        runit \
        shadow-utils \
        sshpass && \
    dnf clean all && \
    rm -rf /var/cache/dnf

# The base image is an s2i/OpenShift builder image: it puts its own Python 3.13
# venv first on PATH and auto-sources it in every bash shell. ADCM invokes its
# venvs by absolute path, but ansible subprocesses inherit this PATH
# (cm.legacy.utils.get_env_with_venv_path), so reset it to the plain OS one to
# keep 3.13 out of ansible's interpreter discovery.
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENV BASH_ENV=
ENV ENV=

# Non-root runtime user. Writable state is relocated off root-owned paths (/run, /root) onto
# /adcm/data and the user's home. The uid/gid are build args so they are declared and stable: existing installs
# upgrading from a root-based image must `chown -R ${ADCM_UID}:${ADCM_GID}` their /adcm/data volume once.
ARG ADCM_UID=1001
ARG ADCM_GID=1001
# The base image already parks a placeholder `default` user on uid 1001; drop it
# so the uid/gid below stay the pair existing installs chowned their volume to.
RUN userdel default && \
    groupadd -g "${ADCM_GID}" adcm && \
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
# Collections only. That image is Alpine, so its musl-linked venv cannot run
# here; ansible-core itself comes from the 3.10 venv built above.
COPY --from=arenadata/ansible:2.16.4-python3.10 /root/.ansible/collections /usr/share/ansible/collections
COPY conf /adcm/conf
COPY python/ansible_collections/arenadata/adcm/plugins /usr/share/ansible/plugins
COPY python/ansible_collections/arenadata/adcm /usr/share/ansible/collections/ansible_collections/arenadata/adcm
COPY python /adcm/python

# `python3` keeps pointing at the distro interpreter: dnf runs on it.
RUN ln -s -f /usr/bin/python3 /usr/bin/python && \
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
# Drop the base image's s2i wrapper so ADCM's startup script is PID 1.
ENTRYPOINT []
CMD ["/etc/startup.sh"]
