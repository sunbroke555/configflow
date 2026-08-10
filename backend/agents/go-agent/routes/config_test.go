package routes

import "testing"

func TestMosdnsCacheDumpFile(t *testing.T) {
	tests := []struct {
		name     string
		config   string
		expected string
	}{
		{
			name: "persistence enabled",
			config: `plugins:
  - tag: lazy_cache
    type: cache
    args:
      size: 10240
      dump_file: ./cache.dump
      dump_interval: 300
`,
			expected: "./cache.dump",
		},
		{
			name: "persistence disabled",
			config: `plugins:
  - tag: lazy_cache
    type: cache
    args:
      size: 10240
`,
			expected: "",
		},
		{
			name: "cache disabled",
			config: `plugins:
  - tag: forward_local
    type: forward
`,
			expected: "",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			actual, err := mosdnsCacheDumpFile(test.config)
			if err != nil {
				t.Fatalf("mosdnsCacheDumpFile returned an error: %v", err)
			}
			if actual != test.expected {
				t.Fatalf("mosdnsCacheDumpFile() = %q, want %q", actual, test.expected)
			}
		})
	}
}
