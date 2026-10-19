// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { oxidnsGlobalSchema, oxidnsPluginCatalog } from '../src/lib/oxidnsSchema';
import {
  getOxidnsGlobalCategory,
  getOxidnsPluginCategory,
  oxidnsFeatureCategories,
} from '../src/lib/oxidnsFeatureNavigation';

describe('OxiDNS functional navigation categories', () => {
  it('classifies every supported global section and plugin into a visible category', () => {
    const categoryIds = new Set(oxidnsFeatureCategories.map(category => category.id));
    for (const key of Object.keys(oxidnsGlobalSchema.properties ?? {})) {
      const category = getOxidnsGlobalCategory(key);
      expect(category, key).not.toBe('advanced');
      expect(categoryIds.has(category), key).toBe(true);
    }
    expect(oxidnsPluginCatalog).toHaveLength(65);
    for (const plugin of oxidnsPluginCatalog) {
      const category = getOxidnsPluginCategory(plugin.type);
      expect(category, plugin.type).not.toBe('advanced');
      expect(categoryIds.has(category), plugin.type).toBe(true);
      if (plugin.category === 'server') expect(category).toBe('listeners');
      if (plugin.category === 'matcher') expect(category).toBe('routing');
      if (plugin.category === 'provider') expect(category).toBe('resources');
    }
  });

  it('keeps upstreams, caches and execution chains in distinct functional groups', () => {
    expect(getOxidnsPluginCategory('forward')).toBe('upstreams');
    expect(getOxidnsPluginCategory('cache')).toBe('cache');
    expect(getOxidnsPluginCategory('reverse_lookup')).toBe('cache');
    expect(getOxidnsPluginCategory('sequence')).toBe('routing');
    expect(getOxidnsPluginCategory('fallback')).toBe('routing');
    expect(getOxidnsPluginCategory('ip_selector')).toBe('features');
    expect(getOxidnsPluginCategory('ros_address_list')).toBe('features');
    expect(getOxidnsGlobalCategory('api')).toBe('runtime');
    expect(getOxidnsGlobalCategory('network')).toBe('network');
    expect(getOxidnsGlobalCategory('include')).toBe('resources');
  });

  it('routes unknown and prototype-like names to extension editing without changing them', () => {
    const config = {
      future_global: { enabled: false, count: 0, extra: null },
      plugins: [{ tag: 'custom', type: 'future_plugin', args: { nested: ['preserved'] } }],
    };
    const before = JSON.stringify(config);
    expect(getOxidnsGlobalCategory('future_global')).toBe('advanced');
    expect(getOxidnsPluginCategory(config.plugins[0].type)).toBe('advanced');
    for (const name of ['', '__proto__', 'constructor', 'toString']) {
      expect(getOxidnsGlobalCategory(name)).toBe('advanced');
      expect(getOxidnsPluginCategory(name)).toBe('advanced');
    }
    expect(JSON.stringify(config)).toBe(before);
  });
});
