from __future__ import annotations

import re
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).parents[1]
RELEASE_FILE = "docker-compose.release.yml"
SOURCE_FILE = "docker-compose.yml"
IMAGE_PREFIX = "${DRAMACLAW_IMAGE_PREFIX:-claymorelab}/"


def _compose() -> dict:
    return yaml.safe_load((REPOSITORY_ROOT / RELEASE_FILE).read_text())


def test_repository_ships_only_source_and_release_compose_files() -> None:
    variants = sorted(
        {p.name for p in REPOSITORY_ROOT.glob("docker-compose*.y*ml")}
        | {p.name for p in REPOSITORY_ROOT.glob("compose.y*ml")}
    )
    assert variants == ["docker-compose.release.yml", "docker-compose.yml"]


def test_compose_is_image_only_and_prefixed() -> None:
    services = _compose()["services"]
    assert set(services) == {"api", "web"}
    for name, service in services.items():
        assert "build" not in service, f"{name} must not carry a build block"
        assert "pull_policy" not in service, f"{name} must not set pull_policy"
        assert service["image"].startswith(IMAGE_PREFIX), name
        assert service.get("restart") == "unless-stopped", name


def test_ce_images_share_one_version_variable() -> None:
    # The default moves with every release (the packaging workflow opens a PR that bumps it),
    # so assert the shape and that api and web move together — never the literal version.
    services = _compose()["services"]
    versions = set()
    for name, repository in (("api", "dramaclaw"), ("web", "dramaclaw-frontend")):
        image = services[name]["image"]
        match = re.fullmatch(
            re.escape(IMAGE_PREFIX + repository) + r":\$\{DRAMACLAW_VERSION:-(\d+\.\d+\.\d+)\}",
            image,
        )
        assert match, image
        versions.add(match.group(1))
    assert len(versions) == 1, versions


def test_api_persists_generated_media_in_ce_data_volume() -> None:
    api = _compose()["services"]["api"]
    assert api["environment"] | {
        "NOVELVIDEO_DATA_ROOT": "/data",
        "NOVELVIDEO_OUTPUT_DIR": "/data/output",
        "NOVELVIDEO_STATE_DIR": "/data/state",
        "NOVELVIDEO_RUNTIME_DIR": "/data/runtime",
    } == api["environment"]
    assert "ce-data:/data" in api["volumes"]


def test_compose_ships_no_model_gateway() -> None:
    compose = _compose()
    api = compose["services"]["api"]
    assert not any(key.startswith("NEWAPI_") for key in api["environment"])
    assert "depends_on" not in api
    assert "newapi" not in (REPOSITORY_ROOT / RELEASE_FILE).read_text().lower()


def test_compose_pins_env_file_long_syntax_ports_and_volumes() -> None:
    compose = _compose()
    api = compose["services"]["api"]
    assert api["env_file"] == [{"path": ".env", "required": False}]
    assert api["ports"] == ["${ST_API_PORT:-8780}:8780"]
    assert compose["services"]["web"]["ports"] == ["${ST_WEB_PORT:-8080}:80"]
    assert set(compose["volumes"]) == {"ce-data"}


def test_source_file_extends_release_and_builds_every_service() -> None:
    source = yaml.safe_load((REPOSITORY_ROOT / SOURCE_FILE).read_text())

    assert set(source) == {"services", "volumes"}
    services = source["services"]
    assert set(services) == {"api", "web"}
    for name, service in services.items():
        assert set(service) == {"extends", "image", "build"}, f"{name} keys: {set(service)}"
        assert service["extends"] == {"file": RELEASE_FILE, "service": name}

    assert services["api"]["image"] == "dramaclaw-local/api"
    assert services["web"]["image"] == "dramaclaw-local/web"

    assert services["api"]["build"] == {
        "context": ".",
        "dockerfile": "Dockerfile",
        "args": {"INSTALL_WORLD": "${INSTALL_WORLD:-0}"},
    }
    assert services["web"]["build"] == {"context": "./frontend", "dockerfile": "Dockerfile"}

    assert set(source["volumes"]) == {"ce-data"}


def test_source_file_never_mentions_release_versions() -> None:
    source_text = (REPOSITORY_ROOT / SOURCE_FILE).read_text()

    assert "DRAMACLAW_VERSION" not in source_text


def test_env_example_configures_data_root_instead_of_individual_directories() -> None:
    env_example = (REPOSITORY_ROOT / ".env.example").read_text()

    assert re.search(r"^NOVELVIDEO_OUTPUT_DIR=", env_example, re.MULTILINE) is None
    assert "# NOVELVIDEO_DATA_ROOT=" in env_example


def test_env_example_documents_image_variables() -> None:
    env_example = (REPOSITORY_ROOT / ".env.example").read_text()

    for key in ("DRAMACLAW_IMAGE_PREFIX", "DRAMACLAW_VERSION"):
        assert re.search(rf"^# {key}=", env_example, re.MULTILINE), key
