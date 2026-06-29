# Arenadata Cluster Manager

That is Arenadata Cluster Manager Project aka Chapelnik

# Documentation

[ArenaData ADCM Documentation](http://docs.arenadata.io/adcm/)

# Develop

All standard Django commands are available.

Run dev server for the first time with these commands:
1. `manage.py migrate`
2. `init_db.py`
3. `manage.py upgraderole`
4. `manage.py runserver --insecure`

Re-run them when needed/applicable.

# Sources

## Dirs

* assemble - information about the way we build product
* python - core django modules and python functions
* docs 
* go - golang part of application. There is a status server here now.
* test 
* spec - specification in form of Sphinx RST 
* adcm-web - UI source

# Build logic

There is a Makefile in repo. It could be used for building application.

## Fast start with make

You have to have GNU Make on your host and Docker daemon accessible for a user. Also, you have to have access to ci.arenadata.io

```sh
# Clone repo
git clone https://github.com/arenadata/adcm

cd adcm

# Run build process for current architecture
make build
```

That will be an image hub.adsw.io/adcm/adcm:<branch_name> as a result of the operation above.

## Makefile description

Makefile has self-documented help message. Just type.

```sh
$ make
buildbaseimage                 Build base image for ADCM's container. That is alpine with all packages.
build                          Build final docker image and all depended targets except baseimage.
buildjs                        Build client side js/html/css in directory wwwroot
buildss                        Build status server
clean                          Cleanup. Just a cleanup.
describe                       Create .version file with output of describe
help                           Shows that help
build2js                       For new design and api v2: Build client side js/html/css in directory wwwroot
build2                         For new design and api v2: Build final docker image and all depended targets except baseimage
```

And check out the description for every operation available.

## Pre-commit hook

We are using black, pylint and pre-commit to care about code formatting and linting.

So you have to install pre-commit hook before you do something with code.

``` sh
pip install pre-commit # Or do it with your preffered way to install pip packages
pre-commit install
```

After this you will see invocation of black and pylint on every commit.

## Running ADCM in Docker

_PostgreSQL 13 or newer is required._

A `docker-compose.yaml` is provided at the repository root. It starts ADCM together with a PostgreSQL 14 instance.

### Quick start

```shell
docker compose up -d
```

ADCM will be available at `http://localhost:8000` once the containers are ready.

To stop and remove the containers:

```shell
docker compose down
```

To also remove the data volumes:

```shell
docker compose down -v
```

### Using a custom ADCM image

If you built a local image with `make build`, override the image in the compose file:

```shell
ADCM_IMAGE=hub.adsw.io/adcm/adcm:my_branch docker compose up -d
```

Or edit the `image` field in `docker-compose.yaml` directly.

### Using an existing PostgreSQL instance

If you already have a running PostgreSQL server, you can start just the ADCM container:

```shell
docker run -d --restart=always -p 8000:8000 -v /opt/adcm:/adcm/data \
  -e DB_HOST="DATABASE_HOSTNAME_OR_IP_ADDRESS" -e DB_PORT="DATABASE_TCP_PORT" \
  -e DB_USER="DATABASE_USERNAME" -e DB_NAME="DATABASE_NAME" \
  -e DB_PASS="DATABASE_USER_PASSWORD" --name adcm hub.arenadata.io/adcm/adcm:latest
```

All database environment variables (`DB_HOST`, `DB_USER`, `DB_PASS`, `DB_NAME`) are mandatory. `DB_PORT` is optional and defaults to `5432`.

### Set log level

Add `LOG_LEVEL` to the `environment` section in `docker-compose.yaml` or pass it via the command line:

```shell
LOG_LEVEL=INFO docker compose up -d
```

Valid choices: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (defaults to `ERROR`).
