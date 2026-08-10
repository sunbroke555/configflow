import unittest

import yaml

from backend.converters.mosdns import generate_mosdns_config


class MosdnsCacheConfigTest(unittest.TestCase):
    @staticmethod
    def _cache_plugin(mosdns_settings):
        generated = generate_mosdns_config({'mosdns': mosdns_settings})
        config = yaml.safe_load(generated)
        return next(
            (plugin for plugin in config['plugins'] if plugin.get('tag') == 'lazy_cache'),
            None
        )

    def test_cache_persistence_can_be_disabled(self):
        plugin = self._cache_plugin({
            'cache_enabled': True,
            'cache_dump_enabled': False,
        })

        self.assertIsNotNone(plugin)
        self.assertNotIn('dump_file', plugin['args'])
        self.assertNotIn('dump_interval', plugin['args'])

    def test_cache_persistence_defaults_remain_compatible(self):
        plugin = self._cache_plugin({'cache_enabled': True})

        self.assertEqual(plugin['args']['dump_file'], './cache.dump')
        self.assertEqual(plugin['args']['dump_interval'], 300)

    def test_cache_plugin_can_still_be_disabled(self):
        plugin = self._cache_plugin({'cache_enabled': False})

        self.assertIsNone(plugin)


if __name__ == '__main__':
    unittest.main()
