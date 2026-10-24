import os

import pytest

from apps.oa.runtime import environment


def test_local_model_configuration_keeps_private_file_and_process_precedence(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    stable = tmp_path / 'auth.env'
    stable.write_text('OA_REVIEW_API_KEY=fake-model-key\nOA_UID=77\n')
    stable.chmod(0o600)
    worktree = project / '.env'
    worktree.write_text('OA_REVIEW_API_KEY=fake-worktree-key\n')
    worktree.chmod(0o600)
    monkeypatch.setattr(environment, 'PROJECT_ROOT', project)
    monkeypatch.setattr(environment.load_local_env, '_loaded', False, raising=False)
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(stable))
    monkeypatch.delenv('OA_REVIEW_API_KEY', raising=False)
    monkeypatch.setenv('OA_UID', '88')
    try:
        assert environment.env('OA_REVIEW_API_KEY') == 'fake-model-key'
        assert environment.env('OA_UID') == '88'
    finally:
        os.environ.pop('OA_REVIEW_API_KEY', None)


def test_local_environment_does_not_accept_a_public_credentials_file(tmp_path, monkeypatch):
    stable = tmp_path / 'auth.env'
    stable.write_text('OA_REVIEW_API_KEY=fake-model-key\n')
    stable.chmod(0o644)
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(stable))
    monkeypatch.setattr(environment.load_local_env, '_loaded', False, raising=False)
    with pytest.raises(RuntimeError, match='oa_auth_file_permissions_invalid'):
        environment.env('OA_REVIEW_API_KEY')
