import os
import shutil
import sys

from pathlib import Path
from threading import Thread

import docker

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

# Sentinel objects that are distinct from None
_NOT_SET = object()

class Misconfiguration(Exception):
    """Exception that is raised when something is misconfigured in this file."""

_environments = ["production", "staging", "testing", "development"]
AGENT_ENV = os.environ.get("AGENT_ENV", "development")
if AGENT_ENV not in _environments:
    raise Misconfiguration(f"Set DJANGO_ENV to one of: {', '.join(_environments)}")

def setting(*, development, production, staging=_NOT_SET, testing=_NOT_SET):
    """Generate a setting depending on the AGENT_ENV and the arguments.

    This function is meant for static settings that depend on the AGENT_ENV. If the
    staging or testing arguments are left to their defaults, they will fall back to
    the production and development settings respectively.
    """
    if AGENT_ENV == "development" or (AGENT_ENV == "testing" and testing is _NOT_SET):
        return development
    if AGENT_ENV == "testing":
        return testing
    if AGENT_ENV == "production" or (AGENT_ENV == "staging" and staging is _NOT_SET):
        return production
    if AGENT_ENV == "staging":
        return staging
    raise Misconfiguration(f"Set AGENT_ENV to one of: {', '.join(_environments)}")


async def status(request):
    try:
        app_status = client.containers.get("peertube")
    except docker.errors.NotFound:
        app_status = {"error": "not_found"}
    try:
        nginx_status = client.containers.get("nginx")
    except docker.errors.NotFound:
        app_status = {"error": "not_found"}
    try:
        redis_status = client.containers.get("redis")
    except docker.errors.NotFound:
        app_status = {"error": "not_found"}
    try:
        postgres_status = client.containers.get("postgres")
    except docker.errors.NotFound:
        app_status = {"error": "not_found"}
    return JSONResponse({"app": app_status.attrs, "nginx": nginx_status.attrs, "redis": redis_status.attrs, "postgres": postgres_status.attrs, })


def startup():
    startup_thread = Thread(target=start)
    startup_thread.start()

def start():
    check_certbot_bootstrap()

    events_thread.start()

    network = get_network()

    try:
        postgres_status = client.containers.get("postgres")
        # One of created, restarting, running, removing, paused, exited, or dead
        # TODO: figure out what to do if restarting, removing
        if postgres_status.status in ["exited", "paused", "created"]:
            if postgres_status.attrs["State"]["ExitCode"] != 0:
                postgres_status.remove()
                start_postgres(network)
            else:
                postgres_status.start()
        if postgres_status.status == "dead":
            postgres_status.remove()
            start_postgres(network)
    except docker.errors.NotFound:
        start_postgres(network)

    try:
        redis_status = client.containers.get("redis")
        # One of created, restarting, running, removing, paused, exited, or dead
        # TODO: figure out what to do if restarting, removing
        if redis_status.status in ["exited", "paused", "created"]:
            if redis_status.attrs["State"]["ExitCode"] != 0:
                redis_status.remove()
                start_redis(network)
            else:
                redis_status.start()
        if redis_status.status == "dead":
            redis_status.remove()
            start_redis(network)
    except docker.errors.NotFound:
        start_redis(network)

    try:
        app_status = client.containers.get("peertube")
        # One of created, restarting, running, removing, paused, exited, or dead
        # TODO: figure out what to do if restarting, removing
        if app_status.status in ["exited", "paused", "created"]:
            if app_status.attrs["State"]["ExitCode"] != 0:
                app_status.remove()
                start_app(network)
            else:
                app_status.start()
        if app_status.status == "dead":
            app_status.remove()
            start_app(network)
    except docker.errors.NotFound:
        start_app(network)
    
    try:
        nginx_status = client.containers.get("nginx")
        # One of created, restarting, running, removing, paused, exited, or dead
        # TODO: figure out what to do if restarting, removing
        if nginx_status.status in ["exited", "paused", "created"]:
            if nginx_status.attrs["State"]["ExitCode"] != 0:
                nginx_status.remove()
                start_nginx(network)
            else:
                nginx_status.start()
        if nginx_status.status == "dead":
            nginx_status.remove()
            start_nginx(network)
    except docker.errors.NotFound:
        start_nginx(network)

def shutdown():
    docker_events.close()


def check_certbot_bootstrap():
    certbot_workdir = config_dir / "letsencrypt-var"
    cert_dir = certbot_dir / "live" / domain_name
    if (cert_dir / "fullchain.pem").exists() and (cert_dir / "privkey.pem").exists():
        return

    print("Letsencrypt needs to be bootstrapped")
    # Nothing can listen on port 80 while bootstrapping
    try:
        client.containers.get("nginx").stop()
    except docker.errors.NotFound:
        pass
    
    certbot_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    certbot_workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        client.containers.run(
            "certbot/certbot:v1.17.0",
            f"certonly -v -n {setting(development='--test-cert ', production='')}--agree-tos -m jelle@pingiun.com --standalone -d {domain_name} -d www.{domain_name}",
            name="certbot",
            stdout=True,
            stderr=True,
            ports={"80/tcp": "80"},
            volumes={
                str(certbot_dir): {"bind": "/etc/letsencrypt", "mode": "rw"},
                str(certbot_workdir): {"bind": "/var/lib/letsencrypt", "mode": "rw"},
            },
        )
        
    except docker.errors.ContainerError as e:
        sys.stdout.buffer.write(e.container.logs())
        print(e)
    finally:
        try:
            client.containers.get("certbot").remove()
        except docker.errors.DockerException:
            pass


def get_network():
    try:
        return client.networks.get("peertube")
    except docker.errors.NotFound:
        return create_network()


def create_network():
    return client.networks.create("peertube", driver="bridge")


def start_nginx(network: docker.models.networks.Network):
    nginxdir = config_dir / "nginx"
    nginxdir.mkdir(mode=0o770, parents=True, exist_ok=True)

    shutil.copyfile(
        Path(__file__).parent / "nginxconfig", nginxdir / "peertube.conf.template"
    )

    client.containers.run(
        "nginx:1.21.1",
        name="nginx",
        detach=True,
        network=network.id,
        ports={"80/tcp": "80/tcp", "443/tcp": "443"},
        volumes={
            "assets": {
                "bind": "/var/www/peertube/peertube-latest/client/dist",
                "mode": "ro",
            },
            str(config_dir / "peertube" / "data"): {
                "bind": "/var/www/peertube/storage",
                "mode": "ro",
            },
            str(nginxdir): {"bind": "/etc/nginx/templates", "mode": "ro"},
            str(certbot_dir): {"bind": "/etc/letsencrypt", "mode": "ro"},
        },
        environment={
            "PEERTUBE_HOST": "peertube:9000",
            "WEBSERVER_HOST": domain_name,
        },
    )


def start_postgres(network: docker.models.networks.Network):
    client.containers.run(
        "postgres:13.3",
        name="postgres",
        detach=True,
        network=network.id,
        environment={
            "POSTGRES_PASSWORD": "peertube",
            "POSTGRES_USER": "peertube",
            "POSTGRES_DB": "peertube",
        },
    )


def start_redis(network: docker.models.networks.Network):
    client.containers.run(
        "redis:6.2.4",
        name="redis",
        detach=True,
        network=network.id,
    )


def start_app(network: docker.models.networks.Network):
    client.containers.run(
        "chocobozzz/peertube:v3.3.0-buster",
        name="peertube",
        detach=True,
        network=network.id,
        volumes={
            "assets": {
                "bind": "/app/client/dist",
                "mode": "rw",
            },
            str(config_dir / "peertube" / "data"): {
                "bind": "/data",
                "mode": "rw",
            }
        },
        environment={
            "PEERTUBE_DB_PASSWORD": "peertube",
            "PEERTUBE_DB_USERNAME": "peertube",
            "POSTGRES_DB": "peertube",
            "PEERTUBE_DB_HOSTNAME": "postgres",
            "PEERTUBE_WEBSERVER_HOSTNAME": domain_name,
            "PEERTUBE_ADMIN_EMAIL": "jelle@pingiun.com",
            "PT_INITIAL_ROOT_PASSWORD": "secretpassword",
        },
    )


def events_check():
    for event in docker_events:
        print(event)
        if (
            event.get("status") == "die"
            and event.get("Actor", {}).get("Attributes", {}).get("name") == "peertube"
            and event.get("Actor", {}).get("Attributes", {}).get("exitCode") == "255"
        ):
            print("App closed with 255, restarting...")
            try:
                client.containers.get("peertube").remove()
            except docker.errors.NotFound:
                pass
            start_app(get_network())
        
        if (
            event.get("status") == "die"
            and event.get("Actor", {}).get("Attributes", {}).get("name") == "nginx"
            and event.get("Actor", {}).get("Attributes", {}).get("exitCode") == "1"
        ):
            print("nginx closed with 1, restarting...")
            try:
                client.containers.get("nginx").remove()
            except docker.errors.NotFound:
                pass
            start_nginx(get_network())


client = docker.client.from_env()
docker_events = client.events(decode=True)
events_thread = Thread(target=events_check)
config_dir = Path(os.getenv("APP_DIR", "/var/lib/peertube/"))
config_dir.mkdir(mode=0o770, parents=True, exist_ok=True)
certbot_dir = config_dir / "letsencrypt-etc"
domain_name = os.environ["DOMAIN_NAME"]
app = Starlette(
    debug=True,
    routes=[
        Route("/status", status),
    ],
    on_startup=[startup],
    on_shutdown=[shutdown],
)
