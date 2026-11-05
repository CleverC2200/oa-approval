from __future__ import annotations

import json

import pytest

from apps.oa.approvals import finance_precheck


def test_policy_is_private_to_the_auth_configuration_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(finance_precheck, 'stable_env_file', lambda: tmp_path / 'oa-auth.env')
    path = tmp_path / 'prediction-finance-precheck.json'
    assert finance_precheck.policy_path() == path
    assert finance_precheck.load_policy() is None
    policy = {'leader': {'id': '100', 'name': '测试高层'},
              'initiators': [{'id': '4', 'name': '名单内发起人'}]}
    path.write_text(json.dumps(policy), encoding='utf-8')
    assert finance_precheck.load_policy() == policy


@pytest.mark.parametrize('text', ['bad-json', '{}',
    '{"leader":{"id":"100","name":"测试"},"initiators":[{"id":"4","name":"甲"},{"id":"4","name":"乙"}]}',
    '{"leader":{"id":"0","name":"测试"},"initiators":[{"id":"4","name":"甲"}]}',
])
def test_invalid_policy_is_not_silently_used_as_an_empty_list(monkeypatch, tmp_path, text):
    path = tmp_path / 'prediction-finance-precheck.json'
    path.write_text(text, encoding='utf-8')
    monkeypatch.setattr(finance_precheck, 'policy_path', lambda: path)
    with pytest.raises(ValueError, match='^finance_precheck_configuration_invalid$'):
        finance_precheck.load_policy()
