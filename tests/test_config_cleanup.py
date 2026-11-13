import hashlib

import yaml

from hermes_memory.config_cleanup import remove_hybrid_age_config, remove_owned_env_values


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def test_env_cleanup_preserves_comments_order_and_changed_values(tmp_path):
    path = tmp_path / '.env'
    path.write_text('# heading\nHYBRID_AGE_DSN=original\nOTHER=yes\nHYBRID_AGE_GRAPH=edited\n# end\n')
    remove_owned_env_values(path, {'HYBRID_AGE_DSN': fingerprint('original'),
                                   'HYBRID_AGE_GRAPH': fingerprint('old')})
    assert path.read_text() == '# heading\nOTHER=yes\nHYBRID_AGE_GRAPH=edited\n# end\n'


def test_yaml_cleanup_preserves_unrelated_config_and_current_provider(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('memory:\n  provider: other\n  limit: 5\nhybrid_age:\n  graph: old\nother: value\n')
    remove_hybrid_age_config(path)
    assert yaml.safe_load(path.read_text()) == {'memory': {'provider': 'other', 'limit': 5}, 'other': 'value'}


def test_yaml_cleanup_resets_owned_provider(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('memory:\n  provider: hybrid-age\nhybrid_age: {}\n')
    remove_hybrid_age_config(path)
    assert yaml.safe_load(path.read_text()) == {'memory': {'provider': 'built-in'}}


def test_yaml_alias_does_not_change_unrelated_mapping(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('memory: &settings\n  provider: hybrid-age\n  limit: 5\nother: *settings\nhybrid_age: {}\n')
    remove_hybrid_age_config(path)
    result = yaml.safe_load(path.read_text())
    assert result['memory'] == {'provider': 'built-in', 'limit': 5}
    assert result['other'] == {'provider': 'hybrid-age', 'limit': 5}
