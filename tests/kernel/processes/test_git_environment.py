"""Git subprocess environment authority tests."""

from hyprial.kernel import git_subprocess_environment


def test_git_subprocess_environment_removes_every_git_setting() -> None:
    source = {
        "PATH": "/bin",
        "HOME": "/home/worker",
        "GIT_DIR": "/outside/repository",
        "GIT_WORK_TREE": "/outside/worktree",
        "GIT_INDEX_FILE": "/outside/index",
        "GIT_OBJECT_DIRECTORY": "/outside/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/outside/alternate",
        "GIT_COMMON_DIR": "/outside/common",
        "GIT_CEILING_DIRECTORIES": "/outside/ceiling",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": "/outside/configured-worktree",
        "GIT_FUTURE_REPOSITORY_SELECTOR": "/outside/future",
    }

    assert git_subprocess_environment(source) == {
        "PATH": "/bin",
        "HOME": "/home/worker",
    }
    assert source["GIT_DIR"] == "/outside/repository"
