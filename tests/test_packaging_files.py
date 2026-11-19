"""Small facts about what the builds and the image ship, checked in the repo."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_build_ships_the_third_party_notice_with_the_licences():
    assert (ROOT / "THIRD_PARTY.md").is_file()
    for spec in ("packaging/couchelephant.spec", "packaging/macos/menubar.spec"):
        assert "THIRD_PARTY.md" in (ROOT / spec).read_text(), spec


def test_the_docker_health_check_follows_the_port_setting():
    line = [ln for ln in (ROOT / "Dockerfile").read_text().splitlines() if "healthz" in ln][0]
    assert "COUCHELEPHANT_PORT" in line


def test_time_zone_data_is_pinned_for_the_frozen_builds():
    assert any(ln.startswith("tzdata==") for ln in (ROOT / "requirements.txt").read_text().splitlines())


def test_the_welcome_page_renders(client):
    assert client.get("/welcome").status_code == 200


def _check_tag(tag):
    import subprocess
    return subprocess.run([str(ROOT / "scripts" / "check_tag_version.sh"), tag],
                          capture_output=True, text=True)


def test_a_release_tag_must_match_the_version():
    from app.version import VERSION
    assert _check_tag("v" + VERSION).returncode == 0
    wrong = _check_tag("v0.0.1")
    assert wrong.returncode == 1 and "does not match" in wrong.stderr


def test_the_release_workflow_runs_that_check_on_tag_pushes_only():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "scripts/check_tag_version.sh" in text
    block = text[text.index("Tag matches VERSION"):]
    assert "github.event_name == 'push'" in block.split("run:")[0]
