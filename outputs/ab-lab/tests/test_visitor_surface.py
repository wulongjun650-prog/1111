from ablab.models import Config
from ablab.store import Store


def test_new_site_defaults_to_slot_a(tmp_path):
    config, _ = Store(tmp_path).config()
    assert config.allowed_slot == 'A'
