from contextlib import suppress, contextmanager
from dataclasses import dataclass
from typing import Tuple, Callable, Collection, Optional, Any
from warnings import warn

import allure
from _pytest.fixtures import SubRequest
from adcm_client.util.wait import wait_for_url
from docker import DockerClient
from docker.errors import APIError
from docker.models.containers import Container
from requests import ReadTimeout

from adcm_pytest_plugin.docker.adcm import ADCM, ADCMWithPostgres
from adcm_pytest_plugin.docker.postgresql import PostgreSQL
from adcm_pytest_plugin.docker.utils import remove_container_volumes
from adcm_pytest_plugin.utils import random_string, retry

CONTAINER_START_RETRY_COUNT = 20


@contextmanager
def _add_step_on_error():
    try:
        yield
    except Exception as err:
        with allure.step(f"[ERROR] {err}"):
            ...


class RetryCountExceeded(Exception):
    """Raise when container restart count is exceeded"""


@dataclass
class Stages:
    prepare_image: Collection[Callable] = ()
    # second argument is the current step value of kwargs dict
    prepare_run_arguments: Collection[Callable[["ADCMLauncher", dict], dict]] = ()
    after_run: Collection[Callable[["ADCMLauncher"], None]] = ()
    pre_stop: Collection[Callable[["ADCMLauncher", SubRequest], None]] = ()
    on_cleanup: Collection[Callable[["ADCMLauncher"], None]] = ()


class ADCMLauncher:

    _adcm_class = ADCM
    _local_repository = "local/adcminit"

    def __init__(self, adcm_image: Tuple[str, str], docker_client: DockerClient, bind_ip: str, stages: Stages, **_):
        self._adcm_base_repository, self._adcm_base_tag = adcm_image
        self._docker: DockerClient = docker_client
        self._stages = stages
        # Use this dictionary to store information required between various stages and steps
        self._stages_info = {}

        self.bind_ip = bind_ip
        # this image is the one containers are started from, it can be changed during preparation / at runtime
        self.image_name: str = f"{self._adcm_base_repository}:{self._adcm_base_tag}"
        self.adcm: Optional[ADCM] = None

    def _prepare_default_kwargs(self) -> dict:
        return {"remove": True}

    def _run_adcm_container(self, kwargs_mutator: Callable[[dict], dict] = lambda d: d) -> Container:
        for _ in range(CONTAINER_START_RETRY_COUNT):
            kwargs = self._prepare_default_kwargs()
            for step in self._stages.prepare_run_arguments:
                kwargs.update(step(self, {**kwargs}))

            try:
                return self._docker.containers.run(image=self.image_name, detach=True, **kwargs_mutator({**kwargs}))
            except APIError as err:
                if (
                    "failed: port is already allocated" in err.explanation
                    or "bind: address already in use" in err.explanation  # noqa: W503
                    or "bind: cannot assign requested address" in err.explanation  # noqa: W503
                ):
                    continue
                else:
                    raise err

        raise RetryCountExceeded(f"Unable to start container after {CONTAINER_START_RETRY_COUNT} retries")

    def _wait_adcm_container_init(self) -> None:
        timeout = 300
        url = f"{self.adcm.url}/api/v1/"
        with allure.step(f"Waiting for ADCM API on {url}"):
            if not wait_for_url(url, timeout):
                additional_message = ""
                try:
                    self.adcm.container.kill()
                except APIError:
                    additional_message = "\nWARNING: Failed to kill docker container. Try to remove it by hand"
                raise TimeoutError(f"ADCM API has not responded in {timeout} seconds{additional_message}")

    @allure.step("Prepare ADCM")
    def prepare(self):
        if not self._stages.prepare_image:
            return

        container = self._run_adcm_container()
        self.adcm = self._adcm_class(container, ip=self.bind_ip, https=False)
        self._wait_adcm_container_init()

        for step in self._stages.prepare_image:
            step(self)

        tag = random_string()
        with allure.step(f"Commit initialized ADCM container to image {self._local_repository}:{tag}"):
            container.commit(repository=self._local_repository, tag=tag)

        self.adcm.container.stop()

        self.image_name = f"{self._local_repository}:{tag}"
        self.adcm = None

    @allure.step("Run ADCM")
    def run(self, *, run_arguments_mutator: Callable[[dict], dict] = lambda x: x):
        container = self._run_adcm_container(run_arguments_mutator)
        is_https = "8443/tcp" in container.ports
        self.adcm = self._adcm_class(container, ip=self.bind_ip, https=is_https)
        self._wait_adcm_container_init()

        for step in self._stages.after_run:
            step(self)

    @allure.step("Stop ADCM")
    def stop(self, request: SubRequest):
        for step in self._stages.pre_stop:
            with _add_step_on_error():
                with allure.step(f'Running "{step.__name__}"'):
                    step(self, request)

        with suppress(ReadTimeout):
            self.adcm.container.stop()

        remove_container_volumes(self.adcm.container)

    @allure.step("ADCM Cleanup")
    def cleanup(self):
        for step in self._stages.on_cleanup:
            with _add_step_on_error():
                with allure.step(f'Running "{step.__name__}"'):
                    step(self)

    def add_step_fact(self, key: str, value: Any) -> None:
        if key in self._stages_info:
            warn(f"Fact by key '{key}' is already presented in stages info dict, it'll be rewritten")

        self._stages_info[key] = value

    def get_step_fact(self, key: str) -> Optional[Any]:
        return self._stages_info.get(key)


class ADCMWithPostgresLauncher(ADCMLauncher):

    _adcm_class = ADCMWithPostgres

    def __init__(self, adcm_image: Tuple[str, str], docker_client: DockerClient, bind_ip: str, stages: Stages, **_):
        super().__init__(adcm_image=adcm_image, docker_client=docker_client, bind_ip=bind_ip, stages=stages)
        self.postgres: Optional[PostgreSQL] = None

    def _run_postgres(self):
        # TODO add customization ??
        repo, tag = "postgres", "latest"

        postgres_image = self._docker.images.pull(repository=repo, tag=tag)

        name = f"db-{random_string(6)}"

        with allure.step(f"Launch container with Postgres {tag} from repository {repo}"):
            container: Container = self._docker.containers.run(
                image=postgres_image.id,
                name=name,
                environment={
                    "POSTGRES_PASSWORD": "postgres",
                    "POSTGRES_ADCM_PASS": "password",
                },
                network="bridge",
                remove=True,
                detach=True,
            )

        with allure.step("Wait until container is started properly"):
            wait_kwargs = {"attempts_": 30, "wait_between_": 0.2, "err_type_": RuntimeError}
            with allure.step("Wait DB is up"):
                retry(
                    lambda: "database system is ready to accept connections" in container.logs(tail=15).decode("utf-8"),
                    err_message_=lambda: f"Failed to start container:\n{container.logs().decode('utf-8').splitlines()}",
                    **wait_kwargs,
                )
            with allure.step("Wait psql"):
                retry(
                    lambda: container.exec_run(["psql", "--username", "postgres", "-c", "\\dt"]).exit_code == 0,
                    err_message_=lambda: f"psql is not available\n{container.logs().decode('utf-8').splitlines()}",
                    **wait_kwargs,
                )

        with allure.step("Prepare database"):
            for statement in (
                "CREATE USER adcm WITH ENCRYPTED PASSWORD 'password';",
                "CREATE DATABASE adcm OWNER adcm;",
                "ALTER USER adcm CREATEDB;",
            ):
                with allure.step(f"Run '{statement}' in Postgres container"):
                    result = container.exec_run(
                        ["psql", "--username", "postgres", "--dbname", "postgres", "-c", statement]
                    )

                    if result.exit_code != 0:
                        raise RuntimeError(f"Failed to execute statement:\n{result.output.decode()}")

        return container

    def _prepare_default_kwargs(self) -> dict:
        postgres_ip = self._docker.api.inspect_container(self.postgres.container.id)["NetworkSettings"]["IPAddress"]
        return {
            **super()._prepare_default_kwargs(),
            "network": "bridge",
            "environment": {
                "POSTGRES_ADCM_PASS": "password",
                "DB_NAME": "adcm",
                "DB_USER": "adcm",
                "DB_HOST": postgres_ip,
                "DB_PORT": "5432",
            },
        }

    @allure.step("Prepare ADCM")
    def prepare(self):
        self.postgres = PostgreSQL(container=self._run_postgres())

        super().prepare()

    @allure.step("ADCM cleanup")
    def cleanup(self):
        super().cleanup()

        self.postgres.container.stop()
